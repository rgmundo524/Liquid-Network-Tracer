"""Empty caption representation must not hide real edits or clear partial PATCHes."""

import copy
import unittest

from liquid_tracer.miro import _editable, _merge_fields, _recover_updates
from liquid_tracer.miro_conflicts import editable_report


def record(captions=None):
    fields = {"captions": captions if captions is not None else [],
              "style": {"fontSize": "11", "strokeColor": "#123456", "strokeWidth": "2"}}
    return {"id": "connector-1", "endpoint": "connectors",
            "managed": copy.deepcopy(fields), "intent": copy.deepcopy(fields)}


class ConnectorComparisonTests(unittest.TestCase):
    def test_omitted_empty_fields_are_equal_without_mutating_input_or_saved_state(self):
        saved = record()
        body = {"style": {"strokeColor": "#123456", "strokeWidth": "2"}}
        original = copy.deepcopy((saved, body))
        state = {"board_id": "board", "items": {"edge": saved}}
        self.assertIsNone(editable_report(state, {"edge": body}, {"edge": {}}))
        patch, _, _, conflicts = _merge_fields(saved, saved["intent"], body, "edge")
        self.assertEqual((patch, conflicts), ({}, []))
        self.assertEqual((saved, body), original)

    def test_missing_stroke_fields_style_dict_and_explicit_font_edits_remain_conflicts(self):
        for body in ({}, {"style": {}}, {"style": {"strokeColor": "#123456"}},
                     {"style": {"strokeColor": "#123456", "strokeWidth": "2", "fontSize": "19"}},
                     {"style": {"strokeColor": "#123456", "strokeWidth": "2", "fontSize": None}}):
            with self.subTest(body=body):
                state = {"board_id": "board", "items": {"edge": record()}}
                self.assertIsNotNone(editable_report(state, {"edge": body}, {"edge": {}}))

    def test_new_generated_caption_includes_font_when_server_omitted_unused_font(self):
        saved = record()
        desired = copy.deepcopy(saved["intent"])
        desired["captions"] = [{"content": "New amount", "position": "50%"}]
        body = {"style": {"strokeColor": "#123456", "strokeWidth": "2"}}
        patch, _, _, conflicts = _merge_fields(saved, desired, body, "edge")
        self.assertEqual(conflicts, [])
        self.assertEqual(patch, {"captions": desired["captions"], "style": {"fontSize": "11"}})

    def test_partial_style_patch_never_acknowledges_a_missing_nonempty_caption(self):
        saved = record([{"content": "Original label", "position": "50%"}])
        state = {"items": {"edge": saved}, "pending_updates": {
            "edge": {"patch": {"style": {"strokeColor": "#654321"}}}}}
        body = {"style": {"strokeColor": "#654321", "strokeWidth": "2"}}
        self.assertNotIn("captions", _editable(state["pending_updates"]["edge"]["patch"], "connectors"))
        _recover_updates(state, {"edge": body})
        self.assertEqual(saved["managed"]["captions"][0]["content"], "Original label")
        report = editable_report({"board_id": "board", **state}, {"edge": body}, {"edge": {}})
        self.assertIn("captions", [change["field"] for change in report["items"][0]["changes"]])

    def test_new_caption_never_guesses_a_font_without_a_saved_font_baseline(self):
        saved = record()
        desired = copy.deepcopy(saved["intent"])
        desired["captions"] = [{"content": "New amount", "position": "50%"}]
        del saved["managed"]["style"]["fontSize"]
        body = {"style": {"strokeColor": "#123456", "strokeWidth": "2"}}
        patch, _, _, conflicts = _merge_fields(saved, desired, body, "edge")
        self.assertEqual(patch, {"captions": desired["captions"]})
        self.assertEqual([conflict["field"] for conflict in conflicts], ["style.fontSize"])

    def test_pending_clear_caption_can_acknowledge_omission_without_replaying(self):
        saved = record([{"content": "Old generated label", "position": "50%"}])
        state = {"items": {"edge": saved}, "pending_updates": {"edge": {"patch": {"captions": []}}}}
        body = {"style": {"strokeColor": "#123456", "strokeWidth": "2"}}
        _recover_updates(state, {"edge": body})
        self.assertEqual(saved["managed"]["captions"], [])
        self.assertEqual(saved["intent"]["captions"], [])
        patch, _, _, conflicts = _merge_fields(saved, saved["intent"], body, "edge")
        self.assertEqual((patch, conflicts), ({}, []))

    def test_pending_nonempty_caption_is_not_acknowledged_from_omission(self):
        saved = record()
        desired = [{"content": "Not applied", "position": "50%"}]
        state = {"items": {"edge": saved}, "pending_updates": {"edge": {"patch": {"captions": desired}}}}
        _recover_updates(state, {"edge": {"style": {"strokeColor": "#123456", "strokeWidth": "2"}}})
        self.assertEqual(saved["managed"]["captions"], [])
        self.assertEqual(saved["intent"]["captions"], [])


if __name__ == "__main__":
    unittest.main()
