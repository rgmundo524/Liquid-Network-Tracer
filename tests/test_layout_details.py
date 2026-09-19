import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET

from liquid_tracer.edge_labels import caption_box, caption_size, route_signature
from liquid_tracer.layout_details import (PAGE_HEIGHT, PAGE_WIDTH, SCALE, _activity_pages,
                                         _segment_hits, render_details)
from liquid_tracer.layout_preview import _geometry, drawing_bounds, export_layout, render_svg
from tests.test_layout_preview import graph_fixture


class LayoutDetailsTests(unittest.TestCase):
    def product(self, graph):
        nodes, edges = _geometry(graph)
        return render_details(graph, nodes, edges, render_svg(graph))

    def test_atlas_preserves_evidence_and_deterministically_indexes_every_connection(self):
        graph = graph_fixture()
        before = copy.deepcopy(graph)
        document, index = self.product(graph)
        self.assertEqual(graph, before)
        self.assertEqual(set(index["edge_pages"]), {edge["id"] for edge in graph["edges"]})
        pages = [page for activity in index["activities"] for page in activity["pages"]]
        self.assertTrue(pages)
        self.assertEqual({key for page in pages for key in page["node_ids"]}, {node["id"] for node in graph["nodes"]})
        for page in pages:
            self.assertEqual(page["bounds"][2] - page["bounds"][0], PAGE_WIDTH)
            self.assertEqual(page["bounds"][3] - page["bounds"][1], PAGE_HEIGHT)
        self.assertEqual(index["scale"], SCALE)
        graph["nodes"].reverse()
        graph["edges"].reverse()
        self.assertEqual(self.product(graph), (document, index))

    def test_activity_pages_are_independent_of_space_between_disconnected_graphs(self):
        graph = graph_fixture()
        isolated = copy.deepcopy(graph["nodes"][0])
        isolated.update(id="tx:isolated", x=100000, y=-100000)
        graph["nodes"].append(isolated)
        document, index = self.product(graph)
        self.assertEqual(len(index["activities"]), 2)
        self.assertLess(index["page_count"], 6)
        self.assertIn("Activity 2", document)
        self.assertEqual(sum(len(activity["node_ids"]) for activity in index["activities"]), 4)

    def test_empty_tiles_are_omitted_but_route_only_tiles_and_continuations_survive(self):
        graph = graph_fixture()
        graph["nodes"] = graph["nodes"][:2]
        graph["nodes"][0].update(x=0, y=0)
        graph["nodes"][1].update(x=7000, y=3500)
        graph["edges"] = graph["edges"][:1]
        edge = graph["edges"][0]
        edge.update(connector_shape="elbowed", route=[{"x": 80, "y": 0}, {"x": 6000, "y": 0},
                    {"x": 6000, "y": 3500}, {"x": 6920, "y": 3500}])
        document, index = self.product(graph)
        pages = index["activities"][0]["pages"]
        self.assertLess(len(pages), 8 * 8)
        self.assertTrue(any(not page["node_ids"] and page["edge_ids"] for page in pages))
        self.assertTrue(any(page["boundary_edge_ids"] for page in pages))
        self.assertTrue(any(page["neighbors"] for page in pages))
        self.assertIn("connections continue beyond this page", document)
        self.assertEqual(set(index["edge_pages"][edge["id"]]), {page["id"] for page in pages})
        # Every sample on a long routed connector is covered by at least one
        # emitted viewport, even where no transaction/address is nearby.
        for start, end in zip(edge["route"], edge["route"][1:]):
            for offset in range(101):
                fraction = offset / 100
                x = start["x"] + (end["x"] - start["x"]) * fraction
                y = start["y"] + (end["y"] - start["y"]) * fraction
                self.assertTrue(any(box[0] <= x <= box[2] and box[1] <= y <= box[3]
                                    for box in (page["bounds"] for page in pages)))

    def test_bounds_include_negative_routes_thick_borders_and_detached_captions(self):
        graph = graph_fixture()
        graph["nodes"][0]["convergence"] = True
        edge = graph["edges"][1]
        edge["route"][1] = {"x": -1000, "y": -1000}
        edge["label_layout"] = {"x": -1500, "y": -2000, **caption_size(edge),
                                "route_signature": route_signature([(point["x"], point["y"]) for point in edge["route"]])}
        nodes, edges = _geometry(graph)
        box = drawing_bounds(nodes, edges)
        self.assertLessEqual(box[0], -1503)
        self.assertLessEqual(box[1], -2003)
        _, index = self.product(graph)
        pages = index["activities"][0]["pages"]
        self.assertTrue(any(edge["id"] in page["caption_ids"] for page in pages))
        border_node = next(node for node in nodes if node["id"] == "tx:synthetic")
        border_box = drawing_bounds([border_node], [])
        self.assertEqual(border_box, (114, 114, 286, 286))

    def test_cycles_parallel_edges_and_group_members_remain_inspectable(self):
        graph = graph_fixture()
        node = graph["nodes"][1]
        node.update(kind="context_group", label="2 context addresses", details={"members": [
            {"id": "address:first", "label": "First address"}, {"id": "address:second", "label": "Second address"}]})
        first = graph["edges"][0]
        graph["edges"].append({**copy.deepcopy(first), "id": "parallel"})
        graph["edges"].append({"id": "return", "source": node["id"], "target": "tx:synthetic",
                               "role": "traced_input", "label": "vin 2"})
        document, index = self.product(graph)
        self.assertEqual(set(index["edge_pages"]), {edge["id"] for edge in graph["edges"]})
        self.assertIn("address:first", document)
        self.assertIn("Second address", document)
        self.assertIn("Full member records are preserved in graph.json", document)

    def test_complete_export_exposes_details_before_publishing_completion_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "preview"
            observed = []
            original = Path.replace

            def capture(path, target):
                if Path(target).name == "graph.html":
                    observed.append(all((destination / name).is_file() for name in ("details.html", "details.json")))
                return original(path, target)

            with patch.object(Path, "replace", capture):
                result = export_layout(graph_fixture(), destination)
            self.assertEqual(observed, [True])
            self.assertIn('href="details.html"', Path(result["html"]).read_text())
            self.assertEqual(json.loads(Path(result["details_index"]).read_text())["schema_version"], 1)
            self.assertTrue(Path(result["details"]).is_file())

    def test_generated_atlas_is_offline_safe_and_reuses_drawing_with_unique_ids(self):
        graph = graph_fixture()
        graph["nodes"][0]["label"] = '<script>alert("x")</script>'
        graph["run_id"] = '<img src="https://example.invalid/">'
        document, _ = self.product(graph)
        self.assertNotIn("<script", document)
        self.assertNotIn("<img", document)
        self.assertIn("&lt;script&gt;", document)
        self.assertIn('default-src \'none\'', document)
        identifiers = re.findall(r'(?<![\w-])id="([^"]+)"', document)
        self.assertEqual(len(identifiers), len(set(identifiers)))
        for svg in re.findall(r'<svg\b.*?</svg>', document, re.S):
            ET.fromstring(svg)
        self.assertIn('<use href="#activity-1-drawing"', document)

    def test_extreme_geometry_retains_complete_overview_with_explicit_atlas_notice(self):
        graph = graph_fixture()
        graph["nodes"][1]["x"] = 10 ** 200
        document, index = self.product(graph)
        self.assertEqual(index["page_count"], 0)
        self.assertIn("complete overview", index["unavailable_reason"])
        self.assertIn("address:synthetic", document)
        self.assertIn("out:synthetic:0", document)
        self.assertIn("Complete SVG", document)


if __name__ == "__main__":
    unittest.main()
