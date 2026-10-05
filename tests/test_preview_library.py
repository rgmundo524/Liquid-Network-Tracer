"""Saved preview pagination stays metadata-only and preserves explicit verification."""
import base64
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.investigations import read_case
from liquid_tracer.workflow_api import plot_summary, plot_summary_page, workflow_result
from tests import test_web, test_workflow_summaries


class PreviewLibraryTests(unittest.TestCase):
    setUp = test_workflow_summaries.WorkflowSummaryTests.setUp
    report = test_workflow_summaries.WorkflowSummaryTests.report

    def test_finished_preview_returns_pending_metadata_without_reverifying_graph(self):
        directory = self.report(1)
        with patch("liquid_tracer.plots.reviewed_plot", side_effect=AssertionError("no verification")), \
             patch("liquid_tracer.workflow_api.plot_artifact", side_effect=AssertionError("no graph reads")):
            result = workflow_result(self.case, {"preview_id": directory.name}, "plot")
        self.assertEqual(result["preview_id"], directory.name)
        self.assertTrue(result["validation_pending"])
        self.assertFalse(result["reviewable"])
        self.assertIn("preview_url", result["artifact"])

    def reports(self, count):
        result = [self.report(number) for number in range(1, count + 1)]
        for number, directory in enumerate(result, 1):
            os.utime(directory / "plot.json", ns=(number, number))
        return result

    def test_every_preview_is_reachable_across_bounded_pages(self):
        directories = self.reports(105)
        opened = []
        original = Path.open

        def metadata_only(path, *args, **kwargs):
            self.assertIn(path.name, {"case.json", "plot.json", "preview-numbers.json", "preview-numbers.lock", "preview-numbers.json.tmp"})
            opened.append(path)
            return original(path, *args, **kwargs)

        with ExitStack() as stack:
            for name in ("liquid_tracer.plots.reviewed_plot", "liquid_tracer.plots.list_plots",
                         "liquid_tracer.workflow_api.selected_plot", "liquid_tracer.cli.verify_export"):
                stack.enter_context(patch(name, side_effect=AssertionError("unexpected verification")))
            stack.enter_context(patch.object(Path, "open", metadata_only))
            first = plot_summary_page(self.case)
            self.assertEqual(sum(path.name == "plot.json" for path in opened), 100)
            second = plot_summary_page(self.case, first["next_cursor"])
        self.assertEqual([item["id"] for item in first["plots"] + second["plots"]],
                         [directory.name for directory in reversed(directories)])
        self.assertIsNone(second["next_cursor"])
        self.assertTrue(all(item["validation_pending"] and not item["reviewable"]
                            for item in first["plots"] + second["plots"]))

    def test_new_arrivals_and_deleted_anchor_do_not_shift_older_pages(self):
        directories = self.reports(5)
        with patch("liquid_tracer.workflow_api.PLOT_SUMMARY_LIMIT", 2):
            first = plot_summary_page(self.case)
            shutil.rmtree(directories[3])  # The cursor is a value, not a required directory.
            new = self.report(6)
            os.utime(new / "plot.json", ns=(6, 6))
            second = plot_summary_page(self.case, first["next_cursor"])
            third = plot_summary_page(self.case, second["next_cursor"])
        self.assertEqual([item["id"] for item in second["plots"] + third["plots"]],
                         [directory.name for directory in reversed(directories[:3])])
        self.assertIsNone(third["next_cursor"])

    def test_equal_timestamps_use_preview_id_as_stable_tie_breaker(self):
        directories = self.reports(3)
        for directory in directories:
            os.utime(directory / "plot.json", ns=(10, 10))
        with patch("liquid_tracer.workflow_api.PLOT_SUMMARY_LIMIT", 1):
            pages, cursor = [], None
            for _ in directories:
                result = plot_summary_page(self.case, cursor)
                pages.extend(result["plots"])
                cursor = result["next_cursor"]
        self.assertEqual([item["id"] for item in pages], sorted((path.name for path in directories), reverse=True))
        self.assertIsNone(cursor)

    def test_invalid_reports_consume_scan_budget_without_hiding_older_previews(self):
        directories = self.reports(5)
        (directories[4] / "plot.json").write_text("{")
        (directories[3] / "plot.json").write_bytes(b"x" * 1025)
        (directories[2] / "SHA256SUMS").unlink()
        for number, directory in enumerate(directories, 1):
            os.utime(directory / "plot.json", ns=(number, number))
        with patch("liquid_tracer.workflow_api.PLOT_SUMMARY_LIMIT", 2), \
             patch("liquid_tracer.workflow_api.PLOT_SUMMARY_BYTES", 1024):
            first = plot_summary_page(self.case)
            self.assertEqual(first["plots"], [])
            self.assertIsNotNone(first["next_cursor"])
            second = plot_summary_page(self.case, first["next_cursor"])
            third = plot_summary_page(self.case, second["next_cursor"])
        self.assertEqual([item["id"] for item in second["plots"] + third["plots"]],
                         [directories[1].name, directories[0].name])

    def test_specific_older_summary_does_not_scan_or_verify_other_previews(self):
        directories = self.reports(3)
        with patch("liquid_tracer.workflow_api.PLOT_SUMMARY_LIMIT", 1):
            self.assertNotIn(directories[0].name, [item["id"] for item in plot_summary_page(self.case)["plots"]])
        with patch.object(Path, "glob", side_effect=AssertionError("no directory scan")), \
             patch("liquid_tracer.plots.reviewed_plot", side_effect=AssertionError("no verification")):
            result = plot_summary(self.case, directories[0].name)
        self.assertEqual(result["id"], directories[0].name)
        self.assertTrue(result["validation_pending"])

    def test_specific_summary_rejects_foreign_identity_symlinks_and_missing_ids(self):
        good = self.report()
        bad = self.report(2, case_id="b" * 32)
        linked = self.case / "previews" / ("a" * 16 + "-plots-00000003")
        linked.symlink_to(good, target_is_directory=True)
        for value in (bad.name, linked.name, "../" + good.name, "a" * 16 + "-plots-ffffffff"):
            with self.subTest(value=value), self.assertRaises(TraceError):
                plot_summary(self.case, value)

    def test_cursors_are_canonical_strictly_typed_and_case_bound(self):
        directories = self.reports(2)
        with patch("liquid_tracer.workflow_api.PLOT_SUMMARY_LIMIT", 1):
            cursor = plot_summary_page(self.case)["next_cursor"]
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        invalid = ["", "!", cursor + "=", "a" * 513, 1, True]
        changed = [{**raw, "version": True}, {**raw, "case_id": "b" * 32}, {**raw, "extra": 1},
                   {**raw, "before": [True, directories[1].name]},
                   {**raw, "before": [2 ** 63, directories[1].name]},
                   {**raw, "before": [2, "../outside"]}]
        for value in changed:
            invalid.append(base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("="))
        for value in invalid:
            with self.subTest(value=value), self.assertRaisesRegex(TraceError, "Invalid saved-preview cursor"):
                plot_summary_page(self.case, value)


class PreviewLibraryHTTPTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    create = test_web.LocalWebTests.create
    report = test_workflow_summaries.WorkflowSummaryTests.report
    real_plot = test_workflow_summaries.WorkflowSummaryTests.real_plot

    def prepare(self):
        _, case = self.create()
        self.case, _ = self.server.case(case["id"])
        self.identity = read_case(self.case)["case_id"]
        return "/api/cases/" + self.identity

    def test_pages_summary_and_workflow_are_read_only_without_implicit_review(self):
        route = self.prepare()
        directories = [self.report(number) for number in (1, 2, 3)]
        for number, directory in enumerate(directories, 1):
            os.utime(directory / "plot.json", ns=(number, number))
        with patch("liquid_tracer.workflow_api.PLOT_SUMMARY_LIMIT", 2), \
             patch("liquid_tracer.plots.reviewed_plot", side_effect=AssertionError("no implicit verification")), \
             patch.object(self.server, "start_job", side_effect=AssertionError("no background job")):
            first = self.success(route + "/plots")
            workflow = self.success(route + "/workflow")
            self.assertEqual(workflow["plots_next_cursor"], first["next_cursor"])
            second = self.success(route + "/plots?cursor=" + first["next_cursor"])
            summary = self.success(route + "/plots/" + directories[0].name + "/summary")
        self.assertEqual([item["id"] for item in first["plots"] + second["plots"]],
                         [directory.name for directory in reversed(directories)])
        self.assertEqual(summary, second["plots"][0])
        self.assertTrue(summary["validation_pending"])
        self.assertEqual(self.success("/api/jobs"), {"jobs": []})

    def test_malformed_queries_and_specific_unavailable_reports_are_rejected(self):
        route = self.prepare()
        good = self.report()
        bad = self.report(2, case_id="b" * 32)
        for query in ("cursor=", "cursor=!", "cursor=a&cursor=b", "other=a", "cursor", "cursor=" + "a" * 1025):
            with self.subTest(query=query):
                self.assertEqual(self.request(route + "/plots?" + query)[0], 400)
        self.assertEqual(self.request(route + "/overview?cursor=x")[0], 404)
        self.assertEqual(self.request(route + "/plots?cursor=x", {})[0], 404)
        for name in (bad.name, "a" * 16 + "-plots-ffffffff"):
            code, body, _ = self.request(route + "/plots/" + name + "/summary")
            self.assertEqual(code, 404)
            self.assertEqual(body["error"], "Saved preview is unavailable")
        self.assertEqual(self.success(route + "/plots/" + good.name + "/summary")["id"], good.name)

    def test_metadata_links_do_not_bypass_existing_html_verification(self):
        route = self.prepare()
        plot = self.real_plot()
        (Path(plot["directory"]) / "graph.html").write_text("changed since manifest")
        summary = self.success(route + "/plots/" + plot["preview_id"] + "/summary")
        self.assertTrue(summary["validation_pending"])
        code, body, _ = self.request(summary["artifact"]["preview_url"])
        self.assertEqual(code, 400)
        self.assertIn("error", body)
        self.assertEqual(self.request(route + "/plots/" + plot["preview_id"])[0], 400)


if __name__ == "__main__":
    unittest.main()
