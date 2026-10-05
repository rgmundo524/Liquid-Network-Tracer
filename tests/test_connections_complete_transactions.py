"""Starter path selection prunes transactions without truncating their local I/O."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import LBTC, TraceError, digest, read_json, save_json
from liquid_tracer.connections import connection_graph, preview_connections, reviewed_connections
from liquid_tracer.investigations import create_investigation, update_case
from liquid_tracer.export import legend_lines
from liquid_tracer.legend import legend_notes
from liquid_tracer.miro import make_plan, validate_plan
from liquid_tracer.transaction_csv import transaction_csv_rows
from tests.test_attribution_convergence import annotation, graph_state, tx
from tests.test_connections import saved_case
from tests.test_pegout_paths import add_pegout, add_unspendable, mark_unspent, set_address


def complete(state, **options):
    return connection_graph(state, connection_scope="all_saved", transaction_io="complete", **options)


def io_ids(state, names):
    return {f"{side}:{tx(name)}:{index}"
            for name in names for side, field in (("in", "vin"), ("out", "vout"))
            for index in range(len(state["transactions"][tx(name)]["data"][field]))}


class CompleteTransactionConnectionTests(unittest.TestCase):
    def test_fees_are_explicit_display_context_with_hidden_removal_proofs(self):
        state = graph_state((("a:0", "b"),))
        data = state["transactions"][tx("b")]["data"]
        fee = f"{tx('b')}:{len(data['vout'])}"
        data["vout"].append({"scriptpubkey": "", "scriptpubkey_type": "fee", "value": 12, "asset": LBTC})
        before = deepcopy(state)
        for scope, maximum in (("all_saved", 10), ("hop_limited", 1)):
            with self.subTest(scope=scope):
                options = {"connection_scope": scope, "transaction_io": "complete"}
                hidden = connection_graph(state, maximum, **options)
                visible = connection_graph(state, maximum, include_fees=True, **options)
                self.assertFalse(hidden["include_fees"])
                self.assertNotIn("out:" + fee, {edge["id"] for edge in hidden["edges"]})
                self.assertNotIn("event:" + fee, {node["id"] for node in hidden["nodes"]})
                self.assertEqual(set(visible["fee_items"]), {"out:" + fee, "event:" + fee})
                self.assertEqual(hidden["fee_items"], visible["fee_items"])
                self.assertEqual(hidden["connections"]["outpoints"], visible["connections"]["outpoints"])
                self.assertEqual(hidden["connections"]["context_edge_count"] + 1,
                                 visible["connections"]["context_edge_count"])
                self.assertEqual(len(transaction_csv_rows(hidden, state)), len(hidden["edges"]))
                for renderer in (legend_lines, legend_notes):
                    self.assertIn("Fee flows are hidden", " ".join(renderer(hidden)))
                    self.assertIn("including fees", " ".join(renderer(visible)))
                validate_plan(make_plan(hidden))
                validate_plan(make_plan(visible))
        legacy = connection_graph(state, connection_scope="all_saved", include_fees=True)
        self.assertFalse(legacy["include_fees"])
        self.assertEqual(legacy["fee_items"], {})
        for value in (None, 1, 0, "true", [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(TraceError, "Fee display"):
                complete(state, include_fees=value)
        self.assertEqual(state, before)

    def test_all_local_io_preserves_selection_without_expanding_context_or_descendants(self):
        state = graph_state((("a:0", "c"), ("c:0", "b"), ("a:1", "d"), ("e:0", "c")),
                            raw_links=(("f:0", "c"),))
        set_address(state, tx("a") + ":1", "SYNTHETIC-unfollowed-head")
        mark_unspent(state, tx("b") + ":0")
        before = deepcopy(state)
        legacy = connection_graph(state, connection_scope="all_saved")
        graph = complete(state)
        self.assertEqual({node["id"] for node in graph["nodes"] if node["kind"] == "transaction"},
                         {"tx:" + tx(name) for name in "abc"})
        self.assertEqual({edge["id"] for edge in graph["edges"]}, io_ids(state, "abc"))
        for key in ("pairs", "outpoints", "transaction_count", "connection_count", "status"):
            self.assertEqual(graph["connections"][key], legacy["connections"][key])
        self.assertEqual(graph["connections"]["context_edge_count"], len(graph["edges"]) - len(legacy["edges"]))
        legacy_ids = {edge["id"] for edge in legacy["edges"]}
        for edge in graph["edges"]:
            if edge["id"] not in legacy_ids:
                self.assertTrue(edge["role"].startswith("context"))
                self.assertNotIn(edge["id"], graph["branch_structure"]["edge_memberships"])
        for key in (tx("a") + ":1", tx("b") + ":0"):
            node = next(node for node in graph["nodes"] if any(
                item["outpoint"] == key for item in node.get("details", {}).get("occurrences", [])))
            self.assertTrue(all(item["trace"] is None for item in node["details"]["occurrences"]))
            self.assertNotIn("unspent_endpoints", node["details"])
            self.assertNotIn(node["id"], graph["branch_structure"]["node_memberships"])
        starters = [node for node in graph["nodes"] if node.get("role") == "starting_transaction"]
        self.assertEqual({node["id"] for node in starters}, {"tx:" + tx(name) for name in "ab"})
        self.assertTrue(all(node["label"].count("INPUT MERGE") <= 1 for node in graph["nodes"]))
        self.assertEqual(len(transaction_csv_rows(graph, state)), len(graph["edges"]))
        validate_plan(make_plan(graph))
        self.assertEqual(state, before)

    def test_special_inputs_fees_and_events_are_complete_without_becoming_paths(self):
        state = graph_state((("a:0", "b"),))
        data = state["transactions"][tx("b")]["data"]
        data["vin"].extend([
            {"is_coinbase": True},
            {"txid": tx("e"), "vout": 0, "is_pegin": True,
             "prevout": deepcopy(state["transactions"][tx("a")]["data"]["vout"][0])},
            {"txid": tx("f"), "vout": 0},
        ])
        pegout = add_pegout(state, tx("b"))
        op_return = add_unspendable(state, tx("b"))
        fee = f"{tx('b')}:{len(data['vout'])}"
        data["vout"].append({"scriptpubkey": "", "scriptpubkey_type": "fee", "value": 12, "asset": LBTC})
        graph = complete(state, include_fees=True)
        edges = {edge["id"]: edge for edge in graph["edges"]}
        self.assertEqual(set(edges), io_ids(state, "ab"))
        self.assertEqual(graph["connections"]["outpoints"], [tx("a") + ":0"])
        for outpoint in (pegout, op_return, fee):
            self.assertEqual(edges["out:" + outpoint]["role"], "context_output")
            node = next(node for node in graph["nodes"] if node["id"] == "event:" + outpoint)
            self.assertIsNone(node["details"]["trace"])
        self.assertTrue(graph["include_fees"])
        self.assertEqual(set(graph["fee_items"]), {"out:" + fee, "event:" + fee})
        rows = transaction_csv_rows(graph, state)
        self.assertEqual(len(rows), len(edges))
        self.assertTrue(any("COINBASE" in row["Address Flags"] for row in rows))
        self.assertTrue(any("PEG-IN" in row["Address Flags"] for row in rows))
        validate_plan(make_plan(graph))

    def test_grouped_context_keeps_all_connectors_csv_and_attributed_addresses(self):
        state = graph_state((("a:0", "b"),), raw_links=(("c:0", "b"), ("d:0", "b"), ("e:0", "b")))
        state["labels"] = [annotation(stop=False, name="Known service", address="SYNTHETIC-e-address")]
        ordinary = complete(state)
        grouped = complete(state, group_context_inputs=True)
        summary, = [node for node in grouped["nodes"] if node["kind"] == "context_group"]
        self.assertEqual({node["details"]["address"] for node in summary["details"]["members"]},
                         {"SYNTHETIC-c-address", "SYNTHETIC-d-address"})
        named = next(node for node in grouped["nodes"] if node["id"] == "liquid:address:SYNTHETIC-e-address")
        self.assertIn("Known service", named["label"])
        self.assertEqual({edge["id"] for edge in grouped["edges"]}, io_ids(state, "ab"))
        self.assertEqual(transaction_csv_rows(grouped, state), transaction_csv_rows(ordinary, state))
        self.assertEqual(grouped["connections"], ordinary["connections"])
        validate_plan(make_plan(grouped))

    def test_missing_context_prevout_resolves_excluded_saved_funder_without_mutating_evidence(self):
        for prevout in (None, {}, {"scriptpubkey_address": "SYNTHETIC-c-address"}):
            with self.subTest(prevout=prevout):
                state = graph_state((("a:0", "b"),), raw_links=(("c:0", "b"),))
                state["transactions"][tx("b")]["data"]["vin"][1]["prevout"] = prevout
                before = deepcopy(state)
                graph = complete(state)
                self.assertNotIn("tx:" + tx("c"), {node["id"] for node in graph["nodes"]})
                context = next(node for node in graph["nodes"] if node["id"] == "liquid:address:SYNTHETIC-c-address")
                self.assertEqual(context["details"]["address"], "SYNTHETIC-c-address")
                rows = transaction_csv_rows(graph, state)
                row = next(row for row in rows if row["Transaction Hash"] == tx("b")
                           and row["Direction"] == "IN" and row["Number of I/O"] == 1)
                self.assertEqual(row["Address Hash"], "SYNTHETIC-c-address")
                self.assertEqual(state, before)

    def test_complete_mode_shows_all_starters_and_legacy_keeps_original_membership(self):
        graph = complete(graph_state())
        self.assertEqual({node["id"] for node in graph["nodes"] if node["kind"] == "transaction"},
                         {"tx:" + tx("a"), "tx:" + tx("b")})
        self.assertEqual(graph["connections"]["connection_count"], 0)
        self.assertEqual(graph["connections"]["context_edge_count"], 2)
        self.assertEqual(len(transaction_csv_rows(graph, graph_state())), 2)
        state = graph_state((("a:0", "b"),))
        legacy = connection_graph(state, connection_scope="all_saved")
        self.assertEqual(len(legacy["edges"]), 2)
        self.assertFalse(legacy["include_fees"])
        self.assertNotIn("transaction_io", legacy["graph_options"])
        self.assertNotIn("transaction_io", legacy["connections"])
        self.assertEqual(legacy["address_mode"], complete(state)["address_mode"])
        for value in (True, False, "", "partial", 1, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(TraceError, "transaction I/O"):
                connection_graph(state, transaction_io=value)
        for value in (1, "yes", None):
            with self.subTest(value=value), self.assertRaisesRegex(TraceError, "grouping"):
                complete(state, group_context_inputs=value)

    def test_standalone_preview_uses_complete_io_and_current_grouping(self):
        with tempfile.TemporaryDirectory() as temporary, patch("liquid_tracer.elk_layout.optimize_graph",
                side_effect=lambda graph, **kwargs: graph), patch("liquid_tracer.api.Esplora.get",
                side_effect=AssertionError("Saved evidence only")):
            case = create_investigation(Path(temporary), "Complete starter context")
            state = graph_state((("a:0", "b"),), raw_links=(("c:0", "b"), ("d:0", "b")))
            saved_case(case, state)
            update_case(case, {"run_defaults": {"group_context_inputs": True}})
            preview = preview_connections(case)
            graph, plan = reviewed_connections(case, preview["preview_id"])
            self.assertEqual(preview["transaction_io"], "complete")
            self.assertFalse(preview["include_fees"])
            self.assertTrue(preview["group_context_inputs"])
            self.assertEqual(graph["context_groups"]["group_count"], 1)
            validate_plan(plan)
            update_case(case, {"run_defaults": {"group_context_inputs": False}})
            with self.assertRaisesRegex(TraceError, "changed"):
                reviewed_connections(case, preview["preview_id"])

    def test_standalone_preview_rejects_disagreeing_complete_marker(self):
        with tempfile.TemporaryDirectory() as temporary, patch("liquid_tracer.elk_layout.optimize_graph",
                side_effect=lambda graph, **kwargs: graph):
            case = create_investigation(Path(temporary), "Marker consistency")
            saved_case(case, graph_state((("a:0", "b"),)))
            preview = preview_connections(case)
            directory = Path(preview["directory"])
            graph = read_json(directory / "graph.json")
            graph["graph_options"].pop("transaction_io")
            save_json(directory / "graph.json", graph)
            manifest = directory / "SHA256SUMS"
            manifest.write_text("".join(digest((directory / line.split("  ")[1]).read_bytes()) + "  "
                                        + line.split("  ")[1] + "\n" for line in manifest.read_text().splitlines()))
            with self.assertRaisesRegex(TraceError, "transaction I/O"):
                reviewed_connections(case, preview["preview_id"])


if __name__ == "__main__":
    unittest.main()
