"""Named Trace focus keeps forks/joins visible without inventing a spine."""
import copy
import unittest
from unittest.mock import patch

from liquid_tracer.branch_boundaries import branch_order
from liquid_tracer.elk_layout import _candidate_score, _request_graph, optimize_graph
from liquid_tracer.named_group_layout import CORE_STRAIGHTNESS, center_order
from liquid_tracer.trace_layout import (SPINE_STRAIGHTNESS, trace_metrics, trace_order,
                                        trace_priorities, trace_structure)
from liquid_tracer.trace_sections import plan
from liquid_tracer.transaction_neighborhoods import neighborhood_order
from tests.test_elk_layout import synthetic_candidate
from tests.test_trace_layout import fixture
from tests.test_trace_sections import sections


def named_fork():
    graph = {"nodes": [], "edges": [], "fee_items": {},
             "graph_options": {"layout_style": "trace", "center_name": " Group "}}
    def node(key, kind, column, *, named=False, y=0):
        graph["nodes"].append({"id": key, "kind": kind, "column": column,
                               "x": column * 300, "y": y, "width": 80, "height": 80,
                               "details": {"address_attributions": [{"entity": "group"}] if named else []}})
    def edge(key, source, target, outpoint):
        graph["edges"].append({"id": key, "source": source, "target": target,
                               "outpoint": outpoint})
    node("seed", "transaction", 0)
    graph["nodes"][-1]["role"] = "starting_transaction"
    node("named-input", "address", -1, named=True)
    edge("seed-in", "named-input", "seed", "external:0")
    for side, y in (("left", -200), ("right", 200)):
        node(side + "-in", "address", 1, named=True, y=y)
        node(side + "-tx", "transaction", 2, y=y)
        node(side + "-out", "address", 3, named=True, y=y)
        edge(side + "-deposit", "seed", side + "-in", "seed:" + side)
        edge(side + "-spend", side + "-in", side + "-tx", "seed:" + side)
        edge(side + "-output", side + "-tx", side + "-out", side + ":0")
        edge(side + "-join", side + "-out", "join", side + ":0")
    node("join", "transaction", 4)
    node("named-end", "address", 5, named=True)
    edge("join-out", "join", "named-end", "join:0")
    for key, y in (("context-top", -600), ("context-bottom", 600)):
        node(key, "address", 1, y=y)
        edge(key + "-in", key, "left-tx", "outside:" + key)
    return graph


def score(trace, *, overlaps=0):
    geometry = dict(node_overlaps=overlaps, node_intersections=0, crossings=0,
                    connector_overlaps=0, edge_length=1000, truncated=False)
    return _candidate_score(geometry, geometry,
        dict(endpoint_order_inversions=0, coincident_ports=0), dict(weighted_vertical_travel=0),
        dict(flow_order_inversions=0, sibling_interleavings=0, transaction_distance=0,
             transaction_center_drift=0),
        dict(interleavings=0, boundary_depth=0, interbranch_travel=0),
        dict(alignment_deviation=0, center_offset=0), "geometry", trace=trace)


