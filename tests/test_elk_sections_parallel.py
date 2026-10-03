"""Section scheduling uses bounded leases without running ELK or real graphs."""

import copy
import threading
import unittest
from concurrent.futures import CancelledError
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.elk_errors import ElkWorkerFailure
from liquid_tracer.elk_sections_parallel import iter_sections


MODULE = "liquid_tracer.elk_sections_parallel"


def requests(count=5):
    return [{"children": [{}] * (10 - index), "edges": [{}] * (12 + index % 2),
             "centerNodeOrder": [f"opaque-{index}"], "branchProfile": "preserve-me",
             "boundaryOrdering": bool(index % 2)} for index in range(count)]


def budget(attempts, *, peak_rss_mb=None, **kwargs):
    count = 1 if peak_rss_mb is None else min(3, attempts)
    return count, 2048 * count, 2048


class FakeLease:
    resource_lease_fd = 71
    active_layouts = 1
    machine_heap_mb = 8192

    def __init__(self, owner, workers, heap):
        self.owner = owner
        self.worker_count = workers
        self.total_heap_mb = heap
        self.heap_mb = heap // workers

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.owner.leased = False


class FakeResources:
    def __init__(self):
        self.leased = False
        self.closed = False
        self.calls = []
        self.worker_limit = 64

    def __enter__(self):
        return self

    def __exit__(self, *_):
        assert not self.leased, "Resource lease outlived worker batch"
        self.closed = True

    def acquire(self, workers, heap, **kwargs):
        assert not self.leased, "Nested resource leases"
        self.calls.append((workers, heap, kwargs))
        actual_workers = min(workers, self.worker_limit)
        lease = FakeLease(self, actual_workers, heap // workers * actual_workers)
        kwargs["progress"]({"stage": "resource_allocated", "worker_count": actual_workers})
        self.leased = True
        return lease


class SectionWorkersTests(unittest.TestCase):
    def setUp(self):
        self.resources = FakeResources()
        self.factory = self.enterContext(patch(MODULE + ".SharedRenderResources", return_value=self.resources))
        self.budget = self.enterContext(patch(MODULE + ".elk_worker_budget", side_effect=budget))
        self.enterContext(patch(MODULE + ".renderer_heap_mb", return_value=8192))
        self.enterContext(patch(MODULE + ".renderer_heap_is_auto", return_value=True))

    def test_measured_pilot_then_bounded_parallel_work_preserves_requests_and_delivery_order(self):
        inputs = requests()
        original = copy.deepcopy(inputs)
        indices = {id(request): index for index, request in enumerate(inputs)}
        barrier = threading.Barrier(3)
        second_finished = threading.Event()
        pilot_finished = threading.Event()
        lock = threading.Lock()
        active, completed, callbacks = {}, [], []
        max_workers = max_heap = 0
        caller = threading.get_ident()

        def progress(event):
            callbacks.append((threading.get_ident(), event))

        def worker(request, seeds, *, heap_mb, progress, cancel_event, resource_lease_fd):
            nonlocal max_workers, max_heap
            index = indices[id(request)]
            self.assertEqual(seeds, [17])
            self.assertEqual(resource_lease_fd, 71)
            self.assertTrue(self.resources.leased)
            with lock:
                active[index] = heap_mb
                max_workers = max(max_workers, len(active))
                max_heap = max(max_heap, sum(active.values()))
            try:
                if index == 0:
                    self.assertEqual(len(active), 1)
                    pilot_finished.set()
                else:
                    self.assertTrue(pilot_finished.is_set())
                if index in (1, 2, 3):
                    barrier.wait(timeout=5)
                if index == 1:
                    self.assertTrue(second_finished.wait(5))
                self.assertFalse(cancel_event.is_set())
                progress({"stage": "memory_measured", "peak_rss_mb": 1024 if index == 3 else 512})
                completed.append(index)
                if index == 2:
                    second_finished.set()
                return [{"section": index}]
            finally:
                with lock:
                    active.pop(index)

        metadata = {}
        output = list(iter_sections(inputs, 17, worker, progress, metadata))
        self.assertEqual(output, [(index + 1, [{"section": index}]) for index in range(5)])
        self.assertLess(completed.index(2), completed.index(1))
        self.assertEqual((max_workers, max_heap), (3, 6144))
        self.assertEqual(inputs, original)
        self.assertTrue(all(thread == caller for thread, _ in callbacks))
        self.assertTrue(all(1 <= event["section_index"] <= 5 and event["section_total"] == 5
                            and "worker_count" in event for _, event in callbacks))
        self.assertFalse(any("opaque-" in str(event) for _, event in callbacks))
        self.assertEqual([call.kwargs["peak_rss_mb"] for call in self.budget.call_args_list], [None, 512, 1024])
        self.assertEqual([(call.kwargs["node_count"], call.kwargs["edge_count"])
                          for call in self.budget.call_args_list], [(10, 13), (9, 13), (6, 12)])
        self.assertEqual(metadata, {"execution": "parallel", "worker_count": 3,
                                    "section_count": 5, "memory_retry_count": 0, "peak_rss_mb": 1024})
        self.assertTrue(self.resources.closed)

    def test_missing_or_invalid_peak_keeps_workers_serial(self):
        for peak in (None, True, -1, 0, "512", 2147483648):
            with self.subTest(peak=peak):
                self.budget.reset_mock()
                calls = []

                def worker(request, seeds, *, progress, **kwargs):
                    calls.append(request)
                    progress({"stage": "memory_measured", "peak_rss_mb": peak})
                    return []

                metadata = {}
                list(iter_sections(requests(3), 17, worker, None, metadata))
                self.assertEqual(len(calls), 3)
                self.assertTrue(all(call.kwargs["peak_rss_mb"] is None for call in self.budget.call_args_list))
                self.assertEqual(metadata["worker_count"], 1)
                self.assertNotIn("peak_rss_mb", metadata)

    def test_resource_coordinator_can_reduce_requested_worker_count(self):
        self.resources.worker_limit = 1

        def worker(request, seeds, *, progress, **kwargs):
            progress({"stage": "memory_measured", "peak_rss_mb": 512})
            return []

        metadata = {}
        output = list(iter_sections(requests(4), 17, worker, None, metadata))
        self.assertEqual([index for index, _ in output], [1, 2, 3, 4])
        self.assertEqual([workers for workers, _, _ in self.resources.calls], [1, 3, 2, 1])
        self.assertEqual(metadata["execution"], "sequential")
        self.assertEqual(metadata["worker_count"], 1)

    def test_memory_failure_retries_once_exclusively_after_siblings_drain(self):
        inputs = requests()
        indices = {id(request): index for index, request in enumerate(inputs)}
        barrier = threading.Barrier(3)
        lock = threading.Lock()
        active, calls = set(), []
        events, metadata = [], {}

        def worker(request, seeds, *, progress, heap_mb, **kwargs):
            index = indices[id(request)]
            with lock:
                active.add(index)
                calls.append((index, heap_mb))
                if heap_mb == 8192:
                    self.assertEqual(active, {index})
            try:
                if index in (1, 2, 3) and heap_mb == 2048:
                    barrier.wait(timeout=5)
                if index == 1:
                    # A failed retry is reported once, without losing siblings.
                    raise ElkWorkerFailure("private diagnostic", failure_code="heap_exhausted")
                progress({"stage": "memory_measured", "peak_rss_mb": 512})
                return [{"section": index}]
            finally:
                with lock:
                    active.remove(index)

        output = list(iter_sections(inputs, 17, worker, events.append, metadata))
        self.assertEqual([index for index, _ in output], [1, 2, 3, 4, 5])
        self.assertIsInstance(output[1][1], ElkWorkerFailure)
        self.assertEqual([heap for index, heap in calls if index == 1], [2048, 8192])
        self.assertEqual([heap for index, heap in calls if index == 4], [8192])
        self.assertEqual(sum(kwargs.get("exclusive", False) for _, _, kwargs in self.resources.calls), 1)
        self.assertEqual(metadata["memory_retry_count"], 1)
        self.assertTrue(any(event["stage"] == "retrying_memory" and event["section_index"] == 2 for event in events))
        self.assertFalse(any("private diagnostic" in str(event) for event in events))

    def test_failed_pilot_memory_retry_uses_fresh_successful_measurement(self):
        inputs = requests(3)
        calls, events, metadata = [], [], {}

        def worker(request, seeds, *, progress, heap_mb, **kwargs):
            calls.append((inputs.index(request), heap_mb))
            if request is inputs[0] and heap_mb == 2048:
                progress({"stage": "memory_measured", "peak_rss_mb": 30000})
                raise ElkWorkerFailure("pilot exhausted heap", failure_code="heap_exhausted")
            progress({"stage": "memory_measured", "peak_rss_mb": 700})
            return []

        self.assertEqual(list(iter_sections(inputs, 17, worker, events.append, metadata)),
                         [(1, []), (2, []), (3, [])])
        self.assertEqual(calls, [(0, 2048), (0, 8192), (1, 8192), (2, 8192)])
        self.assertEqual(metadata["peak_rss_mb"], 700)
        self.assertEqual(metadata["memory_retry_count"], 1)
        retry = self.resources.calls[1][2]
        self.assertTrue(retry["exclusive"])
        self.assertTrue(callable(retry["refresh_heap"]))

    def test_failed_pilot_measurement_is_not_used_for_subsequent_budget(self):
        inputs = requests(3)

        def worker(request, seeds, *, progress, **kwargs):
            if request is inputs[0]:
                progress({"stage": "memory_measured", "peak_rss_mb": 30000})
                raise ElkWorkerFailure("failed pilot", failure_code="elk_engine_error")
            progress({"stage": "memory_measured", "peak_rss_mb": 512})
            return []

        output = list(iter_sections(inputs, 17, worker, None, {}))
        self.assertIsInstance(output[0][1], ElkWorkerFailure)
        self.assertEqual([call.kwargs["peak_rss_mb"] for call in self.budget.call_args_list], [None, None, 512])
        self.assertFalse(any(kwargs.get("exclusive") for _, _, kwargs in self.resources.calls))

    def test_fatal_errors_and_cancellation_drain_siblings_and_stop_future_batches(self):
        for error in (TraceError("invalid response"), CancelledError(), KeyboardInterrupt(),
                      ElkWorkerFailure("invalid output", failure_code="elk_invalid_output")):
            with self.subTest(error=type(error).__name__):
                inputs = requests()
                indices = {id(request): index for index, request in enumerate(inputs)}
                barrier = threading.Barrier(3)
                started, stopped = [], []

                def worker(request, seeds, *, progress, cancel_event, **kwargs):
                    index = indices[id(request)]
                    started.append(index)
                    if index:
                        barrier.wait(timeout=5)
                        if index == 1:
                            raise error
                        self.assertTrue(cancel_event.wait(5), "Sibling was not cancelled")
                        stopped.append(index)
                    progress({"stage": "memory_measured", "peak_rss_mb": 512})
                    return []

                with self.assertRaises(type(error)):
                    list(iter_sections(inputs, 17, worker, None, {}))
                self.assertCountEqual(started, [0, 1, 2, 3])
                self.assertCountEqual(stopped, [2, 3])
                self.assertFalse(self.resources.leased)

    def test_progress_cancellation_stops_running_worker(self):
        stopped = threading.Event()

        def worker(request, seeds, *, progress, cancel_event, **kwargs):
            progress({"stage": "calculating"})
            self.assertTrue(cancel_event.wait(5), "Progress cancellation did not reach worker")
            stopped.set()
            return []

        def progress(event):
            if event["stage"] == "calculating":
                raise CancelledError()

        with self.assertRaises(CancelledError):
            list(iter_sections(requests(1), 17, worker, progress, {}))
        self.assertTrue(stopped.is_set())
        self.assertFalse(self.resources.leased)

    def test_closing_iterator_after_result_releases_batch_and_does_not_start_more_work(self):
        calls = []

        def worker(request, seeds, *, progress, **kwargs):
            calls.append(request)
            progress({"stage": "memory_measured", "peak_rss_mb": 512})
            return []

        iterator = iter_sections(requests(), 17, worker, None, {})
        self.assertEqual(next(iterator), (1, []))
        self.assertFalse(self.resources.leased)
        iterator.close()
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.resources.closed)

    def test_empty_sections_do_not_acquire_resources_or_start_workers(self):
        metadata = {}
        self.assertEqual(list(iter_sections([], 17, None, None, metadata)), [])
        self.factory.assert_not_called()
        self.assertEqual(metadata["section_count"], 0)


if __name__ == "__main__":
    unittest.main()
