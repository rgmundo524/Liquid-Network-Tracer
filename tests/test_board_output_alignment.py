"""Board additions align dependency stages without moving reviewed board objects."""

import copy
import tempfile
import unittest
from pathlib import Path

from liquid_tracer import board_layout
from liquid_tracer.common import read_json
from liquid_tracer.edge_labels import route_signature
from liquid_tracer.elk_layout import attachment_point, layout_metrics
from liquid_tracer.miro import make_plan, sync, validate_plan
from tests.test_board_layout import BoardMiro
from tests.test_board_projection_safety import scope_plan
from tests.test_elk_layout import HAS_ELK
from tests.test_miro_sync import graph


def staged_additions(*, shared=False):
    """A retained producer, a long-lived output, and a later detached branch."""
    source = graph("two")
    for column, node in enumerate(source["nodes"]):
        node["column"] = column
    source.update(fee_items={}, graph_options={})

    def node(key, kind, column, row, *, width=160, height=160):
        source["nodes"].append({"id": key, "kind": kind, "column": column,
            "x": column * 400, "y": row * 300, "width": width, "height": height,
            "label": "Synthetic " + key, "color": "#facc15", "details": {}})

    def edge(key, start, end, *, incoming=False):
        source["edges"].append({"id": key, "source": start, "target": end,
            "label": "vin 0" if incoming else "vout 0", "quantity": "amount confidential",
            "role": "traced_input" if incoming else "candidate_output"})

    node("addr:boundary", "address", 2, 1)
    edge("output:1:new", "tx:1", "addr:boundary")
    node("tx:early", "transaction", 1, 2)
    node("addr:early", "address", 2, 2, height=200)
    # arrange() can put a long-lived output midway between producer and spender.
    # Its producing stage must still determine the update's output column.
    node("addr:delayed", "address", 4, 3)
    node("event:early", "event", 2, 4, width=120, height=120)
    edge("output:early:0", "tx:early", "addr:early")
    edge("output:early:1", "tx:early", "addr:delayed")
    edge("output:early:2", "tx:early", "event:early")
    node("tx:middle", "transaction", 5, 3)
    node("addr:middle", "address", 6, 3)
    edge("input:middle:0", "addr:delayed", "tx:middle", incoming=True)
    source["edges"][-1]["outpoint"] = "early:1"
    next(edge for edge in source["edges"] if edge["id"] == "output:early:1")["outpoint"] = "early:1"
    # In the addition-only graph this is an input-only context node; its
    # retained producer must keep it in the output column after compaction.
    edge("input:middle:1", "addr:boundary", "tx:middle", incoming=True)
    source["edges"][-1]["role"] = "context_input"
    edge("output:middle:0", "tx:middle", "addr:middle")
    node("tx:late", "transaction", 9, 6)
    node("addr:late-a", "address", 10, 6)
    node("addr:late-b", "address", 10, 7, width=200)
    edge("output:late:0", "tx:late", "addr:late-a")
    edge("output:late:1", "tx:late", "addr:late-b")
    if shared:
        node("addr:shared", "address", 5, 5)
        edge("output:early:shared", "tx:early", "addr:shared")
        edge("output:middle:shared", "tx:middle", "addr:shared")
    return source


