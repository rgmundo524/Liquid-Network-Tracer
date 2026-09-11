import copy
import json
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.export import COLORS
from liquid_tracer.layout_preview import LAYOUT_NOTICE, export_layout, render_svg


NS = {"s": "http://www.w3.org/2000/svg"}


def graph_fixture():
    return {
        "run_id": "synthetic-layout-run", "simulated": True, "include_fees": True,
        "notice": "UTXO reachability; ?? means the public value is unavailable.",
        "layout": {"algorithm": "elk-layered", "metrics": {
            "before": {"crossings": 3, "node_overlaps": 1, "node_intersections": 2},
            "after": {"crossings": 0, "node_overlaps": 0, "node_intersections": 0},
            "estimated": True}},
        "nodes": [
            {"id": "tx:synthetic", "kind": "transaction", "label": "TX\nsynthetic-start\n2023-11-14 UTC\nhop 0",
             "x": 200, "y": 200, "width": 160, "height": 160, "color": COLORS["starting_transaction"]},
            {"id": "address:synthetic", "kind": "address", "label": "Synthetic recipient",
             "x": 560, "y": 200, "width": 160, "height": 160, "color": COLORS["seed"]},
            {"id": "event:synthetic", "kind": "event", "label": "FEE\nvout 1\n2023-11-14 UTC",
             "x": 200, "y": -100, "width": 160, "height": 160, "color": COLORS["event"]}],
        "edges": [
            {"id": "out:synthetic:0", "source": "tx:synthetic", "target": "address:synthetic",
             "role": "seed_output", "label": "vout 0", "quantity": "?? ??", "connector_shape": "straight",
             "attachment": {"startItem": {"position": {"x": "100%", "y": "50%"}},
                            "endItem": {"position": {"x": "0%", "y": "50%"}}},
             "route": [{"x": 280, "y": 200}, {"x": 300, "y": 400}, {"x": 480, "y": 200}]},
            {"id": "out:synthetic:1", "source": "tx:synthetic", "target": "event:synthetic",
             "role": "context_output", "label": "vout 1", "quantity": "10 base units L-BTC", "connector_shape": "elbowed",
             "attachment": {"startItem": {"position": {"x": "100%", "y": "25%"}},
                            "endItem": {"position": {"x": "50%", "y": "100%"}}},
             "route": [{"x": 280, "y": 160}, {"x": 350, "y": 160}, {"x": 350, "y": -10},
                       {"x": 200, "y": -10}, {"x": 200, "y": -20}]}]}


class LayoutPreviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.destination = self.root / "nested" / "preview"

    def test_render_preserves_all_physical_edges_roles_dates_and_input_graph(self):
        graph = graph_fixture()
        before = copy.deepcopy(graph)
        result = export_layout(graph, self.destination)
        self.assertEqual(graph, before)
        saved = json.loads(Path(result["graph"]).read_text())
        self.assertEqual(saved, graph)
        svg = ET.fromstring(Path(result["svg"]).read_bytes())
        nodes = svg.findall(".//s:g[@data-node-id]", NS)
        paths = svg.findall(".//s:path[@data-edge-id]", NS)
        self.assertEqual({item.get("data-node-id") for item in nodes}, {item["id"] for item in graph["nodes"]})
        self.assertEqual({item.get("data-edge-id") for item in paths}, {item["id"] for item in graph["edges"]})
        for path in paths:
            original = next(edge for edge in graph["edges"] if edge["id"] == path.get("data-edge-id"))
            self.assertEqual(path.get("data-source"), original["source"])
            self.assertEqual(path.get("data-target"), original["target"])
        self.assertIsNotNone(svg.find(".//s:ellipse", NS))
        self.assertIsNotNone(svg.find(".//s:polygon", NS))
        self.assertIsNotNone(svg.find(f".//s:rect[@fill='{COLORS['starting_transaction']}']", NS))
        text = " ".join(svg.itertext())
        self.assertIn("2023-11-14 UTC", text)
        self.assertIn("vout 0 · ?? ??", text)
        self.assertIn(LAYOUT_NOTICE, text)
        self.assertEqual(result["layout_metrics"], graph["layout"]["metrics"])

    def test_straight_lines_use_ports_and_route_exceptions_keep_bends(self):
        result = export_layout(graph_fixture(), self.destination)
        svg = ET.fromstring(Path(result["svg"]).read_bytes())
        paths = {item.get("data-edge-id"): item.get("d") for item in svg.findall(".//s:path[@data-edge-id]", NS)}
        self.assertEqual(paths["out:synthetic:0"], "M 280 200 L 480 200")
        self.assertEqual(paths["out:synthetic:1"], "M 280 160 L 350 160 L 350 -10 L 200 -10 L 200 -20")

    def test_cycle_and_parallel_edges_remain_separate_paths(self):
        graph = graph_fixture()
        duplicate = copy.deepcopy(graph["edges"][0])
        duplicate["id"] = "parallel:synthetic"
        graph["edges"].append(duplicate)
        graph["edges"].append({"id": "return:synthetic", "source": "address:synthetic", "target": "tx:synthetic",
                               "role": "traced_input", "label": "vin 0", "connector_shape": "curved",
                               "attachment": {"startItem": {"position": {"x": "100%", "y": "50%"}},
                                              "endItem": {"position": {"x": "0%", "y": "50%"}}},
                               "route": [{"x": 640, "y": 200}, {"x": 700, "y": 200}, {"x": 700, "y": 400},
                                         {"x": 100, "y": 400}, {"x": 100, "y": 200}, {"x": 120, "y": 200}]})
        result = export_layout(graph, self.destination)
        svg = ET.fromstring(Path(result["svg"]).read_bytes())
        paths = svg.findall(".//s:path[@data-edge-id]", NS)
        self.assertEqual(len(paths), 4)
        back = next(path for path in paths if path.get("data-edge-id") == "return:synthetic")
        self.assertTrue(back.get("d").startswith("M 640 200"))
        self.assertTrue(back.get("d").endswith("L 120 200"))
        self.assertIn(" Q ", back.get("d"))

    def test_html_is_self_contained_with_readable_metrics_and_safe_text(self):
        graph = graph_fixture()
        hostile = '<script>alert("x")</script><img src="https://example.invalid/a"> & \x00'
        graph["notice"] = hostile
        graph["run_id"] = hostile
        graph["nodes"][0]["label"] = hostile
        graph["edges"][0]["label"] = hostile
        graph["nodes"][0]["id"] = 'tx:hostile" onload="alert(1)'
        for edge in graph["edges"]:
            edge["source"] = graph["nodes"][0]["id"]
        result = export_layout(graph, self.destination)
        page = Path(result["html"]).read_text()
        self.assertNotIn("<script", page)
        self.assertNotIn('<img src="https:', page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("<th>Baseline layout</th><th>ELK layout</th>", page)
        self.assertIn('<main id="chart"', page)
        self.assertIn('body:has(#chart:target) header { display:none; }', page)
        self.assertIn('#chart:target svg { width:100%; height:auto; }', page)
        self.assertIn("Line crossings</th><td>3</td><td>0</td>", page)
        inline = re.search(r'<main id="chart" class="chart">(.*?)</main>', page, re.S).group(1)
        self.assertEqual(inline.encode(), Path(result["svg"]).read_bytes())
        svg = ET.fromstring(inline)
        self.assertIsNone(svg.find(".//s:script", NS))
        self.assertIsNone(svg.find(".//s:image", NS))
        self.assertFalse(any("onload" in item.attrib for item in svg.iter()))
        self.assertEqual(json.loads(Path(result["graph"]).read_text())["notice"], hostile)

    def test_explorer_links_are_active_inline_and_in_downloaded_svg(self):
        for network in ("liquid", "liquidtestnet"):
            with self.subTest(network=network):
                graph = graph_fixture()
                graph["simulated"] = False
                graph["nodes"][0]["url"] = f"https://blockstream.info/{network}/tx/" + "a" * 64
                graph["nodes"][1]["url"] = f"https://blockstream.info/{network}/address/ex1syntheticaddress"
                result = export_layout(graph, self.root / network)
                page = Path(result["html"]).read_text()
                standalone = Path(result["svg"]).read_text()
                inline = re.search(r'<main id="chart" class="chart">(.*?)</main>', page, re.S).group(1)
                self.assertEqual(inline, standalone)
                self.assertNotIn("<img", page)
                self.assertNotIn("<object", page)
                self.assertNotIn("<script", page)
                svg = ET.fromstring(standalone)
                anchors = svg.findall(".//s:a", NS)
                self.assertEqual([item.get("href") for item in anchors],
                                 [graph["nodes"][1]["url"], graph["nodes"][0]["url"]])
                for anchor in anchors:
                    self.assertEqual(anchor.get("target"), "_blank")
                    self.assertEqual(anchor.get("rel"), "noopener noreferrer")
                    self.assertEqual(anchor.get("referrerpolicy"), "no-referrer")
                    self.assertEqual(anchor.get("tabindex"), "0")
                    self.assertIn("Open Blockstream explorer in a new tab", anchor.get("aria-label"))
                    self.assertEqual(len(anchor.findall(".//s:text[.='Explorer']", NS)), 1)
                    self.assertIsNotNone(anchor.find("./s:g[@data-node-id]", NS))

    def test_explorer_links_reject_unsafe_urls_and_do_not_link_simulated_events(self):
        valid_tx = "https://blockstream.info/liquid/tx/" + "a" * 64
        invalid = [None, 3, {}, "", "javascript:alert(1)", "data:text/html,<script>alert(1)</script>",
                   "//blockstream.info/liquid/tx/" + "a" * 64,
                   valid_tx.replace("https:", "http:"),
                   valid_tx.replace("blockstream.info", "blockstream.info.evil.invalid"),
                   valid_tx.replace("blockstream.info", "blockstream.info@evil.invalid"),
                   valid_tx.replace("blockstream.info", "secret@blockstream.info"),
                   valid_tx.replace("blockstream.info", "blockstream.info:443"),
                   valid_tx + "?token=secret", valid_tx + "#fragment", valid_tx + "\n",
                   valid_tx + '\" onload="alert(1)', valid_tx.replace("/tx/", "/address/../tx/"),
                   valid_tx.replace("/tx/", "/%74x/"), valid_tx.replace("/liquid/", "/bitcoin/"),
                   valid_tx[:-1], "https://blockstream.info/liquid/address/ex1address"]
        for url in invalid:
            with self.subTest(url=url):
                graph = graph_fixture()
                graph["simulated"] = False
                graph["nodes"][0]["url"] = url
                svg = ET.fromstring(render_svg(graph))
                self.assertEqual(svg.findall(".//s:a", NS), [])
        graph = graph_fixture()
        for node in graph["nodes"]:
            node["url"] = valid_tx
        self.assertEqual(ET.fromstring(render_svg(graph)).findall(".//s:a", NS), [])
        graph["simulated"] = False
        graph["nodes"][0].pop("url")
        # Event URLs and mismatched address/transaction routes are inert.
        self.assertEqual(ET.fromstring(render_svg(graph)).findall(".//s:a", NS), [])

    def test_link_labels_stay_within_shapes_without_changing_geometry(self):
        graph = graph_fixture()
        graph["simulated"] = False
        baseline = ET.fromstring(render_svg(graph))
        for node in graph["nodes"][:2]:
            node["label"] = "\n".join("A long label " + str(index) for index in range(12))
        graph["nodes"][0]["url"] = "https://blockstream.info/liquid/tx/" + "a" * 64
        graph["nodes"][1]["url"] = "https://blockstream.info/liquid/address/ex1syntheticaddress"
        before = copy.deepcopy(graph)
        svg = ET.fromstring(render_svg(graph))
        self.assertEqual(graph, before)
        self.assertEqual(svg.attrib, baseline.attrib)
        for kind in ("rect", "ellipse", "polygon"):
            original_shapes = baseline.findall(f".//s:g[@data-node-id]/s:{kind}", NS)
            linked_shapes = svg.findall(f".//s:g[@data-node-id]/s:{kind}", NS)
            self.assertEqual([item.attrib for item in linked_shapes], [item.attrib for item in original_shapes])
        self.assertEqual([item.attrib for item in svg.findall(".//s:path[@data-edge-id]", NS)],
                         [item.attrib for item in baseline.findall(".//s:path[@data-edge-id]", NS)])
        for anchor in svg.findall(".//s:a", NS):
            clip = anchor.find(".//s:clipPath/s:rect", NS)
            minimum = float(clip.get("y"))
            maximum = minimum + float(clip.get("height"))
            for label in anchor.findall(".//s:text", NS):
                # Twelve-pixel labels fit between their ascent and descender.
                self.assertGreaterEqual(float(label.get("y")) - 12, minimum)
                self.assertLessEqual(float(label.get("y")) + 3, maximum)

    def test_tiny_nodes_keep_clickable_anchor_without_overflowing_explorer_label(self):
        graph = graph_fixture()
        graph["simulated"] = False
        graph["nodes"][0].update(width=20, height=20,
                                  url="https://blockstream.info/liquid/tx/" + "a" * 64)
        svg = ET.fromstring(render_svg(graph))
        anchor = svg.find(".//s:a", NS)
        self.assertIsNotNone(anchor)
        self.assertEqual(anchor.findall(".//s:text[.='Explorer']", NS), [])
        self.assertIn("Open Blockstream explorer", anchor.find(".//s:title", NS).text)

    def test_rejects_invalid_geometry_and_identifiers_before_creating_directory(self):
        variants = []
        for field, value in (("x", float("nan")), ("y", float("inf")), ("width", 0), ("height", -2),
                             ("x", True), ("x", 10 ** 1000), ("color", 'red" onload="x')):
            graph = graph_fixture()
            graph["nodes"][0][field] = value
            variants.append(graph)
        graph = graph_fixture()
        graph["nodes"].append(copy.deepcopy(graph["nodes"][0]))
        variants.append(graph)
        graph = graph_fixture()
        graph["edges"][0]["target"] = "missing"
        variants.append(graph)
        graph = graph_fixture()
        graph["edges"][0]["attachment"]["startItem"]["position"]["x"] = "101%"
        variants.append(graph)
        graph = graph_fixture()
        graph["edges"][0]["route"][0]["y"] = float("inf")
        variants.append(graph)
        graph = graph_fixture()
        graph["edges"].append(copy.deepcopy(graph["edges"][0]))
        variants.append(graph)
        for index, graph in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(TraceError):
                export_layout(graph, self.destination)
            self.assertFalse(self.destination.exists())

    def test_existing_directory_and_symlink_cannot_overwrite_artifacts(self):
        self.destination.mkdir(parents=True)
        sentinel = self.destination / "keep.txt"
        sentinel.write_text("existing evidence")
        with self.assertRaisesRegex(TraceError, "already exists"):
            export_layout(graph_fixture(), self.destination)
        self.assertEqual(sentinel.read_text(), "existing evidence")
        link = self.root / "alias"
        link.symlink_to(self.destination, target_is_directory=True)
        with self.assertRaisesRegex(TraceError, "symbolic links"):
            export_layout(graph_fixture(), link / "child")
        self.assertFalse((self.destination / "child").exists())

    def test_finite_coordinates_above_previous_limit_remain_renderable(self):
        graph = graph_fixture()
        graph["nodes"][1]["x"] = 10 ** 200
        graph["edges"][0].pop("attachment")
        # The old squared ellipse-distance formula overflowed even though the
        # coordinates and the completed SVG geometry are all finite.
        svg = ET.fromstring(render_svg(graph))
        self.assertEqual(len(svg.findall(".//s:g[@data-node-id]", NS)), 3)
        self.assertEqual(len(svg.findall(".//s:path[@data-edge-id]", NS)), 2)
        self.assertNotRegex(str(svg.attrib), r"(?i)nan|inf")

    def test_arithmetic_overflow_cannot_emit_invalid_svg(self):
        for mutation in (
                lambda graph: graph["nodes"][0].update(x=1.7e308, width=1.7e308),
                lambda graph: (graph["nodes"][0].update(x=-1.7e308),
                               graph["nodes"][1].update(x=1.7e308)),
                lambda graph: graph["edges"][1].update(route=[{"x": 1.7e308, "y": 0},
                    {"x": -1.7e308, "y": 0}, {"x": 1.7e308, "y": 0},
                    {"x": 0, "y": 0}], connector_shape="curved")):
            graph = graph_fixture()
            mutation(graph)
            with self.subTest(graph=graph), self.assertRaisesRegex(TraceError, "geometry"):
                export_layout(graph, self.destination)
            self.assertFalse(self.destination.exists())

    def test_tiny_direction_with_large_finite_shapes_does_not_underflow(self):
        graph = graph_fixture()
        graph["edges"] = [graph["edges"][0]]
        graph["edges"][0].pop("attachment")
        graph["nodes"] = graph["nodes"][:2]
        for index, node in enumerate(graph["nodes"]):
            node.update(kind="address", x=index * 1e-300, y=0, width=1e300, height=1e300)
        svg = ET.fromstring(render_svg(graph))
        self.assertEqual(len(svg.findall(".//s:path[@data-edge-id]", NS)), 1)

    def test_truncated_metrics_are_displayed_as_lower_bounds(self):
        graph = graph_fixture()
        graph["layout"]["metrics"]["before"]["truncated"] = True
        result = export_layout(graph, self.destination)
        page = Path(result["html"]).read_text()
        self.assertIn("Line crossings</th><td>≥ 3</td><td>0</td>", page)
        self.assertIn("Object overlaps</th><td>≥ 1</td><td>0</td>", page)
        self.assertIn("lower bounds", page)

    def test_failure_retains_diagnostics_without_publishing_completed_html(self):
        original = Path.write_bytes

        def fail_svg(path, data):
            if path.name == "graph.svg":
                raise OSError("synthetic failure")
            return original(path, data)

        with patch.object(Path, "write_bytes", fail_svg), self.assertRaisesRegex(TraceError, "Cannot finish"):
            export_layout(graph_fixture(), self.destination)
        self.assertTrue((self.destination / "graph.json").exists())
        self.assertTrue((self.destination / "layout-report.json").exists())
        self.assertFalse((self.destination / "graph.html").exists())

    def test_completion_marker_is_published_last(self):
        original = Path.replace
        observed = []

        def capture_replace(path, target):
            if Path(target).name == "graph.html":
                observed.append(all((self.destination / name).is_file() for name in
                                    ("graph.svg", "graph.json", "layout-report.json")))
            return original(path, target)

        with patch.object(Path, "replace", capture_replace):
            export_layout(graph_fixture(), self.destination)
        self.assertEqual(observed, [True])


if __name__ == "__main__":
    unittest.main()
