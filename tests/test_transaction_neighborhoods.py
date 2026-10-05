import copy
import hashlib
import unittest

from liquid_tracer.transaction_neighborhoods import neighborhood_order, neighborhood_metrics
from liquid_tracer.elk_layout import optimize_graph, layout_metrics, attachment_point
from liquid_tracer.compaction import compact_graph
from tests.test_elk_layout import HAS_ELK


def split_graph(deep=False):
    """One original tree, later forks, inverted transactions, mixed outputs.

    Labels/dates deliberately disagree with branch order, like the reported
    screenshot. One transaction has additional external input context.
    """
    nodes, edges = [], []
    def node(key, kind, column, y):
        nodes.append({"id": key, "kind": kind, "column": column, "x": column * 650,
                      "y": y, "width": 160, "height": 160, "label": key,
                      "details": {}, "color": "#aaccee"})
    def edge(a, b, key, outpoint):
        edges.append({"id": key, "source": a, "target": b, "outpoint": outpoint,
                      "label": key, "quantity": "?? ??"})
    node("root", "transaction", 0, 400)
    for key, y in (("input-a", 100), ("context", 400), ("input-b", 700)):
        node(key, "address", 1, y)
    node("older-tx", "transaction", 2, 100)
    node("newer-tx", "transaction", 2, 700)
    for key, y in (("b1", 100), ("a1", 400), ("a2", 700), ("b2", 1000)):
        node(key, "address", 3, y)
    edge("root", "input-a", "root-out-a", "root:0")
    edge("root", "input-b", "root-out-b", "root:1")
    edge("input-a", "newer-tx", "vin-a", "root:0")
    edge("context", "newer-tx", "vin-context", "external:0")
    edge("input-b", "older-tx", "vin-b", "root:1")
    for tx, names in (("older-tx", ("b1", "b2")), ("newer-tx", ("a1", "a2"))):
        for index, key in enumerate(names):
            edge(tx, key, "out-" + key, tx + ":" + str(index))
    if deep:
        for i, key in enumerate(("a2", "b1", "b2", "a1")):
            node(key + "-child", "transaction", 4, 300 * i)
            node(key + "-end", "address", 5, 300 * (3 - i))
            source = next(e for e in edges if e["id"] == "out-" + key)
            edge(key, key + "-child", key + "-vin", source["outpoint"])
            edge(key + "-child", key + "-end", key + "-out", key + "-child:0")
    return {"nodes": nodes, "edges": edges, "fee_items": {}, "graph_options": {},
            "presentation_version": 6}


def ordered_copy(graph):
    order = {key: index for index, key in enumerate(neighborhood_order(graph))}
    result = copy.deepcopy(graph)
    for node in result["nodes"]:
        node["y"] = order[node["id"]] * 300
    return result


def named_split_graph():
    graph = split_graph(True)
    graph["nodes"].append({"id": "anchor", "kind": "address", "column": -1,
                           "x": -650, "y": 400, "width": 160, "height": 160,
                           "label": "Treasury", "details": {"address_attributions": [{"entity": "Treasury"}]}})
    graph["edges"].append({"id": "anchor-root", "source": "anchor", "target": "root",
                           "outpoint": "anchor:0", "label": "vin 0", "quantity": "?? ??"})
    graph["graph_options"]["center_name"] = "Treasury"
    # Address/transaction IDs carry no useful vertical order in a real trace.
    identities = {node["id"]: hashlib.sha256(node["id"].encode()).hexdigest() for node in graph["nodes"]}
    for node in graph["nodes"]:
        node["id"] = identities[node["id"]]
    for edge in graph["edges"]:
        edge["source"], edge["target"] = identities[edge["source"]], identities[edge["target"]]
    return graph


