"""Context placement must stay clear of saved and estimated board routes."""

import copy
import math
import unittest
from unittest.mock import patch

from liquid_tracer.compaction import compact_graph
from liquid_tracer.context_clearance import repair_context_clearance
from liquid_tracer.elk_layout import _port, attachment_point, layout_metrics, optimize_graph, segment_hits_node
from liquid_tracer.routing_estimates import route_variants
from tests.test_elk_layout import HAS_ELK
from tests.test_hub_layout import busy_hub_graph, node_id


def node(key, kind, x, y, *, width=160, height=160, column=0):
    return {"id": key, "kind": kind, "x": x, "y": y, "width": width,
            "height": height, "column": column, "label": key,
            "color": "#f5f6f8", "details": {"network": "liquid"}}


def edge(key, source, target, nodes, *, role="context_input", source_y=50, target_y=50):
    item = {"id": key, "source": source, "target": target, "role": role,
            "label": "", "quantity": "", "connector_shape": "elbowed",
            "outpoint": "SYNTHETIC-" + key + ":0", "routing_exception": None,
            "details": {"vin": {"txid": "SYNTHETIC-" + key, "vout": 0}},
            "attachment": {"startItem": _port(nodes[source], nodes[source]["width"],
                                               nodes[source]["height"] * source_y / 100),
                           "endItem": _port(nodes[target], 0, nodes[target]["height"] * target_y / 100)}}
    a = attachment_point(nodes[source], item["attachment"]["startItem"])
    b = attachment_point(nodes[target], item["attachment"]["endItem"])
    middle = (a["x"] + b["x"]) / 2
    item["route"] = [a, {"x": middle, "y": a["y"]}, {"x": middle, "y": b["y"]}, b]
    return item


def conflict_graph(*, summary=False, shared=False):
    """The unrelated ELK route detours left; Miro's midpoint cuts the context."""
    values = [node("context", "context_group" if summary else "address", 800, 300,
                   width=240 if summary else 160, height=200 if summary else 160),
              node("tx", "transaction", 1800, 300, column=1),
              node("left", "transaction", -600, -600),
              node("right", "transaction", 2200, 900, column=2)]
    if shared:
        values.append(node("tx2", "transaction", 1800, 700, column=1))
    nodes = {item["id"]: item for item in values}
    inputs = [edge("input", "context", "tx", nodes)]
    if summary:
        inputs = [edge("input" + str(i), "context", "tx", nodes,
                       source_y=25 + 25 * i, target_y=30 + 20 * i) for i in range(3)]
        members = [node("member" + str(i), "address", 800, 300) for i in range(3)]
        nodes["context"]["details"].update(members=members, input_count=3,
                                           input_edge_ids=[item["id"] for item in inputs])
        for index, item in enumerate(inputs):
            item["original_source"] = members[index]["id"]
    elif shared:
        inputs.append(edge("input2", "context", "tx2", nodes, source_y=65))
    crossing = edge("crossing", "left", "right", nodes, role="traced_input")
    a, b = crossing["route"][0], crossing["route"][-1]
    crossing["route"] = [a, {"x": -300, "y": a["y"]},
                         {"x": -300, "y": b["y"]}, b]
    return {"nodes": values, "edges": [*inputs, crossing], "layout": {}, "fee_items": {}}


def hits(route, value):
    return any(segment_hits_node({"x": a[0], "y": a[1]}, {"x": b[0], "y": b[1]}, value)
               for a, b in zip(route, route[1:]))


def context_hits(graph, context_ids):
    nodes = {item["id"]: item for item in graph["nodes"]}
    return {(item["id"], key) for item in graph["edges"] for key in context_ids
            if key not in (item["source"], item["target"])
            and any(hits(route, nodes[key]) for route in route_variants(item, nodes))}


