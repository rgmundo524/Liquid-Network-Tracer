"""Change-row alignment must also update the transaction's output ports."""

import copy
import unittest

from liquid_tracer.attachment_order import attachment_order_metrics
from liquid_tracer.change_layout import apply_change_layout
from liquid_tracer.elk_layout import attachment_point, layout_metrics, optimize_graph
from liquid_tracer.export import build_graph
from tests.test_change_layout import change_state
from tests.test_elk_layout import HAS_ELK


def screenshot_graph(sibling_port=25):
    nodes = [{"id": key, "kind": kind, "x": x, "y": y, "width": 160, "height": 160,
              "column": column} for key, kind, x, y, column in (
                  ("tx", "transaction", 0, 0, 0), ("change", "address", 800, 0, 1),
                  ("payment", "address", 800, 260, 1))]
    edges = [{"id": f"out:tx:{index}", "source": "tx", "target": target, "outpoint": f"tx:{index}",
              "label": f"vout {index}" + (" · Change" if index == 0 else ""),
              "attachment": {"startItem": {"position": {"x": "100%", "y": f"{port}%"}},
                             "endItem": {"position": {"x": "0%", "y": "50%"}}},
              "route": []}
             for index, target, port in ((0, "change", 50), (1, "payment", sibling_port))]
    return {"nodes": nodes, "edges": edges, "layout": {}, "graph_options": {"connector_style": "elbowed"},
            "change_outputs": {"designations": [{"edge_id": "out:tx:0", "txid": "tx", "vout": 0,
                                                  "outpoint": "tx:0"}]}}


def evidence(graph):
    return [{key: value for key, value in edge.items() if key in (
        "id", "source", "target", "outpoint", "details", "label", "change_output")}
        for edge in graph["edges"]]


class ChangeOutputPortTests(unittest.TestCase):
    def test_screenshot_crossing_is_fixed_even_when_neither_object_moves(self):
        for old_port in (25, 50):
            with self.subTest(old_port=old_port):
                graph = screenshot_graph(old_port)
                before = copy.deepcopy(graph)
                apply_change_layout(graph)
                self.assertEqual(graph["nodes"], before["nodes"])
                self.assertEqual(evidence(graph), evidence(before))
                self.assertEqual(graph["edges"][0]["attachment"]["startItem"]["position"]["y"], "50%")
                self.assertGreater(float(graph["edges"][1]["attachment"]["startItem"]["position"]["y"][:-1]), 50)
                metric = attachment_order_metrics(graph)
                self.assertEqual(metric["endpoint_order_inversions"], 0)
                self.assertEqual(metric["coincident_ports"], 0)
                self.assertEqual(layout_metrics(graph, midpoint_elbows=True)["crossings"], 0)

    def test_siblings_follow_final_destination_rows_without_renumbering_vouts(self):
        graph = screenshot_graph()
        lower = copy.deepcopy(graph["nodes"][-1])
        lower.update(id="lower", y=520)
        graph["nodes"].append(lower)
        # vout 1 goes to the lower row; vout 2 goes to the nearer upper row.
        graph["edges"][1]["target"] = "lower"
        upper = copy.deepcopy(graph["edges"][1])
        upper.update(id="out:tx:2", outpoint="tx:2", label="vout 2", target="payment")
        graph["edges"].append(upper)
        before = evidence(graph)
        apply_change_layout(graph)
        lower_port = float(graph["edges"][1]["attachment"]["startItem"]["position"]["y"][:-1])
        upper_port = float(graph["edges"][2]["attachment"]["startItem"]["position"]["y"][:-1])
        self.assertLess(50, upper_port)
        self.assertLess(upper_port, lower_port)
        self.assertEqual(evidence(graph), before)
        self.assertEqual(attachment_order_metrics(graph)["endpoint_order_inversions"], 0)

    def test_fee_lane_keeps_its_original_attachment(self):
        graph = screenshot_graph()
        fee = {"id": "fee", "kind": "event", "x": 500, "y": -300, "width": 80, "height": 80, "column": 1}
        graph["nodes"].append(fee)
        fee_edge = copy.deepcopy(graph["edges"][-1])
        fee_edge.update(id="out:tx:2", target="fee", outpoint="tx:2", label="Fee", routing_exception="fee")
        graph["edges"].append(fee_edge)
        graph["fee_items"] = {"fee": {"endpoint": "shapes"}}
        before = copy.deepcopy(fee_edge)
        apply_change_layout(graph)
        self.assertEqual(graph["edges"][-1], before)


@unittest.skipUnless(HAS_ELK, "Local ELK dependencies unavailable")
class ChangeOutputElkTests(unittest.TestCase):
    def test_real_elk_change_output_has_distinct_correctly_ordered_sibling_ports(self):
        for change_index in (0, 1):
            with self.subTest(change_index=change_index):
                state = change_state(1)
                txid = next(iter(state["transactions"]))
                state["transactions"][txid]["data"]["vout"] = state["transactions"][txid]["data"]["vout"][:2]
                state["service_controls"]["change_outputs"][txid]["vout"] = change_index
                original = build_graph(state)
                graph = optimize_graph(original, "elbowed", layout_attempts=1)
                self.assertEqual(evidence(graph), evidence(original))
                metric = attachment_order_metrics(graph)
                self.assertEqual(metric["endpoint_order_inversions"], 0)
                self.assertEqual(metric["coincident_ports"], 0)
                self.assertEqual(layout_metrics(graph, midpoint_elbows=True)["crossings"], 0)
                nodes = {node["id"]: node for node in graph["nodes"]}
                for edge in graph["edges"]:
                    self.assertEqual(edge["route"][0], attachment_point(nodes[edge["source"]], edge["attachment"]["startItem"]))
                    self.assertEqual(edge["route"][-1], attachment_point(nodes[edge["target"]], edge["attachment"]["endItem"]))


if __name__ == "__main__":
    unittest.main()
