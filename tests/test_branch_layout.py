import copy
import unittest

from liquid_tracer.branch_layout import (BRANCH_LAYOUT_VERSION, compact_context_inputs,
                                         edge_priorities, organization_metrics)
from liquid_tracer.compaction import compact_graph
from liquid_tracer.elk_layout import (_apply_candidate, _request_graph, _worker,
                                     attachment_point, layout_metrics, optimize_graph)
from liquid_tracer.export import build_graph
from liquid_tracer.horizontal_spacing import compact_candidate
from liquid_tracer.input_order import input_orders
from tests.fixtures import fixture
from tests.test_elk_layout import HAS_ELK
from tests.test_input_order import child_input, input_order_state, west_positions
from tests.test_layout import chain, state_from


def context_graph():
    return {"nodes": [
        {"id": "context", "kind": "address", "x": 100, "y": 400, "width": 160,
         "height": 160, "column": 0},
        {"id": "tx", "kind": "transaction", "x": 1600, "y": 700, "width": 160,
         "height": 160, "column": 1}],
        "edges": [{"id": "input", "source": "context", "target": "tx", "label": "vin 0",
                   "quantity": "?? ??", "connector_shape": "elbowed",
                   "attachment": {"startItem": {"position": {"x": "100%", "y": "50%"}},
                                  "endItem": {"position": {"x": "0%", "y": "50%"}}},
                   "route": [{"x": 180, "y": 400}, {"x": 850, "y": 400},
                             {"x": 850, "y": 700}, {"x": 1520, "y": 700}]}],
        "fee_items": {}, "layout": {}}


