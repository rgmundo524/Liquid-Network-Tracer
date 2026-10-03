"""Repeated context UTXOs share a display line, never evidence or addresses."""
from copy import deepcopy
import json
import unittest

from liquid_tracer.common import TraceError
from liquid_tracer.context_connectors import (PARALLEL_PREFIX, canonical_graph, display_graph,
    parallel_identity, prepare, restore_graph, summaries)
from liquid_tracer.context_groups import group_context_inputs
from liquid_tracer.export import build_graph
from tests.fixtures import output
from tests.test_attribution_convergence import annotation
from tests.test_input_order import child_input, input_order_state
from tests.test_layout import txid


ADDRESS = "SYNTHETICrepeatedcontextaddress"


def repeated_state(count=5, continuing=(4,)):
    state = input_order_state(count, continuing=continuing)
    for record in state["transactions"].values():
        for vin in record["data"]["vin"]:
            vin["prevout"]["scriptpubkey_address"] = ADDRESS
        if record["data"]["txid"] != txid("input-order-child"):
            record["data"]["vout"][0]["scriptpubkey_address"] = ADDRESS
    return state


class ContextParallelConnectorTests(unittest.TestCase):
    def graph(self, **options):
        return build_graph(repeated_state(), group_context_inputs=True, **options)

    def test_repeated_address_keeps_nodes_and_canonical_records_and_exact_continuation(self):
        state = repeated_state()
        ordinary = build_graph(state)
        graph = build_graph(state, group_context_inputs=True)
        before = deepcopy(graph)
        summary, = summaries(graph)
        source, target = "liquid:address:" + ADDRESS, "tx:" + txid("input-order-child")
        self.assertEqual(graph["context_connectors"]["version"], 2)
        self.assertEqual(summary["id"], parallel_identity(source, target))
        self.assertEqual(summary["details"]["context_summary"], {
            "version": 2, "kind": "parallel", "input_count": 4, "address_count": 1,
            "member_edge_ids": [child_input(index) for index in range(4)]})
        self.assertEqual(summary["label"], "4 context inputs · 1 address")
        self.assertEqual((summary["quantity"], summary["outpoint"]), ("", None))
        self.assertEqual(graph["nodes"], ordinary["nodes"])
        self.assertEqual(graph["edges"], ordinary["edges"])
        projected = display_graph(graph)
        self.assertEqual(len(projected["edges"]), len(graph["edges"]) - 3)
        self.assertIn(child_input(4), {edge["id"] for edge in projected["edges"]})
        self.assertEqual(canonical_graph(projected)["edges"], graph["edges"])
        self.assertEqual(graph, before)

    def test_named_hub_and_shared_seed_address_remains_visible(self):
        state = repeated_state()
        state["labels"] = [annotation(address=ADDRESS, name="Known source", stop=False)]
        state["seeds"] = [txid("input-order-parent-4") + ":0"]
        ordinary = build_graph(state, hub_addresses=[ADDRESS])
        graph = build_graph(state, group_context_inputs=True, hub_addresses=[ADDRESS])
        summary, = summaries(graph)
        node = next(node for node in graph["nodes"] if node["id"] == summary["source"])
        self.assertEqual(node["role"], "seed")
        self.assertTrue(node["layout_hub"])
        self.assertIn("Known source", node["label"])
        self.assertEqual(graph["nodes"], ordinary["nodes"])
        self.assertEqual(summary["details"]["context_summary"]["member_edge_ids"],
                         [child_input(index) for index in range(4)])

    def test_protected_input_evidence_is_excluded_independently(self):
        mutations = (
            lambda graph, edge: edge.update(role="traced_input"),
            lambda graph, edge: edge["details"].update(validated_trace_link={}),
            lambda graph, edge: edge["details"]["vin"].update(is_pegin=True),
            lambda graph, edge: edge["details"]["vin"].update(is_coinbase=True),
            lambda graph, edge: edge.update(change_output={"vout": 0}),
            lambda graph, edge: graph["run"].update(seeds=[edge["outpoint"]]),
            lambda graph, edge: graph.setdefault("service_controls", {}).update(change_outputs={
                edge["outpoint"].rsplit(":", 1)[0]: {"vout": 0}}),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                graph = self.graph()
                graph.pop("context_connectors")
                edge = next(edge for edge in graph["edges"] if edge["id"] == child_input(0))
                mutation(graph, edge)
                graph = prepare(graph)
                summary, = summaries(graph)
                self.assertEqual(summary["details"]["context_summary"]["member_edge_ids"],
                                 [child_input(index) for index in (1, 2, 3)])
                self.assertIn(child_input(0), {edge["id"] for edge in display_graph(graph)["edges"]})

    def test_backward_displayed_output_still_protects_its_exact_input(self):
        graph = self.graph()
        graph.pop("context_connectors")
        parent = next(node for node in graph["nodes"]
                      if node["id"] == "tx:" + txid("input-order-parent-4"))
        parent["column"] = 1000
        graph = prepare(graph)
        summary, = summaries(graph)
        self.assertNotIn(child_input(4), summary["details"]["context_summary"]["member_edge_ids"])
        self.assertIn(child_input(4), {edge["id"] for edge in display_graph(graph)["edges"]})

    def test_pairs_are_separate_and_identity_is_independent_of_membership_and_order(self):
        state = repeated_state()
        child = state["transactions"][txid("input-order-child")]["data"]
        second = deepcopy(child)
        second["txid"] = txid("second-child")
        second["vin"] = deepcopy(child["vin"][:2])
        for vin in second["vin"]:
            vin["vout"] = 1
        state["transactions"][second["txid"]] = {"data": second, "depth": 0, "observation_id": "second"}
        graph = build_graph(state, group_context_inputs=True)
        values = summaries(graph)
        self.assertEqual(len(values), 2)
        self.assertEqual(len({item["source"] for item in values}), 1)
        self.assertEqual(len({item["target"] for item in values}), 2)
        original = deepcopy(values)
        graph["nodes"].reverse()
        graph["edges"].reverse()
        self.assertEqual(summaries(graph), original)
        graph.pop("context_connectors")
        graph["edges"] = [edge for edge in graph["edges"] if edge["id"] != child_input(0)]
        changed = summaries(prepare(graph))
        self.assertEqual([item["id"] for item in changed], [item["id"] for item in original])

    def test_disabled_single_and_unknown_inputs_stay_individual(self):
        graph = build_graph(repeated_state(), group_context_inputs=False)
        self.assertIs(display_graph(graph), graph)
        self.assertNotIn("context_connectors", graph)
        for count, missing in ((2, False), (4, True)):
            state = repeated_state(count, continuing=(count - 1,))
            if missing:
                for record in state["transactions"].values():
                    for vin in record["data"]["vin"]:
                        vin["prevout"].pop("scriptpubkey_address", None)
                    for output in record["data"]["vout"]:
                        output.pop("scriptpubkey_address", None)
            graph = build_graph(state, group_context_inputs=True)
            self.assertEqual(summaries(graph), [])
            self.assertIs(display_graph(graph), graph)

    def test_parallel_and_isolated_group_summaries_have_disjoint_exact_members(self):
        state = repeated_state()
        inputs = state["transactions"][txid("input-order-child")]["data"]["vin"]
        for index in (5, 6):
            inputs.append({"txid": txid("isolated-" + str(index)), "vout": 0,
                           "prevout": output("SYNTHETIC-isolated-" + str(index))})
        graph = build_graph(state, group_context_inputs=True)
        isolated, parallel = summaries(graph)
        self.assertEqual(isolated["details"]["context_summary"]["version"], 1)
        self.assertEqual(isolated["details"]["context_summary"]["member_edge_ids"],
                         [child_input(5), child_input(6)])
        self.assertEqual(parallel["details"]["context_summary"]["kind"], "parallel")
        self.assertEqual(parallel["details"]["context_summary"]["member_edge_ids"],
                         [child_input(index) for index in range(4)])
        self.assertEqual(restore_graph(display_graph(graph), graph), graph)

    def test_restore_preserves_v1_snapshots_without_adding_parallel_summaries(self):
        isolated = build_graph(input_order_state(5, continuing=(4,)), group_context_inputs=True)
        isolated["context_connectors"]["version"] = 1
        before = deepcopy(isolated)
        restored = restore_graph(display_graph(isolated), isolated)
        self.assertEqual(restored, before)
        self.assertEqual(prepare(isolated), before)
        graph = self.graph()
        graph["context_connectors"] = {"version": 1, "summaries": []}
        before = deepcopy(graph)
        self.assertIs(display_graph(graph), graph)
        self.assertEqual(prepare(graph), before)
        self.assertEqual(group_context_inputs(graph, enabled=True), before)

    def test_v2_geometry_restores_without_changing_evidence_or_summary_semantics(self):
        graph = self.graph()
        projected = display_graph(graph)
        summary = next(edge for edge in projected["edges"] if edge["id"].startswith(PARALLEL_PREFIX))
        summary["route"] = [{"x": 1, "y": 2}, {"x": 3, "y": 4}]
        restored = restore_graph(projected, graph)
        self.assertEqual(restored["context_connectors"]["version"], 2)
        self.assertEqual(restored["nodes"], graph["nodes"])
        self.assertEqual(restored["edges"], graph["edges"])
        self.assertEqual(summaries(restored)[0]["route"], summary["route"])

    def test_tampered_membership_counts_identity_and_canonical_roles_fail_closed(self):
        mutations = (
            lambda graph: graph["context_connectors"].update(version=1),
            lambda graph: graph["context_connectors"]["summaries"][0].update(id="context-parallel:forged"),
            lambda graph: graph["context_connectors"]["summaries"][0].update(source="invented"),
            lambda graph: graph["context_connectors"]["summaries"][0].update(quantity="1 L-BTC"),
            lambda graph: graph["context_connectors"]["summaries"][0]["details"]["context_summary"].update(input_count=4.0),
            lambda graph: graph["context_connectors"]["summaries"][0]["details"]["context_summary"].update(address_count=True),
            lambda graph: graph["context_connectors"]["summaries"][0]["details"]["context_summary"].update(kind="group"),
            lambda graph: graph["context_connectors"]["summaries"][0]["details"]["context_summary"]["member_edge_ids"].pop(),
            lambda graph: next(edge for edge in graph["edges"] if edge["id"] == child_input(0)).update(role="traced_input"),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                graph = self.graph()
                mutation(graph)
                with self.assertRaises(TraceError):
                    display_graph(graph)
        serialized_projection = json.loads(json.dumps(display_graph(self.graph())))
        with self.assertRaises(TraceError):
            display_graph(serialized_projection)


if __name__ == "__main__":
    unittest.main()
