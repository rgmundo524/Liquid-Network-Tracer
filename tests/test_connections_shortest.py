"""Shortest starter routes preserve exact UTXO evidence and old search scopes."""
from copy import deepcopy
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import LBTC, TraceError
from liquid_tracer.connections import connecting_outpoints, connection_graph, preview_connections, reviewed_connections
from liquid_tracer.investigations import create_investigation
from liquid_tracer.shortest_paths import shortest_routes
from tests.test_attribution_convergence import annotation, graph_state, tx
from tests.test_connections import saved_case
from tests.test_named_hop_plots import named_state


def shortest(state):
    return connecting_outpoints(state, None, connection_scope="shortest")


def point(name, index=0):
    return f"{tx(name)}:{index}"


class ShortestConnectionTests(unittest.TestCase):
    def test_shortest_omits_longer_routes_and_selects_stable_equal_route(self):
        state = graph_state(raw_links=(("a:2", "e"), ("e:0", "f"), ("f:0", "d"),
                                      ("a:1", "c"), ("c:0", "d"), ("a:0", "b"), ("b:0", "d")),
                            seeds=("a:0", "a:1", "a:2", "d:0"))
        before = deepcopy(state)
        report = shortest(state)
        self.assertEqual(report["outpoints"], [point("a"), point("b")])
        self.assertEqual(report["pairs"], [{"source": tx("a"), "target": tx("d"), "shortest_hops": 2}])
        self.assertIsNone(report["max_hops"])
        self.assertEqual(report["connection_scope"], "shortest")
        self.assertEqual(len(connecting_outpoints(state, None, connection_scope="all_saved")["outpoints"]), 7)
        self.assertEqual(state, before)

    def test_each_pair_has_its_own_shortest_not_only_global_closest_pair(self):
        state = graph_state(raw_links=(("a:0", "b"), ("b:0", "c"), ("c:0", "d")),
                            seeds=("a:0", "b:0", "d:0"))
        self.assertEqual(shortest(state)["pairs"], [
            {"source": tx("a"), "target": tx("b"), "shortest_hops": 1},
            {"source": tx("a"), "target": tx("d"), "shortest_hops": 3},
            {"source": tx("b"), "target": tx("d"), "shortest_hops": 2}])

    def test_first_output_restriction_does_not_restrict_intermediate_starters(self):
        state = graph_state(raw_links=(("a:0", "b"), ("a:1", "d"), ("b:1", "c"), ("c:0", "d")),
                            seeds=("a:0", "b:0", "d:0"))
        report = shortest(state)
        self.assertEqual(report["pairs"], [
            {"source": tx("a"), "target": tx("b"), "shortest_hops": 1},
            {"source": tx("a"), "target": tx("d"), "shortest_hops": 3}])
        self.assertEqual(set(report["outpoints"]), {point("a"), point("b", 1), point("c")})

    def test_named_hops_stops_and_confirmation_do_not_change_shortest_selection(self):
        state = named_state((("a:0", "b"), ("b:0", "c"), ("c:0", "d")),
                            seeds=("a:0", "d:0"), group=("a", "b", "c", "d"), maximum=0)
        state["labels"][0].update(stop=True, hop_limit=0)
        state["transactions"][tx("b")]["data"]["status"] = {"confirmed": False}
        graph = connection_graph(state, None, connection_scope="shortest", transaction_io="complete")
        self.assertEqual(graph["connections"]["pairs"][0]["shortest_hops"], 3)
        self.assertEqual(graph["connections"]["transaction_reference_hops"][tx("d")], 0)
        self.assertIn("do not change minimum-hop route selection", graph["notice"])
        self.assertIn("One shortest route", graph["notice"])

    def test_complete_context_and_fee_opt_in_do_not_expand_other_branches(self):
        state = graph_state((("a:0", "b"), ("a:1", "c"), ("c:0", "d")), seeds=("a:0", "b:0"))
        fee = point("a", 2)
        state["transactions"][tx("a")]["data"]["vout"].append(
            {"scriptpubkey": "", "scriptpubkey_type": "fee", "value": 7, "asset": LBTC})
        for enabled in (False, True):
            graph = connection_graph(state, None, connection_scope="shortest", transaction_io="complete", include_fees=enabled)
            self.assertEqual(graph["connections"]["outpoints"], [point("a")])
            self.assertEqual({node["id"] for node in graph["nodes"] if node["kind"] == "transaction"},
                             {"tx:" + tx("a"), "tx:" + tx("b")})
            edges = {edge["id"]: edge for edge in graph["edges"]}
            self.assertEqual(edges["out:" + point("a", 1)]["role"], "context_output")
            self.assertEqual("out:" + fee in edges, enabled)

    def test_unconnected_and_address_reuse_do_not_invent_paths(self):
        state = graph_state(seeds=("a:0", "b:0"), labels=[annotation()])
        state["transactions"][tx("b")]["data"]["vout"] = deepcopy(state["transactions"][tx("a")]["data"]["vout"])
        self.assertEqual(shortest(state)["status"], "no_connection_found")
        self.assertEqual(shortest(state)["outpoints"], [])

    def test_invalid_saved_evidence_fails_closed(self):
        for edges in ((("a:0", "b"), ("b:0", "a")), (("a:0", "b"), ("a:0", "c"))):
            with self.subTest(edges=edges), self.assertRaises(TraceError):
                shortest(graph_state(raw_links=edges))
        with self.assertRaises(TraceError):
            connecting_outpoints(graph_state(), True, connection_scope="shortest")

    def test_many_dags_match_exhaustive_minimum_hop_lexicographic_oracle(self):
        rng = random.Random(32147)
        names = "0123456"
        for attempt in range(70):
            edges, seeds = [], [f"{root}:0" for root in "036"]
            for index, source in enumerate(names):
                children = [child for child in names[index + 1:] if rng.random() < .4]
                for output, child in enumerate(children):
                    key = f"{source}:{output}"
                    edges.append((key, child))
                    if source in "036" and output % 2 == 0:
                        seeds.append(key)
            state = graph_state(raw_links=edges, seeds=sorted(set(seeds)))
            expected = {}
            for source in "036":
                pending = [(source, [])]
                while pending:
                    parent, path = pending.pop()
                    if parent in "036" and parent != source:
                        candidate = (len(path), tuple(path))
                        pair = (tx(source), tx(parent))
                        expected[pair] = min(expected.get(pair, candidate), candidate)
                    for key, child in edges:
                        if key.split(":")[0] == parent and (parent != source or key in seeds):
                            pending.append((child, [*path, point(parent, key.split(":")[1])]))
            actual = shortest(state)
            self.assertEqual(actual["pairs"], [{"source": source, "target": target, "shortest_hops": result[0]}
                                               for (source, target), result in sorted(expected.items())], attempt)
            self.assertEqual(set(actual["outpoints"]), {key for _, path in expected.values() for key in path}, attempt)

    def test_reverse_bfs_finishes_tie_level_then_stops(self):
        incoming = {"d": [("c", "c:0"), ("b", "b:0")], "c": [("a", "a:1")], "b": [("a", "a:0")]}
        # Use valid hashes for parse_outpoint while recording topology reads.
        edges = {tx(child): [(tx(parent), point(*key.split(":"))) for parent, key in rows]
                 for child, rows in incoming.items()}
        reads = []
        def load(child):
            reads.append(child)
            return edges.get(child, [])
        routes = shortest_routes([point("a"), point("a", 1), point("d")], load)
        self.assertEqual(routes[0]["outpoints"], [point("a"), point("b")])
        self.assertEqual(reads.count(tx("a")), 1)  # its own target search, not after finding a -> d


class ShortestSnapshotTests(unittest.TestCase):
    def test_new_and_existing_scopes_remain_reviewable_with_frozen_membership(self):
        with tempfile.TemporaryDirectory() as temporary, patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph):
            case = create_investigation(Path(temporary), "Shortest routes")
            saved_case(case, graph_state((("a:0", "b"), ("a:1", "c"), ("c:0", "b")),
                                        seeds=("a:0", "a:1", "b:0")))
            previews = [preview_connections(case, max_hops=2, connection_scope=scope)
                        for scope in (None, "all_saved", "hop_limited", "shortest")]
            graphs = [reviewed_connections(case, item["preview_id"])[0] for item in previews]
            self.assertEqual([len(graph["connections"]["outpoints"]) for graph in graphs], [3, 3, 3, 1])
            for item, graph in zip(previews, graphs):
                self.assertEqual(reviewed_connections(case, item["preview_id"])[0], graph)


if __name__ == "__main__":
    unittest.main()