class BranchPreferenceTests(unittest.TestCase):
    def test_continuation_weight_matches_exact_displayed_outpoint(self):
        graph = build_graph(input_order_state(3, continuing=(2,)))
        original = copy.deepcopy(graph)
        priorities = edge_priorities(graph)
        continuation = next(edge for edge in graph["edges"] if edge["id"] == child_input(2))
        output = next(edge for edge in graph["edges"] if edge["id"].startswith("out:")
                      and edge.get("outpoint") == continuation["outpoint"])
        self.assertEqual(priorities[continuation["id"]], 8)
        self.assertEqual(priorities[output["id"]], 8)
        self.assertEqual(priorities[child_input(0)], 1)
        self.assertEqual(graph, original)
        continuation["outpoint"] += "-unrelated"
        self.assertEqual(edge_priorities(graph)[continuation["id"]], 1)

    def test_change_priority_and_return_pegin_exclusions(self):
        graph = build_graph(input_order_state(3, continuing=(2,)))
        continuation = next(edge for edge in graph["edges"] if edge["id"] == child_input(2))
        output = next(edge for edge in graph["edges"] if edge["id"].startswith("out:")
                      and edge.get("outpoint") == continuation["outpoint"])
        output["change_output"] = {"vout": 0}
        self.assertEqual(edge_priorities(graph)[continuation["id"]], 12)
        continuation.setdefault("details", {})["vin"] = {"is_pegin": True}
        self.assertEqual(edge_priorities(graph)[continuation["id"]], 1)
        continuation["details"]["vin"] = {}
        nodes = {node["id"]: node for node in graph["nodes"]}
        nodes[output["source"]]["column"] = nodes[continuation["target"]]["column"]
        self.assertEqual(edge_priorities(graph)[continuation["id"]], 1)

    def test_context_input_compacts_without_changing_vin_or_attachment(self):
        graph = context_graph()
        before = copy.deepcopy(graph)
        compact_context_inputs(graph)
        report = graph["layout"]["branch_organization"]
        self.assertEqual(report["context_inputs_moved"], 1)
        self.assertLess(report["context_after"]["context_distance"], report["context_before"]["context_distance"])
        self.assertEqual(graph["nodes"][1], before["nodes"][1])
        self.assertEqual(graph["edges"][0]["attachment"], before["edges"][0]["attachment"])
        self.assertEqual(graph["edges"][0]["label"], "vin 0")
        self.assertEqual(layout_metrics(graph)["node_overlaps"], 0)
        for field, index in (("startItem", 0), ("endItem", 1)):
            self.assertEqual(graph["edges"][0]["route"][0 if index == 0 else -1],
                             attachment_point(graph["nodes"][index], graph["edges"][0]["attachment"][field]))

    def test_shared_and_explicit_change_nodes_do_not_move(self):
        for shared in (True, False):
            with self.subTest(shared=shared):
                graph = context_graph()
                if shared:
                    graph["edges"].append({**copy.deepcopy(graph["edges"][0]), "id": "input2"})
                else:
                    graph["layout"]["change_outputs"] = {"locked_nodes": ["context"]}
                nodes = copy.deepcopy(graph["nodes"])
                compact_context_inputs(graph)
                self.assertEqual(graph["nodes"], nodes)
                self.assertEqual(graph["layout"]["branch_organization"]["context_inputs_moved"], 0)

    def test_obstacle_checks_preserve_clearance(self):
        graph = context_graph()
        graph["nodes"].append({"id": "obstacle", "kind": "transaction", "x": 1240, "y": 700,
                               "width": 160, "height": 160, "column": 1})
        obstacle = copy.deepcopy(graph["nodes"][-1])
        compact_context_inputs(graph)
        context = graph["nodes"][0]
        self.assertTrue(abs(context["x"] - obstacle["x"]) >= 240
                        or abs(context["y"] - obstacle["y"]) >= 240)
        self.assertEqual(graph["nodes"][-1], obstacle)

    def test_summary_context_does_not_gain_flow_priority(self):
        graph = context_graph()
        graph["nodes"][0]["kind"] = "context_group"
        self.assertEqual(edge_priorities(graph), {"input": 1})
        before = copy.deepcopy(graph["nodes"])
        compact_context_inputs(graph)
        self.assertEqual(graph["nodes"], before)

    def test_hub_is_explicit_and_its_partition_does_not_rewrite_dependency_columns(self):
        graph = context_graph()
        graph["nodes"][0]["details"] = {"transaction_count": 100000}
        ordinary, _, _ = _request_graph(graph)
        self.assertEqual(ordinary["children"][0]["layoutOptions"]["elk.partitioning.partition"], "0")
        graph["nodes"][0]["layout_hub"] = True
        original = copy.deepcopy(graph)
        request, _, _ = _request_graph(graph)
        hub = next(node for node in request["children"] if node["id"] == "context")
        self.assertEqual(hub["layoutOptions"]["elk.partitioning.partition"], "-1")
        self.assertTrue(all(port["layoutOptions"]["elk.port.side"] == "EAST" for port in hub["ports"]))
        self.assertEqual(graph, original)
        compact_context_inputs(graph)
        self.assertEqual(graph["nodes"], original["nodes"])

    def test_optional_compaction_preserves_manual_hub_lane(self):
        graph = context_graph()
        graph["layout"]["algorithm"] = "elk_layered_v1"
        ordinary = compact_graph(graph)
        self.assertEqual(ordinary["layout"]["compaction"]["moved_addresses"], 1)
        graph["nodes"][0]["layout_hub"] = True
        compacted = compact_graph(graph)
        before = tuple(graph["nodes"][0][axis] - graph["nodes"][1][axis] for axis in ("x", "y"))
        after = tuple(compacted["nodes"][0][axis] - compacted["nodes"][1][axis] for axis in ("x", "y"))
        self.assertEqual(before, after)
        self.assertEqual(compacted["layout"]["compaction"]["moved_addresses"], 0)

    def test_straight_travel_uses_displayed_ports_instead_of_hidden_elk_detours(self):
        graph = context_graph()
        edge = graph["edges"][0]
        edge["route"][1]["y"] = 5000
        edge["route"][2]["y"] = -3000
        edge["attachment"]["endItem"]["position"]["y"] = "75%"
        elbowed = organization_metrics(graph)["weighted_vertical_travel"]
        edge["connector_shape"] = "straight"
        straight = organization_metrics(graph)["weighted_vertical_travel"]
        self.assertEqual(straight, 340)
        self.assertGreater(elbowed, 10000)


