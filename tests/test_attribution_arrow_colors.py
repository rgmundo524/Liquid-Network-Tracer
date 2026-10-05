"""Optional attribution arrow colors remain presentation-only across renderers."""

import copy
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from liquid_tracer.api import Esplora
from liquid_tracer.common import TraceError, read_json
from liquid_tracer.connector_styles import stroke_width
from liquid_tracer.export import COLORS, build_graph, edge_color, legend_lines, svg_graph
from liquid_tracer.layout_preview import render_svg
from liquid_tracer.mermaid import mermaid_source
from liquid_tracer.miro import make_plan, sync
from tests.fixtures import A, B, C, D, X, output
from tests.test_miro_sync import FakeMiro
from tests import test_service_presentation
from tests.test_service_presentation import designation


class AttributionArrowColorTests(unittest.TestCase):
    setUp = test_service_presentation.ServicePresentationTests.setUp
    node = staticmethod(test_service_presentation.ServicePresentationTests.node)

    def configure(self):
        self.state["labels"] = [designation(entity="bTsE", stop=False)]
        self.state["service_controls"] = {"name_colors": {"btse": "#123abc"}}

    def test_only_adjacent_input_and_output_links_change_without_changing_evidence(self):
        self.configure()
        before = copy.deepcopy(self.state)
        ordinary = build_graph(self.state)
        colored = build_graph(self.state, color_attribution_arrows=True)
        address = "liquid:address:SYNTHETIC-branch-A"
        changed = []
        for old, new in zip(ordinary["edges"], colored["edges"]):
            stripped = {key: value for key, value in new.items() if key not in ("color", "color_source")}
            self.assertEqual(old, stripped)
            if address in (new["source"], new["target"]):
                changed.append(new)
                self.assertEqual((new["color"], new["color_source"]), ("#123abc", "name"))
            else:
                self.assertNotIn("color", new)
                self.assertEqual(edge_color(new), edge_color(new["role"]))
        self.assertEqual({edge["id"] for edge in changed},
                         {"out:" + B + ":0", "out:" + C + ":0", "in:" + C + ":0", "in:" + D + ":0"})
        self.assertEqual(ordinary["nodes"], colored["nodes"])
        self.assertEqual(self.state, before)
        self.assertIn("thicker = traced", " ".join(legend_lines(colored)))

    def test_seed_fill_priority_is_independent_of_assigned_arrow_color(self):
        self.configure()
        self.state["labels"] = [designation("SYNTHETIC-victim-deposit", entity="BTSE", stop=False)]
        self.state["service_controls"]["role_colors"] = {"seed": "#abcdef"}
        for merge in (False, True):
            graph = build_graph(self.state, merge, color_attribution_arrows=True)
            node = self.node(graph, A + ":0")
            self.assertEqual((node["color"], node["role"], node["color_source"]),
                             ("#abcdef", "seed", "role_palette"))
            incident = [edge for edge in graph["edges"] if node["id"] in (edge["source"], edge["target"])]
            self.assertEqual(len(incident), 2)
            self.assertEqual({edge_color(edge) for edge in incident}, {"#123abc"})

    def test_conflicting_names_fall_back_but_compatible_names_share_color(self):
        self.configure()
        self.state["labels"].append(designation(entity="Other", confidence="confirmed", stop=False))
        self.state["service_controls"]["name_colors"]["other"] = "#654321"
        graph = build_graph(self.state, color_attribution_arrows=True)
        self.assertTrue(self.node(graph)["details"]["name_color_conflict"])
        self.assertTrue(all("color" not in edge for edge in graph["edges"]))
        self.state["service_controls"]["name_colors"]["other"] = "#123abc"
        graph = build_graph(self.state, color_attribution_arrows=True)
        self.assertEqual(next(edge for edge in graph["edges"] if edge["id"] == "out:" + B + ":0")["color"], "#123abc")
        self.state["service_controls"]["name_colors"] = {}
        self.assertTrue(all("color" not in edge for edge in build_graph(self.state, color_attribution_arrows=True)["edges"]))

    def test_snapshot_default_and_explicit_disable_restore_default_arrows(self):
        self.configure()
        default = build_graph(self.state)
        self.assertFalse(default["graph_options"]["color_attribution_arrows"])
        self.state["graph_options"] = {"color_attribution_arrows": True}
        self.assertTrue(any("color" in edge for edge in build_graph(self.state)["edges"]))
        disabled = build_graph(self.state, color_attribution_arrows=False)
        self.assertEqual(disabled["edges"], default["edges"])
        self.assertFalse(disabled["graph_options"]["color_attribution_arrows"])
        self.state["graph_options"]["color_attribution_arrows"] = "false"
        with self.assertRaises(TraceError):
            build_graph(self.state)

    def test_bitcoin_inputs_and_events_do_not_acquire_liquid_attribution_colors(self):
        self.configure()
        self.state["labels"].extend([
            designation("SYNTHETIC-funding-context", entity="BTSE", stop=False),
            designation("SYNTHETIC-bitcoin-payout-request", entity="BTSE", stop=False),
        ])
        self.state["transactions"][A]["data"]["vin"][0]["is_pegin"] = True
        graph = build_graph(self.state, include_fees=True, color_attribution_arrows=True)
        nodes = {node["id"]: node for node in graph["nodes"]}
        excluded = [edge for edge in graph["edges"] if any(
            nodes[edge[key]]["kind"] == "event" or nodes[edge[key]]["details"].get("network") == "bitcoin"
            for key in ("source", "target"))]
        self.assertTrue(excluded)
        for edge in excluded:
            self.assertNotIn("color", edge)

    def test_context_links_keep_thin_role_and_grouping_keeps_individual_colors(self):
        self.configure()
        self.state["labels"].append(designation("SYNTHETIC-external-coinput", entity="BTSE", stop=False))
        for index in (2, 3):
            self.state["transactions"][C]["data"]["vin"].append({"txid": X, "vout": index,
                "prevout": output("SYNTHETIC-extra-context-" + str(index)),
                "is_coinbase": False, "is_pegin": False})
        graph = build_graph(self.state, group_context_inputs=True, color_attribution_arrows=True)
        self.assertEqual(graph["context_groups"]["group_count"], 1)
        named = next(edge for edge in graph["edges"] if edge["id"] == "in:" + C + ":2")
        self.assertEqual((edge_color(named), named["role"], stroke_width(named["role"])),
                         ("#123abc", "context_input", 1))
        grouped = [edge for edge in graph["edges"] if edge["source"].startswith("context-group:")]
        self.assertEqual(len(grouped), 2)
        self.assertTrue(all(edge_color(edge) == COLORS["context_edge"] for edge in grouped))

    def test_miro_mermaid_and_both_svg_renderers_agree_on_line_and_arrowhead_colors(self):
        self.configure()
        self.state["labels"].append(designation("SYNTHETIC-funding-context", entity="BTSE", stop=False))
        with patch.object(Esplora, "get", side_effect=AssertionError("No API requests")):
            graph = build_graph(self.state, color_attribution_arrows=True)
            plan = make_plan(graph)
            mermaid = mermaid_source(graph)
            svgs = [(ET.fromstring(svg_graph(graph)), "data-edge-key"),
                    (ET.fromstring(render_svg(graph)), "data-edge-id")]
        expected = {edge["id"]: edge for edge in graph["edges"]}
        for item in plan["connectors"]:
            edge = expected[item["key"]]
            self.assertEqual(item["body"]["style"]["strokeColor"], edge_color(edge))
            self.assertEqual(item["body"]["style"]["strokeWidth"], str(stroke_width(edge["role"])))
        for index, edge in enumerate(sorted(graph["edges"], key=lambda edge: edge["id"])):
            self.assertIn(f"linkStyle {index} stroke:{edge_color(edge)},stroke-width:2px", mermaid)
        for svg, attribute in svgs:
            markers = {element.get("id"): element for element in svg.iter() if element.tag.endswith("marker")}
            drawn = [element for element in svg.iter() if element.get(attribute) in expected]
            self.assertEqual(len(drawn), len(expected))
            for element in drawn:
                edge = expected[element.get(attribute)]
                path = next(child for child in element.iter() if child.tag.endswith("path"))
                self.assertEqual(path.get("stroke"), edge_color(edge))
                self.assertEqual(path.get("stroke-width"), str(stroke_width(edge["role"])))
                marker = markers[path.get("marker-end")[5:-1]]
                self.assertTrue(any(child.get("fill") == edge_color(edge) for child in marker.iter()))

    def test_unsafe_saved_edge_colors_are_rejected_by_every_renderer(self):
        graph = build_graph(self.state)
        graph["edges"][0]["color"] = '#123456" onload="alert(1)'
        for renderer in (make_plan, mermaid_source, svg_graph, render_svg):
            with self.subTest(renderer=renderer.__name__), self.assertRaises(TraceError):
                renderer(graph)

    def test_sync_toggle_recolors_existing_connectors_and_preserves_manual_edits(self):
        self.configure()
        remote = FakeMiro()
        path = self.root / "miro-arrow-colors.json"
        sync(make_plan(build_graph(self.state)), "board=", path, token="test", transport=remote, interval=0)
        ids = {key: value["id"] for key, value in read_json(path)["items"].items()}
        positions = {key: copy.deepcopy(remote.items[value].get("position"))
                     for key, value in ids.items() if key.startswith(("liquid:", "tx:"))}
        changed_key, manual_key = "out:" + B + ":0", "out:" + C + ":0"
        for enabled, color in ((True, "#123abc"), (True, "#654321"), (False, "#654321")):
            self.state["service_controls"]["name_colors"]["btse"] = color
            graph = build_graph(self.state, color_attribution_arrows=enabled)
            sync(make_plan(graph), "board=", path, token="test", transport=remote, interval=0)
            self.assertEqual({key: value["id"] for key, value in read_json(path)["items"].items()}, ids)
            self.assertEqual({key: remote.items[value].get("position")
                              for key, value in ids.items() if key in positions}, positions)
            self.assertEqual(remote.items[ids[changed_key]]["style"]["strokeColor"],
                             color if enabled else COLORS["traced_edge"])
            if color == "#123abc":
                remote.items[ids[manual_key]]["style"]["strokeColor"] = "#abcdef"
            else:
                self.assertEqual(remote.items[ids[manual_key]]["style"]["strokeColor"], "#abcdef")


if __name__ == "__main__":
    unittest.main()