class NeighborhoodOrderingTests(unittest.TestCase):
    def test_later_forks_stay_together_without_multiple_seed_lineages(self):
        for deep in (False, True):
            with self.subTest(deep=deep):
                graph = split_graph(deep)
                before = copy.deepcopy(graph)
                old = neighborhood_metrics(graph)
                new = neighborhood_metrics(ordered_copy(graph))
                self.assertGreater(old["sibling_interleavings"], 0)
                self.assertGreater(old["flow_order_inversions"], 0)
                self.assertEqual(new["sibling_interleavings"], 0)
                self.assertEqual(new["flow_order_inversions"], 0)
                self.assertEqual(graph, before)

    def test_deterministic_when_serialized_nodes_and_edges_are_reversed(self):
        graph = split_graph(True)
        expected = neighborhood_order(graph), neighborhood_metrics(graph)
        graph["nodes"].reverse()
        graph["edges"].reverse()
        self.assertEqual((neighborhood_order(graph), neighborhood_metrics(graph)), expected)

    def test_shared_addresses_return_edges_and_hubs_keep_their_identity(self):
        graph = split_graph()
        graph["edges"].append({"id": "join", "source": "older-tx", "target": "a1", "outpoint": "older:2"})
        graph["edges"].append({"id": "return", "source": "newer-tx", "target": "input-b", "outpoint": "newer:3"})
        graph["nodes"][1]["layout_hub"] = True
        graph["fee_items"] = {"b2": {"endpoint": "shapes"}}
        original = copy.deepcopy(graph)
        order = neighborhood_order(graph)
        self.assertEqual(set(order), {node["id"] for node in graph["nodes"]})
        self.assertEqual(len(order), len(set(order)))
        neighborhood_metrics(graph)
        self.assertEqual(graph, original)

    def test_proximity_counts_both_inputs_and_outputs(self):
        graph = split_graph()
        before = neighborhood_metrics(graph)
        moved = copy.deepcopy(graph)
        next(node for node in moved["nodes"] if node["id"] == "older-tx")["y"] = 625
        after = neighborhood_metrics(moved)
        self.assertLess(after["transaction_distance"], before["transaction_distance"])
        self.assertLess(after["transaction_center_drift"], before["transaction_center_drift"])

    def test_long_chains_and_wide_forks_have_no_recursive_or_quadratic_expansion(self):
        graph = {"nodes": [], "edges": []}
        for i in range(1600):
            key = str(i)
            graph["nodes"].append({"id": key, "kind": "transaction" if i % 2 == 0 else "address",
                                   "column": i, "x": i * 300, "y": 0, "width": 160, "height": 160})
            if i:
                graph["edges"].append({"id": key, "source": str(i - 1), "target": key})
        self.assertEqual(len(neighborhood_order(graph)), 1600)
        self.assertEqual(neighborhood_metrics(graph)["flow_order_inversions"], 0)
        for node in graph["nodes"][1:]:
            node["column"] = 1
            node["kind"] = "address"
        for edge in graph["edges"]:
            edge["source"] = "0"
        self.assertEqual(len(neighborhood_order(graph)), 1600)
        metrics = neighborhood_metrics(graph)
        self.assertEqual(metrics["sibling_interleavings"], 0)
        self.assertFalse(metrics["truncated"])


@unittest.skipUnless(HAS_ELK, "local ELK package is not installed")
class NeighborhoodEngineTests(unittest.TestCase):
    def test_named_center_preserves_local_forks_and_compaction_keeps_the_order(self):
        # The previous forced named order produces seven endpoint inversions
        # and two foreign nodes between siblings on this same fixture.
        graph = named_split_graph()
        result = optimize_graph(graph, "elbowed", layout_attempts=1)
        for candidate in (result, compact_graph(result)):
            metrics = neighborhood_metrics(candidate)
            self.assertEqual(metrics["flow_order_inversions"], 0)
            self.assertEqual(metrics["sibling_interleavings"], 0)
            self.assertEqual(layout_metrics(candidate, midpoint_elbows=True)["crossings"], 0)
            self.assertEqual(layout_metrics(candidate)["node_overlaps"], 0)
            self.assertEqual(candidate["layout"]["named_group"]["matched_addresses"], 1)
        self.assertEqual(graph, named_split_graph())

    def test_shared_address_and_return_connection_survive_ordering(self):
        graph = split_graph()
        graph["edges"].extend([
            {"id": "shared", "source": "newer-tx", "target": "b1", "outpoint": "newer-tx:2"},
            {"id": "return", "source": "older-tx", "target": "input-a", "outpoint": "older-tx:2"}])
        result = optimize_graph(graph, "elbowed", layout_attempts=3)
        self.assertEqual({n["id"] for n in graph["nodes"]}, {n["id"] for n in result["nodes"]})
        self.assertEqual(len(result["nodes"]), len(graph["nodes"]))
        self.assertEqual({(e["id"], e["source"], e["target"], e["outpoint"]) for e in result["edges"]},
                         {(e["id"], e["source"], e["target"], e["outpoint"]) for e in graph["edges"]})
        self.assertEqual(layout_metrics(result)["node_overlaps"], 0)
        self.assertEqual(next(e for e in result["edges"] if e["id"] == "return")["routing_exception"], "return")

    def test_single_attempt_groups_forks_and_places_transactions_near_their_io(self):
        graph = split_graph(True)
        original = copy.deepcopy(graph)
        result = optimize_graph(graph, "elbowed", layout_attempts=1)
        report = result["layout"]["branch_organization"]["neighborhoods"]
        self.assertEqual(report["sibling_interleavings"], 0)
        self.assertEqual(report["flow_order_inversions"], 0)
        self.assertEqual(layout_metrics(result)["node_overlaps"], 0)
        self.assertEqual(layout_metrics(result, midpoint_elbows=True)["crossings"], 0)
        self.assertTrue(result["layout"]["branch_organization"]["boundary_ordering"])
        self.assertLess(report["transaction_distance"], neighborhood_metrics(graph)["transaction_distance"])
        self.assertEqual(graph, original)
        self.assertEqual({(e["id"], e["source"], e["target"], e["outpoint"]) for e in result["edges"]},
                         {(e["id"], e["source"], e["target"], e["outpoint"]) for e in graph["edges"]})
        nodes = {node["id"]: node for node in result["nodes"]}
        for node in graph["nodes"]:
            for field in ("label", "details", "width", "height", "column", "kind"):
                self.assertEqual(nodes[node["id"]][field], node[field])
        for edge in result["edges"]:
            self.assertEqual(edge["route"][0], attachment_point(nodes[edge["source"]], edge["attachment"]["startItem"]))
            self.assertEqual(edge["route"][-1], attachment_point(nodes[edge["target"]], edge["attachment"]["endItem"]))


if __name__ == "__main__":
    unittest.main()
