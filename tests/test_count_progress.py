import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.progress import MESSAGES, ProgressReporter, public_progress


class CountProgressTests(unittest.TestCase):
    def event(self, **updates):
        return {"phase": "address_counts", "completed": 127, "total": 7956,
                "worker_count": 16, "worker_limit": 64, "observed_rps": 12.34,
                **updates}

    def test_count_telemetry_has_generated_message(self):
        for phase in ("address_counts", "address_counts_ready", "address_counts_incomplete"):
            with self.subTest(phase=phase):
                value = public_progress(self.event(phase=phase, message="PRIVATE", error="PRIVATE"))
                self.assertEqual(value["worker_count"], 16)
                self.assertEqual(value["worker_limit"], 64)
                self.assertEqual(value["observed_rps"], 12.34)
                self.assertEqual(value["message"], MESSAGES[phase]
                                 + "; up to 16 concurrent requests; 12.3 counts/s")
                self.assertNotIn("PRIVATE", json.dumps(value))

    def test_worker_bounds_and_single_request(self):
        for workers in (1, 64):
            with self.subTest(workers=workers):
                value = public_progress(self.event(worker_count=workers))
                self.assertEqual(value["worker_count"], workers)
                noun = "request" if workers == 1 else "requests"
                self.assertIn(f"up to {workers} concurrent {noun};", value["message"])

    def test_invalid_worker_fields_are_ignored(self):
        invalid = (True, False, None, "16", "PRIVATE", [], {}, 1.0, 0, -1, 65,
                   float("nan"), float("inf"))
        for field in ("worker_count", "worker_limit"):
            for number in invalid:
                with self.subTest(field=field, number=number):
                    value = public_progress(self.event(**{field: number}))
                    self.assertNotIn(field, value)
                    self.assertNotIn("PRIVATE", json.dumps(value))

    def test_worker_count_cannot_exceed_resource_limit(self):
        value = public_progress(self.event(worker_count=32, worker_limit=16))
        self.assertNotIn("worker_count", value)
        self.assertNotIn("worker_limit", value)
        self.assertNotIn("concurrent", value["message"])
        self.assertEqual(value["observed_rps"], 12.34)

    def test_invalid_rates_are_ignored(self):
        for rate in (True, False, None, "12.3", "PRIVATE", [], {}, -1, -0.1,
                     float("nan"), float("inf"), float("-inf"), 2 ** 53):
            with self.subTest(rate=rate):
                value = public_progress(self.event(observed_rps=rate))
                self.assertNotIn("observed_rps", value)
                self.assertNotIn("counts/s", value["message"])

    def test_zero_rate_and_optional_fields(self):
        base = {"phase": "address_counts", "completed": 0, "total": 5}
        self.assertEqual(public_progress({**base, "observed_rps": 0})["message"],
                         MESSAGES["address_counts"] + "; 0.0 counts/s")
        self.assertEqual(public_progress({**base, "worker_count": 8})["worker_count"], 8)
        self.assertEqual(public_progress({**base, "worker_limit": 32})["worker_limit"], 32)
        self.assertEqual(public_progress(base), {**base, "message": MESSAGES["address_counts"]})

    def test_count_fields_do_not_escape_into_other_phases(self):
        for phase in ("collecting", "preflight", "pegout_search", "optimizing"):
            with self.subTest(phase=phase):
                value = public_progress(self.event(phase=phase))
                for field in ("worker_count", "worker_limit", "observed_rps"):
                    self.assertNotIn(field, value)
                self.assertNotIn("counts/s", value["message"])
                self.assertNotIn("concurrent", value["message"])

    def test_reporter_emits_sanitized_counts_to_terminal_and_ipc(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "progress.json"
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                ProgressReporter(path)(self.event(message="PRIVATE", response="PRIVATE"))
            self.assertIn("up to 16 concurrent requests; 12.3 counts/s (127/7956)", output.getvalue())
            saved = json.loads(path.read_text())
            self.assertEqual(saved, public_progress(self.event()))
            self.assertNotIn("PRIVATE", output.getvalue() + path.read_text())


if __name__ == "__main__":
    unittest.main()
