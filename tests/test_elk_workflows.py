"""Real local ELK through the saved-run CLI and loopback worker boundary."""

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import layout_preview_run, main, sync_run, verify_export
from liquid_tracer.common import TraceError, read_json
from liquid_tracer.export import PRESENTATION_VERSION
from liquid_tracer.investigations import read_case, update_case, validate_settings
from liquid_tracer.web import LocalServer, public_layout_metrics
from tests import test_web as web_tests


class ElkWorkflowTests(unittest.TestCase):
    setUp = web_tests.LocalWebTests.setUp
    close_server = web_tests.LocalWebTests.close_server
    request = web_tests.LocalWebTests.request
    success = web_tests.LocalWebTests.success
    wait = web_tests.LocalWebTests.wait
    create = web_tests.LocalWebTests.create

    @staticmethod
    def snapshot(directory):
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}

    def test_real_worker_preview_continuation_downloads_and_reopen(self):
        _, case = self.create()
        route = "/api/cases/" + case["id"]
        first = self.wait(self.success(route + "/actions", {"action": "trace"}, 202))
        path, metadata = self.server.case(case["id"])
        archive = path / "runs" / first["run_id"]
        original = self.snapshot(archive)
        product_job = self.success(route + "/actions", {"action": "layout"}, 202)
        self.assertFalse(product_job["live"])
        product = self.wait(product_job)
        self.assertEqual(product["connector_style"], "straight")
        self.assertEqual(product["layout_algorithm"], "elk_layered_v1")
        self.assertTrue(product["layout_metrics"]["estimated"])
        self.assertNotIn(str(path), json.dumps(product))
        self.assertEqual(len(product["downloads"]), 4)
        for file in product["downloads"]:
            status, data, response = self.request(file["url"])
            self.assertEqual(status, 200)
            self.assertTrue(data)
            if file["name"] == "graph.svg":
                self.assertIn(b"<svg", data)
                self.assertIn("attachment", response.getheader("Content-Disposition"))
            if file["name"] in ("graph.html", "graph.svg"):
                policy = response.getheader("Content-Security-Policy")
                self.assertIn("sandbox allow-popups allow-popups-to-escape-sandbox;", policy)
                self.assertNotIn("allow-scripts", policy)
                self.assertNotIn("allow-same-origin", policy)
            if file["name"] == "graph.json":
                self.assertEqual(data["layout"]["algorithm"], "elk_layered_v1")
        second = self.wait(self.success(route + "/actions", {"action": "trace"}, 202))
        self.assertNotEqual(first["run_id"], second["run_id"])
        curved = self.wait(self.success(route + "/actions", {
            "action": "layout", "run_id": second["run_id"],
            "settings": {"connector_style": "curved", "include_fees": True}}, 202))
        self.assertEqual(curved["connector_style"], "curved")
        self.assertTrue(curved["include_fees"])
        self.assertNotEqual(product["preview_url"], curved["preview_url"])
        reopened = LocalServer(self.server.root, self.assets, port=0)
        self.addCleanup(reopened.server_close)
        artifacts = reopened.case_summary(path, read_case(path), detail=True)["artifacts"]
        self.assertEqual(artifacts[first["run_id"]]["elk"]["downloads"], product["downloads"])
        self.assertEqual(artifacts[second["run_id"]]["elk"]["connector_style"], "curved")
        verify_export(archive)
        self.assertEqual(self.snapshot(archive), original)
        # A one-action presentation override never silently edits defaults.
        self.assertEqual(read_case(path)["run_defaults"]["connector_style"], "straight")

    def test_settings_cli_preview_and_miro_plan_use_same_layout_without_api_calls(self):
        _, case = self.create()
        route = "/api/cases/" + case["id"]
        saved = self.wait(self.success(route + "/actions", {"action": "trace"}, 202))
        path, metadata = self.server.case(case["id"])
        update_case(path, {"run_defaults": {"connector_style": "elbowed"}, "miro_board": "SYNTHETIC="})
        before = self.snapshot(path / "runs")
        events = []
        with patch("liquid_tracer.cli.Esplora", side_effect=AssertionError("No explorer calls")), \
                patch("liquid_tracer.miro._request", side_effect=AssertionError("No Miro calls")):
            output = layout_preview_run(path, progress=events.append)
            report = sync_run(path, saved["run_id"], dry_run=True)
        graph = read_json(output["graph"])
        self.assertEqual(graph["presentation_version"], PRESENTATION_VERSION)
        self.assertEqual(graph["graph_options"]["connector_style"], "elbowed")
        self.assertEqual(report["layout_metrics"], graph["layout"]["metrics"])
        self.assertEqual(report["connector_style"], "elbowed")
        self.assertTrue(all(event["phase"] == "optimizing" for event in events))
        self.assertTrue(any("heap budget" in event["message"] for event in events))
        self.assertEqual(events[-1]["completed"], 1)
        self.assertEqual(self.snapshot(path / "runs"), before)
        with self.assertRaisesRegex(TraceError, "outside runs"):
            layout_preview_run(path, out=path / "runs" / "new-preview")
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=AssertionError("Reviewed plan unchanged")):
            explicit = sync_run(path, saved["run_id"], dry_run=True,
                                plan_path=path / "runs" / saved["run_id"] / "miro-plan.json")
        self.assertFalse(explicit["presentation_refreshed"])
        with self.assertRaisesRegex(TraceError, "--plan cannot be combined"):
            sync_run(path, saved["run_id"], dry_run=True, plan_path=Path("missing"), connector_style="curved")

    def test_invalid_styles_and_metrics_do_not_cross_settings_or_browser_boundary(self):
        for style in (None, True, 1, {}, "bezier", ""):
            with self.subTest(style=style), self.assertRaises(TraceError):
                validate_settings({"connector_style": style})
        self.assertEqual(validate_settings({})["connector_style"], "straight")
        counts = {"crossings": 1, "node_overlaps": 0, "node_intersections": 2, "truncated": True,
                  "secret": "SYNTHETIC-NOT-FOR-UI"}
        metrics = public_layout_metrics({"before": counts, "after": counts, "path": "/private"})
        self.assertTrue(metrics["before"]["truncated"])
        self.assertNotIn("secret", json.dumps(metrics))
        for value in (-1, True, float("nan"), "1", 2 ** 54):
            self.assertIsNone(public_layout_metrics({"before": {**counts, "crossings": value}, "after": counts}))
        _, case = self.create()
        status, _, _ = self.request("/api/cases/" + case["id"] + "/settings",
                                     {"settings": {"connector_style": "invalid"}})
        self.assertEqual(status, 400)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["layout-preview", "--case", "unused", "--connector-style", "invalid"])


if __name__ == "__main__":
    unittest.main()
