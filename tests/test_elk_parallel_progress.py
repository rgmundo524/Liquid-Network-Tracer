"""Parallel layout progress exposes bounded scheduler data, never worker output."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.progress import ProgressReporter, public_progress


class ParallelLayoutProgressTests(unittest.TestCase):
    def event(self, **changes):
        return {
            "phase": "optimizing", "completed": 0, "total": 0,
            "stage": "calculating", "attempt_index": 3, "attempt_total": 25,
            "worker_count": 4, "active_workers": 3,
            "total_heap_mb": 24000, "heap_mb": 6000,
            "message": "PRIVATE-WORKER-TEXT", "stderr": "PRIVATE-WORKER-TEXT",
            "worker": {"path": "PRIVATE-WORKER-TEXT"},
            **changes,
        }

    def test_parallel_message_distinguishes_shared_budget_and_worker_cap(self):
        value = public_progress(self.event())
        self.assertEqual((value["worker_count"], value["active_workers"]), (4, 3))
        self.assertEqual((value["total_heap_mb"], value["heap_mb"]), (24000, 6000))
        self.assertIn("up to 4 ELK workers; 3 active", value["message"])
        self.assertIn("shared heap budget 24,000 MiB", value["message"])
        self.assertIn("per-worker Node heap budget 6,000 MiB", value["message"])
        self.assertNotIn("PRIVATE-WORKER-TEXT", json.dumps(value))

    def test_invalid_or_inconsistent_worker_counts_are_ignored(self):
        for workers, active in (
            (True, 1), (4, False), (0, 0), (-1, 0), (1001, 1), (26, 1),
            (4, -1), (4, 5), ("4", 1), (4, "1"), (None, 1), (4, None),
            ([], 1), (4, {}), (float("nan"), 1), (4, float("inf")),
        ):
            with self.subTest(workers=workers, active=active):
                value = public_progress(self.event(worker_count=workers, active_workers=active))
                self.assertNotIn("worker_count", value)
                self.assertNotIn("active_workers", value)
                self.assertNotIn("total_heap_mb", value)
                self.assertEqual(value["attempt_index"], 3)
                self.assertNotIn("ELK workers", value["message"])
                json.dumps(value, allow_nan=False)

    def test_invalid_shared_budget_cannot_reach_consumers(self):
        for budget in (True, 0, -1, 5999, 2 ** 53, "PRIVATE-WORKER-TEXT", [],
                       float("nan"), float("inf"), None):
            with self.subTest(budget=budget):
                value = public_progress(self.event(total_heap_mb=budget))
                self.assertNotIn("total_heap_mb", value)
                self.assertEqual(value["worker_count"], 4)
                self.assertNotIn("shared heap budget", value["message"])
                self.assertNotIn("PRIVATE-WORKER-TEXT", json.dumps(value, allow_nan=False))

    def test_zero_active_workers_and_full_pool_serial_retry_are_valid(self):
        value = public_progress(self.event(active_workers=0))
        self.assertEqual(value["active_workers"], 0)
        value = public_progress(self.event(
            stage="retrying_memory", worker_count=1, active_workers=1,
            heap_mb=24000, failure_code="heap_exhausted"))
        self.assertEqual(value["stage"], "retrying_memory")
        self.assertIn("alone with the full shared heap budget", value["message"])
        self.assertIn("JavaScript heap exhausted", value["message"])
        self.assertIn("Node heap budget 24,000 MiB", value["message"])
        self.assertEqual(value["heap_mb"], value["total_heap_mb"])

    def test_worker_killed_retry_does_not_claim_confirmed_memory_exhaustion(self):
        value = public_progress(self.event(stage="retrying_memory", failure_code="worker_killed"))
        self.assertEqual(value["failure_code"], "worker_killed")
        self.assertIn("memory exhaustion is possible but unconfirmed", value["message"])
        for failure in ("PRIVATE-WORKER-TEXT", [], {}):
            value = public_progress(self.event(stage="retrying_memory", failure_code=failure))
            self.assertNotIn("failure_code", value)
            self.assertNotIn("PRIVATE-WORKER-TEXT", json.dumps(value))

    def test_serial_message_is_unchanged(self):
        expected = ("Calculating the graph layout with ELK; layout attempt 3 of 25"
                    "; Node heap budget 6,000 MiB")
        value = public_progress(self.event(worker_count=1, active_workers=1, total_heap_mb=6000))
        self.assertEqual(value["message"], expected)
        event = self.event()
        for field in ("worker_count", "active_workers", "total_heap_mb"):
            event.pop(field)
        self.assertEqual(public_progress(event)["message"], expected)

    def test_measured_worker_ram_uses_only_bounded_numbers_and_fixed_text(self):
        for peak in (1, 1536, 2 ** 31 - 1):
            with self.subTest(peak=peak):
                value = public_progress(self.event(
                    stage="memory_measured", peak_rss_mb=peak,
                    worker_count=1, active_workers=0, attempt_index=1))
                self.assertEqual(value["stage"], "memory_measured")
                self.assertEqual(value["peak_rss_mb"], peak)
                self.assertIn(f"Measured ELK worker peak RAM: {peak:,} MiB", value["message"])
                self.assertIn("layout attempt 1 of 25", value["message"])
                self.assertNotIn("PRIVATE-WORKER-TEXT", json.dumps(value))

    def test_invalid_measurements_are_ignored_without_losing_stage_or_attempt(self):
        for peak in (True, False, 0, -1, 1.5, 2 ** 31, 10 ** 400,
                     "PRIVATE-WORKER-TEXT", [], {}, None, float("nan"), float("inf")):
            with self.subTest(peak=peak):
                value = public_progress(self.event(stage="memory_measured", peak_rss_mb=peak))
                self.assertNotIn("peak_rss_mb", value)
                self.assertEqual(value["stage"], "memory_measured")
                self.assertEqual(value["attempt_index"], 3)
                self.assertNotIn("Measured ELK worker peak RAM:", value["message"])
                self.assertNotIn("PRIVATE-WORKER-TEXT", json.dumps(value, allow_nan=False))
        value = public_progress(self.event(peak_rss_mb=1536))
        self.assertNotIn("peak_rss_mb", value)

    def test_cpu_sized_parallelism_above_four_workers_is_reported(self):
        value = public_progress(self.event(
            attempt_total=100, worker_count=64, active_workers=64,
            heap_mb=512, total_heap_mb=32768))
        self.assertEqual(value["worker_count"], 64)
        self.assertIn("up to 64 ELK workers; 64 active", value["message"])

    def test_interleaved_attempts_reach_file_and_terminal_without_monotonic_assumption(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            output = io.StringIO()
            with contextlib.redirect_stderr(output), \
                    patch("liquid_tracer.progress.time.monotonic", return_value=1):
                reporter = ProgressReporter(path)
                for index in (3, 1, 4, 2, 1):
                    reporter(self.event(attempt_index=index))
                    value = json.loads(path.read_text())
                    self.assertEqual(value["attempt_index"], index)
                    self.assertEqual(value["worker_count"], 4)
            lines = output.getvalue().splitlines()
            self.assertEqual(len(lines), 5)
            for index, line in zip((3, 1, 4, 2, 1), lines):
                self.assertIn(f"layout attempt {index} of 25", line)
            self.assertNotIn("PRIVATE-WORKER-TEXT", output.getvalue() + path.read_text())

    def test_other_phases_do_not_expose_worker_fields(self):
        value = public_progress(self.event(phase="preflight"))
        for field in ("worker_count", "active_workers", "total_heap_mb", "heap_mb"):
            self.assertNotIn(field, value)


if __name__ == "__main__":
    unittest.main()
