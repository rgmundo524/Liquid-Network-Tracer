"""Recovery dispatch and blocked-sync checks use saved synthetic investigations."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import main, miro_recovery_status, recover_miro_run, sync_run
from liquid_tracer.common import TraceError, digest, read_json, save_json
from liquid_tracer.investigations import update_case
from liquid_tracer.miro import sync
from tests.fixtures import A, fixture


class MiroRecoveryCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.case = root / "case"
        source = root / "fixture.json"
        save_json(source, fixture())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(["trace", "--case", str(self.case), "--fixture", str(source),
                           "--seed", A + ":0", "--hops", "1"])
        self.assertEqual(status, 0, output.getvalue())
        self.run = json.loads(output.getvalue())["run_id"]
        self.archive = self.case / "runs" / self.run
        self.plan = read_json(self.archive / "miro-plan.json")
        update_case(self.case, {"miro_board": "SYNTHETIC="})
        self.path = self.case / "miro" / (digest(b"SYNTHETIC=")[:24] + ".json")

    def fail_initial_batch(self):
        calls = []
        def failure(method, url, headers, body, timeout):
            calls.append((method, url))
            return 500, {}, b"{}"
        with self.assertRaisesRegex(TraceError, "HTTP 500"):
            sync(self.plan, "SYNTHETIC=", self.path, token="SYNTHETIC-token", transport=failure, interval=0)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1].endswith("/items/bulk"))
        return read_json(self.path)

    def test_pending_sync_rejects_before_elk_for_live_preview_reorganize_and_explicit_plan(self):
        state = self.fail_initial_batch()
        before = self.path.read_bytes()
        archive = {p.name: p.read_bytes() for p in self.archive.iterdir() if p.is_file()}
        for options in ({}, {"dry_run": True}, {"reorganize": True},
                        {"plan_path": self.archive / "miro-plan.json"}):
            with self.subTest(options=options), \
                    patch("liquid_tracer.cli.refresh_presentation", side_effect=AssertionError("No ELK while pending")), \
                    patch("liquid_tracer.cli.sync", side_effect=AssertionError("No publication while pending")):
                with self.assertRaisesRegex(TraceError, "Prior Miro POST outcome is uncertain") as caught:
                    sync_run(self.case, self.run, **options)
                self.assertIn(str(len(state["pending_creations"])) + " item(s)", str(caught.exception))
                self.assertIn("miro-recover", str(caught.exception))
                self.assertLess(len(str(caught.exception)), 1100)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual({p.name: p.read_bytes() for p in self.archive.iterdir() if p.is_file()}, archive)

    def test_recovery_summary_is_local_and_contains_no_keys_paths_or_board_content(self):
        self.assertEqual(miro_recovery_status(self.case), {"pending_count": 0, "can_confirm_empty": False})
        state = self.fail_initial_batch()
        before = self.path.read_bytes()
        with patch("liquid_tracer.miro_recovery.MiroHTTP", side_effect=AssertionError("No API status lookup")):
            summary = miro_recovery_status(self.case)
        self.assertEqual(summary, {"pending_count": len(state["pending_creations"]), "can_confirm_empty": True})
        self.assertNotIn(str(self.case), json.dumps(summary))
        self.assertNotIn(A, json.dumps(summary))
        self.assertEqual(self.path.read_bytes(), before)

    def test_wrong_board_namespace_and_malformed_state_never_offer_empty_recovery(self):
        state = self.fail_initial_batch()
        for field, value in (("board_id", "OTHER="), ("namespace", {**state["namespace"], "case_id": "f" * 32}),
                             ("pending", "broken"), ("shape_batch_size", False)):
            with self.subTest(field=field):
                save_json(self.path, {**state, field: value})
                summary = miro_recovery_status(self.case)
                self.assertFalse(summary["can_confirm_empty"])
                self.assertTrue(summary["unavailable"])

    def test_recovery_dispatch_uses_saved_board_namespace_without_layout_or_trace(self):
        report = {"recovered_items": 20, "shape_batch_size": 1, "board_id": "SYNTHETIC=",
                  "recovery": "confirmed_empty_board"}
        before = {p.name: p.read_bytes() for p in self.archive.iterdir() if p.is_file()}
        with patch("liquid_tracer.miro_recovery.recover_empty_board", return_value=report) as recover, \
                patch("liquid_tracer.cli.refresh_presentation", side_effect=AssertionError("No ELK")), \
                patch.dict("os.environ", {"LIQUID_MIRO_BOARD": "OTHER="}):
            result = recover_miro_run(self.case, True)
        recover.assert_called_once_with(self.path, "SYNTHETIC=", self.plan["namespace"],
                                        confirmed_empty=True, progress=None)
        self.assertEqual(result["recovered_items"], 20)
        self.assertEqual(result["board_url"], "https://miro.com/app/board/SYNTHETIC%3D/")
        self.assertEqual({p.name: p.read_bytes() for p in self.archive.iterdir() if p.is_file()}, before)

    def test_recovery_requires_confirmation_and_saved_board(self):
        with patch("liquid_tracer.miro_recovery.recover_empty_board", side_effect=AssertionError("No API")):
            for confirmed in (False, None, 1, "true"):
                with self.subTest(confirmed=confirmed), self.assertRaisesRegex(TraceError, "confirm"):
                    recover_miro_run(self.case, confirmed)
            update_case(self.case, {"miro_board": None})
            with patch.dict("os.environ", {"LIQUID_MIRO_BOARD": "OTHER="}), \
                    self.assertRaisesRegex(TraceError, "no linked"):
                recover_miro_run(self.case, True)

    def test_command_requires_explicit_confirmation_and_returns_json(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as missing:
            main(["miro-recover", "--case", str(self.case)])
        self.assertEqual(missing.exception.code, 2)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("liquid_tracer.cli.recover_miro_run", return_value={"recovered_items": 20}) as recover:
            self.assertEqual(main(["miro-recover", "--case", str(self.case), "--confirm-empty"]), 0)
        self.assertEqual(json.loads(output.getvalue()), {"recovered_items": 20})
        self.assertEqual(recover.call_args.args, (self.case, True))


if __name__ == "__main__":
    unittest.main()
