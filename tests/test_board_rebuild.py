"""Receipt, retry and local preflight boundaries for fresh-board publication."""

import contextlib
import fcntl
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from liquid_tracer.board_rebuild import rebuild_board, rebuild_status
from liquid_tracer.cli import main
from liquid_tracer.common import TraceError, digest, read_json, save_json
from liquid_tracer.investigations import read_case, update_case
from tests.fixtures import A, fixture


class BoardRebuildTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.case = root / "case"
        source = root / "fixture.json"
        save_json(source, fixture())
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            code = main(["trace", "--case", str(self.case), "--fixture", str(source),
                         "--seed", A + ":0", "--hops", "1"])
        self.assertEqual(code, 0)
        self.run = json.loads(output.getvalue())["run_id"]
        self.plan = read_json(self.case / "runs" / self.run / "miro-plan.json")
        update_case(self.case, {"miro_board": "old_board="})
        self.directory = self.case / "miro" / "rebuilds" / digest(b"old_board=")[:24]
        self.receipt = self.directory / "receipt.json"
        self.remote = Mock(return_value=(201, {}, b'{"id":"new_board="}'))
        for target, kwargs in (
            ("liquid_tracer.cli.refresh_presentation", {"return_value": self.plan}),
            ("liquid_tracer.board_rebuild.sync", {"side_effect": lambda *a, **k: {"dry_run": k.get("dry_run", False)}}),
        ):
            wrapper = patch(target, **kwargs)
            mocked = wrapper.start()
            self.addCleanup(wrapper.stop)
            if target.endswith("refresh_presentation"):
                self.refresh = mocked
            else:
                self.sync = mocked
        environment = patch.dict(os.environ, {"MIRO_ACCESS_TOKEN": "synthetic-secret"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def rebuild(self, **options):
        return rebuild_board(self.case, source_board="old_board=", transport=self.remote, **options)

    def test_cli_dispatch_pins_source_and_one_off_budget(self):
        with patch("liquid_tracer.cli.rebuild_board", return_value={"board_id": "new_board="}) as call, \
                contextlib.redirect_stdout(io.StringIO()):
            code = main(["miro-rebuild-board", "--case", str(self.case), "--source-board", "old_board=",
                         "--name", "Fresh board", "--run", self.run, "--max-new-items", "3000"])
        self.assertEqual(code, 0)
        self.assertEqual(call.call_args.args, (self.case, self.run, "old_board=", "Fresh board", 3000))

    def test_preflight_before_create_private_receipt_and_exact_retry(self):
        old = self.case / "miro" / (digest(b"old_board=")[:24] + ".json")
        old.parent.mkdir(parents=True)
        old.write_text("do not open or change this old mapping")
        def remote(*args):
            self.assertEqual(read_json(self.receipt)["status"], "pending")
            self.assertEqual(read_case(self.case)["miro_board"], "old_board=")
            self.assertEqual(self.sync.call_count, 1)
            self.assertEqual(json.loads(args[3])["policy"]["sharingPolicy"]["teamAccess"], "private")
            return 201, {}, b'{"id":"new_board="}'
        self.remote.side_effect = remote
        result = self.rebuild()
        self.assertTrue(result["created"])
        self.assertEqual(rebuild_status(self.case)["status"], "complete")
        with patch.dict(os.environ, {}, clear=True):
            again = self.rebuild()
        self.assertTrue(again["reused"])
        self.assertEqual(again["board_id"], result["board_id"])
        self.assertEqual(self.remote.call_count, 1)
        self.assertEqual(self.refresh.call_count, 1)
        self.assertEqual(old.read_text(), "do not open or change this old mapping")
        self.assertNotIn("synthetic-secret", self.receipt.read_text())

    def test_busy_trace_or_case_prevents_any_preparation(self):
        for filename in ("trace.lock", "case.lock"):
            with self.subTest(filename=filename), (self.case / filename).open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(TraceError, "busy"):
                    self.rebuild()
        self.refresh.assert_not_called()
        self.remote.assert_not_called()

    def test_rebuild_uses_current_attribution_arrow_preference(self):
        update_case(self.case, {"run_defaults": {"color_attribution_arrows": True}})
        self.rebuild()
        self.assertTrue(self.refresh.call_args.kwargs["color_attribution_arrows"])

    def test_missing_token_invalid_options_and_stale_source_never_create(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(TraceError, "MIRO_ACCESS_TOKEN"):
            self.rebuild()
        for options in ({"name": ""}, {"max_new_items": True}, {"max_new_items": -1}):
            with self.subTest(options=options), self.assertRaises(TraceError):
                self.rebuild(**options)
        update_case(self.case, {"miro_board": "unrelated_board="})
        with self.assertRaisesRegex(TraceError, "linked Miro board changed"):
            self.rebuild()
        self.refresh.assert_not_called()
        self.remote.assert_not_called()

    def test_settings_change_during_preparation_rejects_before_post(self):
        def changed(*args, **kwargs):
            save_json(self.case / "case.json", {**read_case(self.case), "miro_board": "other_board="})
            return self.plan
        self.refresh.side_effect = changed
        with self.assertRaisesRegex(TraceError, "settings changed"):
            self.rebuild()
        self.remote.assert_not_called()

    def test_uncertain_creation_and_corrupt_success_cannot_repost(self):
        for response in ((503, {}, b"private response"), (201, {}, b"[]"),
                         (201, {}, b'{"id":"old_board="}'), (201, {}, b"not json")):
            with self.subTest(response=response):
                if self.receipt.exists():
                    self.receipt.unlink()
                self.remote.reset_mock()
                self.remote.return_value = response
                with self.assertRaisesRegex(TraceError, "uncertain"):
                    self.rebuild()
                with self.assertRaisesRegex(TraceError, "uncertain"):
                    self.rebuild()
                self.assertEqual(self.remote.call_count, 1)
                self.assertEqual(rebuild_status(self.case)["status"], "pending")
                self.assertEqual(read_case(self.case)["miro_board"], "old_board=")

    def test_known_rejection_allows_retry_without_reusing_error_text(self):
        self.remote.return_value = 403, {}, b"private server response"
        with self.assertRaisesRegex(TraceError, "HTTP 403") as caught:
            self.rebuild()
        self.assertNotIn("private server response", str(caught.exception))
        self.assertEqual(rebuild_status(self.case)["status"], "rejected")
        self.remote.return_value = 201, {}, b'{"id":"new_board="}'
        self.assertTrue(self.rebuild()["created"])

    def test_partial_sync_uses_frozen_plan_without_second_post(self):
        def fail(*args, **kwargs):
            if not kwargs.get("dry_run"):
                raise TraceError("synthetic interrupted publication")
            return {}
        self.sync.side_effect = fail
        with self.assertRaisesRegex(TraceError, "Resume this rebuild"):
            self.rebuild()
        self.assertEqual(read_case(self.case)["miro_board"], "new_board=")
        status = rebuild_status(self.case)
        self.assertEqual((status["status"], status["source_board_id"]), ("syncing", "old_board="))
        self.refresh.side_effect = AssertionError("No new ELK on resume")
        self.sync.side_effect = lambda *a, **k: {}
        self.assertTrue(self.rebuild(max_new_items=4000)["reused"])
        self.assertEqual(self.remote.call_count, 1)

    def test_missing_frozen_plan_blocks_resumption_without_new_post(self):
        self.rebuild()
        (self.directory / "miro-plan.json").unlink()
        with self.assertRaisesRegex(TraceError, "plan is missing or invalid"):
            self.rebuild()
        self.assertEqual(self.remote.call_count, 1)

    def test_receipt_save_failure_reports_id_and_never_reposts(self):
        def save(path, value):
            if Path(path) == self.receipt and value.get("status") == "created":
                raise OSError("disk failure")
            save_json(path, value)
        with patch("liquid_tracer.board_rebuild.save_json", side_effect=save), \
                self.assertRaisesRegex(TraceError, "new_board="):
            self.rebuild()
        self.assertEqual(read_json(self.receipt)["status"], "pending")
        with self.assertRaisesRegex(TraceError, "uncertain"):
            self.rebuild()
        self.assertEqual(self.remote.call_count, 1)

    def test_link_failure_reuses_created_receipt(self):
        def save(path, value):
            if Path(path) == self.case / "case.json":
                raise OSError("disk failure")
            save_json(path, value)
        with patch("liquid_tracer.board_rebuild.save_json", side_effect=save), \
                self.assertRaisesRegex(TraceError, "linking it failed"):
            self.rebuild()
        self.assertEqual(read_json(self.receipt)["status"], "created")
        self.assertEqual(read_case(self.case)["miro_board"], "old_board=")
        self.assertTrue(self.rebuild()["reused"])
        self.assertEqual(self.remote.call_count, 1)

    def test_receipt_conflicts_and_later_link_cannot_redirect_existing_operation(self):
        self.rebuild()
        for options in ({"name": "another name"}, {"run_id": "f" * 16}):
            with self.subTest(options=options), self.assertRaisesRegex(TraceError, "another run or name"):
                self.rebuild(**options)
        update_case(self.case, {"miro_board": "later_board="})
        with self.assertRaisesRegex(TraceError, "linked Miro board changed"):
            self.rebuild()
        self.assertEqual(self.remote.call_count, 1)

    def test_corrupt_receipt_fails_closed_in_action_and_status(self):
        self.rebuild()
        self.receipt.write_text("not json")
        with self.assertRaisesRegex(TraceError, "receipt"):
            self.rebuild()
        self.assertEqual(rebuild_status(self.case)["status"], "unavailable")
        self.assertEqual(self.remote.call_count, 1)

    def test_created_mapping_collision_stays_blocked_on_retry(self):
        path = self.case / "miro" / (digest(b"new_board=")[:24] + ".json")
        path.parent.mkdir(parents=True)
        path.write_text("existing independent mapping")
        for _ in range(2):
            with self.assertRaisesRegex(TraceError, "already has a local mapping"):
                self.rebuild()
        self.assertEqual(self.remote.call_count, 1)
        self.assertEqual(path.read_text(), "existing independent mapping")
        self.assertEqual(read_case(self.case)["miro_board"], "old_board=")

    def test_partial_board_cannot_be_used_to_create_another_replacement(self):
        self.sync.side_effect = lambda *a, **k: {} if k.get("dry_run") else (_ for _ in ()).throw(TraceError("partial"))
        with self.assertRaises(TraceError):
            self.rebuild()
        with self.assertRaisesRegex(TraceError, "unfinished rebuild"):
            rebuild_board(self.case, source_board="new_board=", transport=self.remote)
        self.assertEqual(self.remote.call_count, 1)

    def test_completed_retry_after_manual_relink_restores_replacement(self):
        self.rebuild()
        update_case(self.case, {"miro_board": "old_board="})
        self.rebuild()
        self.assertEqual(read_case(self.case)["miro_board"], "new_board=")
        self.assertEqual(self.remote.call_count, 1)


if __name__ == "__main__":
    unittest.main()
