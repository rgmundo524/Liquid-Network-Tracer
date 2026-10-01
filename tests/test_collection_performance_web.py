"""Saved collection measurements cross the web boundary without arbitrary data."""

import json
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.common import save_json
from liquid_tracer.investigations import create_investigation, read_case
from liquid_tracer.progress import public_progress
from liquid_tracer.web import LocalServer


class CollectionPerformanceWebTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.server = LocalServer(self.root / "cases", self.root / "assets", port=0)
        self.addCleanup(self.server.server_close)
        self.case = create_investigation(self.server.root, "Performance fixture", seeds=["a" * 64 + ":0"])

    def test_worker_result_and_saved_run_allow_only_valid_performance_measurements(self):
        performance = {"schema_version": 1, "tracing_seconds": 4500, "address_counts_seconds": 510,
                       "checkpoint_count": 12, "worker_peak": 32, "request_count": 24991,
                       "fetch_wait_seconds": -1, "network_seconds_total": float("inf"),
                       "checkpoint_seconds": True, "worker_limit": 1000,
                       "private": "DO-NOT-EXPOSE", "token": "DO-NOT-EXPOSE"}
        expected = {key: performance[key] for key in ("schema_version", "tracing_seconds",
                    "address_counts_seconds", "checkpoint_count", "worker_peak", "request_count")}
        result = self.server.public_result({"run_id": "a" * 16, "performance": performance}, "trace", self.case, None)
        self.assertEqual(result["performance"], expected)
        json.dumps(result, allow_nan=False)

        metadata = read_case(self.case)
        metadata["latest_run"] = "a" * 16
        directory = self.case / "runs" / ("a" * 16)
        directory.mkdir(parents=True)
        (directory / "SHA256SUMS").touch()
        save_json(directory / "trace.json", {"run_id": "a" * 16, "case_id": metadata["case_id"],
                  "stats": {}, "performance": performance})
        summary = self.server.case_summary(self.case, metadata)
        self.assertEqual(summary["latest"]["performance"], expected)
        self.assertNotIn("DO-NOT-EXPOSE", json.dumps(summary, allow_nan=False))

    def test_old_results_and_unknown_schemas_omit_performance(self):
        for value in (None, {}, {"schema_version": 2, "tracing_seconds": 2}):
            with self.subTest(value=value):
                result = self.server.public_result({"performance": value}, "trace", self.case, None)
                self.assertNotIn("performance", result)

    def test_collection_worker_updates_use_request_rate_and_strip_unsafe_fields(self):
        event = {"phase": "collecting", "completed": 3, "total": 10, "worker_count": 16,
                 "worker_limit": 64, "observed_rps": 37.25, "message": "DO-NOT-EXPOSE",
                 "shared_api_active_clients": 2, "shared_api_effective_rps": 49,
                 "shared_api_wait_reason": "server_cooldown", "shared_api_wait_seconds": 3}
        result = public_progress(event)
        self.assertEqual(result["worker_count"], 16)
        self.assertEqual(result["worker_limit"], 64)
        self.assertEqual(result["observed_rps"], 37.25)
        self.assertIn("37.2 requests/s", result["message"])
        self.assertIn("shared API cooldown", result["message"])
        self.assertNotIn("counts/s", result["message"])
        self.assertNotIn("DO-NOT-EXPOSE", json.dumps(result))
        counts = public_progress({**event, "phase": "address_counts"})
        self.assertIn("37.2 counts/s", counts["message"])
        for invalid in (-1, True, float("nan"), float("inf"), "private"):
            result = public_progress({**event, "observed_rps": invalid})
            self.assertNotIn("observed_rps", result)


if __name__ == "__main__":
    unittest.main()
