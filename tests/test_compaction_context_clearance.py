"""Compaction must preserve clearance after Miro recomputes elbow bends."""

import copy
import unittest
from unittest.mock import patch

from liquid_tracer.compaction import _Budget, _Geometry, _points, compact_graph
from liquid_tracer.elk_layout import segment_hits_node
from liquid_tracer.routing_estimates import route_variants
from tests.test_compaction import edge, graph, node


def geometry_for(fixture):
    nodes = {item["id"]: item for item in fixture["nodes"]}
    edges = {item["id"]: item for item in fixture["edges"]}
    points = {key: _points(item, nodes) for key, item in edges.items()}
    return nodes, edges, _Geometry(nodes, edges, points, _Budget(len(nodes) + len(edges)))


def store_route(item, points):
    item["route"] = [{"x": x, "y": y} for x, y in points]


def context_fixture():
    context = edge("context", "c", "t", caption="")
    context["role"] = "context_input"
    fixture = graph([node("c", "address", 0, 300), node("t", "transaction", 1000, 300),
                     node("u", "transaction", 0, 0), node("v", "address", 1500, 600)],
                    [context, edge("branch", "u", "v", caption="", shape="elbowed")])
    store_route(fixture["edges"][1], [(50, 0), (400, 0), (400, 600), (1450, 600)])
    return fixture


class CompactionContextClearanceTests(unittest.TestCase):
    def test_context_cannot_move_onto_miro_bend_when_saved_route_is_clear(self):
        fixture = context_fixture()
        nodes, edges, geometry = geometry_for(fixture)
        nodes["c"]["x"] = 750
        proposed = {"context": _points(edges["context"], nodes)}
        bounds = (-100, -100, 1600, 700)
        self.assertFalse(geometry.safe("c", proposed, bounds))
        # Only the board's midpoint bend occupies the proposed context circle.
        geometry.context_nodes.clear()
        self.assertTrue(geometry.safe("c", proposed, bounds))

    def test_changed_output_route_cannot_cover_stationary_context_summary(self):
        context = edge("context", "summary", "t", caption="")
        context["role"] = "context_input"
        fixture = graph([node("u", "transaction", 0, 0), node("a", "address", 2000, 600),
                         node("summary", "context_group", 625, 300), node("t", "transaction", 1000, 300)],
                        [edge("branch", "u", "a", caption="", shape="elbowed"), context])
        store_route(fixture["edges"][0], [(50, 0), (300, 0), (300, 600), (1950, 600)])
        nodes, _, geometry = geometry_for(fixture)
        nodes["a"]["x"] = 1250
        proposed = {"branch": [(50, 0), (300, 0), (300, 600), (1200, 600)]}
        bounds = (-100, -100, 2100, 700)
        self.assertFalse(geometry.safe("a", proposed, bounds))
        geometry.context_nodes.clear()
        self.assertTrue(geometry.safe("a", proposed, bounds))

    def test_moved_contexts_estimated_input_route_must_clear_other_nodes(self):
        context = edge("context", "c", "t", caption="", shape="elbowed")
        context["role"] = "context_input"
        fixture = graph([node("c", "address", 0, 600), node("t", "transaction", 2000, 0),
                         node("obstacle", "transaction", 1375, 300)], [context])
        store_route(fixture["edges"][0], [(50, 600), (1000, 600), (1000, 0), (1950, 0)])
        nodes, _, geometry = geometry_for(fixture)
        nodes["c"]["x"] = 750
        proposed = {"context": [(800, 600), (1000, 600), (1000, 0), (1950, 0)]}
        bounds = (-100, -100, 2100, 700)
        self.assertFalse(geometry.safe("c", proposed, bounds))
        geometry.context_nodes.clear()
        self.assertTrue(geometry.safe("c", proposed, bounds))

    def test_incomplete_estimated_route_query_rejects_move(self):
        fixture = context_fixture()
        nodes, edges, geometry = geometry_for(fixture)
        nodes["c"]["x"] = 250
        proposed = {"context": _points(edges["context"], nodes)}
        with patch.object(geometry.estimated_segment_index, "query", return_value=None):
            self.assertFalse(geometry.safe("c", proposed, (-100, -100, 1600, 700)))

    def test_optional_compaction_keeps_context_clear_and_preserves_evidence(self):
        fixture = context_fixture()
        fixture["edges"][0].update(outpoint="synthetic:2", details={"vin": 1, "validated_trace_link": False})
        before = copy.deepcopy(fixture)
        result = compact_graph(fixture)
        self.assertEqual(fixture, before)
        self.assertEqual([item["id"] for item in result["nodes"]], [item["id"] for item in before["nodes"]])
        for old, new in zip(before["edges"], result["edges"]):
            for key in ("id", "source", "target", "role", "attachment", "outpoint", "details"):
                self.assertEqual(old.get(key), new.get(key))
        nodes = {item["id"]: item for item in result["nodes"]}
        for item in result["edges"]:
            if "c" in (item["source"], item["target"]):
                continue
            for route in route_variants(item, nodes):
                for a, b in zip(route, route[1:]):
                    self.assertFalse(segment_hits_node({"x": a[0], "y": a[1]}, {"x": b[0], "y": b[1]}, nodes["c"]))
        report = result["layout"]["compaction"]
        self.assertLessEqual(report["after"]["main"]["width"], report["before"]["main"]["width"])
        self.assertLessEqual(report["after"]["main"]["height"], report["before"]["main"]["height"])

    def test_packing_context_component_reserves_estimated_fee_corridor(self):
        summary = node("summary", "context_group", 1000, 1000)
        summary["width"] = 440
        context = edge("context", "summary", "t", caption="")
        context["role"] = "context_input"
        fee = edge("fee", "payer", "fee-node", caption="", shape="elbowed")
        fee["routing_exception"] = "fee"
        fee["attachment"]["endItem"]["position"] = {"x": "50%", "y": "100%"}
        fixture = graph([node("payer", "transaction", 0, 0), node("fee-node", "address", 1500, 1000),
                         summary, node("t", "transaction", 1500, 1000)], [fee, context])
        fixture["fee_items"] = {"fee-node": {"endpoint": "shapes"}}
        store_route(fixture["edges"][0], [(50, 0), (1800, 0), (1800, 1200), (1500, 1200), (1500, 1050)])

        def covers_summary(result):
            nodes = {item["id"]: item for item in result["nodes"]}
            return any(segment_hits_node({"x": a[0], "y": a[1]}, {"x": b[0], "y": b[1]}, nodes["summary"])
                       for route in route_variants(result["edges"][0], nodes)
                       for a, b in zip(route, route[1:]))

        self.assertFalse(covers_summary(fixture))
        self.assertFalse(covers_summary(compact_graph(fixture)))
        # Saved ELK bends alone allow a disconnected summary to be packed
        # across the fee connector's vertical Miro approach.
        with patch("liquid_tracer.compaction._route_variants", side_effect=lambda edge, nodes, points: [points]):
            self.assertTrue(covers_summary(compact_graph(fixture)))


if __name__ == "__main__":
    unittest.main()
