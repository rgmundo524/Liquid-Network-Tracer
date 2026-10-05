"""Conflicts identify generated objects and differences without writing to Miro."""

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import main
from liquid_tracer.common import TraceError, read_json
from liquid_tracer.context_groups import group_context_inputs
from liquid_tracer.export import build_graph
from liquid_tracer.miro import make_plan, sync
from liquid_tracer.miro_conflicts import (MAX_REPORT_BYTES, MiroEditConflict, editable_report,
                                          from_error, object_url, print_report, public_report)
from tests.test_board_projection_safety import projection_plan
from tests.test_input_order import child_input, input_order_state
from tests.test_presentation_annotations import AnnotationMiro


def report_fixture():
    return {"kind": "miro_edit_conflicts", "board_id": "test-board=", "items": [
        {"key": "addr:test", "item_id": "remote-1", "kind": "shape", "changes": [
            {"field": "data.content", "saved": {"present": True, "type": "string", "value": "Original"},
             "current": {"present": True, "type": "string", "value": "Edited"}}]}]}


class ConflictTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "miro.json"
        self.remote = AnnotationMiro()

    def sync(self, plan):
        return sync(plan, "test-board", self.path, token="synthetic-token", interval=0,
                    transport=self.remote, reorganize=True)

    def test_grouping_lists_changed_shapes_and_connector_caption_before_any_write(self):
        graph = build_graph(input_order_state())
        graph.pop("activity_frames", None)
        graph["run"]["ancestor_runs"] = []
        plain, grouped = make_plan(graph), make_plan(group_context_inputs(graph, enabled=True))
        self.sync(plain)
        mapping = read_json(self.path)["items"]
        group = next(iter(grouped["context_group_items"].values()))
        member = next(iter(group["members"]))
        self.remote.items[mapping[member]["id"]]["data"]["content"] += "<p>Accidental note</p>"
        edge = child_input(0)
        self.remote.items[mapping[edge]["id"]]["captions"][0]["content"] = "Accidental label"
        before = copy.deepcopy(self.remote.items)
        writes = len(self.remote.writes)
        with self.assertRaises(MiroEditConflict) as caught:
            self.sync(grouped)
        items = {item["key"]: item for item in caught.exception.report["items"]}
        self.assertEqual(set(items), {member, edge})
        self.assertEqual(items[member]["changes"][0]["field"], "data.content")
        self.assertTrue(items[member]["changes"][0]["current"]["value"].endswith("<p>Accidental note</p>"))
        self.assertEqual(items[edge]["changes"][0]["field"], "captions[0].content")
        self.assertEqual(items[member]["item_id"], mapping[member]["id"])
        self.assertEqual(items[member]["object_url"], object_url("test-board", mapping[member]["id"]))
        self.assertEqual(before, self.remote.items)
        self.assertEqual(writes, len(self.remote.writes))

    def test_projection_lists_all_edited_obsolete_objects_without_writes(self):
        self.sync(projection_plan(extended=True))
        mapping = read_json(self.path)["items"]
        for key in ("addr:c", "tx:2"):
            self.remote.items[mapping[key]["id"]]["style"]["fillColor"] = "#123456"
        before = len(self.remote.writes)
        with self.assertRaises(MiroEditConflict) as caught:
            self.sync(projection_plan("two"))
        self.assertEqual({item["key"] for item in caught.exception.report["items"]}, {"addr:c", "tx:2"})
        self.assertEqual(before, len(self.remote.writes))

    def test_missing_and_null_remain_distinguishable_and_normalized_styles_stay_unchanged(self):
        state = {"board_id": "b", "items": {"a": {"id": "1", "endpoint": "shapes", "managed": {
            "data": {"content": None}, "style": {"fillColor": "#ABCDEF", "fontSize": "12"}}}}}
        report = editable_report(state, {"a": {"style": {"fillColor": "#abcdef", "fontSize": 12}}}, {"a": {}})
        changes = report["items"][0]["changes"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["saved"], {"present": True, "value": "null", "type": "null", "truncated": False})
        self.assertEqual(changes[0]["current"], {"present": False})

    def test_invalid_diagnostic_metadata_never_bypasses_manual_edit_protection(self):
        from liquid_tracer.context_group_miro import check_remote
        for key, item_id in (("a" * 300, "1"), ("a", "unsupported/item")):
            with self.subTest(key=key, item_id=item_id):
                state = {"board_id": "b", "items": {key: {"id": item_id, "endpoint": "shapes", "managed": {"data": {"content": "Original"}}}}}
                remote = {key: {"data": {"content": "Edited"}}}
                with self.assertRaisesRegex(TraceError, "manual edits"):
                    check_remote(state, remote, {key: {"endpoint": "shapes", "shape": "circle"}}, {})

    def test_long_content_excerpts_show_change_beyond_prefix(self):
        saved = "same text " * 500 + "ORIGINAL"
        current = "same text " * 500 + "ACCIDENTAL"
        state = {"board_id": "b", "items": {"a": {"id": "1", "endpoint": "shapes", "managed": {"data": {"content": saved}}}}}
        report = editable_report(state, {"a": {"data": {"content": current}}}, {"a": {}})
        change = report["items"][0]["changes"][0]
        self.assertIn("ORIGINAL", change["saved"]["value"])
        self.assertIn("ACCIDENTAL", change["current"]["value"])
        self.assertIn("Excerpt starting", change["current"]["value"])
        self.assertTrue(change["saved"]["truncated"])

    def test_report_bounds_drop_unknown_fields_and_untrusted_link(self):
        report = report_fixture()
        report["secret"] = "omit"
        report["items"][0]["object_url"] = "javascript:alert(1)"
        report["items"][0]["notes"] = "omit"
        for side in ("saved", "current"):
            report["items"][0]["changes"][0][side]["value"] = "😀" * 4000
        report["items"][0]["changes"] *= 30
        report["items"] *= 30
        clean = public_report(report)
        self.assertTrue(clean["truncated"])
        self.assertLessEqual(len(clean["items"]), 20)
        self.assertLessEqual(len(json.dumps(clean, ensure_ascii=True).encode()), MAX_REPORT_BYTES)
        self.assertNotIn("secret", clean)
        self.assertNotIn("notes", clean["items"][0])
        self.assertEqual(clean["items"][0]["object_url"], "https://miro.com/app/board/test-board%3D/?moveToWidget=remote-1")
        self.assertEqual(public_report(clean), clean)

    def test_invalid_reports_cannot_supply_external_urls_or_arbitrary_payloads(self):
        for board in ("https://evil.example", "b/../../x", "a\n", None):
            report = report_fixture()
            report["board_id"] = board
            self.assertIsNone(public_report(report))
        report = report_fixture()
        report["items"][0]["changes"][0]["current"]["value"] = {"secret": "no"}
        self.assertIsNone(public_report(report))
        self.assertIsNone(public_report({"kind": "anything_else"}))

    def test_cli_preserves_typed_diagnostics_through_wrapped_errors(self):
        conflict = MiroEditConflict("Manual edits", report_fixture())
        try:
            raise conflict
        except MiroEditConflict as cause:
            try:
                raise TraceError("Saved layout remains available") from cause
            except TraceError as wrapper:
                failure = wrapper
        received, stderr = [], io.StringIO()
        with patch("liquid_tracer.cli.sync_run", side_effect=failure), contextlib.redirect_stderr(stderr):
            code = main(["miro-sync", "--case", str(self.path.parent)], diagnostics=received.append)
        self.assertEqual(code, 1)
        self.assertEqual(received, [public_report(report_fixture())])
        self.assertIn("Open object: https://miro.com/", stderr.getvalue())
        self.assertIn("data.content", stderr.getvalue())
        self.assertIn('Last synced: "Original"', stderr.getvalue())
        self.assertIn('Current Miro: "Edited"', stderr.getvalue())
        arbitrary = TraceError("Ordinary failure")
        arbitrary.report = report_fixture()
        self.assertIsNone(from_error(arbitrary))

    def test_terminal_quotes_remote_control_characters(self):
        report = report_fixture()
        report["items"][0]["changes"][0]["current"]["value"] = "\x1b[31munsafe\ntext"
        output = io.StringIO()
        print_report(report, file=output)
        self.assertNotIn("\x1b", output.getvalue())
        self.assertIn(r"\u001b", output.getvalue())


if __name__ == "__main__":
    unittest.main()
