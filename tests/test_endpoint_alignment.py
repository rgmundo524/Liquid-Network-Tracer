import copy
import unittest
from unittest.mock import patch

from liquid_tracer.attachment_order import attachment_order_metrics
from liquid_tracer.elk_layout import attachment_point, layout_metrics, optimize_graph
from liquid_tracer.endpoint_alignment import align_near_horizontal_endpoints


def node(key, kind, x, y, size=160):
    return {"id": key, "kind": kind, "x": x, "y": y, "width": size, "height": size,
            "column": int(x // 400)}


def connect(key, source, target, *, start_y="50%", end_y="50%"):
    attachment = {"startItem": {"position": {"x": "100%", "y": start_y}},
                  "endItem": {"position": {"x": "0%", "y": end_y}}}
    a, b = attachment_point(source, attachment["startItem"]), attachment_point(target, attachment["endItem"])
    middle = (a["x"] + b["x"]) / 2
    return {"id": key, "source": source["id"], "target": target["id"],
            "outpoint": key + ":0", "label": "vin 7", "quantity": "?? ??",
            "attachment": attachment, "connector_shape": "elbowed", "routing_exception": None,
            "route": [a, {"x": middle, "y": a["y"]}, {"x": middle, "y": b["y"]}, b]}


def small_jog_graph(outgoing=False):
    source = node("tx" if outgoing else "address", "transaction" if outgoing else "address", 100, 120)
    target = node("address" if outgoing else "tx", "address" if outgoing else "transaction", 800, 100)
    return {"nodes": [source, target], "edges": [connect("edge", source, target)],
            "layout": {}, "fee_items": {}}


def worker_candidate(graph, request, seed, policy):
    """Controlled ELK coordinates exercise the real postprocessing pipeline."""
    nodes = {node["id"]: node for node in graph["nodes"]}
    edges = {edge["id"]: edge for edge in graph["edges"]}
    port_ends = {}
    for edge in request["edges"]:
        port_ends[edge["sources"][0]] = edges[edge["id"]]["attachment"]["startItem"]
        port_ends[edge["targets"][0]] = edges[edge["id"]]["attachment"]["endItem"]
    children, ports = [], {}
    for child in request["children"]:
        node = nodes[child["id"]]
        x, y = node["x"] - node["width"] / 2, node["y"] - node["height"] / 2
        raw = {"id": node["id"], "x": x, "y": y, "width": node["width"], "height": node["height"], "ports": []}
        for port in child["ports"]:
            point = port_ends[port["id"]]["position"]
            px, py = (float(point[axis].strip("%")) * node[size] / 100
                      for axis, size in (("x", "width"), ("y", "height")))
            raw["ports"].append({"id": port["id"], "x": px, "y": py})
            ports[port["id"]] = {"x": x + px, "y": y + py}
        children.append(raw)
    routes = []
    for edge in request["edges"]:
        a, b = ports[edge["sources"][0]], ports[edge["targets"][0]]
        middle = (a["x"] + b["x"]) / 2
        routes.append({"id": edge["id"], "sections": [{
            "startPoint": a, "endPoint": b,
            "bendPoints": [{"x": middle, "y": a["y"]}, {"x": middle, "y": b["y"]}]}],
            "labels": [{**label, "x": middle - label["width"] / 2,
                        "y": min(a["y"], b["y"]) - label["height"] - 10}
                       for label in edge.get("labels", [])]})
    return {"nodes": children, "edges": routes, "seed": seed, "inputOrderPolicy": policy}


class EndpointAlignmentTests(unittest.TestCase):
    def test_both_directions_align_only_circle_port_and_preserve_evidence(self):
        for outgoing in (False, True):
            with self.subTest(outgoing=outgoing):
                graph = small_jog_graph(outgoing)
                edge = graph["edges"][0]
                edge["label_layout"] = {"x": 400, "y": 80, "width": 80, "height": 20}
                before = copy.deepcopy(graph)
                self.assertIs(align_near_horizontal_endpoints(graph), graph)
                self.assertEqual(graph["layout"]["endpoint_alignment"]["applied"], 1)
                self.assertEqual(graph["nodes"], before["nodes"])
                self.assertEqual(len(edge["route"]), 2)
                self.assertAlmostEqual(edge["route"][0]["y"], edge["route"][-1]["y"], places=5)
                tx_field = "startItem" if outgoing else "endItem"
                addr_field = "endItem" if outgoing else "startItem"
                self.assertEqual(edge["attachment"][tx_field], before["edges"][0]["attachment"][tx_field])
                position = edge["attachment"][addr_field]["position"]
                px, py = (float(position[axis].strip("%")) for axis in ("x", "y"))
                self.assertAlmostEqual(((px - 50) / 50) ** 2 + ((py - 50) / 50) ** 2, 1, places=6)
                self.assertLess(px, 50) if outgoing else self.assertGreater(px, 50)
                for key in ("id", "source", "target", "outpoint", "label", "quantity", "connector_shape"):
                    self.assertEqual(edge[key], before["edges"][0][key])
                self.assertNotIn("label_layout", edge)

    def test_three_inputs_keep_every_transaction_slot_and_spacing(self):
        graph = small_jog_graph()
        target = graph["nodes"][1]
        target["y"] = 160
        graph["edges"][0] = connect("edge", graph["nodes"][0], target, end_y="10%")
        for index, y in enumerate((330, 520)):
            context = node("context" + str(index), "address", 100, y)
            graph["nodes"].append(context)
            graph["edges"].append(connect("context-edge" + str(index), context, target,
                                          end_y=("50%", "90%")[index]))
        before = copy.deepcopy(graph)
        align_near_horizontal_endpoints(graph)
        self.assertEqual(graph["layout"]["endpoint_alignment"]["applied"], 1)
        self.assertEqual([edge["attachment"]["endItem"] for edge in graph["edges"]],
                         [edge["attachment"]["endItem"] for edge in before["edges"]])
        self.assertEqual(attachment_order_metrics(graph)["endpoint_order_inversions"], 0)

    def test_singleton_on_other_hemisphere_does_not_block_alignment(self):
        graph = small_jog_graph()
        prior = node("prior", "transaction", -600, 120)
        graph["nodes"].append(prior)
        graph["edges"].append(connect("previous", prior, graph["nodes"][0]))
        previous = copy.deepcopy(graph["edges"][-1])
        align_near_horizontal_endpoints(graph)
        self.assertEqual(graph["layout"]["endpoint_alignment"]["applied"], 1)
        self.assertEqual(graph["edges"][-1], previous)

    def test_shared_hemisphere_and_ordinary_branch_bends_remain(self):
        graph = small_jog_graph()
        sibling = node("sibling", "transaction", 800, 400)
        graph["nodes"].append(sibling)
        graph["edges"].append(connect("shared", graph["nodes"][0], sibling))
        before = copy.deepcopy(graph["edges"])
        align_near_horizontal_endpoints(graph)
        self.assertEqual(graph["layout"]["endpoint_alignment"]["applied"], 0)
        self.assertEqual(graph["edges"], before)

    def test_real_detours_returns_fees_change_rows_and_curves_remain(self):
        def detour(graph):
            graph["edges"][0]["route"][1]["y"] = 40

        def branch(graph):
            graph["nodes"][1]["y"] = 300
            graph["edges"][0] = connect("edge", *graph["nodes"])

        def change(graph):
            graph["layout"]["change_outputs"] = {"applied": [{"edge_id": "other", "outpoint": "edge:0"}]}

        mutations = [detour, branch, change,
                     lambda graph: graph["edges"][0].update(routing_exception="return"),
                     lambda graph: graph["edges"][0].update(routing_exception="fee"),
                     lambda graph: graph["edges"][0].update(routing_exception="obstacle"),
                     lambda graph: graph["edges"][0].update(connector_shape="curved"),
                     lambda graph: graph["fee_items"].update(edge={"endpoint": "connectors"})]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                graph = small_jog_graph()
                mutate(graph)
                before = copy.deepcopy(graph["edges"])
                align_near_horizontal_endpoints(graph)
                self.assertEqual(graph["edges"], before)
                self.assertEqual(graph["layout"]["endpoint_alignment"]["applied"], 0)

    def test_new_node_intersection_rejects_batch(self):
        graph = small_jog_graph()
        graph["nodes"].append(node("obstacle", "transaction", 340, 100, size=10))
        before = copy.deepcopy(graph["edges"])
        self.assertEqual(layout_metrics(graph)["node_intersections"], 0)
        align_near_horizontal_endpoints(graph)
        self.assertEqual(graph["layout"]["endpoint_alignment"]["rejected"], 1)
        self.assertEqual(graph["edges"], before)

    def test_new_line_overlap_rejects_batch(self):
        graph = small_jog_graph()
        source, target = node("other-source", "transaction", 290, 95, 10), node("other-target", "transaction", 430, 95, 10)
        graph["nodes"].extend((source, target))
        edge = connect("other", source, target, start_y="100%", end_y="100%")
        edge["connector_shape"] = "straight"
        graph["edges"].append(edge)
        before = copy.deepcopy(graph["edges"])
        align_near_horizontal_endpoints(graph)
        self.assertEqual(graph["layout"]["endpoint_alignment"]["rejected"], 1)
        self.assertEqual(graph["edges"], before)

    def test_incomplete_checks_leave_all_edges_untouched(self):
        graph = small_jog_graph()
        before = copy.deepcopy(graph["edges"])
        with patch("liquid_tracer.elk_layout.layout_metrics", return_value={"truncated": True}):
            align_near_horizontal_endpoints(graph)
        self.assertEqual(graph["edges"], before)
        self.assertEqual(graph["layout"]["endpoint_alignment"]["reason"], "geometry_checks_truncated")

    def test_repeat_does_not_introduce_small_floating_point_jogs(self):
        graph = small_jog_graph()
        align_near_horizontal_endpoints(graph)
        edges = copy.deepcopy(graph["edges"])
        align_near_horizontal_endpoints(graph)
        self.assertEqual(graph["edges"], edges)
        self.assertEqual(graph["layout"]["endpoint_alignment"]["candidates"], 0)

    def test_full_optimizer_keeps_small_jog_flat_after_context_compaction(self):
        for policy in ("geometry", "traced_first"):
            with self.subTest(policy=policy):
                graph = small_jog_graph()
                before = copy.deepcopy(graph)
                with patch("liquid_tracer.elk_layout._worker", side_effect=lambda request, seeds, **_:
                           [worker_candidate(graph, request, seeds[0], policy)]):
                    result = optimize_graph(graph, "elbowed", layout_attempts=1)
                edge = result["edges"][0]
                self.assertEqual(graph, before)
                self.assertGreater(result["layout"]["branch_organization"]["context_inputs_moved"], 0)
                report = result["layout"]["endpoint_alignment"]
                self.assertEqual(report["applied"], 1)
                self.assertEqual(report["after_context_compaction"]["applied"], 1)
                self.assertAlmostEqual(edge["route"][0]["y"], edge["route"][-1]["y"], places=5)
                self.assertEqual(len(edge["route"]), 2)
                self.assertEqual(edge["attachment"]["endItem"]["position"], {"x": "0%", "y": "50%"})

    def test_full_optimizer_retains_multi_input_slots_for_both_port_policies(self):
        for policy in ("geometry", "traced_first"):
            with self.subTest(policy=policy):
                graph = small_jog_graph()
                target = graph["nodes"][1]
                target["y"] = 160
                graph["edges"][0] = connect("edge", graph["nodes"][0], target, end_y="10%")
                # A producing transaction makes the nearly horizontal input
                # a traced continuation, leaving context compaction separate.
                prior = node("prior", "transaction", -600, 120)
                graph["nodes"].append(prior)
                graph["edges"].append(connect("prior-edge", prior, graph["nodes"][0]))
                graph["edges"][-1]["outpoint"] = graph["edges"][0]["outpoint"]
                for index, y in enumerate((330, 520)):
                    context = node("context" + str(index), "address", 100, y)
                    graph["nodes"].append(context)
                    graph["edges"].append(connect("context-edge" + str(index), context, target,
                                                  end_y=("50%", "90%")[index]))
                expected = {edge["id"]: edge["attachment"]["endItem"] for edge in graph["edges"]
                            if edge["target"] == "tx"}
                with patch("liquid_tracer.elk_layout._worker", side_effect=lambda request, seeds, **_:
                           [worker_candidate(graph, request, seeds[0], policy)]):
                    result = optimize_graph(graph, "elbowed", layout_attempts=1)
                by_id = {edge["id"]: edge for edge in result["edges"]}
                self.assertEqual({key: by_id[key]["attachment"]["endItem"] for key in expected}, expected)
                self.assertAlmostEqual(by_id["edge"]["route"][0]["y"], by_id["edge"]["route"][-1]["y"], places=5)
                self.assertEqual(attachment_order_metrics(result)["endpoint_order_inversions"], 0)


if __name__ == "__main__":
    unittest.main()
