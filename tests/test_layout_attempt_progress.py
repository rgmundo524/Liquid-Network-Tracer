"""Long layout searches expose each attempt without passing arbitrary worker text."""

import contextlib
import io
import json
import unittest
from unittest.mock import patch

from liquid_tracer.progress import ProgressReporter, public_progress


class LayoutAttemptProgressTests(unittest.TestCase):
    def event(self, index=1, total=25, seed=1):
        return dict(phase="optimizing", completed=0, total=0, stage="calculating",
                    attempt_index=index, attempt_total=total, seed=seed,
                    message="PRIVATE", path="PRIVATE")

    def test_attempt_number_reaches_terminal_and_browser_with_no_private_text(self):
        value = public_progress(self.event(4, 25, 37))
        self.assertEqual((value["attempt_index"], value["attempt_total"], value["seed"]), (4, 25, 37))
        self.assertIn("layout attempt 4 of 25", value["message"])
        self.assertNotIn("PRIVATE", json.dumps(value))

    def test_invalid_attempt_metadata_is_ignored_without_losing_stage(self):
        for index, total in ((0, 25), (26, 25), (True, 25), (1, True), (1, 1001), (1, "25"), (-1, 25)):
            with self.subTest(index=index, total=total):
                value = public_progress(self.event(index, total))
                self.assertNotIn("attempt_index", value)
                self.assertNotIn("attempt_total", value)
                self.assertNotIn("seed", value)
                self.assertEqual(value["stage"], "calculating")
        for seed in (True, 0, -1, 2 ** 31, "SECRET", []):
            self.assertNotIn("seed", public_progress(self.event(seed=seed)))

    def test_new_attempt_is_reported_even_when_stage_and_clock_do_not_change(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output), patch("liquid_tracer.progress.time.monotonic", return_value=1):
            reporter = ProgressReporter()
            for index in (1, 2, 3):
                reporter(self.event(index))
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(f"attempt {i} of 25" in line for i, line in enumerate(lines, 1)))


if __name__ == "__main__":
    unittest.main()
