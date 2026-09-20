"""Failed optional seeds remain visible without leaking renderer diagnostics."""

import json
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.layout_preview import export_layout, layout_notice
from liquid_tracer.layout_search_reporting import public_search_counts
from liquid_tracer.progress import public_progress
from liquid_tracer.web import public_layout_metrics
from tests.test_layout_preview import graph_fixture


OUTCOMES = {"attempt_count": 25, "attempted_count": 25, "successful_count": 24, "failed_count": 1}
WARNING = "24 of 25 layout attempts succeeded; 1 failed. Best completed layout retained."


class LayoutFailureReportingTests(unittest.TestCase):
    def test_partial_preview_report_and_html_keep_failure_summary(self):
        graph = graph_fixture()
        graph["layout"]["metrics"].update(OUTCOMES)
        with tempfile.TemporaryDirectory() as root:
            product = export_layout(graph, Path(root) / "preview")
            self.assertIn(WARNING, Path(product["html"]).read_text())
            report = json.loads(Path(product["report"]).read_text())
            self.assertIn(WARNING, report["notice"])
            self.assertEqual(report["metrics"]["successful_count"], 24)

    def test_preview_can_use_search_summary_after_metrics_change(self):
        graph = graph_fixture()
        graph["layout"]["search"] = dict(OUTCOMES)
        self.assertIn(WARNING, layout_notice(graph))

    def test_all_success_or_legacy_preview_has_no_failure_warning(self):
        graph = graph_fixture()
        self.assertNotIn("failed", layout_notice(graph))
        graph["layout"]["metrics"].update({**OUTCOMES, "successful_count": 25, "failed_count": 0})
        self.assertNotIn("failed", layout_notice(graph))

    def test_public_metrics_copy_only_consistent_numeric_outcomes(self):
        metrics = graph_fixture()["layout"]["metrics"]
        value = public_layout_metrics({**metrics, **OUTCOMES, "failed_attempts": [{"error": "PRIVATE"}]})
        self.assertEqual({key: value[key] for key in OUTCOMES}, OUTCOMES)
        self.assertNotIn("PRIVATE", json.dumps(value))
        self.assertNotIn("failed_attempts", value)

    def test_invalid_outcomes_do_not_cross_browser_boundary(self):
        for bad in ({"failed_count": True}, {"attempt_count": 1001}, {"successful_count": 25},
                    {"failed_count": -1}, {"attempted_count": 26}, {"successful_count": "PRIVATE"},
                    {"failed_count": None}, {"successful_count": 0, "failed_count": 25}):
            with self.subTest(bad=bad):
                outcomes = {**OUTCOMES, **bad}
                self.assertEqual(public_search_counts(outcomes), {})
                metrics = public_layout_metrics({**graph_fixture()["layout"]["metrics"], **outcomes})
                self.assertNotIn("successful_count", metrics)
                self.assertNotIn("failed_count", metrics)

    def test_failed_attempt_has_fixed_public_warning_and_safe_running_counts(self):
        value = public_progress({"phase": "optimizing", "stage": "attempt_failed", "completed": 0, "total": 0,
                                 "attempt_index": 3, "attempt_total": 25, "seed": 19,
                                 "attempted_count": 3, "successful_count": 2, "failed_count": 1,
                                 "message": "PRIVATE", "stderr": "PRIVATE", "failure_code": "PRIVATE"})
        self.assertEqual(value["successful_count"], 2)
        self.assertIn("continuing the layout search", value["message"])
        self.assertNotIn("PRIVATE", json.dumps(value))

    def test_final_progress_reports_partial_success(self):
        value = public_progress({"phase": "optimizing", "stage": "ready_with_failures", "completed": 1, "total": 1,
                                 "attempt_index": 25, "attempt_total": 25, **OUTCOMES})
        self.assertIn(WARNING, value["message"])
        self.assertEqual(value["failed_count"], 1)

    def test_first_failed_attempt_can_report_zero_success_without_claiming_retained_layout(self):
        value = public_progress({"phase": "optimizing", "stage": "attempt_failed", "completed": 0, "total": 0,
                                 "attempt_index": 1, "attempt_total": 25,
                                 "attempted_count": 1, "successful_count": 0, "failed_count": 1})
        self.assertEqual(value["successful_count"], 0)
        self.assertNotIn("retained", value["message"])


if __name__ == "__main__":
    unittest.main()
