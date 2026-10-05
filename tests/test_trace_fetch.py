"""Rolling endpoint scheduling without an entire-window completion barrier."""

import heapq
import random
import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from unittest.mock import patch

from liquid_tracer.common import StopRun, TraceError
from liquid_tracer.trace_fetch import FrontierFetcher, heap_prefix


class ControlledAPI:
    def __init__(self):
        self.futures = {}
        self.calls = []
        self.closed = 0
        self.submitted = threading.Condition()

    def submit(self, endpoint):
        with self.submitted:
            self.calls.append(endpoint)
            future = self.futures.setdefault(endpoint, Future())
            self.submitted.notify_all()
            return future

    def wait_for(self, endpoint):
        with self.submitted:
            if not self.submitted.wait_for(lambda: endpoint in self.futures, timeout=3):
                raise AssertionError("Endpoint was not submitted: " + endpoint)
            return self.futures[endpoint]

    def close(self):
        self.closed += 1
        for future in list(self.futures.values()):
            future.cancel()


class FrontierFetcherTests(unittest.TestCase):
    def setUp(self):
        self.api = ControlledAPI()
        self.fetcher = FrontierFetcher(self.api, lambda: 2)
        self.addCleanup(self.api.close)

    def test_nonblocking_pump_refills_free_slots_without_waiting_for_slow_peer(self):
        callbacks = []
        self.fetcher.add("slow")
        self.fetcher.add("fast", lambda result: (callbacks.append(result), self.fetcher.add("child")))
        self.assertEqual(self.fetcher.pump(), 0)
        self.assertEqual(self.api.calls, ["slow", "fast"])
        self.api.futures["fast"].set_result(("fast-data", 1))
        self.assertEqual(self.fetcher.pump(), 1)
        self.assertEqual(self.api.calls, ["slow", "fast", "child"])
        self.assertEqual(callbacks, [("fast-data", 1)])
        self.assertFalse(self.api.futures["slow"].done())
        self.assertEqual(len(self.fetcher.pending), 2)

    def test_get_keeps_other_dependencies_running_on_the_coordinating_thread(self):
        callback_threads = []
        self.fetcher.add("slow")
        self.fetcher.add("fast", lambda result: (callback_threads.append(threading.get_ident()),
                                                self.fetcher.add("child")))

        def consume():
            return threading.get_ident(), self.fetcher.get("slow")

        with ThreadPoolExecutor(max_workers=1) as pool:
            consuming = pool.submit(consume)
            try:
                self.api.wait_for("fast").set_result(("fast-data", 1))
                self.api.wait_for("child").set_result(("child-data", 2))
                self.assertFalse(consuming.done())
                self.api.wait_for("slow").set_result(("slow-data", 3))
                coordinator, result = consuming.result(timeout=3)
            finally:
                self.api.close()
        self.assertEqual(result, ("slow-data", 3))
        self.assertEqual(callback_threads, [coordinator])
        self.assertEqual(self.api.calls.count("slow"), 1)

    def test_unregistered_demand_precedes_speculative_queue_without_exceeding_target(self):
        self.fetcher = FrontierFetcher(self.api, lambda: 1)
        self.fetcher.add("first")
        self.fetcher.pump()
        self.fetcher.add("speculative")
        with ThreadPoolExecutor(max_workers=1) as pool:
            consuming = pool.submit(self.fetcher.get, "demand")
            try:
                self.api.futures["first"].set_result(("first", 1))
                self.api.wait_for("demand").set_result(("demand", 2))
                self.assertEqual(consuming.result(timeout=3), ("demand", 2))
            finally:
                self.api.close()
        self.assertEqual(self.api.calls[:2], ["first", "demand"])
        self.assertLessEqual(len(self.fetcher.pending), 1)

    def test_coalesces_callbacks_and_reuses_already_completed_result(self):
        rows = []
        self.fetcher.add("one", rows.append)
        self.fetcher.add("one", rows.append)
        self.fetcher.pump()
        self.api.futures["one"].set_result(("data", 10))
        self.fetcher.pump()
        self.fetcher.add("one", rows.append)
        self.assertEqual(self.fetcher.get("one"), ("data", 10))
        self.assertEqual(self.api.calls, ["one"])
        self.assertEqual(rows, [("data", 10)] * 3)

    def test_hard_stop_prevents_callback_or_queued_work_from_being_submitted(self):
        self.fetcher.add("failure")
        self.fetcher.add("success", lambda _: self.fetcher.add("child"))
        self.fetcher.add("queued")
        self.fetcher.pump()
        self.api.futures["failure"].set_exception(StopRun("request_limit"))
        self.api.futures["success"].set_result(("valid", 1))
        self.fetcher.pump()
        self.assertEqual(self.api.calls, ["failure", "success"])
        self.assertEqual(self.fetcher.get("success"), ("valid", 1))
        for endpoint in ("failure", "queued", "child", "unregistered"):
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(StopRun, "request_limit"):
                self.fetcher.get(endpoint)
        self.fetcher.run()  # A halted ready queue must not spin forever.
        self.assertEqual(self.api.calls, ["failure", "success"])

    def test_submission_budget_failure_is_delivered_without_spinning(self):
        with patch.object(self.api, "submit", side_effect=StopRun("time_limit")):
            with self.assertRaisesRegex(StopRun, "time_limit"):
                self.fetcher.get("expired")
        self.assertIsInstance(self.fetcher.results["expired"], StopRun)
        self.assertEqual(self.api.closed, 0)

    def test_ordinary_endpoint_error_is_cached_and_does_not_stop_other_requests(self):
        self.fetcher.add("bad")
        self.fetcher.add("good")
        self.fetcher.add("later")
        self.fetcher.pump()
        self.api.futures["bad"].set_exception(TraceError("Malformed synthetic response"))
        self.fetcher.pump()
        self.assertIn("later", self.api.calls)
        with self.assertRaisesRegex(TraceError, "Malformed synthetic response"):
            self.fetcher.get("bad")
        self.assertIsNone(self.fetcher.stop_error)

    def test_callback_failure_closes_api_and_preserves_original_exception(self):
        failure = RuntimeError("consumer failed")

        def fail(_):
            raise failure

        self.fetcher.add("one", fail)
        self.fetcher.add("other")
        self.fetcher.pump()
        self.api.futures["one"].set_result(("data", 1))
        with self.assertRaises(RuntimeError) as raised:
            self.fetcher.pump()
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.api.closed, 1)
        self.assertTrue(self.api.futures["other"].cancelled())
        self.assertNotIn("one", self.fetcher.callbacks)

    def test_wait_interruption_closes_api_before_propagating(self):
        self.fetcher.add("one")
        with patch("liquid_tracer.trace_fetch.wait", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.fetcher.run()
        self.assertEqual(self.api.closed, 1)
        self.assertTrue(self.api.futures["one"].cancelled())

    def test_completed_cancelled_request_surfaces_as_interrupted_stop(self):
        self.fetcher.add("one")
        self.fetcher.pump()
        self.api.futures["one"].cancel()
        with self.assertRaisesRegex(StopRun, "interrupted"):
            self.fetcher.get("one")


class HeapPrefixTests(unittest.TestCase):
    def test_matches_sorted_prefix_with_duplicates_without_mutating_frontier(self):
        generator = random.Random(10)
        values = [(generator.randrange(5), generator.randrange(100)) for _ in range(1000)]
        heapq.heapify(values)
        original = list(values)
        for count in (0, 1, 2, 50, 1000, 2000):
            self.assertEqual(heap_prefix(values, count), sorted(values)[:count])
        self.assertEqual(values, original)
        self.assertEqual(heap_prefix([], 20), [])

    def test_small_prefix_does_not_scan_large_frontier(self):
        class CountingList(list):
            reads = 0

            def __getitem__(self, index):
                self.reads += 1
                return super().__getitem__(index)

            def __iter__(self):
                raise AssertionError("Must not scan the entire frontier")

        frontier = CountingList(range(100000))
        self.assertEqual(heap_prefix(frontier, 64), list(range(64)))
        self.assertLessEqual(frontier.reads, 129)


if __name__ == "__main__":
    unittest.main()
