"""Display-only input aggregation retains complete canonical evidence."""
from copy import deepcopy
import json
import unittest

from liquid_tracer.common import TraceError
from liquid_tracer.context_connectors import (PREFIX, canonical_graph, display_graph,
    prepare, restore_graph, summaries)
from liquid_tracer.context_groups import group_context_inputs
from liquid_tracer.export import build_graph
from liquid_tracer.miro_frames import activity_frames
from tests.test_input_order import input_order_state


class ContextConnectorTests(unittest.TestCase):
    def graph(self, count=5):
        return build_graph(input_order_state(count, continuing=(count - 1,)), group_context_inputs=True)

    def test_one_display_edge_keeps_every_input_outpoint_and_record(self):
        graph = self.graph()
        before = deepcopy(graph)
        projected = display_graph(graph)
        summary, = summaries(graph)
        self.assertEqual(summary["id"], PREFIX + summary["target"][3:])
        self.assertEqual(summary["label"], "4 context inputs · 4 addresses")
        self.assertEqual(summary["quantity"], "")
        self.assertIsNone(summary["outpoint"])
        ids = summary["details"]["context_summary"]["member_edge_ids"]
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(projected["edges"]), len(graph["edges"]) - 3)
        self.assertFalse(set(ids).intersection(edge["id"] for edge in projected["edges"]))
        self.assertEqual(graph, before)
        self.assertEqual(canonical_graph(projected)["edges"], graph["edges"])

    def test_legacy_and_disabled_graphs_are_not_implicitly_upgraded(self):
        disabled = build_graph(input_order_state())
        self.assertIs(display_graph(disabled), disabled)
        graph = self.graph()
        graph.pop("context_connectors")
        self.assertIs(display_graph(graph), graph)
        self.assertNotIn("context_connectors", group_context_inputs(graph, enabled=True))

    def test_empty_group_metadata_needs_no_runtime_projection(self):
        graph = build_graph(input_order_state(2, continuing=(1,)), group_context_inputs=True)
        self.assertEqual(graph["context_connectors"]["summaries"], [])
        self.assertIs(display_graph(graph), graph)

    def test_runtime_projection_is_idempotent_and_deepcopy_does_not_copy_canonical_graph(self):
        graph = self.graph()
        projected = display_graph(graph)
        copied = deepcopy(projected)
        self.assertIs(display_graph(projected), projected)
        self.assertIs(display_graph(copied), copied)
        self.assertIs(copied._canonical, graph)
        copied["nodes"][0]["x"] += 100
        self.assertNotEqual(copied["nodes"][0]["x"], graph["nodes"][0]["x"])
        self.assertEqual(prepare(graph), graph)

    def test_serialized_projection_cannot_bypass_membership_validation(self):
        projected = display_graph(self.graph())
        counterfeit = json.loads(json.dumps(projected))
        counterfeit["_display_graph"] = True
        with self.assertRaises(TraceError):
            display_graph(counterfeit)

    def test_summary_input_and_address_counts_are_distinct(self):
        graph = build_graph(input_order_state(5, continuing=(4,)), merge_addresses=False)
        # Use the grouping implementation's exact occurrence-mode semantics.
        input_edges = sorted((edge for edge in graph["edges"] if edge["id"].startswith("in:")), key=lambda edge:edge["id"])
        lookup = {node["id"]:node for node in graph["nodes"]}
        lookup[input_edges[1]["source"]]["details"]["address"] = lookup[input_edges[0]["source"]]["details"]["address"]
        grouped = group_context_inputs(graph, enabled=True)
        summary, = summaries(grouped)
        self.assertEqual(summary["label"], "4 context inputs · 3 addresses")

    def test_frames_use_summary_identity_and_restore_canonical_partition(self):
        graph = self.graph()
        projected = display_graph(graph)
        self.assertEqual(projected["activity_frames"], activity_frames(projected))
        ids = {key for item in projected["activity_frames"]["activities"] for key in item["connector_keys"]}
        self.assertEqual(ids, {edge["id"] for edge in projected["edges"]})
        restored = restore_graph(projected, graph)
        self.assertEqual(restored["activity_frames"], graph["activity_frames"])

    def test_restore_keeps_layout_and_original_hidden_evidence(self):
        graph = self.graph()
        projected = display_graph(graph)
        projected["nodes"][0]["x"] += 17
        summary = next(edge for edge in projected["edges"] if edge["id"].startswith(PREFIX))
        summary["route"] = [{"x": 1, "y": 2}, {"x": 3, "y": 4}]
        summary["connector_shape"] = "elbowed"
        restored = restore_graph(projected, graph)
        self.assertNotEqual(restored["nodes"][0]["x"], graph["nodes"][0]["x"])
        self.assertEqual(restored["edges"], graph["edges"])
        self.assertEqual(summaries(restored)[0]["route"], summary["route"])
        self.assertEqual(summaries(display_graph(restored))[0]["route"], summary["route"])

    def test_restore_rejects_changed_ordinary_and_summary_evidence(self):
        graph = self.graph()
        for mutation in (lambda p:p["edges"].pop(),
                         lambda p:p["nodes"].pop(),
                         lambda p:p["edges"][0].update(outpoint="changed:1"),
                         lambda p:p["edges"][-1].update(label="claim of common ownership")):
            projected = display_graph(graph)
            mutation(projected)
            with self.assertRaises(TraceError):
                restore_graph(projected, graph)

    def test_tampered_metadata_never_hides_edges(self):
        mutations = [lambda g:g.update(context_connectors=None),
                     lambda g:g["context_connectors"].update(version=True),
                     lambda g:g["context_connectors"].update(version=999),
                     lambda g:g["context_connectors"]["summaries"].clear(),
                     lambda g:g["context_connectors"]["summaries"][0].update(quantity="100 L-BTC"),
                     lambda g:g["context_connectors"]["summaries"][0].update(outpoint="invented:0"),
                     lambda g:g["context_connectors"]["summaries"][0].update(caption_display="details_only"),
                     lambda g:g["context_connectors"]["summaries"][0]["details"]["context_summary"].update(version=True),
                     lambda g:g["context_connectors"]["summaries"][0]["details"]["context_summary"].update(input_count=4.0),
                     lambda g:g["context_connectors"]["summaries"][0]["details"]["context_summary"]["member_edge_ids"].pop()]
        for mutation in mutations:
            graph = self.graph()
            mutation(graph)
            with self.assertRaises(TraceError):
                display_graph(graph)

    def test_protected_members_and_traced_edges_cannot_be_hidden_after_tampering(self):
        mutations = [lambda n,e,g:n.update(role="seed"),
                     lambda n,e,g:n.update(layout_hub=True),
                     lambda n,e,g:n.update(notes="important"),
                     lambda n,e,g:n["details"].update(address_attributions=[{"name":"exchange"}]),
                     lambda n,e,g:n["details"]["occurrences"][0].update(trace={"status":"pending"}),
                     lambda n,e,g:e.update(role="traced_input"),
                     lambda n,e,g:e["details"].update(validated_trace_link={"vin":0}),
                     lambda n,e,g:e["details"]["vin"].update(is_pegin=True),
                     lambda n,e,g:e.update(change_output={"vout":0}),
                     lambda n,e,g:e.update(original_source="another-address"),
                     lambda n,e,g:g["graph_options"].update(hub_addresses=[n["details"]["address"]]),
                     lambda n,e,g:g.setdefault("service_controls",{}).update(change_outputs={
                         e["outpoint"].rsplit(":",1)[0]:{"vout":int(e["outpoint"].rsplit(":",1)[1])}})]
        for mutation in mutations:
            graph = self.graph()
            group = next(n for n in graph["nodes"] if n["kind"] == "context_group")
            member = group["details"]["members"][0]
            edge = next(e for e in graph["edges"] if e.get("original_source") == member["id"])
            mutation(member,edge,graph)
            with self.assertRaises(TraceError):
                display_graph(graph)

    def test_confidential_and_known_values_remain_in_original_records(self):
        graph = self.graph()
        grouped_edges = [e for e in graph["edges"] if e.get("original_source")]
        grouped_edges[0]["quantity"] = "0.5 L-BTC"
        grouped_edges[0]["details"]["vin"]["prevout"].update(value=50_000_000, asset="known")
        before = deepcopy(graph["edges"])
        result = restore_graph(display_graph(graph), graph)
        self.assertEqual(result["edges"], before)
        summary, = summaries(result)
        self.assertEqual(summary["quantity"], "")
        self.assertNotIn("value", summary["details"]["context_summary"])


if __name__ == "__main__":
    unittest.main()
