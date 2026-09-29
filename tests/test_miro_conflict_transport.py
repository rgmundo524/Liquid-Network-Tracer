"""Only typed, bounded Miro edit differences cross a failed job's boundary."""

import contextlib
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.web import MAX_FAILURE_RESULT_BYTES, read_edit_conflicts
from liquid_tracer.web_worker import main as worker_main
from tests import test_web


SENTINEL = "SYNTHETIC-PRIVATE-CREDENTIAL-OR-PATH"


def sample_report():
    return {
        "kind": "miro_edit_conflicts", "board_id": "BOARD=", "truncated": False,
        "items": [{
            "key": "addr:synthetic-address", "item_id": "item-1", "kind": "shape",
            "object_url": "https://miro.com/app/board/BOARD%3D/?moveToWidget=item-1",
            "changes": [{
                "field": "data.content",
                "saved": {"present": True, "value": "Original label", "type": "string"},
                "current": {"present": True, "value": "<script>accidental edit</script>", "type": "string"},
            }], "truncated": False,
        }],
    }


class ConflictWorkerTests(unittest.TestCase):
    def run_worker(self, cli):
        with tempfile.TemporaryDirectory() as directory:
            request, result = Path(directory) / "request.json", Path(directory) / "result.json"
            request.write_text(json.dumps({"arguments": ["miro-sync", "--case", "/synthetic/case"]}))
            stderr = io.StringIO()
            with patch("liquid_tracer.web_worker.cli_main", side_effect=cli), \
                    patch("liquid_tracer.web_worker.signal.signal"), contextlib.redirect_stderr(stderr):
                status = worker_main([str(request), str(result)])
            self.assertTrue(result.is_file(), stderr.getvalue())
            self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o600)
            return status, json.loads(result.read_text()), stderr.getvalue()

    def test_only_known_callback_report_crosses_failure_boundary(self):
        def fail(arguments, *, progress, diagnostics):
            report = sample_report()
            report["token"] = SENTINEL
            report["items"][0]["private_path"] = SENTINEL
            diagnostics(report)
            print(SENTINEL)
            return 1

        status, result, stderr = self.run_worker(fail)
        self.assertEqual(status, 1)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["result"])
        self.assertEqual(result["edit_conflicts"]["items"][0]["changes"][0]["field"], "data.content")
        self.assertNotIn(SENTINEL, json.dumps(result))
        self.assertIn(SENTINEL, stderr)

    def test_stdout_lookalike_report_and_unknown_callback_do_not_become_diagnostics(self):
        def fail(arguments, *, progress, diagnostics):
            diagnostics({"kind": "provider_error", "message": SENTINEL})
            print(json.dumps({"edit_conflicts": sample_report(), "token": SENTINEL}))
            return 1

        status, result, stderr = self.run_worker(fail)
        self.assertEqual(status, 1)
        self.assertEqual(result, {"ok": False, "result": None})
        self.assertIn(SENTINEL, stderr)

    def test_success_keeps_original_result_contract(self):
        def succeed(arguments, *, progress, diagnostics):
            diagnostics(sample_report())
            print(json.dumps({"created": 1}))
            return 0

        status, result, _ = self.run_worker(succeed)
        self.assertEqual(status, 0)
        self.assertEqual(result, {"ok": True, "result": {"created": 1}})

    def test_real_cli_generic_errors_remain_terminal_only(self):
        from liquid_tracer.cli import main

        with patch("liquid_tracer.cli.sync_run", side_effect=TraceError(SENTINEL)):
            status, result, stderr = self.run_worker(main)
        self.assertEqual(status, 1)
        self.assertEqual(result, {"ok": False, "result": None})
        self.assertIn(SENTINEL, stderr)

    def test_real_cli_typed_cause_reaches_worker_diagnostic_callback(self):
        from liquid_tracer.cli import main
        from liquid_tracer.miro_conflicts import MiroEditConflict

        def fail_sync(*args, **kwargs):
            try:
                raise MiroEditConflict("Context object has manual edits", sample_report())
            except TraceError as error:
                raise TraceError("Wrapper error: " + SENTINEL) from error

        with patch("liquid_tracer.cli.sync_run", side_effect=fail_sync):
            status, result, stderr = self.run_worker(main)
        self.assertEqual(status, 1)
        self.assertEqual(result["edit_conflicts"]["items"][0]["item_id"], "item-1")
        self.assertNotIn(SENTINEL, json.dumps(result))
        self.assertIn(SENTINEL, stderr)
        self.assertIn("Open object:", stderr)

    def test_real_cli_untyped_error_cannot_spoof_a_known_report_attribute(self):
        from liquid_tracer.cli import main

        error = TraceError(SENTINEL)
        error.report = sample_report()
        with patch("liquid_tracer.cli.sync_run", side_effect=error):
            status, result, _ = self.run_worker(main)
        self.assertEqual(status, 1)
        self.assertEqual(result, {"ok": False, "result": None})


