import copy
import json
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.common import TraceError, canonical, digest, read_json
from liquid_tracer.miro import make_plan, publish, resolve, sync, validate_plan
from liquid_tracer.miro_frames import activity_frames
from tests.test_miro_layout import DeletingMiro
from tests.test_miro_sync import graph


def framed_graph(run="one", merged=False):
    value = graph(run)
    value["nodes"], value["edges"] = [], []
    for branch in range(1, 4):
        tree = graph()
        rename = {node["id"]: node["id"] + f":branch{branch}" for node in tree["nodes"]}
        for node in tree["nodes"]:
            node["id"] = rename[node["id"]]
            node["y"] += branch * 800
            if node["kind"] == "transaction":
                node["role"] = "starting_transaction"
            value["nodes"].append(node)
        for edge in tree["edges"]:
            edge.update({"id": edge["id"] + f":branch{branch}",
                         "source": rename[edge["source"]], "target": rename[edge["target"]]})
            value["edges"].append(edge)
    if merged:
        value["edges"].append({"id": "merge:branches1:2", "source": "addr:b:branch1", "target": "tx:1:branch2",
                               "label": "vin 1", "quantity": "??", "role": "traced_input"})
    value["activity_frames"] = activity_frames(value)
    return value


def frame_keys(state):
    return {key for key, record in state["items"].items() if record["endpoint"] == "frames"}


def contains(frame, item):
    return all(abs(frame["position"][axis] - item["position"][axis]) + item["geometry"][size] / 2
               < frame["geometry"][size] / 2 for axis, size in (("x", "width"), ("y", "height")))


class FrameMiro(DeletingMiro):
    """The current frame contract, including direct children enumeration."""

    def __call__(self, method, url, headers, body, timeout):
        from urllib.parse import parse_qs, urlsplit
        parsed = urlsplit(url)
        if method == "GET" and parsed.path.endswith("/items"):
            self.calls.append((method, url, None))
            parent = parse_qs(parsed.query)["parent_item_id"][0]
            children = [copy.deepcopy(item) for item in self.items.values()
                        if (item.get("parent") or {}).get("id") == parent]
            return 200, {}, canonical({"type": "cursor-list", "data": children, "total": len(children)})
        result = super().__call__(method, url, headers, body, timeout)
        payload = json.loads(body) if body else {}
        if method == "PATCH" and payload.get("parent") == {"id": None} and 200 <= result[0] < 300:
            item = self.items[parsed.path.rsplit("/", 1)[-1]]
            item["position"]["relativeTo"] = "canvas_center"
            return result[0], result[1], canonical(item)
        if method == "POST" and parsed.path.endswith("/frames") and 200 <= result[0] < 300:
            response = json.loads(result[2])
            self.items[response["id"]]["type"] = "frame"
            return result[0], result[1], canonical(self.items[response["id"]])
        return result


class MiroFrameSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.remote = FrameMiro()

    def sync(self, value=None, **kwargs):
        return sync(make_plan(value or framed_graph()), "board=", self.path, token="synthetic-token",
                    transport=kwargs.pop("transport", self.remote), interval=0, **kwargs)

    def state(self):
        return read_json(self.path)

    def item(self, key):
        return self.remote.items[self.state()["items"][key]["id"]]

    def test_three_trees_create_four_frames_then_merge_to_three(self):
        value = framed_graph()
        initial = self.sync(value)
        self.assertEqual(initial["new_frames"], 4)
        state = self.state()
        self.assertEqual(len(frame_keys(state)), 4)
        original_ids = {key: record["id"] for key, record in state["items"].items()}
        self.remote.items["manual-frame"] = {"id": "manual-frame", "type": "frame", "data": {"title": "Analyst notes"}}
        for activity in value["activity_frames"]["activities"]:
            frame = self.item(activity["key"])
            for key in activity["shape_keys"]:
                self.assertTrue(contains(frame, self.item(key)))
            self.assertTrue(contains(self.item("frame:graph"), frame))
        report = self.sync(framed_graph("two", merged=True))
        current = self.state()
        self.assertEqual(len(frame_keys(current)), 3)
        self.assertEqual((report["frames_to_remove"], report["deleted"], report["new_frames"]), (1, 1, 0))
        self.assertIn("manual-frame", self.remote.items)
        for key, item_id in original_ids.items():
            if key in current["items"]:
                self.assertEqual(current["items"][key]["id"], item_id)
        deleted = [url for method, url, _ in self.remote.calls if method == "DELETE"]
        self.assertEqual(len(deleted), 1)
        self.assertIn("/frames/", deleted[0])
        self.assertTrue(contains(self.item("frame:graph"), self.item("run:one")))
        self.assertTrue(contains(self.item("frame:graph"), self.item("run:two")))
        writes = len(self.remote.writes)
        repeated = self.sync(framed_graph("two", merged=True), max_items=0)
        self.assertEqual((repeated["created"], repeated["updated"], repeated["deleted"]), (0, 0, 0))
        self.assertEqual(len(self.remote.writes), writes)

    def test_existing_graph_acquires_frames_without_replotting_shapes(self):
        old = framed_graph()
        del old["activity_frames"]
        self.sync(old)
        previous = copy.deepcopy(self.state()["items"])
        report = self.sync()
        self.assertEqual((report["created"], report["new_shapes"], report["new_connectors"]), (4, 0, 0))
        self.assertEqual(len(frame_keys(self.state())), 4)
        for key, record in previous.items():
            self.assertEqual(self.state()["items"][key], record)

    def test_manual_shapes_and_frame_titles_survive_automatic_bounds_refresh(self):
        value = framed_graph()
        self.sync(value)
        activity = value["activity_frames"]["activities"][0]
        shape = self.item(activity["shape_keys"][0])
        shape["position"].update({"x": 50000, "y": 8000})
        shape["geometry"].update({"width": 900, "height": 400, "rotation": 20})
        frame = self.item(activity["key"])
        frame["data"]["title"] = "Reviewed activity"
        frame["style"]["fillColor"] = "#ffffffff"
        before = copy.deepcopy(shape)
        report = self.sync(value)
        self.assertEqual(shape, before)
        self.assertEqual(frame["data"]["title"], "Reviewed activity")
        self.assertTrue(contains(frame, shape))
        self.assertGreater(report["updated"], 0)
        self.assertTrue(any(conflict["field"] == "data.title" for conflict in report["conflicts"]))

    def test_frame_budget_is_checked_before_network(self):
        plan = make_plan(framed_graph())
        count = len(plan["shapes"]) + len(plan["connectors"]) + 4
        with self.assertRaisesRegex(TraceError, "above max-items"):
            self.sync(max_items=count - 1)
        self.assertEqual(self.remote.calls, [])
        self.assertFalse(self.path.exists())

    def test_frame_plan_tampering_is_rejected_before_network(self):
        plan = make_plan(framed_graph())
        plan["frames"][0]["body"]["geometry"]["width"] = 1
        plan["sha256"] = digest(canonical({key: value for key, value in plan.items() if key != "sha256"}))
        with self.assertRaisesRegex(TraceError, "frame geometry disagrees"):
            validate_plan(plan)

    def test_lost_frame_post_requires_explicit_reconciliation_without_duplicate(self):
        lost = [False]

        def transport(method, url, headers, body, timeout):
            result = self.remote(method, url, headers, body, timeout)
            if method == "POST" and url.endswith("/frames") and not lost[0]:
                lost[0] = True
                raise TraceError("Synthetic frame POST response lost")
            return result

        with self.assertRaisesRegex(TraceError, "frame POST response lost"):
            self.sync(transport=transport)
        pending = self.state()["pending_creations"]
        self.assertEqual(list(pending), ["frame:graph"])
        self.assertEqual(pending["frame:graph"]["endpoint"], "frames")
        with self.assertRaisesRegex(TraceError, "miro-resolve --key"):
            self.sync()
        created = [item for item in self.remote.items.values() if item.get("data", {}).get("title") == "Liquid UTXO trace · Complete graph"]
        self.assertEqual(len(created), 1)
        resolve(self.path, item_id=created[0]["id"], key="frame:graph")
        self.sync()
        self.assertEqual(len(frame_keys(self.state())), 4)
        self.assertEqual(len([item for item in self.remote.items.values() if item.get("type") == "frame"]), 4)

    def test_lost_frame_patch_is_observed_and_not_repeated(self):
        self.sync()
        key = framed_graph()["activity_frames"]["activities"][0]["shape_keys"][0]
        self.item(key)["position"]["x"] += 20000
        lost = [False]

        def transport(method, url, headers, body, timeout):
            result = self.remote(method, url, headers, body, timeout)
            if method == "PATCH" and "/frames/" in url and not lost[0]:
                lost[0] = True
                raise TraceError("Synthetic frame PATCH response lost")
            return result

        with self.assertRaisesRegex(TraceError, "frame PATCH response lost"):
            self.sync(transport=transport)
        acknowledged = {entry["id"] for entry in self.state()["pending_updates"].values()}
        self.assertTrue(acknowledged)
        self.remote.calls.clear()
        self.sync()
        patches = [url.rsplit("/", 1)[-1] for method, url, _ in self.remote.calls if method == "PATCH"]
        self.assertTrue(acknowledged.isdisjoint(patches))
        self.assertEqual(self.state()["pending_updates"], {})

    def test_lost_frame_delete_recovers_only_attempted_removal(self):
        self.sync()
        self.remote.lose_next_delete = True
        with self.assertRaisesRegex(TraceError, "lost DELETE response"):
            self.sync(framed_graph("two", merged=True))
        pending = self.state()["pending_frame_deletions"]
        self.assertEqual(len(pending), 1)
        self.assertTrue(next(iter(pending.values()))["attempted"])
        self.sync(framed_graph("two", merged=True))
        self.assertEqual(self.state()["pending_frame_deletions"], {})
        self.assertEqual(len(frame_keys(self.state())), 3)

    def test_unexplained_missing_frame_aborts_without_writes(self):
        self.sync()
        del self.remote.items[self.state()["items"]["frame:graph"]["id"]]
        before, writes = self.path.read_bytes(), len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "missing or inaccessible"):
            self.sync()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.remote.writes), writes)

    def attach(self, key, frame_key="frame:graph"):
        item, frame = self.item(key), self.item(frame_key)
        expected = {axis: item["position"][axis] for axis in ("x", "y")}
        item["parent"] = {"id": frame["id"]}
        item["position"] = {"relativeTo": "parent_top_left", "origin": "center",
                            "x": expected["x"] - frame["position"]["x"] + frame["geometry"]["width"] / 2,
                            "y": expected["y"] - frame["position"]["y"] + frame["geometry"]["height"] / 2}
        return expected

    def test_attached_shapes_are_detached_before_frames_move(self):
        value = framed_graph()
        self.sync(value)
        activity = value["activity_frames"]["activities"][0]
        key = activity["shape_keys"][0]
        expected = self.attach(key, activity["key"])
        frame = self.item(activity["key"])
        frame["position"]["x"] += 120
        expected["x"] += 120
        self.remote.calls.clear()
        self.sync(value)
        item = self.item(key)
        self.assertEqual(item["parent"], {"id": None})
        self.assertEqual({axis: item["position"][axis] for axis in ("x", "y")}, expected)
        writes = self.remote.writes
        detach = next(i for i, (method, _, body) in enumerate(writes) if method == "PATCH" and body.get("parent") == {"id": None})
        frame_updates = [i for i, (method, url, _) in enumerate(writes) if method == "PATCH" and "/frames/" in url]
        self.assertTrue(frame_updates)
        self.assertTrue(all(detach < index for index in frame_updates))

    def test_old_run_note_is_detached_before_continuation_resizes_outer_frame(self):
        self.sync()
        expected = self.attach("run:one")
        self.sync(framed_graph("two", merged=True))
        note = self.item("run:one")
        self.assertEqual(note["parent"], {"id": None})
        self.assertEqual({axis: note["position"][axis] for axis in ("x", "y")}, expected)
        self.assertTrue(contains(self.item("frame:graph"), note))
        for method, url, body in self.remote.calls:
            if method == "POST" and url.endswith(("/shapes", "/bulk")):
                for item in body if isinstance(body, list) else [body]:
                    self.assertNotIn("parent", item)

    def test_unmanaged_attached_note_stops_before_any_writes(self):
        self.sync()
        frame_id = self.item("frame:graph")["id"]
        self.remote.items["manual-note"] = {"id": "manual-note", "type": "text", "parent": {"id": frame_id},
                                             "position": {"x": 50, "y": 50, "relativeTo": "parent_top_left"}}
        before, writes = self.path.read_bytes(), len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "unmanaged|attached|outside"):
            self.sync(framed_graph("two", merged=True))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.remote.writes), writes)
        self.assertIn("manual-note", self.remote.items)

    def test_unmanaged_note_in_unchanged_frame_does_not_block_noop_sync(self):
        self.sync()
        self.remote.items["manual-note"] = {"id": "manual-note", "type": "text",
                                             "parent": {"id": self.item("frame:graph")["id"]}}
        writes = len(self.remote.writes)
        report = self.sync(max_items=0)
        self.assertEqual((report["created"], report["updated"], report["deleted"]), (0, 0, 0))
        self.assertEqual(len(self.remote.writes), writes)

    def test_lost_detach_response_preserves_position_on_retry(self):
        self.sync()
        key = "addr:a:branch1"
        expected = self.attach(key)
        self.item("frame:graph")["position"]["x"] += 50000
        expected["x"] += 50000
        lost = [False]

        def transport(method, url, headers, body, timeout):
            result = self.remote(method, url, headers, body, timeout)
            if method == "PATCH" and json.loads(body).get("parent") == {"id": None} and not lost[0]:
                lost[0] = True
                raise TraceError("Synthetic detach response lost")
            return result

        with self.assertRaisesRegex(TraceError, "detach response lost"):
            self.sync(transport=transport)
        self.remote.calls.clear()
        self.sync()
        self.assertEqual({axis: self.item(key)["position"][axis] for axis in ("x", "y")}, expected)
        self.assertFalse(any(method == "PATCH" and body.get("parent") == {"id": None}
                             for method, _, body in self.remote.writes))

    def test_legacy_publish_includes_export_frames(self):
        plan = make_plan(framed_graph())
        report = publish(plan, "board=", self.path, token="synthetic-token", transport=self.remote, interval=0)
        self.assertEqual(report["items"], len(plan["shapes"]) + len(plan["connectors"]) + 4)
        self.assertEqual(len([item for item in self.remote.items.values() if item.get("type") == "frame"]), 4)
        writes = len(self.remote.writes)
        publish(plan, "board=", self.path, token="synthetic-token", transport=self.remote, interval=0)
        self.assertEqual(len(self.remote.writes), writes)
