import copy
import json
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.common import TraceError, canonical, digest, read_json
from liquid_tracer.miro import make_plan, sync
from tests.test_miro_sync import FakeMiro, graph


TXID = "a" * 64
FEE_SHAPE = "event:" + TXID + ":1"
FEE_EDGE = "out:" + TXID + ":1"


def presented(run="one", extended=False, include_fees=False):
    value = graph(run, extended)
    value["layout"] = {"algorithm": "dependency_layers_v1", "direction": "left_to_right"}
    value["graph_options"] = {"include_fees": include_fees}
    value["fee_items"] = {
        FEE_SHAPE: {"endpoint": "shapes", "txid": TXID, "vout": 1},
        FEE_EDGE: {"endpoint": "connectors", "source": "tx:" + TXID, "target": FEE_SHAPE},
    }
    for node in value["nodes"]:
        if node["id"] == "tx:1":
            node["id"] = "tx:" + TXID
    for edge in value["edges"]:
        for field in ("source", "target"):
            if edge[field] == "tx:1":
                edge[field] = "tx:" + TXID
    if include_fees:
        value["nodes"].append({"id": FEE_SHAPE, "kind": "event", "label": "FEE\nvout 1", "color": "#ea94bb",
                               "x": 400, "y": -200, "width": 160, "height": 160})
        value["edges"].append({"id": FEE_EDGE, "source": "tx:" + TXID, "target": FEE_SHAPE,
                               "label": "vout 1", "quantity": "100 L-BTC", "role": "context_output"})
    return value


class DeletingMiro(FakeMiro):
    def __init__(self):
        super().__init__()
        self.lose_next_delete = False

    def __call__(self, method, url, headers, body, timeout):
        if method != "DELETE":
            return super().__call__(method, url, headers, body, timeout)
        self.calls.append((method, url, None))
        item_id = url.rsplit("/", 1)[-1]
        if item_id not in self.items:
            return 404, {}, b"{}"
        del self.items[item_id]
        if self.lose_next_delete:
            self.lose_next_delete = False
            raise TraceError("Synthetic lost DELETE response")
        return 204, {}, b""


class MiroLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "state.json"
        self.remote = DeletingMiro()

    def sync(self, graph_value=None, **kwargs):
        return sync(make_plan(graph_value or presented()), "board=", self.state_path,
                    token="test-token", transport=self.remote, interval=0, **kwargs)

    def item(self, key):
        return self.remote.items[read_json(self.state_path)["items"][key]["id"]]

    def test_continuation_stays_near_its_parent_and_preserves_unrelated_manual_layout(self):
        self.sync()
        self.item("addr:a")["position"].update({"x": 10000, "y": 875})
        self.item("addr:a")["geometry"].update({"width": 320, "height": 200})
        before = copy.deepcopy(self.item("addr:a"))
        self.sync(presented("two", extended=True))
        self.assertEqual(self.item("addr:a"), before)
        self.assertEqual(self.item("tx:2")["position"]["x"], 1200)
        self.assertEqual(self.item("addr:c")["position"]["x"], 1600)
        self.assertEqual(self.item("tx:2")["position"]["y"], self.item("addr:b")["position"]["y"])
        for method, _, payload in self.remote.writes:
            if method == "PATCH":
                self.assertNotIn("position", payload)
                self.assertNotIn("geometry", payload)

    def test_new_group_follows_moved_parent_and_avoids_rotated_obstacle(self):
        self.sync()
        self.item("addr:b")["position"].update({"x": 2800, "y": 1000})
        self.item("addr:a")["position"].update({"x": 3200, "y": 1000})
        self.item("addr:a")["geometry"].update({"width": 100, "height": 700, "rotation": 90})
        self.sync(presented("two", extended=True))
        tx = self.item("tx:2")
        self.assertEqual(tx["position"]["x"], 3200)
        self.assertGreater(tx["position"]["y"], 1000 + 50 + 80)
        self.assertEqual(self.item("addr:c")["position"]["y"], tx["position"]["y"])

    def test_reorganization_moves_shapes_without_resizing_or_removing_annotations(self):
        self.sync()
        node = self.item("addr:a")
        node["position"].update({"x": 10000, "y": 800})
        node["geometry"].update({"width": 700, "height": 220})
        node["data"]["content"] += "<p>Analyst observation</p>"
        node["style"]["fillColor"] = "#a855f7"
        geometry, data, style = copy.deepcopy(node["geometry"]), copy.deepcopy(node["data"]), copy.deepcopy(node["style"])
        original_transport = self.remote
        snapshots = []

        def inspect(method, url, headers, body, timeout):
            if method == "PATCH" and "position" in json.loads(body):
                snapshots.append(read_json(self.state_path)["layout_history"][-1])
            return original_transport(method, url, headers, body, timeout)

        report = sync(make_plan(presented()), "board=", self.state_path, token="test-token",
                      transport=inspect, interval=0, reorganize=True)
        self.assertGreater(report["moved"], 0)
        self.assertTrue(snapshots)
        changed = {row["key"]: row for row in report["layout_snapshot"]["positions"]}
        self.assertEqual(changed["addr:a"]["before"]["x"], 10000)
        self.assertEqual(self.item("addr:a")["geometry"], geometry)
        self.assertEqual(self.item("addr:a")["data"], data)
        self.assertEqual(self.item("addr:a")["style"], style)
        left, tx, right = [self.item(key) for key in ("addr:a", "tx:" + TXID, "addr:b")]
        self.assertGreater(tx["position"]["x"] - tx["geometry"]["width"] / 2,
                           left["position"]["x"] + left["geometry"]["width"] / 2)
        self.assertGreater(right["position"]["x"] - right["geometry"]["width"] / 2,
                           tx["position"]["x"] + tx["geometry"]["width"] / 2)
        for method, _, payload in self.remote.writes:
            if method == "PATCH":
                self.assertNotIn("geometry", payload)
        writes = len(self.remote.writes)
        repeated = self.sync(reorganize=True)
        self.assertEqual(repeated["moved"], 0)
        self.assertEqual(len(self.remote.writes), writes)

    def test_fee_visibility_removes_only_known_mapped_fees_and_readds_once(self):
        self.sync(presented(include_fees=True))
        initial = read_json(self.state_path)["items"]
        self.remote.items["manual-item"] = {"id": "manual-item", "data": {"content": "Private analyst note"}}
        report = self.sync(presented(include_fees=False))
        self.assertEqual(report["deleted"], 2)
        remaining = read_json(self.state_path)["items"]
        self.assertEqual(set(initial) - set(remaining), {FEE_SHAPE, FEE_EDGE})
        for key in remaining:
            self.assertEqual(initial[key]["id"], remaining[key]["id"])
        self.assertIn("manual-item", self.remote.items)
        deletes = [url for method, url, _ in self.remote.calls if method == "DELETE"]
        self.assertIn("/connectors/", deletes[0])
        self.assertIn("/shapes/", deletes[1])
        writes = len(self.remote.writes)
        self.assertEqual(self.sync()["deleted"], 0)
        self.assertEqual(len(self.remote.writes), writes)
        self.assertEqual(self.sync(presented(include_fees=True))["created"], 2)
        self.assertEqual(self.sync(presented(include_fees=True))["created"], 0)
        self.assertNotEqual(self.item(FEE_SHAPE)["id"], initial[FEE_SHAPE]["id"])

    def test_lost_delete_response_recovers_only_recorded_pending_items(self):
        self.sync(presented(include_fees=True))
        self.remote.lose_next_delete = True
        with self.assertRaisesRegex(TraceError, "lost DELETE response"):
            self.sync()
        pending = read_json(self.state_path)["pending_deletions"]
        self.assertTrue(pending[FEE_EDGE]["attempted"])
        self.assertFalse(pending[FEE_SHAPE]["attempted"])
        with self.assertRaisesRegex(TraceError, "Finish the interrupted Miro fee removal"):
            self.sync(presented(include_fees=True))
        report = self.sync()
        self.assertEqual(report["deleted"], 2)
        self.assertEqual(read_json(self.state_path)["pending_deletions"], {})
        self.assertNotIn(FEE_SHAPE, read_json(self.state_path)["items"])

    def test_queued_but_unattempted_delete_does_not_excuse_missing_shape(self):
        self.sync(presented(include_fees=True))
        self.remote.lose_next_delete = True
        with self.assertRaisesRegex(TraceError, "lost DELETE response"):
            self.sync()
        del self.remote.items[self.item(FEE_SHAPE)["id"]]
        before, writes = self.state_path.read_bytes(), len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "missing or inaccessible mapped items"):
            self.sync()
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(len(self.remote.writes), writes)

    def test_reorganization_keeps_fees_in_one_row_above_flow(self):
        value = presented(include_fees=True)
        second_shape, second_edge = "event:" + TXID + ":2", "out:" + TXID + ":2"
        value["fee_items"][second_shape] = {"endpoint": "shapes", "txid": TXID, "vout": 2}
        value["fee_items"][second_edge] = {"endpoint": "connectors", "source": "tx:" + TXID, "target": second_shape}
        value["nodes"].append(dict(value["nodes"][-1], id=second_shape, x=800, label="FEE\nvout 2"))
        value["edges"].append(dict(value["edges"][-1], id=second_edge, target=second_shape, label="vout 2"))
        self.sync(value)
        self.item(FEE_SHAPE)["geometry"]["width"] = 800
        self.item(FEE_SHAPE)["position"]["y"] = 500
        self.sync(value, reorganize=True)
        first, second = self.item(FEE_SHAPE), self.item(second_shape)
        self.assertEqual(first["position"]["y"], second["position"]["y"])
        self.assertLess(first["position"]["y"] + 80, self.item("tx:" + TXID)["position"]["y"] - 80)
        self.assertGreater(second["position"]["x"] - 80, first["position"]["x"] + 400)
        self.assertEqual(first["geometry"]["width"], 800)

    def test_missing_fee_without_attempted_deletion_still_aborts_preflight(self):
        self.sync(presented(include_fees=True))
        del self.remote.items[self.item(FEE_SHAPE)["id"]]
        writes, before = len(self.remote.writes), self.state_path.read_bytes()
        with self.assertRaisesRegex(TraceError, "missing or inaccessible mapped items"):
            self.sync()
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(len(self.remote.writes), writes)

    def test_manual_fee_caption_or_style_blocks_all_writes(self):
        for key, group, field in ((FEE_SHAPE, "data", "content"), (FEE_SHAPE, "style", "fillColor"),
                                  (FEE_EDGE, "captions", "content")):
            with self.subTest(key=key, field=field):
                self.sync(presented(include_fees=True))
                target = self.item(key)
                original = copy.deepcopy(target)
                if group == "captions":
                    target[group][0][field] = "Analyst note"
                else:
                    target[group][field] = "Analyst note"
                writes, before = len(self.remote.writes), self.state_path.read_bytes()
                with self.assertRaisesRegex(TraceError, "has manual edits"):
                    self.sync()
                self.assertEqual(self.state_path.read_bytes(), before)
                self.assertEqual(len(self.remote.writes), writes)
                self.remote.items[target["id"]] = original

    def test_forged_nonfee_deletion_metadata_is_rejected_without_network(self):
        self.sync()
        plan = make_plan(presented())
        plan["fee_items"]["addr:a"] = {"endpoint": "shapes", "txid": TXID, "vout": 1}
        plan["sha256"] = digest(canonical({key: value for key, value in plan.items() if key != "sha256"}))
        calls = len(self.remote.calls)
        with self.assertRaisesRegex(TraceError, "Invalid Miro fee shape proof"):
            sync(plan, "board=", self.state_path, token="test-token", transport=self.remote, interval=0)
        self.assertEqual(len(self.remote.calls), calls)

    def test_nonfee_mapped_connection_prevents_fee_shape_deletion(self):
        value = presented(include_fees=True)
        value["edges"].append({"id": "analyst-link", "source": "addr:a", "target": FEE_SHAPE,
                               "label": "Note connection", "quantity": "?? ??", "role": "context_output"})
        self.sync(value)
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "non-fee mapped connector"):
            self.sync()
        self.assertEqual(len(self.remote.writes), writes)

    def test_a_nonfee_event_cannot_be_reclassified_for_deletion(self):
        original = presented(include_fees=True)
        original["nodes"][-1]["label"] = "UNSPENDABLE\nvout 1"
        # A legacy plan knows the event shape but has no fee deletion metadata.
        del original["fee_items"]
        self.sync(original)
        calls = len(self.remote.calls)
        with self.assertRaisesRegex(TraceError, "refusing to remove a non-fee item"):
            self.sync()
        self.assertEqual(len(self.remote.calls), calls)

    def test_fee_preview_and_reorganization_preview_are_local_only(self):
        self.sync(presented(include_fees=True))
        calls, before = len(self.remote.calls), self.state_path.read_bytes()
        report = self.sync(dry_run=True, reorganize=True)
        self.assertEqual(report["fee_items_to_remove"], 2)
        self.assertTrue(report["reorganize"])
        self.assertEqual(len(self.remote.calls), calls)
        self.assertEqual(self.state_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