class ConflictFailureResultTests(unittest.TestCase):
    def test_failed_file_reader_rejects_invalid_envelopes_oversize_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "result.json"
            report = sample_report()
            invalid = [None, [], {"ok": True, "result": None, "edit_conflicts": report},
                       {"ok": 0, "result": None, "edit_conflicts": report},
                       {"ok": False, "edit_conflicts": report},
                       {"ok": False, "result": {"token": SENTINEL}, "edit_conflicts": report},
                       {"ok": False, "result": None, "edit_conflicts": {"kind": "provider_error"}}]
            for payload in invalid:
                result.write_text(json.dumps(payload))
                self.assertIsNone(read_edit_conflicts(result), payload)
            result.write_text("{" + SENTINEL)
            self.assertIsNone(read_edit_conflicts(result))
            result.write_text(" " * (MAX_FAILURE_RESULT_BYTES + 1))
            self.assertIsNone(read_edit_conflicts(result))
            target = Path(directory) / "target.json"
            target.write_text(json.dumps({"ok": False, "result": None, "edit_conflicts": report}))
            result.unlink()
            result.symlink_to(target)
            self.assertIsNone(read_edit_conflicts(result))
            self.assertIsNotNone(read_edit_conflicts(target))


class ConflictJobTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success

    def failed_job(self, payload, *, action="plot-sync"):
        identity = "a" * 32
        self.server.jobs[identity] = {"id": identity, "status": "running", "action": action}
        self.server.active_job = identity

        class FailedWorker:
            def __init__(self, command, **kwargs):
                self.pid = 1234
                Path(command[-1]).write_text(json.dumps(payload))

            def wait(self, timeout=None):
                return 1

            def poll(self):
                return 1

        with patch("liquid_tracer.web.subprocess.Popen", FailedWorker), contextlib.redirect_stderr(io.StringIO()):
            self.server.run_job(identity, [], action, False, None, None)
        self.assertIsNone(self.server.active_job)
        return self.success("/api/jobs/" + identity)

    def test_failed_job_exposes_whitelisted_differences_and_generated_object_link(self):
        report = sample_report()
        report["token"] = SENTINEL
        report["items"][0]["object_url"] = "https://attacker.invalid/" + SENTINEL
        report["items"][0]["private_path"] = SENTINEL
        payload = {"ok": False, "result": None, "edit_conflicts": report,
                   "error": SENTINEL, "provider": {"token": SENTINEL}}
        status = self.failed_job(payload)
        self.assertEqual(status["status"], "failed")
        self.assertFalse(status["cancellable"])
        self.assertNotIn("result", status)
        self.assertIn("last-synced values", status["message"])
        item = status["edit_conflicts"]["items"][0]
        self.assertTrue(item["object_url"].startswith("https://miro.com/app/board/"))
        self.assertIn("moveToWidget=item-1", item["object_url"])
        self.assertEqual(item["changes"][0]["current"]["value"], "<script>accidental edit</script>")
        self.assertNotIn(SENTINEL, json.dumps(status))
        self.assertNotIn(str(self.base), json.dumps(status))

    def test_malformed_or_unknown_reports_keep_existing_generic_failure_messages(self):
        for report in (None, {"kind": "provider_error", "message": SENTINEL},
                       {**sample_report(), "board_id": "https://attacker.invalid/" + SENTINEL}):
            with self.subTest(report=report):
                status = self.failed_job({"ok": False, "result": None, "edit_conflicts": report})
                self.assertEqual(status["status"], "failed")
                self.assertNotIn("edit_conflicts", status)
                self.assertIn("Plot and sync stopped", status["message"])
                self.assertNotIn(SENTINEL, json.dumps(status))
        status = self.failed_job({"ok": False, "result": None, "error": SENTINEL}, action="miro-sync")
        self.assertIn("Action failed. Check the launching terminal", status["message"])
        self.assertNotIn("edit_conflicts", status)

    def test_server_reapplies_report_limits_to_private_worker_file(self):
        report = sample_report()
        report["items"][0]["changes"] = [{
            "field": "captions[" + str(index) + "].content",
            "saved": {"present": True, "value": "A" * 1100, "type": "string"},
            "current": {"present": True, "value": "B" * 1100, "type": "string"},
        } for index in range(30)]
        status = self.failed_job({"ok": False, "result": None, "edit_conflicts": report})
        clean = status["edit_conflicts"]
        self.assertTrue(clean["truncated"])
        self.assertLessEqual(len(clean["items"][0]["changes"]), 20)
        values = [change[side]["value"] for item in clean["items"]
                  for change in item["changes"] for side in ("saved", "current")]
        self.assertTrue(all(len(value) <= 1000 for value in values))
        self.assertLessEqual(sum(map(len, values)), 12000)


if __name__ == "__main__":
    unittest.main()
