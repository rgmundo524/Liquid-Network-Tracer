"""Framing reuses the exact completed sync, without layout or data fetching."""

import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from liquid_tracer.cli import frame_run, main, parser
from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.investigations import update_case
from liquid_tracer.miro import sync
from liquid_tracer.miro_state import load_state
from tests.test_miro_frame_sync import FrameMiro
from tests import test_miro_recovery_cli


class MiroFramesCliTests(unittest.TestCase):
    setUp = test_miro_recovery_cli.MiroRecoveryCliTests.setUp

    def graph_sync(self):
        self.remote = FrameMiro()
        sync(self.plan, "SYNTHETIC=", self.path, token="synthetic", transport=self.remote, interval=0)
        self.remote.calls.clear()

    def test_frame_action_uses_saved_presentation_after_defaults_change_without_layout_or_fetch(self):
        from liquid_tracer.miro import sync_frames
        self.graph_sync()
        archived = {p.name: p.read_bytes() for p in self.archive.iterdir() if p.is_file()}
        update_case(self.case, {"run_defaults": {"include_fees": not self.plan.get("include_fees", True),
                                                 "group_context_inputs": True, "connector_style": "curved"}})
        metadata = (self.case / "case.json").read_bytes()
        events = []
        def frames(*args, **kwargs):
            self.assertEqual(args[0], self.plan)
            return sync_frames(*args, **kwargs, token="synthetic", transport=self.remote, interval=0)
        with patch("liquid_tracer.miro.sync_frames", side_effect=frames) as called, \
                patch("liquid_tracer.cli.refresh_presentation", side_effect=AssertionError("No ELK")), \
                patch("liquid_tracer.cli.ensure_counts", side_effect=AssertionError("No fetching")), \
                patch("liquid_tracer.cli.trace", side_effect=AssertionError("No tracing")):
            report = frame_run(self.case, progress=events.append)
        self.assertEqual(called.call_count, 2)
        self.assertTrue(report["frames_only"])
        self.assertEqual(report["plan_sha256"], self.plan["sha256"])
        self.assertGreater(report["created"], 0)
        self.assertTrue(events)
        self.assertEqual(read_json(report["report_file"]), report)
        writes = [(method, url) for method, url, _ in self.remote.calls if method != "GET"]
        self.assertTrue(writes)
        self.assertTrue(all(url.endswith("/frames") for _, url in writes))
        self.assertEqual((self.case / "case.json").read_bytes(), metadata)
        self.assertEqual({p.name: p.read_bytes() for p in self.archive.iterdir() if p.is_file()}, archived)

    def test_dry_run_is_read_only_and_does_not_need_credentials(self):
        self.graph_sync()
        before = {str(p.relative_to(self.case)): p.read_bytes() for p in self.case.rglob("*") if p.is_file()}
        with patch("liquid_tracer.miro.MiroHTTP", side_effect=AssertionError("No Miro requests")), \
                patch("liquid_tracer.cli.refresh_presentation", side_effect=AssertionError("No layout")):
            report = frame_run(self.case, dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertTrue(report["frames_only"])
        self.assertGreater(report["new_frames"], 0)
        self.assertEqual({str(p.relative_to(self.case)): p.read_bytes() for p in self.case.rglob("*") if p.is_file()}, before)

    def test_unsynced_or_different_selected_run_fails_before_any_remote_action(self):
        with patch("liquid_tracer.miro.sync_frames", side_effect=AssertionError("No Miro")):
            with self.assertRaisesRegex(TraceError, "Sync this saved graph"):
                frame_run(self.case)
        self.graph_sync()
        saved = load_state(self.path)
        save_json(self.path, {**saved, "latest_run_id": "f" * 16})
        with patch("liquid_tracer.miro.sync_frames", side_effect=AssertionError("No Miro")), \
                self.assertRaisesRegex(TraceError, "latest completed"):
            frame_run(self.case)

    def test_legacy_exact_archive_can_frame_but_missing_published_presentation_requests_graph_sync(self):
        self.graph_sync()
        saved = load_state(self.path)
        saved.pop("frame_plan")
        save_json(self.path, saved)
        self.assertTrue(frame_run(self.case, dry_run=True)["frames_only"])
        saved["runs"][self.run]["plan_sha256s"] = ["a" * 64]
        save_json(self.path, saved)
        with patch("liquid_tracer.miro.sync_frames", side_effect=AssertionError("No Miro")), \
                self.assertRaisesRegex(TraceError, "Sync to Miro once"):
            frame_run(self.case, dry_run=True)

    def test_damaged_snapshot_and_wrong_board_are_rejected_locally(self):
        self.graph_sync()
        saved = load_state(self.path)
        modified = copy.deepcopy(saved)
        modified["frame_plan"]["shapes"][0]["body"]["position"]["x"] += 1
        save_json(self.path, modified)
        with patch("liquid_tracer.miro.sync_frames", side_effect=AssertionError("No Miro")):
            with self.assertRaises(TraceError):
                frame_run(self.case)
            with self.assertRaisesRegex(TraceError, "Sync this saved graph"):
                frame_run(self.case, board="OTHER=")
            for value in (True, -1, "750"):
                with self.subTest(value=value), self.assertRaisesRegex(TraceError, "nonnegative integer"):
                    frame_run(self.case, max_new_items=value)

    def test_parser_and_dispatch_offer_no_layout_or_trace_options(self):
        parsed = parser().parse_args(["miro-frames", "--case", str(self.case)])
        self.assertEqual((parsed.run, parsed.max_new_items, parsed.dry_run), ("latest", 750, False))
        self.assertFalse(hasattr(parsed, "layout_attempts"))
        output = io.StringIO()
        with patch("liquid_tracer.cli.frame_run", return_value={"frames_only": True}) as frames, \
                contextlib.redirect_stdout(output):
            status = main(["miro-frames", "--case", str(self.case), "--run", self.run,
                           "--board", "SYNTHETIC=", "--max-new-items", "12", "--dry-run"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue()), {"frames_only": True})
        self.assertEqual(frames.call_args.args, (self.case, self.run, "SYNTHETIC=", 12, True))


if __name__ == "__main__":
    unittest.main()
