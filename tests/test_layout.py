import copy
import csv
import hashlib
import random
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from liquid_tracer.api import Esplora, Limits
from liquid_tracer.common import LBTC, output_kind, read_json, save_json
from liquid_tracer.export import build_graph, export_run, svg_graph
from liquid_tracer.layout import COLUMN_GAP, NODE_SIZE, ROW_GAP, transaction_ranks
from liquid_tracer.store import Store
from liquid_tracer.trace import new_state, trace
from tests.fixtures import A, B, C, D, fixture, output


def txid(name):
    return hashlib.sha256(("SYNTHETIC-layout-" + str(name)).encode()).hexdigest()


def state_from(transactions):
    return {"source": "fixture://synthetic-layout", "run_id": "synthetic-layout",
            "case_id": "synthetic-layout-case", "seeds": [], "outputs": {},
            "links": {}, "labels": [], "transactions": {
                key: {"data": copy.deepcopy(data), "depth": 0, "observation_id": key}
                for key, data in transactions.items()}}


def chain(length):
    transactions = {}
    for index in range(length):
        key = txid(index)
        prev = txid(index - 1)
        previous_output = output("SYNTHETIC-reused-address")
        transactions[key] = {"txid": key,
            "vin": [{"txid": prev, "vout": 0, "prevout": previous_output}],
            "vout": [copy.deepcopy(previous_output)], "status": {}}
    return transactions


