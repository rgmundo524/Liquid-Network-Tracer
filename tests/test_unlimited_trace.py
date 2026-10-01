"""Zero resource budgets retain hop boundaries, API pacing and cancellation."""
from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from liquid_tracer.api import Budget, Esplora, Limits
from liquid_tracer.common import StopRun, TraceError
from liquid_tracer.store import Store
from liquid_tracer.trace import new_state, trace
from liquid_tracer.trace_fetch import FrontierFetcher, TraceConcurrency
from tests.test_trace_concurrency import RecordingTransport, converging_fixture


class UnlimitedBudgetTests(unittest.TestCase):
    def test_zero_defaults_are_json_safe_but_hop_zero_still_means_no_spending_hop(self):
        limits = Limits(max_hops=0)
        limits.validate()
        self.assertEqual(asdict(limits), {"max_hops": 0, "max_transactions": 0, "max_outpoints": 0,
                                          "max_requests": 0, "max_seconds": 0})
        json.dumps(asdict(limits), allow_nan=False)
        for field in asdict(limits):
            for value in (-1, True, float("inf"), float("nan"), "0"):
                with self.subTest(field=field, value=value), self.assertRaises(TraceError):
                    Limits(**{field: value}).validate()

    def test_unlimited_request_and_duration_budgets_keep_twenty_second_request_timeout(self):
        with patch("liquid_tracer.api.time.monotonic", return_value=0):
            budget = Budget(Limits())
        with patch("liquid_tracer.api.time.monotonic", return_value=1_000_000):
            for _ in range(1001):
                budget.request()
            budget.check()
            self.assertEqual(budget.requests, 1001)
            self.assertIsNone(budget.remaining_seconds())
            self.assertEqual(budget.timeout(), 20)
            with patch("liquid_tracer.api.time.sleep") as sleep:
                budget.pause(2)
            sleep.assert_called_once_with(2)

    def test_explicit_finite_deadline_and_request_budget_remain_exact(self):
        with patch("liquid_tracer.api.time.monotonic", return_value=100):
            budget = Budget(Limits(max_requests=2, max_seconds=3))
        with patch("liquid_tracer.api.time.monotonic", return_value=102):
            self.assertEqual(budget.timeout(), 1)
            budget.request(); budget.request()
            with self.assertRaisesRegex(StopRun, "request_limit"):
                budget.request()
            with self.assertRaisesRegex(StopRun, "time_limit"):
                budget.pause(2)
        with patch("liquid_tracer.api.time.monotonic", return_value=103):
            with self.assertRaisesRegex(StopRun, "time_limit"):
                budget.timeout()
            with self.assertRaisesRegex(StopRun, "time_limit"):
                budget.pause(0)

    def test_zero_frontier_budget_still_allows_resource_bounded_adaptive_workers(self):
        with patch.dict("os.environ", {"LIQUID_TRACE_WORKERS": "auto"}), \
                patch("liquid_tracer.count_concurrency._available_bytes", return_value=16 * 1024 ** 3), \
                patch("liquid_tracer.count_concurrency._available_cpu_count", return_value=16):
            policy = TraceConcurrency(8, 0, 49)
            self.assertEqual(policy.target({}), 8)
            self.assertEqual(policy.target({"completed_requests": 8, "latency_seconds": 2}), 16)
            self.assertEqual(policy.ceiling, 64)
            self.assertEqual(TraceConcurrency(3, 0, 49).ceiling, 3)

    def test_unlimited_admission_still_waits_for_shared_quota_and_cooldown(self):
        with tempfile.TemporaryDirectory() as temporary, closing(Store(Path(temporary))) as store:
            clock = [10.]
            admissions = [SimpleNamespace(admitted=False, wait_seconds=.5, active_clients=2,
                                           effective_rps=49, reason="server_cooldown"),
                          SimpleNamespace(admitted=True, wait_seconds=0, active_clients=2,
                                           effective_rps=49, reason=None)]
            quota = SimpleNamespace(reserve=lambda: admissions.pop(0), close=lambda: None)
            with patch("liquid_tracer.api.time.monotonic", side_effect=lambda: clock[0]):
                with Esplora(store, "run", Limits(), auth="none", transport=lambda *args: (200, {}, b'{}'),
                             shared_quota=quota) as api:
                    with patch.object(api._gate, "wait", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)) as wait:
                        api._reserve_request()
                    wait.assert_called_once_with(.25)
                    self.assertEqual(api.budget.requests, 1)
                    self.assertEqual(api.budget.timeout(), 20)

    def test_interrupt_during_unlimited_shared_quota_wait_drains_promptly(self):
        with tempfile.TemporaryDirectory() as temporary, closing(Store(Path(temporary))) as store:
            waiting = threading.Event()
            def reserve():
                waiting.set()
                return SimpleNamespace(admitted=False, wait_seconds=10., active_clients=2,
                                       effective_rps=49, reason="server_cooldown")
            quota = SimpleNamespace(reserve=reserve, close=lambda: None)
            calls = []
            with Esplora(store, "run", Limits(), auth="none", transport=lambda *args: calls.append(args),
                         shared_quota=quota) as api:
                fetcher = FrontierFetcher(api, lambda: 2)
                fetcher.add("/tx/synthetic")
                def interrupt(*args, **kwargs):
                    self.assertTrue(waiting.wait(1), "Worker never reached shared admission")
                    raise KeyboardInterrupt()
                started = time.monotonic()
                with patch("liquid_tracer.trace_fetch.wait", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
                    fetcher.run()
                self.assertLess(time.monotonic() - started, 2)
                self.assertTrue(api._cancelled.is_set())
                self.assertTrue(all(future.done() for future in api._results.values()))
                self.assertEqual(calls, [])

    def test_default_trace_can_exceed_all_previous_resource_caps_without_crossing_hops(self):
        data, roots, _, joined, unrelated = converging_fixture(1001)
        limits = Limits(max_hops=2)
        transport = RecordingTransport(data)
        with tempfile.TemporaryDirectory() as temporary, closing(Store(Path(temporary))) as store:
            with Esplora(store, "pending", limits, auth="none", transport=transport,
                         advertised_rps=1_000_000, adaptive_workers=True) as api:
                state = new_state([root + ":0" for root in roots], api.base, limits, [])
                api.run_id = state["run_id"]
                result = trace(api, state, limits, Path(temporary) / "trace.json")
        self.assertEqual(result["status"], "bounded_complete")
        self.assertGreater(len(result["transactions"]), 250)
        self.assertGreater(result["stats"]["outpoints_examined_this_run"], 2000)
        self.assertGreater(result["stats"]["requests_this_run"], 600)
        self.assertNotIn("/tx/" + joined + "/outspends", transport.calls)
        self.assertNotIn("/tx/" + unrelated, transport.calls)
        self.assertEqual(result["limits"], asdict(limits))
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
