import contextlib
import http.client
import io
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import main as cli_main
from liquid_tracer.progress import ProgressReporter, public_progress
from liquid_tracer.web import LocalServer
from liquid_tracer.web_worker import main as worker_main


SENTINEL = "SYNTHETIC-PRIVATE-PROGRESS-SENTINEL"


class ProgressReportTests(unittest.TestCase):
    def test_only_known_progress_fields_reach_the_browser_or_terminal(self):
        for phase in ("optimizing", "preflight", "layout", "updating", "removing", "creating", "waiting", "complete"):
            with self.subTest(phase=phase):
                result = public_progress({"phase": phase, "completed": 2, "total": 4,
                                          "message": SENTINEL, "token": SENTINEL,
                                          "private_annotation": {"text": SENTINEL}})
                self.assertEqual(result["phase"], phase)
                self.assertEqual((result["completed"], result["total"]), (2, 4))
                self.assertTrue(result["message"])
                self.assertNotIn(SENTINEL, json.dumps(result))
                self.assertNotIn("token", result)
                self.assertNotIn("private_annotation", result)

    def test_invalid_progress_counts_and_retry_delays_cannot_break_json_consumers(self):
        valid = {"phase": "preflight", "completed": 0, "total": 5}
        invalid = [None, [], SENTINEL, {}, {**valid, "phase": SENTINEL},
                   {**valid, "phase": []}, {**valid, "phase": {}}]
        for key in ("completed", "total"):
            invalid.extend({**valid, key: value} for value in
                           (True, False, -1, 1.5, "1", None, float("nan"), float("inf"), 2 ** 53))
        invalid.extend(({**valid, "completed": 6}, {"phase": "creating", "completed": 1}))
        for event in invalid:
            with self.subTest(event=event):
                self.assertIsNone(public_progress(event))
        for retry_after in (0, 2.5, 30):
            result = public_progress({**valid, "phase": "waiting", "retry_after": retry_after})
            self.assertEqual(result["retry_after"], retry_after)
            json.dumps(result, allow_nan=False)
        for retry_after in (True, -1, 31, 10 ** 400, "2", None, float("nan"), float("inf")):
            with self.subTest(retry_after=retry_after):
                result = public_progress({**valid, "phase": "waiting", "retry_after": retry_after})
                self.assertNotIn("retry_after", result)
                json.dumps(result, allow_nan=False)

    def test_reporter_keeps_ephemeral_file_private_and_stdout_as_result_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            stdout, stderr = io.StringIO(), io.StringIO()
            previous_umask = os.umask(0)
            self.addCleanup(os.umask, previous_umask)
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                reporter = ProgressReporter(path)
                reporter({"phase": "preflight", "completed": 0, "total": 3,
                          "message": SENTINEL, "token": SENTINEL})
                first = json.loads(path.read_text())
                self.assertEqual(first["phase"], "preflight")
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                reporter({"phase": "waiting", "completed": 1, "total": 3, "retry_after": 2})
                self.assertEqual(json.loads(path.read_text())["phase"], "waiting")
                reporter({"phase": "complete", "completed": 3, "total": 3})
                print(json.dumps({"created": 2}))
            self.assertEqual(json.loads(stdout.getvalue()), {"created": 2})
            self.assertEqual(json.loads(path.read_text())["phase"], "complete")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(sorted(item.name for item in Path(directory).iterdir()), ["progress.json"])
            self.assertTrue(stderr.getvalue())
            self.assertNotIn(SENTINEL, stderr.getvalue() + path.read_text())

    def test_elk_heartbeats_report_elapsed_without_inventing_completion_counts(self):
        event = {"phase": "optimizing", "completed": 0, "total": 0, "elapsed_seconds": 35}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            ProgressReporter()(event)
        self.assertEqual(public_progress(event)["elapsed_seconds"], 35)
        self.assertIn("35s elapsed", stderr.getvalue())
        self.assertNotIn("(0/0)", stderr.getvalue())
        for elapsed in (True, -1, float("inf"), float("nan"), 10 ** 400, "30", None):
            with self.subTest(elapsed=elapsed):
                self.assertNotIn("elapsed_seconds", public_progress({**event, "elapsed_seconds": elapsed}))

    def test_count_updates_are_throttled_but_phase_changes_and_completion_arrive_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            stderr = io.StringIO()
            with patch("liquid_tracer.progress.time.monotonic", side_effect=[0, .05, .15, 1.05, 1.06, 1.07]), \
                    contextlib.redirect_stderr(stderr):
                reporter = ProgressReporter(path)
                reporter({"phase": "preflight", "completed": 0, "total": 20})
                reporter({"phase": "preflight", "completed": 1, "total": 20})
                self.assertEqual(json.loads(path.read_text())["completed"], 0)
                reporter({"phase": "preflight", "completed": 2, "total": 20})
                self.assertEqual(json.loads(path.read_text())["completed"], 2)
                self.assertEqual(len(stderr.getvalue().splitlines()), 1)
                reporter({"phase": "preflight", "completed": 3, "total": 20})
                self.assertEqual(len(stderr.getvalue().splitlines()), 2)
                reporter({"phase": "creating", "completed": 0, "total": 2})
                self.assertEqual(json.loads(path.read_text())["phase"], "creating")
                reporter({"phase": "creating", "completed": 2, "total": 2})
                self.assertEqual(json.loads(path.read_text())["completed"], 2)
                self.assertEqual(len(stderr.getvalue().splitlines()), 4)

    def test_broken_progress_output_does_not_interrupt_acknowledged_sync_work(self):
        with tempfile.TemporaryDirectory() as directory:
            closed_terminal = io.StringIO()
            closed_terminal.close()
            reporter = ProgressReporter(Path(directory) / "missing-directory" / "progress.json")
            with contextlib.redirect_stderr(closed_terminal):
                reporter({"phase": "creating", "completed": 1, "total": 3})
                reporter({"phase": "complete", "completed": 3, "total": 3})
            self.assertFalse((Path(directory) / "missing-directory").exists())

    def test_cli_callback_does_not_contaminate_successful_json(self):
        received = []

        def pretend_sync(*args, progress=None, **kwargs):
            self.assertIsNotNone(progress)
            progress({"phase": "preflight", "completed": 0, "total": 2})
            progress({"phase": "complete", "completed": 2, "total": 2})
            return {"created": 1, "updated": 0}

        output = io.StringIO()
        with patch("liquid_tracer.cli.sync_run", side_effect=pretend_sync), contextlib.redirect_stdout(output):
            status = cli_main(["miro-sync", "--case", "/synthetic/case", "--run", "latest", "--dry-run"],
                              progress=received.append)
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue()), {"created": 1, "updated": 0})
        self.assertEqual([event["phase"] for event in received], ["preflight", "complete"])

    def test_worker_supplies_private_progress_reporter_without_changing_result_format(self):
        with tempfile.TemporaryDirectory() as directory:
            request, result = Path(directory) / "request.json", Path(directory) / "result.json"
            request.write_text(json.dumps({"arguments": ["miro-sync", "--dry-run"]}))

            def pretend_cli(arguments, *, progress=None):
                self.assertEqual(arguments, ["miro-sync", "--dry-run"])
                self.assertIsInstance(progress, ProgressReporter)
                progress({"phase": "updating", "completed": 1, "total": 2, "message": SENTINEL})
                print(json.dumps({"created": 0, "updated": 1}))
                return 0

            stderr = io.StringIO()
            with patch("liquid_tracer.web_worker.cli_main", side_effect=pretend_cli), \
                    patch("liquid_tracer.web_worker.signal.signal"), contextlib.redirect_stderr(stderr):
                status = worker_main([str(request), str(result)])
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(result.read_text()), {"ok": True, "result": {"created": 0, "updated": 1}})
            progress_path = request.parent / "progress.json"
            self.assertEqual(json.loads(progress_path.read_text())["phase"], "updating")
            self.assertEqual(stat.S_IMODE(progress_path.stat().st_mode), 0o600)
            self.assertNotIn(SENTINEL, stderr.getvalue() + progress_path.read_text())


class InflightWebProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        assets = self.base / "assets"
        assets.mkdir()
        (assets / "index.html").write_text("<!doctype html><title>Synthetic progress test</title>")
        self.server = LocalServer(self.base / "cases", assets, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def get(self, route):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("GET", route)
            response = connection.getresponse()
            data = json.loads(response.read())
            self.assertEqual(response.status, 200, data)
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            self.assertNotIn(SENTINEL, json.dumps(data))
            return data
        finally:
            connection.close()

    def wait_for(self, identity, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            job = self.get("/api/jobs/" + identity)
            if predicate(job):
                return job
            if job["status"] != "running":
                self.fail("Synthetic worker finished before expected progress: " + json.dumps(job))
            time.sleep(.03)
        self.fail("No progress update arrived while the synthetic worker was running")

    def test_running_job_reports_sanitized_stages_and_survives_page_reconnection(self):
        # Gate each stage from this process: the HTTP observer must see genuine
        # in-flight progress, rather than progress recovered only after exit.
        worker = self.base / "synthetic_worker.py"
        worker.write_text('''import json, os, sys, time
from pathlib import Path
request, result, gates = map(Path, sys.argv[1:])
progress = request.parent / "progress.json"
def emit(event):
    pending = progress.with_suffix(".pending")
    pending.write_text(json.dumps(event))
    pending.chmod(0o600)
    os.replace(pending, progress)
def gate(name):
    deadline = time.monotonic() + 20
    while not (gates / name).exists():
        if time.monotonic() > deadline:
            raise RuntimeError("Synthetic test gate timed out")
        time.sleep(.02)
private = "SYNTHETIC-PRIVATE-PROGRESS-SENTINEL"
emit({"phase": "preflight", "completed": 1, "total": 3,
      "message": private, "token": private, "raw_response": {"secret": private}})
gate("invalidate")
emit({"phase": "creating", "completed": 9, "total": 2, "message": private})
(gates / "invalid-written").touch()
gate("wait")
emit({"phase": "waiting", "completed": 1, "total": 3, "retry_after": 2, "message": private})
gate("create")
emit({"phase": "creating", "completed": 1, "total": 2, "message": private})
gate("finish")
emit({"phase": "complete", "completed": 2, "total": 2})
result.write_text(json.dumps({"ok": True, "result": {"created": 2, "updated": 0}}))
''')

        def command(request, result, live):
            self.assertFalse(live)
            return [sys.executable, str(worker), str(request), str(result), str(self.base)]

        with patch("liquid_tracer.web.worker_command", side_effect=command):
            job = self.server.start_job([], action="miro-sync", live=False)
            identity = job["id"]
            checked = self.wait_for(identity, lambda value: value.get("progress", {}).get("phase") == "preflight")
            self.assertEqual(checked["status"], "running")
            self.assertEqual((checked["progress"]["completed"], checked["progress"]["total"]), (1, 3))
            # A newly loaded page uses session.active_job and this same route.
            self.assertEqual(self.get("/api/session")["active_job"], identity)
            self.assertEqual(self.get("/api/jobs/" + identity)["progress"], checked["progress"])
            (self.base / "invalidate").touch()
            deadline = time.monotonic() + 3
            while not (self.base / "invalid-written").exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((self.base / "invalid-written").exists())
            time.sleep(.6)  # Allow more than one server progress poll.
            self.assertEqual(self.get("/api/jobs/" + identity)["progress"], checked["progress"])
            (self.base / "wait").touch()
            waiting = self.wait_for(identity, lambda value: value.get("progress", {}).get("phase") == "waiting")
            self.assertEqual(waiting["progress"]["retry_after"], 2)
            (self.base / "create").touch()
            creating = self.wait_for(identity, lambda value: value.get("progress", {}).get("phase") == "creating")
            self.assertEqual(creating["status"], "running")
            self.assertEqual(creating["progress"]["completed"], 1)
            (self.base / "finish").touch()
            completed = self.wait_for(identity, lambda value: value["status"] == "succeeded")
            self.assertEqual(completed["result"], {"created": 2, "updated": 0})
            self.assertEqual(completed["progress"]["phase"], "complete")
            self.assertIsNone(self.get("/api/session")["active_job"])


if __name__ == "__main__":
    unittest.main()
