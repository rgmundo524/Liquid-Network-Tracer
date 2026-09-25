"""Large context summaries retain every vin without quadratic fan scoring."""

import copy
import unittest

from liquid_tracer.compaction import _Budget
from liquid_tracer.context_clearance import _Clearance, repair_context_clearance
from liquid_tracer.edge_labels import caption_text
from liquid_tracer.elk_layout import attachment_point
from liquid_tracer.routing_estimates import route_variants
from tests.test_context_clearance import conflict_graph, context_hits, edge, hits, node


def dense_graph(count=251):
    graph = conflict_graph(summary=True)
    nodes = {item["id"]: item for item in graph["nodes"]}
    nodes["context"]["height"] = 160
    inputs = [edge("input" + str(index), "context", "tx", nodes,
                   source_y=100 * (index + 1) / (count + 1),
                   target_y=100 * (index + 1) / (count + 1)) for index in range(count)]
    for index, item in enumerate(inputs):
        item.update(label=f"vin {index}", quantity="?? ??", caption_display="details_only",
                    original_source="member" + str(index))
    nodes["context"]["details"].update(input_count=count,
        members=[node("member" + str(index), "address", 800, 300) for index in range(count)],
        input_edge_ids=[item["id"] for item in inputs])
    graph["edges"] = [*inputs, graph["edges"][-1]]
    return graph


def conflicts(graph, keys):
    nodes = {item["id"]: item for item in graph["nodes"]}
    edges = {item["id"]: item for item in graph["edges"]}
    budget = _Budget(len(nodes) + len(edges))
    geometry = _Clearance(nodes, edges, budget)
    return geometry.conflicts({key: geometry.routes[key] for key in keys}), budget


