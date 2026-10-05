"""Retired Report Figures endpoints do not affect scope analysis or previews."""
import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import main, parser
from liquid_tracer.job_resources import PARALLEL_ACTIONS, job_resources
from liquid_tracer.progress import public_progress
from liquid_tracer.web import CANCELLABLE_ACTIONS
from tests import test_web


class RemovedReportFiguresTests(unittest.TestCase):
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    create = test_web.LocalWebTests.create

    def setUp(self):
        test_web.LocalWebTests.setUp(self)
        _, self.metadata = self.create()
        self.case, _ = self.server.case(self.metadata["id"])
        self.route = "/api/cases/" + self.metadata["id"]
        self.preview = "a" * 16 + "-plots-" + "b" * 8
        self.report = self.preview + "-report-" + "c" * 8
        self.saved_report = self.case / "reports" / self.report / "index.html"
        self.saved_report.parent.mkdir(parents=True)
        self.saved_report.write_text("Preserve this historical local report")

    def test_report_cli_commands_are_rejected_without_dispatch(self):
        with patch("liquid_tracer.cli._dispatch") as dispatch:
            for action in ("report-plan", "report-create"):
                with self.subTest(action=action), contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as error:
                    main([action, "--case", str(self.case), "--preview", self.preview])
                self.assertEqual(error.exception.code, 2)
            dispatch.assert_not_called()
        self.assertEqual(self.saved_report.read_text(), "Preserve this historical local report")

    def test_stale_report_actions_create_no_task_and_request_no_credentials(self):
        with patch.object(self.server, "start_job") as start, \
                patch("liquid_tracer.web.worker_command", side_effect=AssertionError("No worker or credentials")):
            for action in ("report-plan", "report-create"):
                response = self.request(self.route + "/actions", {
                    "action": action, "preview_id": self.preview, "max_objects": 160})
                self.assertEqual(response[0], 400, response[1])
                self.assertIn("supported investigation action", response[1]["error"])
                self.assertNotIn(action, PARALLEL_ACTIONS)
                self.assertNotIn(action, CANCELLABLE_ACTIONS)
            start.assert_not_called()
        self.assertEqual(self.success("/api/jobs")["jobs"], [])

    def test_stale_report_routes_never_fall_back_to_static_files(self):
        routes = [self.route + "/reports", self.route + "/reports/" + self.report,
                  "/files/" + self.metadata["id"] + "/reports/" + self.report + "/index.html",
                  "/files/" + self.metadata["id"] + "/reports/" + self.report + "/nested/figure.svg"]
        # An accidental static file at a retired API path must not be served.
        for route in routes:
            decoy = self.assets / route.lstrip("/")
            if decoy.exists() and decoy.is_dir():
                continue
            try:
                decoy.parent.mkdir(parents=True, exist_ok=True)
                decoy.write_text("DO-NOT-SERVE-OLD-REPORT")
            except FileExistsError:
                # A prior shorter URL intentionally occupies this path as a file.
                pass
        for route in routes:
            with self.subTest(route=route):
                status, response, _ = self.request(route)
                self.assertEqual(status, 404, response)
                self.assertNotIn("DO-NOT-SERVE", str(response))
        self.assertEqual(self.saved_report.read_text(), "Preserve this historical local report")

    def test_workspace_listing_omits_reports_without_reading_or_removing_saved_files(self):
        original = Path.iterdir

        def guard(path):
            if path == self.case / "reports":
                raise AssertionError("Workflow listing must not scan retired report figures")
            return original(path)

        with patch.object(Path, "iterdir", guard):
            for route in (self.route, self.route + "/workflow"):
                response = self.success(route)
                self.assertIn("plots", response)
                self.assertNotIn("reports", response)
                self.assertNotIn("reports_notice", response)
            self.assertIn(self.metadata["id"], [case["id"] for case in self.success("/api/session")["cases"]])
        self.assertTrue(self.saved_report.is_file())

    def test_scope_analysis_cli_and_task_admission_still_use_saved_data(self):
        run = "a" * 16
        (self.case / "runs" / run).mkdir(parents=True)
        command = ["scope-analyze", "--case", str(self.case), "--run", run, "--max-hops", "10"]
        self.assertEqual(parser().parse_args(command).command, "scope-analyze")
        with patch("liquid_tracer.scope_analysis.analyze_scope", return_value={"analysis_id": "synthetic"}) as analyze, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(command), 0)
        self.assertEqual(json.loads(output.getvalue()), {"analysis_id": "synthetic"})
        self.assertEqual(analyze.call_args.args, (self.case, run))
        self.assertEqual(analyze.call_args.kwargs["max_hops"], 10)
        with patch.object(self.server, "start_job", return_value={"id": "scope"}) as start:
            self.success(self.route + "/actions", {"action": "scope-analyze", "run_id": run, "max_hops": 10}, 202)
        self.assertEqual(start.call_args.kwargs, {"action": "scope-analyze", "live": False, "case": self.case})
        self.assertEqual(job_resources(start.call_args.args[0], "scope-analyze", self.case),
                         {"resource_kind": "plot", "source_run_id": run})
        self.assertIn("scope-analyze", PARALLEL_ACTIONS)
        self.assertIn("scope-analyze", CANCELLABLE_ACTIONS)

    def test_normal_preview_serving_keeps_restricted_image_policy(self):
        identity = "a" * 16 + "-elk-" + "b" * 8
        preview = self.case / "previews" / identity / "graph.html"
        preview.parent.mkdir(parents=True)
        preview.write_text("<!doctype html><title>Saved graph</title>")
        with patch("liquid_tracer.layout_overview.navigation_files", return_value=frozenset()):
            status, body, response = self.request("/files/" + self.metadata["id"] + "/previews/" + identity + "/graph.html")
        self.assertEqual(status, 200, body)
        self.assertIn(b"Saved graph", body)
        self.assertIn("img-src data:", response.getheader("Content-Security-Policy"))
        self.assertNotIn("img-src 'self'", response.getheader("Content-Security-Policy"))

    def test_retired_figure_progress_is_not_exposed_but_analysis_and_elk_still_are(self):
        for phase in ("report_verifying", "report_partitioning", "report_layout", "report_exporting"):
            with self.subTest(phase=phase):
                self.assertIsNone(public_progress({"phase": phase, "completed": 0, "total": 1,
                                                  "figure_workers": 4, "active_figures": 1}))
        for phase in ("scope_analysis", "optimizing", "deleting_investigation"):
            with self.subTest(phase=phase):
                self.assertEqual(public_progress({"phase": phase, "completed": 0, "total": 1})["phase"], phase)


if __name__ == "__main__":
    unittest.main()
