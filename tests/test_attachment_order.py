"""Port-order estimates use physical endpoints without editing evidence."""

import copy
import random
import unittest

from liquid_tracer.attachment_order import ATTACHMENT_ORDER_VERSION, attachment_order_metrics
from liquid_tracer.elk_layout import _port


def node(key, x, y, kind="address"):
    return {"id": key, "kind": kind, "x": x, "y": y, "width": 200, "height": 200}


def connection(key, source, target, start_y=50, end_y=50):
    return {"id": key, "source": source["id"], "target": target["id"], "label": key,
            "attachment": {"startItem": _port(source, source["width"], source["height"] * start_y / 100),
                           "endItem": _port(target, 0, target["height"] * end_y / 100)}}


def screenshot_graph(swapped=True):
    context = node("context", 0, -300)
    traced = node("traced", 0, 0)
    transaction = node("transaction", 700, 0, "transaction")
    return {"nodes": [context, traced, transaction], "edges": [
        connection("vin:0", context, transaction, end_y=75 if swapped else 25),
        connection("vin:1", traced, transaction, end_y=25 if swapped else 75),
    ]}


class AttachmentOrderTests(unittest.TestCase):
    def test_screenshot_inverted_sources_have_one_order_conflict(self):
        original = screenshot_graph()
        before = copy.deepcopy(original)
        measured = attachment_order_metrics(original)
        self.assertEqual(measured["endpoint_order_inversions"], 1)
        self.assertEqual(measured["coincident_ports"], 0)
        self.assertEqual(measured["compared_ports"], 4)
        self.assertEqual(measured["version"], ATTACHMENT_ORDER_VERSION)
        self.assertTrue(measured["estimated"])
        self.assertFalse(measured["miro_routes_exact"])
        self.assertEqual(original, before)
        self.assertEqual(attachment_order_metrics(screenshot_graph(False))["endpoint_order_inversions"], 0)

    def test_shared_source_uses_distinct_actual_ports_not_its_center(self):
        source = node("shared-address", 0, 0)
        target = node("transaction", 700, 0, "transaction")
        edges = [connection("vin:0", source, target, start_y=25, end_y=75),
                 connection("vin:1", source, target, start_y=75, end_y=25)]
        graph = {"nodes": [source, target], "edges": edges}
        # The inverted pair is visible at both objects. Source centers would
        # miss the inversion at the transaction and report only one.
        self.assertEqual(attachment_order_metrics(graph)["endpoint_order_inversions"], 2)
        edges[0]["attachment"]["endItem"] = _port(target, 0, 50)
        edges[1]["attachment"]["endItem"] = _port(target, 0, 150)
        self.assertEqual(attachment_order_metrics(graph)["endpoint_order_inversions"], 0)

    def test_equal_local_or_remote_y_does_not_create_an_inversion(self):
        graph = screenshot_graph()
        for edge in graph["edges"]:
            edge["attachment"]["endItem"]["position"]["y"] = "50%"
        result = attachment_order_metrics(graph)
        self.assertEqual(result["endpoint_order_inversions"], 0)
        self.assertEqual(result["coincident_ports"], 1)
        graph = screenshot_graph()
        graph["nodes"][0]["y"] = graph["nodes"][1]["y"]
        result = attachment_order_metrics(graph)
        self.assertEqual(result["endpoint_order_inversions"], 0)
        self.assertEqual(result["coincident_ports"], 0)

    def test_three_coincident_ports_count_all_endpoint_pairs(self):
        graph = screenshot_graph()
        graph["edges"].append(copy.deepcopy(graph["edges"][0]))
        graph["edges"][-1]["id"] = "vin:2"
        for edge in graph["edges"]:
            edge["attachment"]["endItem"]["position"]["y"] = "50%"
        # Three pairs at the transaction, one at the repeated source.
        self.assertEqual(attachment_order_metrics(graph)["coincident_ports"], 4)

    def test_returns_fee_records_and_physical_backwards_edges_are_excluded(self):
        for kind in ("return", "fee", "fee_node", "fee_edge", "backwards", "backwards_ports"):
            graph = screenshot_graph()
            if kind in ("return", "fee"):
                graph["edges"][0]["routing_exception"] = kind
            elif kind == "fee_node":
                graph["fee_items"] = {"context": {"endpoint": "shapes"}}
            elif kind == "fee_edge":
                graph["fee_items"] = {"vin:0": {"endpoint": "connectors"}}
            elif kind == "backwards":
                graph["nodes"][0]["x"] = 1000
            else:
                # Centers point forward but the actual perimeters overlap.
                graph["nodes"][0]["x"] = 650
            with self.subTest(kind=kind):
                result = attachment_order_metrics(graph)
                self.assertEqual(result["endpoint_order_inversions"], 0)
                self.assertEqual(result["skipped_edges"], 1)
                self.assertEqual(result["compared_ports"], 2)

    def test_opposite_facing_or_top_ports_are_not_treated_as_east_west(self):
        graph = screenshot_graph()
        graph["edges"][0]["attachment"]["endItem"]["position"]["x"] = "100%"
        self.assertEqual(attachment_order_metrics(graph)["endpoint_order_inversions"], 0)
        graph["edges"][0]["attachment"]["endItem"]["position"] = {"x": "50%", "y": "0%"}
        self.assertEqual(attachment_order_metrics(graph)["endpoint_order_inversions"], 0)

    def test_missing_attachments_use_default_endpoints_without_mutation(self):
        graph = screenshot_graph()
        for edge in graph["edges"]:
            edge.pop("attachment")
        before = copy.deepcopy(graph)
        result = attachment_order_metrics(graph)
        self.assertEqual(result["endpoint_order_inversions"], 0)
        self.assertEqual(result["coincident_ports"], 1)
        self.assertEqual(before, graph)

    def test_shuffling_graph_does_not_change_measurements(self):
        graph = screenshot_graph()
        expected = attachment_order_metrics(graph)
        random.Random(12).shuffle(graph["nodes"])
        random.Random(14).shuffle(graph["edges"])
        self.assertEqual(attachment_order_metrics(graph), expected)

    def test_high_degree_reversal_counts_all_inversions_without_pair_enumeration(self):
        count = 8192
        target = node("transaction", 1000, 0, "transaction")
        sources = [node("address:" + str(index), 0, index * 300) for index in range(count)]
        graph = {"nodes": [target, *sources], "edges": [
            connection("vin:" + str(index), source, target, end_y=100 * (count - index) / (count + 1))
            for index, source in enumerate(sources)]}
        result = attachment_order_metrics(graph)
        self.assertEqual(result["endpoint_order_inversions"], count * (count - 1) // 2)
        self.assertEqual(result["compared_ports"], count * 2)
        self.assertEqual(result["coincident_ports"], 0)

    def test_empty_graph_has_zero_conflicts(self):
        result = attachment_order_metrics({"nodes": [], "edges": []})
        self.assertEqual(result["endpoint_order_inversions"], 0)
        self.assertEqual(result["coincident_ports"], 0)
        self.assertEqual(result["compared_ports"], 0)


if __name__ == "__main__":
    unittest.main()
