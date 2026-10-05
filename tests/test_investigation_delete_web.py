"""Confirmed local investigation deletion owns a visible, case-exclusive task."""
import contextlib
import io
import json
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from liquid_tracer.cli import main, parser
from liquid_tracer.job_resources import conflicts, job_resources
from liquid_tracer.web import RequestError
from tests import test_web


class InvestigationDeleteAdmissionTests(unittest.TestCase):
    def test_admission_does_not_scan_case_evidence_or_request_remote_access(self):
        with patch("liquid_tracer.investigations.read_case",
                   side_effect=AssertionError("Admission must use the pinned case identity")), \
                patch("liquid_tracer.investigation_boards.list_boards",
                      side_effect=AssertionError("Local deletion never inspects remote boards")), \
                patch("liquid_tracer.common.read_json",
                      side_effect=AssertionError("Evidence verification belongs in the worker")):
            resource = job_resources(["investigation-delete", "--investigations-dir", "/synthetic/cases",
                                      "--case", "/synthetic/cases/case", "--case-id", "a" * 32,
                                      "--confirm-name=Synthetic case"],
                                     "investigation-delete", "/synthetic/cases/case")
        self.assertEqual(resource["resource_kind"], "investigation_delete")

    def test_deletion_conflicts_with_every_resource_in_its_case_in_both_directions(self):
        deletion = {"case_id": "a", "resource_kind": "investigation_delete"}
        for kind in ("exclusive", "collection", "shared_collection", "plot", "board", "board_delete",
                     "investigation_delete"):
            with self.subTest(kind=kind):
                active = {"case_id": "a", "resource_kind": kind, "resource_key": "unrelated"}
                self.assertTrue(conflicts(deletion, active))
                self.assertTrue(conflicts(active, deletion))

    def test_deletion_blocks_shared_membership_work_but_not_independent_cases(self):
        deletion = {"case_id": "a", "resource_kind": "investigation_delete"}
        for kind in ("exclusive", "collection", "shared_collection", "plot", "board", "board_delete",
                     "investigation_delete"):
            with self.subTest(kind=kind):
                active = {"case_id": "b", "resource_kind": kind, "resource_key": "unrelated"}
                self.assertEqual(conflicts(deletion, active), kind == "shared_collection")
                self.assertEqual(conflicts(active, deletion), kind == "shared_collection")


