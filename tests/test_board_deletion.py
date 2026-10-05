"""Whole-board deletion keeps evidence and records ambiguous remote outcomes."""

import fcntl
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from liquid_tracer.board_deletion import delete_board, deletion_receipt, assert_board_writable, board_write_lock
from liquid_tracer.boards import create_board as create_legacy_board
from liquid_tracer.common import StopRun, TraceError, digest, read_json, save_json
from liquid_tracer.investigation_boards import create_board, link_board, list_boards, sync_board, _registry_lock, _board_lock
from liquid_tracer.investigations import create_investigation, read_case


class DeleteBoardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = create_investigation(self.root, "Deletion fixture", board="legacy-board")
        self.board = link_board(self.case, "pegouts", "Paths", "selected-board")
        self.remote = Mock(return_value=(204, {}, b""))
        self.environment = patch.dict(os.environ, {"MIRO_ACCESS_TOKEN": "fixture-token"}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def delete(self, **kwargs):
        return delete_board(self.case, self.board["id"], self.board["board_id"], transport=self.remote, **kwargs)

    def test_success_has_durable_intent_before_http_then_hides_only_selected_board(self):
        mapping = self.case / self.board["state_file"]
        save_json(mapping, {"board_id": "selected-board", "items": {}, "runs": {}})
        preview = self.case / "previews" / "fixture" / "graph.json"
        save_json(preview, {"evidence": True})
        original = {p: p.read_bytes() for p in (mapping, preview, self.case / "case.json")}
        def remote(method, url, headers, body, timeout):
            self.assertEqual((method, url, body, timeout), ("DELETE", "https://api.miro.com/v2/boards/selected-board", None, 30))
            self.assertEqual(deletion_receipt(self.case, "selected-board")["status"], "pending")
            listed = next(b for b in list_boards(self.case) if b["id"] == self.board["id"])
            self.assertEqual(listed["status"], "pending_deletion")
            self.assertFalse(listed["can_sync"])
            # A remote request never owns the listing/registry lock.
            with (self.case / "boards.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return 204, {}, b""
        self.remote.side_effect = remote
        result = self.delete()
        self.assertEqual(result["status"], "deleted")
        self.assertTrue(result["deleted"])
        self.assertEqual([b["board_id"] for b in list_boards(self.case)], ["legacy-board"])
        self.assertEqual({p: p.read_bytes() for p in original}, original)
        self.assertNotIn("fixture-token", str(deletion_receipt(self.case, "selected-board")))
        self.assertEqual(self.delete(), result)
        self.assertEqual(self.remote.call_count, 1)

    def test_legacy_delete_does_not_resurrect_mapping_or_creation_receipt(self):
        target = "legacy-board"
        mapping = self.case / "miro" / (digest(target.encode())[:24] + ".json")
        save_json(mapping, {"board_id": target, "namespace": {"case_id": read_case(self.case)["case_id"]}, "items": {}, "runs": {}})
        original = mapping.read_bytes()
        receipt = self.case / "miro" / "board-creation.json"
        save_json(receipt, {"schema_version": 1, "case_id": read_case(self.case)["case_id"], "status": "created", "board_id": target})
        record = next(b for b in list_boards(self.case) if b["board_id"] == target)
        delete_board(self.case, record["id"], target, transport=self.remote)
        self.assertIsNone(read_case(self.case)["miro_board"])
        self.assertEqual(mapping.read_bytes(), original)
        self.assertNotIn(target, [b["board_id"] for b in list_boards(self.case)])
        with self.assertRaisesRegex(TraceError, "deleted"):
            create_legacy_board(self.case, transport=self.remote)
        self.assertEqual(self.remote.call_count, 1)
        delete_board(self.case, record["id"], target, transport=self.remote)
        self.assertEqual(self.remote.call_count, 1)

    def test_legacy_without_mapping_can_finish_idempotently(self):
        record = next(b for b in list_boards(self.case) if b["board_id"] == "legacy-board")
        delete_board(self.case, record["id"], "legacy-board", transport=self.remote)
        delete_board(self.case, record["id"], "legacy-board", transport=self.remote)
        self.assertEqual(self.remote.call_count, 1)

    def test_malformed_registered_mapping_does_not_prevent_confirmed_board_delete(self):
        mapping = self.case / self.board["state_file"]
        mapping.parent.mkdir(parents=True)
        mapping.write_text("broken mapping")
        self.assertEqual(next(b for b in list_boards(self.case) if b["id"] == self.board["id"])["status"], "mapping_error")
        self.delete()
        self.assertEqual(mapping.read_text(), "broken mapping")

    def test_bad_confirmation_or_missing_token_never_calls_remote(self):
        for record, target in ((self.board["id"], "different-board"), ("missing", "selected-board"),
                               (self.board["id"], "https://miro.com/app/board/selected-board/")):
            with self.subTest(record=record, target=target), self.assertRaises(TraceError):
                delete_board(self.case, record, target, transport=self.remote)
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(TraceError, "MIRO_ACCESS_TOKEN"):
            self.delete()
        self.assertIsNone(deletion_receipt(self.case, "selected-board"))
        self.remote.assert_not_called()

    def test_only_204_confirms_delete_and_404_blocks_writes_until_explicit_retry(self):
        for status in (200, 202, 404, 408, 500):
            with self.subTest(status=status):
                self.remote.return_value = (status, {}, b"unsafe remote response")
                with self.assertRaisesRegex(TraceError, "did not confirm"):
                    self.delete()
                self.assertEqual(deletion_receipt(self.case, "selected-board")["status"], "uncertain")
                with self.assertRaisesRegex(TraceError, "did not confirm"):
                    assert_board_writable(self.case, "selected-board")
                self.assertIn("selected-board", [b["board_id"] for b in list_boards(self.case)])
        self.remote.return_value = (204, {}, b"")
        self.assertTrue(self.delete()["deleted"])

    def test_rejected_retry_cannot_resolve_earlier_uncertain_delete(self):
        self.remote.side_effect = OSError("timeout")
        with self.assertRaises(TraceError):
            self.delete()
        self.remote.side_effect = None
        for status in (401, 403, 429):
            self.remote.return_value = (status, {}, b"")
            with self.assertRaisesRegex(TraceError, "did not confirm"):
                self.delete()
            self.assertEqual(deletion_receipt(self.case, "selected-board")["status"], "uncertain")
            with self.assertRaises(TraceError):
                assert_board_writable(self.case, "selected-board")
        self.remote.return_value = (204, {}, b"")
        self.assertTrue(self.delete()["deleted"])

    def test_explicit_create_reuses_saved_preview_on_new_board_after_deletion(self):
        create_remote = Mock(return_value=(201, {}, b'{"id":"first-publication"}'))
        first = create_board(self.case, "full", "Saved layout", creation_preview_id="saved-preview", transport=create_remote)
        delete_board(self.case, first["id"], first["board_id"], transport=self.remote)
        create_remote.return_value = (201, {}, b'{"id":"replacement-publication"}')
        replacement = create_board(self.case, "full", "Saved layout", creation_preview_id="saved-preview", transport=create_remote)
        self.assertNotEqual(replacement["id"], first["id"])
        self.assertEqual(replacement["board_id"], "replacement-publication")
        self.assertTrue(replacement["created"])
        retry = create_board(self.case, "full", "Saved layout", creation_preview_id="saved-preview", transport=create_remote)
        self.assertEqual(retry["id"], replacement["id"])
        self.assertFalse(retry["created"])
        self.assertEqual(create_remote.call_count, 2)
        records = read_json(self.case / "miro" / "boards.json")["boards"]
        self.assertTrue(any(item["id"] == first["id"] for item in records))

    def test_definite_rejection_retains_board_and_diagnostic(self):
        for status in (401, 403, 429):
            self.remote.return_value = (status, {}, b"never expose this")
            with self.assertRaisesRegex(TraceError, "HTTP " + str(status)):
                self.delete()
            receipt = deletion_receipt(self.case, "selected-board")
            self.assertEqual(receipt["status"], "rejected")
            self.assertNotIn("never expose", str(receipt))
            assert_board_writable(self.case, "selected-board")

    def test_transport_failure_preserves_uncertain_intent(self):
        self.remote.side_effect = OSError("fixture-token must not leak")
        with self.assertRaisesRegex(TraceError, "did not confirm") as raised:
            self.delete()
        self.assertNotIn("fixture-token", str(raised.exception))
        self.assertEqual(deletion_receipt(self.case, "selected-board")["status"], "uncertain")

    def test_intent_failure_sends_nothing_ack_failure_blocks_later_writes(self):
        with patch("liquid_tracer.board_deletion._save", side_effect=OSError("full")), self.assertRaises(OSError):
            self.delete()
        self.remote.assert_not_called()
        from liquid_tracer.board_deletion import _save
        def fail_ack(path, receipt):
            if receipt["status"] == "deleted":
                raise OSError("full")
            _save(path, receipt)
        with patch("liquid_tracer.board_deletion._save", side_effect=fail_ack), self.assertRaisesRegex(TraceError, "acknowledgement"):
            self.delete()
        self.assertEqual(deletion_receipt(self.case, "selected-board")["status"], "pending")
        with self.assertRaises(TraceError):
            assert_board_writable(self.case, "selected-board")

    def test_local_unlink_failure_recovers_without_repeat_remote_delete(self):
        from liquid_tracer.board_deletion import _finish_local
        with patch("liquid_tracer.board_deletion._finish_local", side_effect=OSError("full")), self.assertRaisesRegex(TraceError, "updating the local"):
            self.delete()
        self.assertTrue(self.delete()["deleted"])
        self.assertEqual(self.remote.call_count, 1)

    def test_cancellation_before_http_is_safe_and_after_ack_does_not_hide_success(self):
        progress = Mock(side_effect=StopRun("cancel"))
        with self.assertRaises(StopRun):
            self.delete(progress=progress)
        self.remote.assert_not_called()
        self.assertIsNone(deletion_receipt(self.case, "selected-board"))
        def after_ack(event):
            if event["completed"]:
                raise StopRun("cancel")
        self.assertTrue(self.delete(progress=after_ack)["deleted"])

    def test_busy_board_mapping_or_global_lock_prevents_delete(self):
        mapping = self.case / self.board["state_file"]
        mapping.parent.mkdir(parents=True)
        with _board_lock(self.case, self.board["id"]), self.assertRaisesRegex(TraceError, "busy"):
            self.delete()
        with mapping.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(TraceError, "busy"):
                self.delete()
        with board_write_lock(self.case, "selected-board"), self.assertRaisesRegex(TraceError, "busy"):
            self.delete()
        self.remote.assert_not_called()

    def test_shared_receipt_blocks_other_case_relink_and_sync_reuse(self):
        other = create_investigation(self.root, "Other case")
        other_record = link_board(other, "pegouts", "Other paths", "selected-board")
        self.delete()
        self.assertNotIn("selected-board", [b["board_id"] for b in list_boards(other)])
        with self.assertRaisesRegex(TraceError, "deleted"):
            link_board(other, "full", "Re-link", "selected-board")
        with self.assertRaises(TraceError):
            sync_board(other, other_record["id"], "missing-preview", transport=self.remote)
        self.assertEqual(self.remote.call_count, 1)

    def test_symlinked_deletion_authority_or_output_is_rejected(self):
        root_receipts = self.root / ".miro-board-deletions"
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        root_receipts.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(TraceError, "symbolic links"):
            self.delete()
        self.remote.assert_not_called()
        root_receipts.unlink()
        mapping = self.case / self.board["state_file"]
        mapping.parent.mkdir(parents=True)
        mapping.symlink_to(elsewhere / "mapping")
        with self.assertRaisesRegex(TraceError, "symbolic links"):
            self.delete()
        self.remote.assert_not_called()


if __name__ == "__main__":
    unittest.main()
