"""Shortest starter routes survive admission, saved previews, and shared projection."""

from contextlib import redirect_stdout
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from liquid_tracer.cli import main, parser
from liquid_tracer.common import TraceError, digest, read_json, save_json
from liquid_tracer.connections import preview_connections, reviewed_connections
from liquid_tracer.investigations import read_case, update_case
from liquid_tracer.plots import list_plots, plot_files, preview_plot, reviewed_plot
from liquid_tracer.web import RequestError
from liquid_tracer.workflow_api import public_plot, workflow_action
from tests import test_connections_hop_workflow as hop_workflow
from tests.test_attribution_convergence import tx
from tests.test_connections_complete_workflow import csv_rows, files, input_output_keys


class ShortestStarterWorkflowTests(unittest.TestCase):
    setUp = hop_workflow.StarterHopPlotWorkflowTests.setUp

    def test_shortest_and_older_scopes_keep_distinct_immutable_snapshots(self):
        original = files(self.archive)
        saved = []
        for scope, maximum, names in (("all_saved", 0, "abcde"),
                                      ("hop_limited", 2, "abc"),
                                      ("shortest", 0, "ab")):
            with self.subTest(scope=scope):
                result = preview_plot(self.case, "connections", connection_scope=scope, max_hops=maximum)
                graph, plan = reviewed_plot(self.case, result["preview_id"])
                self.assertEqual(hop_workflow.selected_transactions(graph), {tx(name) for name in names})
                expected_query = {"connection_scope": scope, "transaction_io": "complete"}
                if scope == "hop_limited":
                    expected_query["max_hops"] = maximum
                self.assertEqual(result["query"], expected_query)
                self.assertEqual(public_plot(result)["query"], expected_query)
                self.assertEqual(graph["connections"]["max_hops"], maximum if scope == "hop_limited" else None)
                saved.append((result, graph, plan, files(Path(result["directory"]))))
        self.assertEqual(len({result["preview_id"] for result, *_ in saved}), 3)
        update_case(self.case, {"run_defaults": {"group_context_inputs": False, "include_fees": True}})
        for result, graph, plan, before in saved:
            self.assertEqual(reviewed_plot(self.case, result["preview_id"]), (graph, plan))
            self.assertEqual(files(Path(result["directory"])), before)
        self.assertEqual(files(self.archive), original)
        self.assertEqual({item["connection_scope"] for item in list_plots(self.case)},
                         {"all_saved", "hop_limited", "shortest"})
        self.assertTrue(all(item["reviewable"] for item in list_plots(self.case)))

    def test_shortest_retains_complete_local_io_but_does_not_trace_context_branches(self):
        result = preview_plot(self.case, "connections", connection_scope="shortest")
        graph, _ = reviewed_plot(self.case, result["preview_id"])
        self.assertEqual(graph["connections"]["outpoints"], [tx("a") + ":0"])
        self.assertEqual(graph["connections"]["pairs"],
                         [{"source": tx("a"), "target": tx("b"), "shortest_hops": 1}])
        self.assertEqual(hop_workflow.selected_transactions(graph), {tx("a"), tx("b")})
        rows = csv_rows(result)
        self.assertEqual({(row["Transaction Hash"], row["Direction"], row["Number of I/O"]) for row in rows},
                         input_output_keys(self.state, {tx("a"), tx("b")}))
        self.assertFalse(any("FEE" in row["Address Flags"] for row in rows))
        self.assertGreater(result["context_edge_count"], 0)
        self.assertIsNone(result["max_hops"])
        self.assertNotIn("max_hops", result["query"])

    def test_standalone_connections_preview_keeps_shortest_scope_on_review(self):
        result = preview_connections(self.case, connection_scope="shortest", max_hops=0)
        graph, plan = reviewed_connections(self.case, result["preview_id"])
        self.assertEqual(result["connection_scope"], "shortest")
        self.assertIsNone(result["max_hops"])
        self.assertEqual(graph["graph_options"]["connection_scope"], "shortest")
        self.assertEqual(graph["connections"]["outpoints"], [tx("a") + ":0"])
        self.assertEqual(reviewed_connections(self.case, result["preview_id"]), (graph, plan))

    def test_saved_scope_disagreement_is_rejected_even_with_updated_file_checksums(self):
        result = preview_plot(self.case, "connections", connection_scope="shortest")
        directory = Path(result["directory"])
        graph = read_json(directory / "graph.json")
        graph["connections"]["connection_scope"] = "all_saved"
        save_json(directory / "graph.json", graph)
        (directory / "SHA256SUMS").write_text("".join(
            digest((directory / name).read_bytes()) + "  " + name + "\n"
            for name in sorted(plot_files(directory) - {"SHA256SUMS"})))
        with self.assertRaisesRegex(TraceError, "disagree"):
            reviewed_plot(self.case, result["preview_id"])

    def test_shortest_public_query_rejects_bounded_or_mismatched_metadata(self):
        result = preview_plot(self.case, "connections", connection_scope="shortest")
        self.assertEqual(public_plot(result)["query"], result["query"])
        invalid = [
            {**result, "max_hops": 0},
            {**result, "max_hops": True},
            {**result, "query": {**result["query"], "max_hops": None}},
            {**result, "query": {**result["query"], "max_hops": 3}},
            {**result, "query": {**result["query"], "connection_scope": "all_saved"}},
            {**result, "query": {**result["query"], "transaction_io": "paths"}},
        ]
        for value in invalid:
            with self.subTest(value=value["query"], maximum=value["max_hops"]):
                public = public_plot(value)
                self.assertNotIn("query", public)
                self.assertNotIn("context_edge_count", public)

    def test_cli_plot_and_sync_accept_shortest_without_a_hop_limit(self):
        for command, target in (("plot", "liquid_tracer.plots.preview_plot"),
                                ("plot-sync", "liquid_tracer.investigation_boards.generate_and_sync")):
            with self.subTest(command=command), patch(target, return_value={}) as operation, redirect_stdout(io.StringIO()):
                self.assertEqual(main([command, "--case", str(self.case), "--goal", "connections",
                                       "--run", self.state["run_id"], "--connection-scope", "shortest"]), 0)
                self.assertEqual(operation.call_args.kwargs["connection_scope"], "shortest")
                self.assertEqual(operation.call_args.args[2], self.state["run_id"])
        args = parser().parse_args(["connections", "--case", str(self.case), "--connection-scope", "shortest"])
        self.assertEqual(args.connection_scope, "shortest")

    def test_api_pins_run_and_admits_shortest_before_archive_verification(self):
        server = SimpleNamespace(start_job=Mock(return_value={"id": "shortest-job"}))
        original = (self.case / "case.json").read_bytes()
        with patch("liquid_tracer.cli.verify_export", side_effect=AssertionError("Admission must not hash evidence")), \
                patch("liquid_tracer.plots._source", side_effect=AssertionError("Admission must not load evidence")):
            for action in ("plot", "plot-sync"):
                body = {"action": action, "goal": "connections", "run_id": "latest", "connection_scope": "shortest"}
                if action == "plot-sync":
                    body["name"] = "Shortest starter connections"
                with self.subTest(action=action):
                    self.assertEqual(workflow_action(server, self.case, read_case(self.case), body), {"id": "shortest-job"})
                    call = server.start_job.call_args
                    arguments = call.args[0]
                    self.assertEqual(arguments[:7], [action, "--case", str(self.case), "--goal", "connections",
                                                     "--run", self.state["run_id"]])
                    self.assertEqual(arguments[arguments.index("--connection-scope") + 1], "shortest")
                    self.assertEqual(call.kwargs["live"], action == "plot-sync")
        self.assertEqual((self.case / "case.json").read_bytes(), original)

    def test_api_rejects_shortest_for_other_goals_before_admission(self):
        server = SimpleNamespace(start_job=Mock())
        for goal in ("full", "pegouts"):
            with self.subTest(goal=goal), self.assertRaises(RequestError):
                workflow_action(server, self.case, read_case(self.case), {
                    "action": "plot", "goal": goal, "run_id": "latest", "connection_scope": "shortest",
                    "min_hops": 0, "max_hops": 3})
        server.start_job.assert_not_called()

    def test_shortest_worker_still_verifies_source_before_layout(self):
        path = self.archive / "trace.json"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaises(TraceError):
            preview_plot(self.case, "connections", connection_scope="shortest")
        self.layout.assert_not_called()
        self.assertEqual(list_plots(self.case), [])


