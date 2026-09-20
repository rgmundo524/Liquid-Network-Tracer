"""ELK attempts overlap without changing quality selection or multiplying memory."""

import copy
import threading
import time
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.elk_errors import ElkWorkerFailure
from liquid_tracer.elk_layout import optimize_graph
from liquid_tracer.layout_search import layout_seeds
from tests.test_elk_layout import crossing_graph, synthetic_candidate
from tests.test_layout_search import disconnected_graph


BUDGET = "liquid_tracer.elk_parallel.elk_worker_budget"
FULL_HEAP = "liquid_tracer.elk_parallel.renderer_heap_mb"


def measured_candidate(request, seeds, progress=None, **kwargs):
    if progress:
        progress({"phase": "optimizing", "stage": "memory_measured", "peak_rss_mb": 512})
    return synthetic_candidate(request, seeds)


def budget_for(workers, total):
    def budget(attempts, *, peak_rss_mb=None):
        count = min(workers, attempts) if peak_rss_mb is not None else 1
        return count, total, total // count
    return budget


class ParallelLayoutTests(unittest.TestCase):
    def test_workers_overlap_with_bounded_concurrency_and_shared_heap(self):
        first_batch = threading.Barrier(3)
        lock = threading.Lock()
        calls, active_heaps = [], {}
        peak_workers = peak_heap = 0
        pilot_finished = threading.Event()
        seeds = layout_seeds(7)

        def worker(request, requested, *, heap_mb, cancel_event=None, progress=None, **kwargs):
            nonlocal peak_workers, peak_heap
            seed = requested[0]
            if seed != seeds[0]:
                self.assertTrue(pilot_finished.is_set(), "Another attempt started before the pilot finished")
            with lock:
                calls.append(seed)
                active_heaps[seed] = heap_mb
                peak_workers = max(peak_workers, len(active_heaps))
                peak_heap = max(peak_heap, sum(active_heaps.values()))
            try:
                if seed in seeds[1:4]:
                    first_batch.wait(timeout=5)
                self.assertFalse(cancel_event and cancel_event.is_set())
                if seed == seeds[0]:
                    self.assertEqual(heap_mb, 12000)
                    self.assertEqual(len(active_heaps), 1)
                    pilot_finished.set()
                return measured_candidate(request, requested, progress=progress)
            finally:
                with lock:
                    active_heaps.pop(seed)

        with patch(BUDGET, side_effect=budget_for(3, 12000)), \
                patch("liquid_tracer.elk_layout._worker", side_effect=worker):
            result = optimize_graph(crossing_graph(), layout_attempts=7)
        self.assertCountEqual(calls, seeds)
        self.assertEqual(peak_workers, 3)
        self.assertEqual(peak_heap, 12000)
        self.assertEqual(result["layout"]["search"]["execution"], "parallel")
        self.assertEqual(result["layout"]["search"]["worker_count"], 3)
        self.assertEqual(result["layout"]["search"]["successful_count"], 7)
        self.assertEqual(result["layout"]["search"]["selected_seed"], seeds[0])

    def test_missing_memory_measurement_keeps_attempts_serial(self):
        heaps, threads = [], []

        def worker(request, seeds, *, heap_mb, **kwargs):
            heaps.append(heap_mb)
            threads.append(threading.get_ident())
            return synthetic_candidate(request, seeds)

        with patch(BUDGET, side_effect=budget_for(4, 16000)) as budget, \
                patch("liquid_tracer.elk_layout._worker", side_effect=worker):
            result = optimize_graph(crossing_graph(), layout_attempts=5)
        self.assertEqual(heaps, [16000] * 5)
        self.assertEqual(threads, [threading.get_ident()] * 5)
        self.assertTrue(all(call.kwargs["peak_rss_mb"] is None for call in budget.call_args_list))
        self.assertEqual(result["layout"]["search"]["execution"], "sequential")
        self.assertEqual(result["layout"]["search"]["worker_count"], 1)

    def test_heavier_later_attempt_updates_measurement_before_next_batch(self):
        calls, budget_peaks = [], []
        first_batch = threading.Barrier(3)
        seeds = layout_seeds(7)

        def budget(attempts, *, peak_rss_mb=None):
            budget_peaks.append(peak_rss_mb)
            count = 3 if peak_rss_mb is not None and peak_rss_mb < 3000 else 1
            count = min(count, attempts)
            return count, 12000, 12000 // count

        def worker(request, requested, *, heap_mb, progress, **kwargs):
            seed = requested[0]
            calls.append((seed, heap_mb))
            if seed in seeds[1:4]:
                first_batch.wait(timeout=5)
            progress({"stage": "memory_measured", "peak_rss_mb": 3000 if seed == 7 else 512})
            return synthetic_candidate(request, requested)

        with patch(BUDGET, side_effect=budget), \
                patch("liquid_tracer.elk_layout._worker", side_effect=worker):
            result = optimize_graph(crossing_graph(), layout_attempts=7)
        self.assertEqual(budget_peaks, [None, 512, 3000, 3000, 3000])
        self.assertTrue(all(heap == 12000 for seed, heap in calls if seed in seeds[4:]))
        self.assertEqual(result["layout"]["search"]["peak_rss_mb"], 3000)
        self.assertEqual(result["layout"]["search"]["successful_count"], 7)

    def test_failed_pilot_does_not_supply_the_memory_estimate(self):
        def worker(request, seeds, progress=None, **kwargs):
            if seeds == [1]:
                progress({"stage": "memory_measured", "peak_rss_mb": 30000})
                raise ElkWorkerFailure("Synthetic pilot failure", failure_code="elk_engine_error")
            return measured_candidate(request, seeds, progress=progress)

        with patch(BUDGET, side_effect=budget_for(3, 12000)) as budget, \
                patch("liquid_tracer.elk_layout._worker", side_effect=worker):
            result = optimize_graph(crossing_graph(), layout_attempts=5)
        self.assertEqual([call.kwargs["peak_rss_mb"] for call in budget.call_args_list], [None, None, 512])
        self.assertEqual(result["layout"]["search"]["peak_rss_mb"], 512)
        self.assertEqual(result["layout"]["search"]["successful_count"], 4)
        self.assertEqual(result["layout"]["search"]["failed_count"], 1)

    def test_thousand_object_graph_runs_every_requested_seed(self):
        graph = disconnected_graph(1001)
        original = copy.deepcopy(graph)
        observed = []

        def worker(request, seeds, progress=None, **kwargs):
            observed.append((seeds[0], len(request["children"]), request["branchProfile"]))
            return measured_candidate(request, seeds, progress=progress)

        with patch(BUDGET, side_effect=budget_for(4, 16000)), \
                patch("liquid_tracer.elk_layout._worker", side_effect=worker):
            result = optimize_graph(graph, layout_attempts=25)
        self.assertCountEqual([seed for seed, _, _ in observed], layout_seeds(25))
        self.assertTrue(all(count == 1001 and profile == "flow_weighted"
                            for _, count, profile in observed))
        self.assertEqual(result["layout"]["search"]["attempted_count"], 25)
        self.assertEqual(result["layout"]["search"]["successful_count"], 25)
        self.assertEqual([node["id"] for node in result["nodes"]],
                         [node["id"] for node in graph["nodes"]])
        self.assertEqual(graph, original)

    def test_out_of_order_completion_has_same_winner_as_serial_search(self):
        seeds = layout_seeds(4)
        finished, release_first = [], threading.Event()

        def worker(request, requested, progress=None, **kwargs):
            seed = requested[0]
            if seed == seeds[1]:
                self.assertTrue(release_first.wait(timeout=5))
            candidates = synthetic_candidate(request, requested)
            if seed == seeds[0]:
                for node in candidates[0]["nodes"]:
                    node.update(x=0, y=0)
            # Later candidates tie; the earlier scheduled seed must win even
            # when its sibling completes first. The pilot is deliberately worse.
            finished.append(seed)
            if seed == seeds[2]:
                release_first.set()
            if progress:
                progress({"stage": "memory_measured", "peak_rss_mb": 512})
            return candidates

        with patch(BUDGET, side_effect=budget_for(2, 8000)), \
                patch("liquid_tracer.elk_layout._worker", side_effect=worker):
            parallel = optimize_graph(crossing_graph(), layout_attempts=4)
        with patch(BUDGET, side_effect=budget_for(1, 8000)), \
                patch("liquid_tracer.elk_layout._worker", side_effect=worker):
            serial = optimize_graph(crossing_graph(), layout_attempts=4)
        self.assertLess(finished.index(seeds[2]), finished.index(seeds[1]))
        self.assertEqual(parallel["layout"]["search"]["selected_seed"], seeds[1])
        self.assertEqual(parallel["nodes"], serial["nodes"])
        self.assertEqual(parallel["edges"], serial["edges"])
        self.assertEqual(parallel["layout"]["metrics"], serial["layout"]["metrics"])

    def test_progress_callbacks_run_serially_on_the_calling_thread(self):
        start = threading.Barrier(3)
        caller = threading.get_ident()
        callbacks = []
        callback_active = False

        def report(event):
            nonlocal callback_active
            self.assertFalse(callback_active, "Progress callbacks overlapped")
            callback_active = True
            callbacks.append((threading.get_ident(), event))
            time.sleep(.001)
            callback_active = False

        def worker(request, seeds, *, progress, **kwargs):
            if seeds != [1]:
                start.wait(timeout=5)
            for tick in range(4):
                progress({"stage": "calculating", "message": "Synthetic heartbeat",
                          "elapsed_seconds": tick})
            return measured_candidate(request, seeds, progress=progress)

        with patch(BUDGET, side_effect=budget_for(3, 9000)), \
                patch("liquid_tracer.elk_layout._worker", side_effect=worker):
            optimize_graph(crossing_graph(), layout_attempts=4, progress=report)
        self.assertTrue(callbacks)
        self.assertTrue(all(thread_id == caller for thread_id, _ in callbacks))
        heartbeats = [event for _, event in callbacks if event.get("message") == "Synthetic heartbeat"]
        self.assertEqual(len(heartbeats), 16)
        self.assertCountEqual([event["seed"] for event in heartbeats], list(layout_seeds(4)) * 4)
        self.assertTrue(all(event["seed"] == layout_seeds(4)[event["attempt_index"] - 1]
                            for event in heartbeats))

    def test_memory_failure_retries_alone_with_full_heap_and_keeps_later_seeds_serial(self):
        for failure_code in ("heap_exhausted", "memory_exhausted", "worker_killed"):
            with self.subTest(failure_code=failure_code):
                barrier, lock = threading.Barrier(2), threading.Lock()
                active, calls, events = set(), [], []

                def worker(request, seeds, *, heap_mb, progress=None, **kwargs):
                    seed = seeds[0]
                    with lock:
                        active.add(seed)
                        calls.append((seed, heap_mb, len(active)))
                    try:
                        if heap_mb == 4000:
                            barrier.wait(timeout=5)
                            if seed == 7:
                                raise ElkWorkerFailure("Private worker detail", failure_code=failure_code)
                        else:
                            self.assertEqual(heap_mb, 8000)
                            self.assertEqual(len(active), 1, "Full-budget retry overlapped a sibling")
                        return measured_candidate(request, seeds, progress=progress)
                    finally:
                        with lock:
                            active.remove(seed)

                with patch(BUDGET, side_effect=budget_for(2, 8000)), \
                        patch(FULL_HEAP, return_value=8000), \
                        patch("liquid_tracer.elk_layout._worker", side_effect=worker):
                    result = optimize_graph(crossing_graph(), layout_attempts=5, progress=events.append)
                self.assertEqual([seed for seed, _, _ in calls].count(7), 2)
                self.assertCountEqual([seed for seed, _, _ in calls], [7, *layout_seeds(5)])
                self.assertEqual([heap for seed, heap, _ in calls if seed == 7], [4000, 8000])
                self.assertTrue(all(heap == 8000 and concurrent == 1
                                    for seed, heap, concurrent in calls if seed not in (7, 19)))
                search = result["layout"]["search"]
                self.assertEqual(search["successful_count"], 5)
                self.assertEqual(search["failed_count"], 0)
                self.assertEqual(search["memory_retry_count"], 1)
                self.assertTrue(any(event["stage"] == "retrying_memory" for event in events))

    def test_failed_memory_retry_is_counted_once_and_other_results_survive(self):
        def worker(request, seeds, progress=None, **kwargs):
            if seeds == [7]:
                raise ElkWorkerFailure("Private worker detail", failure_code="heap_exhausted")
            return measured_candidate(request, seeds, progress=progress)

        with patch(BUDGET, side_effect=budget_for(2, 8000)), \
                patch(FULL_HEAP, return_value=8000), \
                patch("liquid_tracer.elk_layout._worker", side_effect=worker) as mock_worker:
            result = optimize_graph(crossing_graph(), layout_attempts=4)
        search = result["layout"]["search"]
        self.assertEqual(mock_worker.call_count, 5)
        self.assertEqual(search["attempted_count"], 4)
        self.assertEqual(search["successful_count"], 3)
        self.assertEqual(search["failed_count"], 1)
        self.assertEqual(search["failed_attempts"], [
            {"attempt_index": 2, "seed": 7, "failure_code": "heap_exhausted"}])
        self.assertNotEqual(search["selected_seed"], 7)

    def test_engine_failure_does_not_stop_other_seeds_or_trigger_memory_retry(self):
        def worker(request, seeds, progress=None, **kwargs):
            if seeds == [7]:
                raise ElkWorkerFailure("Private worker detail", failure_code="elk_engine_error")
            return measured_candidate(request, seeds, progress=progress)

        with patch(BUDGET, side_effect=budget_for(3, 9000)), \
                patch("liquid_tracer.elk_layout._worker", side_effect=worker) as mock_worker:
            result = optimize_graph(crossing_graph(), layout_attempts=5)
        self.assertEqual(mock_worker.call_count, 5)
        self.assertEqual(result["layout"]["search"]["failed_count"], 1)
        self.assertEqual(result["layout"]["search"]["successful_count"], 4)
        self.assertEqual(result["layout"]["search"]["memory_retry_count"], 0)

    def test_fatal_error_and_cancellation_stop_active_siblings_and_unscheduled_seeds(self):
        for error in (TraceError("Invalid generated request"), KeyboardInterrupt(), SystemExit()):
            with self.subTest(error=type(error).__name__):
                sibling_started = threading.Event()
                sibling_stopped = threading.Event()
                started = []
                graph = crossing_graph()
                original = copy.deepcopy(graph)

                def worker(request, seeds, *, cancel_event=None, progress=None, **kwargs):
                    started.append(seeds[0])
                    if seeds == [1]:
                        return measured_candidate(request, seeds, progress=progress)
                    if seeds == [7]:
                        self.assertTrue(sibling_started.wait(timeout=5))
                        raise error
                    sibling_started.set()
                    self.assertTrue(cancel_event.wait(timeout=5), "Sibling was not cancelled")
                    sibling_stopped.set()
                    return measured_candidate(request, seeds, progress=progress)

                with patch(BUDGET, side_effect=budget_for(2, 8000)), \
                        patch("liquid_tracer.elk_layout._worker", side_effect=worker), \
                        self.assertRaises(type(error)):
                    optimize_graph(graph, layout_attempts=5)
                self.assertCountEqual(started, [1, 7, 19])
                self.assertTrue(sibling_stopped.is_set())
                self.assertEqual(graph, original)

    def test_invalid_geometry_stops_before_the_next_batch(self):
        def worker(request, seeds, progress=None, **kwargs):
            candidates = synthetic_candidate(request, seeds)
            if seeds == [19]:
                candidates[0]["nodes"].pop()
            if progress:
                progress({"stage": "memory_measured", "peak_rss_mb": 512})
            return candidates

        with patch(BUDGET, side_effect=budget_for(2, 8000)), \
                patch("liquid_tracer.elk_layout._worker", side_effect=worker) as mock_worker, \
                self.assertRaises(TraceError):
            optimize_graph(crossing_graph(), layout_attempts=5)
        self.assertCountEqual([call.args[1][0] for call in mock_worker.call_args_list], [1, 7, 19])


if __name__ == "__main__":
    unittest.main()
