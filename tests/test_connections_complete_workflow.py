"""Starter-path selection keeps complete local I/O in saved plots and exports."""
import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import LBTC
from liquid_tracer.connections import connection_graph, preview_connections, reviewed_connections
from liquid_tracer.investigations import create_investigation, read_case, update_case
from liquid_tracer.plots import _query, list_plots, preview_plot, reviewed_plot
from liquid_tracer.workflow_api import public_plot
from tests.test_attribution_convergence import graph_state, tx
from tests.test_connections import saved_case
from tests.test_pegout_paths import add_pegout
from tests import test_shared_projection


def complete_state():
    state = graph_state((("a:0", "c"), ("c:0", "b"), ("a:1", "d")),
                        seeds=("a:0", "b:0"), raw_links=(("e:0", "c"), ("f:0", "c")))
    add_pegout(state, tx("b"))
    for name in "acb":
        state["transactions"][tx(name)]["data"]["vout"].append(
            {"scriptpubkey": "", "scriptpubkey_type": "fee", "asset": LBTC, "value": 17})
    return state


def input_output_keys(state, selected):
    return {(txid, direction, str(index)) for txid in selected
            for direction, field in (("IN", "vin"), ("OUT", "vout"))
            for index, _ in enumerate(state["transactions"][txid]["data"][field])}


def csv_rows(result):
    with (Path(result["directory"]) / "transactions.csv").open(newline="") as stream:
        return list(csv.DictReader(stream))


def files(directory):
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


class CompleteStarterWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.case = create_investigation(Path(temporary.name), "Complete starter paths",
                run_defaults={"layout_attempts": 1, "include_fees": False,
                              "group_context_inputs": True, "hub_addresses": ["H" * 34]})
        self.state, self.archive = saved_case(self.case, complete_state())
        for name, replacement in (
            ("liquid_tracer.elk_layout.optimize_graph", lambda graph, **kwargs: graph),
            ("liquid_tracer.api.Esplora.get", AssertionError("Use saved evidence only")),
            ("liquid_tracer.address_counts.ensure_counts", AssertionError("Use saved counts only")),
            ("liquid_tracer.miro.publish", AssertionError("Preview does not publish")),
        ):
            guard = patch(name, side_effect=replacement)
            guard.start()
            self.addCleanup(guard.stop)

    def test_new_plot_groups_context_but_exports_every_input_output_and_fee(self):
        before = files(self.archive)
        result = preview_plot(self.case, "connections")
        graph, plan = reviewed_plot(self.case, result["preview_id"])
        self.assertEqual(result["query"], {"connection_scope": "all_saved", "transaction_io": "complete"})
        self.assertTrue(result["layout_settings"]["include_fees"])
        self.assertTrue(result["layout_settings"]["group_context_inputs"])
        self.assertEqual(result["layout_settings"]["hub_addresses"], [])
        selected = {tx(name) for name in "acb"}
        self.assertEqual({node["id"] for node in graph["nodes"] if node["kind"] == "transaction"},
                         {"tx:" + txid for txid in selected})
        rows = csv_rows(result)
        self.assertEqual({(row["Transaction Hash"], row["Direction"], row["Number of I/O"]) for row in rows},
                         input_output_keys(self.state, selected))
        self.assertEqual(len(rows), len(input_output_keys(self.state, selected)))
        grouped, = [node for node in graph["nodes"] if node["kind"] == "context_group"]
        self.assertEqual(grouped["details"]["address_count"], 2)
        context = [row for row in rows if row["Address Hash"] in {"SYNTHETIC-e-address", "SYNTHETIC-f-address"}]
        self.assertEqual({(row["Address Hash"], row["Direction"], row["Number of I/O"]) for row in context},
                         {("SYNTHETIC-e-address", "IN", "1"), ("SYNTHETIC-f-address", "IN", "2")})
        self.assertTrue(all("CONTEXT" in row["Address Flags"] for row in context))
        self.assertTrue(any(row["Transaction Hash"] == tx("a") and row["Number of I/O"] == "1"
                            and row["Direction"] == "OUT" for row in rows))
        self.assertEqual(len(plan["connectors"]), len(graph["edges"]))
        self.assertEqual(files(self.archive), before)

    def test_public_metadata_admits_only_complete_marker_and_valid_context_count(self):
        result = preview_plot(self.case, "connections")
        public = public_plot(result)
        self.assertEqual(public["query"], result["query"])
        self.assertGreater(public["context_edge_count"], 0)
        for invalid in (None, True, -1, 1.5, "1", [], {}):
            with self.subTest(count=invalid):
                self.assertNotIn("context_edge_count", public_plot({**result, "context_edge_count": invalid}))
        for invalid in (None, True, "paths", "COMPLETE", {"path": "/private/path"}):
            with self.subTest(marker=invalid):
                value = public_plot({**result, "query": {**result["query"], "transaction_io": invalid}})
                self.assertNotIn("query", value)
                self.assertNotIn("context_edge_count", value)
        legacy = public_plot({**result, "query": {"connection_scope": "all_saved"}})
        self.assertEqual(legacy["query"], {"connection_scope": "all_saved"})
        self.assertNotIn("context_edge_count", legacy)

    def test_markerless_saved_plot_keeps_path_only_graph_csv_and_bytes(self):
        def legacy_query(*args, **kwargs):
            return _query(*args, **{**kwargs, "transaction_io": None})

        with patch("liquid_tracer.plots._query", side_effect=legacy_query):
            old = preview_plot(self.case, "connections")
        old_graph, old_plan = reviewed_plot(self.case, old["preview_id"])
        original = files(Path(old["directory"]))
        self.assertEqual(old["query"], {"connection_scope": "all_saved"})
        self.assertFalse(old_graph["include_fees"])
        self.assertEqual(len(csv_rows(old)), 4)
        fresh = preview_plot(self.case, "connections")
        self.assertGreater(len(csv_rows(fresh)), len(csv_rows(old)))
        self.assertEqual(reviewed_plot(self.case, old["preview_id"]), (old_graph, old_plan))
        self.assertEqual(files(Path(old["directory"])), original)
        update_case(self.case, {"run_defaults": {"group_context_inputs": False}})
        self.assertEqual(reviewed_plot(self.case, old["preview_id"]), (old_graph, old_plan))
        self.assertEqual(files(Path(old["directory"])), original)
        self.assertTrue(all(item["reviewable"] for item in list_plots(self.case)))

    def test_standalone_preview_preserves_older_markerless_snapshot(self):
        def legacy_graph(state, maximum, **kwargs):
            return connection_graph(state, maximum, **{**kwargs, "transaction_io": None})

        with patch("liquid_tracer.connections.connection_graph", side_effect=legacy_graph):
            old = preview_connections(self.case)
        old_graph, old_plan = reviewed_connections(self.case, old["preview_id"])
        original = files(Path(old["directory"]))
        self.assertNotIn("transaction_io", old_graph["connections"])
        self.assertEqual(len(csv_rows(old)), 4)
        fresh = preview_connections(self.case)
        graph, _ = reviewed_connections(self.case, fresh["preview_id"])
        self.assertEqual(graph["connections"]["transaction_io"], "complete")
        self.assertEqual({(row["Transaction Hash"], row["Direction"], row["Number of I/O"])
                          for row in csv_rows(fresh)}, input_output_keys(self.state, {tx(name) for name in "acb"}))
        self.assertEqual(reviewed_connections(self.case, old["preview_id"]), (old_graph, old_plan))
        self.assertEqual(files(Path(old["directory"])), original)


