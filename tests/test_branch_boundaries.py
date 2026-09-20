"""Branch preferences preserve evidence and measure the intended geometry."""

import copy
import unittest

from liquid_tracer.branch_boundaries import branch_order, boundary_metrics


def graph_fixture():
    nodes = []
    def node(key, column, y, kind="transaction"):
        nodes.append({"id": key, "kind": kind, "column": column, "x": 300 * column,
                      "y": y, "width": 20, "height": 20})
    node("A", 0, 100)
    node("B", 0, 800)
    for key, y in (("a1", 100), ("a2", 300), ("ac", 200),
                   ("b1", 700), ("b2", 900), ("bc", 800)):
        node(key, 2, y)
    node("shared", 3, 500, "address")
    members = {key: ["A"] for key in ("A", "a1", "a2", "ac")}
    members.update({key: ["B"] for key in ("B", "b1", "b2", "bc")})
    members["shared"] = ["A", "B"]
    return {"nodes": nodes, "edges": [
        {"id": "a-out", "source": "ac", "target": "shared", "outpoint": "ac:0"},
        {"id": "b-out", "source": "bc", "target": "shared", "outpoint": "bc:0"}],
        "fee_items": {}, "branch_structure": {"version": 1,
            "roots": [{"key": "A", "index": 1}, {"key": "B", "index": 2}],
            "node_memberships": members, "edge_memberships": {"a-out": ["A"], "b-out": ["B"]}}}


def by_id(graph):
    return {node["id"]: node for node in graph["nodes"]}


