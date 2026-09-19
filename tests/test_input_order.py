"""Continuing UTXOs lead the displayed transaction inputs without renumbering vin."""

import copy
import random
import unittest

from liquid_tracer.compaction import compact_graph
from liquid_tracer.elk_layout import _request_graph, attachment_point, fallback_graph, optimize_graph
from liquid_tracer.export import build_graph
from liquid_tracer.input_order import INPUT_ORDER_VERSION, input_orders
from liquid_tracer.miro import make_plan
from tests.fixtures import output
from tests.test_elk_layout import HAS_ELK
from tests.test_layout import state_from, txid


def input_order_state(count=3, continuing=(2,), change=None):
    """All links intentionally lack trace roles; displayed outpoints are enough."""
    child = txid("input-order-child")
    transactions, inputs = {}, []
    for index in range(count):
        parent = txid("input-order-parent-" + str(index))
        prevout = output("SYNTHETIC-input-order-" + str(index))
        inputs.append({"txid": parent, "vout": 0, "prevout": prevout})
        if index in continuing:
            transactions[parent] = {"txid": parent, "vin": [], "vout": [prevout], "status": {}}
    transactions[child] = {"txid": child, "vin": inputs,
                           "vout": [output("SYNTHETIC-input-order-result")], "status": {}}
    state = state_from(transactions)
    if change is not None:
        parent = txid("input-order-parent-" + str(change))
        state["service_controls"] = {"change_outputs": {parent: {
            "vout": 0, "notes": "Synthetic investigator designation", "updated_at": "2026-09-19"}}}
    return state


def child_input(index):
    return f"in:{txid('input-order-child')}:{index}"


def west_positions(graph, order):
    edges = {edge["id"]: edge for edge in graph["edges"]}
    return [float(edges[key]["attachment"]["endItem"]["position"]["y"].removesuffix("%"))
            for key in order]


class InputOrderSemanticsTests(unittest.TestCase):
    def test_exact_displayed_child_output_precedes_context_without_trace_link(self):
        state = input_order_state()
        original = copy.deepcopy(state)
        graph = build_graph(state)
        before = copy.deepcopy(graph)
        self.assertTrue(all(edge["role"] == "context_input" for edge in graph["edges"]
                            if edge["id"].startswith("in:")))
        self.assertEqual(input_orders(graph), {"tx:" + txid("input-order-child"):
                                               [child_input(2), child_input(0), child_input(1)]})
        self.assertEqual(graph, before)
        self.assertEqual(state, original)

    def test_same_address_different_outpoint_does_not_inherit_priority(self):
        state = input_order_state()
        transaction = state["transactions"][txid("input-order-child")]["data"]
        transaction["vin"][0]["prevout"] = copy.deepcopy(transaction["vin"][2]["prevout"])
        graph = build_graph(state)
        edges = {edge["id"]: edge for edge in graph["edges"]}
        self.assertEqual(edges[child_input(0)]["source"], edges[child_input(2)]["source"])
        edges[child_input(0)]["role"] = "traced_input"
        self.assertEqual(input_orders(graph)["tx:" + txid("input-order-child")],
                         [child_input(2), child_input(0), child_input(1)])

    def test_output_must_be_displayed_and_match_source_node(self):
        graph = build_graph(input_order_state())
        output_id = "out:" + txid("input-order-parent-2") + ":0"
        hidden = copy.deepcopy(graph)
        hidden["edges"] = [edge for edge in hidden["edges"] if edge["id"] != output_id]
        self.assertEqual(input_orders(hidden), {})
        mismatched = copy.deepcopy(graph)
        edges = {edge["id"]: edge for edge in mismatched["edges"]}
        edges[output_id]["target"] = edges[child_input(0)]["source"]
        self.assertEqual(input_orders(mismatched), {})

    def test_numeric_vin_order_within_each_group_is_deterministic(self):
        graph = build_graph(input_order_state(12, continuing=(2, 10)))
        expected = [child_input(index) for index in (2, 10, 0, 1, 3, 4, 5, 6, 7, 8, 9, 11)]
        orders = input_orders(graph)
        self.assertEqual(orders["tx:" + txid("input-order-child")], expected)
        shuffled = copy.deepcopy(graph)
        random.Random(19).shuffle(shuffled["nodes"])
        random.Random(3).shuffle(shuffled["edges"])
        self.assertEqual(input_orders(shuffled), orders)

    def test_pegin_coinbase_and_nonforward_producers_are_excluded(self):
        graph = build_graph(input_order_state())
        for flag in ("is_pegin", "is_coinbase"):
            changed = copy.deepcopy(graph)
            edge = next(edge for edge in changed["edges"] if edge["id"] == child_input(2))
            edge["details"]["vin"][flag] = True
            with self.subTest(flag=flag):
                self.assertEqual(input_orders(changed), {})
        for delta in (0, 2):
            changed = copy.deepcopy(graph)
            nodes = {node["id"]: node for node in changed["nodes"]}
            nodes["tx:" + txid("input-order-parent-2")]["column"] = (
                nodes["tx:" + txid("input-order-child")]["column"] + delta)
            with self.subTest(producer_column_delta=delta):
                self.assertEqual(input_orders(changed), {})

    def test_unmixed_transactions_retain_existing_elk_ordering(self):
        for continuing in ((), (0, 1, 2)):
            with self.subTest(continuing=continuing):
                self.assertEqual(input_orders(build_graph(input_order_state(continuing=continuing))), {})

    def test_worker_request_translates_priority_to_target_ports_without_mutation(self):
        graph = build_graph(input_order_state())
        original = copy.deepcopy(graph)
        request, ports, _ = _request_graph(graph)
        child = "tx:" + txid("input-order-child")
        self.assertEqual(request["inputPortOrders"], {
            child: [ports[child_input(index)][1] for index in (2, 0, 1)]})
        self.assertEqual(graph, original)

    def test_fallback_prefers_physical_order_and_preserves_original_vin_labels(self):
        graph = build_graph(input_order_state())
        expected = input_orders(graph)["tx:" + txid("input-order-child")]
        result = fallback_graph(graph)
        nodes = {node["id"]: node for node in graph["nodes"]}
        edges = {edge["id"]: edge for edge in graph["edges"]}
        physical = sorted(expected, key=lambda key: nodes[edges[key]["source"]]["y"])
        values = west_positions(result, physical)
        self.assertEqual(values, sorted(values))
        self.assertEqual(len(values), len(set(values)))
        self.assertEqual({edge["id"]: edge["label"] for edge in result["edges"]},
                         {edge["id"]: edge["label"] for edge in graph["edges"]})


