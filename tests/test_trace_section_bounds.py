"""Saved section bounds follow the geometry actually displayed."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from liquid_tracer.elk_layout import (_apply_candidate, _request_graph,
                                     attachment_point, optimize_graph)
from liquid_tracer.trace_section_local import refresh_section_bounds
from liquid_tracer.trace_sections import SECTION_LAYOUT_VERSION
from tests.test_elk_layout import synthetic_candidate
from tests.test_layout_compactness import graph_fixture
from tests.test_trace_sections import sections


class SectionBoundsTests(unittest.TestCase):
    def test_adapter_records_translated_bounds_without_mutating_raw_candidate(self):
        graph = graph_fixture()
        request, ports, fees = _request_graph(graph)
        candidate = synthetic_candidate(request, [1])[0]
        candidate["sectionGeometry"] = {"version": SECTION_LAYOUT_VERSION, "sections": [{
            "id": "pair", "node_ids": ["source", "target"],
            "x": -24, "y": -24, "width": 2200, "height": 200}]}
        before = deepcopy(candidate)
        result = _apply_candidate(graph, candidate, ports, fees, "elbowed")
        section = result["layout"]["section_geometry"]["sections"][0]
        self.assertEqual(section["x"], min(n["x"] - n["width"] / 2 for n in result["nodes"]))
        self.assertEqual(section["y"], min(n["y"] - n["height"] / 2 for n in result["nodes"]))
        self.assertEqual(section["height"], 100)
        self.assertEqual(result["layout"]["section_geometry"]["bounds_source"], "final_geometry")
        self.assertEqual(candidate, before)

    def test_final_clearance_move_refreshes_section_bounds(self):
        graph = graph_fixture()
        before = deepcopy(graph)

        def move_context(result):
            node = next(n for n in result["nodes"] if n["id"] == "target")
            node["y"] += 10000
            for edge in result["edges"]:
                if edge["target"] == node["id"]:
                    edge["route"][-1] = attachment_point(node, edge["attachment"]["endItem"])

        with patch("liquid_tracer.trace_sections.MIN_SECTION_NODES", 1), \
             patch("liquid_tracer.trace_sections.iter_sections", side_effect=sections), \
             patch("liquid_tracer.elk_layout._worker", side_effect=synthetic_candidate), \
             patch("liquid_tracer.context_clearance.repair_context_clearance", side_effect=move_context):
            result = optimize_graph(graph, "elbowed", layout_attempts=1)
        node = next(n for n in result["nodes"] if n["id"] == "target")
        section = next(s for s in result["layout"]["section_geometry"]["sections"]
                       if "target" in s["node_ids"])
        self.assertEqual(section["y"], node["y"] - node["height"] / 2)
        self.assertEqual(section["height"], node["height"])
        self.assertEqual(graph, before)

    def test_internal_routes_and_captions_count_but_hidden_inputs_do_not(self):
        graph = graph_fixture()
        graph["layout"] = {"section_geometry": {"sections": [{
            "id": "pair", "node_ids": ["source", "target"]}]}}
        edge = graph["edges"][0]
        edge.update(connector_shape="elbowed", route=[{"x": 50, "y": 0},
            {"x": 50, "y": 8000}, {"x": 950, "y": 8000}, {"x": 950, "y": 0}])
        summary = {**deepcopy(edge), "id": "summary", "label": "bundle",
            "route": [{"x": 50, "y": 0}, {"x": 50, "y": 500},
                      {"x": 950, "y": 500}, {"x": 950, "y": 0}],
            "details": {"context_summary": {"member_edge_ids": [edge["id"]]}}}
        graph["context_connectors"] = {"summaries": [summary]}
        before = deepcopy((graph["nodes"], graph["edges"], graph["context_connectors"]))
        refresh_section_bounds(graph)
        section = graph["layout"]["section_geometry"]["sections"][0]
        self.assertEqual(section["y"], -50)
        self.assertEqual(section["height"], 551)  # Caption descent extends one unit below the route.
        self.assertEqual((graph["nodes"], graph["edges"], graph["context_connectors"]), before)
        # A currently displayed summary route takes precedence over its older
        # metadata copy while a display projection is being optimized.
        graph["edges"] = [{**summary, "route": [summary["route"][0],
            {"x": 50, "y": 700}, {"x": 950, "y": 700}, summary["route"][-1]]}]
        refresh_section_bounds(graph)
        self.assertEqual(section["height"], 751)


if __name__ == "__main__":
    unittest.main()