@unittest.skipUnless(HAS_ELK, "local ELK package is not installed")
class BranchEngineTests(unittest.TestCase):
    def test_worker_compares_profiles_with_bounded_candidate_count(self):
        graph = build_graph(input_order_state(4, continuing=(3,)))
        request, _, _ = _request_graph(graph)
        candidates = _worker(request, [1, 7, 19])
        self.assertEqual([candidate["branchProfile"] for candidate in candidates],
                         ["balanced", "flow_weighted", "flow_weighted"])
        self.assertEqual(len(candidates), 3)
        self.assertEqual(_worker(request, [1])[0]["branchProfile"], "flow_weighted")

    def test_flow_layout_reduces_travel_on_shared_address_cycle(self):
        graph = build_graph(state_from(chain(7)))
        request, ports, fees = _request_graph(graph)
        candidates = _worker(request, [1, 7, 19])
        results = [_apply_candidate(graph, compact_candidate(candidate), ports, fees, "elbowed")
                   for candidate in candidates]
        baseline = organization_metrics(results[0])["weighted_vertical_travel"]
        self.assertLess(min(organization_metrics(result)["weighted_vertical_travel"] for result in results[1:]), baseline)
        original = copy.deepcopy(graph)
        optimized = optimize_graph(graph, "elbowed")
        self.assertEqual(graph, original)
        self.assertEqual({node["id"] for node in optimized["nodes"]}, {node["id"] for node in graph["nodes"]})
        self.assertEqual({edge["id"] for edge in optimized["edges"]}, {edge["id"] for edge in graph["edges"]})
        self.assertEqual(optimized["layout"]["branch_organization"]["version"], BRANCH_LAYOUT_VERSION)

    def test_merge_layout_keeps_dependency_order_and_collision_gates(self):
        state = state_from({value["txid"]: value for key, value in fixture().items()
                            if not key.endswith("outspends")})
        graph = build_graph(state)
        request, ports, fees = _request_graph(graph)
        candidates = _worker(request, [1, 7, 19])
        baseline = _apply_candidate(graph, compact_candidate(candidates[0]), ports, fees, "elbowed")
        optimized = optimize_graph(graph, "elbowed")
        def safety(value):
            metrics = layout_metrics(value)
            return tuple(metrics[key] for key in ("node_overlaps", "node_intersections", "crossings"))
        self.assertLessEqual(safety(optimized), safety(baseline))
        nodes = {node["id"]: node for node in optimized["nodes"]}
        for node in nodes.values():
            for vin in node.get("details", {}).get("transaction", {}).get("vin", []):
                parent = nodes.get("tx:" + vin.get("txid", ""))
                if parent and parent["column"] < node["column"] and not vin.get("is_pegin"):
                    self.assertLess(parent["x"], node["x"])

    def test_manual_hub_keeps_one_identity_and_all_return_connections(self):
        graph = build_graph(state_from(chain(7)))
        hub = next(node for node in graph["nodes"] if node["kind"] == "address")
        hub["layout_hub"] = True
        original = copy.deepcopy(graph)
        optimized = optimize_graph(graph, "elbowed")
        nodes = {node["id"]: node for node in optimized["nodes"]}
        self.assertEqual(len(nodes), len(graph["nodes"]))
        self.assertEqual(graph, original)
        self.assertTrue(all(nodes[hub["id"]]["x"] < node["x"] for node in nodes.values()
                            if node["kind"] == "transaction"))
        self.assertEqual(optimized["layout"]["branch_organization"]["hubs"], [hub["id"]])
        self.assertEqual({(edge["id"], edge["source"], edge["target"], edge["outpoint"])
                          for edge in optimized["edges"]},
                         {(edge["id"], edge["source"], edge["target"], edge["outpoint"])
                          for edge in graph["edges"]})
        incoming = [edge for edge in optimized["edges"] if edge["target"] == hub["id"]]
        self.assertEqual(len(incoming), 7)
        self.assertTrue(all(edge["routing_exception"] == "return" for edge in incoming))

    def test_change_routing_preserves_hub_direction_and_input_order(self):
        graph = build_graph(input_order_state(4, continuing=(2, 3), change=2))
        hub_edge = next(edge for edge in graph["edges"] if edge["id"] == child_input(3))
        hub = next(node for node in graph["nodes"] if node["id"] == hub_edge["source"])
        hub["layout_hub"] = True
        optimized = optimize_graph(graph, "elbowed")
        for edge in optimized["edges"]:
            if edge["source"] == hub["id"]:
                self.assertGreaterEqual(float(edge["attachment"]["startItem"]["position"]["x"].rstrip("%")), 50)
            if edge["target"] == hub["id"]:
                self.assertGreaterEqual(float(edge["attachment"]["endItem"]["position"]["x"].rstrip("%")), 50)
        for order in input_orders(optimized).values():
            positions = west_positions(optimized, order)
            self.assertEqual(positions, sorted(positions))
            self.assertEqual(len(positions), len(set(positions)))
        self.assertTrue(optimized["layout"]["change_outputs"]["applied"])


if __name__ == "__main__":
    unittest.main()
