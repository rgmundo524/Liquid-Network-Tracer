"""Indexed selection keeps path semantics while skipping unrelated payloads."""
from collections import defaultdict
from copy import deepcopy
import random
import unittest

from liquid_tracer.common import TraceError, parse_outpoint
from liquid_tracer.connections import _saved_connection_evidence, connecting_outpoints
from liquid_tracer.export import _unspent_endpoints
from liquid_tracer.saved_inputs import saved_input_output
from liquid_tracer.pegout_paths import pegout_graph, validate_query
from liquid_tracer.shared_projection import _project
from liquid_tracer.snapshot_query import select_snapshot
from tests.test_attribution_convergence import graph_state, tx
from tests.test_pegout_paths import add_pegout, mark_unspent


def point(name, number=0):
    return f"{tx(name)}:{number}"


class MemoryIndex:
    """Same lookup contract as SQLite, recording every full payload request."""
    def __init__(self, state, forbidden=()):
        self.state = _saved_connection_evidence(state)
        self.metadata = {key: deepcopy(value) for key, value in state.items()
                         if key not in {"transactions", "outputs", "links", "address_tx_counts", "observations"}}
        self.metadata["_snapshot_collected_depth"] = 15
        self.forward, self.backward = defaultdict(list), defaultdict(list)
        for key, raw in self.state["links"].items():
            link = {**raw, "outpoint": key}
            self.state["links"][key] = link
            self.forward[parse_outpoint(key)[0]].append(link)
            self.backward[link["spending_txid"]].append(link)
        self.forbidden = set(forbidden)
        self.fetched = []

    def transaction(self, txid):
        if txid in self.forbidden:
            raise AssertionError("Unrelated transaction payload loaded: " + txid)
        self.fetched.append(txid)
        return self.state["transactions"].get(txid)

    def output(self, key):
        return self.state["outputs"].get(key)

    def link(self, key):
        return self.state["links"].get(key)

    def outgoing(self, txid):
        return self.forward[txid]

    def incoming(self, txid):
        return self.backward[txid]

    def counts(self, addresses):
        return {key: value for key, value in self.state.get("address_tx_counts", {}).items() if key in addresses}


