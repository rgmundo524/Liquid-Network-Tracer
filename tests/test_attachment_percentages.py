"""Attachment serialization must be accepted by preview and Miro validators."""

import copy
import json
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from liquid_tracer.common import TraceError
from liquid_tracer.elk_layout import _percent, _port, attachment_point
from liquid_tracer.layout_preview import _fraction, export_layout
from liquid_tracer.miro import make_plan, validate_plan
from tests.test_layout_preview import graph_fixture


class AttachmentPercentageTests(unittest.TestCase):
    def test_finite_percentages_use_plain_decimal_with_six_place_precision(self):
        values = (
            (0, "0%"),
            (0.0000001, "0%"),
            (0.0000009, "0.000001%"),
            (0.000004, "0.000004%"),
            (0.000049, "0.000049%"),
            (0.0001, "0.0001%"),
            (10, "10%"),
            (40.0, "40%"),
            (50.02, "50.02%"),
            (99.999999, "99.999999%"),
            (99.9999999, "100%"),
            (100, "100%"),
        )
        for value, expected in values:
            with self.subTest(value=value):
                serialized = _percent(value)
                self.assertEqual(serialized, expected)
                self.assertRegex(serialized, r"^\d+(?:\.\d{1,6})?%$")
                self.assertAlmostEqual(_fraction(serialized), float(expected[:-1]) / 100)

    def test_clamping_keeps_zero_and_hundred_for_integer_and_fractional_overflow(self):
        values = (
            (-1e-7, "0%"),
            (-0.01, "0%"),
            (-1, "0%"),
            (-1000.5, "0%"),
            (-1e308, "0%"),
            (100.0000001, "100%"),
            (100.01, "100%"),
            (101, "100%"),
            (1000.5, "100%"),
            (1e308, "100%"),
        )
        for value, expected in values:
            with self.subTest(value=value):
                self.assertEqual(_percent(value), expected)

    def test_nonfinite_values_and_booleans_are_not_coordinates(self):
        for value in (float("nan"), float("inf"), float("-inf"), True, False):
            with self.subTest(value=value), self.assertRaises(TraceError):
                _percent(value)

    def test_near_midpoint_circle_port_exports_and_validates_without_geometry_changes(self):
        graph = graph_fixture()
        graph["nodes"] = graph["nodes"][:2]
        graph["edges"] = graph["edges"][:1]
        graph["connector_attachment"] = "transaction_ports_v2"
        source, target = graph["nodes"]
        target.update(width=200, height=200)
        edge = graph["edges"][0]
        edge["attachment"]["endItem"] = _port(target, 0, 100.04)
        edge["route"] = [attachment_point(source, edge["attachment"]["startItem"]),
                         attachment_point(target, edge["attachment"]["endItem"])]
        original = copy.deepcopy(graph)

        with tempfile.TemporaryDirectory() as temporary:
            paths = export_layout(graph, Path(temporary) / "preview")
            saved = json.loads(Path(paths["graph"]).read_text())
            self.assertEqual(graph, original)
            self.assertEqual(saved, original)
            self.assertEqual(edge["attachment"]["endItem"]["position"],
                             {"x": "0.000004%", "y": "50.02%"})

            svg = ET.parse(paths["svg"])
            namespace = {"s": "http://www.w3.org/2000/svg"}
            svg_edges = svg.findall(".//s:path[@data-edge-id]", namespace)
            self.assertEqual([path.get("data-edge-id") for path in svg_edges], [edge["id"]])
            coordinates = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", svg_edges[0].get("d"))]
            self.assertEqual(len(coordinates), 4)
            for actual, expected in zip(coordinates, (edge["route"][0]["x"], edge["route"][0]["y"],
                                                       edge["route"][1]["x"], edge["route"][1]["y"])):
                self.assertAlmostEqual(actual, expected, delta=0.001)

            plan = make_plan(saved)
            validate_plan(plan)
            self.assertEqual(plan["connectors"][0]["attachment"], edge["attachment"])
            shapes = {shape["key"]: shape["body"] for shape in plan["shapes"]}
            for node in graph["nodes"]:
                self.assertEqual(shapes[node["id"]]["position"],
                                 {"x": node["x"], "y": node["y"], "origin": "center"})
                self.assertEqual(shapes[node["id"]]["geometry"],
                                 {"width": node["width"], "height": node["height"]})


if __name__ == "__main__":
    unittest.main()
