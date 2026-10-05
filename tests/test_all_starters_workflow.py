"""Visible starters remain distinct from verified pairwise connection paths."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError, digest, read_json, save_json
from liquid_tracer.connections import connection_graph
from liquid_tracer.investigations import create_investigation, read_case
from liquid_tracer.plots import list_plots, plot_files, preview_plot, reviewed_plot
from liquid_tracer.workflow_api import public_plot
from tests import test_shared_projection
from tests.test_attribution_convergence import graph_state, tx
from tests.test_connections import saved_case
from tests.test_connections_complete_workflow import csv_rows, files, input_output_keys


SUMMARY_FIELDS = ("includes_all_starters", "starting_transaction_count", "unconnected_starting_transactions")
PUBLIC_FIELDS = ("includes_all_starters", "starting_transaction_count", "unconnected_starting_transaction_count")


def transaction_ids(graph):
    return {node["id"].removeprefix("tx:") for node in graph["nodes"] if node["kind"] == "transaction"}


class AllStarterPlotWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.case = create_investigation(Path(temporary.name), "All selected starters",
                                         run_defaults={"layout_attempts": 1, "include_fees": False})
        self.state, self.archive = saved_case(self.case, graph_state(
            (("a:0", "b"), ("c:0", "d")), seeds=("a:0", "b:0", "c:0")))
        for target, action in (
            ("liquid_tracer.elk_layout.optimize_graph", lambda graph, **kwargs: graph),
            ("liquid_tracer.api.Esplora.get", AssertionError("Use saved evidence only")),
            ("liquid_tracer.address_counts.ensure_counts", AssertionError("Use saved counts only")),
            ("liquid_tracer.miro.publish", AssertionError("Preview must not publish")),
        ):
            guard = patch(target, side_effect=action)
            guard.start()
            self.addCleanup(guard.stop)

    def test_every_search_scope_keeps_isolated_starter_without_expanding_its_branch(self):
        before = files(self.archive)
        for scope in ("shortest", "all_saved", "hop_limited"):
            with self.subTest(scope=scope):
                result = preview_plot(self.case, "connections", connection_scope=scope, max_hops=1)
                graph, _ = reviewed_plot(self.case, result["preview_id"])
                self.assertEqual(transaction_ids(graph), {tx(name) for name in "abc"})
                self.assertEqual(graph["connections"]["pairs"],
                                 [{"source": tx("a"), "target": tx("b"), "shortest_hops": 1}])
                self.assertEqual(graph["connections"]["outpoints"], [tx("a") + ":0"])
                self.assertEqual(result["transaction_count"], 3)
                self.assertEqual(result["connection_count"], 1)
                self.assertTrue(result["includes_all_starters"])
                self.assertEqual(result["starting_transaction_count"], 3)
                self.assertEqual(result["unconnected_starting_transactions"], [tx("c")])
                self.assertFalse(result["empty"])
                rows = csv_rows(result)
                self.assertEqual({(row["Transaction Hash"], row["Direction"], row["Number of I/O"]) for row in rows},
                                 input_output_keys(self.state, {tx(name) for name in "abc"}))
                context = next(edge for edge in graph["edges"] if edge["id"] == "out:" + tx("c") + ":0")
                self.assertEqual(context["role"], "context_output")
        self.assertEqual(files(self.archive), before)

    def test_zero_connected_pairs_still_exports_reviewable_preview(self):
        self.state, _ = saved_case(self.case, graph_state(
            (("a:0", "d"), ("b:0", "e"), ("c:0", "f")), seeds=("a:0", "b:0", "c:0")))
        result = preview_plot(self.case, "connections", connection_scope="shortest")
        graph, plan = reviewed_plot(self.case, result["preview_id"])
        self.assertEqual(result["status"], "no_connection_found")
        self.assertEqual(result["connection_count"], 0)
        self.assertFalse(result["empty"])
        self.assertTrue(result["reviewable"])
        self.assertEqual(transaction_ids(graph), {tx(name) for name in "abc"})
        self.assertTrue(plan["shapes"])
        self.assertEqual(result["unconnected_starting_transactions"], [tx(name) for name in "abc"])
        self.assertNotIn("Nothing is plotted", graph["notice"])
        self.assertTrue((Path(result["directory"]) / "graph.svg").is_file())
        self.assertTrue(all(item["reviewable"] for item in list_plots(self.case)))

    def test_public_metadata_exposes_counts_without_raw_transaction_list(self):
        result = preview_plot(self.case, "connections", connection_scope="shortest")
        public = public_plot(result)
        self.assertEqual({key: public[key] for key in PUBLIC_FIELDS}, {
            "includes_all_starters": True, "starting_transaction_count": 3,
            "unconnected_starting_transaction_count": 1})
        self.assertNotIn("unconnected_starting_transactions", public)
        self.assertEqual(public["query"], result["query"])

    def test_public_metadata_drops_malformed_starter_summary_as_a_unit(self):
        result = preview_plot(self.case, "connections", connection_scope="shortest")
        changes = [
            {"includes_all_starters": value} for value in (None, False, 1, "true")
        ] + [
            {"starting_transaction_count": value} for value in (None, True, 1, -1, 1.5, "3", 2 ** 53)
        ] + [
            {"unconnected_starting_transactions": value}
            for value in (None, "abc", [tx("c"), tx("c")], [True], ["bad"], [tx(name) for name in "abcd"])
        ]
        for change in changes:
            with self.subTest(change=change):
                public = public_plot({**result, **change})
                self.assertTrue(all(key not in public for key in PUBLIC_FIELDS))
                self.assertNotIn("unconnected_starting_transactions", public)
        legacy = {key: value for key, value in result.items() if key not in SUMMARY_FIELDS}
        self.assertTrue(all(key not in public_plot(legacy) for key in PUBLIC_FIELDS))

    def test_saved_summary_disagreement_fails_after_file_checksums_are_updated(self):
        result = preview_plot(self.case, "connections", connection_scope="shortest")
        directory = Path(result["directory"])
        original = read_json(directory / "graph.json")
        mutations = (
            lambda value: value["plot"].update(includes_all_starters=False),
            lambda value: value["plot"].update(starting_transaction_count=2),
            lambda value: value["plot"].update(unconnected_starting_transactions=[]),
            lambda value: value["plot"].pop("includes_all_starters"),
        )
        for mutate in mutations:
            graph = deepcopy(original)
            mutate(graph)
            save_json(directory / "graph.json", graph)
            save_json(directory / "plot.json", graph["plot"])
            (directory / "SHA256SUMS").write_text("".join(
                digest((directory / name).read_bytes()) + "  " + name + "\n"
                for name in sorted(plot_files(directory) - {"SHA256SUMS"})))
            with self.assertRaises(TraceError):
                reviewed_plot(self.case, result["preview_id"])

    def test_old_complete_preview_without_visibility_marker_remains_unchanged(self):
        saved_case(self.case, graph_state((("a:0", "b"),), seeds=("a:0", "b:0")))

        def markerless(*args, **kwargs):
            graph = connection_graph(*args, **kwargs)
            for key in SUMMARY_FIELDS:
                graph["connections"].pop(key, None)
            graph["graph_options"].pop("includes_all_starters", None)
            return graph

        with patch("liquid_tracer.connections.connection_graph", side_effect=markerless):
            old = preview_plot(self.case, "connections", connection_scope="shortest")
        graph, plan = reviewed_plot(self.case, old["preview_id"])
        before = files(Path(old["directory"]))
        self.assertEqual(old["query"]["transaction_io"], "complete")
        self.assertTrue(all(key not in old for key in SUMMARY_FIELDS))
        current = preview_plot(self.case, "connections", connection_scope="shortest")
        self.assertTrue(current["includes_all_starters"])
        self.assertEqual(reviewed_plot(self.case, old["preview_id"]), (graph, plan))
        self.assertEqual(files(Path(old["directory"])), before)
        self.assertTrue(all(item["reviewable"] for item in list_plots(self.case)))


class SharedAllStarterWorkflowTests(unittest.TestCase):
    collect = test_shared_projection.SharedProjectionTests.collect

    def setUp(self):
        test_shared_projection.SharedProjectionTests.setUp(self)
        self.case = create_investigation(self.root, "All shared starters", fixture=self.fixture,
                                         seeds=[tx(name) + ":0" for name in "abf"],
                                         run_defaults={"layout_attempts": 1, "include_fees": False})
        self.collect()

    def test_shared_projection_keeps_unconnected_seed_but_not_its_descendants(self):
        before = files(self.source_archive)
        with patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("Reuse shared evidence")), \
                patch("liquid_tracer.address_counts.ensure_counts", side_effect=AssertionError("Reuse saved counts")):
            result = preview_plot(self.case, "connections", self.shared_run, data_source="shared",
                                  dataset_id=self.shared_id, connection_scope="shortest")
            graph, _ = reviewed_plot(self.case, result["preview_id"])
        self.assertEqual(transaction_ids(graph), {tx(name) for name in "abcf"})
        self.assertEqual(result["unconnected_starting_transactions"], [tx("f")])
        self.assertEqual(result["starting_transaction_count"], 3)
        self.assertEqual(graph["connections"]["pairs"],
                         [{"source": tx("a"), "target": tx("b"), "shortest_hops": 2}])
        projected = read_json(self.case / "runs" / result["run_id"] / "trace.json")
        self.assertEqual(set(projected["transactions"]), {tx(name) for name in "abcf"})
        self.assertEqual(files(self.source_archive), before)
        self.assertNotIn("latest_run", read_case(self.case))


if __name__ == "__main__":
    unittest.main()
