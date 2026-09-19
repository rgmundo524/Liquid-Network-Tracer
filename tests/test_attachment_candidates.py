"""Regressions for input ports that force a crossing at a transaction.

The supplied worker layouts keep the same nodes and evidence in both cases.
Only the order of the two input attachments changes, reproducing an upper
context input crossing a lower continuing input on its way into the TX box.
"""

import copy
import unittest
from unittest.mock import patch

from liquid_tracer.elk_layout import (
    _apply_candidate, _request_graph, attachment_point, layout_metrics,
    optimize_graph,
)
from liquid_tracer.export import build_graph
from liquid_tracer.input_order import INPUT_ORDER_VERSION
from liquid_tracer.miro import make_plan, validate_plan
from tests.test_input_order import child_input, input_order_state, west_positions
from tests.test_layout import txid


def fan_in_candidates(graph, request, *, context_above=True):
    """Build valid worker responses without invoking or mocking ELK itself."""
    edges = {edge["id"]: edge for edge in graph["edges"]}
    child = "tx:" + txid("input-order-child")
    parent = "tx:" + txid("input-order-parent-1")
    context = edges[child_input(0)]["source"]
    continuing = edges[child_input(1)]["source"]
    result_address = next(edge["target"] for edge in graph["edges"]
                          if edge["source"] == child)
    continuing_y, context_y = (600, 0) if context_above else (0, 600)
    coordinates = {parent: (0, continuing_y), continuing: (600, continuing_y), context: (600, context_y),
                   child: (1300, 300), result_address: (1900, 300)}
    target_ports = {edge["targets"][0]: edge["id"] for edge in request["edges"]}
    candidates = []
    for policy in ("traced_first", "geometry"):
        nodes, absolute_ports = [], {}
        for child_request in request["children"]:
            key = child_request["id"]
            width, height = child_request["width"], child_request["height"]
            x, y = coordinates[key]
            raw = {"id": key, "x": x, "y": y, "width": width,
                   "height": height, "ports": []}
            for port in child_request["ports"]:
                px = width if port["layoutOptions"]["elk.port.side"] == "EAST" else 0
                py = height / 2
                if key == child and port["id"] in target_ports:
                    input_index = int(target_ports[port["id"]].rsplit(":", 1)[-1])
                    visual_index = input_index if policy == "geometry" else 1 - input_index
                    py = height * (visual_index + 1) / 3
                # A non-central circle port exercises the rectangle-to-circle
                # perimeter projection used by the real Miro plan.
                if key == context:
                    py = height / 3
                raw["ports"].append({"id": port["id"], "x": px, "y": py})
                absolute_ports[port["id"]] = {"x": x + px, "y": y + py}
            nodes.append(raw)
        raw_edges = []
        for edge in request["edges"]:
            start = absolute_ports[edge["sources"][0]]
            end = absolute_ports[edge["targets"][0]]
            if edge["id"] in (child_input(0), child_input(1)):
                channel = 950 if edge["id"] == child_input(0) else 900
            else:
                channel = (start["x"] + end["x"]) / 2
            raw_edges.append({"id": edge["id"], "sections": [{
                "startPoint": copy.deepcopy(start),
                "bendPoints": [{"x": channel, "y": start["y"]},
                               {"x": channel, "y": end["y"]}],
                "endPoint": copy.deepcopy(end),
            }], "labels": [{**label, "x": start["x"] + 20,
                            "y": min(start["y"], end["y"]) - label["height"] - 10}
                           for label in edge.get("labels", [])]})
        candidates.append({"seed": 1, "inputOrderPolicy": policy,
                           "nodes": nodes, "edges": raw_edges})
    return candidates


