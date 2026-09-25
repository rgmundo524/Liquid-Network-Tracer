"""Retire historical run summaries without losing graph edits or run history."""
import copy
import html
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import refresh_presentation
from liquid_tracer.common import TraceError, canonical, digest, read_json, save_json
from liquid_tracer.export import build_graph
from liquid_tracer.miro import _bounds, _record_pending, make_plan, publish, sync, validate_plan
from liquid_tracer.miro_frames import frame_bodies
from tests.test_attribution_convergence import graph_state
from tests.test_miro_sync import graph
from tests.test_presentation_annotations import AnnotationMiro


def legacy_note(run_id):
    return {"key": "run:" + run_id, "body": {
        "data": {"shape": "rectangle", "content": "<p>Run: " + html.escape(run_id) + "<br>status: complete</p>"},
        "position": {"x": 700, "y": -480, "origin": "center"},
        "geometry": {"width": 1300, "height": 280},
        "style": {"fillColor": "#e0f2fe", "fontSize": "14", "textAlign": "left"}}}


def archived_plan(value):
    plan = make_plan(value)
    plan["shapes"].append(legacy_note(plan["run_id"]))
    plan["presentation_version"] = 20
    if "activity_frames" in plan:
        plan["frames"] = frame_bodies(plan["activity_frames"], {
            item["key"]: _bounds(item["body"], item["key"]) for item in plan["shapes"]})
    plan["sha256"] = digest(canonical({k: v for k, v in plan.items() if k != "sha256"}))
    validate_plan(plan)
    return plan


class RunNoteCleanupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "miro.json"
        self.remote = AnnotationMiro()

    def sync(self, plan, **kwargs):
        return sync(plan, "synthetic-board", self.path, token="test-token",
                    transport=self.remote, interval=0, **kwargs)

    def add_old_note(self, run_id):
        # Historical mappings store content/style, not shape geometry or a
        # dedicated annotation proof. Reproduce that original record format.
        item = legacy_note(run_id)
        item_id = "legacy-" + run_id
        remote = {**copy.deepcopy(item["body"]), "id": item_id, "type": "shape"}
        self.remote.items[item_id] = remote
        state = read_json(self.path)
        state["items"][item["key"]] = _record_pending(
            {**item, "endpoint": "shapes"}, item_id, remote)
        save_json(self.path, state)
        return item_id

    def test_cleanup_all_old_runs_preserves_graph_edits_history_and_ids(self):
        self.sync(make_plan(graph()))
        plan = make_plan(graph("two", True))
        self.sync(plan)
        self.add_old_note("one")
        self.add_old_note("two")
        state = read_json(self.path)
        node = self.remote.items[state["items"]["addr:a"]["id"]]
        node["position"]["y"] = 4000
        node["data"]["content"] = "Investigator note"
        retained = {key: copy.deepcopy(value) for key, value in self.remote.items.items()
                    if not key.startswith("legacy-")}
        preview = self.sync(plan, dry_run=True, max_items=0)
        self.assertEqual(preview["run_notes_to_remove"], 2)
        self.assertEqual(preview["fee_items_to_remove"], 0)
        self.assertIn("legacy-one", self.remote.items)
        report = self.sync(plan, max_items=0)
        self.assertEqual((report["created"], report["deleted"]), (0, 2))
        self.assertEqual(self.remote.items, retained)
        after = read_json(self.path)
        self.assertEqual(set(after["runs"]), set(state["runs"]))
        for run_id in state["runs"]:
            for field in ("first_synced_at", "plan_sha256s"):
                self.assertEqual(after["runs"][run_id][field], state["runs"][run_id][field])
        self.assertFalse(any(key.startswith("run:") for key in after["items"]))
        writes = len(self.remote.writes)
        self.sync(plan, max_items=0)
        self.assertEqual(len(self.remote.writes), writes)

    def test_edited_run_summary_blocks_cleanup_before_writes(self):
        plan = make_plan(graph())
        self.sync(plan)
        item_id = self.add_old_note("one")
        self.remote.items[item_id]["data"]["content"] += "<p>Analyst evidence</p>"
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "manual edits"):
            self.sync(plan)
        self.assertEqual(len(self.remote.writes), writes)

    def test_unmanaged_connector_to_run_summary_blocks_cleanup(self):
        plan = make_plan(graph())
        self.sync(plan)
        item_id = self.add_old_note("one")
        self.remote.items["manual"] = {"id": "manual", "type": "connector",
            "startItem": {"id": item_id}, "endItem": {"id": "unrelated"}}
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "connector attaches"):
            self.sync(plan)
        self.assertEqual(len(self.remote.writes), writes)

    def test_lost_delete_response_resumes_without_recreating_note(self):
        plan = make_plan(graph())
        self.sync(plan)
        self.add_old_note("one")
        self.remote.lose_delete = True
        with self.assertRaisesRegex(TraceError, "lost"):
            self.sync(plan)
        self.assertTrue(read_json(self.path)["pending_deletions"])
        self.sync(plan, max_items=0)
        state = read_json(self.path)
        self.assertFalse(state["pending_deletions"])
        self.assertNotIn("run:one", state["items"])
        self.assertNotIn("legacy-one", self.remote.items)

    def test_interrupted_note_update_reconciles_success_but_preserves_manual_edit(self):
        for outcome in ("applied", "not_applied", "manual"):
            with self.subTest(outcome=outcome):
                self.path = self.path.with_name(outcome + ".json")
                self.remote = AnnotationMiro()
                plan = make_plan(graph())
                self.sync(plan)
                item_id = self.add_old_note("one")
                state = read_json(self.path)
                content = "<p>Run: one<br>status: completed</p>"
                state["pending_updates"] = {"run:one": {"id": item_id, "endpoint": "shapes",
                    "patch": {"data": {"content": content}}}}
                state["active_run_id"] = "one"
                save_json(self.path, state)
                if outcome != "not_applied":
                    self.remote.items[item_id]["data"]["content"] = content if outcome == "applied" else "Analyst note"
                if outcome == "manual":
                    before, writes = self.path.read_bytes(), len(self.remote.writes)
                    with self.assertRaisesRegex(TraceError, "manual edits"):
                        self.sync(plan)
                    self.assertEqual(self.path.read_bytes(), before)
                    self.assertEqual(len(self.remote.writes), writes)
                else:
                    self.sync(plan, max_items=0)
                    self.assertNotIn(item_id, self.remote.items)
                    self.assertFalse(read_json(self.path)["pending_updates"])

    def test_already_deleted_run_summary_is_forgotten_but_missing_graph_is_not(self):
        plan = make_plan(graph())
        self.sync(plan)
        item_id = self.add_old_note("one")
        del self.remote.items[item_id]
        self.sync(plan, max_items=0)
        self.assertNotIn("run:one", read_json(self.path)["items"])
        node_id = read_json(self.path)["items"]["addr:a"]["id"]
        del self.remote.items[node_id]
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "missing or inaccessible"):
            self.sync(plan)
        self.assertEqual(len(self.remote.writes), writes)

    def test_unproven_mapping_is_not_deleted(self):
        plan = make_plan(graph())
        self.sync(plan)
        self.add_old_note("one")
        state = read_json(self.path)
        state["items"]["run:one"]["intent"]["data"]["content"] = "Unrelated item"
        save_json(self.path, state)
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "generated identity"):
            self.sync(plan)
        self.assertEqual(len(self.remote.writes), writes)

    def test_explicit_old_plan_cannot_recreate_run_summary(self):
        old = archived_plan(graph())
        for operation in (sync, publish):
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(TraceError, "retired run summaries"):
                    operation(old, "synthetic-board", self.path, token="test-token",
                              transport=self.remote, interval=0)
        self.assertEqual(self.remote.calls, [])

    def test_refresh_old_archive_drops_only_note_without_retracing_or_rewriting(self):
        evidence = graph_state((("a:0", "b"), ("b:0", "c")))
        old = archived_plan(build_graph(evidence))
        trace = self.path.parent / "trace.json"
        save_json(trace, evidence)
        before = trace.read_bytes()
        with patch("liquid_tracer.cli.Esplora", side_effect=AssertionError("Must not retrace")), \
                patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda value, **kwargs: value):
            current = refresh_presentation(old, trace)
        self.assertFalse(any(item["key"].startswith("run:") for item in current["shapes"]))
        self.assertEqual(current["run"], old["run"])
        self.assertEqual(current["connectors"], old["connectors"])
        self.assertEqual(trace.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
