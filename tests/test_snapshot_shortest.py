"""Indexed shortest selection reads route bodies and immediate context only."""
from copy import deepcopy
import random
import unittest

from liquid_tracer.connections import connecting_outpoints
from liquid_tracer.shared_projection import _project
from liquid_tracer.snapshot_query import select_snapshot
from liquid_tracer.shortest_paths import shortest_routes
from tests.test_attribution_convergence import graph_state, tx
from tests.test_snapshot_query import MemoryIndex, point


class SnapshotShortestTests(unittest.TestCase):
    def test_shortest_uses_index_without_loading_long_routes_or_unrelated_tails(self):
        state = graph_state((("a:0", "b"), ("b:0", "f"), ("b:1", "c"), ("c:0", "d"),
                             ("d:0", "f"), ("b:2", "e"), ("e:0", "1"), ("a:1", "2")),
                            seeds=("a:0", "f:0"))
        index = MemoryIndex(state, forbidden={tx("c"), tx("e"), tx("1"), tx("2")})
        before = deepcopy(index.state)
        selected = select_snapshot(index, state["seeds"], connection_scope="shortest")
        self.assertEqual(set(selected["transactions"]), {tx(name) for name in "abf"})
        self.assertEqual(set(selected["saved_transactions"]), {tx("d")})  # immediate input context
        expected = connecting_outpoints(state, None, connection_scope="shortest")
        self.assertEqual(connecting_outpoints(selected, None, connection_scope="shortest"), expected)
        projected = _project(selected, state["seeds"], "recipient", "")
        self.assertEqual(connecting_outpoints(projected, None, connection_scope="shortest"), expected)
        self.assertEqual(index.state, before)

    def test_indexed_and_complete_search_choose_identical_ties_after_projection(self):
        rng = random.Random(4379)
        for attempt in range(40):
            names, edges, seeds = "abcdef123", [], ["a:0", "d:0", "3:0"]
            for number, source in enumerate(names):
                children = [child for child in names[number + 1:] if rng.random() < .3]
                for output, child in enumerate(children):
                    key = f"{source}:{output}"
                    edges.append((key, child))
                    if source in "ad3" and output % 2 == 0:
                        seeds.append(key)
            state = graph_state(raw_links=edges, seeds=sorted(set(seeds)))
            selected = select_snapshot(MemoryIndex(state), state["seeds"], connection_scope="shortest")
            expected = connecting_outpoints(state, None, connection_scope="shortest")
            self.assertEqual(connecting_outpoints(selected, None, connection_scope="shortest"), expected, attempt)
            projected = _project(selected, state["seeds"], "recipient", "")
            self.assertEqual(connecting_outpoints(projected, None, connection_scope="shortest"), expected, attempt)

    def test_reverse_search_checks_cancellation_during_topology_reads(self):
        source, target = f"{0:064x}", f"{300:064x}"
        reads = []
        events = []
        def incoming(child):
            reads.append(child)
            value = int(child, 16)
            return [(f"{value - 1:064x}", f"{value - 1:064x}:0")] if value else []
        def cancel(event):
            events.append(event)
            if len(reads) >= 256:
                raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            shortest_routes([source + ":0", target + ":0"], incoming, progress=cancel)
        self.assertGreaterEqual(len(reads), 256)
        self.assertLess(len(reads), 300)
        self.assertGreater(len(events), 2)


if __name__ == "__main__":
    unittest.main()
