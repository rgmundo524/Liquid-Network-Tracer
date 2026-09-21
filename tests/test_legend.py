"""Readable color keys follow the graph snapshot without changing evidence."""

import copy
import unittest
import xml.etree.ElementTree as ET

from liquid_tracer.common import TraceError
from liquid_tracer.export import COLORS, build_graph, svg_graph
from liquid_tracer.layout_preview import _preview_html as layout_html
from liquid_tracer.legend import LEGEND_CSS, legend_html, legend_notes, legend_rows
from liquid_tracer.mermaid import _preview_html as mermaid_html
from tests.test_input_order import input_order_state


NS = "{http://www.w3.org/2000/svg}"


def named_graph(name="BTSE"):
    name = name.strip()
    state = input_order_state()
    state["labels"] = [{"kind": "address", "value": "SYNTHETIC-input-order-0",
                        "entity": name, "confidence": "confirmed", "stop": False},
                       {"kind": "address", "value": "SYNTHETIC-input-order-1",
                        "entity": name.lower(), "confidence": "suspected", "stop": False}]
    state["service_controls"] = {"name_colors": {name.casefold(): "#123abc", "unused name": "#987654"},
                                 "role_colors": {"seed": "#112233", "transaction": "#fedcba"}}
    return build_graph(state, color_attribution_arrows=True)


class LegendTests(unittest.TestCase):
    def test_role_rows_use_real_default_and_custom_colors(self):
        defaults = {row["key"]: row for row in legend_rows()}
        for key, color in COLORS.items():
            self.assertEqual(defaults["role:" + key]["color"], color)
        graph = named_graph()
        rows = {row["key"]: row for row in legend_rows(graph)}
        self.assertEqual(rows["role:seed"]["color"], "#112233")
        self.assertEqual(rows["role:transaction"]["color"], "#fedcba")
        self.assertEqual(rows["role:traced_edge"]["color"], COLORS["traced_edge"])

    def test_named_rows_deduplicate_case_and_omit_unused_assignments(self):
        graph = named_graph()
        names = [row for row in legend_rows(graph) if row["key"].startswith("name:")]
        self.assertEqual(len(names), 1)
        self.assertEqual(names[0]["key"], "name:btse")
        self.assertEqual(names[0]["label"], "BTSE")
        self.assertEqual(names[0]["color"], "#123abc")
        self.assertIn("arrows directly touching", names[0]["description"])
        graph["graph_options"]["color_attribution_arrows"] = False
        self.assertNotIn("arrows", legend_rows(graph)[-1]["description"])
        self.assertIn("Arrows use", " ".join(legend_notes(graph)))

    def test_html_escapes_names_and_uses_visible_circles(self):
        graph = named_graph('<img src=x onerror="bad()"> & Service')
        document = legend_html(graph)
        self.assertNotIn('<img', document)
        self.assertIn('&lt;img', document)
        self.assertIn('&amp; Service', document)
        self.assertEqual(document.count('class="trace-legend-swatch"'), len(legend_rows(graph)))
        self.assertIn('style="background-color:#123abc"', document)
        self.assertIn('border-radius:50%', LEGEND_CSS)
        self.assertIn('overflow-wrap:anywhere', LEGEND_CSS)
        self.assertIn('aria-label="Graph legend"', document)

    def test_unsafe_saved_colors_are_rejected(self):
        graph = named_graph()
        graph["service_controls"]["role_colors"]["seed"] = 'red;position:absolute'
        with self.assertRaises(TraceError):
            legend_html(graph)

    def test_local_html_previews_share_visible_key_and_collapsed_details(self):
        graph = named_graph()
        for document in (layout_html(graph, b'<svg xmlns="http://www.w3.org/2000/svg"/>', {}),
                         mermaid_html(graph, b'<svg xmlns="http://www.w3.org/2000/svg"/>')):
            self.assertIn(legend_html(graph), document)
            self.assertIn(LEGEND_CSS, document)
            self.assertIn('<details><summary>Detailed evidence notes</summary>', document)
            self.assertLess(document.index('class="trace-legend"'),
                            document.index('<details><summary>Detailed evidence notes</summary>'))

    def test_svg_wraps_long_names_with_safe_bounds_and_exact_swatches(self):
        graph = named_graph("A very long attribution name " * 6)
        svg = ET.fromstring(svg_graph(graph))
        groups = {group.get("data-legend-key"): group for group in svg.iter(NS + "g")
                  if group.get("data-legend-key")}
        expected = legend_rows(graph)
        self.assertEqual(set(groups), {row["key"] for row in expected})
        first_top = min(node["y"] - node["height"] / 2 for node in graph["nodes"])
        left, top, width, height = map(float, svg.get("viewBox").split())
        for row in expected:
            group = groups[row["key"]]
            self.assertEqual(group.find(NS + "circle").get("fill"), row["color"])
            for text in group.findall(NS + "text"):
                self.assertLess(float(text.get("y")) + 13, first_top)
                self.assertGreater(float(text.get("y")) - 13, top)
                self.assertLess(float(text.get("x")) + len(text.text or "") * 8, left + width)
        name_row = groups[expected[-1]["key"]]
        self.assertGreater(len(name_row.findall(NS + "text")), 4)

    def test_rendering_does_not_change_graph_or_evidence(self):
        graph = named_graph()
        before = copy.deepcopy(graph)
        legend_rows(graph)
        legend_html(graph)
        svg_graph(graph)
        layout_html(graph, b'<svg xmlns="http://www.w3.org/2000/svg"/>', {})
        mermaid_html(graph, b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        self.assertEqual(graph, before)
