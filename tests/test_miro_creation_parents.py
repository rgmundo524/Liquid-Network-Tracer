"""Frame autoattachment must not change creation requests or lose acknowledged IDs."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from liquid_tracer.common import TraceError, canonical, read_json, save_json
from liquid_tracer.miro import make_plan, sync
from liquid_tracer.miro_creation_parents import validate_creation_detaches
from liquid_tracer.miro_frames import activity_frames
from liquid_tracer.miro_state import load_state
from tests.test_miro_frame_sync import FrameMiro, framed_graph
from tests.test_miro_sync import graph


class AutoParentMiro(FrameMiro):
    """Simulate frame-relative creation responses, including unordered batches."""

    def __init__(self):
        super().__init__()
        self.parent_id = "external-frame"
        self.items[self.parent_id] = {
            "id": self.parent_id, "type": "frame", "data": {"title": "Untouched analyst frame"},
            "position": {"x": 100, "y": 200, "origin": "center", "relativeTo": "canvas_center"},
            "geometry": {"width": 200000, "height": 200000}, "style": {"fillColor": "#eeeeee"}}
        self.auto_parent = True
        self.detach_failure = None
        self.read_failure = None
        self.state_path = None
        self.max_children = 0
        self.parent_capacity = 5000

    def __call__(self, method, url, headers, body, timeout):
        path = urlsplit(url).path
        payload = json.loads(body) if body else None
        if method == "GET" and self.read_failure and path.endswith(self.read_failure):
            return 503, {}, b"{}"
        if method == "PATCH" and payload.get("parent") == {"id": None}:
            item_id = path.rsplit("/", 1)[-1]
            if self.state_path is not None:
                state = load_state(self.state_path)
                assert any(record["id"] == item_id for record in state["items"].values())
                assert any(entry["id"] == item_id for entry in state["pending_creation_detaches"].values())
                assert not state.get("pending_creations")
            failure, self.detach_failure = self.detach_failure, None
            if failure == "reject":
                return 500, {}, b"{}"
            result = super().__call__(method, url, headers, body, timeout)
            if failure == "lost":
                raise TraceError("Synthetic lost detach response")
            return result
        shapes = method == "POST" and (path.endswith("/items/bulk") or path.endswith("/shapes"))
        if shapes:
            entries = payload if isinstance(payload, list) else [payload]
            assert all("parent" not in entry for entry in entries)
            if self.auto_parent:
                children = sum((item.get("parent") or {}).get("id") == self.parent_id for item in self.items.values())
                assert children + len(entries) <= self.parent_capacity
        result = super().__call__(method, url, headers, body, timeout)
        if shapes and self.auto_parent:
            response = json.loads(result[2])
            items = response["data"] if isinstance(payload, list) else [response]
            frame = self.items[self.parent_id]
            for value in items:
                item = self.items[value["id"]]
                item["parent"] = {"id": self.parent_id}
                for axis, size in (("x", "width"), ("y", "height")):
                    item["position"][axis] -= frame["position"][axis] - frame["geometry"][size] / 2
                item["position"]["relativeTo"] = "parent_top_left"
                value.clear()
                value.update(copy.deepcopy(item))
            self.max_children = max(self.max_children, sum(
                (item.get("parent") or {}).get("id") == self.parent_id for item in self.items.values()))
            if isinstance(payload, list):
                response["data"].reverse()
            return result[0], result[1], canonical(response)
        return result


class CreationParentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.remote = AutoParentMiro()
        self.remote.state_path = self.path

    def sync(self, value=None, **kwargs):
        return sync(make_plan(value or graph()), "synthetic-board=", self.path,
                    token="synthetic-token", transport=kwargs.pop("transport", self.remote),
                    interval=0, max_items=1000, **kwargs)

    def shape_posts(self):
        return [(url, payload) for method, url, payload in self.remote.calls
                if method == "POST" and url.endswith(("/items/bulk", "/shapes"))]

    def test_initial_framed_graph_restores_old_wire_without_losing_batching(self):
        self.remote.auto_parent = False
        self.sync(framed_graph())
        posts = self.shape_posts()
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0][0].endswith("/items/bulk"))
        self.assertTrue(all("parent" not in item for item in posts[0][1]))
        self.assertEqual(read_json(self.path).get("pending_creation_detaches", {}), {})

    def test_external_frame_relative_reversed_bulk_is_matched_and_detached(self):
        frame = copy.deepcopy(self.remote.items[self.remote.parent_id])
        plan = make_plan(graph())
        self.sync()
        state = read_json(self.path)
        for entry in plan["shapes"]:
            item = self.remote.items[state["items"][entry["key"]]["id"]]
            self.assertEqual(item["data"]["content"], entry["body"]["data"]["content"])
            self.assertIsNone(item["parent"]["id"])
            self.assertEqual(item["position"]["relativeTo"], "canvas_center")
        self.assertEqual(self.remote.items[self.remote.parent_id], frame)
        self.assertEqual(state["pending_creation_detaches"], {})
        self.assertFalse(any(method in ("PATCH", "DELETE") and url.endswith("/frames/" + self.remote.parent_id)
                             for method, url, _ in self.remote.calls))

    def test_continuation_new_shapes_detach_from_existing_managed_frame(self):
        first = graph()
        first["activity_frames"] = activity_frames(first)
        self.remote.auto_parent = False
        self.sync(first)
        self.remote.parent_id = read_json(self.path)["items"]["frame:graph"]["id"]
        self.remote.auto_parent = True
        second = graph("two", True)
        second["activity_frames"] = activity_frames(second)
        report = self.sync(second)
        self.assertEqual(report["new_shapes"], 3)
        state = read_json(self.path)
        self.assertEqual(state["pending_creation_detaches"], {})
        for entry in state["items"].values():
            if entry["endpoint"] == "shapes":
                self.assertFalse((self.remote.items[entry["id"]].get("parent") or {}).get("id"))

    def test_individual_shape_mode_also_omits_parent_and_detaches(self):
        value = graph()
        save_json(self.path, {"schema_version": 2, "board_id": "synthetic-board=", "namespace": value["namespace"],
                              "items": {}, "runs": {}, "pending_creations": {}, "shape_batch_size": 1})
        self.sync(value)
        self.assertEqual(len(self.shape_posts()), len(make_plan(value)["shapes"]))
        self.assertTrue(all(url.endswith("/shapes") for url, _ in self.shape_posts()))
        self.assertEqual(read_json(self.path)["pending_creation_detaches"], {})

    def test_failed_detach_retains_all_acknowledged_ids_and_retry_does_not_recreate(self):
        self.remote.detach_failure = "reject"
        with self.assertRaisesRegex(TraceError, "detach returned HTTP 500"):
            self.sync()
        state = read_json(self.path)
        ids = {key: entry["id"] for key, entry in state["items"].items()}
        self.assertEqual(len(ids), len(make_plan(graph())["shapes"]))
        self.assertEqual(len(state["pending_creation_detaches"]), len(ids))
        self.assertEqual(state["pending_creations"], {})
        self.sync()
        self.assertEqual(len(self.shape_posts()), 1)
        for key, item_id in ids.items():
            self.assertEqual(read_json(self.path)["items"][key]["id"], item_id)
        self.assertEqual(read_json(self.path)["pending_creation_detaches"], {})

    def test_lost_successful_detach_response_is_adopted_without_repeating_patch(self):
        self.remote.detach_failure = "lost"
        with self.assertRaisesRegex(TraceError, "lost detach"):
            self.sync()
        failed_id = next(iter(read_json(self.path)["pending_creation_detaches"].values()))["id"]
        self.sync()
        self.assertEqual(len(self.shape_posts()), 1)
        patches = [url for method, url, _ in self.remote.calls if method == "PATCH" and url.endswith("/" + failed_id)]
        self.assertEqual(len(patches), 1)

    def test_manual_shape_or_frame_movement_blocks_retry_without_overwriting(self):
        for moved in ("shape", "frame"):
            with self.subTest(moved=moved):
                self.path = Path(self.temp.name) / (moved + ".json")
                self.remote = AutoParentMiro()
                self.remote.state_path = self.path
                self.remote.detach_failure = "reject"
                with self.assertRaises(TraceError):
                    self.sync()
                entry = next(iter(read_json(self.path)["pending_creation_detaches"].values()))
                item_id = entry["id"] if moved == "shape" else self.remote.parent_id
                self.remote.items[item_id]["position"]["x"] += 25
                before = copy.deepcopy(self.remote.items)
                writes = len(self.remote.writes)
                with self.assertRaisesRegex(TraceError, "was moved before detaching"):
                    self.sync()
                self.assertEqual(self.remote.items, before)
                self.assertEqual(len(self.remote.writes), writes)
                self.assertTrue(read_json(self.path)["pending_creation_detaches"])

    def test_detaches_each_batch_before_creating_more_children(self):
        value = graph()
        template = value["nodes"][0]
        value["nodes"] = [{**template, "id": f"address:{i}", "label": f"Synthetic {i}", "x": i * 300} for i in range(57)]
        value["edges"] = []
        self.remote.parent_capacity = 20
        self.sync(value)
        self.assertEqual([len(payload) for _, payload in self.shape_posts()], [20, 20, 19])
        self.assertEqual(self.remote.max_children, 20)
        self.assertEqual(read_json(self.path)["pending_creation_detaches"], {})

    def test_failed_parent_read_before_identity_matching_preserves_uncertain_batch(self):
        self.remote.read_failure = "/frames/" + self.remote.parent_id
        with self.assertRaisesRegex(TraceError, "parent read"):
            self.sync()
        state = read_json(self.path)
        self.assertEqual(state["items"], {})
        self.assertEqual(len(state["pending_creations"]), len(make_plan(graph())["shapes"]))
        self.assertEqual(len(self.shape_posts()), 1)

    def test_failed_shape_read_after_identity_matching_preserves_acknowledged_batch(self):
        self.remote.read_failure = "/shapes/remote-1"
        with self.assertRaisesRegex(TraceError, "parent read"):
            self.sync()
        state = read_json(self.path)
        self.assertEqual(state["pending_creations"], {})
        self.assertEqual(len(state["items"]), len(make_plan(graph())["shapes"]))
        self.assertTrue(state["pending_creation_detaches"])
        self.remote.read_failure = None
        self.sync()
        self.assertEqual(len(self.shape_posts()), 1)

    def test_invalid_detach_journal_is_rejected(self):
        self.remote.detach_failure = "reject"
        with self.assertRaises(TraceError):
            self.sync()
        state = read_json(self.path)
        for field, value in (("id", "another-shape"), ("parent_id", None), ("parent_id", "../other"),
                             ("position", {"x": float("nan"), "y": 0}), ("position", {"x": "0", "y": 0}),
                             ("position", {"x": True, "y": 0}), ("position", {"x": 10 ** 1000, "y": 0})):
            malformed = copy.deepcopy(state)
            next(iter(malformed["pending_creation_detaches"].values()))[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(TraceError, "Malformed.*detach"):
                validate_creation_detaches(malformed)

    def test_changed_parent_blocks_retry_without_mutating_other_frame(self):
        self.remote.detach_failure = "reject"
        with self.assertRaises(TraceError):
            self.sync()
        entry = next(iter(read_json(self.path)["pending_creation_detaches"].values()))
        self.remote.items[entry["id"]]["parent"] = {"id": "another-frame"}
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "moved to another parent"):
            self.sync()
        self.assertEqual(len(self.remote.writes), writes)
        self.assertTrue(read_json(self.path)["pending_creation_detaches"])

    def test_wrong_single_parent_relative_position_stays_uncertain(self):
        value = graph()
        save_json(self.path, {"schema_version": 2, "board_id": "synthetic-board=", "namespace": value["namespace"],
                              "items": {}, "runs": {}, "pending_creations": {}, "shape_batch_size": 1})
        def wrong_position(method, url, headers, body, timeout):
            result = self.remote(method, url, headers, body, timeout)
            if method == "POST" and url.endswith("/shapes"):
                response = json.loads(result[2])
                response["position"]["x"] += 1000
                return result[0], result[1], canonical(response)
            return result
        with self.assertRaisesRegex(TraceError, "unexpected canvas coordinates"):
            self.sync(value, transport=wrong_position)
        state = read_json(self.path)
        self.assertEqual(state["items"], {})
        self.assertEqual(len(state["pending_creations"]), 1)
        self.assertFalse(any(method == "PATCH" for method, _, _ in self.remote.calls))

    def test_invalid_parent_geometry_never_guesses_coordinates_or_detaches(self):
        for modification in ({"rotation": 15}, {"width": float("inf")}, {"width": 0}):
            with self.subTest(geometry=modification):
                self.path = Path(self.temp.name) / (str(len(self.remote.calls)) + "-invalid.json")
                self.remote = AutoParentMiro()
                self.remote.state_path = self.path
                self.remote.items[self.remote.parent_id]["geometry"].update(modification)
                with self.assertRaises(TraceError):
                    self.sync()
                state = read_json(self.path)
                self.assertEqual(state["items"], {})
                self.assertTrue(state["pending_creations"])
                self.assertFalse(any(method == "PATCH" for method, _, _ in self.remote.calls))