class SharedShortestStarterWorkflowTests(unittest.TestCase):
    setUp = hop_workflow.SharedStarterHopWorkflowTests.setUp
    collect = hop_workflow.SharedStarterHopWorkflowTests.collect

    def test_shared_shortest_selection_is_sealed_and_cannot_replace_all_saved_cache(self):
        before = files(self.source_archive)
        saved = []
        with patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("Reuse shared evidence")), \
                patch("liquid_tracer.address_counts.ensure_counts", side_effect=AssertionError("Reuse saved counts")):
            for scope, names in (("shortest", "ab"), ("all_saved", "abcde"), ("shortest", "ab")):
                result = preview_plot(self.case, "connections", self.shared_run, data_source="shared",
                                      dataset_id=self.shared_id, connection_scope=scope, max_hops=0)
                graph, plan = reviewed_plot(self.case, result["preview_id"])
                self.assertEqual(hop_workflow.selected_transactions(graph), {tx(name) for name in names})
                self.assertEqual(result["collection_source"]["run_id"], self.shared_run)
                projection = read_json(self.case / "runs" / result["run_id"] / "trace.json")
                self.assertEqual(set(projection["transactions"]), {tx(name) for name in names})
                self.assertEqual(projection["collection_source"]["selection"],
                                 {"max_hops": None, "connection_scope": scope})
                saved.append((result, graph, plan))
        self.assertNotEqual(saved[0][0]["run_id"], saved[1][0]["run_id"])
        self.assertEqual(saved[0][0]["run_id"], saved[2][0]["run_id"])
        self.assertEqual(files(self.source_archive), before)
        self.assertNotIn("latest_run", read_case(self.case))
        self.dataset.rename(self.root / "archived-shared-source")
        for result, graph, plan in saved:
            self.assertEqual(reviewed_plot(self.case, result["preview_id"]), (graph, plan))


if __name__ == "__main__":
    unittest.main()
