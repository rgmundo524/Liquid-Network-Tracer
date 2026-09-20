"""Finishing frames is independent of graph publication and layout."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.miro import make_plan, sync, sync_frames
from tests.test_miro_frame_sync import FrameMiro, framed_graph


class SeparateMiroFramesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.remote = FrameMiro()
        self.plan = make_plan(framed_graph())

    def apply(self, operation=sync, plan=None, **kwargs):
        return operation(plan or self.plan, "board=", self.path, token="synthetic-token",
                         transport=kwargs.pop("transport", self.remote), interval=0, **kwargs)

    def test_graph_sync_uses_only_graph_item_budget_and_saves_completed_snapshot(self):
        expected = len(self.plan["shapes"]) + len(self.plan["connectors"])
        result = self.apply(max_items=expected)
        self.assertEqual(result["new_items"], expected)
        self.assertEqual(result["new_frames"], 0)
        self.assertFalse(any("/frames" in url for _, url, _ in self.remote.calls))
        state = read_json(self.path)
        self.assertEqual(state["frame_plan"], self.plan)
        self.assertEqual(state["latest_run_id"], self.plan["run_id"])
        self.assertIsNone(state["active_run_id"])

    def test_frame_action_requires_a_completed_graph_before_network(self):
        with self.assertRaisesRegex(TraceError, "Sync this graph completely"):
            self.apply(sync_frames)
        self.assertEqual(self.remote.calls, [])
        self.assertFalse(self.path.exists())

    def test_framing_reads_live_geometry_without_graph_mutations_or_lineage_changes(self):
        self.apply()
        state = read_json(self.path)
        shape_id = state["items"]["addr:a:branch1"]["id"]
        shape = self.remote.items[shape_id]
        shape["position"].update({"x": 20000, "y": 9000})
        shape["geometry"].update({"width": 750, "height": 430, "rotation": 20})
        shape["data"]["content"] = "Analyst label"
        shape["style"]["fillColor"] = "#123456"
        graph_objects = copy.deepcopy(self.remote.items)
        self.remote.calls.clear()
        events = []
        with patch("liquid_tracer.miro._placements", side_effect=AssertionError("must not reorganize")):
            report = self.apply(sync_frames, progress=events.append)
        self.assertEqual(report["created_frames"], 4)
        self.assertEqual(report["new_shapes"], 0)
        self.assertEqual(report["new_connectors"], 0)
        for item_id, before in graph_objects.items():
            self.assertEqual(self.remote.items[item_id], before)
        after = read_json(self.path)
        for key in ("runs", "latest_run_id", "active_run_id", "frame_plan"):
            self.assertEqual(after[key], state[key])
        self.assertEqual(after["frame_summary"]["run_id"], self.plan["run_id"])
        self.assertTrue(all("/frames" in url for method, url, _ in self.remote.calls if method != "GET"))
        self.assertFalse(any(event["phase"] in ("layout", "creating", "removing") for event in events))

    def test_regular_sync_leaves_existing_frames_stale_until_requested(self):
        self.apply()
        self.apply(sync_frames)
        state = read_json(self.path)
        frame_ids = {record["id"] for record in state["items"].values() if record["endpoint"] == "frames"}
        frames = {item_id: copy.deepcopy(self.remote.items[item_id]) for item_id in frame_ids}
        self.remote.items[state["items"]["addr:a:branch1"]["id"]]["position"]["x"] += 30000
        self.remote.calls.clear()
        self.apply()
        self.assertFalse(any(method != "GET" and "/frames" in url for method, url, _ in self.remote.calls))
        self.assertEqual({item_id: self.remote.items[item_id] for item_id in frame_ids}, frames)
        self.apply(sync_frames)
        self.assertNotEqual({item_id: self.remote.items[item_id] for item_id in frame_ids}, frames)

    def test_frame_update_counts_exclude_shape_detaches(self):
        self.apply()
        self.apply(sync_frames)
        state = read_json(self.path)
        shape = self.remote.items[state["items"]["addr:a:branch1"]["id"]]
        frame = self.remote.items[state["items"]["frame:graph"]["id"]]
        original = copy.deepcopy(shape["position"])
        shape["parent"] = {"id": frame["id"]}
        shape["position"] = {"origin": "center", "relativeTo": "parent_top_left", **{
            axis: original[axis] - frame["position"][axis] + frame["geometry"][size] / 2
            for axis, size in (("x", "width"), ("y", "height"))}}
        frame["position"]["x"] += 25000
        self.remote.calls.clear()
        result = self.apply(sync_frames)
        frame_updates = sum(method == "PATCH" and "/frames/" in url for method, url, _ in self.remote.calls)
        self.assertEqual(result["updated_frames"], frame_updates)
        self.assertEqual(result["updated"], frame_updates + 1)
        self.assertEqual(shape["parent"], {"id": None})
        self.assertEqual(shape["position"]["x"], original["x"] + 25000)

    def test_frame_budget_and_dry_run_do_not_call_network(self):
        self.apply()
        self.remote.calls.clear()
        with self.assertRaisesRegex(TraceError, "above max-items=3"):
            self.apply(sync_frames, max_items=3)
        report = self.apply(sync_frames, dry_run=True, max_items=4)
        self.assertEqual(report["new_items"], 4)
        self.assertEqual(self.remote.calls, [])

    def test_older_or_changed_plan_cannot_frame_latest_graph(self):
        self.apply()
        newer = make_plan(framed_graph("two", merged=True))
        self.apply(plan=newer)
        self.remote.calls.clear()
        with self.assertRaises(TraceError):
            self.apply(sync_frames)
        self.assertEqual(self.remote.calls, [])
        changed = framed_graph("two", merged=True)
        changed["nodes"][0]["label"] = "Unsynced edit"
        with self.assertRaisesRegex(TraceError, "Sync this graph completely"):
            self.apply(sync_frames, plan=make_plan(changed))
        self.assertEqual(self.remote.calls, [])

    def test_earlier_same_run_display_plan_cannot_frame_latest_publication(self):
        self.apply()
        changed = framed_graph()
        changed["nodes"][0]["label"] = "New synced attribution"
        latest = make_plan(changed)
        self.apply(plan=latest)
        self.remote.calls.clear()
        with self.assertRaisesRegex(TraceError, "Sync this graph completely"):
            self.apply(sync_frames)
        self.assertEqual(self.remote.calls, [])
        self.apply(sync_frames, plan=latest)

    def test_missing_graph_mapping_cannot_be_created_by_framing(self):
        self.apply()
        state = read_json(self.path)
        state["items"].pop("addr:a:branch1")
        save_json(self.path, state)
        self.remote.calls.clear()
        with self.assertRaisesRegex(TraceError, "Sync every graph object"):
            self.apply(sync_frames)
        self.assertEqual(self.remote.calls, [])

    def test_legacy_lost_frame_patch_transfers_journal_without_frame_writes(self):
        self.apply()
        self.apply(sync_frames)
        current = read_json(self.path)
        self.remote.items[current["items"]["addr:a:branch1"]["id"]]["position"]["x"] += 20000
        lost = [False]

        def lose_patch(method, url, headers, body, timeout):
            result = self.remote(method, url, headers, body, timeout)
            if method == "PATCH" and "/frames/" in url and not lost[0]:
                lost[0] = True
                raise TraceError("Lost legacy frame PATCH response")
            return result

        with self.assertRaisesRegex(TraceError, "Lost legacy frame PATCH"):
            self.apply(sync_frames, transport=lose_patch)
        legacy = read_json(self.path)
        legacy["active_run_id"] = legacy.pop("active_frame_run_id")
        legacy.pop("frame_plan")
        legacy["runs"] = {}
        legacy["latest_run_id"] = None
        save_json(self.path, legacy)
        pending = copy.deepcopy(legacy["pending_updates"])
        self.assertTrue(pending)
        self.remote.calls.clear()
        self.apply()
        after = read_json(self.path)
        self.assertEqual(after["pending_updates"], pending)
        self.assertEqual(after["active_frame_run_id"], self.plan["run_id"])
        self.assertIsNone(after["active_run_id"])
        self.assertFalse(any(method != "GET" and "/frames/" in url for method, url, _ in self.remote.calls))
        self.remote.calls.clear()
        self.apply(sync_frames)
        self.assertFalse(any(method == "PATCH" and "/frames/" in url for method, url, _ in self.remote.calls))
        self.assertEqual(read_json(self.path)["pending_updates"], {})

    def test_legacy_lost_frame_delete_transfers_missing_frame_proof_without_delete(self):
        self.apply()
        self.apply(sync_frames)
        first = read_json(self.path)
        newer = make_plan(framed_graph("two", merged=True))
        self.apply(plan=newer)
        self.remote.lose_next_delete = True
        with self.assertRaisesRegex(TraceError, "lost DELETE response"):
            self.apply(sync_frames, plan=newer)
        legacy = read_json(self.path)
        legacy["active_run_id"] = legacy.pop("active_frame_run_id")
        legacy.pop("frame_plan")
        legacy["runs"] = first["runs"]
        legacy["latest_run_id"] = first["latest_run_id"]
        save_json(self.path, legacy)
        pending = copy.deepcopy(legacy["pending_frame_deletions"])
        self.assertTrue(pending)
        self.remote.calls.clear()
        self.apply(plan=newer)
        after = read_json(self.path)
        self.assertEqual(after["pending_frame_deletions"], pending)
        self.assertEqual(after["active_frame_run_id"], "two")
        self.assertEqual(after["latest_run_id"], "two")
        self.assertFalse(any(method != "GET" and "/frames/" in url for method, url, _ in self.remote.calls))
        self.apply(sync_frames, plan=newer)
        self.assertEqual(read_json(self.path)["pending_frame_deletions"], {})
        self.assertEqual(sum(record["endpoint"] == "frames" for record in read_json(self.path)["items"].values()), 3)

    def test_legacy_wrong_frame_deletion_proof_blocks_before_network(self):
        self.apply()
        self.apply(sync_frames)
        state = read_json(self.path)
        frame = state["items"]["frame:graph"]
        state["active_run_id"] = self.plan["run_id"]
        state["pending_frame_deletions"] = {"frame:graph": {
            "id": frame["id"], "proof": frame["frame_proof"], "attempted": True}}
        save_json(self.path, state)
        self.remote.calls.clear()
        with self.assertRaisesRegex(TraceError, "Finish the interrupted Miro frame update"):
            self.apply()
        self.assertEqual(self.remote.calls, [])

    def test_interrupted_frame_action_blocks_graph_sync_until_resumed(self):
        self.apply()
        state = read_json(self.path)
        state["active_frame_run_id"] = self.plan["run_id"]
        save_json(self.path, state)
        self.remote.calls.clear()
        with self.assertRaisesRegex(TraceError, "Finish the interrupted Create or update frames"):
            self.apply()
        self.assertEqual(self.remote.calls, [])
        self.apply(sync_frames)
        self.assertIsNone(read_json(self.path)["active_frame_run_id"])
        self.apply()
