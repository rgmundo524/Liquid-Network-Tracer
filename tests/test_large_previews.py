import copy
import io
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.elk_layout import fallback_graph
from liquid_tracer.export import build_graph
from liquid_tracer.layout_preview import _geometry, export_layout, render_svg
from liquid_tracer.mermaid import _preview_html, export_mermaid
from tests.fixtures import fixture
from tests.test_layout import state_from
from tests.test_layout_preview import graph_fixture


NS = {"s": "http://www.w3.org/2000/svg"}


class LargePreviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "preview"
        transactions = {value["txid"]: value for key, value in fixture().items()
                        if not key.endswith("outspends")}
        self.graph = build_graph(state_from(transactions), include_fees=True)

    def assert_complete(self, result):
        svg = ET.fromstring(Path(result["svg"]).read_bytes())
        self.assertEqual({node.get("data-node-id") for node in svg.findall(".//s:g[@data-node-id]", NS)},
                         {node["id"] for node in self.graph["nodes"]})
        self.assertEqual({(edge.get("data-edge-id"), edge.get("data-source"), edge.get("data-target"))
                          for edge in svg.findall(".//s:path[@data-edge-id]", NS)},
                         {(edge["id"], edge["source"], edge["target"]) for edge in self.graph["edges"]})
        saved = json.loads(Path(result["graph"]).read_text())
        for original, actual in zip(self.graph["nodes"], saved["nodes"]):
            self.assertEqual(original, actual)
        for original, actual in zip(self.graph["edges"], saved["edges"]):
            for key, value in original.items():
                self.assertEqual(value, actual[key])
        return saved

    def test_mermaid_runs_above_previous_thresholds_with_complete_source(self):
        graph = {"nodes": [{"id": f"node:{index}", "kind": "address", "label": "Synthetic 𝑋"}
                            for index in range(1001)],
                 "edges": [{"id": f"edge:{index}", "source": f"node:{index % 1001}",
                            "target": f"node:{(index + 1) % 1001}", "role": "traced_input", "label": "vin 0"}
                           for index in range(2001)]}
        before = copy.deepcopy(graph)

        def render(command, directory):
            (directory / "graph.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><text>fixture</text></svg>')
            return 0

        with patch.dict(os.environ, {"LIQUID_MERMAID_BIN": "/synthetic/mmdc"}), \
                patch("liquid_tracer.mermaid._render", side_effect=render) as renderer:
            result = export_mermaid(graph, self.directory)
        renderer.assert_called_once()
        self.assertEqual(graph, before)
        self.assertNotIn("fallback_reason", result)
        self.assertEqual(json.loads(Path(result["graph"]).read_text()), graph)
        source = Path(result["source"]).read_text()
        self.assertEqual(source.count(" -->|"), 2001)
        self.assertEqual(source.count("\n  style "), 1001)
        self.assertEqual(set(json.loads(Path(result["node_map"]).read_text()).values()),
                         {node["id"] for node in graph["nodes"]})
        config = json.loads((self.directory / "mermaid-config.json").read_text())
        self.assertGreater(config["maxEdges"], len(graph["edges"]))
        self.assertGreater(config["maxTextSize"], len(source.encode("utf-16-le")) // 2)
        self.assertIn("Mermaid preview", Path(result["html"]).read_text())
        self.assertNotIn("Direct SVG fallback", Path(result["html"]).read_text())

    def test_renderer_failure_retains_source_without_switching_layout(self):
        def fail(command, directory):
            (directory / "graph.svg").write_text("partial renderer output")
            return 1

        with patch.dict(os.environ, {"LIQUID_MERMAID_BIN": "/synthetic/mmdc"}), \
                patch("liquid_tracer.mermaid._render", side_effect=fail), \
                patch("liquid_tracer.elk_layout.fallback_graph") as fallback:
            with self.assertRaisesRegex(TraceError, "rendering failed.*graph.mmd"):
                export_mermaid(self.graph, self.directory)
        fallback.assert_not_called()
        self.assertEqual(json.loads((self.directory / "graph.json").read_text()), self.graph)
        self.assertTrue((self.directory / "graph.mmd").is_file())
        self.assertFalse((self.directory / "graph.svg").exists())
        self.assertFalse((self.directory / "graph.html").exists())

    def test_cancelled_renderer_retains_source_without_publishing_preview(self):
        with patch.dict(os.environ, {"LIQUID_MERMAID_BIN": "/synthetic/mmdc"}), \
                patch("liquid_tracer.mermaid._render", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                export_mermaid(self.graph, self.directory)
        self.assertTrue((self.directory / "graph.mmd").is_file())
        self.assertFalse((self.directory / "graph.html").exists())

    def test_historical_fallback_preview_labels_algorithm_without_claiming_elk_metrics(self):
        display = fallback_graph(self.graph, reason="timeout")
        result = export_layout(display, self.directory)
        report = json.loads(Path(result["report"]).read_text())
        self.assertEqual(report["layout"]["algorithm"], "dependency_layers_v1")
        self.assertEqual(report["layout"]["fallback_reason"], "timeout")
        self.assertEqual(report["metrics"]["candidate_count"], 0)
        self.assertNotIn("selected_seed", report["metrics"])
        page = Path(result["html"]).read_text()
        self.assertIn("<th>Dependency layout fallback</th>", page)
        self.assertNotIn("<th>ELK layout</th>", page)
        self.assertIn("Dependency layout fallback", Path(result["svg"]).read_text())
        self.assert_complete(result)
        display["preview"] = {"renderer": "direct_svg", "reason": "timeout"}
        historical_page = _preview_html(display, Path(result["svg"]).read_bytes())
        self.assertIn("Direct SVG fallback", historical_page)
        self.assertIn("Mermaid reached its rendering time limit", historical_page)

    def test_svg_retains_more_than_100000_objects_and_large_coordinates(self):
        count = 100001
        graph = {"layout": {"algorithm": "elk-layered"},
                 "nodes": [{"id": f"synthetic:{index}", "kind": "transaction", "label": "",
                            "x": index * 220, "y": 0, "width": 160, "height": 160}
                           for index in range(count)],
                 "edges": [{"id": "synthetic:first-to-last", "source": "synthetic:0",
                            "target": f"synthetic:{count - 1}", "role": "traced_input", "label": "vin 0"}]}
        svg = render_svg(graph)
        # Parse and clear incrementally so this regression does not also retain
        # a second full DOM. The production SVG itself still includes all data.
        nodes, edges = set(), set()
        for event, element in ET.iterparse(io.BytesIO(svg), events=("end",)):
            if element.get("data-node-id") is not None:
                nodes.add(element.get("data-node-id"))
            if element.get("data-edge-id") is not None:
                edges.add((element.get("data-edge-id"), element.get("data-source"), element.get("data-target")))
            element.clear()
        self.assertEqual(nodes, {node["id"] for node in graph["nodes"]})
        self.assertEqual(edges, {("synthetic:first-to-last", "synthetic:0", f"synthetic:{count - 1}")})

    def test_routes_retain_points_above_former_per_edge_and_total_limits(self):
        graph = graph_fixture()
        for edge in graph["edges"]:
            edge["connector_shape"] = "elbowed"
            edge["route"] = [{"x": index, "y": index % 2} for index in range(125001)]
        _, edges = _geometry(graph)
        self.assertEqual(sum(len(edge["points"]) for edge in edges), 250002)
        for edge in edges:
            original = next(item for item in graph["edges"] if item["id"] == edge["id"])
            self.assertEqual(edge["points"][1:-1], [(point["x"], point["y"]) for point in original["route"][1:-1]])


if __name__ == "__main__":
    unittest.main()