class AttachmentCandidateTests(unittest.TestCase):
    def test_crossed_input_fixture_is_resolved_by_ports_without_moving_nodes(self):
        graph = build_graph(input_order_state(count=2, continuing=(1,)))
        request, ports, fees = _request_graph(graph)
        traced, geometry = fan_in_candidates(graph, request)
        self.assertEqual([{key: node[key] for key in ("id", "x", "y", "width", "height")}
                          for node in traced["nodes"]],
                         [{key: node[key] for key in ("id", "x", "y", "width", "height")}
                          for node in geometry["nodes"]])
        for style in ("straight", "elbowed"):
            with self.subTest(style=style):
                crossed = _apply_candidate(graph, traced, ports, fees, style)
                uncrossed = _apply_candidate(graph, geometry, ports, fees, style)
                self.assertGreater(layout_metrics(crossed)["crossings"],
                                   layout_metrics(uncrossed)["crossings"])
                self.assertEqual(layout_metrics(uncrossed)["crossings"], 0)

    def test_safer_input_order_wins_and_preserves_original_evidence(self):
        state = input_order_state(count=2, continuing=(1,))
        original_state = copy.deepcopy(state)
        graph = build_graph(state)
        original_graph = copy.deepcopy(graph)

        def worker(request, seeds, **kwargs):
            return fan_in_candidates(graph, request)

        for style in ("straight", "elbowed"):
            with self.subTest(style=style), \
                    patch("liquid_tracer.elk_layout._worker", side_effect=worker):
                result = optimize_graph(graph, connector_style=style)
                self.assertEqual(result["layout"]["input_order"]["policy"], "geometry")
                self.assertEqual(result["layout"]["input_order"]["version"], INPUT_ORDER_VERSION)
                self.assertGreaterEqual(INPUT_ORDER_VERSION, 3)
                positions = west_positions(result, [child_input(0), child_input(1)])
                self.assertLess(positions[0], positions[1])
                self.assertEqual(result["layout"]["metrics"]["candidate_count"], 2)
                self.assertEqual(layout_metrics(result)["crossings"], 0)
                before_edges = {edge["id"]: edge for edge in graph["edges"]}
                for edge in result["edges"]:
                    self.assertEqual({key: edge[key] for key in before_edges[edge["id"]]},
                                     before_edges[edge["id"]])
                before_nodes = {node["id"]: node for node in graph["nodes"]}
                self.assertEqual({node["id"]: node["details"] for node in result["nodes"]},
                                 {key: node["details"] for key, node in before_nodes.items()})
                self.assertEqual(graph, original_graph)
                self.assertEqual(state, original_state)

    def test_chosen_ports_reach_valid_miro_plan_and_stay_on_circle_perimeter(self):
        state = input_order_state(count=2, continuing=(1,))
        state["ancestor_runs"] = []
        graph = build_graph(state)
        with patch("liquid_tracer.elk_layout._worker",
                   side_effect=lambda request, seeds, **kwargs: fan_in_candidates(graph, request)):
            result = optimize_graph(graph, connector_style="elbowed")
        plan = make_plan(result)
        validate_plan(plan)
        connectors = {connector["key"]: connector for connector in plan["connectors"]}
        nodes = {node["id"]: node for node in result["nodes"]}
        for edge in result["edges"]:
            connector = connectors[edge["id"]]
            self.assertEqual(connector["attachment"], edge["attachment"])
            self.assertEqual((connector["source"], connector["target"]),
                             (edge["source"], edge["target"]))
            for endpoint, key, route_index in (("startItem", "source", 0), ("endItem", "target", -1)):
                node = nodes[edge[key]]
                point = attachment_point(node, edge["attachment"][endpoint])
                self.assertEqual(point, edge["route"][route_index])
                if node["kind"] == "address":
                    radius_squared = (((point["x"] - node["x"]) / (node["width"] / 2)) ** 2
                                      + ((point["y"] - node["y"]) / (node["height"] / 2)) ** 2)
                    self.assertAlmostEqual(radius_squared, 1, places=6)
        for index in (0, 1):
            caption = connectors[child_input(index)]["body"]["captions"][0]["content"]
            self.assertIn("vin " + str(index), caption)

    def test_continuing_input_stays_first_when_its_source_is_above_context(self):
        graph = build_graph(input_order_state(count=2, continuing=(1,)))
        for style in ("straight", "elbowed"):
            with self.subTest(style=style), patch(
                "liquid_tracer.elk_layout._worker",
                side_effect=lambda request, seeds, **kwargs: fan_in_candidates(
                    graph, request, context_above=False),
            ):
                result = optimize_graph(graph, connector_style=style)
                self.assertEqual(result["layout"]["input_order"]["policy"], "traced_first")
                positions = west_positions(result, [child_input(1), child_input(0)])
                self.assertLess(positions[0], positions[1])
                self.assertEqual(layout_metrics(result)["crossings"], 0)

    def test_context_compaction_cannot_reintroduce_attachment_inversions(self):
        graph = build_graph(input_order_state(count=2, continuing=(1,)))
        original = copy.deepcopy(graph)
        winning_geometry = []

        def unsafe_compaction(candidate):
            winning_geometry.append(copy.deepcopy(candidate))
            edges = {edge["id"]: edge for edge in candidate["edges"]}
            nodes = {node["id"]: node for node in candidate["nodes"]}
            context_edge = edges[child_input(0)]
            context = nodes[context_edge["source"]]
            continuing = nodes[edges[child_input(1)]["source"]]
            # Move the previously upper source below its sibling while leaving
            # the already selected top-to-bottom transaction ports unchanged.
            context["y"] = continuing["y"] + 400
            context_edge["route"][0] = attachment_point(context, context_edge["attachment"]["startItem"])
            candidate["layout"]["branch_organization"]["context_inputs_moved"] = 1
            return candidate

        with patch("liquid_tracer.elk_layout._worker",
                   side_effect=lambda request, seeds, **kwargs: fan_in_candidates(graph, request)), \
                patch("liquid_tracer.elk_layout.compact_context_inputs",
                      side_effect=unsafe_compaction) as compact:
            result = optimize_graph(graph, connector_style="elbowed")
        compact.assert_called_once()
        self.assertEqual(result["layout"]["input_order"]["policy"], "geometry")
        self.assertEqual(result["nodes"], winning_geometry[0]["nodes"])
        self.assertEqual(result["edges"], winning_geometry[0]["edges"])
        self.assertEqual(result["layout"]["branch_organization"]["context_compaction_rejected"],
                         "attachment_routing_estimate")
        self.assertEqual(result["layout"]["metrics"]["attachments"]["endpoint_order_inversions"], 0)
        self.assertEqual(layout_metrics(result)["crossings"], 0)
        self.assertEqual(graph, original)


if __name__ == "__main__":
    unittest.main()
