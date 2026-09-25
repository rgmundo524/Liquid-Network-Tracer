"""Whole L-BTC captions preserve exact underlying UTXO evidence."""
import copy
import unittest
import xml.etree.ElementTree as ET

from liquid_tracer.common import LBTC
from liquid_tracer.export import build_graph, graph_quantity, svg_graph
from liquid_tracer.mermaid import mermaid_source
from liquid_tracer.miro import make_plan, validate_plan
from liquid_tracer.transaction_csv import transaction_csv_rows
from tests.fixtures import A, B, C, D
from tests.test_transaction_csv import find, state_fixture


class LbtcDisplayTests(unittest.TestCase):
    def test_explicit_lbtc_uses_exact_eight_decimal_conversion(self):
        cases = (
            (0, "0 L-BTC"),
            (1, "0.00000001 L-BTC"),
            (10_000_000, "0.1 L-BTC"),
            (100_000_000, "1 L-BTC"),
            (123_456_789, "1.23456789 L-BTC"),
            (2 ** 53 + 1, "90071992.54740993 L-BTC"),
            (2 ** 63 - 1, "92233720368.54775807 L-BTC"),
        )
        for asset in (LBTC, LBTC.upper()):
            for value, expected in cases:
                with self.subTest(value=value, asset=asset):
                    self.assertEqual(graph_quantity({"value": value, "asset": asset}), expected)

    def test_amount_and_asset_confidentiality_remain_independent(self):
        hidden = {"valuecommitment": "08" + "ab" * 32,
                  "assetcommitment": "0a" + "cd" * 32}
        self.assertEqual(graph_quantity(hidden), "?? ??")
        self.assertEqual(graph_quantity({}), "?? ??")
        self.assertEqual(graph_quantity({**hidden, "asset": LBTC}), "?? L-BTC")
        self.assertEqual(graph_quantity({**hidden, "value": 100_000_000}),
                         "100000000 base units ??")
        self.assertEqual(graph_quantity({"value": 2 ** 53 + 1}),
                         "9007199254740993 base units ??")
        self.assertEqual(graph_quantity({"value": 100_000_000, "asset": "ab" * 32}),
                         "100000000 base units ababababab…bababab")

    def test_input_output_fee_and_pegout_captions_reach_miro_and_previews(self):
        state = state_fixture()
        original = copy.deepcopy(state)
        graph = build_graph(state, include_fees=True)
        before_render = copy.deepcopy(graph)
        quantities = {
            f"out:{A}:0": "0.01 L-BTC",
            f"in:{B}:0": "0.01 L-BTC",
            f"out:{A}:2": "0.000001 L-BTC",
            f"out:{C}:1": "0 L-BTC",
            f"out:{D}:0": "0.00001234 L-BTC",
            f"out:{B}:0": "?? ??",
        }
        edges = {edge["id"]: edge for edge in graph["edges"]}
        plan = make_plan(graph)
        validate_plan(plan)
        connectors = {item["key"]: item for item in plan["connectors"]}
        source = mermaid_source(graph)
        svg = ET.fromstring(svg_graph(graph))
        ns = {"s": "http://www.w3.org/2000/svg"}
        titles = {item.get("data-edge-key"): item.find("s:title", ns).text
                  for item in svg.findall(".//s:g[@data-edge-key]", ns)}
        for key, quantity in quantities.items():
            with self.subTest(edge=key):
                edge = edges[key]
                self.assertEqual(edge["quantity"], quantity)
                caption = edge["label"] + " · " + quantity
                self.assertEqual(connectors[key]["body"]["captions"][0]["content"], caption)
                self.assertIn('"' + caption + '"', source)
                self.assertEqual(titles[key], edge["outpoint"] + " | " + quantity)
        self.assertEqual(edges[f"out:{A}:0"]["details"]["value"], 1_000_000)
        self.assertEqual(edges[f"in:{B}:0"]["details"]["vin"]["prevout"]["value"], 1_000_000)
        self.assertEqual(state, original)
        self.assertEqual(graph, before_render)
        # Rebuilding from the same evidence must never divide a display amount again.
        self.assertEqual(build_graph(state, include_fees=True), graph)

    def test_whole_lbtc_captions_do_not_change_transaction_csv_base_units(self):
        state = state_fixture()
        value = 2 ** 53 + 1
        state["transactions"][D]["data"]["vout"][0]["value"] = value
        original = copy.deepcopy(state)
        graph = build_graph(state, include_fees=True)
        before_export = copy.deepcopy(graph)
        rows = transaction_csv_rows(graph, state)
        for txid, direction in ((A, "OUT"), (B, "IN")):
            row = find(rows, txid, direction, 0)
            self.assertEqual((row["Asset Value"], row["Asset"]), (1_000_000, "L-BTC"))
        pegout = find(rows, D, "OUT", 0)
        self.assertEqual((pegout["Asset Value"], pegout["PegOut Value"]), (value, value))
        edge = next(edge for edge in graph["edges"] if edge["id"] == f"out:{D}:0")
        self.assertEqual(edge["quantity"], "90071992.54740993 L-BTC")
        self.assertEqual(edge["details"]["value"], value)
        self.assertEqual(find(rows, B, "OUT", 0)["Asset Value"], "")
        self.assertEqual(find(rows, C, "OUT", 1)["Asset Value"], 0)
        self.assertEqual(state, original)
        self.assertEqual(graph, before_export)


if __name__ == "__main__":
    unittest.main()
