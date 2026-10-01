"""Bounded useful-work probes preserve caches, evidence and sealed run history."""

import fcntl
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.explorer_probe import probe_explorer
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
