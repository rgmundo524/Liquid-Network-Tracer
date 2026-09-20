"""Layout branch membership follows saved UTXOs without address inference."""

import copy
import unittest
from unittest.mock import patch

from liquid_tracer import convergence
from liquid_tracer.common import TraceError
from liquid_tracer.export import build_graph
from tests.test_attribution_convergence import annotation, graph_state, tx
from tests.test_branch_interactions import ADDRESS_KEY, SHARED, receiving


def root(name):
    return "tx:" + tx(name)


def structure(state):
    return build_graph(state)["branch_structure"]


class BranchStructureTests(unittest.TestCase):
    def test_join_descendants_retain_membership_without_new_merge_markers(self):
        graph = build_graph(graph_state((("a:0", "c"), ("b:0", "c"), ("c:0", "d"))))
        data = graph["branch_structure"]
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["roots"], [{"key": root("a"), "index": 1},
                                        {"key": root("b"), "index": 2}])
        self.assertEqual(data["node_memberships"][root("a")], [root("a")])
        self.assertEqual(data["node_memberships"][root("b")], [root("b")])
        for name in ("c", "d"):
            self.assertEqual(data["node_memberships"][root(name)], [root("a"), root("b")])
            self.assertEqual(data["edge_memberships"]["out:" + tx(name) + ":0"],
                             [root("a"), root("b")])
        self.assertEqual([node["id"] for node in graph["nodes"] if node.get("convergence")], [root("c")])

    def test_multiple_seed_outputs_share_one_starting_transaction_root(self):
        data = structure(graph_state((("a:0", "c"), ("a:1", "c")),
                                     seeds=("a:0", "a:1", "b:0")))
        self.assertEqual(len(data["roots"]), 2)
        self.assertEqual(data["node_memberships"][root("c")], [root("a")])
        for index in (0, 1):
            self.assertEqual(data["edge_memberships"][f"out:{tx('a')}:{index}"], [root("a")])
            self.assertEqual(data["edge_memberships"][f"in:{tx('c')}:{index}"], [root("a")])

    def test_context_links_and_unselected_seed_siblings_do_not_propagate(self):
        data = structure(graph_state((("a:1", "c"),), raw_links=(("b:0", "d"),)))
        for name in ("c", "d"):
            self.assertNotIn(root(name), data["node_memberships"])
            self.assertNotIn(f"out:{tx(name)}:0", data["edge_memberships"])
            self.assertNotIn(f"in:{tx(name)}:0", data["edge_memberships"])
        self.assertNotIn(f"out:{tx('a')}:1", data["edge_memberships"])

    def test_shared_address_union_never_changes_independent_spending_utxos(self):
        state = graph_state((("a:0", "c"), ("b:0", "d")))
        receiving(state, "a:0")
        receiving(state, "b:0")
        data = structure(state)
        self.assertEqual(data["node_memberships"][ADDRESS_KEY], [root("a"), root("b")])
        for parent, child in (("a", "c"), ("b", "d")):
            self.assertEqual(data["node_memberships"][root(child)], [root(parent)])
            self.assertEqual(data["edge_memberships"][f"in:{tx(child)}:0"], [root(parent)])

    def test_context_at_shared_address_does_not_gain_union_membership(self):
        state = graph_state((("a:0", "c"),), raw_links=(("b:0", "d"),))
        receiving(state, "a:0")
        receiving(state, "b:0")
        data = structure(state)
        self.assertEqual(data["node_memberships"][ADDRESS_KEY], [root("a"), root("b")])
        self.assertEqual(data["node_memberships"][root("c")], [root("a")])
        self.assertNotIn(root("d"), data["node_memberships"])
        self.assertNotIn(f"in:{tx('d')}:0", data["edge_memberships"])

    def test_stopped_arrivals_have_membership_but_later_spends_do_not(self):
        state = graph_state((("a:0", "c"), ("b:0", "c"), ("c:0", "d")),
                            labels=(annotation(address=SHARED, stop=True),))
        receiving(state, "a:0")
        receiving(state, "b:0")
        data = structure(state)
        self.assertEqual(data["node_memberships"][ADDRESS_KEY], [root("a"), root("b")])
        for name in ("c", "d"):
            self.assertNotIn(root(name), data["node_memberships"])
            self.assertNotIn(f"in:{tx(name)}:0", data["edge_memberships"])

    def test_hop_budget_exhaustion_is_specific_to_each_root(self):
        label = {**annotation(stop=False), "hop_limit": 1}
        state = graph_state((("a:0", "c"), ("c:0", "d"), ("b:0", "d")), labels=(label,))
        data = structure(state)
        self.assertEqual(data["node_memberships"][root("c")], [root("a")])
        self.assertEqual(data["node_memberships"][root("d")], [root("b")])
        self.assertEqual(data["edge_memberships"][f"out:{tx('c')}:0"], [root("a")])
        self.assertNotIn(f"in:{tx('d')}:0", data["edge_memberships"])
        self.assertEqual(data["edge_memberships"][f"in:{tx('d')}:1"], [root("b")])

    def test_later_start_only_adds_own_origin_to_its_selected_outputs(self):
        data = structure(graph_state((("a:0", "b"), ("b:1", "c"))))
        self.assertEqual(data["node_memberships"][root("b")], [root("a"), root("b")])
        self.assertEqual(data["edge_memberships"][f"out:{tx('b')}:0"], [root("a"), root("b")])
        self.assertEqual(data["edge_memberships"][f"out:{tx('b')}:1"], [root("a")])
        self.assertEqual(data["node_memberships"][root("c")], [root("a")])

    def test_display_cycles_preserve_exact_memberships(self):
        state = graph_state((("a:0", "c"), ("c:0", "d"), ("b:0", "e")))
        for name in ("a", "b", "c", "d", "e"):
            receiving(state, name + ":0")
        data = structure(state)
        for name in ("c", "d"):
            self.assertEqual(data["node_memberships"][root(name)], [root("a")])
        self.assertEqual(data["node_memberships"][root("e")], [root("b")])
        with self.assertRaisesRegex(TraceError, "cycle"):
            structure(graph_state((("a:0", "b"), ("b:0", "a"))))

    def test_one_analysis_and_only_graph_ids_without_state_mutation(self):
        state = graph_state((("a:0", "c"), ("b:0", "c")))
        before = copy.deepcopy(state)
        with patch.object(convergence, "_lineage_analysis", wraps=convergence._lineage_analysis) as analyze:
            graph = build_graph(state, group_context_inputs=True)
        self.assertEqual(analyze.call_count, 1)
        self.assertEqual(state, before)
        data = graph["branch_structure"]
        self.assertTrue(set(data["node_memberships"]) <= {node["id"] for node in graph["nodes"]})
        self.assertTrue(set(data["edge_memberships"]) <= {edge["id"] for edge in graph["edges"]})
        # The public interaction record retains its established contract.
        interactions = convergence.branch_interactions(state, graph["activity_frames"]["starting_transactions"])
        self.assertEqual(set(interactions), {"transactions", "addresses", "senders"})

    def test_single_start_has_no_multi_branch_membership(self):
        data = structure(graph_state((("a:0", "c"),), seeds=("a:0", "a:1")))
        self.assertEqual(data, {"version": 1, "roots": [{"key": root("a"), "index": 1}],
                                "node_memberships": {}, "edge_memberships": {}})


if __name__ == "__main__":
    unittest.main()
