"""Optional summaries preserve exact input evidence and investigative context."""

import copy
import random
import unittest

from liquid_tracer.context_groups import CONTEXT_GROUP_VERSION, group_context_inputs
from liquid_tracer.compaction import compact_graph
from liquid_tracer.elk_layout import optimize_graph
from liquid_tracer.export import build_graph
from liquid_tracer.input_order import input_orders
from liquid_tracer.layout_preview import render_svg
from liquid_tracer.mermaid import mermaid_source
from liquid_tracer.miro import make_plan, validate_plan
from tests.test_elk_layout import HAS_ELK
from tests.test_input_order import child_input, input_order_state
from tests.test_layout import txid


def summaries(graph):
    return [node for node in graph["nodes"] if node["kind"] == "context_group"]


class ContextGroupTests(unittest.TestCase):
    def graph(self, count=4, continuing=(3,), merge_addresses=True):
        return build_graph(input_order_state(count, continuing=continuing),
                           merge_addresses=merge_addresses)

    def source(self, graph, index):
        key = next(edge["source"] for edge in graph["edges"] if edge["id"] == child_input(index))
        return next(node for node in graph["nodes"] if node["id"] == key)

    def test_disabled_by_default_returns_original_graph_untouched(self):
        graph = self.graph()
        original = copy.deepcopy(graph)
        self.assertIs(group_context_inputs(graph), graph)
        self.assertEqual(graph, original)

    def test_group_keeps_all_original_input_evidence_and_member_records(self):
        graph = self.graph()
        original = copy.deepcopy(graph)
        result = group_context_inputs(graph, enabled=True)
        self.assertEqual(graph, original)
        self.assertEqual(result["namespace"], graph["namespace"])
        self.assertEqual(result["run"], graph["run"])
        self.assertEqual(len(result["edges"]), len(graph["edges"]))
        group, = summaries(result)
        self.assertEqual(group["details"]["address_count"], 3)
        self.assertEqual(group["details"]["input_count"], 3)
        self.assertEqual(group["width"], 240)
        self.assertEqual(group["height"], 160)
        before_nodes = {node["id"]: node for node in graph["nodes"]}
        for member in group["details"]["members"]:
            self.assertEqual(member, before_nodes[member["id"]])
            self.assertNotIn(member["id"], {node["id"] for node in result["nodes"]})
        restored = copy.deepcopy(result["edges"])
        for edge in restored:
            if "original_source" in edge:
                edge["source"] = edge.pop("original_source")
        self.assertEqual(restored, graph["edges"])
        self.assertEqual(result["context_groups"]["version"], CONTEXT_GROUP_VERSION)
        self.assertTrue(result["graph_options"]["group_context_inputs"])

    def test_exact_displayed_output_is_never_grouped_even_with_context_role(self):
        graph = self.graph()
        self.assertEqual(next(edge["role"] for edge in graph["edges"]
                              if edge["id"] == child_input(3)), "context_input")
        result = group_context_inputs(graph, enabled=True)
        self.assertEqual(self.source(graph, 3), self.source(result, 3))
        self.assertEqual(input_orders(result), input_orders(graph))

    def test_shared_address_with_another_transaction_is_never_grouped(self):
        graph = self.graph()
        source = self.source(graph, 0)
        other_tx = next(node["id"] for node in graph["nodes"]
                        if node["kind"] == "transaction" and node["id"] != "tx:" + txid("input-order-child"))
        extra = copy.deepcopy(next(edge for edge in graph["edges"] if edge["id"] == child_input(0)))
        extra.update(id="in:another:0", target=other_tx)
        graph["edges"].append(extra)
        result = group_context_inputs(graph, enabled=True)
        self.assertEqual(self.source(result, 0), source)
        self.assertEqual(summaries(result)[0]["details"]["address_count"], 2)

    def test_same_address_on_other_occurrence_cannot_be_hidden(self):
        graph = self.graph(merge_addresses=False)
        hidden = self.source(graph, 0)
        # An output occurrence of the same address elsewhere is distinct in
        # legacy mode, but must still stop the context occurrence being grouped.
        continuation = self.source(graph, 3)
        continuation["details"]["address"] = hidden["details"]["address"]
        result = group_context_inputs(graph, enabled=True)
        self.assertEqual(self.source(result, 0), hidden)
        self.assertEqual(summaries(result)[0]["details"]["address_count"], 2)

    def test_seed_traced_attributed_and_designated_inputs_remain_individual(self):
        changes = {
            "seed": lambda node, edge, graph: node.update(role="seed"),
            "traced": lambda node, edge, graph: edge.update(role="traced_input"),
            "validated": lambda node, edge, graph: edge["details"].update(validated_trace_link={"vin": 0}),
            "attributed": lambda node, edge, graph: node["details"].update(address_attributions=[{"name": "Service"}]),
            "occurrence_label": lambda node, edge, graph: node["details"]["occurrences"][0].update(labels=[{"name": "Reviewed"}]),
            "tracked": lambda node, edge, graph: node["details"]["occurrences"][0].update(trace={"status": "pending"}),
            "highlighted": lambda node, edge, graph: node.update(address_convergence={"roots": [1, 2]}),
            "hub": lambda node, edge, graph: node.update(layout_hub=True),
            "notes": lambda node, edge, graph: node.update(notes="Investigator finding"),
            "change": lambda node, edge, graph: edge.update(change_output={"vout": 0}),
            "external_change": lambda node, edge, graph: graph.update(service_controls={"change_outputs": {
                edge["outpoint"].rsplit(":", 1)[0]: {"vout": 0}}}),
        }
        for name, mutate in changes.items():
            with self.subTest(protected=name):
                graph = self.graph()
                node = self.source(graph, 0)
                edge = next(edge for edge in graph["edges"] if edge["id"] == child_input(0))
                mutate(node, edge, graph)
                result = group_context_inputs(graph, enabled=True)
                self.assertEqual(self.source(result, 0), node)
                self.assertEqual(summaries(result)[0]["details"]["address_count"], 2)

    def test_bitcoin_pegin_coinbase_events_and_unknown_addresses_are_excluded(self):
        changes = {
            "network": lambda node, edge: node["details"].update(network="bitcoin"),
            "pegin": lambda node, edge: edge["details"]["vin"].update(is_pegin=True),
            "coinbase": lambda node, edge: edge["details"]["vin"].update(is_coinbase=True),
            "event": lambda node, edge: node.update(kind="event"),
            "unknown": lambda node, edge: node["details"].update(address=None),
        }
        for name, mutate in changes.items():
            with self.subTest(excluded=name):
                graph = self.graph()
                node = self.source(graph, 0)
                edge = next(edge for edge in graph["edges"] if edge["id"] == child_input(0))
                mutate(node, edge)
                result = group_context_inputs(graph, enabled=True)
                self.assertEqual(self.source(result, 0), node)

    def test_requires_two_distinct_addresses_not_two_occurrence_nodes(self):
        graph = self.graph(3, continuing=(2,), merge_addresses=False)
        self.source(graph, 1)["details"]["address"] = self.source(graph, 0)["details"]["address"]
        result = group_context_inputs(graph, enabled=True)
        self.assertEqual(summaries(result), [])
        self.assertEqual(result["nodes"], graph["nodes"])
        self.assertEqual(result["edges"], graph["edges"])

    def test_multiple_utxos_of_one_address_count_as_one_address(self):
        graph = self.graph(5, continuing=(4,), merge_addresses=False)
        self.source(graph, 1)["details"]["address"] = self.source(graph, 0)["details"]["address"]
        result = group_context_inputs(graph, enabled=True)
        group, = summaries(result)
        self.assertEqual(group["details"]["address_count"], 3)
        self.assertEqual(group["details"]["input_count"], 4)
        self.assertEqual(len(group["details"]["members"]), 4)

    def test_large_group_grows_for_input_ports_and_id_is_stable(self):
        graph = self.graph(21, continuing=(20,))
        result = group_context_inputs(graph, enabled=True)
        group, = summaries(result)
        self.assertEqual(group["height"], 21 * 18)
        fewer = group_context_inputs(self.graph(), enabled=True)
        self.assertEqual(group["id"], summaries(fewer)[0]["id"])

    def test_idempotent_and_member_order_is_deterministic(self):
        graph = self.graph()
        result = group_context_inputs(graph, enabled=True)
        self.assertEqual(group_context_inputs(result, enabled=True), result)
        random.Random(5).shuffle(graph["nodes"])
        random.Random(7).shuffle(graph["edges"])
        self.assertEqual(summaries(group_context_inputs(graph, enabled=True)), summaries(result))

    def test_rebuilding_without_grouping_restores_every_original_identity(self):
        state = input_order_state(4, continuing=(3,))
        original = build_graph(state)
        original_state = copy.deepcopy(state)
        grouped = group_context_inputs(original, enabled=True)
        self.assertNotEqual(grouped["nodes"], original["nodes"])
        self.assertEqual(build_graph(state), original)
        self.assertEqual(state, original_state)

    def test_mermaid_includes_summary_and_every_original_input_caption(self):
        graph = group_context_inputs(self.graph(), enabled=True)
        source = mermaid_source(graph)
        self.assertIn("3 context addresses", source)
        self.assertEqual(source.count(" -->|"), len(graph["edges"]))
        for index in range(4):
            self.assertIn(f"vin {index}", source)


