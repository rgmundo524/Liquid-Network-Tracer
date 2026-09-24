"""Independent safety checks for retiring a maintained board's filtered objects."""

import copy
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.common import TraceError, canonical, digest, read_json
from liquid_tracer.miro import make_plan, sync
from tests.test_miro_sync import graph
from tests.test_presentation_annotations import AnnotationMiro


BOARD_RECORD = "board-" + "1" * 32


def projection_plan(run="one", extended=False, *, record=BOARD_RECORD, goal="pegouts"):
    source = graph(run, extended)
    return scope_plan(make_plan(source), record=record, goal=goal)


def scope_plan(plan, *, record=BOARD_RECORD, goal="full"):
    plan = copy.deepcopy(plan)
    plan["namespace"]["case_id"] += ":" + record
    plan["board_projection"] = {"version": 1, "record_id": record, "goal": goal}
    plan["sha256"] = digest(canonical({key: value for key, value in plan.items() if key != "sha256"}))
    return plan


class ProjectionSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "miro.json"
        self.remote = AnnotationMiro()
        self.initial = projection_plan(extended=True)
        self.reduced = projection_plan("two")

    def sync(self, plan, **options):
        return sync(plan, "test-board", self.path, token="synthetic-token", interval=0,
                    transport=self.remote, **options)

    def item(self, key):
        return self.remote.items[read_json(self.path)["items"][key]["id"]]

    def assert_blocked_without_writes(self, plan=None):
        writes = len(self.remote.writes)
        before = copy.deepcopy(self.remote.items)
        with self.assertRaises(TraceError):
            self.sync(plan or self.reduced)
        self.assertEqual(len(self.remote.writes), writes)
        self.assertEqual(self.remote.items, before)

    def test_shrinking_preserves_unrelated_objects_and_retained_ids(self):
        self.sync(self.initial)
        before = read_json(self.path)["items"]
        self.remote.items["analyst-note"] = {
            "id": "analyst-note", "type": "shape", "data": {"content": "Keep this note"}}
        self.remote.items["unrelated-link"] = {
            "id": "unrelated-link", "type": "connector",
            "startItem": {"id": "analyst-note"}, "endItem": {"id": before["addr:a"]["id"]}}
        unrelated = {key: copy.deepcopy(self.remote.items[key])
                     for key in ("analyst-note", "unrelated-link")}
        report = self.sync(self.reduced)
        self.assertEqual(report["deleted"], 4)
        after = read_json(self.path)["items"]
        for key, record in after.items():
            self.assertEqual(record["id"], before[key]["id"])
        self.assertFalse({"tx:2", "addr:c", "input:2:0", "output:2:0"}.intersection(after))
        for key, body in unrelated.items():
            self.assertEqual(self.remote.items[key], body)
        writes = len(self.remote.writes)
        self.sync(self.reduced)
        self.assertEqual(len(self.remote.writes), writes)

    def test_all_obsolete_connectors_are_deleted_before_shapes(self):
        self.sync(self.initial)
        mapping = read_json(self.path)["items"]
        endpoint_by_id = {record["id"]: record["endpoint"] for record in mapping.values()}
        self.sync(self.reduced)
        deleted = [endpoint_by_id[url.rsplit("/", 1)[-1]]
                   for method, url, _ in self.remote.writes if method == "DELETE"]
        self.assertEqual(deleted, ["connectors", "connectors", "shapes", "shapes"])

    def test_manual_obsolete_shape_content_blocks_every_write(self):
        self.sync(self.initial)
        self.item("addr:c")["data"]["content"] += "<p>Analyst finding</p>"
        self.assert_blocked_without_writes()

    def test_manually_moved_obsolete_shape_blocks_every_write(self):
        self.sync(self.initial)
        self.item("addr:c")["position"].update(x=2345, y=-456)
        self.assert_blocked_without_writes()

    def test_manual_obsolete_shape_geometry_and_type_block_every_write(self):
        self.sync(self.initial)
        body = self.item("addr:c")
        original = copy.deepcopy(body)
        for change in ("width", "rotation", "shape", "parent"):
            with self.subTest(change=change):
                body.clear()
                body.update(copy.deepcopy(original))
                if change == "width":
                    body["geometry"]["width"] += 30
                elif change == "rotation":
                    body["rotation"] = 45
                elif change == "shape":
                    body["data"]["shape"] = "rectangle"
                else:
                    body["parent"] = {"id": "manual-frame"}
                self.assert_blocked_without_writes()

    def test_manual_obsolete_connector_caption_blocks_every_write(self):
        self.sync(self.initial)
        self.item("output:2:0")["captions"][0]["content"] = "Analyst link annotation"
        self.assert_blocked_without_writes()

    def test_manual_obsolete_connector_style_blocks_every_write(self):
        self.sync(self.initial)
        self.item("output:2:0")["style"]["strokeColor"] = "#123456"
        self.assert_blocked_without_writes()

    def test_manual_obsolete_connector_routing_blocks_every_write(self):
        self.sync(self.initial)
        body = self.item("output:2:0")
        body["shape"] = "straight" if body["shape"] != "straight" else "elbowed"
        self.assert_blocked_without_writes()

    def test_manual_obsolete_connector_ports_block_every_write(self):
        self.sync(self.initial)
        self.item("output:2:0")["startItem"] = {
            "id": self.item("tx:2")["id"], "position": {"x": "50%", "y": "0%"}}
        self.assert_blocked_without_writes()

    def test_unmanaged_attachment_blocks_every_write(self):
        self.sync(self.initial)
        self.remote.items["manual-link"] = {
            "id": "manual-link", "type": "connector",
            "startItem": {"id": self.item("addr:c")["id"]},
            "endItem": {"id": self.item("addr:a")["id"]}}
        self.assert_blocked_without_writes()

    def test_obsolete_connector_reattachment_blocks_every_write(self):
        self.sync(self.initial)
        self.item("output:2:0")["endItem"]["id"] = self.item("addr:a")["id"]
        self.assert_blocked_without_writes()

    def test_interrupted_delete_resumes_same_projection_without_orphans(self):
        self.sync(self.initial)
        self.remote.lose_delete = True
        with self.assertRaisesRegex(TraceError, "lost"):
            self.sync(self.reduced)
        pending = read_json(self.path)["pending_deletions"]
        self.assertTrue(pending)
        self.assertTrue(any(item["attempted"] for item in pending.values()))
        self.assert_blocked_without_writes(self.initial)
        self.sync(self.reduced)
        state = read_json(self.path)
        self.assertFalse(state["pending_deletions"])
        self.assertEqual(set(self.remote.items), {item["id"] for item in state["items"].values()})
        writes = len(self.remote.writes)
        self.sync(self.reduced)
        self.assertEqual(len(self.remote.writes), writes)

    def test_acknowledged_reorganization_can_later_retire_updated_connectors(self):
        from tests.test_elk_miro import elk_graph

        source = elk_graph(extended=True)
        first = scope_plan(make_plan(source), goal="pegouts")
        edge = next(edge for edge in source["edges"] if edge["id"] == "output:2:0")
        edge["connector_shape"] = "elbowed"
        edge["attachment"]["startItem"]["position"]["y"] = "25%"
        organized = scope_plan(make_plan(source), goal="pegouts")
        self.sync(first)
        self.sync(organized, reorganize=True)
        record = read_json(self.path)["items"]["output:2:0"]
        self.assertEqual(record["projection_proof"]["routing"]["shape"], "elbowed")
        self.assertEqual(record["projection_proof"]["routing"]["startItem"]["position"]["y"], "25%")
        self.sync(self.reduced)
        self.assertNotIn("output:2:0", read_json(self.path)["items"])

    def test_different_board_projection_cannot_reuse_mapping(self):
        self.sync(self.initial)
        self.assert_blocked_without_writes(projection_plan("two", record="board-" + "2" * 32))

    def test_different_goal_cannot_retire_original_projection(self):
        self.sync(self.initial)
        self.assert_blocked_without_writes(projection_plan("two", goal="connections"))

    def test_registered_full_board_can_group_and_restore_context_addresses(self):
        from liquid_tracer.context_groups import group_context_inputs
        from liquid_tracer.export import build_graph
        from tests.test_input_order import input_order_state

        source = build_graph(input_order_state())
        source.pop("activity_frames", None)
        source["run"]["ancestor_runs"] = []
        plain = scope_plan(make_plan(source))
        grouped = scope_plan(make_plan(group_context_inputs(source, enabled=True)))
        group_key = next(iter(grouped["context_group_items"]))
        self.sync(plain, reorganize=True)
        self.sync(grouped, reorganize=True)
        self.assertIn(group_key, read_json(self.path)["items"])
        self.sync(plain, reorganize=True)
        self.assertNotIn(group_key, read_json(self.path)["items"])
        mapping = read_json(self.path)["items"]
        self.assertEqual(set(self.remote.items), {item["id"] for item in mapping.values()})

    def test_filter_can_retire_a_complete_context_branch(self):
        from liquid_tracer.context_groups import group_context_inputs
        from liquid_tracer.export import build_graph
        from tests.test_input_order import input_order_state

        source = build_graph(input_order_state())
        source.pop("activity_frames", None)
        source["run"]["ancestor_runs"] = []
        grouped = scope_plan(make_plan(group_context_inputs(source, enabled=True)))
        empty = {key: copy.deepcopy(grouped[key])
                 for key in ("schema_version", "namespace", "run", "run_id", "board_projection")}
        empty.update(shapes=[], connectors=[])
        empty["sha256"] = digest(canonical(empty))
        self.sync(grouped)
        self.sync(empty)
        self.assertFalse(self.remote.items)
        self.assertFalse(read_json(self.path)["items"])
        writes = len(self.remote.writes)
        self.sync(empty)
        self.assertEqual(len(self.remote.writes), writes)


if __name__ == "__main__":
    unittest.main()
