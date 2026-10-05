"""Routing detours must not silently replace the selected curved appearance."""

import copy
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from liquid_tracer.change_layout import _routes, apply_change_layout
from liquid_tracer.elk_layout import _apply_candidate, _request_graph, fallback_graph
from liquid_tracer.export import build_graph
from liquid_tracer.layout_preview import render_svg
from liquid_tracer.miro import make_plan
from tests.fixtures import fixture
from tests.test_change_output_ports import screenshot_graph
from tests.test_elk_layout import point
from tests.test_layout import state_from


def detour_graph():
    nodes = [{"id": key, "kind": kind, "x": x, "y": 200, "width": 80, "height": 80,
              "column": column, "label": "SYNTHETIC", "details": {}}
             for key, kind, x, column in (
                 ("a", "transaction", 0, 0), ("b", "address", 4000, 2),
                 ("c", "transaction", 2000, 1), ("d", "address", 6000, 3),
                 ("fee0", "event", 0, 0), ("fee1", "event", 230, 0))]
    return {"nodes": nodes, "edges": [
        {"id": "ab", "source": "a", "target": "b"},
        {"id": "cd", "source": "c", "target": "d"},
        {"id": "dc", "source": "d", "target": "c"},
        {"id": "fee", "source": "a", "target": "fee1"}],
        "fee_items": {key: {"endpoint": "shapes"} for key in ("fee0", "fee1")},
        "graph_options": {}, "layout": {}, "presentation_version": 6}


def detour_candidate(graph):
    request, ports, fees = _request_graph(graph)
    coordinates = {node["id"]: node["x"] for node in graph["nodes"]}
    nodes, endpoints = [], {}
    for child in request["children"]:
        node = {"id": child["id"], "width": child["width"], "height": child["height"],
                "x": coordinates[child["id"]], "y": 0, "ports": []}
        for port in child["ports"]:
            x = child["width"] if port["layoutOptions"]["elk.port.side"] == "EAST" else 0
            y = child["height"] / 2
            node["ports"].append({"id": port["id"], "x": x, "y": y})
            endpoints[port["id"]] = point(node["x"] + x, y)
        nodes.append(node)
    edges = [{"id": edge["id"], "sections": [{
        "startPoint": endpoints[edge["sources"][0]],
        "bendPoints": [point(1000, -100), point(3000, -100)],
        "endPoint": endpoints[edge["targets"][0]]}]} for edge in request["edges"]]
    return {"nodes": nodes, "edges": edges}, ports, fees


def routes(graph):
    return {edge["id"]: (edge["route"], edge["attachment"], edge.get("routing_exception"))
            for edge in graph["edges"]}


class CurvedRoutingTests(unittest.TestCase):
    def test_elk_detours_preserve_curves_and_geometry_for_every_exception(self):
        original = detour_graph()
        before = copy.deepcopy(original)
        candidate, ports, fees = detour_candidate(original)
        for budget, reasons in ((250000, {"return", "fee", "obstacle"}),
                                (0, {"return", "fee", "unchecked"})):
            with self.subTest(budget=budget), patch("liquid_tracer.elk_layout.MAX_COMPARISONS", budget):
                curved = _apply_candidate(original, candidate, ports, fees, "curved")
                straight = _apply_candidate(original, candidate, ports, fees, "straight")
                elbowed = _apply_candidate(original, candidate, ports, fees, "elbowed")
                self.assertEqual({edge["routing_exception"] for edge in curved["edges"]}, reasons)
                self.assertTrue(all(edge["connector_shape"] == "curved" for edge in curved["edges"]))
                self.assertTrue(all(edge["connector_shape"] == "elbowed" for edge in straight["edges"]))
                self.assertTrue(all(edge["connector_shape"] == "elbowed" for edge in elbowed["edges"]))
                self.assertEqual(curved["layout"]["routing_exceptions"], 0)
                self.assertEqual(elbowed["layout"]["routing_exceptions"], 0)
                self.assertEqual(straight["layout"]["routing_exceptions"], len(straight["edges"]))
                self.assertEqual(curved["nodes"], straight["nodes"])
                self.assertEqual(routes(curved), routes(straight))
                self.assertEqual(routes(curved), routes(elbowed))
        self.assertEqual(original, before)

    def test_fallback_and_miro_plan_keep_all_curved_connectors_including_fees_and_returns(self):
        state = state_from({data["txid"]: data for key, data in fixture().items()
                            if not key.endswith("outspends")})
        original = build_graph(state, include_fees=True)
        curved = fallback_graph(original, "curved")
        straight = fallback_graph(original, "straight")
        self.assertTrue({"fee", "return"}.issubset({edge["routing_exception"] for edge in curved["edges"]}))
        self.assertEqual(curved["layout"]["routing_exceptions"], 0)
        self.assertEqual(curved["layout"]["metrics"]["routing_exceptions"], 0)
        self.assertEqual(routes(curved), routes(straight))
        plan = make_plan(curved)
        self.assertTrue(plan["connectors"])
        self.assertTrue(all(item["body"]["shape"] == "curved" for item in plan["connectors"]))
        self.assertEqual({item["key"]: item["attachment"] for item in plan["connectors"]},
                         {edge["id"]: edge["attachment"] for edge in curved["edges"]})
        svg = ET.fromstring(render_svg(curved))
        paths = {path.attrib["data-edge-id"]: path.attrib
                 for path in svg.findall(".//{http://www.w3.org/2000/svg}path[@data-edge-id]")}
        self.assertEqual(set(paths), {edge["id"] for edge in curved["edges"]})
        self.assertTrue(all(path["data-appearance"] == "curved" for path in paths.values()))
        self.assertTrue(any("Q" in paths[edge["id"]]["d"] for edge in curved["edges"]
                            if edge["routing_exception"]))

    def test_change_rerouting_preserves_curves_on_fee_return_and_unchecked_paths(self):
        original = detour_graph()
        positions = {node["id"]: (node["x"], node["y"] - index * 10)
                     for index, node in enumerate(original["nodes"])}
        results = {}
        for style in ("curved", "straight", "elbowed"):
            graph = copy.deepcopy(original)
            graph["graph_options"]["connector_style"] = style
            _routes(graph, {node["id"]: node for node in graph["nodes"]}, positions, set())
            self.assertEqual({edge["routing_exception"] for edge in graph["edges"]}, {"fee", "return", "unchecked"})
            expected = "elbowed" if style == "straight" else style
            self.assertTrue(all(edge["connector_shape"] == expected for edge in graph["edges"]))
            results[style] = graph
        self.assertEqual(routes(results["curved"]), routes(results["straight"]))
        self.assertEqual(routes(results["curved"]), routes(results["elbowed"]))

    def test_change_layout_reports_only_actual_style_overrides(self):
        for style, exceptions in (("curved", 0), ("elbowed", 0), ("straight", 1)):
            with self.subTest(style=style):
                graph = screenshot_graph()
                graph["graph_options"]["connector_style"] = style
                apply_change_layout(graph)
                self.assertEqual(graph["layout"]["routing_exceptions"], exceptions)
                if style == "curved":
                    self.assertTrue(all(edge["connector_shape"] == "curved" for edge in graph["edges"]))
                self.assertEqual(graph["edges"][1]["routing_exception"], "unchecked")


if __name__ == "__main__":
    unittest.main()
