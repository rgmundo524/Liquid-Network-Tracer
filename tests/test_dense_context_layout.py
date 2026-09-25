"""Hundreds of summarized inputs retain evidence without a giant caption lane."""

import copy
import unittest

from liquid_tracer.context_groups import CONTEXT_GROUP_VERSION
from liquid_tracer.edge_labels import caption_text
from liquid_tracer.elk_layout import _request_graph, attachment_point, optimize_graph
from liquid_tracer.export import build_graph
from liquid_tracer.input_order import input_orders
from liquid_tracer.miro import make_plan, validate_plan
from tests.fixtures import output
from tests.test_context_groups import summaries
from tests.test_elk_layout import HAS_ELK
from tests.test_input_order import input_order_state
from tests.test_layout import txid


def dense_state():
    state = input_order_state(252, continuing=(251,))
    state["ancestor_runs"] = []
    state["transactions"][txid("input-order-child")]["data"]["vout"].append(
        output("SYNTHETIC-second-output"))
    return state


class DenseContextEvidenceTests(unittest.TestCase):
    def test_251_inputs_keep_all_original_evidence_with_compact_presentation(self):
        state = dense_state()
        before = copy.deepcopy(state)
        original = build_graph(state)
        graph = build_graph(state, group_context_inputs=True)
        group, = summaries(graph)
        self.assertEqual(state, before)
        self.assertEqual(graph["context_groups"]["version"], CONTEXT_GROUP_VERSION)
        self.assertEqual((group["width"], group["height"]), (240, 160))
        self.assertEqual(group["details"]["address_count"], 251)
        self.assertEqual(group["details"]["input_count"], 251)
        self.assertEqual(len(group["details"]["members"]), 251)
        original_nodes = {node["id"]: node for node in original["nodes"]}
        for member in group["details"]["members"]:
            self.assertEqual(member, original_nodes[member["id"]])
        self.assertEqual(graph["namespace"], original["namespace"])
        self.assertEqual(graph["run"], original["run"])
        self.assertEqual(graph["activity_frames"]["starting_transactions"],
                         original["activity_frames"]["starting_transactions"])
        self.assertEqual({key for frame in graph["activity_frames"]["activities"]
                          for key in frame["connector_keys"]},
                         {key for frame in original["activity_frames"]["activities"]
                          for key in frame["connector_keys"]})
        original_edges = {edge["id"]: edge for edge in original["edges"]}
        self.assertEqual(len(graph["edges"]), len(original_edges))
        for edge in graph["edges"]:
            restored = copy.deepcopy(edge)
            if edge["source"] == group["id"]:
                self.assertEqual(edge["caption_display"], "details_only")
                self.assertEqual(caption_text(edge), "")
                restored["source"] = restored.pop("original_source")
                restored.pop("caption_display")
            else:
                self.assertNotIn("caption_display", edge)
                self.assertTrue(caption_text(edge))
            self.assertEqual(restored, original_edges[edge["id"]])

    def test_elk_request_keeps_all_ports_and_only_skips_dense_caption_reservations(self):
        graph = build_graph(dense_state(), group_context_inputs=True)
        group, = summaries(graph)
        request, ports, _ = _request_graph(graph)
        children = {node["id"]: node for node in request["children"]}
        self.assertEqual(children[group["id"]]["height"], 160)
        self.assertEqual(len(children[group["id"]]["ports"]), 251)
        self.assertEqual(set(ports), {edge["id"] for edge in graph["edges"]})
        grouped = {edge["id"] for edge in graph["edges"] if edge["source"] == group["id"]}
        self.assertEqual({edge["id"] for edge in request["edges"]}, set(ports))
        for edge in request["edges"]:
            self.assertEqual(bool(edge.get("labels")), edge["id"] not in grouped)


@unittest.skipUnless(HAS_ELK, "local ELK package is not installed")
class DenseContextEngineTests(unittest.TestCase):
    def test_251_input_layout_stays_near_its_branch_with_every_connector(self):
        graph = build_graph(dense_state(), group_context_inputs=True)
        before = copy.deepcopy(graph)
        result = optimize_graph(graph, "elbowed", layout_attempts=1)
        self.assertEqual(graph, before)
        group, = summaries(result)
        original_group, = summaries(before)
        self.assertEqual(group["details"], original_group["details"])
        self.assertEqual((group["width"], group["height"]), (240, 160))
        original_nodes = {node["id"]: node for node in before["nodes"]}
        self.assertEqual({node["id"] for node in result["nodes"]}, set(original_nodes))
        for node in result["nodes"]:
            self.assertEqual({key: value for key, value in node.items() if key not in ("x", "y")},
                             {key: value for key, value in original_nodes[node["id"]].items()
                              if key not in ("x", "y")})
        original_edges = {edge["id"]: edge for edge in before["edges"]}
        self.assertEqual(len(result["edges"]), len(original_edges))
        grouped = [edge for edge in result["edges"] if edge["source"] == group["id"]]
        self.assertEqual(len(grouped), 251)
        for edge in result["edges"]:
            for field in ("id", "source", "original_source", "target", "role", "outpoint",
                          "label", "quantity", "details", "caption_display"):
                self.assertEqual(edge.get(field), original_edges[edge["id"]].get(field))
        self.assertEqual(len({edge["id"] for edge in grouped}), 251)
        self.assertEqual(input_orders(result), input_orders(before))
        by_id = {node["id"]: node for node in result["nodes"]}
        for edge in grouped:
            # ELK may quantize nearby ports to the same position on a compact
            # shape. Each UTXO still has its own connector and both endpoints.
            for attachment, endpoint, index in (("startItem", "source", 0), ("endItem", "target", -1)):
                self.assertEqual(edge["route"][index], attachment_point(
                    by_id[edge[endpoint]], edge["attachment"][attachment]))

        # The old 251-input summary alone was 4536 units tall; even capping its
        # rectangle left a 7090-unit node span due to 251 caption reservations.
        nodes = result["nodes"]
        top = min(node["y"] - node["height"] / 2 for node in nodes)
        bottom = max(node["y"] + node["height"] / 2 for node in nodes)
        left = min(node["x"] - node["width"] / 2 for node in nodes)
        right = max(node["x"] + node["width"] / 2 for node in nodes)
        self.assertLess(bottom - top, 1200)
        self.assertLess(right - left, 5000)
        target = next(node for node in nodes if node["id"] == group["details"]["transaction_id"])
        self.assertLess(abs(group["y"] - target["y"]), 600)
        report = result["layout"]["branch_organization"]["context_clearance"]
        self.assertFalse(report["checks_truncated"])
        self.assertEqual(report["unresolved"], 0)

        plan = make_plan(result)
        validate_plan(plan)
        self.assertEqual(len(plan["connectors"]), len(result["edges"]))
        grouped_ids = {edge["id"] for edge in grouped}
        for connector in plan["connectors"]:
            if connector["key"] in grouped_ids:
                self.assertFalse(connector["body"].get("captions"))
            else:
                self.assertTrue(connector["body"].get("captions"))


if __name__ == "__main__":
    unittest.main()