class ContextClearanceTests(unittest.TestCase):
    def assert_clear(self, graph, context_ids):
        self.assertFalse(context_hits(graph, context_ids))
        nodes = {item["id"]: item for item in graph["nodes"]}
        for item in graph["edges"]:
            if item["source"] not in context_ids:
                continue
            for route in route_variants(item, nodes):
                for key, value in nodes.items():
                    if key not in (item["source"], item["target"]):
                        self.assertFalse(hits(route, value), (item["id"], key, route))

    def assert_evidence(self, original, result):
        nodes = {item["id"]: item for item in result["nodes"]}
        self.assertEqual(set(nodes), {item["id"] for item in original["nodes"]})
        for item in original["nodes"]:
            self.assertEqual({key: value for key, value in item.items() if key not in ("x", "y")},
                             {key: value for key, value in nodes[item["id"]].items() if key not in ("x", "y")})
        edges = {item["id"]: item for item in result["edges"]}
        self.assertEqual(set(edges), {item["id"] for item in original["edges"]})
        for item in original["edges"]:
            current = edges[item["id"]]
            for key in ("id", "source", "target", "role", "outpoint", "original_source",
                        "quantity", "label", "details", "attachment"):
                self.assertEqual(current.get(key), item.get(key), (item["id"], key))
            for name, endpoint, index in (("startItem", "source", 0), ("endItem", "target", -1)):
                self.assertEqual(current["route"][index], attachment_point(
                    nodes[current[endpoint]], current["attachment"][name]))

    def test_saved_detour_does_not_hide_midpoint_collision(self):
        graph = conflict_graph()
        original = copy.deepcopy(graph)
        nodes = {item["id"]: item for item in graph["nodes"]}
        routes = route_variants(graph["edges"][-1], nodes)
        self.assertFalse(hits(routes[0], nodes["context"]))
        self.assertTrue(any(hits(route, nodes["context"]) for route in routes[1:]))
        self.assertIs(repair_context_clearance(graph), graph)
        report = graph["layout"]["branch_organization"]["context_clearance"]
        self.assertEqual(report["candidates"], 1)
        self.assertEqual(report["moved"], 1)
        self.assertEqual(report["unresolved"], 0)
        self.assertFalse(report["miro_routes_exact"])
        self.assert_clear(graph, {"context"})
        self.assert_evidence(original, graph)

    def test_summary_keeps_three_individual_inputs_and_members(self):
        graph = conflict_graph(summary=True)
        original = copy.deepcopy(graph)
        self.assertTrue(context_hits(graph, {"context"}))
        repair_context_clearance(graph)
        self.assertEqual(graph["layout"]["branch_organization"]["context_clearance"]["moved"], 1)
        self.assert_clear(graph, {"context"})
        self.assert_evidence(original, graph)

    def test_shared_context_address_can_move_without_duplication(self):
        graph = conflict_graph(shared=True)
        original = copy.deepcopy(graph)
        repair_context_clearance(graph)
        self.assertEqual(graph["layout"]["branch_organization"]["context_clearance"]["moved"], 1)
        self.assert_clear(graph, {"context"})
        self.assert_evidence(original, graph)

    def test_clearance_can_take_priority_over_context_distance(self):
        graph = conflict_graph()
        nodes = {item["id"]: item for item in graph["nodes"]}
        # Start at the minimum horizontal gap. A vertical lane through that
        # position requires moving away from the tx to uncover the connector.
        nodes["context"].update(x=1440)
        nodes["left"].update(x=80, y=-600)
        nodes["right"].update(x=2800, y=900)
        graph["edges"] = [edge("input", "context", "tx", nodes),
                          edge("crossing", "left", "right", nodes, role="traced_input")]
        old_distance = math.dist((1440, 300), (1800, 300))
        self.assertTrue(context_hits(graph, {"context"}))
        repair_context_clearance(graph)
        self.assert_clear(graph, {"context"})
        current = nodes["context"]
        self.assertGreater(math.dist((current["x"], current["y"]), (1800, 300)), old_distance)

    def test_unrelated_existing_collision_does_not_block_safe_context_repair(self):
        graph = conflict_graph()
        graph["nodes"].extend([node("existing-obstacle", "transaction", 3400, -2000),
                               node("existing-left", "transaction", 3000, -2000),
                               node("existing-right", "transaction", 3800, -2000)])
        nodes = {item["id"]: item for item in graph["nodes"]}
        graph["edges"].append(edge("existing-crossing", "existing-left", "existing-right", nodes,
                                   role="traced_input"))
        original_fixed = copy.deepcopy(graph["nodes"][1:])
        repair_context_clearance(graph)
        self.assertEqual(graph["layout"]["branch_organization"]["context_clearance"]["moved"], 1)
        self.assert_clear(graph, {"context"})
        self.assertEqual(graph["nodes"][1:], original_fixed)
        self.assertGreater(layout_metrics(graph)["node_intersections"], 0)

    def test_protected_and_non_context_nodes_remain_fixed(self):
        for protection in ("hub", "change", "fee", "named", "incoming", "traced"):
            with self.subTest(protection=protection):
                graph = conflict_graph()
                context = graph["nodes"][0]
                if protection == "hub":
                    context["layout_hub"] = True
                elif protection == "change":
                    graph["layout"]["change_outputs"] = {"locked_nodes": ["context"]}
                elif protection == "fee":
                    graph["fee_items"] = {"context": {"endpoint": "shapes"}}
                elif protection == "named":
                    context["details"]["address_attributions"] = [{"entity": "Core"}]
                    graph["graph_options"] = {"center_name": "Core"}
                elif protection == "incoming":
                    nodes = {item["id"]: item for item in graph["nodes"]}
                    graph["edges"].append(edge("incoming", "left", "context", nodes, role="output"))
                else:
                    graph["edges"][0]["role"] = "traced_input"
                before_nodes, before_edges = copy.deepcopy(graph["nodes"]), copy.deepcopy(graph["edges"])
                repair_context_clearance(graph)
                self.assertEqual(graph["nodes"], before_nodes)
                self.assertEqual(graph["edges"], before_edges)
                self.assertEqual(graph["layout"]["branch_organization"]["context_clearance"]["moved"], 0)

    def test_exhausted_checks_preserve_graph_and_report_uncertainty(self):
        class Exhausted:
            remaining = -1
            truncated = True

            def spend(self):
                return False

        graph = conflict_graph()
        original = copy.deepcopy(graph)
        with patch("liquid_tracer.context_clearance._Budget", return_value=Exhausted()):
            repair_context_clearance(graph)
        self.assertEqual(graph["nodes"], original["nodes"])
        self.assertEqual(graph["edges"], original["edges"])
        report = graph["layout"]["branch_organization"]["context_clearance"]
        self.assertTrue(report["checks_truncated"])
        self.assertEqual(report["moved"], 0)
        self.assertGreaterEqual(report["unresolved"], 1)