@unittest.skipUnless(HAS_ELK, "local ELK package is not installed")
class BoardOutputAlignmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.json"
        self.remote = BoardMiro()
        self.first = scope_plan(make_plan(graph()))
        self.sync(self.first)

    def sync(self, plan):
        return sync(plan, "test-board", self.path, token="synthetic-token",
                    transport=self.remote, interval=0)

    def item(self, key):
        return self.remote.items[read_json(self.path)["items"][key]["id"]]

    def prepare(self, source):
        snapshot = board_layout.capture("test-board", self.path, self.first["namespace"],
            token="synthetic-token", transport=self.remote, interval=0)
        result = board_layout.prepare_graph(source, snapshot,
            connector_style="elbowed", layout_attempts=2)
        return result, snapshot

    def assert_geometry(self, result, snapshot):
        nodes = {node["id"]: node for node in result["nodes"]}
        new_ids = set(result["board_layout"]["new_node_ids"])
        self.assertEqual(layout_metrics(result)["node_overlaps"], 0)
        for key in new_ids:
            self.assertGreaterEqual(nodes[key]["x"] - nodes[key]["width"] / 2,
                                    snapshot["bounds"][2] + board_layout.GAP - .01)
        internal = [edge for edge in result["edges"]
                    if edge["source"] in new_ids and edge["target"] in new_ids]
        self.assertTrue(internal)
        labels = 0
        for edge in internal:
            with self.subTest(edge=edge["id"]):
                self.assertTrue(edge.get("route"))
                for endpoint, field, logical in ((0, "startItem", "source"), (-1, "endItem", "target")):
                    expected = attachment_point(nodes[edge[logical]], edge["attachment"][field])
                    for axis in ("x", "y"):
                        self.assertAlmostEqual(edge["route"][endpoint][axis], expected[axis], places=3)
                if edge.get("label_layout"):
                    labels += 1
                    signature = route_signature([(point["x"], point["y"]) for point in edge["route"]])
                    self.assertEqual(edge["label_layout"]["route_signature"], signature)
        self.assertGreater(labels, 0)

    def test_outputs_align_by_producer_stage_across_boundary_and_detached_additions(self):
        self.item("addr:b")["position"].update(x=1800, y=-900)
        self.item("addr:b")["geometry"].update(width=220, height=180)
        retained = {key: copy.deepcopy(self.item(key)) for key in ("addr:a", "tx:1", "addr:b")}
        source = staged_additions()
        before = copy.deepcopy(source)
        result, snapshot = self.prepare(source)
        self.assertEqual(source, before)
        nodes = {node["id"]: node for node in result["nodes"]}
        early = ["addr:boundary", "addr:early", "addr:delayed", "event:early"]
        self.assertEqual(len({round(nodes[key]["x"], 3) for key in early}), 1)
        self.assertAlmostEqual(nodes["addr:late-a"]["x"], nodes["addr:late-b"]["x"], places=3)
        self.assertLess(nodes["addr:early"]["x"], nodes["addr:middle"]["x"])
        self.assertLess(nodes["addr:middle"]["x"], nodes["addr:late-a"]["x"])
        self.assert_geometry(result, snapshot)
        plan = scope_plan(make_plan(result))
        validate_plan(plan)
        self.sync(plan)
        for key, body in retained.items():
            with self.subTest(retained=key):
                self.assertEqual(self.item(key)["position"], body["position"])
                self.assertEqual(self.item(key)["geometry"], body["geometry"])
        self.assertEqual(self.item("output:1:new")["startItem"]["id"], retained["tx:1"]["id"])
        for key in early:
            self.assertAlmostEqual(self.item(key)["position"]["x"], nodes[key]["x"], places=3)

    def test_multistage_reused_address_remains_single_and_keeps_every_relationship(self):
        source = staged_additions(shared=True)
        before = copy.deepcopy(source)
        result, snapshot = self.prepare(source)
        self.assertEqual(source, before)
        self.assertEqual(sum(node["id"] == "addr:shared" for node in result["nodes"]), 1)
        self.assertEqual({node["id"] for node in result["nodes"]}, {node["id"] for node in source["nodes"]})
        topology = lambda value: {(edge["id"], edge["source"], edge["target"]) for edge in value["edges"]}
        self.assertEqual(topology(result), topology(source))
        nodes = {node["id"]: node for node in result["nodes"]}
        # A reused address cannot occupy both producing stages. Other outputs
        # must still align, without duplicating the shared address to do so.
        self.assertEqual(len({round(nodes[key]["x"], 3)
            for key in ("addr:boundary", "addr:early", "addr:delayed", "event:early")}), 1)
        self.assert_geometry(result, snapshot)
        plan = scope_plan(make_plan(result))
        validate_plan(plan)
        self.sync(plan)
        shared = self.item("addr:shared")["id"]
        self.assertEqual(self.item("output:early:shared")["endItem"]["id"], shared)
        self.assertEqual(self.item("output:middle:shared")["endItem"]["id"], shared)

    def test_retained_hub_keeps_verified_dependency_resets_for_new_spenders(self):
        from tests.test_hub_layout import busy_hub_graph, node_id

        source = busy_hub_graph()
        before = copy.deepcopy(source)
        hub = next(node for node in source["nodes"] if node.get("layout_hub"))
        snapshot = {
            "mapped": {hub["id"]: {"id": "retained-hub", "endpoint": "shapes"}},
            "geometry": {"retained-hub": [450., -300., 220., 180.]},
            "bounds": [340., -390., 560., -210.],
        }
        result = board_layout.prepare_graph(source, snapshot,
            connector_style="elbowed", layout_attempts=1)
        nodes = {node["id"]: node for node in result["nodes"]}
        self.assertEqual(source, before)
        self.assertEqual([nodes[hub["id"]][field] for field in ("x", "y", "width", "height")],
                         snapshot["geometry"]["retained-hub"])
        self.assertNotIn(hub["id"], result["board_layout"]["new_node_ids"])
        self.assertEqual(len({round(nodes[node_id(index)]["x"], 3) for index in range(7)}), 1)
        payments = [node for node in result["nodes"]
                    if node.get("details", {}).get("address", "").startswith("SYNTHETIC-payment-")]
        self.assertEqual(len(payments), 7)
        self.assertEqual(len({round(node["x"], 3) for node in payments}), 1)
        self.assertTrue(all(node["x"] > nodes[node_id(0)]["x"] for node in payments))
        topology = lambda value: {(edge["id"], edge["source"], edge["target"]) for edge in value["edges"]}
        self.assertEqual(topology(result), topology(source))
        self.assert_geometry(result, snapshot)
