"""Bounded useful-work probes preserve caches, evidence and sealed run history."""

import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.explorer_probe import MAX_WINDOW_SAMPLES, _Windows, probe_explorer
from liquid_tracer.investigations import create_investigation
from tests.test_address_count_concurrency import CountTransport, SOURCE
from tests.test_attribution_convergence import graph_state
from tests.test_connections import saved_case


def report(**changes):
    return {"run_id": "0123456789abcdef", "fetched": 160, "known": 1160,
            "total": 2000, "remaining": 840, "failed": 0, "errors": [],
            "requests_this_lookup": 160, "elapsed_seconds": 12.,
            "stop_reason": "time_limit", **changes}


class ExplorerProbeMeasurementTests(unittest.TestCase):
    def test_samples_successes_not_cached_baseline_or_failed_attempts(self):
        clock = [0.]
        events = []

        def fetch(case, run_id, **options):
            self.assertEqual((case, run_id), ("case", "run"))
            self.assertEqual(options["max_seconds"], 60)
            self.assertEqual(options["max_requests"], 10000)
            self.assertFalse(options["refresh"])
            self.assertEqual(options["transport"], "offline-transport")
            for at, fetched in ((0, 0), (2, 10), (5, 50), (10, 150), (12, 160)):
                clock[0] = at
                options["progress"]({"phase": "address_counts", "fetched": fetched,
                    "completed": 1000 + fetched + 20, "total": 2000,
                    "api_rate_mode": "adaptive", "api_target_rps": 300.,
                    "shared_api_effective_rps": 250., "rate_limit_responses": 1,
                    "retry_responses": 1})
            clock[0] = 14.  # Include final durable cache save in wall time.
            return report(api_target_rps=250.)

        with patch("liquid_tracer.explorer_probe.time", SimpleNamespace(monotonic=lambda: clock[0])), \
                patch("liquid_tracer.explorer_probe.fetch_counts", side_effect=fetch):
            result = probe_explorer("case", "run", progress=events.append,
                                    transport="offline-transport")
        self.assertEqual(result["counts_per_second"], 160 / 12)
        self.assertEqual(result["probe_wall_seconds"], 14.)
        self.assertEqual(result["probe_outside_lookup_seconds"], 2.)
        self.assertEqual(result["sample_windows"], 2)
        self.assertEqual(result["peak_window"], {"fetched": 100, "seconds": 5., "counts_per_second": 20.})
        self.assertEqual(result["latest_window"], result["peak_window"])
        self.assertEqual((result["api_target_rps"], result["api_rate_mode"]), (250., "adaptive"))
        self.assertEqual(result["rate_limit_feedback"], "observed")
        self.assertEqual(result["outcome"], "provider_throttled")
        self.assertEqual(len(events), 5)

    def test_no_throttle_and_no_metrics_do_not_claim_a_provider_maximum(self):
        for metrics, feedback in (({}, "unavailable"), ({"rate_limit_responses": 0}, "not_observed")):
            with self.subTest(feedback=feedback), \
                    patch("liquid_tracer.explorer_probe.fetch_counts", return_value=report(**metrics)):
                result = probe_explorer("case", seconds=2, max_requests=3)
            self.assertEqual(result["rate_limit_feedback"], feedback)
            self.assertEqual(result["outcome"], "short_sample")
            self.assertIsNone(result["peak_window"])
            self.assertIn("do not establish a provider or account maximum", result["notice"])

    def test_no_work_and_finished_small_backlog_are_identified(self):
        for fetched, outcome in ((0, "no_missing_counts"), (4, "backlog_exhausted")):
            with self.subTest(fetched=fetched), patch("liquid_tracer.explorer_probe.fetch_counts",
                    return_value=report(fetched=fetched, remaining=0)):
                result = probe_explorer("case")
            self.assertEqual(result["outcome"], outcome)

    def test_cached_probe_attributes_all_time_to_preparation(self):
        cached = report(fetched=0, remaining=0)
        del cached["elapsed_seconds"]
        with patch("liquid_tracer.explorer_probe.fetch_counts", return_value=cached), \
                patch("liquid_tracer.explorer_probe.time.monotonic", side_effect=[10., 14.]):
            result = probe_explorer("case")
        self.assertEqual(result["probe_wall_seconds"], 4.)
        self.assertEqual(result["probe_outside_lookup_seconds"], 4.)
        self.assertEqual(result["counts_per_second"], 0.)

    def test_invalid_probe_budgets_fail_before_opening_a_case(self):
        invalid = [{"seconds": value} for value in (0, -1, True, "60", None, float("inf"), float("nan"))]
        invalid += [{"max_requests": value} for value in (0, -1, True, "10", 1.5, None)]
        with patch("liquid_tracer.explorer_probe.fetch_counts") as fetch:
            for options in invalid:
                with self.subTest(options=options), self.assertRaises(TraceError):
                    probe_explorer("case", **options)
            fetch.assert_not_called()

    def test_progress_failure_is_advisory(self):
        def fetch(case, run_id, **options):
            options["progress"]({"phase": "address_counts", "fetched": 0})
            return report()

        def progress(event):
            raise ValueError("closed UI")

        with patch("liquid_tracer.explorer_probe.fetch_counts", side_effect=fetch):
            result = probe_explorer("case", progress=progress)
        self.assertEqual(result["fetched"], 160)

    def test_probe_keeps_api_diagnostics_separate_from_lookup_and_total_wall_time(self):
        clock = [0.]
        metrics = {"completed_requests": 2300, "completed_endpoints": 2300,
                   "peak_in_flight": 12, "latency_seconds": .1, "service_latency_seconds": .17,
                   "network_seconds_total": 230, "evidence_seconds_total": 95,
                   "pacing_wait_seconds_total": 440, "retry_wait_seconds_total": 0,
                   "quota_reserve_calls": 3400, "quota_reserve_seconds": .5,
                   "quota_admitted": 2301, "quota_denied": 1099}

        def fetch(case, run_id, **options):
            clock[0] = 125.8
            return report(elapsed_seconds=60.6, **metrics)

        with patch("liquid_tracer.explorer_probe.time", SimpleNamespace(monotonic=lambda: clock[0])), \
                patch("liquid_tracer.explorer_probe.fetch_counts", side_effect=fetch):
            result = probe_explorer("case")
        for key, value in metrics.items():
            self.assertEqual(result[key], value)
        self.assertEqual(result["elapsed_seconds"], 60.6)
        self.assertEqual(result["probe_wall_seconds"], 125.8)
        self.assertAlmostEqual(result["probe_outside_lookup_seconds"], 65.2)
        self.assertIn("Worker timing totals overlap and must not be added together", result["notice"])

    def test_windows_capture_throughput_and_storage_wait_changes_without_worker_time_sums(self):
        clock = [0.]

        def fetch(case, run_id, **options):
            for index, (at, fetched, store_wait) in enumerate(((0, 0, 0), (5, 370, 1), (10, 525, 16))):
                clock[0] = at
                options["progress"]({"phase": "address_counts", "fetched": fetched,
                    "completed": fetched, "total": 2000, "worker_count": 24, "worker_limit": 64,
                    "in_flight": 5 - index, "peak_in_flight": 10,
                    "api_rate_mode": "adaptive", "api_target_rps": 110.25,
                    "shared_api_effective_rps": 110.25, "shared_api_active_clients": 1,
                    "latency_seconds": .08, "service_latency_seconds": .14,
                    "network_seconds_total": index * 20, "evidence_seconds_total": index * 40,
                    "pacing_wait_seconds_total": index * 80, "quota_reserve_seconds": index * .1,
                    "evidence_write_lock_wait_seconds_total": store_wait,
                    "evidence_read_lock_wait_seconds_total": index * 2,
                    "evidence_commit_seconds_total": index * .5, "evidence_commits": fetched * 2,
                    "evidence_journal_mode": "wal", "evidence_synchronous": "full",
                    "headers": "PRIVATE", "url": "PRIVATE"})
            return report(fetched=525, elapsed_seconds=10., shared_api_active_clients=1,
                          evidence_journal_mode="wal", evidence_synchronous="full")

        with patch("liquid_tracer.explorer_probe.time", SimpleNamespace(monotonic=lambda: clock[0])), \
                patch("liquid_tracer.explorer_probe.fetch_counts", side_effect=fetch):
            result = probe_explorer("case")
        first, last = result["window_series"]
        self.assertEqual((first["start_seconds"], first["end_seconds"], last["end_seconds"]), (0, 5, 10))
        self.assertEqual((first["counts_per_second"], last["counts_per_second"]), (74, 31))
        self.assertEqual((last["worker_count"], last["in_flight"], last["shared_api_active_clients"]), (24, 3, 1))
        self.assertEqual(last["api_target_rps"], 110.25)
        self.assertEqual(last["deltas"]["network_seconds_total"], 20)
        self.assertEqual(last["deltas"]["evidence_write_lock_wait_seconds_total"], 15)
        self.assertEqual(last["deltas"]["evidence_commits"], 310)
        self.assertEqual(last["deltas"]["pacing_wait_seconds_total"], 80)
        self.assertNotIn("latency_seconds", last["deltas"])
        self.assertEqual(result["shared_api_active_clients"], 1)
        self.assertEqual(result["window_series_dropped"], 0)
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_window_deltas_require_both_samples_and_detect_resets_across_missing_events(self):
        clock = [0.]
        windows = _Windows()
        with patch("liquid_tracer.explorer_probe.time", SimpleNamespace(monotonic=lambda: clock[0])):
            windows.observe({"phase": "address_counts", "fetched": 0,
                "network_seconds_total": 100, "evidence_seconds_total": 10,
                "quota_reserve_seconds": 1})
            clock[0] = 1.
            windows.observe({"phase": "address_counts", "fetched": 3,
                             "evidence_seconds_total": 11})
            clock[0] = 2.
            windows.observe({"phase": "address_counts", "fetched": 5,
                "network_seconds_total": 0, "evidence_seconds_total": 12})
            clock[0] = 5.
            windows.observe({"phase": "address_counts", "fetched": 10,
                "network_seconds_total": 120, "evidence_seconds_total": 15,
                "pacing_wait_seconds_total": 99})
        self.assertEqual(windows.series[0]["deltas"], {"evidence_seconds_total": 5})

    def test_group_commit_windows_delta_logical_work_and_physical_commits_but_not_maxima(self):
        clock = [0.]
        windows = _Windows()
        with patch("liquid_tracer.explorer_probe.time", SimpleNamespace(monotonic=lambda: clock[0])):
            for at, jobs, batches, commits, wait, group_max, queue_peak in (
                    (0, 100, 26, 25, 10, 8, 16),
                    (5, 300, 76, 75, 30, 12, 24),
                    (10, 500, 116, 115, 48, 12, 24)):
                clock[0] = at
                windows.observe({"phase": "address_counts", "fetched": jobs // 2,
                    "evidence_write_operations": jobs, "evidence_write_batches": batches,
                    "evidence_commits": commits, "evidence_queue_wait_seconds_total": wait,
                    "evidence_batch_size_max": group_max, "evidence_queue_depth_peak": queue_peak})
        first, last = windows.series
        self.assertEqual(first["deltas"], {"evidence_write_operations": 200,
            "evidence_write_batches": 50, "evidence_commits": 50,
            "evidence_queue_wait_seconds_total": 20})
        self.assertEqual(last["deltas"], {"evidence_write_operations": 200,
            "evidence_write_batches": 40, "evidence_commits": 40,
            "evidence_queue_wait_seconds_total": 18})
        for window in (first, last):
            self.assertEqual(window["evidence_batch_size_max"], 12)
            self.assertEqual(window["evidence_queue_depth_peak"], 24)
            self.assertNotIn("evidence_batch_size_max", window["deltas"])
            self.assertNotIn("evidence_queue_depth_peak", window["deltas"])

    def test_window_history_is_bounded_and_preserves_all_180_second_samples(self):
        clock = [0.]
        windows = _Windows()
        with patch("liquid_tracer.explorer_probe.time", SimpleNamespace(monotonic=lambda: clock[0])):
            for index in range(37):
                clock[0] = index * 5.
                windows.observe({"phase": "address_counts", "fetched": index * 100})
            self.assertEqual((windows.count, len(windows.series)), (36, 36))
            for index in range(37, MAX_WINDOW_SAMPLES + 6):
                clock[0] = index * 5.
                windows.observe({"phase": "address_counts", "fetched": index * 100})
        self.assertEqual(windows.count, MAX_WINDOW_SAMPLES + 5)
        self.assertEqual(len(windows.series), MAX_WINDOW_SAMPLES)
        self.assertEqual(windows.series[0]["start_seconds"], 25.)
        self.assertEqual(windows.series[-1]["end_seconds"], (MAX_WINDOW_SAMPLES + 5) * 5.)

    def test_reset_fetched_counter_restarts_the_window_without_negative_counts(self):
        clock = [0.]
        windows = _Windows()
        with patch("liquid_tracer.explorer_probe.time", SimpleNamespace(monotonic=lambda: clock[0])):
            for at, fetched in ((0, 100), (5, 200), (7, 0), (12, 10)):
                clock[0] = at
                windows.observe({"phase": "address_counts", "fetched": fetched})
        self.assertEqual(windows.count, 2)
        self.assertEqual(windows.series[-1]["fetched"], 10)
        self.assertEqual(windows.series[-1]["start_seconds"], 7)

    def test_invalid_window_gauges_and_provider_strings_are_not_exposed(self):
        clock = [0.]
        windows = _Windows()
        with patch("liquid_tracer.explorer_probe.time", SimpleNamespace(monotonic=lambda: clock[0])):
            for at, fetched in ((0, 0), (5, 10)):
                clock[0] = at
                windows.observe({"phase": "address_counts", "fetched": fetched,
                    "worker_count": 64, "worker_limit": 8, "in_flight": 1000,
                    "shared_api_active_clients": 65536, "evidence_journal_mode": "PRIVATE",
                    "evidence_commit_seconds_total": float("inf"), "body": "PRIVATE"})
        value = windows.series[0]
        for key in ("worker_count", "worker_limit", "in_flight", "shared_api_active_clients",
                    "evidence_journal_mode"):
            self.assertNotIn(key, value)
        self.assertEqual(value["deltas"], {})
        self.assertNotIn("PRIVATE", json.dumps(value))

    def test_invalid_final_report_diagnostics_cannot_bypass_the_sanitizer(self):
        with patch("liquid_tracer.explorer_probe.fetch_counts", return_value=report(
                shared_api_active_clients=65536, shared_api_peak_active_clients=65536,
                in_flight=65, api_rate_mode="PRIVATE",
                evidence_journal_mode="PRIVATE", evidence_sqlite_version="3.50.0 PRIVATE",
                evidence_batch_size_max="PRIVATE", evidence_queue_depth_peak=True,
                evidence_write_batches=-1, evidence_queue_wait_seconds_total=float("inf"),
                evidence_write_lock_wait_seconds_total=float("inf"))):
            result = probe_explorer("case")
        for key in ("shared_api_active_clients", "shared_api_peak_active_clients", "in_flight", "api_rate_mode",
                    "evidence_journal_mode", "evidence_sqlite_version",
                    "evidence_batch_size_max", "evidence_queue_depth_peak",
                    "evidence_write_batches", "evidence_queue_wait_seconds_total",
                    "evidence_write_lock_wait_seconds_total"):
            self.assertNotIn(key, result)
        self.assertNotIn("PRIVATE", json.dumps(result, allow_nan=False))

    def test_active_client_peaks_capture_short_lived_peers_between_window_boundaries(self):
        clock = [0.]

        def fetch(case, run_id, **options):
            for at, fetched, clients in ((0, 0, 1), (2, 20, 3), (5, 50, 1), (10, 100, 1)):
                clock[0] = at
                options["progress"]({"phase": "address_counts", "fetched": fetched,
                                     "shared_api_active_clients": clients})
            return report(shared_api_active_clients=1)

        with patch("liquid_tracer.explorer_probe.time", SimpleNamespace(monotonic=lambda: clock[0])), \
                patch("liquid_tracer.explorer_probe.fetch_counts", side_effect=fetch):
            result = probe_explorer("case")
        self.assertEqual(result["shared_api_active_clients"], 1)
        self.assertEqual(result["shared_api_peak_active_clients"], 3)
        self.assertEqual([window["shared_api_window_peak_active_clients"] for window in result["window_series"]], [3, 1])
        self.assertEqual([window["shared_api_active_clients"] for window in result["window_series"]], [1, 1])

    def test_final_summary_preserves_peak_from_count_report_without_progress_samples(self):
        with patch("liquid_tracer.explorer_probe.fetch_counts", return_value=report(
                shared_api_active_clients=1, shared_api_peak_active_clients=4)):
            result = probe_explorer("case")
        self.assertEqual(result["shared_api_peak_active_clients"], 4)


class ExplorerProbeIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.case = create_investigation(root / "cases", "Probe test")
        state = graph_state(seeds=tuple(name + ":0" for name in "abcdef"))
        state.update(source=SOURCE, fetch_options={"workers": 1})
        self.state, self.archive = saved_case(self.case, state)
        for context in (patch("liquid_tracer.api.default_min_interval", return_value=.000001),
                        patch.dict(os.environ, {"XDG_CACHE_HOME": str(root / "cache"),
                                                "LIQUID_COUNT_WORKERS": "1"})):
            context.start()
            self.addCleanup(context.stop)

    def snapshot(self):
        return {str(path): path.read_bytes() for path in self.archive.rglob("*") if path.is_file()}

    def test_probe_keeps_useful_counts_resumes_without_duplicates_and_preserves_archive(self):
        original = self.snapshot()
        transport = CountTransport()
        result = probe_explorer(self.case, max_requests=2, transport=transport)
        self.assertEqual((result["fetched"], result["remaining"], result["requests_this_lookup"]), (2, 4, 2))
        self.assertEqual(result["stop_reason"], "request_limit")
        self.assertEqual(self.snapshot(), original)
        next_transport = CountTransport()
        resumed = probe_explorer(self.case, transport=next_transport)
        self.assertEqual((resumed["fetched"], resumed["remaining"]), (4, 0))
        self.assertFalse(set(transport.calls) & set(next_transport.calls))
        self.assertEqual(self.snapshot(), original)
        with patch("liquid_tracer.api.Esplora", side_effect=AssertionError("No duplicate count requests")):
            cached = probe_explorer(self.case)
        self.assertEqual(cached["outcome"], "no_missing_counts")
        self.assertEqual(cached["requests_this_lookup"], 0)
        self.assertEqual(self.snapshot(), original)

    def test_busy_collection_or_count_lookup_rejects_probe(self):
        for name in ("trace.lock", "address-counts.lock"):
            with self.subTest(lock=name), (self.case / name).open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch("liquid_tracer.api.Esplora") as api, self.assertRaises(TraceError):
                    probe_explorer(self.case)
                api.assert_not_called()

    def test_corrupt_archive_rejected_before_network(self):
        path = self.archive / "trace.json"
        path.write_bytes(path.read_bytes() + b" ")
        with patch("liquid_tracer.api.Esplora") as api, self.assertRaises(TraceError):
            probe_explorer(self.case)
        api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
