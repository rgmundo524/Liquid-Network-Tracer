"""Real HTTP contracts for one saved collection and multiple plotted boards."""

import contextlib
import csv
import io
import json
import unittest
from unittest.mock import patch

from liquid_tracer.cli import main
from liquid_tracer.common import TraceError, save_json
from liquid_tracer.investigations import read_case, update_case
from liquid_tracer.investigation_boards import link_board
from liquid_tracer.plots import preview_plot
from tests import test_web
from tests.test_attribution_convergence import graph_state
from tests.test_connections import saved_case
from tests.test_pegout_paths import add_pegout
from tests.test_input_order import input_order_state


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
        self.assertNotIn(str(case), json.dumps(detail))
        self.assertNotIn("state_file", json.dumps(detail))
        self.assertEqual((case / "case.json").read_bytes(), before)

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

    def test_board_actions_use_one_contract_and_goal_bound_sync(self):
        case, route, _ = self.collected()
        plot = preview_plot(case, "pegouts")
        board = link_board(case, "pegouts", "Cashouts", "PEG=")
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

    def test_stale_plots_remain_listed_but_cannot_be_served_or_synced(self):
        case, route, _ = self.collected()
        plot = preview_plot(case, "full")
        artifact = self.server.public_result(plot, "plot", case, [])["artifact"]
        update_case(case, {"run_defaults": {"include_fees": True}})
        detail = self.success(route)
        self.assertFalse(detail["plots"][0]["reviewable"])
        self.assertNotIn("artifact", detail["plots"][0])
        self.assertEqual(self.request(artifact["preview_url"])[0], 400)

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