class LayoutTests(unittest.TestCase):
    def assert_no_overlap(self, graph):
        nodes = graph["nodes"]
        for index, first in enumerate(nodes):
            for second in nodes[index + 1:]:
                with self.subTest(first=first["id"], second=second["id"]):
                    self.assertTrue(abs(first["x"] - second["x"]) >= NODE_SIZE
                                    or abs(first["y"] - second["y"]) >= NODE_SIZE)

    def assert_forward(self, graph):
        nodes = {node["id"]: node for node in graph["nodes"]}
        for edge in graph["edges"]:
            with self.subTest(edge=edge["id"]):
                self.assertLess(nodes[edge["source"]]["x"], nodes[edge["target"]]["x"])

    def test_ten_related_starting_transactions_move_forward_despite_all_hop_zero(self):
        state = state_from(chain(10))
        state["seeds"] = [key + ":0" for key in state["transactions"]]
        graph = build_graph(state)
        self.assert_forward(graph)
        self.assert_no_overlap(graph)
        nodes = {node["id"]: node for node in graph["nodes"]}
        for index in range(9):
            self.assertEqual(nodes["tx:" + txid(index + 1)]["x"] - nodes["tx:" + txid(index)]["x"], 2 * COLUMN_GAP)
        self.assertEqual(len({node["y"] for node in graph["nodes"]}), 1)
        self.assertEqual(len([node for node in graph["nodes"] if node["kind"] == "address"]), 11)

    def test_split_join_and_reused_address_have_local_neighbors_without_overlap(self):
        state = state_from({data["txid"]: data for key, data in fixture().items()
                            if not key.endswith("outspends")})
        graph = build_graph(state)
        self.assert_forward(graph)
        self.assert_no_overlap(graph)
        nodes = {node["id"]: node for node in graph["nodes"]}
        for key in ("liquid:outpoint:" + B + ":0", "liquid:outpoint:" + B + ":1"):
            self.assertLessEqual(abs(nodes[key]["y"] - nodes["tx:" + C]["y"]), 2 * ROW_GAP)
        self.assertNotEqual(nodes["liquid:outpoint:" + B + ":0"]["x"],
                            nodes["liquid:outpoint:" + C + ":0"]["x"])
        self.assertIn("event:" + C + ":1", nodes)
        self.assertIn("event:" + D + ":0", nodes)

    def test_disconnected_branches_stay_together_in_separate_compact_lanes(self):
        transactions = {}
        for index in range(10):
            key = txid(index)
            transactions[key] = {"txid": key, "vin": [{"txid": txid("parent" + str(index)),
                "vout": 0, "prevout": output("SYNTHETIC-input-" + str(index))}],
                "vout": [output("SYNTHETIC-output-" + str(index))], "status": {}}
        graph = build_graph(state_from(transactions))
        self.assert_forward(graph)
        self.assert_no_overlap(graph)
        nodes = {node["id"]: node for node in graph["nodes"]}
        for edge in graph["edges"]:
            self.assertEqual(nodes[edge["source"]]["y"], nodes[edge["target"]]["y"])
        self.assertEqual(len({node["y"] for node in graph["nodes"]}), 10)

    def test_many_starting_fanouts_keep_siblings_together_before_a_shared_join(self):
        transactions = {}
        for index in range(10):
            key = txid(index)
            transactions[key] = {"txid": key, "vin": [{"txid": txid("source-" + str(index)),
                "vout": 0, "prevout": output("SYNTHETIC-input-" + str(index))}],
                "vout": [output(f"SYNTHETIC-root-{index}-{out}") for out in range(3)], "status": {}}
            child = txid("next-" + str(index))
            transactions[child] = {"txid": child, "vin": [{"txid": key, "vout": 0,
                "prevout": transactions[key]["vout"][0]}],
                "vout": [output(f"SYNTHETIC-child-{index}-{out}") for out in range(3)], "status": {}}
        joined = txid("join")
        transactions[joined] = {"txid": joined, "vin": [{"txid": txid("next-" + str(index)),
            "vout": 0, "prevout": transactions[txid("next-" + str(index))]["vout"][0]}
            for index in range(10)], "vout": [output("SYNTHETIC-joined")], "status": {}}
        graph = build_graph(state_from(transactions))
        self.assert_forward(graph)
        self.assert_no_overlap(graph)
        nodes = {node["id"]: node for node in graph["nodes"]}
        for key, data in transactions.items():
            siblings = [nodes[f"liquid:outpoint:{key}:{index}"] for index in range(len(data["vout"]))]
            self.assertEqual(max(node["y"] for node in siblings) - min(node["y"] for node in siblings),
                             (len(siblings) - 1) * ROW_GAP)
            self.assertLessEqual(max(abs(node["y"] - nodes["tx:" + key]["y"]) for node in siblings), ROW_GAP)
        for index in range(10):
            self.assertLessEqual(abs(nodes["tx:" + txid(index)]["y"]
                                     - nodes["tx:" + txid("next-" + str(index))]["y"]), ROW_GAP)

    def test_layout_is_deterministic_under_transaction_and_seed_order(self):
        state = state_from(chain(10))
        state["seeds"] = [key + ":0" for key in state["transactions"]]
        first = build_graph(state)
        shuffled = copy.deepcopy(state)
        items = list(shuffled["transactions"].items())
        random.Random(12).shuffle(items)
        shuffled["transactions"] = dict(items)
        shuffled["seeds"].reverse()
        second = build_graph(shuffled)
        self.assertEqual(first["nodes"], second["nodes"])
        self.assertEqual(first["edges"], second["edges"])
        self.assertEqual(first["layout"], second["layout"])

    def test_merged_reuse_draws_return_edges_but_transaction_order_remains_forward(self):
        graph = build_graph(state_from(chain(10)), merge_addresses=True)
        nodes = {node["id"]: node for node in graph["nodes"]}
        self.assertEqual(sum(node["kind"] == "address" for node in graph["nodes"]), 1)
        self.assert_no_overlap(graph)
        self.assertTrue(any(nodes[edge["source"]]["x"] >= nodes[edge["target"]]["x"] for edge in graph["edges"]))
        self.assertEqual([nodes["tx:" + txid(index)]["x"] for index in range(10)],
                         sorted(nodes["tx:" + txid(index)]["x"] for index in range(10)))

    def test_transaction_dependency_cycles_terminate_with_deterministic_fallback(self):
        transactions = chain(3)
        transactions[txid(0)]["vin"][0]["txid"] = txid(2)
        state = state_from(transactions)
        graph = build_graph(state)
        self.assertEqual(graph["layout"]["cycle_groups"], [sorted(transactions)])
        self.assert_no_overlap(graph)
        self.assertEqual(graph, build_graph(state))

    def test_deep_dependencies_do_not_use_python_recursion(self):
        state = state_from(chain(1500))
        ranks, cycles = transaction_ranks(state["transactions"])
        self.assertFalse(cycles)
        self.assertEqual(ranks[txid(1499)], 1499)

    def test_pegin_txid_does_not_create_a_liquid_dependency(self):
        transactions = chain(2)
        transactions[txid(1)]["vin"][0]["is_pegin"] = True
        ranks, _ = transaction_ranks(state_from(transactions)["transactions"])
        self.assertEqual(ranks[txid(0)], ranks[txid(1)])

    def test_fees_hidden_by_default_without_changing_other_graph_items_or_evidence(self):
        state = state_from({data["txid"]: data for key, data in fixture().items()
                            if not key.endswith("outspends")})
        original = copy.deepcopy(state)
        hidden, shown = build_graph(state), build_graph(state, include_fees=True)
        self.assertEqual(state, original)
        self.assertEqual(hidden["namespace"], shown["namespace"])
        self.assertFalse(hidden["graph_options"]["include_fees"])
        self.assertTrue(shown["graph_options"]["include_fees"])
        self.assertEqual(hidden["fee_items"], shown["fee_items"])
        self.assertEqual(len(hidden["fee_items"]), 8)
        self.assertEqual(hidden["nodes"], [node for node in shown["nodes"] if node["id"] not in shown["fee_items"]])
        self.assertEqual(hidden["edges"], [edge for edge in shown["edges"] if edge["id"] not in shown["fee_items"]])
        self.assertEqual({node["id"] for node in hidden["nodes"] if node["kind"] == "event"},
                         {"event:" + C + ":1", "event:" + D + ":0"})
        for key, item in hidden["fee_items"].items():
            if item["endpoint"] == "shapes":
                out = state["transactions"][item["txid"]]["data"]["vout"][item["vout"]]
                self.assertEqual(output_kind(out), "fee")
            else:
                self.assertEqual(key[4:], item["target"][6:])

    def test_fee_row_is_chronological_above_main_graph_with_unknown_dates_last(self):
        transactions = chain(6)
        statuses = [{"block_height": 11, "block_time": 1700000100},
                    {"block_height": 12, "block_time": 1700000200},
                    {"block_height": 12, "block_time": 1700000200},
                    {"block_time": 1700000300}, {}, {}]
        fee = {"scriptpubkey": "", "scriptpubkey_type": "fee", "asset": LBTC, "value": 100}
        for index, tx in enumerate(transactions.values()):
            tx["status"] = statuses[index]
            tx["vout"].append(copy.deepcopy(fee))
        graph = build_graph(state_from(transactions), include_fees=True)
        fees = sorted((node for node in graph["nodes"] if node["id"] in graph["fee_items"]), key=lambda node: node["x"])
        self.assertEqual([node["id"] for node in fees], ["event:" + txid(index) + ":1" for index in range(6)])
        self.assertEqual({node["y"] for node in fees}, {graph["layout"]["fee_row_y"]})
        self.assertLess(fees[0]["y"] + NODE_SIZE / 2, graph["layout"]["main_top"])
        self.assertIn("2023-11-14 UTC", fees[0]["label"])
        self.assertIn("Date ??", fees[-1]["label"])
        self.assert_no_overlap(graph)

    def test_svg_bounds_include_negative_fee_row_and_legend_is_above_every_node(self):
        state = state_from({data["txid"]: data for key, data in fixture().items()
                            if not key.endswith("outspends")})
        graph = build_graph(state, include_fees=True)
        svg = ET.fromstring(svg_graph(graph))
        left, top, width, height = map(float, svg.attrib["viewBox"].split())
        self.assertLess(top, 0)
        for node in graph["nodes"]:
            self.assertGreaterEqual(node["x"] - NODE_SIZE / 2, left)
            self.assertLessEqual(node["x"] + NODE_SIZE / 2, left + width)
            self.assertGreaterEqual(node["y"] - NODE_SIZE / 2, top)
            self.assertLessEqual(node["y"] + NODE_SIZE / 2, top + height)
        header = svg.find("{http://www.w3.org/2000/svg}g")
        bottom = max(float(text.attrib["y"]) + float(text.attrib["font-size"])
                     for text in header.findall("{http://www.w3.org/2000/svg}text"))
        self.assertLess(bottom, min(node["y"] - NODE_SIZE / 2 for node in graph["nodes"]))

    def test_fee_display_preference_leaves_raw_exports_and_observations_intact(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture_file = root / "fixture.json"
            save_json(fixture_file, fixture())
            store = Store(root / "case")
            self.addCleanup(store.close)
            limits = Limits(max_hops=3)
            api = Esplora(store, "pending", limits, fixture=fixture_file, min_interval=0)
            state = new_state([A + ":0"], api.base, limits, [])
            api.run_id = state["run_id"]
            state = trace(api, state, limits, root / "trace.json")
            export_run(store, state, root / "hidden")
            shown_state = copy.deepcopy(state)
            shown_state["graph_options"] = {"include_fees": True}
            export_run(store, shown_state, root / "shown")
            self.assertFalse(read_json(root / "hidden" / "graph.json")["graph_options"]["include_fees"])
            self.assertTrue(read_json(root / "shown" / "graph.json")["graph_options"]["include_fees"])
            for filename in ("outputs.csv", "events.csv", "inputs.csv", "spends.csv", "evidence-index.json"):
                self.assertEqual((root / "hidden" / filename).read_bytes(), (root / "shown" / filename).read_bytes())
            for filename in ("outputs.csv", "events.csv"):
                with (root / "hidden" / filename).open() as stream:
                    self.assertEqual(sum(row["kind"] == "fee" for row in csv.DictReader(stream)), 4)
            for response in (root / "hidden" / "evidence").iterdir():
                self.assertEqual(response.read_bytes(), (root / "shown" / "evidence" / response.name).read_bytes())


if __name__ == "__main__":
    unittest.main()