class TraceNamedFocusTests(unittest.TestCase):
    def test_both_named_forks_and_join_share_a_focus_band_but_not_a_spine(self):
        graph = named_fork()
        original = copy.deepcopy(graph)
        structure = trace_structure(graph)
        core = {"named-input", "seed", "left-in", "left-tx", "left-out",
                "right-in", "right-tx", "right-out", "join", "named-end"}
        self.assertEqual(structure["named_core"], core)
        self.assertEqual(structure["named_transactions"], {"seed", "left-tx", "right-tx", "join"})
        self.assertIn("left-tx", structure["spine"])
        self.assertNotIn("right-tx", structure["spine"])
        self.assertNotIn("right-spend", structure["edges"])
        self.assertTrue(any("right-tx" in branch for branch in structure["branches"]))
        order = trace_order(graph, structure=structure)
        indexes = sorted(order.index(key) for key in core)
        self.assertEqual(indexes, list(range(indexes[0], indexes[-1] + 1)))
        self.assertEqual(len(order), len(set(order)))
        self.assertEqual(set(order), {node["id"] for node in graph["nodes"]})
        priorities = trace_priorities(graph, structure)
        self.assertEqual(priorities["right-spend"], CORE_STRAIGHTNESS)
        self.assertEqual(priorities["left-spend"], SPINE_STRAIGHTNESS)
        request, _, _ = _request_graph(graph, structure=structure)
        requested = {edge["id"]: edge for edge in request["edges"]}
        self.assertEqual(requested["right-spend"]["layoutOptions"]["elk.layered.priority.straightness"],
                         str(CORE_STRAIGHTNESS))
        self.assertEqual(graph, original)

    def test_named_reused_junction_is_single_focus_member_and_never_a_backbone(self):
        graph = named_fork()
        # One address receives two outputs and spends both, including a real
        # backward continuation. The graph and its loop must stay unchanged.
        graph["nodes"] = [node for node in graph["nodes"] if node["id"] != "right-out"]
        for edge in graph["edges"]:
            for field in ("source", "target"):
                if edge[field] == "right-out":
                    edge[field] = "left-in"
        graph["edges"].append({"id": "return", "source": "left-in", "target": "seed",
                               "outpoint": "right:0"})
        next(node for node in graph["nodes"] if node["id"] == "named-end")["layout_hub"] = True
        original = copy.deepcopy(graph)
        structure = trace_structure(graph)
        self.assertIn("left-in", structure["shared_hubs"])
        self.assertIn("left-in", structure["named_core"])
        self.assertNotIn("left-in", structure["spine"])
        self.assertNotIn("return", structure["edges"])
        self.assertNotIn("named-end", structure["named_core"])
        self.assertEqual(structure["named_excluded_hubs"], {"named-end"})
        self.assertEqual(trace_order(graph).count("left-in"), 1)
        self.assertEqual(trace_order(graph).count("named-end"), 1)
        self.assertEqual(trace_metrics(graph)["named_group_excluded_hubs"], 1)
        self.assertEqual(graph, original)

    def test_named_band_metrics_allow_parallel_rows_but_detect_displacement_and_intrusion(self):
        graph = named_fork()
        baseline = trace_metrics(graph)
        for field in ("named_group_alignment", "named_group_center_offset", "named_group_interleaving"):
            self.assertEqual(baseline[field], 0)
        # Widening both sides equally preserves the band's center; there is no
        # reward for collapsing two visible named nodes onto one another.
        for node in graph["nodes"]:
            if node["id"].startswith("left-"):
                node["y"] = -500
            elif node["id"].startswith("right-"):
                node["y"] = 500
        self.assertEqual(trace_metrics(graph)["named_group_alignment"], 0)
        self.assertEqual(trace_metrics(graph)["named_group_center_offset"], 0)
        next(node for node in graph["nodes"] if node["id"] == "context-top")["y"] = 0
        self.assertGreater(trace_metrics(graph)["named_group_interleaving"], 0)
        for node in graph["nodes"]:
            if node["column"] == 3:
                node["y"] += 1000
        self.assertGreater(trace_metrics(graph)["named_group_alignment"], 0)
        self.assertGreater(trace_metrics(graph)["named_group_center_offset"], 0)

    def test_name_costs_affect_selection_after_collision_safety_only(self):
        baseline = trace_metrics(named_fork())
        for field in ("named_group_alignment", "named_group_center_offset", "named_group_interleaving"):
            with self.subTest(field=field):
                worse = {**baseline, field: 10}
                self.assertLess(score(baseline), score(worse))
                self.assertLess(score(worse), score(baseline, overlaps=1))
                self.assertEqual(score({**baseline, "enabled": False}),
                                 score({**worse, "enabled": False}))

    def test_no_matching_name_preserves_existing_trace_preferences(self):
        graph, _, _ = fixture()
        expected = trace_structure(graph), trace_order(graph), trace_priorities(graph), trace_metrics(graph)
        graph["graph_options"]["center_name"] = "Unmatched"
        actual = trace_structure(graph), trace_order(graph), trace_priorities(graph), trace_metrics(graph)
        expected[0]["selected_name"] = True
        self.assertEqual(actual, expected)
        named = named_fork()
        expected = trace_structure(named), trace_order(named), trace_priorities(named), trace_metrics(named)
        named["nodes"].reverse()
        named["edges"].reverse()
        self.assertEqual((trace_structure(named), trace_order(named), trace_priorities(named), trace_metrics(named)),
                         expected)

    def test_named_metrics_ignore_separately_placed_fees_and_need_no_exact_spine(self):
        graph = named_fork()
        before = trace_metrics(graph)
        for index in range(20):
            key = f"fee:{index}"
            graph["nodes"].append({"id": key, "kind": "event", "column": 1, "x": 0, "y": 99999,
                                   "width": 5000, "height": 5000})
            graph["fee_items"][key] = {"endpoint": "shapes"}
        self.assertEqual({key: value for key, value in trace_metrics(graph).items() if key.startswith("named_")},
                         {key: value for key, value in before.items() if key.startswith("named_")})
        graph["nodes"] = [node for node in graph["nodes"] if node["kind"] == "address"]
        graph["edges"], graph["fee_items"] = [], {}
        structure = trace_structure(graph)
        self.assertEqual(structure["spine"], [])
        self.assertEqual(len(structure["named_members"]), 6)
        self.assertEqual(len(trace_order(graph)), len(graph["nodes"]))
        self.assertEqual(trace_metrics(graph)["named_group_nodes"], 6)

    def test_section_focus_preserves_exact_backbone_and_one_bounded_owner_per_node(self):
        graph = named_fork()
        structure = trace_structure(graph)
        request, _, _ = _request_graph(graph, structure=structure)
        original = copy.deepcopy((graph, request, structure))
        groups, _, backbone = plan(graph, request, structure=structure)
        remaining = structure["named_core"] - backbone
        self.assertGreater(len(remaining), 1)
        self.assertIn(remaining, [set(group) for group in groups])
        with patch("liquid_tracer.trace_sections.MAX_SECTION_NODES", 5):
            groups, requests, backbone = plan(graph, request, structure=structure)
        self.assertEqual(backbone, set(structure["spine"]))
        members = [key for group in groups for key in group]
        self.assertEqual(len(members), len(set(members)))
        self.assertEqual(set(members), {node["id"] for node in graph["nodes"]})
        self.assertTrue(all(len(item["children"]) <= 5 for item in requests))
        self.assertEqual((graph, request, structure), original)

    def test_synthetic_section_pipeline_retains_named_forks_and_every_connection(self):
        graph = named_fork()
        original = copy.deepcopy(graph)
        with patch("liquid_tracer.trace_sections.MIN_SECTION_NODES", 1), \
             patch("liquid_tracer.trace_sections.iter_sections", side_effect=sections), \
             patch("liquid_tracer.elk_layout._worker", side_effect=synthetic_candidate):
            result = optimize_graph(graph, "elbowed", layout_attempts=1)
        self.assertEqual(graph, original)
        self.assertEqual({node["id"] for node in result["nodes"]}, {node["id"] for node in graph["nodes"]})
        self.assertEqual([(edge["id"], edge["source"], edge["target"], edge["outpoint"]) for edge in result["edges"]],
                         [(edge["id"], edge["source"], edge["target"], edge["outpoint"]) for edge in graph["edges"]])
        self.assertEqual(result["layout"]["trace_layout"]["named_group_nodes"], 10)
        self.assertEqual(result["layout"]["trace_layout"]["named_group_transactions"], 4)
        self.assertEqual(result["layout"]["metrics"]["after"]["node_overlaps"], 0)
        members = [key for section in result["layout"]["section_geometry"]["sections"]
                   for key in section["node_ids"]]
        self.assertEqual(len(members), len(set(members)))
        self.assertEqual(set(members), {node["id"] for node in graph["nodes"]})

    def test_legacy_standard_and_pegin_exclusion_remain_explicit(self):
        graph = named_fork()
        graph["graph_options"]["layout_style"] = "standard"
        expected = center_order(graph, neighborhood_order(graph, branch_order(graph)))
        request, _, _ = _request_graph(graph)
        self.assertEqual(request["centerNodeOrder"], expected)
        self.assertIsNone(trace_order(graph))
        self.assertEqual(trace_priorities(graph), {})
        graph["graph_options"]["layout_style"] = "trace"
        for edge in graph["edges"]:
            if edge["id"] == "right-spend":
                edge["details"] = {"vin": {"is_pegin": True}}
        structure = trace_structure(graph)
        self.assertNotIn("right-tx", structure["named_transactions"])
        self.assertNotIn("right-spend", structure["named_edges"])
        self.assertNotIn("right-spend", structure["edges"])


if __name__ == "__main__":
    unittest.main()
