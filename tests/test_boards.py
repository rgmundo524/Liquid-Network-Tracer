import contextlib
import fcntl
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from liquid_tracer.boards import board_options, create_board, default_board_name
from liquid_tracer.cli import main
from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.investigations import create_investigation, read_case, update_case


class BoardCreationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = create_investigation(self.root, "Synthetic investigation")
        self.receipt = self.case / "miro" / "board-creation.json"
        self.environment = patch.dict(os.environ, {"MIRO_ACCESS_TOKEN": "synthetic-token-do-not-display"}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def success(self):
        return 201, {}, json.dumps({"id": "test_board=", "name": "Synthetic investigation",
                                    "team": {"id": "123456"}}).encode()

    def test_creation_saves_receipt_before_link_and_reuses_linked_board(self):
        original = read_case(self.case)
        evidence = self.case / "runs" / "synthetic" / "trace.json"
        evidence.parent.mkdir(parents=True)
        evidence.write_bytes(b'{"synthetic": true}\n')
        before = evidence.read_bytes()
        def transport(method, url, headers, body, timeout):
            self.assertEqual(read_json(self.receipt)["status"], "pending")
            self.assertIsNone(read_case(self.case)["miro_board"])
            self.assertEqual((method, url, timeout), ("POST", "https://api.miro.com/v2/boards", 30))
            self.assertEqual(headers["Authorization"], "Bearer synthetic-token-do-not-display")
            request = json.loads(body)
            self.assertNotIn("teamId", request)
            self.assertEqual(request["policy"]["sharingPolicy"], {
                "access": "private", "organizationAccess": "private", "teamAccess": "private"})
            return self.success()
        remote = Mock(side_effect=transport)
        result = create_board(self.case, transport=remote)
        self.assertTrue(result["created"])
        self.assertEqual(result["board_url"], "https://miro.com/app/board/test_board%3D/")
        self.assertEqual(result["team_id"], "123456")
        self.assertEqual(read_case(self.case), {**original, "miro_board": "test_board="})
        self.assertEqual(evidence.read_bytes(), before)
        self.assertEqual(read_json(self.receipt)["status"], "created")
        self.assertNotIn("synthetic-token-do-not-display", self.receipt.read_text())
        with patch.dict(os.environ, {}, clear=True):
            again = create_board(self.case, name="Different requested name", transport=remote)
        self.assertEqual(again["board_id"], result["board_id"])
        self.assertTrue(again["reused"])
        self.assertEqual(remote.call_count, 1)

    def test_explicit_team_visibility_and_destination(self):
        remote = Mock(return_value=self.success())
        create_board(self.case, name="Selected board", team_id="987654", visibility="team", transport=remote)
        body = json.loads(remote.call_args.args[3])
        self.assertEqual(body["name"], "Selected board")
        self.assertEqual(body["teamId"], "987654")
        self.assertEqual(body["policy"]["sharingPolicy"], {
            "access": "private", "organizationAccess": "private", "teamAccess": "edit"})

    def test_rejected_auth_and_permissions_allow_corrected_retry_without_raw_body(self):
        for status in (400, 401, 403, 404, 409, 429):
            with self.subTest(status=status):
                remote = Mock(return_value=(status, {}, b"secret-token raw server body"))
                with self.assertRaises(TraceError) as caught:
                    create_board(self.case, transport=remote)
                self.assertIn("HTTP " + str(status), str(caught.exception))
                self.assertNotIn("secret-token", str(caught.exception))
                self.assertEqual(read_json(self.receipt)["status"], "rejected")
                self.assertIsNone(read_case(self.case)["miro_board"])
                self.assertEqual(remote.call_count, 1)
        remote = Mock(return_value=self.success())
        self.assertTrue(create_board(self.case, transport=remote)["created"])
        self.assertEqual(remote.call_count, 1)

    def test_ambiguous_http_status_never_reposts(self):
        for status in (200, 202, 301, 408, 500, 503):
            with self.subTest(status=status):
                if self.receipt.exists():
                    self.receipt.unlink()
                remote = Mock(return_value=(status, {}, b"do not echo"))
                with self.assertRaisesRegex(TraceError, "uncertain"):
                    create_board(self.case, transport=remote)
                self.assertEqual(read_json(self.receipt)["status"], "pending")
                with self.assertRaisesRegex(TraceError, "Investigation settings"):
                    create_board(self.case, transport=remote)
                self.assertEqual(remote.call_count, 1)

    def test_transport_failure_retains_pending_without_echoing_exception(self):
        remote = Mock(side_effect=TraceError("synthetic-secret in unsafe transport error"))
        with self.assertRaises(TraceError) as caught:
            create_board(self.case, transport=remote)
        self.assertNotIn("synthetic-secret", str(caught.exception))
        self.assertEqual(read_json(self.receipt)["status"], "pending")
        with self.assertRaisesRegex(TraceError, "uncertain"):
            create_board(self.case, transport=remote)
        self.assertEqual(remote.call_count, 1)

    def test_malformed_success_never_reposts(self):
        for raw in (b"not json", b"[]", b"{}", b'{"id":null}', b'{"id":"bad/id"}'):
            with self.subTest(raw=raw):
                if self.receipt.exists():
                    self.receipt.unlink()
                remote = Mock(return_value=(201, {}, raw))
                with self.assertRaisesRegex(TraceError, "uncertain"):
                    create_board(self.case, transport=remote)
                with self.assertRaisesRegex(TraceError, "uncertain"):
                    create_board(self.case, transport=remote)
                self.assertEqual(remote.call_count, 1)

    def test_link_failure_recovers_receipt_without_new_post(self):
        original = read_case(self.case)
        def save(path, data):
            if Path(path) == self.case / "case.json":
                self.assertEqual(read_json(self.receipt)["status"], "created")
                raise OSError("synthetic disk failure")
            save_json(path, data)
        remote = Mock(return_value=self.success())
        with patch("liquid_tracer.boards.save_json", side_effect=save):
            with self.assertRaisesRegex(TraceError, "reuse the saved board"):
                create_board(self.case, transport=remote)
        self.assertEqual(read_case(self.case), original)
        with patch.dict(os.environ, {}, clear=True):
            result = create_board(self.case, transport=remote)
        self.assertTrue(result["reused"])
        self.assertEqual(read_case(self.case)["miro_board"], "test_board=")
        self.assertEqual(remote.call_count, 1)

    def test_receipt_save_failure_retains_pending_and_reports_acknowledged_id(self):
        def save(path, data):
            if Path(path) == self.receipt and data["status"] == "created":
                raise OSError("synthetic disk failure")
            save_json(path, data)
        remote = Mock(return_value=self.success())
        with patch("liquid_tracer.boards.save_json", side_effect=save):
            with self.assertRaisesRegex(TraceError, "test_board="):
                create_board(self.case, transport=remote)
        self.assertEqual(read_json(self.receipt)["status"], "pending")
        with self.assertRaisesRegex(TraceError, "uncertain"):
            create_board(self.case, transport=remote)
        self.assertEqual(remote.call_count, 1)

    def test_settings_link_recovers_pending_creation(self):
        remote = Mock(return_value=(503, {}, b""))
        with self.assertRaises(TraceError):
            create_board(self.case, transport=remote)
        update_case(self.case, {"miro_board": "https://miro.com/app/board/recovered_board/"})
        result = create_board(self.case, transport=remote)
        self.assertEqual(result["board_id"], "recovered_board")
        self.assertEqual(remote.call_count, 1)

    def test_existing_case_lock_prevents_concurrent_creation(self):
        remote = Mock(return_value=self.success())
        with (self.case / "case.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(TraceError, "busy"):
                create_board(self.case, transport=remote)
        remote.assert_not_called()
        self.assertFalse(self.receipt.exists())

    def test_preflight_options_and_missing_token_make_no_request(self):
        remote = Mock()
        for options in ({"name": ""}, {"name": "x" * 61}, {"name": "bad\nname"},
                        {"team_id": "../123"}, {"visibility": "public"}):
            with self.subTest(options=options), self.assertRaises(TraceError):
                create_board(self.case, transport=remote, **options)
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(TraceError, "MIRO_ACCESS_TOKEN"):
            create_board(self.case, transport=remote)
        remote.assert_not_called()
        self.assertFalse(self.receipt.exists())

    def test_invalid_case_and_corrupt_receipt_make_no_request(self):
        remote = Mock()
        with self.assertRaises(OSError):
            create_board(self.root / "missing", transport=remote)
        self.assertFalse((self.root / "missing").exists())
        save_json(self.receipt, {"schema_version": 1, "case_id": "wrong", "status": "rejected"})
        with self.assertRaisesRegex(TraceError, "Invalid Miro board creation receipt"):
            create_board(self.case, transport=remote)
        remote.assert_not_called()

    def test_default_name_limits_and_synthetic_marker(self):
        self.assertEqual(len(default_board_name({"name": "n" * 120})), 60)
        demo = default_board_name({"name": "n" * 120, "fixture": "synthetic.json"})
        self.assertEqual(len(demo), 60)
        self.assertTrue(demo.startswith("SYNTHETIC DEMO · "))
        self.assertEqual(board_options(" Name ")["name"], "Name")

    def test_cli_dispatches_board_creation_without_graph_sync(self):
        expected = {"board_id": "example_board", "created": True}
        with patch("liquid_tracer.cli.create_board", return_value=expected) as create, \
                patch("liquid_tracer.cli.sync_run") as sync, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["miro-create-board", "--case", str(self.case), "--name", "Example board",
                         "--team-id", "123456", "--visibility", "team"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), expected)
        create.assert_called_once_with(self.case, "Example board", "123456", "team")
        sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