class SharedCompleteStarterWorkflowTests(unittest.TestCase):
    setUp = test_shared_projection.SharedProjectionTests.setUp
    collect = test_shared_projection.SharedProjectionTests.collect

    def test_shared_complete_plot_keeps_recipient_starters_and_branch_heads_only(self):
        self.case = create_investigation(self.root, "Combined active", fixture=self.fixture,
                                         seeds=[tx("a") + ":0", tx("b") + ":0"],
                                         run_defaults={"layout_attempts": 1})
        self.collect()
        original = files(self.source_archive)
        with patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("No collection from plot")):
            plot = preview_plot(self.case, "connections", self.shared_run,
                                data_source="shared", dataset_id=self.shared_id)
            graph, _ = reviewed_plot(self.case, plot["preview_id"])
        selected = {tx(name) for name in "acb"}
        self.assertEqual({node["id"] for node in graph["nodes"] if node["kind"] == "transaction"},
                         {"tx:" + txid for txid in selected})
        self.assertEqual({(row["Transaction Hash"], row["Direction"], row["Number of I/O"])
                          for row in csv_rows(plot)}, input_output_keys(self.source, selected))
        self.assertEqual(graph["plot"]["collection_source"]["projection_seeds"], [tx("a") + ":0", tx("b") + ":0"])
        self.assertEqual(plot["collection_source"]["run_id"], self.shared_run)
        self.assertEqual(files(self.source_archive), original)
        self.assertNotIn("latest_run", read_case(self.case))


if __name__ == "__main__":
    unittest.main()