class InvestigationDeleteWebTests(unittest.TestCase):
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    create = test_web.LocalWebTests.create
    wait = test_web.LocalWebTests.wait

    def setUp(self):
        test_web.LocalWebTests.setUp(self)
        _, self.metadata = self.create()
        self.case, _ = self.server.case(self.metadata["id"])
        self.route = "/api/cases/" + self.metadata["id"]
        self.body = {"action": "investigation-delete", "confirm_name": self.metadata["name"]}

    def test_requires_exact_name_and_only_the_supported_fields_before_creating_a_job(self):
        bodies = [dict(self.body, confirm_name=value)
                  for value in (None, True, 1, "", "wrong name", self.metadata["name"].lower(),
                                self.metadata["name"] + " ")]
        bodies += [{"action": "investigation-delete"},
                   dict(self.body, case_id="b" * 32),
                   dict(self.body, path="/unrelated/case"),
                   dict(self.body, token="never-accept-browser-tokens")]
        with patch.object(self.server, "start_job") as start:
            for body in bodies:
                with self.subTest(body=body):
                    self.assertEqual(self.request(self.route + "/actions", body)[0], 400)
            start.assert_not_called()
        self.assertTrue(self.case.is_dir())
        self.assertEqual(self.success("/api/jobs")["jobs"], [])

    def test_submission_pins_root_case_identity_and_literal_confirmation_without_credentials(self):
        # A display name can resemble an option; it remains one literal CLI value.
        name = "--board"
        self.success(self.route + "/settings", {"name": name})
        with patch.object(self.server, "start_job", return_value={"id": "synthetic"}) as start:
            self.success(self.route + "/actions", dict(self.body, confirm_name=name), 202)
        arguments = start.call_args.args[0]
        parsed = parser().parse_args(arguments)
        self.assertEqual(parsed.command, "investigation-delete")
        self.assertEqual(parsed.investigations_dir, self.server.root)
        self.assertEqual(parsed.case, self.case)
        self.assertEqual(parsed.case_id, self.metadata["id"])
        self.assertEqual(parsed.confirm_name, name)
        self.assertEqual(start.call_args.kwargs,
                         {"action": "investigation-delete", "live": False, "case": self.case})

    def test_cli_requires_target_identity_and_confirmation_and_dispatches_only_local_deletion(self):
        command = ["investigation-delete", "--investigations-dir", str(self.server.root),
                   "--case", str(self.case), "--case-id", self.metadata["id"]]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(command)
        with patch("liquid_tracer.investigation_deletion.delete_investigation",
                   return_value={"deleted": True}) as delete, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(command + ["--confirm-name=" + self.metadata["name"]]), 0)
        self.assertEqual(delete.call_args.args, (self.server.root, self.case))
        self.assertEqual(delete.call_args.kwargs["case_id"], self.metadata["id"])
        self.assertEqual(delete.call_args.kwargs["confirm_name"], self.metadata["name"])
        self.assertIsNotNone(delete.call_args.kwargs["progress"])

    def test_slow_worker_is_visible_without_blocking_jobs_and_reserves_conflicting_work(self):
        _, other = self.create()
        other_route = "/api/cases/" + other["id"]
        gate = self.base / "release"
        entered = self.base / "entered"
        cleanup_gate = self.base / "cleanup-release"
        cleanup_entered = self.base / "cleanup-entered"
        worker = self.base / "slow-delete-worker.py"
        worker.write_text('''import json, sys, time
from pathlib import Path
from liquid_tracer import investigation_deletion, web_worker
original = investigation_deletion.delete_investigation
cleanup = investigation_deletion._cleanup
gate, entered, cleanup_gate, cleanup_entered = map(Path, sys.argv[3:])
def pause(gate, entered):
    entered.touch()
    deadline = time.monotonic() + 20
    while not gate.exists():
        if time.monotonic() > deadline:
            raise RuntimeError("Test deletion gate timed out")
        time.sleep(.02)
def slow_delete(*args, **kwargs):
    pause(gate, entered)
    return original(*args, **kwargs)
def slow_cleanup(*args, **kwargs):
    pause(cleanup_gate, cleanup_entered)
    return cleanup(*args, **kwargs)
investigation_deletion.delete_investigation = slow_delete
investigation_deletion._cleanup = slow_cleanup
payload = json.loads(Path(sys.argv[1]).read_text())
assert "terminal_handoff" not in payload
raise SystemExit(web_worker.main(sys.argv[1:3]))
''')
        self.addCleanup(gate.touch)
        self.addCleanup(cleanup_gate.touch)

        def command(request, result, live):
            self.assertFalse(live, "Local investigation deletion must not request credentials")
            return [sys.executable, str(worker), str(request), str(result), str(gate), str(entered),
                    str(cleanup_gate), str(cleanup_entered)]

        with patch("liquid_tracer.web.worker_command", side_effect=command):
            before = time.monotonic()
            job = self.success(self.route + "/actions", self.body, 202)
            self.assertLess(time.monotonic() - before, 3)
            self.assertEqual(job["resource_kind"], "investigation_delete")
            self.assertEqual(job["case_id"], self.metadata["id"])
            self.assertFalse(job["live"])
            self.assertFalse(job["cancellable"])
            deadline = time.monotonic() + 8
            while not entered.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(entered.exists(), self.success("/api/jobs/" + job["id"]))
            before = time.monotonic()
            jobs = self.success("/api/jobs")["jobs"]
            self.assertLess(time.monotonic() - before, 3)
            self.assertEqual(next(item for item in jobs if item["id"] == job["id"])["status"], "running")
            self.assertEqual(self.request(self.route + "/actions", self.body)[0], 409)
            self.assertEqual(self.request(self.route + "/actions", {"action": "trace"})[0], 409)
            self.assertEqual(self.request(self.route + "/settings", {"name": "Unsafe rename"})[0], 409)
            shared = {"action": "shared-trace", "mode": "collect", "hops": 1,
                      "case_ids": [self.metadata["id"], other["id"]]}
            self.assertEqual(self.request(self.route + "/actions", shared)[0], 409)
            self.assertEqual(self.request(other_route + "/actions", shared)[0], 409)
            self.assertEqual(self.request("/api/jobs/" + job["id"] + "/cancel", {})[0], 409)
            self.success(other_route + "/settings", {"name": "Independent investigation"})
            self.assertTrue(self.case.is_dir())
            gate.touch()
            deadline = time.monotonic() + 8
            while not cleanup_entered.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(cleanup_entered.exists(), self.success("/api/jobs/" + job["id"]))
            # The atomic move has hidden the case; slow physical cleanup must
            # still leave the task visible, listable, and holding its reservation.
            self.assertFalse(self.case.exists())
            before = time.monotonic()
            jobs = self.success("/api/jobs")["jobs"]
            self.assertLess(time.monotonic() - before, 3)
            self.assertEqual(next(item for item in jobs if item["id"] == job["id"])["status"], "running")
            self.assertEqual(self.request(other_route + "/actions", shared)[0], 409)
            self.assertEqual([case["id"] for case in self.success("/api/session")["cases"]], [other["id"]])
            cleanup_gate.touch()
            result = self.wait(job)
        self.assertEqual(result, {"case_id": self.metadata["id"], "name": self.metadata["name"],
                                  "deleted": True, "cleanup_pending": False})
        self.assertFalse(self.case.exists())
        self.assertEqual(self.request(self.route)[0], 404)
        self.assertEqual([case["id"] for case in self.success("/api/session")["cases"]], [other["id"]])
        self.assertEqual(self.success("/api/jobs/" + job["id"])["result"], result)
        self.assertNotIn(str(self.base), json.dumps(self.success("/api/jobs")))

    def test_separate_cli_plots_share_lifetime_guard_and_block_deletion_until_both_finish(self):
        # No graph computation is needed: pause at the actual CLI dispatch
        # boundary after its lifetime guard has been acquired.
        script = """import sys, time
from pathlib import Path
from liquid_tracer import cli, plots
case, entered, release = map(Path, sys.argv[1:])
def plot(*args, **kwargs):
    entered.touch()
    deadline = time.monotonic() + 20
    while not release.exists():
        if time.monotonic() > deadline:
            raise RuntimeError("Test plot gate timed out")
        time.sleep(.02)
    return {"preview_id": "synthetic"}
plots.preview_plot = plot
raise SystemExit(cli.main(["plot", "--case", str(case), "--goal", "full"]))
"""
        gates = [(self.base / f"plot-{number}.entered", self.base / f"plot-{number}.release")
                 for number in range(2)]
        workers = []
        original_metadata = (self.case / "case.json").read_bytes()
        evidence = self.case / "preserved-evidence.txt"
        evidence.write_text("Keep this evidence while any operation is active")
        delete = [sys.executable, "-m", "liquid_tracer", "investigation-delete",
                  "--investigations-dir", str(self.server.root), "--case", str(self.case),
                  "--case-id", self.metadata["id"], "--confirm-name=" + self.metadata["name"]]
        try:
            for entered, release in gates:
                workers.append(subprocess.Popen([sys.executable, "-c", script, str(self.case),
                    str(entered), str(release)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            deadline = time.monotonic() + 8
            while not all(entered.exists() for entered, _ in gates) and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(all(entered.exists() for entered, _ in gates),
                            "Independent CLI plots must hold shared lifetime locks concurrently")
            for remaining in (2, 1):
                rejected = subprocess.run(delete, capture_output=True, text=True, timeout=5)
                self.assertNotEqual(rejected.returncode, 0, rejected.stdout)
                self.assertEqual((self.case / "case.json").read_bytes(), original_metadata)
                self.assertTrue(evidence.is_file())
                if remaining == 2:
                    gates[0][1].touch()
                    stdout, stderr = workers[0].communicate(timeout=5)
                    self.assertEqual(workers[0].returncode, 0, stderr or stdout)
            gates[1][1].touch()
            stdout, stderr = workers[1].communicate(timeout=5)
            self.assertEqual(workers[1].returncode, 0, stderr or stdout)
            completed = subprocess.run(delete, capture_output=True, text=True, timeout=5)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(json.loads(completed.stdout)["deleted"])
            self.assertFalse(self.case.exists())
        finally:
            for _, release in gates:
                release.touch()
            for worker in workers:
                if worker.poll() is None:
                    worker.terminate()
                worker.communicate(timeout=5)

    def test_public_completion_after_removal_does_not_read_the_deleted_case_or_expose_paths(self):
        value = {"case_id": self.metadata["id"], "name": self.metadata["name"], "deleted": True,
                 "cleanup_pending": True, "path": str(self.case), "token": "PRIVATE-SENTINEL"}
        with patch("liquid_tracer.web.read_case", side_effect=AssertionError("Case has been removed")), \
                patch.object(self.server, "case", side_effect=AssertionError("Case has been removed")):
            public = self.server.public_result(value, "investigation-delete", self.case, None)
        self.assertEqual(public, {key: value[key] for key in
                                  ("case_id", "name", "deleted", "cleanup_pending")})
        self.assertNotIn("PRIVATE-SENTINEL", json.dumps(public))


    def test_public_completion_rejects_malformed_success_claims(self):
        value = {"case_id": self.metadata["id"], "name": self.metadata["name"], "deleted": True,
                 "cleanup_pending": False}
        replacements = [("case_id", "../other"), ("name", ""), ("name", "case\nname"),
                        ("deleted", 1), ("deleted", False), ("cleanup_pending", None),
                        ("cleanup_pending", "false"), ("cleanup_pending", 0)]
        for key, invalid in replacements:
            with self.subTest(key=key, value=invalid), self.assertRaises(RequestError):
                self.server.public_result({**value, key: invalid}, "investigation-delete", self.case, None)


if __name__ == "__main__":
    unittest.main()