@unittest.skipUnless(HAS_ELK, "Run liquid-layout-setup to install the pinned local ELK engine")
class RealInputOrderTests(unittest.TestCase):
    def assert_order(self, graph, expected):
        positions = west_positions(graph, expected)
        if graph["layout"]["input_order"]["policy"] == "traced_first":
            self.assertEqual(positions, sorted(positions))
        else:
            self.assertEqual(graph["layout"]["input_order"]["policy"], "geometry")
            self.assertTrue(graph["layout"]["input_order"]["crossing_avoidance_first"])
        self.assertEqual(len(positions), len(set(positions)))
        self.assertEqual(graph["layout"]["input_order"]["version"], INPUT_ORDER_VERSION)
        edges = {edge["id"]: edge for edge in graph["edges"]}
        nodes = {node["id"]: node for node in graph["nodes"]}
        for key in expected:
            edge = edges[key]
            self.assertEqual(edge["attachment"]["endItem"]["position"]["x"], "0%")
            self.assertEqual(edge["route"][-1],
                             attachment_point(nodes[edge["target"]], edge["attachment"]["endItem"]))

    def test_elk_and_miro_plan_preserve_order_without_changing_evidence(self):
        state = input_order_state(4, continuing=(1, 3))
        original = copy.deepcopy(state)
        graph = build_graph(state)
        before = copy.deepcopy(graph)
        expected = input_orders(graph)["tx:" + txid("input-order-child")]
        result = optimize_graph(graph, connector_style="elbowed")
        self.assert_order(result, expected)
        self.assertEqual(graph, before)
        self.assertEqual(state, original)
        before_nodes = {node["id"]: node for node in graph["nodes"]}
        for node in result["nodes"]:
            self.assertEqual(node["details"], before_nodes[node["id"]]["details"])
        before_edges = {edge["id"]: edge for edge in graph["edges"]}
        for edge in result["edges"]:
            self.assertEqual({key: edge[key] for key in before_edges[edge["id"]]}, before_edges[edge["id"]])
        plan = make_plan(result)
        connectors = {edge["key"]: edge for edge in plan["connectors"]}
        for edge in result["edges"]:
            self.assertEqual(connectors[edge["id"]]["attachment"], edge["attachment"])

    def test_change_alignment_and_compaction_keep_continuing_inputs_first(self):
        graph = build_graph(input_order_state(4, continuing=(1, 2), change=2))
        expected = input_orders(graph)["tx:" + txid("input-order-child")]
        result = optimize_graph(graph, connector_style="elbowed")
        self.assert_order(result, expected)
        self.assertTrue(result["layout"]["change_outputs"]["applied"])
        self.assert_order(compact_graph(result), expected)

    def test_shuffling_evidence_graph_does_not_change_input_ports(self):
        graph = build_graph(input_order_state())
        shuffled = copy.deepcopy(graph)
        random.Random(7).shuffle(shuffled["nodes"])
        random.Random(11).shuffle(shuffled["edges"])
        first, second = optimize_graph(graph), optimize_graph(shuffled)
        expected = input_orders(graph)["tx:" + txid("input-order-child")]
        self.assert_order(first, expected)
        self.assert_order(second, expected)
        self.assertEqual({edge["id"]: edge["attachment"] for edge in first["edges"]},
                         {edge["id"]: edge["attachment"] for edge in second["edges"]})
