"""Real ELK recovery for many inputs sharing a fixed-height transaction node."""

import copy
import unittest

from liquid_tracer.elk_layout import attachment_point, optimize_graph
from liquid_tracer.export import build_graph
from liquid_tracer.miro import make_plan, validate_plan
from tests.test_elk_layout import HAS_ELK
from tests.test_input_order import input_order_state


@unittest.skipUnless(HAS_ELK, "Run liquid-layout-setup to install the pinned local ELK engine")
class DenseElkInputOrderTests(unittest.TestCase):
    def test_dense_inputs_keep_complete_geometry_when_optional_order_is_rejected(self):
        # More than 300 graph nodes selects the production flow-weighted first
        # attempt. Network simplex rounds these 320 WEST ports onto the
        # 160-unit transaction side, so strict traced-first ordering cannot be
        # preserved. The independently captured geometry remains usable.
        state = input_order_state(320, continuing=(319,))
        state["ancestor_runs"] = []
        original_state = copy.deepcopy(state)
        graph = build_graph(state)
        original_graph = copy.deepcopy(graph)
        self.assertGreater(len(graph["nodes"]), 300)

        result = optimize_graph(graph, connector_style="elbowed", layout_attempts=1)

        self.assertEqual(state, original_state)
        self.assertEqual(graph, original_graph)
        self.assertEqual(result["layout"]["algorithm"], "elk_layered_v1")
        self.assertEqual(result["layout"]["branch_organization"]["profile"], "flow_weighted")
        self.assertEqual(result["layout"]["input_order"]["policy"], "geometry")
        self.assertEqual(result["layout"]["input_order"]["fallback_reason"],
                         "traced_first_order_not_preserved")
        self.assertEqual(result["layout"]["metrics"]["candidate_count"], 1)
        self.assertEqual(result["layout"]["metrics"]["successful_count"], 1)
        self.assertEqual(result["layout"]["metrics"]["failed_count"], 0)

        original_nodes = {node["id"]: node for node in graph["nodes"]}
        nodes = {node["id"]: node for node in result["nodes"]}
        self.assertEqual(nodes.keys(), original_nodes.keys())
        for key, node in nodes.items():
            self.assertEqual(node["details"], original_nodes[key]["details"])
        original_edges = {edge["id"]: edge for edge in graph["edges"]}
        edges = {edge["id"]: edge for edge in result["edges"]}
        self.assertEqual(edges.keys(), original_edges.keys())
        for key, edge in edges.items():
            self.assertEqual({field: edge[field] for field in original_edges[key]}, original_edges[key])
            self.assertEqual(edge["route"][0],
                             attachment_point(nodes[edge["source"]], edge["attachment"]["startItem"]))
            self.assertEqual(edge["route"][-1],
                             attachment_point(nodes[edge["target"]], edge["attachment"]["endItem"]))

        # Complete attachments and topology must still pass the real board
        # boundary validation. This does not make any live Miro requests.
        plan = make_plan(result)
        validate_plan(plan)
        connectors = {item["key"]: item for item in plan["connectors"]}
        self.assertEqual(connectors.keys(), original_edges.keys())
        for key, edge in edges.items():
            self.assertEqual(connectors[key]["attachment"], edge["attachment"])