@unittest.skipUnless(HAS_ELK, "local ELK package is not installed")
class ContextClearanceEngineTests(unittest.TestCase):
    def test_busy_hub_contexts_clear_board_routes_and_keep_vertical_spenders(self):
        graph = busy_hub_graph()
        original = copy.deepcopy(graph)
        result = optimize_graph(graph, "elbowed", layout_attempts=1)
        self.assertEqual(graph, original)
        context_ids = {item["id"] for item in result["nodes"]
                       if item.get("details", {}).get("address", "").startswith("SYNTHETIC-context-")}
        self.assertEqual(len(context_ids), 7)
        ContextClearanceTests.assert_clear(self, result, context_ids)
        nodes = {item["id"]: item for item in result["nodes"]}
        self.assertEqual(len({round(nodes[node_id(i)]["x"], 5) for i in range(7)}), 1)
        self.assertEqual(layout_metrics(result)["node_overlaps"], 0)
        settled = copy.deepcopy(result)
        repair_context_clearance(result)
        self.assertEqual(result["nodes"], settled["nodes"])
        self.assertEqual(result["edges"], settled["edges"])
        self.assertEqual({(item["id"], item["source"], item["target"], item["outpoint"])
                          for item in result["edges"]},
                         {(item["id"], item["source"], item["target"], item["outpoint"])
                          for item in original["edges"]})
        ContextClearanceTests.assert_clear(self, compact_graph(result), context_ids)


if __name__ == "__main__":
    unittest.main()
