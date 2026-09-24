"""Compact drawing captions must not erase input values from local evidence."""

import copy
import csv
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from liquid_tracer.compaction import _caption
from liquid_tracer.edge_labels import caption_text
from liquid_tracer.elk_layout import fallback_graph
from liquid_tracer.export import build_graph, svg_graph, write_csv
from liquid_tracer.layout_preview import render_svg
from liquid_tracer.mermaid import mermaid_source
from tests.test_input_order import child_input, input_order_state


SVG = "{http://www.w3.org/2000/svg}"


class DenseContextCaptionTests(unittest.TestCase):
    def setUp(self):
        self.graph = fallback_graph(build_graph(input_order_state(10, continuing=(9,)),
                                                group_context_inputs=True), "elbowed")
        self.edge = next(edge for edge in self.graph["edges"] if edge["id"] == child_input(0))
        self.edge["quantity"] = "0.0211651 L-BTC"

    def test_both_svg_renderers_retain_full_hover_text_without_visible_dense_captions(self):
        original = copy.deepcopy(self.graph)
        for renderer, identity in ((svg_graph, "data-edge-key"), (render_svg, "data-edge-id")):
            with self.subTest(renderer=renderer.__name__):
                tree = ET.fromstring(renderer(self.graph))
                drawn = next(item for item in tree.iter() if item.get(identity) == self.edge["id"])
                title = drawn.find(SVG + "title").text
                self.assertIn("vin 0", title)
                self.assertIn("0.0211651 L-BTC", title)
                self.assertEqual(sum(item.get(identity) is not None for item in tree.iter()), len(self.graph["edges"]))
                visible = " ".join(item.text or "" for item in tree.iter(SVG + "text"))
                self.assertNotIn("vin 0", visible)
                self.assertIn("vin 9", visible)
        self.assertEqual(self.graph, original)

    def test_mermaid_retains_every_connector_without_the_dense_text_lane(self):
        source = mermaid_source(self.graph)
        self.assertEqual(source.count(" -->"), len(self.graph["edges"]))
        self.assertNotIn("vin 0", source)
        self.assertIn("vin 9", source)
        self.assertIn("9 context addresses", source)

    def test_display_policy_does_not_change_csv_fields_or_reserve_empty_caption_boxes(self):
        self.assertEqual(caption_text(self.edge), "")
        self.assertEqual(caption_text(self.edge, display=False), "vin 0 · 0.0211651 L-BTC")
        self.assertIsNone(_caption(self.edge, [(0, 0), (500, 0)]))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "edges.csv"
            write_csv(path, self.graph["edges"], ["id", "outpoint", "label", "quantity", "details"])
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), len(self.graph["edges"]))
        row = next(row for row in rows if row["id"] == self.edge["id"])
        self.assertEqual(row["label"], "vin 0")
        self.assertEqual(row["quantity"], "0.0211651 L-BTC")
        self.assertEqual(row["outpoint"], self.edge["outpoint"])


if __name__ == "__main__":
    unittest.main()