class DenseContextClearanceTests(unittest.TestCase):
    def test_clear_251_input_summary_moves_near_transaction_without_evidence_changes_and_is_idempotent(self):
        graph = dense_graph()
        graph["edges"].pop()  # Remove the unrelated obstruction; only proximity remains.
        original = copy.deepcopy(graph)
        self.assertFalse(context_hits(graph, {"context"}))
        repair_context_clearance(graph)
        report = graph["layout"]["branch_organization"]["context_clearance"]
        self.assertEqual((report["moved"], report["unresolved"]), (1, 0))
        self.assertFalse(report["checks_truncated"])
        self.assertEqual(report["added_crossing_pairs"], 0)
        self.assertFalse(context_hits(graph, {"context"}))
        nodes = {item["id"]: item for item in graph["nodes"]}
        self.assertLessEqual(abs(nodes["context"]["x"] - nodes["tx"]["x"]), 350)
        self.assertEqual(nodes["context"]["y"], nodes["tx"]["y"])
        self.assertEqual(graph["nodes"][1:], original["nodes"][1:])
        self.assertEqual({key: value for key, value in graph["nodes"][0].items() if key not in ("x", "y")},
                         {key: value for key, value in original["nodes"][0].items() if key not in ("x", "y")})
        self.assertEqual(len(graph["edges"]), 251)
        for current, before in zip(graph["edges"], original["edges"]):
            self.assertEqual({key: value for key, value in current.items() if key not in ("route", "label_layout")},
                             {key: value for key, value in before.items() if key not in ("route", "label_layout")})
            self.assertEqual(current["route"][0], attachment_point(nodes["context"], current["attachment"]["startItem"]))
            self.assertEqual(current["route"][-1], attachment_point(nodes["tx"], current["attachment"]["endItem"]))
        stable = copy.deepcopy(graph)
        repair_context_clearance(graph)
        self.assertEqual(graph["nodes"], stable["nodes"])
        self.assertEqual(graph["edges"], stable["edges"])
        report = graph["layout"]["branch_organization"]["context_clearance"]
        self.assertEqual((report["moved"], report["unresolved"]), (0, 0))
        self.assertFalse(report["checks_truncated"])

    def test_clear_summary_stays_put_without_unresolved_warning_when_closer_slots_are_blocked(self):
        graph = dense_graph()
        graph["edges"].pop()
        # Unrelated connector corridors occupy every closer X position while
        # leaving the original summary clear by more than the required margin.
        # Their endpoint shapes are far outside the context's input routes.
        for index, x in enumerate((981, 1260, 1500, 1600)):
            source, target = "above" + str(index), "below" + str(index)
            graph["nodes"].extend([node(source, "transaction", x - 80, -10000),
                                   node(target, "transaction", x + 80, 10000)])
            nodes = {item["id"]: item for item in graph["nodes"]}
            corridor = edge("corridor" + str(index), source, target, nodes, role="traced_input")
            corridor.update(connector_shape="straight", caption_display="details_only")
            graph["edges"].append(corridor)
        original = copy.deepcopy(graph)
        self.assertFalse(context_hits(graph, {"context"}))
        repair_context_clearance(graph)
        self.assertEqual(graph["nodes"], original["nodes"])
        self.assertEqual(graph["edges"], original["edges"])
        report = graph["layout"]["branch_organization"]["context_clearance"]
        self.assertEqual((report["moved"], report["unresolved"]), (0, 0))
        self.assertFalse(report["checks_truncated"])
        self.assertEqual(report["added_crossing_pairs"], 0)

    def test_blocked_251_input_summary_moves_near_transaction_with_complete_checks(self):
        graph = dense_graph()
        original = copy.deepcopy(graph)
        self.assertTrue(context_hits(graph, {"context"}))
        self.assertTrue(all(not caption_text(item) for item in graph["edges"][:-1]))
        repair_context_clearance(graph)
        report = graph["layout"]["branch_organization"]["context_clearance"]
        self.assertEqual((report["moved"], report["unresolved"]), (1, 0))
        self.assertFalse(report["checks_truncated"])
        self.assertEqual(report["added_crossing_pairs"], 0)
        self.assertFalse(context_hits(graph, {"context"}))
        nodes = {item["id"]: item for item in graph["nodes"]}
        self.assertLess(abs(nodes["context"]["x"] - nodes["tx"]["x"]), 500)
        self.assertEqual(nodes["context"]["y"], nodes["tx"]["y"])
        self.assertEqual(graph["nodes"][1:], original["nodes"][1:])
        self.assertEqual({key: value for key, value in graph["nodes"][0].items() if key not in ("x", "y")},
                         {key: value for key, value in original["nodes"][0].items() if key not in ("x", "y")})
        self.assertEqual(len(graph["edges"]), 252)
        for current, before in zip(graph["edges"], original["edges"]):
            self.assertEqual({key: value for key, value in current.items() if key not in ("route", "label_layout")},
                             {key: value for key, value in before.items() if key not in ("route", "label_layout")})
            for route in route_variants(current, nodes):
                for key, value in nodes.items():
                    if current["source"] == "context" and key not in (current["source"], current["target"]):
                        self.assertFalse(hits(route, value), (current["id"], key))
            if current["source"] == "context":
                self.assertEqual(current["route"][0], attachment_point(nodes["context"], current["attachment"]["startItem"]))
                self.assertEqual(current["route"][-1], attachment_point(nodes["tx"], current["attachment"]["endItem"]))

    def test_dense_fan_still_counts_every_external_connector_intersection(self):
        graph = dense_graph()
        nodes = {item["id"]: item for item in graph["nodes"]}
        nodes["left"].update(x=1200, y=-100)
        nodes["right"].update(x=1200, y=700)
        external = edge("external", "left", "right", nodes, role="traced_input")
        external["connector_shape"] = "straight"
        graph["edges"][-1] = external
        keys = [item["id"] for item in graph["edges"][:-1]]
        pairs, budget = conflicts(graph, keys)
        self.assertEqual(pairs, {tuple(sorted((key, "external"))) for key in keys})
        self.assertFalse(budget.truncated)

    def test_same_summary_same_target_fan_is_exempt_but_another_transaction_is_not(self):
        values = [node("context", "context_group", 0, 0, width=240),
                  node("tx", "transaction", 600, 0), node("tx2", "transaction", 600, 300)]
        nodes = {item["id"]: item for item in values}
        inputs = [edge("one", "context", "tx", nodes, source_y=25, target_y=75),
                  edge("two", "context", "tx", nodes, source_y=75, target_y=25),
                  edge("three", "context", "tx2", nodes, source_y=0)]
        graph = {"nodes": values, "edges": inputs[:2]}
        pairs, budget = conflicts(graph, ["one", "two"])
        self.assertEqual(pairs, set())
        self.assertFalse(budget.truncated)
        graph["edges"] = inputs
        pairs, budget = conflicts(graph, ["one", "two", "three"])
        self.assertNotIn(("one", "two"), pairs)
        self.assertTrue(any("three" in pair for pair in pairs))
        self.assertFalse(budget.truncated)

    def test_ordinary_shared_context_does_not_receive_summary_exemption(self):
        values = [node("context", "address", 0, 0), node("tx", "transaction", 600, 0)]
        nodes = {item["id"]: item for item in values}
        graph = {"nodes": values, "edges": [
            edge("one", "context", "tx", nodes, source_y=25, target_y=75),
            edge("two", "context", "tx", nodes, source_y=75, target_y=25)]}
        pairs, budget = conflicts(graph, ["one", "two"])
        self.assertEqual(pairs, {("one", "two")})
        self.assertFalse(budget.truncated)


if __name__ == "__main__":
    unittest.main()
