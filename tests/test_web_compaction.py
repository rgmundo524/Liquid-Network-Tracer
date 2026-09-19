"""Compaction stays local until the investigator applies a verified preview."""

import copy
import json
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.investigations import read_case, update_case
from liquid_tracer.web import LocalServer, public_compaction_report, public_graph_options
from tests import test_web


class WebCompactionTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    wait = test_web.LocalWebTests.wait
    create = test_web.LocalWebTests.create

    def traced(self):
        _, case = self.create()
        route = "/api/cases/" + case["id"]
        traced = self.wait(self.success(route + "/actions", {"action": "trace"}, 202))
        path, _ = self.server.case(case["id"])
        return route, path, traced["run_id"]

    def compact(self, route, run_id):
        job = self.success(route + "/actions", {"action": "compact", "run_id": run_id}, 202)
        self.assertFalse(job["live"])
        self.assertTrue(job["cancellable"])
        return self.wait(job)

    def test_real_preview_downloads_and_reopen_preserve_archive(self):
        route, path, run_id = self.traced()
        archive = path / "runs" / run_id
        before = {item.name: item.read_bytes() for item in archive.iterdir() if item.is_file()}
        result = self.compact(route, run_id)
        self.assertEqual(result["layout_algorithm"], "elk_layered_v1")
        self.assertRegex(result["preview_id"], "^" + run_id + r"-compact-[0-9a-f]{8}$")
        self.assertIn("before", result["compaction"])
        self.assertIn("after", result["compaction"])
        self.assertNotIn(str(path), json.dumps(result))
        names = {item["name"] for item in result["downloads"]}
        self.assertEqual(names, {"graph.html", "graph.svg", "graph.json", "layout-report.json",
                                 "before.html", "before.svg", "before.json", "compaction.json", "SHA256SUMS",
                                 "details.html", "details.json"})
        self.assertFalse(result["group_context_inputs"])
        self.assertEqual(result["hub_addresses"], [])
        for item in result["downloads"]:
            status, data, response = self.request(item["url"])
            self.assertEqual(status, 200)
            self.assertTrue(data)
            if item["name"].endswith((".html", ".svg")):
                policy = response.getheader("Content-Security-Policy")
                self.assertIn("sandbox allow-popups allow-popups-to-escape-sandbox;", policy)
                self.assertNotIn("allow-scripts", policy)
                self.assertNotIn("allow-same-origin", policy)
        forbidden = result["preview_url"].rsplit("/", 1)[0] + "/miro-plan.json"
        self.assertEqual(self.request(forbidden)[0], 404)
        reopened = LocalServer(self.server.root, self.assets, port=0)
        self.addCleanup(reopened.server_close)
        saved = reopened.case_summary(path, read_case(path), detail=True)["artifacts"][run_id]["compact"]
        self.assertEqual(saved["preview_id"], result["preview_id"])
        self.assertEqual(saved["downloads"], result["downloads"])
        self.assertEqual(saved["compaction"], result["compaction"])
        self.assertEqual(before, {item.name: item.read_bytes() for item in archive.iterdir() if item.is_file()})

    def test_compact_action_fixed_arguments_uses_no_live_credentials(self):
        route, path, run_id = self.traced()
        with patch.object(self.server, "start_job", return_value={"id": "synthetic"}) as start:
            self.success(route + "/actions", {"action": "compact", "run_id": run_id,
                "source": "https://attacker.invalid", "path": "/private/sentinel",
                "arguments": ["--shell"], "settings": {"include_fees": True, "connector_style": "elbowed"}}, 202)
            self.assertEqual(start.call_args.args[0], ["compact-preview", "--case", str(path),
                             "--run", run_id, "--include-fees", "--connector-style", "elbowed",
                             "--layout-attempts", "25", "--ungroup-context-inputs"])
            self.assertFalse(start.call_args.kwargs["live"])
            self.assertEqual(start.call_args.kwargs["action"], "compact")
            start.reset_mock()
            for run in ("../outside", "--run", {}, True):
                self.assertEqual(self.request(route + "/actions", {"action": "compact", "run_id": run})[0], 400)
            start.assert_not_called()

    def test_changed_group_or_hub_selection_blocks_apply_before_live_job(self):
        route, path, run_id = self.traced()
        result = self.compact(route, run_id)
        for settings in ({"group_context_inputs": True}, {"hub_addresses": ["G" + "a" * 33]}):
            with self.subTest(settings=settings):
                update_case(path, {"miro_board": "SYNTHETIC=", "run_defaults": settings})
                with patch.object(self.server, "start_job") as start:
                    status, error, _ = self.request(route + "/actions", {"action": "miro-compact", "run_id": run_id,
                        "preview_id": result["preview_id"]})
                    self.assertEqual(status, 400)
                    self.assertNotIn(str(path), error["error"])
                    start.assert_not_called()

    def test_public_group_and_hub_options_are_validated_and_canonical(self):
        address = "G" + "a" * 33
        self.assertEqual(public_graph_options({}), {"group_context_inputs": False, "hub_addresses": []})
        self.assertEqual(public_graph_options({"group_context_inputs": True, "hub_addresses": [address, " " + address], "private": "hidden"}),
                         {"group_context_inputs": True, "hub_addresses": [address]})
        for options in ({"group_context_inputs": "true"}, {"hub_addresses": ["/private"]}, None):
            self.assertIsNone(public_graph_options(options))

    def test_apply_uses_exact_preview_and_live_handoff_without_default_overrides(self):
        route, path, run_id = self.traced()
        result = self.compact(route, run_id)
        update_case(path, {"miro_board": "SYNTHETIC=", "run_defaults": {
            "connector_style": "curved", "include_fees": True, "max_new_items": 123}})
        with patch.object(self.server, "start_job", return_value={"id": "synthetic"}) as start:
            self.success(route + "/actions", {"action": "miro-compact", "run_id": run_id,
                "preview_id": result["preview_id"], "plan": "/private/sentinel", "arguments": ["--shell"]}, 202)
            self.assertEqual(start.call_args.args[0], ["miro-sync", "--case", str(path), "--run", run_id,
                             "--board", "SYNTHETIC=", "--max-new-items", "123",
                             "--compact-preview", result["preview_id"], "--reorganize"])
            self.assertTrue(start.call_args.kwargs["live"])
            # Ordinary sync retains its existing flags and never selects compaction implicitly.
            self.success(route + "/actions", {"action": "miro-sync", "run_id": run_id}, 202)
            self.assertNotIn("--compact-preview", start.call_args.args[0])
            self.assertNotIn("--reorganize", start.call_args.args[0])

    def test_invalid_or_wrong_source_previews_rejected_before_live_job(self):
        route, path, run_id = self.traced()
        update_case(path, {"miro_board": "SYNTHETIC="})
        identity = run_id + "-compact-" + "a" * 8
        with patch.object(self.server, "start_job") as start:
            for preview in (None, "../../private", "--plan", {}, identity + "/graph.json",
                            "f" * 16 + "-compact-" + "a" * 8):
                self.assertEqual(self.request(route + "/actions", {"action": "miro-compact", "run_id": run_id,
                                             "preview_id": preview})[0], 400)
            with patch("liquid_tracer.cli.verified_compaction_preview", side_effect=TraceError("Source changed")) as verify:
                self.assertEqual(self.request(route + "/actions", {"action": "miro-compact", "run_id": run_id,
                                             "preview_id": identity})[0], 400)
                verify.assert_called_once_with(path, run_id, identity)
            start.assert_not_called()

    def test_pending_recovery_blocks_apply_before_verification_or_job(self):
        route, path, run_id = self.traced()
        update_case(path, {"miro_board": "SYNTHETIC="})
        with patch.object(self.server, "start_job") as start, \
                patch("liquid_tracer.cli.verified_compaction_preview") as verify, \
                patch("liquid_tracer.cli.miro_recovery_status", return_value={"pending_count": 20}):
            status, result, _ = self.request(route + "/actions", {"action": "miro-compact", "run_id": run_id,
                "preview_id": run_id + "-compact-" + "a" * 8})
            self.assertEqual(status, 400)
            self.assertIn("Recover", result["error"])
            verify.assert_not_called()
            start.assert_not_called()

    def test_stale_service_assessment_and_modified_previews_are_not_rediscovered(self):
        route, path, run_id = self.traced()
        result = self.compact(route, run_id)
        preview = path / "previews" / result["preview_id"]
        original = (preview / "before.svg").read_bytes()
        (preview / "before.svg").write_text("Changed preview")
        self.assertNotIn("compact", self.success(route)["artifacts"].get(run_id, {}))
        (preview / "before.svg").write_bytes(original)
        self.assertIn("compact", self.success(route)["artifacts"][run_id])
        self.success(route + "/services", {"address": "SYNTHETIC-reviewed-address", "enabled": True,
                                          "name": "Possible service"})
        self.assertNotIn("compact", self.success(route)["artifacts"].get(run_id, {}))

    def test_public_report_only_exposes_finite_measurements_and_counts(self):
        sizes = {"main": {"width": 100, "height": 50, "area": 5000,
                           "address_distance": 120.5, "edge_length": 350},
                 "board": {"width": 200, "height": 70, "area": 14000}}
        report = {"before": sizes, "after": sizes, "moved_addresses": 2, "moved_components": 1,
                  "truncated": True, "path": "/private/sentinel", "raw_error": "do not expose"}
        result = public_compaction_report(report)
        self.assertEqual(result["after"]["main"]["address_distance"], 120.5)
        self.assertEqual(result["moved_addresses"], 2)
        self.assertTrue(result["truncated"])
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("raw_error", result)
        for invalid in (True, -1, float("nan"), float("inf"), "100", 2 ** 54, 10 ** 400):
            altered = copy.deepcopy(report)
            altered["after"]["main"]["width"] = invalid
            self.assertIsNone(public_compaction_report(altered))
        report["moved_addresses"] = True
        self.assertNotIn("moved_addresses", public_compaction_report(report))


if __name__ == "__main__":
    unittest.main()
