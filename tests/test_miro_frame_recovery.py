"""Frame recovery reads a populated board without replaying uncertain POSTs."""

import copy
import fcntl
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from liquid_tracer.common import TraceError, canonical, save_json
from liquid_tracer.miro import make_plan, sync, sync_frames
from liquid_tracer.miro_frame_recovery import pending_frame, recover_pending_frame, review_pending_frame
from liquid_tracer.miro_state import load_state
from tests.test_miro_frame_sync import FrameMiro, framed_graph
from tests.test_miro_sync import NAMESPACE


BOARD = "synthetic-board="


class RecoveryMiro(FrameMiro):
    def __init__(self):
        super().__init__()
        self.frame_pages = None

    def __call__(self, method, url, headers, body, timeout):
        parsed, query = urlsplit(url), parse_qs(urlsplit(url).query)
        if method == "GET" and parsed.path.endswith("/items") and query.get("type") == ["frame"]:
            self.calls.append((method, url, None))
            if query.get("limit") != ["50"]:
                raise AssertionError("Recovery must request the supported full page size")
            if self.frame_pages is not None:
                page = self.frame_pages[query.get("cursor", [""])[0]]
                if isinstance(page, tuple):
                    return page
                return 200, {}, page if isinstance(page, bytes) else canonical(page)
            frames = [copy.deepcopy(item) for item in self.items.values() if item.get("type") == "frame"]
            return 200, {}, canonical({"type": "cursor-list", "data": frames, "total": len(frames)})
        return super().__call__(method, url, headers, body, timeout)


class MiroFrameRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sync.json"
        self.remote = RecoveryMiro()
        self.plan = make_plan(framed_graph())

    def interrupt(self, committed=False):
        sync(self.plan, BOARD, self.path, token="synthetic-token", transport=self.remote, interval=0)
        count = 0

        def transport(method, url, headers, body, timeout):
            nonlocal count
            if method == "POST" and url.endswith("/frames"):
                count += 1
                if count == 3:
                    if committed:
                        self.remote(method, url, headers, body, timeout)
                    return 500, {}, b'{}'
            return self.remote(method, url, headers, body, timeout)

        with self.assertRaisesRegex(TraceError, "HTTP 500"):
            sync_frames(self.plan, BOARD, self.path, token="synthetic-token", transport=transport, interval=0)
        self.initial = load_state(self.path)
        self.key, self.entry = pending_frame(self.initial)
        self.assertEqual(sum(record["endpoint"] == "frames" for record in self.initial["items"].values()), 2)
        self.remote.calls.clear()

    def review(self, **kwargs):
        return review_pending_frame(self.path, BOARD, NAMESPACE, token="synthetic-token",
                                    transport=kwargs.pop("transport", self.remote), interval=0, **kwargs)

    def recover(self, review, **kwargs):
        return recover_pending_frame(self.path, BOARD, NAMESPACE, review_id=review["review_id"],
                                     token="synthetic-token", transport=kwargs.pop("transport", self.remote),
                                     interval=0, **kwargs)

    def assert_unchanged(self):
        self.assertEqual(load_state(self.path), self.initial)
        self.assertTrue(all(call[0] == "GET" for call in self.remote.calls))

    def test_500_before_frame_creation_confirm_absent_then_resume_only_remaining_frames(self):
        self.interrupt()
        events = []
        review = self.review(progress=events.append)
        self.assertTrue(review["can_confirm_absent"])
        self.assertEqual(review["candidates"], [])
        self.assertEqual(review["potential_match_count"], 0)
        self.assertEqual(review["pending_frame"]["title"], self.entry["body"]["data"]["title"])
        self.assert_unchanged()
        self.assertTrue(events)
        self.assertEqual({event["phase"] for event in events}, {"frame_recovery"})
        report = self.recover(review, confirmed_absent=True)
        self.assertEqual(report, {"recovery": "confirmed_absent_frame", "run_id": "one",
                                  "resolved_count": 1, "remaining_pending": 0})
        state = load_state(self.path)
        self.assertEqual(state["items"], self.initial["items"])
        self.assertEqual(state["active_frame_run_id"], "one")
        self.assertIsNone(state["active_run_id"])
        self.assertTrue(all(call[0] == "GET" for call in self.remote.calls))
        self.assertEqual(state["pending_creations"], {})
        self.assertTrue(state["recovery_history"][-1]["confirmed_absent"])
        resumed = sync_frames(self.plan, BOARD, self.path, token="synthetic-token", transport=self.remote, interval=0)
        self.assertEqual((resumed["created"], resumed["new_shapes"], resumed["new_connectors"]), (2, 0, 0))
        self.assertEqual(sum(item["type"] == "frame" for item in self.remote.items.values()), 4)
        for key, record in self.initial["items"].items():
            self.assertEqual(load_state(self.path)["items"][key]["id"], record["id"])

    def test_500_after_frame_creation_adopt_then_resume_without_duplicate(self):
        self.interrupt(committed=True)
        review = self.review()
        self.assertEqual(len(review["candidates"]), 1)
        self.assertFalse(review["can_confirm_absent"])
        candidate = review["candidates"][0]
        self.assert_unchanged()
        report = self.recover(review, item_id=candidate["id"])
        self.assertEqual(report["recovery"], "adopted_frame")
        state = load_state(self.path)
        self.assertEqual(state["items"][self.key]["id"], candidate["id"])
        self.assertEqual(state["items"][self.key]["frame_proof"], self.entry["frame_proof"])
        for key, record in self.initial["items"].items():
            self.assertEqual(state["items"][key], record)
        self.assertTrue(all(call[0] == "GET" for call in self.remote.calls))
        resumed = sync_frames(self.plan, BOARD, self.path, token="synthetic-token", transport=self.remote, interval=0)
        self.assertEqual(resumed["created"], 1)
        self.assertEqual(sum(item["type"] == "frame" for item in self.remote.items.values()), 4)

    def test_legacy_interrupted_graph_frame_recovers_into_separate_frame_workflow(self):
        self.interrupt(committed=True)
        legacy = copy.deepcopy(self.initial)
        legacy.pop("active_frame_run_id")
        legacy.pop("frame_plan")
        legacy.update(active_run_id="one", latest_run_id=None, runs={})
        save_json(self.path, legacy)
        review = self.review()
        self.recover(review, item_id=review["candidates"][0]["id"])
        self.assertTrue(all(call[0] == "GET" for call in self.remote.calls))
        graph_report = sync(self.plan, BOARD, self.path, token="synthetic-token",
                            transport=self.remote, interval=0)
        self.assertEqual(graph_report["new_frames"], 0)
        self.assertFalse(any(method == "POST" and url.endswith("/frames")
                             for method, url, _ in self.remote.calls))
        report = sync_frames(self.plan, BOARD, self.path, token="synthetic-token",
                             transport=self.remote, interval=0)
        self.assertEqual(report["created"], 1)
        self.assertEqual(sum(item["type"] == "frame" for item in self.remote.items.values()), 4)

    def test_ambiguous_matches_require_explicit_selection_and_preserve_other_frame(self):
        self.interrupt(committed=True)
        candidate = self.review()["candidates"][0]
        self.remote.items["manual-copy"] = {**copy.deepcopy(self.remote.items[candidate["id"]]), "id": "manual-copy"}
        review = self.review()
        self.assertEqual(len(review["candidates"]), 2)
        with self.assertRaisesRegex(TraceError, "Choose one reviewed frame"):
            self.recover(review)
        with self.assertRaisesRegex(TraceError, "possible frame exists"):
            self.recover(review, confirmed_absent=True)
        self.assert_unchanged()
        self.recover(review, item_id=candidate["id"])
        self.assertIn("manual-copy", self.remote.items)
        self.assertNotIn("manual-copy", {record["id"] for record in load_state(self.path)["items"].values()})

    def test_mapped_id_cannot_be_adopted_and_is_not_a_candidate(self):
        self.interrupt()
        review = self.review()
        mapped_id = next(iter(self.initial["items"].values()))["id"]
        with self.assertRaisesRegex(TraceError, "reviewed candidates"):
            self.recover(review, item_id=mapped_id)
        self.assert_unchanged()

    def test_moved_or_renamed_possible_match_blocks_absence(self):
        for changed in ("position", "title"):
            with self.subTest(changed=changed):
                self.remote, self.path = RecoveryMiro(), Path(self.tmp.name) / (changed + ".json")
                self.interrupt(committed=True)
                item_id = self.review()["candidates"][0]["id"]
                if changed == "position":
                    self.remote.items[item_id]["position"]["x"] += 100
                else:
                    self.remote.items[item_id]["data"]["title"] = "Manually reviewed frame"
                review = self.review()
                self.assertEqual(review["candidates"], [])
                self.assertEqual(review["potential_match_count"], 1)
                self.assertFalse(review["can_confirm_absent"])
                with self.assertRaisesRegex(TraceError, "possible frame exists"):
                    self.recover(review, confirmed_absent=True)
                self.assert_unchanged()

    def test_candidates_match_decimal_rounding_without_relative_coordinate_tolerance(self):
        self.interrupt(committed=True)
        item_id = self.review()["candidates"][0]["id"]
        self.remote.items[item_id]["position"]["x"] += .005
        self.assertEqual(len(self.review()["candidates"]), 1)
        self.remote.items[item_id]["position"]["x"] += .1
        self.assertEqual(self.review()["candidates"], [])
        self.assert_unchanged()

    def test_remote_manual_edit_invalidates_review(self):
        self.interrupt(committed=True)
        review = self.review()
        item_id = review["candidates"][0]["id"]
        self.remote.items[item_id]["style"]["fillColor"] = "#abcdef"
        with self.assertRaisesRegex(TraceError, "review changed"):
            self.recover(review, item_id=item_id)
        self.assert_unchanged()

    def test_new_frame_invalidates_absence_review(self):
        self.interrupt()
        review = self.review()
        self.remote.items["late-frame"] = {"id": "late-frame", "type": "frame", **copy.deepcopy(self.entry["body"])}
        with self.assertRaisesRegex(TraceError, "review changed"):
            self.recover(review, confirmed_absent=True)
        self.assert_unchanged()

    def test_local_state_edit_invalidates_review(self):
        self.interrupt()
        review = self.review()
        self.initial["recovery_history"] = [{"note": "A later local change"}]
        save_json(self.path, self.initial)
        with self.assertRaisesRegex(TraceError, "review changed"):
            self.recover(review, confirmed_absent=True)
        self.assert_unchanged()

    def test_inventory_pages_are_complete_and_candidate_details_fetched(self):
        self.interrupt(committed=True)
        frames = [item for item in self.remote.items.values() if item["type"] == "frame"]
        summaries = [{"id": item["id"], "type": "frame"} for item in frames]
        self.remote.frame_pages = {"": {"data": summaries[:2], "cursor": "encoded/+ next", "total": 3},
                                   "encoded/+ next": {"data": summaries[2:], "total": 3}}
        review = self.review()
        self.assertEqual(len(review["candidates"]), 1)
        self.assertTrue(any("cursor=encoded%2F%2B+next" in call[1] for call in self.remote.calls))
        self.assertTrue(any("/frames/" in call[1] for call in self.remote.calls))
        self.assert_unchanged()

    def test_malformed_or_partial_inventory_never_changes_state(self):
        self.interrupt()
        invalid = [b"not-json", [], {"data": [] , "cursor": 3}, {"data": [], "total": True},
                   {"data": [], "total": 1}, {"data": [], "size": 1},
                   {"data": [], "links": {"next": "https://unsafe.invalid/"}},
                   {"data": [], "cursor": "still-more"},
                   {"data": [{"id": "x", "type": "shape"}]},
                   {"data": [{"id": "x/y", "type": "frame"}]},
                   {"data": [{"id": "x", "type": "frame", "isSupported": False}]},
                   {"data": [{"id": "x", "type": "frame"}, {"id": "x", "type": "frame"}]}]
        for page in invalid:
            with self.subTest(page=page):
                self.remote.frame_pages = {"": page}
                with self.assertRaises(TraceError):
                    self.review()
                self.assert_unchanged()

    def test_repeated_cursor_and_changing_totals_fail_closed(self):
        self.interrupt()
        self.remote.frame_pages = {
            "": {"data": [{"id": "a", "type": "frame"}], "cursor": "next", "total": 3},
            "next": {"data": [{"id": "b", "type": "frame"}], "cursor": "next", "total": 3}}
        with self.assertRaisesRegex(TraceError, "pagination did not complete"):
            self.review()
        self.remote.frame_pages["next"]["total"] = 4
        with self.assertRaisesRegex(TraceError, "changed during review"):
            self.review()
        self.assert_unchanged()

    def test_auth_and_server_errors_only_retry_reads_and_preserve_pending(self):
        self.interrupt()
        for status in (401, 403, 500):
            with self.subTest(status=status), patch("liquid_tracer.miro_requests.MiroRequests._wait"):
                self.remote.frame_pages = {"": (status, {}, b'{"message":"synthetic-secret-token"}')}
                with self.assertRaisesRegex(TraceError, "HTTP " + str(status)) as error:
                    self.review()
                self.assertNotIn("synthetic-secret-token", str(error.exception))
                self.assert_unchanged()

    def test_malformed_full_frame_or_noncanvas_geometry_blocks_recovery(self):
        self.interrupt(committed=True)
        item_id = self.review()["candidates"][0]["id"]
        original = copy.deepcopy(self.remote.items[item_id])
        self.remote.frame_pages = {"": {"data": [{"id": frame["id"], "type": "frame"}
                                      for frame in self.remote.items.values() if frame["type"] == "frame"]}}
        changes = [lambda item: item.update(type="shape"), lambda item: item.update(id="wrong"),
                   lambda item: item["position"].update(relativeTo="parent_top_left"),
                   lambda item: item.update(parent={"id": "some-frame"}),
                   lambda item: item["geometry"].update(width=0),
                   lambda item: item["geometry"].update(height=True),
                   lambda item: item["geometry"].update(rotation=90),
                   lambda item: item["data"].update(title=None)]
        for change in changes:
            self.remote.items[item_id] = copy.deepcopy(original)
            change(self.remote.items[item_id])
            with self.assertRaises(TraceError):
                self.review()
                self.assert_unchanged()

    def test_unrelated_preset_and_untitled_frames_do_not_block_review(self):
        self.interrupt()
        manual = {"id": "manual-preset", "type": "frame", **copy.deepcopy(self.entry["body"])}
        manual["data"] = {"title": "", "type": "fixed_ratio", "format": "a4"}
        manual["position"]["x"] += 20000
        self.remote.items[manual["id"]] = manual
        review = self.review()
        self.assertTrue(review["can_confirm_absent"])
        self.assertEqual(review["potential_match_count"], 0)
        self.assert_unchanged()

    def test_adopting_frame_preserves_its_manual_style_as_live_baseline(self):
        self.interrupt(committed=True)
        item_id = self.review()["candidates"][0]["id"]
        self.remote.items[item_id]["style"]["fillColor"] = "#abcdef"
        review = self.review()
        self.recover(review, item_id=item_id)
        record = load_state(self.path)["items"][self.key]
        self.assertEqual(record["managed"]["style"]["fillColor"], "#abcdef")
        self.assertEqual(record["intent"]["style"]["fillColor"], "#ffffffff")

    def test_item_links_change_without_invalidating_relevant_review(self):
        self.interrupt(committed=True)
        review = self.review()
        item_id = review["candidates"][0]["id"]
        for item in self.remote.items.values():
            item["links"] = {"self": "https://synthetic.invalid/changing-link"}
        self.recover(review, item_id=item_id)
        self.assertEqual(load_state(self.path)["items"][self.key]["id"], item_id)

    def test_pending_full_frame_disappearing_during_scan_stays_pending(self):
        self.interrupt(committed=True)
        item_id = self.review()["candidates"][0]["id"]

        def disappears(method, url, headers, body, timeout):
            if method == "GET" and url.endswith("/frames/" + item_id):
                return 404, {}, b'{}'
            return self.remote(method, url, headers, body, timeout)

        with self.assertRaisesRegex(TraceError, "HTTP 404"):
            self.review(transport=disappears)
        self.assert_unchanged()

    def test_recovery_input_validation_and_concurrent_publisher(self):
        self.interrupt()
        review = self.review()
        for options in ({}, {"confirmed_absent": 1}, {"item_id": "../bad"},
                        {"item_id": "some-id", "confirmed_absent": True}):
            with self.assertRaises(TraceError):
                self.recover(review, **options)
        with self.path.with_suffix(".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(TraceError, "publisher"):
                self.review()
        self.assert_unchanged()

    def test_read_and_candidate_budgets_fail_closed(self):
        self.interrupt(committed=True)
        with patch("liquid_tracer.miro_frame_recovery.MAX_FRAME_PAGES", 0):
            with self.assertRaisesRegex(TraceError, "read budget"):
                self.review()
        with patch("liquid_tracer.miro_frame_recovery.MAX_UNMAPPED_FRAMES", 0):
            with self.assertRaisesRegex(TraceError, "Too many unmapped"):
                self.review()
        with patch("liquid_tracer.miro_frame_recovery.MAX_CANDIDATES", 0):
            with self.assertRaisesRegex(TraceError, "Too many matching"):
                self.review()
        self.assert_unchanged()

    def test_atomic_commit_failure_does_not_clear_intent_or_create_board_items(self):
        self.interrupt(committed=True)
        review = self.review()
        with patch("liquid_tracer.miro_frame_recovery.SyncState.commit", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.recover(review, item_id=review["candidates"][0]["id"])
        self.assert_unchanged()

    def test_pending_frame_rejects_other_uncertainties_and_malformed_intents(self):
        self.interrupt()
        for field in ("pending_updates", "pending_deletions", "pending_frame_deletions",
                      "pending_creation_detaches", "address_migration"):
            state = {**copy.deepcopy(self.initial), field: {"other": {}}}
            with self.assertRaises(TraceError):
                pending_frame(state)
        for field, value in (("endpoint", "shapes"), ("run_id", "other"), ("operation_id", "invalid"),
                             ("frame_proof", {}), ("body", {})):
            state = copy.deepcopy(self.initial)
            state["pending_creations"][self.key][field] = value
            with self.assertRaises(TraceError):
                pending_frame(state)
        for state in (None, {}, {**self.initial, "pending_creations": {}},
                      {**self.initial, "pending": {"key": "old"}},
                      {**self.initial, "recovery_history": {}}):
            with self.assertRaises(TraceError):
                pending_frame(state)


if __name__ == "__main__":
    unittest.main()