class SnapshotQueryTests(unittest.TestCase):
    def test_hop_bound_is_relative_to_seeds_and_skips_unrelated_tails(self):
        state = graph_state((("a:0", "b"), ("b:0", "c"), ("c:0", "d"),
                             ("a:1", "e"), ("e:0", "f")), seeds=("a:0",))
        for record in state["transactions"].values():
            record["depth"] = 12
        state["outputs"][point("a")]["depth"] = 12
        index = MemoryIndex(state, forbidden={tx("d"), tx("e"), tx("f")})
        before = deepcopy(index.state)
        selected = select_snapshot(index, [point("a")], max_hops=2)
        self.assertEqual(set(selected["transactions"]), {tx("a"), tx("b"), tx("c")})
        self.assertEqual(set(selected["links"]), {point("a"), point("b")})
        self.assertEqual(selected["transactions"][tx("a")]["depth"], 12)
        self.assertEqual(selected["_snapshot_collected_depth"], 15)
        projected = _project(selected, [point("a")], "recipient", "")
        self.assertEqual(projected["transactions"][tx("c")]["depth"], 2)
        self.assertEqual(index.state, before)
        selected["transactions"][tx("a")]["data"]["vout"].clear()
        self.assertEqual(index.state, before)

    def test_zero_hops_preserves_all_local_io_without_following_outputs(self):
        state = graph_state((("a:0", "b"), ("a:1", "c")), seeds=("a:0",))
        index = MemoryIndex(state, forbidden={tx("b"), tx("c")})
        selected = select_snapshot(index, [point("a")], max_hops=0)
        self.assertEqual(set(selected["transactions"]), {tx("a")})
        self.assertEqual(selected["transactions"][tx("a")]["data"], state["transactions"][tx("a")]["data"])
        self.assertEqual(selected["links"], {})

    def test_another_seed_reached_as_child_can_continue_its_other_outputs(self):
        state = graph_state((("a:0", "b"), ("b:1", "c"), ("b:0", "d"), ("c:0", "e")),
                            seeds=("a:0", "b:0"))
        index = MemoryIndex(state, forbidden={tx("e")})
        selected = select_snapshot(index, state["seeds"], max_hops=2)
        self.assertEqual(set(selected["transactions"]), {tx(name) for name in "abcd"})
        projected = _project(selected, state["seeds"], "recipient", "")
        self.assertEqual(projected["transactions"][tx("c")]["depth"], 2)
        self.assertEqual(projected["transactions"][tx("d")]["depth"], 1)

    def test_unbounded_traversal_retains_reachable_deep_descendants(self):
        state = graph_state((("a:0", "b"), ("b:0", "c"), ("c:0", "d")), seeds=("a:0",))
        state["hop_reference_name"] = "Exchange"
        selected = select_snapshot(MemoryIndex(state), state["seeds"])
        self.assertEqual(set(selected["transactions"]), {tx(name) for name in "abcd"})
        self.assertEqual(selected["hop_reference_name"], "Exchange")

    def test_stale_unspent_at_boundary_is_not_made_into_false_endpoint(self):
        state = graph_state((("a:0", "b"),), raw_links=(("b:0", "c"),), seeds=("a:0",))
        state["outputs"][point("b")].update(status="unspent_at_observation", observed_spend={"spent": False})
        state["outputs"][point("b")]["spend_observation_id"] = 100
        index = MemoryIndex(state, forbidden={tx("c")})
        selected = select_snapshot(index, state["seeds"], max_hops=1)
        self.assertEqual(selected["outputs"][point("b")]["status"], "spent_in_saved_evidence")
        self.assertNotIn(point("b"), _unspent_endpoints(selected))
        self.assertNotIn(point("b"), selected["links"])
        self.assertEqual(selected["outputs"][point("b")]["observed_spend"], {"spent": False})

    def test_unknown_and_observed_unspent_endpoints_remain_distinct(self):
        state = graph_state((("a:0", "b"), ("a:1", "c")), seeds=("a:0", "a:1"))
        state["outputs"][point("b")].update(status="not_observed")
        state["outputs"][point("c")].update(status="unspent_at_observation", observed_spend={"spent": False},
                                             spend_observation_id=101)
        selected = select_snapshot(MemoryIndex(state), state["seeds"], max_hops=5)
        self.assertEqual(selected["outputs"][point("b")]["status"], "not_observed")
        self.assertEqual(_unspent_endpoints(selected), {point("c")})

    def test_connections_keep_alternate_paths_and_skip_unrelated_payloads(self):
        state = graph_state((("a:0", "b"), ("b:0", "f"), ("b:1", "c"),
                             ("c:0", "d"), ("d:0", "f"), ("b:2", "e"),
                             ("e:0", "1"), ("a:1", "2")), seeds=("a:0", "f:0"))
        for scope, hops in (("all_saved", None), ("hop_limited", 4)):
            with self.subTest(scope=scope):
                index = MemoryIndex(state, forbidden={tx("e"), tx("1"), tx("2")})
                selected = select_snapshot(index, state["seeds"], max_hops=hops, connection_scope=scope)
                self.assertEqual(set(selected["transactions"]), {tx(name) for name in "abcdf"})
                expected = connecting_outpoints(state, hops, connection_scope=scope)
                actual = connecting_outpoints(selected, hops, connection_scope=scope)
                self.assertEqual(actual, expected)

    def test_shorter_connection_bound_excludes_long_alternate_route(self):
        state = graph_state((("a:0", "b"), ("b:0", "f"), ("b:1", "c"),
                             ("c:0", "d"), ("d:0", "f")), seeds=("a:0", "f:0"))
        selected = select_snapshot(MemoryIndex(state), state["seeds"], max_hops=2, connection_scope="hop_limited")
        self.assertEqual(set(selected["transactions"]), {tx(name) for name in "abf"})
        self.assertEqual(connecting_outpoints(selected, 2, connection_scope="hop_limited"),
                         connecting_outpoints(state, 2, connection_scope="hop_limited"))
        # d only supplies f's complete local vin context; its branch is not followed.
        self.assertEqual(set(selected["saved_transactions"]), {tx("d")})

    def test_unselected_starting_output_does_not_create_connection(self):
        state = graph_state((("a:0", "b"), ("a:1", "c"), ("c:0", "d")), seeds=("a:0", "d:0"))
        selected = select_snapshot(MemoryIndex(state), state["seeds"], connection_scope="all_saved")
        self.assertEqual(set(selected["transactions"]), {tx("a"), tx("d")})
        self.assertEqual(connecting_outpoints(selected, None, connection_scope="all_saved")["pairs"], [])
        self.assertEqual(connecting_outpoints(selected, None, connection_scope="all_saved")["outpoints"], [])

    def test_missing_prevout_uses_saved_funding_context_without_expansion(self):
        state = graph_state((("a:0", "b"), ("c:0", "b"), ("c:1", "d")), seeds=("a:0",))
        state["transactions"][tx("b")]["data"]["vin"][1].pop("prevout")
        state["address_tx_counts"] = {"SYNTHETIC-c-address": 17, "unrelated": 99}
        selected = select_snapshot(MemoryIndex(state, forbidden={tx("d")}), state["seeds"], max_hops=1)
        self.assertEqual(set(selected["transactions"]), {tx("a"), tx("b")})
        self.assertEqual(set(selected["saved_transactions"]), {tx("c")})
        vin = selected["transactions"][tx("b")]["data"]["vin"][1]
        self.assertNotIn("prevout", vin)
        resolved = saved_input_output({**selected["saved_transactions"], **selected["transactions"]}, vin)
        self.assertEqual(resolved["scriptpubkey_address"], "SYNTHETIC-c-address")
        self.assertEqual(selected["address_tx_counts"], {"SYNTHETIC-c-address": 17})

    def test_connection_selection_matches_existing_algorithm_for_many_dags(self):
        generator = random.Random(741)
        names = "abcdef123456789"
        for attempt in range(20):
            edges = []
            for number, source in enumerate(names):
                children = [child for child in names[number + 1:] if generator.random() < .18]
                edges.extend((source + ":" + str(index), child) for index, child in enumerate(children))
            state = graph_state(tuple(edges), seeds=("a:0", "d:0", "9:0"))
            for scope, hops in (("all_saved", None), ("hop_limited", 2), ("hop_limited", 4)):
                with self.subTest(attempt=attempt, scope=scope, hops=hops):
                    selected = select_snapshot(MemoryIndex(state), state["seeds"], max_hops=hops, connection_scope=scope)
                    self.assertEqual(connecting_outpoints(selected, hops, connection_scope=scope),
                                     connecting_outpoints(state, hops, connection_scope=scope))

    def test_scoped_pegout_paths_and_endpoints_match_complete_projection(self):
        state = graph_state((("a:0", "b"), ("b:0", "c"), ("c:0", "d"),
                             ("a:1", "e"), ("e:0", "f")), seeds=("a:0", "a:1"))
        add_pegout(state, tx("c"))
        add_pegout(state, tx("d"))
        mark_unspent(state, point("f"))
        for key, link in state["links"].items():
            link["outpoint"] = key
        for hops in (0, 1, 2, 3, 15):
            with self.subTest(hops=hops):
                selected = select_snapshot(MemoryIndex(state), state["seeds"], max_hops=hops)
                expected = _project(state, state["seeds"], "recipient", "")
                actual = _project(selected, state["seeds"], "recipient", "")
                query = validate_query(seeds=state["seeds"], max_hops=hops, include_unspent=True,
                                       transaction_io="complete")
                expected_graph, actual_graph = (pegout_graph(item, query) for item in (expected, actual))
                self.assertEqual(actual_graph["pegouts"], expected_graph["pegouts"])
                self.assertEqual(actual_graph["nodes"], expected_graph["nodes"])
                self.assertEqual(actual_graph["edges"], expected_graph["edges"])

    def test_query_validation_and_cancellation(self):
        state = graph_state(seeds=("a:0",))
        for seeds, options in (([], {}), ([point("b")], {}), ([point("a", 100)], {}),
                               (state["seeds"], {"max_hops": -1}),
                               (state["seeds"], {"connection_scope": "hop_limited"}),
                               (state["seeds"], {"connection_scope": "all_saved"})):
            with self.subTest(seeds=seeds, options=options), self.assertRaises(TraceError):
                select_snapshot(MemoryIndex(state), seeds, **options)
        def cancelled(event):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            select_snapshot(MemoryIndex(state), state["seeds"], progress=cancelled)


if __name__ == "__main__":
    unittest.main()
