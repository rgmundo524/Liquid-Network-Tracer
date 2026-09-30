"""Credential handoff waits are bounded and cancellation keeps gate ownership."""

import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from liquid_tracer.web import LocalServer
from liquid_tracer.web_worker import finish_terminal_handoff, main


class WorkerCredentialHandoffTests(unittest.TestCase):
    def test_worker_detaches_before_ready_and_waits_for_acknowledgement(self):
        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory) / "request.json"
            ready, acknowledgement = request.parent / "credentials-ready", request.parent / "credentials-ack"

            def acknowledge(_):
                detach.assert_called_once()
                self.assertEqual(detach.call_args.args[1], 0)
                self.assertEqual(ready.read_text(), "ready\n")
                self.assertEqual(stat.S_IMODE(ready.stat().st_mode), 0o600)
                acknowledgement.write_text("ready\n")

            with patch("liquid_tracer.web_worker.os.dup2") as detach, \
                    patch("liquid_tracer.web_worker.time.sleep", side_effect=acknowledge):
                finish_terminal_handoff(request, os.getpid())
            self.assertTrue(acknowledgement.exists())

    def test_missing_acknowledgement_times_out_without_starting_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            request, result = Path(directory) / "request.json", Path(directory) / "result.json"
            request.write_text(json.dumps({"arguments": [], "terminal_handoff": True, "server_pid": os.getpid()}))
            with patch("liquid_tracer.web_worker.os.dup2"), \
                    patch("liquid_tracer.web_worker.signal.signal"), \
                    patch("liquid_tracer.web_worker.time.monotonic", side_effect=[0, 31]), \
                    patch("liquid_tracer.web_worker.cli_main") as cli:
                self.assertEqual(main([str(request), str(result)]), 1)
            cli.assert_not_called()
            self.assertFalse(result.exists())
            self.assertTrue((request.parent / "credentials-ready").exists())

    def test_dead_server_aborts_before_cli_can_write(self):
        with tempfile.TemporaryDirectory() as directory:
            request, result = Path(directory) / "request.json", Path(directory) / "result.json"
            request.write_text(json.dumps({"arguments": [], "terminal_handoff": True, "server_pid": os.getpid()}))
            with patch("liquid_tracer.web_worker.os.dup2"), \
                    patch("liquid_tracer.web_worker.signal.signal"), \
                    patch("liquid_tracer.web_worker.os.kill", side_effect=ProcessLookupError), \
                    patch("liquid_tracer.web_worker.cli_main") as cli:
                self.assertEqual(main([str(request), str(result)]), 1)
            cli.assert_not_called()
            self.assertFalse(result.exists())

    def test_cancel_waiting_for_credentials_does_not_release_another_jobs_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = LocalServer(root, root, port=0)
            server.credential_lock.acquire()
            try:
                with patch("liquid_tracer.web.subprocess.Popen") as launch:
                    job = server.start_job([], action="layout", live=True)
                    thread = server.job_thread
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        with server.job_lock:
                            if server.jobs[job["id"]]["message"].startswith("Waiting for another"):
                                break
                        time.sleep(.01)
                    else:
                        self.fail("The queued credential job did not start waiting")
                    with server.job_lock:
                        server.cancel_job(job["id"])
                    thread.join(timeout=3)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(server.jobs[job["id"]]["status"], "canceled")
                    self.assertTrue(server.credential_lock.locked())
                    self.assertEqual(server.processes, {})
                    launch.assert_not_called()
            finally:
                server.credential_lock.release()
                server.server_close()

    def test_failed_terminal_restoration_never_acknowledges_cli_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = LocalServer(root, root, port=0)
            process = Mock()
            request_path = None

            def launch(command, **_):
                nonlocal request_path
                request_path = Path(command[0])
                (request_path.parent / "credentials-ready").write_text("ready\n")
                return process

            def restore(terminal, *, strict=False):
                self.assertFalse((request_path.parent / "credentials-ack").exists())
                if strict:
                    raise OSError("Synthetic terminal disconnected")

            try:
                with patch("liquid_tracer.web.threading.Thread.start"):
                    job = server.start_job([], action="layout", live=True)
                with patch("liquid_tracer.web.worker_command", side_effect=lambda request, result, live: [str(request)]), \
                        patch("liquid_tracer.web.subprocess.Popen", side_effect=launch), \
                        patch("liquid_tracer.web.handoff_terminal", return_value=(0, 42)), \
                        patch("liquid_tracer.web.restore_terminal", side_effect=restore) as restored, \
                        patch("liquid_tracer.web.stop_worker") as stop:
                    server.run_job(job["id"], [], "layout", True, None, None)
                self.assertEqual(server.jobs[job["id"]]["status"], "failed")
                self.assertFalse(server.credential_lock.locked())
                self.assertEqual(restored.call_args_list[0].kwargs, {"strict": True})
                stop.assert_called_once_with(process)
                process.wait.assert_not_called()
            finally:
                server.server_close()


if __name__ == "__main__":
    unittest.main()
