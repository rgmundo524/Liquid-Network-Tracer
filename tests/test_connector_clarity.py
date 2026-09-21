"""Flow hierarchy and distinct change-merge ports preserve evidence and edits."""

import copy
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from liquid_tracer.change_layout import _routes
from liquid_tracer.common import canonical, digest, read_json
from liquid_tracer.connector_styles import stroke_width
from liquid_tracer.elk_layout import attachment_point, fallback_graph
from liquid_tracer.export import build_graph, svg_graph
from liquid_tracer.legend import legend_notes, legend_rows
from liquid_tracer.input_order import centered_input_positions, input_orders
from liquid_tracer.miro import make_plan, sync
from tests.test_input_order import child_input, input_order_state
from tests.test_miro_sync import FakeMiro, graph


def signed(plan):
    plan["sha256"] = digest(canonical({key: value for key, value in plan.items() if key != "sha256"}))
    return plan


class ConnectorHierarchyTests(unittest.TestCase):
    def test_traced_links_are_emphasized_without_changing_colors_or_roles(self):
        value = graph()
        value["edges"][0]["role"] = "context_input"
        before = copy.deepcopy(value)
        plan = make_plan(value)
        styles = [item["body"]["style"] for item in plan["connectors"]]
        self.assertEqual([style["strokeWidth"] for style in styles], ["1", "3"])
        self.assertEqual([style["strokeColor"] for style in styles], ["#9ca3af", "#155e75"])
        self.assertEqual(value, before)
        self.assertEqual(stroke_width("context_output"), 1)
        self.assertEqual(stroke_width("traced_input"), 3)

    def test_style_upgrade_preserves_manual_edits_and_repeated_sync_is_idle(self):
        for reorganize in (False, True):
            with self.subTest(reorganize=reorganize), tempfile.TemporaryDirectory() as temporary:
                state_path = Path(temporary) / "miro.json"
                remote = FakeMiro()
                value = graph()
                value["edges"][1]["role"] = "context_output"
                desired = make_plan(value)
                old = copy.deepcopy(desired)
                for item in old["connectors"]:
                    item["body"]["style"]["strokeWidth"] = "2"
                old = signed(old)
                def publish(plan):
                    return sync(plan, "synthetic-board", state_path, token="synthetic-token",
                                transport=remote, interval=0, reorganize=reorganize)

                publish(old)
                ids = {key: item["id"] for key, item in read_json(state_path)["items"].items()}
                edited = remote.items[ids["input:1:0"]]
                edited["style"].update(strokeWidth="5", strokeColor="#123456")
                edited["captions"][0]["content"] = "Analyst annotation"
                report = publish(desired)
                self.assertEqual(edited["style"]["strokeWidth"], "5")
                self.assertEqual(edited["style"]["strokeColor"], "#123456")
                self.assertEqual(edited["captions"][0]["content"], "Analyst annotation")
                self.assertEqual(remote.items[ids["output:1:0"]]["style"]["strokeWidth"], "1")
                self.assertTrue(any(row["field"] == "style.strokeWidth" for row in report["conflicts"]))
                self.assertEqual(ids, {key: item["id"] for key, item in read_json(state_path)["items"].items()})
                writes = len(remote.writes)
                publish(desired)
                self.assertEqual(len(remote.writes), writes)

    def test_context_summary_is_a_rectangle_without_address_count_caption(self):
        value = build_graph(input_order_state(), group_context_inputs=True)
        summary = next(node for node in value["nodes"] if node["kind"] == "context_group")
        shape = next(item for item in make_plan(value)["shapes"] if item["key"] == summary["id"])
        self.assertEqual(shape["body"]["data"]["shape"], "rectangle")
        self.assertNotIn("TX:", shape["body"]["data"]["content"])

    def test_basic_svg_uses_summary_dimensions_and_shared_connector_widths(self):
        value = build_graph(input_order_state(), group_context_inputs=True)
        summary = next(node for node in value["nodes"] if node["kind"] == "context_group")
        summary["height"] = 240
        svg = ET.fromstring(svg_graph(value))
        namespace = "{http://www.w3.org/2000/svg}"
        group = next(node for node in svg.iter(namespace + "g") if node.get("data-key") == summary["id"])
        rectangle = group.find(namespace + "rect")
        self.assertEqual(float(rectangle.get("width")), summary["width"])
        self.assertEqual(float(rectangle.get("height")), summary["height"])
        self.assertIn("context addresses", " ".join(text.text or "" for text in group.findall(namespace + "text")))
        edges = {edge["id"]: edge for edge in value["edges"]}
        for group in svg.iter(namespace + "g"):
            key = group.get("data-edge-key")
            if key:
                self.assertEqual(float(group.find(namespace + "path").get("stroke-width")),
                                 stroke_width(edges[key]["role"]))

    def test_basic_svg_places_swatch_legend_above_the_graph(self):
        value = build_graph(input_order_state())
        svg = ET.fromstring(svg_graph(value))
        namespace = "{http://www.w3.org/2000/svg}"
        header = svg.find(namespace + "g")
        rows = [group for group in header.findall(namespace + "g") if group.get("data-legend-key")]
        self.assertEqual(len(rows), len(legend_rows(value)))
        for group, row in zip(rows, legend_rows(value)):
            self.assertEqual(group.find(namespace + "circle").get("fill"), row["color"])
            self.assertEqual(" ".join(text.text or "" for text in group.findall(namespace + "text")),
                             row["label"] + " " + row["description"])
        notes = [text for text in header.findall(namespace + "text") if text.get("class") == "legend-note"]
        self.assertEqual(" ".join(text.text or "" for text in notes), " ".join(legend_notes(value)))
        labels = header.findall(namespace + "text") + [text for row in rows for text in row.findall(namespace + "text")]
        bottom = max(float(text.get("y")) + float(text.get("font-size")) for text in labels)
        self.assertLess(bottom, min(node["y"] - node["height"] / 2 for node in value["nodes"]))