@unittest.skipUnless(HAS_ELK, "Run liquid-layout-setup to install the pinned local ELK engine")
class ContextGroupIntegrationTests(unittest.TestCase):
    def test_grouping_layout_compaction_and_miro_keep_each_input_and_full_evidence(self):
        state = input_order_state(8, continuing=(6, 7))
        state["ancestor_runs"] = []
        before = copy.deepcopy(state)
        graph = build_graph(state, group_context_inputs=True)
        group, = summaries(graph)
        self.assertEqual(group["details"]["input_count"], 6)
        self.assertEqual(group["details"]["address_count"], 6)
        original_edges = {edge["id"]: edge for edge in graph["edges"]}
        result = optimize_graph(graph, connector_style="elbowed")
        result = compact_graph(result)
        group_after, = summaries(result)
        self.assertEqual(group_after["details"], group["details"])
        self.assertEqual(input_orders(result), input_orders(graph))
        self.assertEqual(len(result["edges"]), len(original_edges))
        for edge in result["edges"]:
            original = original_edges[edge["id"]]
            for field in ("source", "target", "outpoint", "label", "quantity", "details"):
                self.assertEqual(edge[field], original[field])
        plan = make_plan(result)
        validate_plan(plan)
        summary_shape = next(item for item in plan["shapes"] if item["key"] == group["id"])
        self.assertEqual(summary_shape["body"]["data"]["shape"], "rectangle")
        self.assertEqual(len(plan["connectors"]), len(original_edges))
        svg = render_svg(result).decode("utf-8")
        self.assertIn("6 context addresses", svg)
        self.assertIn("Details in local export", svg)
        self.assertEqual(state, before)


if __name__ == "__main__":
    unittest.main()
