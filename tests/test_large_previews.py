import copy
import json
import os
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.elk_layout import fallback_graph
from liquid_tracer.export import build_graph
from liquid_tracer.layout_preview import export_layout
from liquid_tracer.mermaid import RENDER_TIMEOUT, export_mermaid
from tests.fixtures import fixture
from tests.test_layout import state_from


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

    def test_oversized_mermaid_skips_browser_and_retains_complete_source_and_svg(self):
        before = copy.deepcopy(self.graph)
        with patch("liquid_tracer.mermaid.MAX_RENDER_NODES", 1), \
                patch("liquid_tracer.mermaid._render") as renderer, \
                patch("liquid_tracer.elk_layout._worker") as worker:
            result = export_mermaid(self.graph, self.directory)
        renderer.assert_not_called()
        worker.assert_not_called()
        self.assertEqual(self.graph, before)
        saved = self.assert_complete(result)
        self.assertEqual(result["renderer"], "direct_svg")
        self.assertEqual(result["fallback_reason"], "size_limit")
        self.assertEqual(saved["preview"], {"renderer": "direct_svg", "reason": "size_limit"})
        self.assertEqual(Path(result["source"]).read_text().count(" -->|"), len(self.graph["edges"]))
        self.assertEqual(set(json.loads(Path(result["node_map"]).read_text()).values()),
                         {node["id"] for node in self.graph["nodes"]})
        page = Path(result["html"]).read_text()
        self.assertIn("Direct SVG fallback", page)
        self.assertIn("ELK and Mermaid optimization were not applied", page)
        self.assertNotIn("<script", page)
        self.assertIn("data:image/svg+xml;base64,", page)

    def test_mermaid_timeout_replaces_partial_svg_and_finishes_fallback(self):
        def timeout(command, directory):
            (directory / "graph.svg").write_text("partial renderer output")
            raise subprocess.TimeoutExpired(command, RENDER_TIMEOUT)

        with patch.dict(os.environ, {"LIQUID_MERMAID_BIN": "/synthetic/mmdc"}), \
                patch("liquid_tracer.mermaid._render", side_effect=timeout) as renderer:
            result = export_mermaid(self.graph, self.directory)
        renderer.assert_called_once()
        self.assertEqual(result["fallback_reason"], "timeout")
        self.assert_complete(result)
        self.assertIn("Mermaid reached its rendering time limit", Path(result["html"]).read_text())
        self.assertFalse((self.directory / "graph.html.tmp").exists())

    def test_connection_threshold_also_skips_mermaid(self):
        with patch("liquid_tracer.mermaid.MAX_RENDER_EDGES", 1), \
                patch("liquid_tracer.mermaid._render") as renderer:
            result = export_mermaid(self.graph, self.directory)
        renderer.assert_not_called()
        self.assert_complete(result)

    def test_fallback_preview_labels_algorithm_and_does_not_claim_elk_metrics(self):
        display = fallback_graph(self.graph, reason="timeout")
        result = export_layout(display, self.directory)
        report = json.loads(Path(result["report"]).read_text())
        self.assertEqual(report["layout"]["algorithm"], "dependency_layers_v1")
        self.assertEqual(report["layout"]["fallback_reason"], "timeout")
        self.assertEqual(report["metrics"]["candidate_count"], 0)
        self.assertNotIn("selected_seed", report["metrics"])
        page = Path(result["html"]).read_text()
        self.assertIn("<th>Dependency layout fallback</th>", page)
        self.assertIn("ELK reached its 30-second time limit", page)
        self.assertNotIn("<th>ELK layout</th>", page)
        self.assertIn("Dependency layout fallback", Path(result["svg"]).read_text())
        self.assert_complete(result)

    def test_direct_display_limit_keeps_source_and_no_partial_success(self):
        with patch("liquid_tracer.mermaid.MAX_RENDER_NODES", 1), \
                patch("liquid_tracer.layout_preview._MAX_ITEMS", 1), \
                patch("liquid_tracer.mermaid._render") as renderer:
            with self.assertRaisesRegex(TraceError, "combined.*Export CSV.*graph.mmd"):
                export_mermaid(self.graph, self.directory)
        renderer.assert_not_called()
        self.assertTrue((self.directory / "graph.mmd").is_file())
        self.assertTrue((self.directory / "graph.json").is_file())
        self.assertFalse((self.directory / "graph.html").exists())
        self.assertFalse((self.directory / "graph.svg").exists())

    def test_interrupt_fallback_publish_cannot_hide_previous_complete_preview(self):
        original_write = Path.write_text

        def interrupt(path, contents, *args, **kwargs):
            if path.name == "graph.html.tmp":
                original_write(path, contents[:20], *args, **kwargs)
                raise KeyboardInterrupt
            return original_write(path, contents, *args, **kwargs)

        with patch("liquid_tracer.mermaid.MAX_RENDER_NODES", 1), \
                patch.object(Path, "write_text", new=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                export_mermaid(self.graph, self.directory)
        self.assertFalse((self.directory / "graph.html").exists())
        self.assertTrue((self.directory / "graph.mmd").is_file())


if __name__ == "__main__":
    unittest.main()
