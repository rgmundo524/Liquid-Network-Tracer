"""Trace section work remains visible within a single long ELK attempt."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.progress import ProgressReporter, public_progress


PRIVATE = "PRIVATE-TRANSACTION-ADDRESS-PATH"


class TraceSectionProgressTests(unittest.TestCase):
    def event(self, **changes):
        return {"phase": "optimizing", "completed": 0, "total": 0,
                "stage": "calculating", "attempt_index": 1, "attempt_total": 2,
                "section_index": 1250, "section_total": 5000,
                "worker_count": 8, "active_workers": 7,
                "heap_mb": 1024, "total_heap_mb": 8192,
                "message": PRIVATE, "section_id": PRIVATE, "path": PRIVATE, **changes}

    def test_thousands_of_sections_are_independent_of_attempt_budget(self):
        value = public_progress(self.event())
        self.assertEqual((value["section_index"], value["section_total"]), (1250, 5000))
        self.assertEqual((value["worker_count"], value["active_workers"]), (8, 7))
        self.assertIn("layout attempt 1 of 2; Trace section 1,250 of 5,000", value["message"])
        self.assertIn("up to 8 ELK workers; 7 active", value["message"])
        self.assertNotIn(PRIVATE, json.dumps(value))
        # Local UI ingestion can safely validate the IPC payload a second time.
        self.assertEqual(public_progress(value), value)

    def test_known_stages_have_fixed_messages_and_counts(self):
        for stage, message in (
            ("section_preparing", "Preparing Trace sections around the preferred backbone"),
            ("section_ready", "Trace section layout completed"),
            ("section_assembling", "Assembling Trace sections and their connections"),
        ):
            with self.subTest(stage=stage):
                value = public_progress(self.event(stage=stage, section_index=None, section_total=None,
                                                   section_count=8000, section_worker_count=5000))
                self.assertEqual(value["stage"], stage)
                self.assertTrue(value["message"].startswith(message))
                self.assertEqual((value["section_count"], value["section_worker_count"]), (8000, 5000))
                self.assertIn("8,000 Trace sections; 5,000 sections require ELK", value["message"])
                self.assertNotIn(PRIVATE, json.dumps(value))

    def test_invalid_section_pairs_cannot_reach_browser(self):
        invalid = (True, False, -1, 0, 1.5, "4", None, [], {}, float("nan"), float("inf"), 2 ** 53)
        for field in ("section_index", "section_total"):
            for number in invalid:
                with self.subTest(field=field, number=number):
                    value = public_progress(self.event(**{field: number}))
                    self.assertNotIn("section_index", value)
                    self.assertNotIn("section_total", value)
                    self.assertNotIn("Trace section", value["message"])
                    self.assertEqual(value["attempt_index"], 1)
                    self.assertNotIn(PRIVATE, json.dumps(value, allow_nan=False))
        for extra in ({"section_index": 5001}, {"section_count": 4999}):
            value = public_progress(self.event(**extra))
            self.assertNotIn("section_index", value)
            self.assertNotIn("section_total", value)

    def test_section_counts_are_bounded_and_consistent(self):
        for field in ("section_count", "section_worker_count"):
            for number in (True, -1, 1.5, "4", None, [], {}, float("nan"), float("inf"), 2 ** 53):
                with self.subTest(field=field, number=number):
                    event = self.event(section_count=8000, section_worker_count=5000)
                    value = public_progress({**event, field: number})
                    self.assertNotIn(field, value)
                    self.assertNotIn(PRIVATE, json.dumps(value, allow_nan=False))
        value = public_progress(self.event(section_count=8000, section_worker_count=8001))
        self.assertEqual(value["section_count"], 8000)
        self.assertNotIn("section_worker_count", value)
        value = public_progress(self.event(section_index=None, section_total=None,
                                          section_count=0, section_worker_count=0))
        self.assertEqual((value["section_count"], value["section_worker_count"]), (0, 0))

    def test_section_workers_cannot_exceed_section_count_or_global_worker_cap(self):
        for extra in ({"worker_count": 65}, {"section_index": 1, "section_total": 7},
                      {"worker_count": 8, "active_workers": 9}):
            with self.subTest(extra=extra):
                value = public_progress(self.event(**extra))
                self.assertNotIn("worker_count", value)
                self.assertNotIn("active_workers", value)
        self.assertEqual(public_progress(self.event(worker_count=64, active_workers=64))["worker_count"], 64)

    def test_section_changes_bypass_terminal_and_ipc_throttling_within_same_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            terminal = io.StringIO()
            with contextlib.redirect_stderr(terminal), patch("liquid_tracer.progress.time.monotonic", return_value=1):
                reporter = ProgressReporter(path)
                for index in (1250, 1252, 1251, 1253):
                    reporter(self.event(section_index=index))
                    payload = json.loads(path.read_text())
                    self.assertEqual(payload["section_index"], index)
                    self.assertEqual(payload["attempt_index"], 1)
                    self.assertEqual(payload["worker_count"], 8)
                reporter(self.event(section_index=1253))  # Unchanged heartbeat stays throttled.
            lines = terminal.getvalue().splitlines()
            self.assertEqual(len(lines), 4)
            for index, line in zip((1250, 1252, 1251, 1253), lines):
                self.assertIn(f"Trace section {index:,} of 5,000", line)
                self.assertIn("layout attempt 1 of 2", line)
            self.assertNotIn(PRIVATE, terminal.getvalue() + path.read_text())

    def test_other_phases_and_unknown_stages_do_not_expose_private_text(self):
        value = public_progress(self.event(phase="preflight", section_count=8000, section_worker_count=5000))
        for field in ("section_index", "section_total", "section_count", "section_worker_count"):
            self.assertNotIn(field, value)
        value = public_progress(self.event(stage=PRIVATE))
        self.assertNotIn("stage", value)
        self.assertNotIn(PRIVATE, json.dumps(value))


if __name__ == "__main__":
    unittest.main()
