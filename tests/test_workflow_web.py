"""Real HTTP contracts for one saved collection and multiple plotted boards."""

import contextlib
import csv
import fcntl
import io
import json
import os
import unittest
from unittest.mock import patch

from liquid_tracer.cli import main
from liquid_tracer.common import TraceError, canonical, save_json
from liquid_tracer.investigations import read_case, update_case
from liquid_tracer.investigation_boards import create_board, link_board
from liquid_tracer.plots import preview_plot
from liquid_tracer.services import set_service
from tests import test_web
from tests.test_attribution_convergence import graph_state
from tests.test_connections import saved_case
from tests.test_pegout_paths import add_pegout
from tests.test_input_order import input_order_state
from tests.test_layout import txid


class WorkflowWebTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    wait = test_web.LocalWebTests.wait
    create = test_web.LocalWebTests.create

    def collected(self):
        _, detail = self.create()
        case, _ = self.server.case(detail["id"])
        state = graph_state((("a:0", "b"),), seeds=("a:0", "b:0"))
        add_pegout(state, "b" * 64)
        state, _ = saved_case(case, state)
        update_case(case, {"miro_board": "MAIN="})
        layout = patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph)
        layout.start()
        self.addCleanup(layout.stop)
        return case, "/api/cases/" + detail["id"], state["run_id"]

    def created_for(self, case, plot, target="CREATED="):
        with patch.dict(os.environ, {"MIRO_ACCESS_TOKEN": "synthetic-token"}):
            return create_board(case, plot["goal"], "Synthetic new board",
                creation_preview_id=plot["preview_id"],
                transport=lambda *args: (201, {}, canonical({"id": target})))

    def test_real_case_payload_lists_goals_coverage_artifacts_and_separate_boards(self):
        case, route, run = self.collected()
        before = (case / "case.json").read_bytes()
        with patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("offline only")), \
                patch("liquid_tracer.address_counts.ensure_counts", side_effect=AssertionError("offline only")):
            for goal in ("full", "connections", "pegouts"):
                preview_plot(case, goal)
                link_board(case, goal, goal.title(), goal.upper() + "=")
            detail = self.success(route)
        self.assertEqual({plot["goal"] for plot in detail["plots"]}, {"full", "connections", "pegouts"})
        self.assertEqual(len(detail["boards"]), 4)
        self.assertEqual(detail["runs"][0]["max_hops"], 10)
        self.assertEqual(detail["latest_run"], run)
        for plot in detail["plots"]:
            self.assertTrue(plot["reviewable"])
            self.assertEqual(plot["source_max_hops"], 10)
            self.assertIn(b"html", self.success(plot["artifact"]["preview_url"]).lower())
            if plot["goal"] == "connections":
                self.assertEqual(plot["connection_scope"], "all_saved")
                self.assertEqual(plot["query"], {"connection_scope": "all_saved"})
                self.assertIsNone(plot["max_hops"])
        self.assertNotIn(str(case), json.dumps(detail))
        self.assertNotIn("state_file", json.dumps(detail))
        self.assertEqual((case / "case.json").read_bytes(), before)

    def test_pegout_path_csv_downloads_follow_the_selected_plot(self):
        case, route, _ = self.collected()
        plot = preview_plot(case, "pegouts", min_hops=1, max_hops=1)
        full = preview_plot(case, "full")
        detail = self.success(route)
        saved = next(item for item in detail["plots"] if item["preview_id"] == plot["preview_id"])
        downloads = {item["name"]: item["url"] for item in saved["artifact"]["downloads"]}
        paths = list(csv.DictReader(io.StringIO(self.success(downloads["transactions.csv"]).decode())))
        endpoints = list(csv.DictReader(io.StringIO(self.success(downloads["endpoints.csv"]).decode())))
        self.assertEqual({row["Transaction Hash"] for row in paths}, {"a" * 64, "b" * 64})
        self.assertEqual(len(endpoints), 1)
        self.assertEqual(endpoints[0]["Status"], "Pegout")
        self.assertEqual(endpoints[0]["Source Seed Outpoints"], "a" * 64 + ":0")
        full_saved = next(item for item in detail["plots"] if item["preview_id"] == full["preview_id"])
        self.assertNotIn("endpoints.csv", {item["name"] for item in full_saved["artifact"]["downloads"]})
        wrong_url = downloads["endpoints.csv"].replace(plot["preview_id"], full["preview_id"])
        self.assertEqual(self.request(wrong_url)[0], 400)

    def test_plot_action_pins_collection_and_is_offline_for_live_investigations(self):
        case, route, run = self.collected()
        metadata = read_case(case)
        metadata.pop("fixture", None)
        save_json(case / "case.json", metadata)
        with patch.object(self.server, "start_job", return_value={"id": "plot"}) as start:
            for goal in ("full", "connections", "pegouts"):
                self.success(route + "/actions", {"action": "plot", "goal": goal, "run_id": "latest",
                                                  "min_hops": 0, "max_hops": 10}, 202)
                start.assert_called_with(["plot", "--case", str(case), "--goal", goal, "--run", run,
                                          "--min-hops", "0", "--max-hops", "10"],
                                         action="plot", live=False, case=case)

    def test_starter_action_accepts_no_hop_range_but_other_goals_require_it(self):
        case, route, run = self.collected()
        with patch.object(self.server, "start_job", return_value={"id": "plot"}) as start:
            self.success(route + "/actions", {"action": "plot", "goal": "connections", "run_id": run}, 202)
            start.assert_called_once_with(["plot", "--case", str(case), "--goal", "connections", "--run", run,
                                          "--min-hops", "0", "--max-hops", "0"],
                                         action="plot", live=False, case=case)
            start.reset_mock()
            for goal in ("full", "pegouts"):
                self.assertEqual(self.request(route + "/actions", {"action": "plot", "goal": goal,
                                                                  "run_id": run})[0], 400)
            start.assert_not_called()

    def test_board_actions_use_one_contract_and_goal_bound_sync(self):
        case, route, _ = self.collected()
        plot = preview_plot(case, "pegouts")
        board = self.created_for(case, plot, "PEG=")
        with patch.object(self.server, "start_job", return_value={"id": "board"}) as start:
            self.success(route + "/actions", {"action": "board-create", "goal": "pegouts", "name": "Cashouts"}, 202)
            self.assertTrue(start.call_args.kwargs["live"])
            self.assertEqual(start.call_args.args[0][0], "investigation-board-create")
            self.success(route + "/actions", {"action": "board-link", "goal": "full", "name": "Overview", "board": "NEXT="}, 202)
            self.assertFalse(start.call_args.kwargs["live"])
            body = {"action": "board-sync", "record_id": board["id"], "preview_id": plot["preview_id"], "reorganize": True}
            self.success(route + "/actions", body, 202)
            self.assertIn("--reorganize", start.call_args.args[0])
            self.assertTrue(start.call_args.kwargs["live"])
            full = preview_plot(case, "full")
            start.reset_mock()
            self.assertEqual(self.request(route + "/actions", {**body, "preview_id": full["preview_id"]})[0], 400)
            empty = preview_plot(case, "pegouts", max_hops=0)
            self.assertEqual(self.request(route + "/actions", {**body, "preview_id": empty["preview_id"]})[0], 400)
            start.assert_not_called()

    def test_plot_settings_are_bound_to_job_without_mutating_defaults(self):
        from liquid_tracer.plots import _settings, LAYOUT_SETTINGS
        case, route, run = self.collected()
        before = (case / "case.json").read_bytes()
        settings = {key: value for key, value in _settings(read_case(case)).items() if key in LAYOUT_SETTINGS}
        settings["connector_style"] = "curved"
        body = {"action": "plot", "goal": "full", "run_id": "latest", "min_hops": 0,
                "max_hops": 10, "layout_settings": settings}
        with patch.object(self.server, "start_job", return_value={"id": "snapshot"}) as start:
            self.success(route + "/actions", body, 202)
            arguments = start.call_args.args[0]
            captured = json.loads(arguments[arguments.index("--layout-settings-json") + 1])
            self.assertEqual(captured["connector_style"], "curved")
            self.assertIn("presentation_version", captured)
            self.assertEqual(arguments[arguments.index("--run") + 1], run)
            self.assertEqual((case / "case.json").read_bytes(), before)
            start.reset_mock()
            for invalid in ({}, {**settings, "extra": True}, {**settings, "connector_style": "invalid"}, None):
                self.assertEqual(self.request(route + "/actions", {**body, "layout_settings": invalid})[0], 400)
            start.assert_not_called()

    def test_saved_plot_submission_is_available_while_collection_runs(self):
        case, route, run = self.collected()
        job = {**test_web.synthetic_running_job(read_case(case)["case_id"]), "resource_kind": "collection"}
        with patch.dict(self.server.jobs, {job["id"]: job}), \
                patch.object(self.server, "start_job", return_value={"id": "concurrent-plot"}) as start:
            self.success(route + "/actions", {"action": "plot", "goal": "full", "run_id": "latest",
                                               "min_hops": 0, "max_hops": 10}, 202)
            arguments = start.call_args.args[0]
            self.assertEqual(arguments[arguments.index("--run") + 1], run)
            self.assertEqual(self.request(route + "/plot-settings", {"settings": {"include_fees": True}})[0], 409)

    def test_real_plot_worker_uses_submitted_settings_while_collector_holds_its_lock(self):
        from liquid_tracer.plots import _settings, LAYOUT_SETTINGS
        case, route, run = self.collected()
        before = (case / "case.json").read_bytes()
        settings = {key: value for key, value in _settings(read_case(case)).items() if key in LAYOUT_SETTINGS}
        settings.update(layout_attempts=1, connector_style="curved")
        collector = {**test_web.synthetic_running_job(read_case(case)["case_id"]), "resource_kind": "collection"}
        with (case / "trace.lock").open("a") as lock, patch.dict(self.server.jobs, {collector["id"]: collector}):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            launched = self.success(route + "/actions", {"action": "plot", "goal": "full", "run_id": "latest",
                "min_hops": 0, "max_hops": 10, "layout_settings": settings}, 202)
            completed = self.wait(launched)
            self.assertEqual(completed["status"], "plotted")
            self.assertEqual(completed["layout_settings"]["connector_style"], "curved")
            self.assertEqual(completed["layout_settings"]["layout_attempts"], 1)
            self.assertEqual(completed["run_id"], run)
            self.assertEqual(completed["input_snapshot_version"], 1)
        self.assertEqual((case / "case.json").read_bytes(), before)

    def test_update_plot_requires_a_matching_board_and_requests_read_credentials(self):
        case, route, run = self.collected()
        board = link_board(case, "full", "Manual arrangement", "MANUAL=")
        other = link_board(case, "pegouts", "Different goal", "OTHER=")
        body = {"action": "plot", "goal": "full", "run_id": run, "min_hops": 0, "max_hops": 10,
                "layout_mode": "update", "board_record_id": board["id"]}
        with patch.object(self.server, "start_job", return_value={"id": "update"}) as start:
            self.success(route + "/actions", body, 202)
            self.assertEqual(start.call_args.args[0][-4:],
                             ["--layout-mode", "update", "--board-record-id", board["id"]])
            self.assertTrue(start.call_args.kwargs["live"])
            self.assertEqual(start.call_args.kwargs["action"], "plot")
            start.reset_mock()
            for changes in ({"board_record_id": other["id"]}, {"board_record_id": "missing"},
                            {"board_record_id": None}, {"layout_mode": "fresh"}, {"layout_mode": []}):
                with self.subTest(changes=changes):
                    self.assertEqual(self.request(route + "/actions", {**body, **changes})[0], 400)
            start.assert_not_called()

    def test_create_and_sync_uses_a_fresh_reviewed_plot(self):
        case, route, _ = self.collected()
        plot = preview_plot(case, "full")
        body = {"action": "board-create-sync", "preview_id": plot["preview_id"], "name": "New assessment"}
        with patch.object(self.server, "start_job", return_value={"id": "create-sync"}) as start:
            self.success(route + "/actions", body, 202)
            self.assertEqual(start.call_args.args[0], ["investigation-board-create-sync", "--case", str(case),
                "--preview", plot["preview_id"], "--name", "New assessment", "--max-items", "750"])
            self.assertTrue(start.call_args.kwargs["live"])
            start.reset_mock()
            self.assertEqual(self.request(route + "/actions", {**body, "board": "EXISTING="})[0], 400)
            with patch("liquid_tracer.plots.reviewed_plot", return_value=({"plot": {"layout_mode": "update"}}, {})):
                self.assertEqual(self.request(route + "/actions", body)[0], 400)
            start.assert_not_called()

    def test_combined_plot_sync_validates_destination_before_one_live_job(self):
        case, route, run = self.collected()
        board = link_board(case, "full", "Existing", "EXISTING=")
        other = link_board(case, "pegouts", "Other goal", "OTHER=")
        body = {"action": "plot-sync", "goal": "full", "run_id": "latest", "min_hops": 0, "max_hops": 10,
                "layout_mode": "fresh", "name": "New investigation chart"}
        with patch.object(self.server, "start_job", return_value={"id": "combined"}) as start:
            self.success(route + "/actions", body, 202)
            start.assert_called_once_with(["plot-sync", "--case", str(case), "--goal", "full", "--run", run,
                "--min-hops", "0", "--max-hops", "10", "--name", "New investigation chart", "--max-items", "750"],
                action="plot-sync", live=True, case=case)
            update = {key: value for key, value in body.items() if key != "name"}
            update.update(layout_mode="update", board_record_id=board["id"])
            start.reset_mock()
            self.success(route + "/actions", update, 202)
            start.assert_called_once_with(["plot-sync", "--case", str(case), "--goal", "full", "--run", run,
                "--min-hops", "0", "--max-hops", "10", "--layout-mode", "update", "--board-record-id", board["id"],
                "--max-items", "750"], action="plot-sync", live=True, case=case)
            start.reset_mock()
            for invalid in ({**body, "name": ""}, {key: value for key, value in body.items() if key != "name"},
                    {**body, "board_record_id": board["id"]}, {**body, "max_items": 100000},
                    {**update, "name": "Rename"}, {**update, "board_record_id": other["id"]},
                    {**update, "board_record_id": "missing"}, {**body, "include_context": "true"}):
                with self.subTest(payload=invalid):
                    self.assertEqual(self.request(route + "/actions", invalid)[0], 400)
            start.assert_not_called()

    def test_combined_job_cannot_be_canceled_during_miro_writes(self):
        case, route, run = self.collected()
        with patch("liquid_tracer.web.threading.Thread.start"):
            job = self.server.action(case, read_case(case), {"action": "plot-sync", "goal": "full", "run_id": run,
                "min_hops": 0, "max_hops": 10, "layout_mode": "fresh", "name": "New board"})
        self.server.job_thread = None
        try:
            self.assertTrue(job["live"])
            self.assertFalse(job["cancellable"])
            self.assertEqual(self.request("/api/jobs/" + job["id"] + "/cancel", {})[0], 409)
            self.assertEqual(self.server.jobs[job["id"]]["status"], "running")
        finally:
            self.server.active_job = None

    def test_combined_result_includes_safe_board_and_saved_plot_artifacts(self):
        case, _, _ = self.collected()
        plot = preview_plot(case, "full")
        value = {**plot, "published": True, "status": "synced", "record_id": "synthetic-record", "board_id": "NEW=",
                 "board_url": "https://untrusted.invalid", "new_shapes": 3, "private_path": "/private/secret"}
        public = self.server.public_result(value, "plot-sync", case, None)
        self.assertEqual(public["board_url"], "https://miro.com/app/board/NEW%3D/")
        self.assertEqual(public["preview_id"], plot["preview_id"])
        self.assertTrue(public["published"])
        self.assertIn("preview_url", public["artifact"])
        self.assertNotIn("private_path", public)
        empty = self.server.public_result({**plot, "published": False, "status": "empty", "empty": True},
                                         "plot-sync", case, None)
        self.assertFalse(empty["published"])
        self.assertNotIn("board_url", empty)

    def test_update_sync_allows_removal_only_plot_but_rejects_other_target_or_reorganization(self):
        case, route, _ = self.collected()
        board = link_board(case, "full", "Manual arrangement", "MANUAL=")
        other = link_board(case, "full", "Other arrangement", "OTHER=")
        graph = {"nodes": [], "plot": {"goal": "full", "layout_mode": "update",
                 "board_record_id": board["id"], "board_id": board["board_id"]}}
        body = {"action": "board-sync", "record_id": board["id"], "preview_id": "synthetic", "reorganize": False}
        with patch("liquid_tracer.plots.reviewed_plot", return_value=(graph, {})), \
                patch.object(self.server, "start_job", return_value={"id": "sync"}) as start:
            self.success(route + "/actions", body, 202)
            self.assertNotIn("--reorganize", start.call_args.args[0])
            start.reset_mock()
            for changes in ({"record_id": other["id"]}, {"reorganize": True}):
                self.assertEqual(self.request(route + "/actions", {**body, **changes})[0], 400)
            start.assert_not_called()

    def test_fresh_plot_cannot_overwrite_linked_board(self):
        case, route, _ = self.collected()
        board = link_board(case, "full", "Manual arrangement", "MANUAL=")
        plot = preview_plot(case, "full")
        with patch.object(self.server, "start_job") as start:
            self.assertEqual(self.request(route + "/actions", {"action": "board-sync", "record_id": board["id"],
                "preview_id": plot["preview_id"], "reorganize": True})[0], 400)
            start.assert_not_called()

    def test_update_counts_expose_only_the_validated_summary(self):
        from liquid_tracer.workflow_api import public_plot
        counts = {"new_nodes": 3, "retained_nodes": 4, "removed_nodes": 1,
                  "new_connectors": 2, "removed_connectors": 0}
        plot = {"layout_mode": "update", "board_record_id": "synthetic", "board_id": "BOARD=",
                "board_name": "Manual", "update_counts": counts, "board_layout": {"private": "hidden"}}
        self.assertEqual(public_plot(plot)["update_counts"], counts)
        self.assertNotIn("board_layout", public_plot(plot))
        for invalid in (None, [], {**counts, "new_nodes": True}, {**counts, "removed_nodes": -1},
                        {**counts, "private_path": "/private/case"}):
            with self.subTest(value=invalid):
                self.assertNotIn("update_counts", public_plot({**plot, "update_counts": invalid}))
        self.assertNotIn("update_counts", public_plot({**plot, "layout_mode": "fresh"}))

    def test_cli_forwards_layout_binding_and_create_sync_without_live_requests(self):
        case, _, _ = self.collected()
        with contextlib.redirect_stdout(io.StringIO()), \
                patch("liquid_tracer.plots.preview_plot", return_value={}) as plotted:
            self.assertEqual(main(["plot", "--case", str(case), "--goal", "full", "--layout-mode", "update",
                                   "--board-record-id", "synthetic-board"]), 0)
            self.assertEqual(plotted.call_args.kwargs["layout_mode"], "update")
            self.assertEqual(plotted.call_args.kwargs["board_record_id"], "synthetic-board")
        with contextlib.redirect_stdout(io.StringIO()), \
                patch("liquid_tracer.investigation_boards.create_and_sync", return_value={}) as synced:
            self.assertEqual(main(["investigation-board-create-sync", "--case", str(case),
                "--preview", "synthetic-preview", "--name", "New board", "--max-items", "120"]), 0)
            self.assertEqual(synced.call_args.args, (case, "synthetic-preview", "New board"))
            self.assertEqual(synced.call_args.kwargs["max_items"], 120)
        with contextlib.redirect_stdout(io.StringIO()), \
                patch("liquid_tracer.investigation_boards.generate_and_sync", return_value={}) as generated:
            self.assertEqual(main(["plot-sync", "--case", str(case), "--goal", "pegouts", "--run", "saved-run",
                "--layout-mode", "update", "--board-record-id", "board-test", "--include-context", "--max-items", "120"]), 0)
            self.assertEqual(generated.call_args.args, (case, "pegouts", "saved-run"))
            self.assertEqual(generated.call_args.kwargs["board_record_id"], "board-test")
            self.assertTrue(generated.call_args.kwargs["include_context"])
            self.assertEqual(generated.call_args.kwargs["max_items"], 120)

    def test_plot_endpoint_options_are_optional_strict_booleans_and_pegout_only(self):
        case, route, run = self.collected()
        body = {"action": "plot", "goal": "pegouts", "run_id": run, "min_hops": 0, "max_hops": 10}
        with patch.object(self.server, "start_job", return_value={"id": "plot"}) as start:
            self.success(route + "/actions", {**body, "include_unspent": True, "include_unspendable": True,
                "include_context": True}, 202)
            self.assertEqual(start.call_args.args[0], ["plot", "--case", str(case), "--goal", "pegouts", "--run", run,
                "--min-hops", "0", "--max-hops", "10", "--include-unspent", "--include-unspendable", "--include-context"])
            self.assertFalse(start.call_args.kwargs["live"])
            self.success(route + "/actions", {**body, "include_unspent": False, "include_unspendable": False,
                "include_context": False}, 202)
            self.assertNotIn("--include-unspent", start.call_args.args[0])
            self.assertNotIn("--include-unspendable", start.call_args.args[0])
            self.assertNotIn("--include-context", start.call_args.args[0])
            start.reset_mock()
            for key in ("include_unspent", "include_unspendable", "include_context"):
                for value in (None, 0, 1, "true", [], {}):
                    with self.subTest(option=key, value=value):
                        self.assertEqual(self.request(route + "/actions", {**body, key: value})[0], 400)
                for goal in ("full", "connections"):
                    self.assertEqual(self.request(route + "/actions", {**body, "goal": goal, key: True})[0], 400)
            start.assert_not_called()

    def test_terminal_only_plot_exposes_selected_endpoints_and_can_sync_to_pegout_board(self):
        case, route, _ = self.collected()
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        state["outputs"]["b" * 64 + ":0"].update(status="unspent_at_observation",
                observed_spend={"spent": False}, spend_observation_id=1)
        saved_case(case, state)
        plot = preview_plot(case, "pegouts", include_unspent=True)
        board = self.created_for(case, plot, "ENDPOINTS=")
        detail = self.success(route)
        listed = detail["plots"][0]
        self.assertEqual(listed["status"], "endpoints_found")
        self.assertEqual(listed["match_count"], 0)
        self.assertEqual(listed["endpoint_count"], 1)
        self.assertEqual(listed["endpoint_counts"], {"pegout": 0, "unspent": 1, "unspendable": 0})
        self.assertIs(listed["query"]["include_unspent"], True)
        self.assertNotIn("include_unspendable", listed["query"])
        self.assertTrue(listed["reviewable"])
        self.assertIn(b"svg", self.success(listed["artifact"]["preview_url"]).lower())
        with patch.object(self.server, "start_job", return_value={"id": "sync"}) as start:
            self.success(route + "/actions", {"action": "board-sync", "record_id": board["id"],
                "preview_id": plot["preview_id"], "reorganize": True}, 202)
            self.assertEqual(start.call_args.args[0][0], "investigation-board-sync")

    def test_contract_rejects_arbitrary_fields_ranges_and_unreviewed_plots(self):
        _, route, run = self.collected()
        body = {"action": "plot", "goal": "full", "run_id": run, "min_hops": 0, "max_hops": 10}
        with patch.object(self.server, "start_job") as start:
            for values in ({"goal": []}, {"run_id": "../private"}, {"run_id": {}}, {"max_hops": True},
                           {"min_hops": 11}, {"max_hops": -1}, {"arguments": []}, {"seeds": []}):
                self.assertEqual(self.request(route + "/actions", {**body, **values})[0], 400)
            for values in ({"visibility": "team"}, {"team_id": "123"}, {"name": ""}, {"goal": {}}):
                self.assertEqual(self.request(route + "/actions", {
                    "action": "board-create", "goal": "full", "name": "Overview", **values})[0], 400)
            start.assert_not_called()

    def test_frozen_plots_remain_available_after_new_attribution_is_imported(self):
        case, route, _ = self.collected()
        plot = preview_plot(case, "full")
        artifact = self.server.public_result(plot, "plot", case, [])["artifact"]
        set_service(case, "SYNTHETIC-a-address", name="Changed assessment", stop_tracing=False)
        detail = self.success(route)
        self.assertTrue(detail["plots"][0]["reviewable"])
        self.assertEqual(detail["plots"][0]["input_snapshot_version"], 1)
        self.assertEqual(detail["plots"][0]["input_snapshot_at"], plot["input_snapshot_at"])
        self.assertIn("artifact", detail["plots"][0])
        self.assertEqual(self.request(artifact["preview_url"])[0], 200)

    def test_saved_layout_settings_remain_available_and_syncable_after_preference_change(self):
        case, route, _ = self.collected()
        plot = preview_plot(case, "full")
        board = self.created_for(case, plot, "ORIGINAL=")
        update_case(case, {"run_defaults": {"include_fees": True, "connector_style": "elbowed"}})
        detail = self.success(route)
        listed = detail["plots"][0]
        self.assertTrue(listed["reviewable"])
        self.assertEqual(listed["layout_settings"], plot["layout_settings"])
        self.assertFalse(listed["layout_settings"]["include_fees"])
        self.assertEqual(listed["layout_settings"]["connector_style"], "straight")
        self.assertIn(b"svg", self.success(listed["artifact"]["preview_url"]).lower())
        with patch.object(self.server, "start_job", return_value={"id": "sync"}) as start:
            self.success(route + "/actions", {"action": "board-sync", "record_id": board["id"],
                                                "preview_id": plot["preview_id"], "reorganize": True}, 202)
            self.assertEqual(start.call_args.args[0][0], "investigation-board-sync")

    def test_public_layout_settings_omit_unknown_or_malformed_nested_fields(self):
        from liquid_tracer.workflow_api import public_plot

        case, _, _ = self.collected()
        plot = preview_plot(case, "full")
        settings = plot["layout_settings"]
        for invalid in (None, [], {**settings, "private_path": "/private/source"},
                        {**settings, "connector_style": {"private_path": "/private/source"}}):
            with self.subTest(value=invalid):
                result = public_plot({**plot, "layout_settings": invalid})
                self.assertNotIn("layout_settings", result)
                self.assertNotIn("/private/source", json.dumps(result))

    def test_public_endpoint_options_and_counts_omit_unknown_or_malformed_fields(self):
        from liquid_tracer.workflow_api import public_plot

        case, _, _ = self.collected()
        plot = preview_plot(case, "pegouts", include_unspent=True)
        for invalid in (None, [], {**plot["query"], "private_path": "/private/source"},
                        {**plot["query"], "include_unspent": 1}):
            with self.subTest(query=invalid):
                result = public_plot({**plot, "query": invalid})
                self.assertNotIn("query", result)
                self.assertNotIn("/private/source", json.dumps(result))
        for invalid in (None, [], {**plot["endpoint_counts"], "private_path": "/private/source"},
                        {**plot["endpoint_counts"], "unspent": True}):
            with self.subTest(counts=invalid):
                result = public_plot({**plot, "endpoint_counts": invalid})
                self.assertNotIn("endpoint_counts", result)
                self.assertNotIn("endpoint_count", result)
                self.assertNotIn("/private/source", json.dumps(result))

    def test_context_plot_query_and_count_survive_http_listing_with_strict_public_counts(self):
        from liquid_tracer.workflow_api import public_plot

        case, route, _ = self.collected()
        with patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("offline only")):
            plot = preview_plot(case, "pegouts", include_context=True)
            detail = self.success(route)
        listed = next(item for item in detail["plots"] if item["preview_id"] == plot["preview_id"])
        self.assertIs(listed["query"]["include_context"], True)
        self.assertEqual(listed["context_edge_count"], plot["context_edge_count"])
        self.assertTrue(listed["reviewable"])
        for invalid in (None, True, -1, 1.0, "1", [], {}):
            with self.subTest(count=invalid):
                self.assertNotIn("context_edge_count", public_plot({**plot, "context_edge_count": invalid}))
        for invalid in (None, {**plot["query"], "include_context": 1}, {**plot["query"], "include_context": False}):
            with self.subTest(query=invalid):
                self.assertNotIn("context_edge_count", public_plot({**plot, "query": invalid}))

    def test_default_pegout_cli_and_http_results_always_include_complete_transaction_io(self):
        case, route, _ = self.collected()
        for supplied in ({}, {"include_context": False}):
            with self.subTest(supplied=supplied):
                # Same entry point used by both plot and plot-sync actions.
                plot = preview_plot(case, "pegouts", **supplied)
                listed = next(item for item in self.success(route)["plots"]
                              if item["preview_id"] == plot["preview_id"])
                self.assertEqual(listed["query"]["transaction_io"], "complete")
                self.assertNotIn("include_context", listed["query"])
                self.assertEqual(listed["context_edge_count"], plot["context_edge_count"])
                self.assertTrue(listed["layout_settings"]["include_fees"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["plot", "--case", str(case), "--goal", "pegouts"]), 0)
        self.assertEqual(json.loads(output.getvalue())["query"]["transaction_io"], "complete")

    def test_busy_plot_registry_does_not_hide_the_investigation_or_expose_paths(self):
        _, route, _ = self.collected()
        with patch("liquid_tracer.plots.list_plots", side_effect=TraceError("private/path")), \
                patch("liquid_tracer.investigation_boards.list_boards", side_effect=TraceError("private/path")):
            detail = self.success(route)
        self.assertEqual(detail["plots"], [])
        self.assertEqual(detail["boards"], [])
        self.assertIn("plots_notice", detail)
        self.assertIn("boards_notice", detail)
        self.assertNotIn("private/path", json.dumps(detail))

    def test_cli_plot_list_and_link_share_the_http_models(self):
        case, _, _ = self.collected()
        commands = [
            ["plot", "--case", str(case), "--goal", "pegouts"],
            ["investigation-board-link", "--case", str(case), "--goal", "pegouts", "--name", "Cashouts", "--board", "PEG="],
            ["investigation-boards", "--case", str(case)],
        ]
        results = []
        for command in commands:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(command), 0)
            results.append(json.loads(output.getvalue()))
        self.assertEqual(results[0]["goal"], "pegouts")
        self.assertEqual(results[1]["board_id"], "PEG=")
        self.assertEqual(len(results[2]["boards"]), 2)
        self.assertEqual(read_case(case)["miro_board"], "MAIN=")

    def test_cli_plot_forwards_optional_terminal_endpoints(self):
        case, _, _ = self.collected()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["plot", "--case", str(case), "--goal", "pegouts",
                "--include-unspent", "--include-unspendable", "--include-context"]), 0)
        result = json.loads(output.getvalue())
        self.assertIs(result["query"]["include_unspent"], True)
        self.assertIs(result["query"]["include_unspendable"], True)
        self.assertIs(result["query"]["include_context"], True)

    def test_worker_board_result_has_no_local_paths_or_supplied_links(self):
        case, _, _ = self.collected()
        result = self.server.public_result({"id": "board", "goal": "full", "board_id": "NEW=",
            "board_url": "javascript:alert(1)", "state_file": "/private/state", "token": "secret"}, "board-create", case, [])
        self.assertEqual(result, {"id": "board", "goal": "full", "board_id": "NEW=",
                                  "board_url": "https://miro.com/app/board/NEW%3D/"})

    def test_real_offline_plot_worker_publishes_usable_http_artifact(self):
        _, route, run = self.collected()
        result = self.wait(self.success(route + "/actions", {"action": "plot", "goal": "full",
            "run_id": run, "min_hops": 0, "max_hops": 10}, 202))
        self.assertEqual(result["goal"], "full")
        self.assertEqual(result["run_id"], run)
        self.assertTrue(result["reviewable"])
        self.assertIn(b"svg", self.success(result["artifact"]["preview_url"]).lower())
        self.assertEqual(self.success(route)["plots"][0]["preview_id"], result["preview_id"])

    def test_full_plot_with_grouped_context_completes_real_elk_and_csv_export(self):
        case, route, _ = self.collected()
        state = input_order_state(12, continuing=(11,))
        state["ancestor_runs"] = []
        # A saved collection has a verified seed-to-child path. The shared
        # layout fixture deliberately omits trace roles; supply them here so
        # current attribution scope still reaches the child's context inputs.
        parent, child = txid("input-order-parent-11"), txid("input-order-child")
        state["seeds"] = [parent + ":0"]
        state["links"] = {parent + ":0": {"spending_txid": child, "vin": 11}}
        for depth, key in enumerate((parent, child)):
            state["transactions"][key]["depth"] = depth
            state["outputs"][key + ":0"] = {"outpoint": key + ":0", "txid": key, "vout": 0,
                "depth": depth, "status": "spent" if depth == 0 else "hop_limit"}
        state, archive = saved_case(case, state)
        update_case(case, {"run_defaults": {"group_context_inputs": True, "layout_attempts": 1}})
        before = {path.name: path.read_bytes() for path in archive.iterdir() if path.is_file()}
        result = self.wait(self.success(route + "/actions", {"action": "plot", "goal": "full",
            "run_id": state["run_id"], "min_hops": 0, "max_hops": 10}, 202))
        downloads = {item["name"]: item["url"] for item in result["artifact"]["downloads"]}
        graph = self.success(downloads["graph.json"])
        self.assertEqual(graph["context_groups"]["group_count"], 1)
        self.assertEqual(graph["context_groups"]["input_count"], 11)
        self.assertIn(b"svg", self.success(downloads["graph.svg"]).lower())
        rows = list(csv.DictReader(io.StringIO(self.success(downloads["transactions.csv"]).decode())))
        self.assertEqual(len(rows), len(graph["edges"]))
        child = next(node["details"]["transaction_id"] for node in graph["nodes"] if node["kind"] == "context_group")[3:]
        inputs = [row for row in rows if row["Direction"] == "IN" and row["Transaction Hash"] == child]
        self.assertEqual([int(row["Number of I/O"]) for row in inputs], list(range(12)))
        self.assertEqual([row["Address Hash"] for row in inputs], [f"SYNTHETIC-input-order-{index}" for index in range(12)])
        self.assertTrue(self.success(route)["plots"][0]["reviewable"])
        self.assertEqual({path.name: path.read_bytes() for path in archive.iterdir() if path.is_file()}, before)