class ChangeMergePortTests(unittest.TestCase):
    def test_multiple_change_inputs_and_intervening_inputs_get_distinct_ports(self):
        value = fallback_graph(build_graph(input_order_state(4, continuing=(1, 2, 3))))
        before = copy.deepcopy(value)
        order = next(iter(input_orders(value).values()))
        positions = centered_input_positions(value, {child_input(1), child_input(3)})
        self.assertEqual(order, [child_input(i) for i in (1, 2, 3, 0)])
        self.assertEqual(positions[child_input(1)], 50)
        self.assertEqual(len(set(positions.values())), 4)
        self.assertEqual([positions[key] for key in order], sorted(positions.values()))
        self.assertEqual(value, before)

    def test_routes_use_distinct_merge_ports_without_moving_change_objects(self):
        value = fallback_graph(build_graph(input_order_state(4, continuing=(1, 2, 3))))
        nodes = {node["id"]: node for node in value["nodes"]}
        edges = {edge["id"]: edge for edge in value["edges"]}
        aligned = {child_input(1), child_input(3)}
        target = nodes[edges[child_input(1)]["target"]]
        for key in aligned:
            nodes[edges[key]["source"]]["y"] = target["y"]
        original = {key: (node["x"], node["y"]) for key, node in nodes.items()}
        evidence = {key: {name: copy.deepcopy(edge.get(name)) for name in
                         ("id", "source", "target", "outpoint", "role", "label", "details")}
                    for key, edge in edges.items()}
        _routes(value, nodes, original, aligned)
        self.assertEqual(original, {key: (node["x"], node["y"]) for key, node in nodes.items()})
        positions = [edges[child_input(index)]["attachment"]["endItem"]["position"]["y"]
                     for index in (1, 2, 3, 0)]
        self.assertEqual(len(set(positions)), 4)
        self.assertEqual(len(edges[child_input(1)]["route"]), 2)
        self.assertEqual(edges[child_input(3)]["connector_shape"], "elbowed")
        for index in range(4):
            edge = edges[child_input(index)]
            self.assertEqual(edge["route"][-1], attachment_point(target, edge["attachment"]["endItem"]))
        self.assertEqual(evidence, {key: {name: copy.deepcopy(edge.get(name)) for name in row}
                                    for key, row in evidence.items() for edge in [edges[key]]})

    def test_all_continuation_inputs_still_keep_one_center_and_distinct_ports(self):
        value = fallback_graph(build_graph(input_order_state(3, continuing=(0, 1, 2))))
        self.assertEqual(input_orders(value), {})
        positions = centered_input_positions(value, {child_input(0), child_input(1)})
        self.assertEqual(len(positions), 3)
        self.assertEqual(len(set(positions.values())), 3)
        self.assertEqual(sum(position == 50 for position in positions.values()), 1)


if __name__ == "__main__":
    unittest.main()
