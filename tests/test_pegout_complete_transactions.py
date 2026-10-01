"""Peg-out pruning selects transactions; every selected transaction keeps its I/O."""
from copy import deepcopy
import unittest

from liquid_tracer.common import LBTC, TraceError
from liquid_tracer.legend import legend_notes
from liquid_tracer.miro import make_plan, validate_plan
from liquid_tracer.pegout_paths import pegout_graph, validate_query
from liquid_tracer.transaction_csv import transaction_csv_rows
from tests.test_attribution_convergence import annotation, graph_state, tx
from tests.test_pegout_paths import add_pegout, add_unspendable, mark_unspent, set_address


def query_for(state, **options):
    return validate_query(seeds=state["seeds"], transaction_io="complete", **options)


def io_ids(state, names):
    return {f"{side}:{tx(name)}:{index}"
            for name in names for side, field in (("in", "vin"), ("out", "vout"))
            for index in range(len(state["transactions"][tx(name)]["data"][field]))}


class CompleteTransactionPegoutTests(unittest.TestCase):
    def test_complete_query_is_explicit_strict_and_preserves_archived_identity(self):
        old = validate_query(tx("a"))
        self.assertNotIn("transaction_io", old)
        complete = validate_query(tx("a"), transaction_io="complete")
        self.assertEqual(complete, {**old, "transaction_io": "complete"})
        self.assertEqual(validate_query(**complete), complete)
        for invalid in (True, False, 0, 1, "", "partial", [], {}):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(TraceError, "transaction I/O"):
                validate_query(tx("a"), transaction_io=invalid)

    def test_every_selected_transaction_io_is_visible_without_expanding_branches(self):
        state = graph_state((("a:0", "b"), ("b:0", "c"), ("a:1", "d"), ("e:0", "b")),
                            raw_links=(("f:0", "b"),), seeds=("a:0",))
        endpoint = add_pegout(state, tx("c"))
        mark_unspent(state, tx("c") + ":0")
        set_address(state, tx("a") + ":1", "SYNTHETIC-pruned-branch-head")
        before = deepcopy(state)
        legacy_query = validate_query(seeds=state["seeds"])
        legacy = pegout_graph(state, legacy_query)
        graph = pegout_graph(state, {**legacy_query, "transaction_io": "complete"})
        self.assertEqual({node["id"] for node in graph["nodes"] if node["kind"] == "transaction"},
                         {"tx:" + tx(name) for name in ("a", "b", "c")})
        self.assertEqual({edge["id"] for edge in graph["edges"]}, io_ids(state, ("a", "b", "c")))
        self.assertEqual(len(graph["edges"]), len(io_ids(state, ("a", "b", "c"))))
        for key in ("matches", "match_count", "outpoints", "transaction_count", "status"):
            self.assertEqual(graph["pegouts"][key], legacy["pegouts"][key])
        self.assertEqual(graph["pegouts"]["matches"][0]["outpoint"], endpoint)
        self.assertEqual(graph["branch_structure"], legacy["branch_structure"])
        legacy_edges = {edge["id"]: edge for edge in legacy["edges"]}
        for edge in graph["edges"]:
            if edge["id"] in legacy_edges:
                self.assertEqual(edge, legacy_edges[edge["id"]])
            else:
                self.assertTrue(edge["role"].startswith("context"))
                self.assertNotIn(edge["id"], graph["branch_structure"]["edge_memberships"])
        for address in ("SYNTHETIC-pruned-branch-head", "SYNTHETIC-c-address"):
            node = next(n for n in graph["nodes"] if n["id"] == "liquid:address:" + address)
            self.assertEqual(node["role"], "address")
            self.assertTrue(all(item["trace"] is None for item in node["details"]["occurrences"]))
            self.assertNotIn("unspent_endpoints", node["details"])
            self.assertNotIn("Unspent", node["label"])
            self.assertNotIn("hop", node["label"])
        self.assertEqual(len(transaction_csv_rows(graph, state)), len(graph["edges"]))
        validate_plan(make_plan(graph))
        self.assertEqual(state, before)

    def test_special_inputs_and_every_event_output_remain_context_without_matches(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0", "a:1", "a:2"))
        for index in (1, 2):
            set_address(state, f"{tx('a')}:{index}", None)
        data = state["transactions"][tx("b")]["data"]
        data["vin"].extend([
            {"txid": tx("d"), "vout": 2, "is_pegin": True,
             "prevout": deepcopy(state["transactions"][tx("a")]["data"]["vout"][0])},
            {"is_coinbase": True},
            {"txid": tx("e"), "vout": 0, "prevout": {"scriptpubkey": "6a"}},
            {"txid": tx("f"), "vout": 0},
        ])
        excluded_pegout = add_pegout(state, tx("a"))
        unspendable = add_unspendable(state, tx("b"))
        endpoint = add_pegout(state, tx("b"))
        fee = f"{tx('b')}:{len(data['vout'])}"
        data["vout"].append({"scriptpubkey": "", "scriptpubkey_type": "fee", "value": 1, "asset": LBTC})
        query = validate_query(seeds=[tx("a") + ":0"], min_hops=1, max_hops=1, transaction_io="complete")
        graph = pegout_graph(state, query)
        edges = {edge["id"]: edge for edge in graph["edges"]}
        self.assertEqual(set(edges), io_ids(state, ("a", "b")))
        self.assertEqual([item["outpoint"] for item in graph["pegouts"]["matches"]], [endpoint])
        for key in (excluded_pegout, unspendable, fee):
            self.assertEqual(edges["out:" + key]["role"], "context_output")
            node = next(node for node in graph["nodes"] if node["id"] == "event:" + key)
            self.assertIsNone(node["details"]["trace"])
        ids = {node["id"] for node in graph["nodes"]}
        self.assertTrue({f"liquid:outpoint:{tx('a')}:1", f"liquid:outpoint:{tx('a')}:2",
                         f"liquid:outpoint:{tx('f')}:0", "bitcoin:address:SYNTHETIC-a-address",
                         "liquid:address:SYNTHETIC-a-address", f"coinbase:{tx('b')}:2"} <= ids)
        self.assertTrue(graph["include_fees"])
        self.assertEqual(set(graph["fee_items"]), {"out:" + fee, "event:" + fee})
        self.assertEqual(len(transaction_csv_rows(graph, state)), len(edges))
        validate_plan(make_plan(graph))

    def test_grouping_remains_optional_and_preserves_named_addresses_and_each_connector(self):
        state = graph_state((("a:0", "b"),),
                            raw_links=(("c:0", "b"), ("d:0", "b"), ("e:0", "b")), seeds=("a:0",))
        state["labels"] = [annotation(stop=False, name="Known service", address="SYNTHETIC-e-address")]
        add_pegout(state, tx("b"))
        query = query_for(state)
        ordinary = pegout_graph(state, query)
        grouped = pegout_graph(state, query, group_context_inputs=True)
        summary, = [node for node in grouped["nodes"] if node["kind"] == "context_group"]
        self.assertEqual({node["details"]["address"] for node in summary["details"]["members"]},
                         {"SYNTHETIC-c-address", "SYNTHETIC-d-address"})
        named = next(node for node in grouped["nodes"] if node["id"] == "liquid:address:SYNTHETIC-e-address")
        self.assertIn("Known service", named["label"])
        self.assertEqual({edge["id"] for edge in grouped["edges"]}, io_ids(state, ("a", "b")))
        self.assertEqual(grouped["pegouts"], ordinary["pegouts"])
        self.assertEqual(transaction_csv_rows(grouped, state), transaction_csv_rows(ordinary, state))
        self.assertNotIn("include_context", query)
        self.assertEqual(pegout_graph(state, {**query, "include_context": False}), ordinary)
        validate_plan(make_plan(grouped))

    def test_hop_boundary_and_stop_rules_prune_transactions_but_keep_branch_heads(self):
        state = graph_state((("a:0", "b"), ("b:0", "c")), seeds=("a:0",))
        first = add_pegout(state, tx("b"))
        add_pegout(state, tx("c"))
        for boundary in ("hop", "stop", "service_cap"):
            current = deepcopy(state)
            query = query_for(current, max_hops=1 if boundary == "hop" else 10)
            if boundary == "stop":
                current["labels"] = [annotation(stop=True, address="SYNTHETIC-b-address")]
            elif boundary == "service_cap":
                current["labels"] = [{**annotation(stop=False), "hop_limit": 1}]
            with self.subTest(boundary=boundary):
                graph = pegout_graph(current, query)
                self.assertEqual([item["outpoint"] for item in graph["pegouts"]["matches"]], [first])
                self.assertNotIn("tx:" + tx("c"), {node["id"] for node in graph["nodes"]})
                self.assertEqual({edge["id"] for edge in graph["edges"]}, io_ids(current, ("a", "b")))
                branch = next(edge for edge in graph["edges"] if edge["id"] == "out:" + tx("b") + ":0")
                self.assertEqual(branch["role"], "context_output")

    def test_named_group_context_does_not_inherit_hop_measure(self):
        state = graph_state((("a:0", "b"), ("b:0", "c"), ("b:1", "d")), seeds=("a:0",))
        state["hop_reference_name"] = "Known group"
        state["labels"] = [annotation(stop=False, name="Known group")]
        endpoint = add_pegout(state, tx("c"))
        graph = pegout_graph(state, query_for(state, hop_reference_name="Known group"))
        self.assertEqual([item["outpoint"] for item in graph["pegouts"]["matches"]], [endpoint])
        branch = next(edge for edge in graph["edges"] if edge["id"] == "out:" + tx("b") + ":1")
        self.assertEqual(branch["role"], "context_output")
        self.assertNotIn("reference_hops", branch["details"])
        self.assertNotIn("seed_depth", branch["details"])
        node = next(node for node in graph["nodes"] if node["id"] == branch["target"])
        occurrence = next(item for item in node["details"]["occurrences"] if item["outpoint"] == tx("b") + ":1")
        self.assertIsNone(occurrence["trace"])
        self.assertNotIn("reference_hops", occurrence)

    def test_empty_result_stays_empty_and_legend_explains_complete_io(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        query = query_for(state, include_unspendable=True)
        graph = pegout_graph(state, query)
        self.assertEqual((graph["nodes"], graph["edges"], graph["pegouts"]["context_edge_count"]), ([], [], 0))
        add_pegout(state, tx("b"))
        graph = pegout_graph(state, query)
        notes = " ".join(legend_notes(graph))
        self.assertIn("Every input and output", notes)
        self.assertIn("including fees", notes)
        self.assertIn("excluded from endpoint selection", notes)
        self.assertNotIn("Only qualifying paths are plotted", notes)
        self.assertIn("Every input and output", graph["notice"])


if __name__ == "__main__":
    unittest.main()