class BoundaryGeometryTests(unittest.TestCase):
    def test_interacting_transactions_face_each_other_across_the_shared_node(self):
        graph = graph_fixture()
        before = copy.deepcopy(graph)
        order = branch_order(graph)
        self.assertLess(order.index("a1"), order.index("ac"))
        self.assertLess(order.index("a2"), order.index("ac"))
        self.assertLess(order.index("ac"), order.index("shared"))
        self.assertLess(order.index("shared"), order.index("bc"))
        self.assertLess(order.index("bc"), order.index("b1"))
        self.assertLess(order.index("bc"), order.index("b2"))
        self.assertEqual(set(order), set(by_id(graph)))
        boundary_metrics(graph)
        self.assertEqual(graph, before)

    def test_moving_contacts_from_branch_interiors_to_facing_edges_improves_score(self):
        graph = graph_fixture()
        before = boundary_metrics(graph)
        nodes = by_id(graph)
        nodes["ac"]["y"], nodes["bc"]["y"] = 350, 650
        after = boundary_metrics(graph)
        self.assertEqual(before["interleavings"], 0)
        self.assertGreater(before["boundary_depth"], 0)
        self.assertEqual(after["boundary_depth"], 0)
        self.assertLess(after["interbranch_travel"], before["interbranch_travel"])
        self.assertFalse(after["truncated"])

    def test_foreign_core_nodes_inside_a_branch_are_counted(self):
        graph = graph_fixture()
        self.assertEqual(boundary_metrics(graph)["interleavings"], 0)
        by_id(graph)["b1"]["y"] = 250
        self.assertEqual(boundary_metrics(graph)["interleavings"], 2)

    def test_boundary_depth_uses_nearest_populated_column(self):
        graph = graph_fixture()
        nodes = by_id(graph)
        nodes["ac"]["y"], nodes["bc"]["y"] = 350, 650
        nodes["shared"]["column"] = 8
        clear = boundary_metrics(graph)["boundary_depth"]
        nodes["shared"]["y"] = 200
        buried = boundary_metrics(graph)["boundary_depth"]
        self.assertEqual(clear, 0)
        self.assertGreater(buried, clear)

    def test_context_affinity_is_presentation_only_and_requires_one_transaction(self):
        graph = graph_fixture()
        graph["nodes"].append({"id": "context", "kind": "address", "column": 1,
                               "x": 300, "y": 0, "width": 20, "height": 20})
        graph["edges"].append({"id": "context-edge", "source": "context", "target": "ac"})
        metadata = copy.deepcopy(graph["branch_structure"])
        order = branch_order(graph)
        self.assertLess(order.index("a2"), order.index("context"))
        self.assertLess(order.index("context"), order.index("shared"))
        graph["edges"].append({"id": "context-edge-2", "source": "context", "target": "bc"})
        self.assertEqual(branch_order(graph)[-1], "context")
        self.assertEqual(graph["branch_structure"], metadata)

    def test_fee_and_explicit_hub_nodes_do_not_receive_branch_preferences(self):
        graph = graph_fixture()
        baseline = boundary_metrics(graph)
        for key in ("fee", "hub"):
            graph["nodes"].append({"id": key, "kind": "address", "column": 2,
                                   "x": 600, "y": 500, "width": 20, "height": 20})
            graph["branch_structure"]["node_memberships"][key] = ["A"]
        graph["fee_items"]["fee"] = {"endpoint": "shapes"}
        by_id(graph)["hub"]["layout_hub"] = True
        self.assertEqual(boundary_metrics(graph), baseline)
        self.assertEqual(set(branch_order(graph)[-2:]), {"fee", "hub"})

    def test_same_mixed_lineage_trunk_is_not_counted_as_new_interbranch_travel(self):
        graph = graph_fixture()
        graph["nodes"].append({"id": "merged", "kind": "transaction", "column": 4,
                               "x": 1200, "y": 650, "width": 20, "height": 20})
        graph["branch_structure"]["node_memberships"]["merged"] = ["A", "B"]
        before = boundary_metrics(graph)["interbranch_travel"]
        graph["edges"].append({"id": "trunk", "source": "shared", "target": "merged"})
        graph["branch_structure"]["edge_memberships"]["trunk"] = ["A", "B"]
        self.assertEqual(boundary_metrics(graph)["interbranch_travel"], before)

    def test_boundary_preference_reaches_only_matching_upstream_utxo(self):
        graph = graph_fixture()
        nodes = by_id(graph)
        for key, column, y, kind in (("parent", 0, 0, "transaction"),
                                     ("sibling", 0, 1000, "transaction"),
                                     ("input", 1, 0, "address")):
            graph["nodes"].append({"id": key, "kind": kind, "column": column,
                                   "x": column * 300, "y": y, "width": 20, "height": 20})
            graph["branch_structure"]["node_memberships"][key] = ["A"]
        graph["edges"].extend([
            {"id": "parent-output", "source": "parent", "target": "input", "outpoint": "parent:0"},
            {"id": "child-input", "source": "input", "target": "ac", "outpoint": "parent:0"}])
        graph["branch_structure"]["edge_memberships"].update({"parent-output": ["A"], "child-input": ["A"]})
        order = branch_order(graph)
        self.assertGreater(order.index("parent"), order.index("sibling"))
        self.assertGreater(order.index("input"), order.index("a2"))
        graph["edges"][-1]["outpoint"] = "unrelated:0"
        order = branch_order(graph)
        self.assertLess(order.index("parent"), order.index("sibling"))
        self.assertEqual(nodes["ac"]["y"], 200)

    def test_joint_spend_pull_reaches_its_exact_producer_transaction(self):
        graph = graph_fixture()
        nodes = by_id(graph)
        nodes["shared"]["kind"] = "transaction"
        graph["edges"] = []
        graph["branch_structure"]["edge_memberships"] = {}
        for root, producer in (("A", "ac"), ("B", "bc")):
            key = producer + "-input"
            graph["nodes"].append({"id": key, "kind": "address", "column": 2.5,
                                   "x": 750, "y": 500, "width": 20, "height": 20})
            graph["branch_structure"]["node_memberships"][key] = [root]
            for edge_id, source, target in ((key + "-out", producer, key), (key + "-in", key, "shared")):
                graph["edges"].append({"id": edge_id, "source": source, "target": target,
                                       "outpoint": producer + ":0"})
                graph["branch_structure"]["edge_memberships"][edge_id] = [root]
        order = branch_order(graph)
        self.assertGreater(order.index("ac"), order.index("a2"))
        self.assertLess(order.index("bc"), order.index("b1"))

    def test_pulled_parent_keeps_ordinary_sibling_subtree_with_its_boundary_child(self):
        graph = graph_fixture()
        for key, column, y, kind in (("parent", 0, 0, "transaction"),
                                     ("contact-input", 1, 0, "address"),
                                     ("private-input", 1, 0, "address"),
                                     ("private-child", 2, 0, "transaction"),
                                     ("terminal", 3, 0, "address")):
            graph["nodes"].append({"id": key, "kind": kind, "column": column,
                                   "x": column * 300, "y": y, "width": 20, "height": 20})
            graph["branch_structure"]["node_memberships"][key] = ["A"]
        for edge_id, source, target, outpoint in (
                ("contact-out", "parent", "contact-input", "parent:0"),
                ("contact-in", "contact-input", "ac", "parent:0"),
                ("private-out", "parent", "private-input", "parent:1"),
                ("private-in", "private-input", "private-child", "parent:1"),
                ("terminal-out", "private-child", "terminal", "private-child:0")):
            graph["edges"].append({"id": edge_id, "source": source, "target": target, "outpoint": outpoint})
            graph["branch_structure"]["edge_memberships"][edge_id] = ["A"]
        before = copy.deepcopy(graph)
        order = branch_order(graph)
        # Unrelated core objects stay inside; the private sibling follows its
        # parent toward the edge, with the contacting child still facingmost.
        self.assertLess(order.index("a2"), order.index("private-child"))
        self.assertLess(order.index("private-child"), order.index("ac"))
        self.assertLess(order.index("a2"), order.index("private-input"))
        self.assertLess(order.index("a2"), order.index("terminal"))
        self.assertEqual(graph, before)

    def test_display_return_connections_do_not_propagate_an_ancestor_pull(self):
        graph = graph_fixture()
        graph["nodes"].extend([
            {"id": "later", "kind": "transaction", "column": 4, "x": 1200,
             "y": 0, "width": 20, "height": 20},
            {"id": "return-address", "kind": "address", "column": 1, "x": 300,
             "y": 0, "width": 20, "height": 20}])
        graph["branch_structure"]["node_memberships"].update({"later": ["A"], "return-address": ["A"]})
        graph["edges"].extend([
            {"id": "later-out", "source": "later", "target": "return-address", "outpoint": "later:0"},
            {"id": "earlier-in", "source": "return-address", "target": "ac", "outpoint": "later:0"}])
        graph["branch_structure"]["edge_memberships"].update({"later-out": ["A"], "earlier-in": ["A"]})
        order = branch_order(graph)
        self.assertLess(order.index("later"), order.index("a1"))


