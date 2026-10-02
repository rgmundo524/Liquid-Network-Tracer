"""Optional starter hop limits survive saved plots, shared data, and publication."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.cli import main
from liquid_tracer.common import LBTC, TraceError, save_json
from liquid_tracer.investigation_boards import generate_and_sync, list_boards
from liquid_tracer.investigations import create_investigation, read_case, update_case
from liquid_tracer.plots import list_plots, preview_plot, reviewed_plot
from liquid_tracer.services import set_service
from liquid_tracer.workflow_api import public_plot
from tests import test_shared_projection, test_web
from tests.test_attribution_convergence import graph_state, tx
from tests.test_board_workflow import BoardRemote
from tests.test_connections import saved_case
from tests.test_connections_complete_workflow import csv_rows, files, input_output_keys


def progressive_state():
    """One-, two-, and three-hop alternatives, plus a nonconnecting branch."""
    state = graph_state((("a:0", "b"), ("a:1", "c"), ("c:0", "b"),
                         ("a:2", "d"), ("d:0", "e"), ("e:0", "b"), ("a:3", "f")),
                        seeds=("a:0", "a:1", "a:2", "a:3", "b:0"),
                        raw_links=(("0:0", "c"), ("1:0", "c")))
    for record in state["transactions"].values():
        record["data"]["vout"].append({"scriptpubkey": "", "scriptpubkey_type": "fee",
                                        "asset": LBTC, "value": 17})
    return state


def selected_transactions(graph):
    return {node["id"][3:] for node in graph["nodes"] if node["kind"] == "transaction"}


class StarterHopPlotWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.case = create_investigation(Path(temporary.name), "Progressive starter connections",
                                         run_defaults={"layout_attempts": 1, "group_context_inputs": True})
        self.state, self.archive = saved_case(self.case, progressive_state())
        layout = patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph)
        self.layout = layout.start()
        self.addCleanup(layout.stop)
        for target in ("liquid_tracer.api.Esplora.get", "liquid_tracer.address_counts.ensure_counts"):
            guard = patch(target, side_effect=AssertionError("Use saved transaction and count data"))
            guard.start()
            self.addCleanup(guard.stop)

    def test_increasing_hops_saves_distinct_immutable_complete_transaction_plots(self):
        original = files(self.archive)
        saved = []
        # Starter limits only constrain the connection search. Attribution
        # stops must not hide paths already proved by the saved transactions.
        set_service(self.case, "SYNTHETIC-c-address", name="Stopped service", stop_tracing=True, hop_limit=0)
        for limit, names in ((1, "ab"), (2, "abc"), (3, "abcde")):
            with self.subTest(limit=limit):
                result = preview_plot(self.case, "connections", connection_scope="hop_limited", max_hops=limit)
                graph, plan = reviewed_plot(self.case, result["preview_id"])
                selected = {tx(name) for name in names}
                self.assertEqual(selected_transactions(graph), selected)
                self.assertEqual(result["query"], {"connection_scope": "hop_limited", "max_hops": limit,
                                                    "transaction_io": "complete"})
                self.assertEqual(result["max_hops"], limit)
                self.assertEqual(result["connection_scope"], "hop_limited")
                self.assertEqual(graph["connections"]["max_hops"], limit)
                self.assertEqual(public_plot(result)["query"], result["query"])
                rows = csv_rows(result)
                expected = input_output_keys(self.state, selected)
                self.assertEqual({(row["Transaction Hash"], row["Direction"], row["Number of I/O"])
                                  for row in rows}, expected)
                self.assertEqual(len(rows), len(expected))
                self.assertEqual(len(plan["connectors"]), len(graph["edges"]))
                saved.append((result, graph, plan, files(Path(result["directory"]))))
        self.assertEqual(len({result["preview_id"] for result, *_ in saved}), 3)
        self.assertEqual(files(self.archive), original)
        update_case(self.case, {"run_defaults": {"group_context_inputs": False}})
        for result, graph, plan, before in saved:
            self.assertEqual(reviewed_plot(self.case, result["preview_id"]), (graph, plan))
            self.assertEqual(files(Path(result["directory"])), before)
        self.assertEqual({plot["max_hops"] for plot in list_plots(self.case)}, {1, 2, 3})
        self.assertTrue(all(plot["reviewable"] for plot in list_plots(self.case)))

    def test_omitted_scope_keeps_all_saved_connections_even_with_small_legacy_maximum(self):
        result = preview_plot(self.case, "connections", max_hops=1)
        graph, _ = reviewed_plot(self.case, result["preview_id"])
        self.assertEqual(selected_transactions(graph), {tx(name) for name in "abcde"})
        self.assertIsNone(result["max_hops"])
        self.assertEqual(result["query"], {"connection_scope": "all_saved", "transaction_io": "complete"})

    def test_invalid_scope_or_hop_range_never_reaches_layout_or_saves_a_plot(self):
        invalid = [{"connection_scope": value} for value in ("", "all", True, [], {})]
        invalid += [{"connection_scope": "hop_limited", "max_hops": value}
                    for value in (None, True, -1, 1.5, "2", 2147483648)]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(TraceError):
                preview_plot(self.case, "connections", **kwargs)
        for goal in ("full", "pegouts"):
            with self.subTest(goal=goal), self.assertRaises(TraceError):
                preview_plot(self.case, goal, connection_scope="hop_limited", max_hops=2)
        self.layout.assert_not_called()
        self.assertEqual(list_plots(self.case), [])

    def test_public_query_rejects_mismatched_or_untyped_hop_limits(self):
        result = preview_plot(self.case, "connections", connection_scope="hop_limited", max_hops=2)
        self.assertEqual(public_plot(result)["query"], result["query"])
        for value in (None, True, -1, 1.5, "2", 3):
            with self.subTest(value=value):
                public = public_plot({**result, "query": {**result["query"], "max_hops": value}})
                self.assertNotIn("query", public)
                self.assertNotIn("context_edge_count", public)
        query = {key: value for key, value in result["query"].items() if key != "max_hops"}
        self.assertNotIn("query", public_plot({**result, "query": query}))

    def test_generate_and_sync_applies_increasing_hops_to_the_same_board(self):
        remote = BoardRemote()
        record_id = None
        saved = []
        for limit, names in ((1, "ab"), (2, "abc"), (3, "abcde")):
            options = {} if record_id is None else {"layout_mode": "update", "board_record_id": record_id}
            result = generate_and_sync(self.case, "connections", connection_scope="hop_limited", max_hops=limit,
                                       token="synthetic", transport=remote, interval=0, workers=1, **options)
            self.assertTrue(result["published"])
            graph, _ = reviewed_plot(self.case, result["preview_id"])
            self.assertEqual(selected_transactions(graph), {tx(name) for name in names})
            self.assertEqual(graph["plot"]["query"]["max_hops"], limit)
            record_id = result["record_id"]
            saved.append(result["preview_id"])
        self.assertEqual(len(remote.creations), 1)
        self.assertEqual(len(list_boards(self.case)), 1)
        self.assertEqual(len(set(saved)), 3)

    def test_cli_plot_and_plot_sync_forward_explicit_scope_and_limit(self):
        for command, target in (("plot", "liquid_tracer.plots.preview_plot"),
                                ("plot-sync", "liquid_tracer.investigation_boards.generate_and_sync")):
            with self.subTest(command=command), patch(target, return_value={}) as operation, redirect_stdout(io.StringIO()):
                self.assertEqual(main([command, "--case", str(self.case), "--goal", "connections",
                                       "--run", self.state["run_id"], "--connection-scope", "hop_limited",
                                       "--max-hops", "3"]), 0)
                self.assertEqual(operation.call_args.kwargs["connection_scope"], "hop_limited")
                self.assertEqual(operation.call_args.kwargs["max_hops"], 3)
                self.assertEqual(operation.call_args.args[2], self.state["run_id"])


class SharedStarterHopWorkflowTests(unittest.TestCase):
    collect = test_shared_projection.SharedProjectionTests.collect

    def setUp(self):
        test_shared_projection.SharedProjectionTests.setUp(self)
        source = progressive_state()
        fixture = {}
        for txid, record in source["transactions"].items():
            fixture["/tx/" + txid] = record["data"]
            fixture["/tx/" + txid + "/outspends"] = [
                ({"spent": True, "txid": source["links"][key]["spending_txid"],
                  "vin": source["links"][key]["vin"], "status": {"confirmed": True}}
                 if key in source["links"] else {"spent": False})
                for key in (f"{txid}:{index}" for index in range(len(record["data"]["vout"])))
            ]
        save_json(self.fixture, fixture)
        self.case = create_investigation(self.root, "Shared starter limits", fixture=self.fixture,
                                         seeds=source["seeds"],
                                         run_defaults={"layout_attempts": 1, "group_context_inputs": True})
        self.collect()

    def test_shared_hop_limits_reuse_evidence_and_preserve_every_saved_snapshot(self):
        before = files(self.source_archive)
        saved = []
        with patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("No further collection")), \
                patch("liquid_tracer.address_counts.ensure_counts", side_effect=AssertionError("No count fetching")):
            for limit, names in ((1, "ab"), (2, "abc"), (3, "abcde")):
                result = preview_plot(self.case, "connections", self.shared_run, data_source="shared",
                                      dataset_id=self.shared_id, connection_scope="hop_limited", max_hops=limit)
                graph, _ = reviewed_plot(self.case, result["preview_id"])
                selected = {tx(name) for name in names}
                self.assertEqual(selected_transactions(graph), selected)
                self.assertEqual(graph["plot"]["collection_source"]["projection_seeds"], read_case(self.case)["seeds"])
                self.assertEqual(result["collection_source"]["run_id"], self.shared_run)
                self.assertEqual({(row["Transaction Hash"], row["Direction"], row["Number of I/O"])
                                  for row in csv_rows(result)}, input_output_keys(self.source, selected))
                saved.append((result, files(Path(result["directory"]))))
        self.assertEqual(files(self.source_archive), before)
        self.assertNotIn("latest_run", read_case(self.case))
        for result, snapshot in saved:
            self.assertEqual(reviewed_plot(self.case, result["preview_id"])[0]["connections"]["max_hops"],
                             result["max_hops"])
            self.assertEqual(files(Path(result["directory"])), snapshot)


class StarterHopHttpWorkflowTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    create = test_web.LocalWebTests.create

    def source(self):
        _, detail = self.create()
        case, _ = self.server.case(detail["id"])
        state, _ = saved_case(case, progressive_state())
        return case, "/api/cases/" + detail["id"] + "/actions", state["run_id"]

    def test_preview_and_sync_actions_forward_explicit_hops_before_starting_worker(self):
        case, route, run = self.source()
        before = (case / "case.json").read_bytes()
        with patch.object(self.server, "start_job", return_value={"id": "bounded-starter"}) as start:
            for action in ("plot", "plot-sync"):
                with self.subTest(action=action):
                    body = {"action": action, "goal": "connections", "run_id": "latest",
                            "connection_scope": "hop_limited", "max_hops": 2}
                    if action == "plot-sync":
                        body["name"] = "Two-hop connections"
                    self.success(route, body, 202)
                    arguments = start.call_args.args[0]
                    self.assertEqual(arguments[:7], [action, "--case", str(case), "--goal", "connections", "--run", run])
                    self.assertEqual(arguments[arguments.index("--max-hops") + 1], "2")
                    self.assertEqual(arguments[arguments.index("--connection-scope") + 1], "hop_limited")
                    self.assertEqual(start.call_args.kwargs["live"], action == "plot-sync")
        self.assertEqual((case / "case.json").read_bytes(), before)

    def test_bad_scope_missing_limit_and_other_goals_are_rejected_without_worker(self):
        _, route, run = self.source()
        base = {"action": "plot", "goal": "connections", "run_id": run,
                "connection_scope": "hop_limited", "max_hops": 2}
        cases = [{key: value for key, value in base.items() if key != "max_hops"}]
        cases += [{**base, "connection_scope": value} for value in (None, True, [], {}, "all", "")]
        cases += [{**base, "max_hops": value} for value in (None, True, -1, "2", 1.5, 2147483648)]
        cases += [{**base, "goal": goal, "min_hops": 0} for goal in ("full", "pegouts")]
        with patch.object(self.server, "start_job") as start:
            for action in ("plot", "plot-sync"):
                for invalid in cases:
                    body = {**invalid, "action": action}
                    if action == "plot-sync":
                        body["name"] = "Invalid starter plot"
                    with self.subTest(body=body):
                        self.assertEqual(self.request(route, body)[0], 400)
            start.assert_not_called()

    def test_old_action_without_scope_still_forwards_compatible_default(self):
        _, route, run = self.source()
        with patch.object(self.server, "start_job", return_value={"id": "legacy-starter"}) as start:
            self.success(route, {"action": "plot", "goal": "connections", "run_id": run}, 202)
            self.assertNotIn("--connection-scope", start.call_args.args[0])

    def test_http_listing_keeps_saved_hop_scope_and_complete_csv(self):
        case, route, _ = self.source()
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph):
            result = preview_plot(case, "connections", connection_scope="hop_limited", max_hops=2)
        detail = self.success(route.removesuffix("/actions"))
        listed = next(plot for plot in detail["plots"] if plot["preview_id"] == result["preview_id"])
        self.assertEqual(listed["query"], result["query"])
        self.assertEqual(listed["connection_scope"], "hop_limited")
        self.assertEqual(listed["max_hops"], 2)
        self.assertTrue(listed["reviewable"])
        self.assertIn("transactions.csv", {item["name"] for item in listed["artifact"]["downloads"]})


if __name__ == "__main__":
    unittest.main()
