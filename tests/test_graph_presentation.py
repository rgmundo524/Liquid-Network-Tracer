import copy
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from liquid_tracer.api import Esplora, Limits
from liquid_tracer.common import LBTC, save_json
from liquid_tracer.export import (COLORS, PALETTE, PRESENTATION_VERSION,
                                  build_graph, edge_color, graph_quantity,
                                  short, svg_graph)
from liquid_tracer.miro import make_plan, validate_plan
from liquid_tracer.store import Store
from liquid_tracer.trace import new_state, trace
from tests.fixtures import A, B, C, X, fixture


class GraphPresentationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        store = Store(root / "case")
        self.addCleanup(store.close)
        fixture_file = root / "fixture.json"
        save_json(fixture_file, fixture())
        limits = Limits(max_hops=3)
        api = Esplora(store, "pending", limits, fixture=fixture_file, min_interval=0)
        state = new_state([A + ":0"], api.base, limits, [])
        api.run_id = state["run_id"]
        self.state = trace(api, state, limits, root / "run" / "trace.json")

    def test_missing_quantities_are_compact_and_public_values_remain_exact(self):
        hidden = {"valuecommitment": "08" + "ab" * 32,
                  "assetcommitment": "0a" + "cd" * 32}
        self.assertEqual(graph_quantity(hidden), "?? ??")
        self.assertEqual(graph_quantity({}), "?? ??")
        self.assertEqual(graph_quantity({"asset": LBTC}), "?? L-BTC")
        self.assertEqual(graph_quantity({"value": 0, "asset": LBTC}), "0 base units L-BTC")
        self.assertEqual(graph_quantity({"value": 9007199254740993}),
                         "9007199254740993 base units ??")
        other_asset = "12" * 32
        self.assertEqual(graph_quantity({"value": 42, "asset": other_asset}),
                         "42 base units " + short(other_asset))
        self.assertNotIn("L-BTC", graph_quantity(hidden))

    def test_address_and_input_captions_omit_prior_hash_but_keep_evidence(self):
        original = copy.deepcopy(self.state)
        graph = build_graph(self.state)
        nodes = {node["id"]: node for node in graph["nodes"]}
        funding = nodes["liquid:outpoint:" + X + ":0"]
        self.assertEqual(funding["label"], short("SYNTHETIC-funding-context"))
        self.assertEqual(funding["details"]["occurrences"][0]["outpoint"], X + ":0")
        for node in nodes.values():
            if node["kind"] == "address":
                self.assertEqual(node["label"], short(node["details"]["address"]))
        input_edge = next(edge for edge in graph["edges"] if edge["id"] == "in:" + A + ":0")
        self.assertEqual(input_edge["label"], "vin 0")
        self.assertEqual(input_edge["outpoint"], X + ":0")
        self.assertEqual(input_edge["details"]["vin"]["txid"], X)
        self.assertEqual(self.state, original)
        plan = make_plan(graph)
        visible = " ".join(shape["body"]["data"]["content"] for shape in plan["shapes"]
                           if not shape["key"].startswith("run:"))
        visible += " ".join(item["body"]["captions"][0]["content"] for item in plan["connectors"])
        self.assertNotIn("confidential", visible.lower())
        self.assertNotIn("unknown", visible.lower())
        self.assertIn("vin 0 · ?? ??", visible)

    def test_legends_and_renderers_share_actual_node_and_connector_palette(self):
        graph = build_graph(self.state)
        nodes = {node["id"]: node for node in graph["nodes"]}
        expected = {
            "tx:" + A: "starting_transaction",
            "tx:" + B: "transaction",
            "liquid:outpoint:" + A + ":0": "seed",
            "liquid:outpoint:" + A + ":1": "address",
            "liquid:outpoint:" + B + ":0": "candidate",
            "event:" + C + ":1": "event",
        }
        plan = make_plan(graph)
        shapes = {item["key"]: item["body"] for item in plan["shapes"]}
        svg = ET.fromstring(svg_graph(graph))
        svg_nodes = {element.attrib["data-key"]: element for element in svg.iter()
                     if "data-key" in element.attrib}
        for key, role in expected.items():
            self.assertEqual(nodes[key]["color"], COLORS[role])
            self.assertEqual(shapes[key]["style"]["fillColor"], COLORS[role])
            self.assertTrue(any(element.attrib.get("fill") == COLORS[role]
                                for element in svg_nodes[key]))
        connectors = {item["key"]: item["body"] for item in plan["connectors"]}
        for edge in graph["edges"]:
            expected_color = COLORS["context_edge" if edge["role"].startswith("context") else "traced_edge"]
            self.assertEqual(edge_color(edge["role"]), expected_color)
            self.assertEqual(connectors[edge["id"]]["style"]["strokeColor"], expected_color)
        svg_text = " ".join(svg.itertext())
        legend = shapes["legend"]["data"]["content"]
        for color_name, _ in PALETTE.values():
            self.assertIn(color_name.lower(), legend.lower())
            self.assertIn(color_name.lower(), svg_text.lower())
        self.assertIn("provided starting transactions", legend)
        self.assertIn("Starting role takes priority", legend)
        self.assertIn("selected seed outputs", legend)
        self.assertIn("amount asset", legend)
        self.assertIn("?? = not publicly available", legend)
        # The full legend fits above the existing first row; its extra color
        # descriptions must not cover the graph after this presentation update.
        svg_group = svg.find("{http://www.w3.org/2000/svg}g")
        header_text = svg_group.findall("{http://www.w3.org/2000/svg}text")
        header_bottom = max(float(text.attrib["y"]) + float(text.attrib["font-size"])
                            for text in header_text)
        first_node_top = min(node["y"] - node["height"] / 2 for node in graph["nodes"])
        self.assertLess(header_bottom, first_node_top)

    def test_provided_transaction_color_wins_over_descendant_and_hop_roles(self):
        state = copy.deepcopy(self.state)
        state["seeds"].extend([B + ":0", B + ":1"])
        # B spends the original A seed and is also explicitly provided. Its
        # recorded hop must not decide its color, even in an older saved run.
        state["transactions"][B]["depth"] = 7
        state["transactions"][C]["depth"] = 0
        original = copy.deepcopy(state)
        graph = build_graph(state)
        nodes = {node["id"]: node for node in graph["nodes"]}
        for key in (A, B):
            self.assertEqual(nodes["tx:" + key]["role"], "starting_transaction")
            self.assertEqual(nodes["tx:" + key]["color"], "#c4b5fd")
        self.assertEqual(nodes["tx:" + C]["color"], COLORS["transaction"])
        self.assertGreater(nodes["tx:" + B]["x"], nodes["tx:" + A]["x"])
        self.assertEqual(state, original)
        unselected = build_graph(self.state)
        self.assertEqual({node["id"] for node in graph["nodes"]},
                         {node["id"] for node in unselected["nodes"]})
        self.assertEqual({(edge["id"], edge["source"], edge["target"]) for edge in graph["edges"]},
                         {(edge["id"], edge["source"], edge["target"]) for edge in unselected["edges"]})

    def test_continuation_keeps_provided_transaction_colors_from_original_seeds(self):
        state = copy.deepcopy(self.state)
        state["seeds"].append(B + ":0")
        original = copy.deepcopy(state)
        continuation = new_state([], state["source"], Limits(max_hops=5), [], parent=state)
        self.assertEqual(continuation["parent_run"], state["run_id"])
        self.assertEqual(continuation["seeds"], state["seeds"])
        for merged in (False, True):
            nodes = {node["id"]: node for node in build_graph(continuation, merged)["nodes"]}
            for key in (A, B):
                self.assertEqual(nodes["tx:" + key]["color"], COLORS["starting_transaction"])
            self.assertEqual(nodes["tx:" + C]["color"], COLORS["transaction"])
        self.assertEqual(state, original)

    def test_svg_transaction_ports_stay_left_in_right_out_for_return_edges_and_fees(self):
        for arrangement in ("normal", "backward", "same_column"):
            with self.subTest(arrangement=arrangement):
                graph = build_graph(self.state, merge_addresses=True, include_fees=True)
                if arrangement == "backward":
                    for node in graph["nodes"]:
                        node["x"] = -node["x"]
                        node["y"] = 240
                elif arrangement == "same_column":
                    for node in graph["nodes"]:
                        node["x"] = 400
                nodes = {node["id"]: node for node in graph["nodes"]}
                svg = ET.fromstring(svg_graph(graph))
                paths = {group.attrib["data-edge-key"]:
                         group.find("{http://www.w3.org/2000/svg}path").attrib["d"]
                         for group in svg.iter() if "data-edge-key" in group.attrib}
                left, top, width, height = map(float, svg.attrib["viewBox"].split())
                for edge in graph["edges"]:
                    values = [float(value) for value in re.findall(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", paths[edge["id"]])]
                    points = list(zip(values[::2], values[1::2]))
                    start, end = nodes[edge["source"]], nodes[edge["target"]]
                    if start["kind"] == "transaction":
                        self.assertEqual(points[0], (start["x"] + start["width"] / 2, start["y"]))
                        self.assertGreater(points[1][0], points[0][0])
                        self.assertEqual(points[1][1], points[0][1])
                    if end["kind"] == "transaction":
                        self.assertEqual(points[-1], (end["x"] - end["width"] / 2, end["y"]))
                        self.assertLess(points[-2][0], points[-1][0])
                        self.assertEqual(points[-2][1], points[-1][1])
                    # Return-edge detours must remain visible in the preview.
                    for x, y in points:
                        self.assertTrue(left <= x <= left + width)
                        self.assertTrue(top <= y <= top + height)
                    if arrangement == "normal":
                        # Fixed endpoints alone are insufficient: a wide
                        # return curve can still cross its own transaction.
                        # Check the visible merged-address and fee routes.
                        for node in (start, end):
                            if node["kind"] != "transaction":
                                continue
                            for segment in range(1, len(points), 3):
                                controls = points[segment - 1:segment + 3]
                                for step in range(1, 20):
                                    t = step / 20
                                    weights = ((1 - t) ** 3, 3 * (1 - t) ** 2 * t,
                                               3 * (1 - t) * t ** 2, t ** 3)
                                    x, y = [sum(weight * point[axis] for weight, point in zip(weights, controls))
                                            for axis in (0, 1)]
                                    self.assertFalse(abs(x - node["x"]) < node["width"] / 2 - .01
                                                     and abs(y - node["y"]) < node["height"] / 2 - .01,
                                                     edge["id"] + " crosses its transaction")
                if arrangement == "backward":
                    self.assertTrue(any(path.count("C ") == 3 for path in paths.values()))

    def test_merged_address_role_priority_is_independent_of_visit_order(self):
        shared = "SYNTHETIC-reused-address"
        state = copy.deepcopy(self.state)
        for record in state["transactions"].values():
            for output in record["data"]["vout"]:
                if output.get("scriptpubkey_address"):
                    output["scriptpubkey_address"] = shared
            for vin in record["data"]["vin"]:
                if vin.get("prevout", {}).get("scriptpubkey_address"):
                    vin["prevout"]["scriptpubkey_address"] = shared
        key = "liquid:address:" + shared
        label = {"kind": "outpoint", "value": B + ":0", "entity": "Synthetic service",
                 "confidence": "candidate", "source": "fixture://label", "observed_at": "2026-01-01"}
        for labels, role in (([], "seed"), ([label], "attributed")):
            state["labels"] = labels
            variants = [state, copy.deepcopy(state)]
            for record in variants[1]["transactions"].values():
                record["depth"] = 10 - record["depth"]
            for variant in variants:
                merged = next(node for node in build_graph(variant, True)["nodes"] if node["id"] == key)
                self.assertEqual(merged["role"], role)
                self.assertEqual(merged["color"], COLORS[role])
                self.assertIn(X + ":0", {item["outpoint"] for item in merged["details"]["occurrences"]})
                self.assertIn(A + ":0", {item["outpoint"] for item in merged["details"]["occurrences"]})
                if labels:
                    self.assertEqual(merged["label"], short(shared) + "\nSynthetic service (candidate)")
                    shape = next(item for item in make_plan(build_graph(variant, True))["shapes"]
                                 if item["key"] == key)
                    self.assertEqual(shape["body"]["style"]["fillColor"], COLORS["attributed"])

    def test_bitcoin_input_does_not_inherit_liquid_seed_or_candidate_role(self):
        state = copy.deepcopy(self.state)
        vin = state["transactions"][B]["data"]["vin"][0]
        vin["is_pegin"] = True
        graph = build_graph(state)
        node = next(node for node in graph["nodes"] if node["id"] == "bitcoin:outpoint:" + A + ":0")
        self.assertEqual(node["color"], COLORS["address"])
        self.assertIsNone(node["details"]["occurrences"][0]["trace"])
        edge = next(edge for edge in graph["edges"] if edge["id"] == "in:" + B + ":0")
        self.assertEqual(edge["role"], "context_input")

    def test_presentation_version_keeps_identity_namespace_and_plan_integrity(self):
        graph = build_graph(self.state)
        plan = make_plan(graph)
        validate_plan(plan)
        self.assertEqual(graph["presentation_version"], PRESENTATION_VERSION)
        self.assertEqual(plan["presentation_version"], PRESENTATION_VERSION)
        self.assertEqual(plan["namespace"], graph["namespace"])
        self.assertEqual({item["key"] for item in plan["shapes"]},
                         {node["id"] for node in graph["nodes"]} | {"legend", "run:" + graph["run_id"]})
        self.assertEqual({(item["key"], item["source"], item["target"]) for item in plan["connectors"]},
                         {(edge["id"], edge["source"], edge["target"]) for edge in graph["edges"]})

    def test_run_note_summarizes_many_seeds_without_losing_full_plan_metadata(self):
        graph = build_graph(self.state)
        seeds = [f"{index:064x}:0" for index in range(10)] + [f"{0:064x}:1"]
        graph["run"]["seeds"] = seeds
        plan = make_plan(graph)
        content = next(item["body"]["data"]["content"] for item in plan["shapes"]
                       if item["key"].startswith("run:"))
        self.assertIn("Starting outputs: 11 across 10 transactions", content)
        self.assertNotIn(seeds[0], content)
        self.assertEqual(plan["run"]["seeds"], seeds)


if __name__ == "__main__":
    unittest.main()