class BoundaryMetadataTests(unittest.TestCase):
    def test_missing_single_root_and_malformed_present_metadata_stay_neutral(self):
        for mode in ("missing", "one-core", "invalid-member", "duplicate-root", "bool-version"):
            with self.subTest(mode=mode):
                graph = graph_fixture()
                if mode == "missing":
                    graph.pop("branch_structure")
                elif mode == "one-core":
                    graph["branch_structure"]["node_memberships"] = {"A": ["A"], "shared": ["A", "B"]}
                elif mode == "invalid-member":
                    graph["branch_structure"]["node_memberships"]["A"] = ["unknown"]
                elif mode == "duplicate-root":
                    graph["branch_structure"]["roots"].append({"key": "A", "index": 3})
                else:
                    graph["branch_structure"]["version"] = True
                self.assertIsNone(branch_order(graph))
                score = boundary_metrics(graph)
                self.assertFalse(score["enabled"])
                self.assertEqual((score["interleavings"], score["boundary_depth"], score["interbranch_travel"]), (0, 0, 0))

    def test_filtered_views_ignore_removed_objects_and_unused_catalog_roots(self):
        graph = graph_fixture()
        before_order, before_metrics = branch_order(graph), boundary_metrics(graph)
        graph["branch_structure"]["roots"].append({"key": "offscreen", "index": 3})
        graph["branch_structure"]["node_memberships"]["removed"] = ["offscreen"]
        graph["branch_structure"]["edge_memberships"]["removed-edge"] = ["offscreen"]
        self.assertEqual(branch_order(graph), before_order)
        self.assertEqual(boundary_metrics(graph), before_metrics)

    def test_offscreen_roots_can_still_identify_visible_branch_members(self):
        graph = graph_fixture()
        graph["nodes"] = [node for node in graph["nodes"] if node["id"] not in ("A", "B")]
        self.assertIsNotNone(branch_order(graph))
        self.assertTrue(boundary_metrics(graph)["enabled"])

    def test_input_sequence_does_not_change_the_preferred_order_or_metrics(self):
        graph = graph_fixture()
        order, metrics = branch_order(graph), boundary_metrics(graph)
        graph["nodes"].reverse()
        graph["edges"].reverse()
        graph["branch_structure"]["roots"].reverse()
        self.assertEqual(branch_order(graph), order)
        self.assertEqual(boundary_metrics(graph), metrics)

    def test_large_many_root_graph_is_fully_measured_without_pairwise_expansion(self):
        graph = {"nodes": [], "edges": [], "branch_structure": {
            "version": 1, "roots": [], "node_memberships": {}, "edge_memberships": {}}}
        metadata = graph["branch_structure"]
        for index in range(250):
            root = f"r{index:03}"
            metadata["roots"].append({"key": root, "index": index + 1})
            for column in range(5):
                key = root if column == 0 else f"{root}:{column}"
                graph["nodes"].append({"id": key, "kind": "transaction", "column": column,
                                       "x": column * 100, "y": index * 30, "width": 10, "height": 10})
                metadata["node_memberships"][key] = [root]
        graph["nodes"].append({"id": "shared", "kind": "address", "column": 5,
                               "x": 500, "y": 3750, "width": 10, "height": 10})
        metadata["node_memberships"]["shared"] = [root["key"] for root in metadata["roots"]]
        for root in metadata["roots"]:
            key = root["key"]
            edge = {"id": f"{key}-edge", "source": f"{key}:4", "target": "shared"}
            graph["edges"].append(edge)
            metadata["edge_memberships"][edge["id"]] = [key]
        order = branch_order(graph)
        self.assertEqual(len(order), 1251)
        self.assertEqual(len(set(order)), 1251)
        metrics = boundary_metrics(graph)
        self.assertTrue(metrics["enabled"])
        self.assertFalse(metrics["truncated"])
        self.assertEqual(metrics["interleavings"], 0)


if __name__ == "__main__":
    unittest.main()
