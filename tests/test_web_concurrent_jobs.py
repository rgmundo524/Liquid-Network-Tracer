"""Independent investigations can run, reconnect, and stop without sharing job ownership."""

import concurrent.futures
import json
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

from liquid_tracer.web import RequestError
from tests import test_web


class ConcurrentWebJobTests(unittest.TestCase):
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    create = test_web.LocalWebTests.create

    def setUp(self):
        test_web.LocalWebTests.setUp(self)
        self.first = self.create()[1]
        self.second = self.create()[1]
        self.worker = self.base / "concurrent-worker.py"
        self.worker.write_text('''import json, os, sys, time
from pathlib import Path
request, result, gates = map(Path, sys.argv[1:])
label, case = json.loads(request.read_text())["arguments"]
(gates / (label + ".pid")).write_text(str(os.getpid()))
progress = request.parent / "progress.json"
def emit(completed):
    pending = progress.with_suffix(".pending")
    pending.write_text(json.dumps({"phase": "preflight", "completed": completed,
                                  "total": 2, "token": "PRIVATE-WORKER-SENTINEL"}))
    os.replace(pending, progress)
emit(0)
advanced = False
deadline = time.monotonic() + 30
while not (gates / (label + ".finish")).exists():
    if time.monotonic() > deadline:
        raise RuntimeError("Synthetic worker gate timed out")
    if not advanced and (gates / (label + ".advance")).exists():
        emit(1)
        advanced = True
    time.sleep(.02)
result.write_text(json.dumps({"ok": True, "result": {
    "name": label, "directory": str(Path(case) / "previews" / ("a" * 16 + "-elk-" + "b" * 8)),
    "token": "PRIVATE-WORKER-SENTINEL", "private_path": str(request)}}))
''')
        helper = patch("liquid_tracer.web.worker_command", side_effect=lambda request, result, live:
                       [sys.executable, str(self.worker), str(request), str(result), str(self.base)])
        helper.start()
        self.addCleanup(helper.stop)

    def start(self, label, case, action="layout"):
        path = self.server.case(case["id"])[0]
        return self.server.start_job([label, str(path)], action=action, case=path)

    def observe(self, job, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.success("/api/jobs/" + job["id"])
            if predicate(value):
                return value
            time.sleep(.02)
        self.fail("Job did not reach the expected state: " + json.dumps(value))

    def started(self, job):
        return self.observe(job, lambda value: value.get("progress", {}).get("completed") == 0)

    def finish(self, label, job):
        (self.base / (label + ".finish")).touch()
        value = self.observe(job, lambda current: current["status"] == "succeeded")
        self.assertEqual(value["result"], {"name": label, "downloads": []})
        return value

    def test_two_investigations_run_simultaneously_and_reconnect_with_separate_progress(self):
        first = self.start("first", self.first)
        self.started(first)
        second = self.start("second", self.second)
        self.started(second)
        # Both workers reached their gates without either job being released.
        self.assertNotEqual((self.base / "first.pid").read_text(), (self.base / "second.pid").read_text())
        (self.base / "second.advance").touch()
        self.observe(second, lambda value: value.get("progress", {}).get("completed") == 1)
        session = self.success("/api/session")
        active = {job["id"]: job for job in session["active_jobs"]}
        self.assertEqual(set(active), {first["id"], second["id"]})
        self.assertEqual(active[first["id"]]["case_id"], self.first["id"])
        self.assertEqual(active[second["id"]]["case_id"], self.second["id"])
        self.assertEqual(active[first["id"]]["progress"]["completed"], 0)
        self.assertEqual(active[second["id"]]["progress"]["completed"], 1)
        listed = self.success("/api/jobs")["jobs"]
        self.assertEqual({job["id"] for job in listed}, set(active))
        self.assertNotIn("PRIVATE-WORKER-SENTINEL", json.dumps([session, listed]))
        self.assertNotIn(str(self.base), json.dumps(listed))
        self.finish("second", second)
        self.assertEqual([job["id"] for job in self.success("/api/session")["active_jobs"]], [first["id"]])
        self.assertEqual(self.success("/api/jobs/" + first["id"])["status"], "running")
        self.finish("first", first)
        self.assertEqual(self.success("/api/session")["active_jobs"], [])
        self.assertEqual({job["status"] for job in self.success("/api/jobs")["jobs"]}, {"succeeded"})

    def test_cancel_targets_only_one_worker_and_late_cancel_does_not_stop_its_replacement(self):
        first, second = self.start("first", self.first), self.start("second", self.second)
        self.started(first)
        self.started(second)
        route = "/api/jobs/" + first["id"] + "/cancel"
        self.assertEqual(self.request(route, {}, headers={"X-Liquid-CSRF": "wrong"})[0], 403)
        self.assertEqual(self.success(route, {}, 202)["status"], "cancelling")
        self.observe(first, lambda value: value["status"] == "canceled")
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.base / "first.pid").read_text()), 0)
        os.kill(int((self.base / "second.pid").read_text()), 0)
        (self.base / "second.advance").touch()
        self.observe(second, lambda value: value.get("progress", {}).get("completed") == 1)
        replacement = self.start("replacement", self.first)
        self.started(replacement)
        self.assertEqual(self.request(route, {})[0], 409)
        self.assertEqual(self.success("/api/jobs/" + replacement["id"])["status"], "running")
        self.finish("replacement", replacement)
        self.finish("second", second)

    def test_running_case_blocks_its_mutations_but_not_other_case_or_workspace_changes(self):
        job = self.start("first", self.first)
        self.started(job)
        first = "/api/cases/" + self.first["id"]
        second = "/api/cases/" + self.second["id"]
        with self.assertRaises(RequestError) as error:
            self.start("duplicate", self.first)
        self.assertEqual(error.exception.status, 409)
        for route, body in ((first + "/actions", {"action": "trace"}),
                            (first + "/settings", {"name": "Unsafe concurrent rename"}),
                            (first + "/plot-settings", {"settings": {"include_fees": True}})):
            self.assertEqual(self.request(route, body)[0], 409)
        self.assertEqual(self.success(first)["name"], self.first["name"])
        self.success(first + "/addresses", {})
        self.success(first + "/change-outputs", {"query": ""})
        self.assertTrue(self.success(second + "/plot-settings", {"settings": {"include_fees": True}})
                        ["run_defaults"]["include_fees"])
        self.assertTrue(self.success("/api/settings", {"settings": {"include_fees": True}})
                        ["settings"]["include_fees"])
        third = self.create()[1]
        self.assertNotIn(third["id"], {self.first["id"], self.second["id"]})
        self.finish("first", job)
        self.success(first + "/plot-settings", {"settings": {"include_fees": True}})

    def test_simultaneous_submissions_reserve_the_case_before_worker_launch(self):
        barrier = threading.Barrier(2)

        def submit(label):
            barrier.wait(timeout=3)
            try:
                return self.start(label, self.first)
            except RequestError as error:
                return error

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, ("race-a", "race-b")))
        accepted = [value for value in results if isinstance(value, dict)]
        rejected = [value for value in results if isinstance(value, RequestError)]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].status, 409)
        self.started(accepted[0])
        for label in ("race-a", "race-b"):
            (self.base / (label + ".finish")).touch()
        self.observe(accepted[0], lambda value: value["status"] == "succeeded")

    def test_shutdown_reaps_all_active_workers(self):
        jobs = [self.start("first", self.first), self.start("second", self.second)]
        for job in jobs:
            self.started(job)
        pids = [int((self.base / (label + ".pid")).read_text()) for label in ("first", "second")]
        self.server.shutdown()
        self.server.server_close()
        for pid in pids:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        for job in jobs:
            self.assertEqual(self.server.jobs[job["id"]]["status"], "failed")
        self.assertFalse(self.server.processes)
        self.assertFalse(any(thread.is_alive() for thread in self.server.job_threads.values()))

    def test_recent_job_history_never_evicts_an_older_active_worker(self):
        first = self.start("first", self.first)
        self.started(first)
        with self.server.job_lock:
            for number in range(129):
                identity = format(number, "032x")
                self.server.jobs[identity] = {**test_web.synthetic_running_job(None),
                    "id": identity, "status": "succeeded", "cancellable": False}
        second = self.start("second", self.second)
        self.started(second)
        listed = self.success("/api/jobs")["jobs"]
        self.assertEqual(len(listed), 130)
        self.assertEqual({job["id"] for job in listed if job["status"] == "running"},
                         {first["id"], second["id"]})
        self.assertNotIn("0" * 32, {job["id"] for job in listed})
        self.finish("first", first)
        self.finish("second", second)


if __name__ == "__main__":
    unittest.main()
