"""Peg-out fee visibility changes presentation, never traced evidence or totals."""
from copy import deepcopy
import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import LBTC, TraceError, read_json, save_json
from liquid_tracer.investigations import create_investigation, read_case, update_case
from liquid_tracer.pegout_csv import pegout_csv_rows, pegout_lbtc_summary
from liquid_tracer.pegout_paths import pegout_graph, validate_query
from liquid_tracer.plot_csv import build_plot_csv
from liquid_tracer.plots import _settings, preview_plot, reviewed_plot
from liquid_tracer.transaction_csv import transaction_csv_rows
from tests.test_attribution_convergence import graph_state, tx
from tests.test_connections import saved_case
from tests.test_pegout_paths import add_pegout


class PegoutFeeVisibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.case = create_investigation(Path(temporary.name), "Peg-out fee settings",
                                         seeds=[tx("a") + ":0"],
                                         run_defaults={"include_fees": False, "layout_attempts": 1})
        state = graph_state((("a:0", "b"), ("b:0", "c")), seeds=("a:0",))
        self.endpoint = add_pegout(state, tx("c"))
        self.fee_outpoints = set()
        for amount, name in enumerate(("a", "b", "c"), 2):
            outputs = state["transactions"][tx(name)]["data"]["vout"]
            self.fee_outpoints.add(f"{tx(name)}:{len(outputs)}")
            outputs.append({"scriptpubkey": "", "scriptpubkey_type": "fee", "asset": LBTC, "value": amount})
        self.state, self.archive = saved_case(self.case, state)
        self.query = validate_query(seeds=self.state["seeds"], max_hops=2, transaction_io="complete")
        layout = patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph)
        layout.start()
        self.addCleanup(layout.stop)
        for name in ("liquid_tracer.api.Esplora.get", "liquid_tracer.address_counts.ensure_counts",
                     "liquid_tracer.miro.publish", "liquid_tracer.miro.sync"):
            blocker = patch(name, side_effect=AssertionError("Fee rendering must stay offline"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def assert_fee_visibility(self, graph, visible):
        edges = {edge["id"] for edge in graph["edges"]}
        nodes = {node["id"] for node in graph["nodes"]}
        fee_edges = {"out:" + key for key in self.fee_outpoints}
        fee_nodes = {"event:" + key for key in self.fee_outpoints}
        self.assertEqual(edges & fee_edges, fee_edges if visible else set())
        self.assertEqual(nodes & fee_nodes, fee_nodes if visible else set())
        self.assertIs(graph["include_fees"], visible)
        # Hidden fee ownership proofs may remain for safe Miro removals.
        if visible:
            self.assertEqual(set(graph["fee_items"]), fee_edges | fee_nodes)
        for node in graph["nodes"]:
            if node["kind"] == "transaction":
                original = self.state["transactions"][node["id"][3:]]["data"]
                self.assertEqual(node["details"]["transaction"]["vout"], original["vout"])

    def test_api_defaults_preserve_legacy_complete_and_path_only_graphs(self):
        complete = pegout_graph(self.state, self.query)
        self.assert_fee_visibility(complete, True)
        self.assertEqual(complete, pegout_graph(self.state, self.query, include_fees=None))
        self.assertEqual(complete, pegout_graph(self.state, self.query, include_fees=True))
        legacy = validate_query(seeds=self.state["seeds"], max_hops=2)
        default = pegout_graph(self.state, legacy)
        self.assert_fee_visibility(default, False)
        self.assertEqual(default, pegout_graph(self.state, legacy, include_fees=None))

    def test_api_flag_is_strict_and_changes_only_visible_fee_occurrences(self):
        before = deepcopy(self.state)
        shown = pegout_graph(self.state, self.query, include_fees=True)
        hidden = pegout_graph(self.state, self.query, include_fees=False)
        self.assert_fee_visibility(shown, True)
        self.assert_fee_visibility(hidden, False)
        self.assertEqual({edge["id"] for edge in shown["edges"]} - {edge["id"] for edge in hidden["edges"]},
                         {"out:" + key for key in self.fee_outpoints})
        for field in ("matches", "match_count", "outpoints", "transaction_count", "status"):
            self.assertEqual(shown["pegouts"][field], hidden["pegouts"][field])
        self.assertEqual(pegout_lbtc_summary(shown, self.state), pegout_lbtc_summary(hidden, self.state))
        for graph in (shown, hidden):
            self.assertEqual(graph["pegouts"]["context_edge_count"],
                             sum(edge.get("role", "").startswith("context") for edge in graph["edges"]))
        self.assertEqual(self.state, before)
        for value in (0, 1, "true", "false", [], {}):
            with self.subTest(value=value), self.assertRaises(TraceError):
                pegout_graph(self.state, self.query, include_fees=value)

    def test_csv_validation_still_requires_non_fee_context_and_matching_flags(self):
        hidden = pegout_graph(self.state, self.query, include_fees=False)
        # This spendable sibling is local context, not a qualifying path edge.
        context_id = "out:" + tx("c") + ":0"
        context = next(edge for edge in hidden["edges"] if edge["id"] == context_id)
        self.assertEqual(context["role"], "context_output")
        missing_context = deepcopy(hidden)
        missing_context["edges"] = [edge for edge in missing_context["edges"] if edge["id"] != context_id]
        with self.assertRaisesRegex(TraceError, "complete transaction inputs or outputs"):
            pegout_csv_rows(missing_context, self.state)
        for target in ("graph", "graph_options"):
            mismatched = deepcopy(hidden)
            if target == "graph":
                mismatched["include_fees"] = True
            else:
                mismatched["graph_options"]["include_fees"] = True
            with self.subTest(target=target), self.assertRaisesRegex(TraceError, "inconsistent fee visibility"):
                pegout_csv_rows(mismatched, self.state)

    def test_saved_previews_and_csvs_honor_both_settings_without_changing_totals(self):
        archive_before = {path.name: path.read_bytes() for path in self.archive.iterdir() if path.is_file()}
        results = {}
        for visible in (False, True):
            update_case(self.case, {"run_defaults": {"include_fees": visible, "layout_attempts": 1}})
            result = preview_plot(self.case, "pegouts", max_hops=2)
            graph, _ = reviewed_plot(self.case, result["preview_id"])
            self.assert_fee_visibility(graph, visible)
            self.assertIs(result["layout_settings"]["include_fees"], visible)
            self.assertIs(graph["plot"]["layout_settings"]["include_fees"], visible)
            rows = list(csv.DictReader(io.StringIO(build_plot_csv(self.case, result["preview_id"], "transactions.csv")["data"].decode())))
            edge_ids = {f"{row['Direction'].lower()}:{row['Transaction Hash']}:{row['Number of I/O']}" for row in rows}
            self.assertEqual(edge_ids, {edge["id"] for edge in graph["edges"]})
            self.assertEqual(len(rows), len(graph["edges"]))
            self.assertEqual(sum("FEE" in row["Address Flags"].split("; ") for row in rows),
                             len(self.fee_outpoints) if visible else 0)
            self.assertEqual(len(transaction_csv_rows(graph, self.state)), len(rows))
            results[visible] = result, graph, build_plot_csv(self.case, result["preview_id"], "endpoints.csv")["data"]
        hidden, shown = results[False], results[True]
        self.assertEqual(hidden[0]["pegout_lbtc_summary"], shown[0]["pegout_lbtc_summary"])
        self.assertEqual(hidden[0]["pegout_lbtc_summary"]["value_base_units"], "50")
        self.assertEqual(hidden[1]["pegouts"]["matches"], shown[1]["pegouts"]["matches"])
        self.assertEqual(hidden[2], shown[2])
        self.assertEqual(archive_before, {path.name: path.read_bytes() for path in self.archive.iterdir() if path.is_file()})
        self.assertEqual(read_json(self.archive / "trace.json"), self.state)

    def test_saved_fee_visible_plot_keeps_its_snapshot_after_preference_changes(self):
        update_case(self.case, {"run_defaults": {"include_fees": True, "layout_attempts": 1}})
        old = preview_plot(self.case, "pegouts", max_hops=2)
        graph, plan = reviewed_plot(self.case, old["preview_id"])
        self.assert_fee_visibility(graph, True)
        directory = Path(old["directory"])
        before = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
        update_case(self.case, {"run_defaults": {"include_fees": False, "layout_attempts": 1}})
        self.assertEqual(reviewed_plot(self.case, old["preview_id"]), (graph, plan))
        new = preview_plot(self.case, "pegouts", max_hops=2)
        self.assert_fee_visibility(reviewed_plot(self.case, new["preview_id"])[0], False)
        self.assertEqual(before, {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()})

    def test_explicit_frozen_layout_overrides_later_fee_preference(self):
        frozen = _settings(read_case(self.case))
        self.assertFalse(frozen["include_fees"])
        update_case(self.case, {"run_defaults": {"include_fees": True, "layout_attempts": 1}})
        result = preview_plot(self.case, "pegouts", max_hops=2, layout_settings=frozen)
        graph, _ = reviewed_plot(self.case, result["preview_id"])
        self.assert_fee_visibility(graph, False)
        self.assertFalse(result["layout_settings"]["include_fees"])
        self.assertTrue(read_case(self.case)["run_defaults"]["include_fees"])

    def test_standalone_saved_search_preview_honors_fees_and_preserves_old_choice(self):
        from liquid_tracer.pegouts import (ARCHIVE_FILES, _write_manifest,
                                          preview_pegouts, reviewed_pegouts)

        search_id = "f" * 16
        directory = self.case / "pegouts" / search_id
        directory.mkdir(parents=True)
        query = validate_query(seeds=self.state["seeds"], max_hops=2)
        state = deepcopy(self.state)
        state.update(run_id=search_id, pegout_query=query, started_at="2026-10-02T00:00:00Z")
        state["limits"]["max_hops"] = 2
        save_json(directory / "trace.json", state)
        save_json(directory / "query.json", query)
        save_json(directory / "evidence-index.json", [])
        _write_manifest(directory, ARCHIVE_FILES)
        archive_before = {path.name: path.read_bytes() for path in directory.iterdir()}
        saved = {}
        for visible in (True, False):
            update_case(self.case, {"run_defaults": {"include_fees": visible, "layout_attempts": 1}})
            result = preview_pegouts(self.case, search_id)
            graph, plan = reviewed_pegouts(self.case, result["preview_id"])
            self.assert_fee_visibility(graph, visible)
            self.assertIs(result["include_fees"], visible)
            self.assertIs(graph["pegouts"]["include_fees"], visible)
            with (Path(result["directory"]) / "transactions.csv").open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), len(graph["edges"]))
            self.assertEqual(sum("FEE" in row["Address Flags"].split("; ") for row in rows),
                             len(self.fee_outpoints) if visible else 0)
            saved[visible] = result, graph, plan
        old, old_graph, old_plan = saved[True]
        self.assertEqual(reviewed_pegouts(self.case, old["preview_id"]), (old_graph, old_plan))
        self.assertEqual(saved[True][1]["pegouts"]["matches"], saved[False][1]["pegouts"]["matches"])
        self.assertEqual(archive_before, {path.name: path.read_bytes() for path in directory.iterdir()})
