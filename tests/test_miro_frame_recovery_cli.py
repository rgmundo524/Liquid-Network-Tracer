"""Frame recovery stays bound to the interrupted run and saved linked board."""

import contextlib
import io
import json
import unittest
from unittest.mock import patch

from liquid_tracer.cli import main, miro_recovery_status, recover_miro_frame, save_latest
from liquid_tracer.common import TraceError, save_json
from liquid_tracer.miro import sync
from liquid_tracer.miro_state import load_state
from tests import test_miro_recovery_cli
from tests.test_miro_frame_sync import FrameMiro


class FrameRecoveryCliTests(unittest.TestCase):
    setUp = test_miro_recovery_cli.MiroRecoveryCliTests.setUp

    def fail_frame(self):
        from liquid_tracer.miro import sync_frames
        remote = FrameMiro()
        sync(self.plan, "SYNTHETIC=", self.path, token="synthetic", transport=remote, interval=0)

        def transport(method, url, headers, body, timeout):
            if method == "POST" and url.endswith("/frames"):
                return 500, {}, b"{}"
            return remote(method, url, headers, body, timeout)

        with self.assertRaisesRegex(TraceError, "HTTP 500"):
            sync_frames(self.plan, "SYNTHETIC=", self.path, token="synthetic", transport=transport, interval=0)
        return load_state(self.path)

    def test_status_and_review_use_interrupted_archive_without_layout_or_network_status_lookup(self):
        state = self.fail_frame()
        before = {p.name: p.read_bytes() for p in self.archive.iterdir() if p.is_file()}
        save_latest(self.case, "f" * 16)
        with patch("liquid_tracer.miro_frame_recovery.review_pending_frame", return_value={"run_id": self.run}) as review, \
                patch("liquid_tracer.cli.refresh_presentation", side_effect=AssertionError("No ELK")), \
                patch.dict("os.environ", {"LIQUID_MIRO_BOARD": "OTHER="}):
            self.assertEqual(miro_recovery_status(self.case), {
                "pending_count": 1, "can_confirm_empty": False, "can_recover_frame": True})
            review.assert_not_called()
            result = recover_miro_frame(self.case)
        review.assert_called_once_with(self.path, "SYNTHETIC=", state["namespace"], progress=None)
        self.assertEqual(result["board_url"], "https://miro.com/app/board/SYNTHETIC%3D/")
        self.assertEqual(result["run_id"], self.run)
        self.assertEqual(result["resume_action"], "miro-frames")
        self.assertEqual(load_state(self.path), state)
        self.assertEqual({p.name: p.read_bytes() for p in self.archive.iterdir() if p.is_file()}, before)

    def test_legacy_interrupted_graph_sync_is_directed_to_sync_once_before_framing(self):
        state = self.fail_frame()
        state.pop("frame_plan")
        state.pop("active_frame_run_id", None)
        state.update(active_run_id=self.run, latest_run_id=None, runs={})
        save_json(self.path, state)
        with patch("liquid_tracer.miro_frame_recovery.review_pending_frame", return_value={"run_id": self.run}):
            report = recover_miro_frame(self.case)
        self.assertEqual(report["resume_action"], "miro-sync")

    def test_apply_passes_exact_review_and_explicit_choice(self):
        state = self.fail_frame()
        with patch("liquid_tracer.miro_frame_recovery.recover_pending_frame", return_value={"run_id": self.run}) as recover:
            recover_miro_frame(self.case, review_id="a" * 64, item_id="frame-123")
        recover.assert_called_once_with(self.path, "SYNTHETIC=", state["namespace"], review_id="a" * 64,
                                        item_id="frame-123", confirmed_absent=False, progress=None)

    def test_wrong_source_or_archive_and_missing_review_never_reach_miro(self):
        state = self.fail_frame()
        with patch("liquid_tracer.miro_frame_recovery.review_pending_frame", side_effect=AssertionError("No Miro")), \
                patch("liquid_tracer.miro_frame_recovery.recover_pending_frame", side_effect=AssertionError("No Miro")):
            with self.assertRaisesRegex(TraceError, "Review"):
                recover_miro_frame(self.case, item_id="existing")
            save_json(self.path, {**state, "namespace": {**state["namespace"], "source": "other-source"}})
            with self.assertRaisesRegex(TraceError, "archive"):
                recover_miro_frame(self.case)
            save_json(self.path, state)
            (self.archive / "trace.json").write_text("{}")
            with self.assertRaises(TraceError):
                recover_miro_frame(self.case)

    def test_cli_requires_choice_and_preserves_existing_output(self):
        for extra in ([], ["--review-id", "a" * 64],
                      ["--review-id", "a" * 64, "--item-id", "123", "--confirm-absent"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(["miro-frame-recover", "--case", str(self.case), *extra])
        output = self.case / "review.json"
        output.write_text("keep me")
        with patch("liquid_tracer.cli.recover_miro_frame", side_effect=AssertionError("No action")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["miro-frame-review", "--case", str(self.case), "--output", str(output)]), 1)
        self.assertEqual(output.read_text(), "keep me")
        result = {"review_id": "a" * 64, "run_id": self.run}
        output.unlink()
        with patch("liquid_tracer.cli.recover_miro_frame", return_value=result) as recover, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["miro-frame-review", "--case", str(self.case), "--output", str(output)]), 0)
        self.assertEqual(json.loads(output.read_text()), result)
        self.assertEqual(recover.call_args.args, (self.case,))


if __name__ == "__main__":
    unittest.main()
