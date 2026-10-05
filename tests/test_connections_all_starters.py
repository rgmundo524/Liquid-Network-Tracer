"""Complete starter previews retain every seed without inventing trace paths."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import LBTC, TraceError
from liquid_tracer.connections import (connection_graph, connecting_outpoints, preview_connections,
                                       reviewed_connections, validate_starter_visibility)
from liquid_tracer.investigations import create_investigation
from tests.test_attribution_convergence import graph_state, tx
from tests.test_connections import saved_case
from tests.test_named_hop_plots import named_state


def point(name, number=0):
    return f"{tx(name)}:{number}"


def complete(state, scope="shortest", **options):
    return connection_graph(state, 10, connection_scope=scope, transaction_io="complete", **options)


def transactions(graph):
    return {node["id"].removeprefix("tx:"): node for node in graph["nodes"] if node["kind"] == "transaction"}


class AllStarterGraphTests(unittest.TestCase):
    def test_all_scopes_retain_extra_unconnected_starter_and_same_three_pairs(self):
        state = graph_state((("a:0", "b"), ("b:0", "c"), ("f:0", "e")),
                            seeds=("a:0", "b:0", "c:0", "f:0"))
        before = deepcopy(state)
        for scope in ("shortest", "all_saved", "hop_limited"):
            with self.subTest(scope=scope):
                expected = connecting_outpoints(state, 10, connection_scope=scope)
                graph = complete(state, scope)
                report = graph["connections"]
                self.assertEqual(set(transactions(graph)), {tx(name) for name in "abcf"})
                self.assertEqual(report["pairs"], expected["pairs"])
                self.assertEqual(report["outpoints"], expected["outpoints"])
                self.assertEqual(report["connection_count"], 3)
                self.assertEqual(report["transaction_count"], 4)
                self.assertEqual(report["starting_transaction_count"], 4)
                self.assertEqual(report["unconnected_starting_transactions"], [tx("f")])
                self.assertTrue(report["includes_all_starters"])
                self.assertTrue(graph["graph_options"]["includes_all_starters"])
                edge = next(edge for edge in graph["edges"] if edge["id"] == "out:" + point("f"))
                self.assertEqual(edge["role"], "context_output")
                self.assertNotIn(edge["id"], graph["branch_structure"]["edge_memberships"])
                validate_starter_visibility(graph)
        self.assertEqual(state, before)

    def test_no_connections_retains_all_starters_and_their_local_io(self):
        state = graph_state((("a:0", "c"), ("b:0", "d")), seeds=("a:0", "b:0"))
        for scope in ("shortest", "all_saved", "hop_limited"):
            with self.subTest(scope=scope):
                graph = complete(state, scope)
                self.assertEqual(set(transactions(graph)), {tx("a"), tx("b")})
                self.assertEqual(graph["connections"]["status"], "no_connection_found")
                self.assertEqual(graph["connections"]["pairs"], [])
                self.assertEqual(graph["connections"]["outpoints"], [])
                self.assertEqual(graph["connections"]["unconnected_starting_transactions"], [tx("a"), tx("b")])
                self.assertEqual(len(graph["edges"]), 2)
                self.assertNotIn("Nothing is plotted", graph["notice"])
                self.assertIn("All 2 selected starting transactions are shown", graph["notice"])

    def test_fee_only_starter_is_visible_even_when_it_has_no_rendered_edges(self):
        state = graph_state(seeds=("a:0", "b:0"))
        state["transactions"][tx("b")]["data"]["vout"] = [
            {"scriptpubkey": "", "scriptpubkey_type": "fee", "value": 7, "asset": LBTC}]
        for include in (False, True):
            graph = complete(state, include_fees=include)
            self.assertEqual(set(transactions(graph)), {tx("a"), tx("b")})
            self.assertEqual(any(edge["id"] == "out:" + point("b") for edge in graph["edges"]), include)
            self.assertEqual(transactions(graph)[tx("b")]["role"], "starting_transaction")
            validate_starter_visibility(graph)

    def test_newly_retained_named_starter_uses_zero_not_stale_collection_depth(self):
        state = named_state((("a:0", "b"), ("b:0", "c"), ("f:0", "e")),
                            seeds=("a:0", "c:0", "f:0"), group=("a",), maximum=0)
        state["transactions"][tx("f")]["depth"] = 15
        state["transactions"][tx("f")]["reference_hops"] = 12
        state["outputs"][point("f")].update(status="unspent_at_observation", observed_spend={"spent": False}, trace_scope_depth=12)
        graph = complete(state)
        starter = transactions(graph)[tx("f")]
        self.assertEqual(starter["details"]["reference_hops"], 0)
        self.assertEqual(starter["details"]["seed_depth"], 0)
        self.assertEqual(graph["connections"]["transaction_reference_hops"][tx("f")], 0)
        output = next(node for node in graph["nodes"] if node.get("details", {}).get("address") == "SYNTHETIC-f-address")
        self.assertNotIn("unspent_endpoints", output["details"])
        self.assertTrue(all(item["trace"] is None for item in output["details"]["occurrences"]))

    def test_shared_address_does_not_create_an_extra_pair_or_path(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0", "b:0", "f:0"))
        state["transactions"][tx("f")]["data"]["vout"][0] = deepcopy(state["transactions"][tx("a")]["data"]["vout"][0])
        graph = complete(state)
        self.assertEqual(graph["connections"]["connection_count"], 1)
        self.assertEqual(graph["connections"]["outpoints"], [point("a")])
        self.assertEqual(graph["connections"]["unconnected_starting_transactions"], [tx("f")])
        f_edge = next(edge for edge in graph["edges"] if edge["id"] == "out:" + point("f"))
        self.assertEqual(f_edge["role"], "context_output")
        self.assertNotIn(f_edge["id"], graph["branch_structure"]["edge_memberships"])

    def test_missing_selected_starter_fails_closed(self):
        state = graph_state(seeds=("a:0", "b:0"))
        del state["transactions"][tx("b")]
        for scope in ("shortest", "all_saved", "hop_limited"):
            with self.subTest(scope=scope), self.assertRaisesRegex(TraceError, "starting transaction is missing"):
                complete(state, scope)

    def test_legacy_path_only_remains_empty_when_no_connections(self):
        for scope in (None, "shortest", "all_saved", "hop_limited"):
            with self.subTest(scope=scope):
                graph = connection_graph(graph_state(), 10, connection_scope=scope)
                self.assertEqual(graph["nodes"], [])
                self.assertEqual(graph["edges"], [])
                self.assertNotIn("includes_all_starters", graph["connections"])
                self.assertNotIn("includes_all_starters", graph["graph_options"])
                self.assertIn("Nothing is plotted", graph["notice"])


class AllStarterReviewTests(unittest.TestCase):
    def test_marker_requires_consistent_roots_count_pairs_and_visible_nodes(self):
        graph = complete(graph_state((("a:0", "b"),), seeds=("a:0", "b:0", "f:0")))
        mutations = [
            lambda value: value["connections"].update(includes_all_starters=False),
            lambda value: value["graph_options"].pop("includes_all_starters"),
            lambda value: value["connections"].update(starting_transaction_count=True),
            lambda value: value["connections"].update(starting_transaction_count=2),
            lambda value: value["connections"].update(unconnected_starting_transactions=[]),
            lambda value: value["connections"].update(starting_transactions=[tx("a"), tx("b")]),
            lambda value: value.update(nodes=[node for node in value["nodes"] if node["id"] != "tx:" + tx("f")]),
        ]
        for mutation in mutations:
            value = deepcopy(graph)
            mutation(value)
            with self.assertRaisesRegex(TraceError, "starter visibility"):
                validate_starter_visibility(value)

    def test_complete_snapshot_without_new_marker_still_reviews(self):
        with tempfile.TemporaryDirectory() as temporary, patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph):
            case = create_investigation(Path(temporary), "Starter compatibility")
            saved_case(case, graph_state((("a:0", "b"),), seeds=("a:0", "b:0")))
            def historical(*args, **kwargs):
                graph = connection_graph(*args, **kwargs)
                for name in ("includes_all_starters", "starting_transaction_count", "unconnected_starting_transactions"):
                    graph["connections"].pop(name, None)
                graph["graph_options"].pop("includes_all_starters")
                return graph
            with patch("liquid_tracer.connections.connection_graph", side_effect=historical):
                old = preview_connections(case, connection_scope="shortest")
            previous, _ = reviewed_connections(case, old["preview_id"])
            self.assertNotIn("includes_all_starters", previous["connections"])
            current = preview_connections(case, connection_scope="shortest")
            self.assertTrue(current["includes_all_starters"])
            self.assertEqual(current["starting_transaction_count"], 2)
            self.assertEqual(current["unconnected_starting_transactions"], [])
            self.assertNotIn("includes_all_starters", old)
            self.assertTrue(reviewed_connections(case, current["preview_id"])[0]["connections"]["includes_all_starters"])
            self.assertEqual(reviewed_connections(case, old["preview_id"])[0], previous)


if __name__ == "__main__":
    unittest.main()
