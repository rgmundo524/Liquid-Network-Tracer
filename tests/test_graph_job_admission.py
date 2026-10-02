"""Graph validation runs in registered workers, without blocking HTTP job control."""

import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

from liquid_tracer.common import read_json, save_json
from liquid_tracer.investigation_boards import link_board
from liquid_tracer.investigations import create_investigation, read_case
from liquid_tracer.shared_collection import collect_prepared, dataset_path, prepare_collection
from tests import test_web
from tests.fixtures import A, B, fixture
from tests.test_attribution_convergence import graph_state
from tests.test_connections import saved_case


WORKER = r'''
import json, os, socket, sys, time
from pathlib import Path
from liquid_tracer import cli, elk_layout, investigation_boards, plots, web_worker

request, result, gate = map(Path, sys.argv[1:])
payload = json.loads(request.read_text())
(gate / "launch.json").write_text(json.dumps({"pid": os.getpid(), **payload}))
os.environ["MIRO_ACCESS_TOKEN"] = "PRIVATE-SYNTHETIC-TOKEN"

def forbidden(*args, **kwargs):
    (gate / "forbidden-side-effect").touch()
    raise AssertionError("The validation test must not contact APIs or publish")

socket.create_connection = forbidden
investigation_boards.create_and_sync = forbidden
investigation_boards.sync_board = forbidden
elk_layout.optimize_graph = lambda graph, **kwargs: graph
original_graph = plots._graph
def graph(*args, **kwargs):
    (gate / "graph-started").touch()
    return original_graph(*args, **kwargs)
plots._graph = graph

original_verify = cli.verify_export
entered = False
def verify(directory, progress=None):
    global entered
    if not entered:
        entered = True
        if progress is None:
            raise AssertionError("Validation must receive the worker's progress callback")
        progress({"phase": "verifying_files", "completed": 0, "total": 10,
                  "token": "PRIVATE-SYNTHETIC-TOKEN", "path": str(directory)})
        (gate / "entered.json").write_text(json.dumps({"directory": str(directory)}))
        deadline = time.monotonic() + 20
        while not (gate / "release").exists():
            if time.monotonic() >= deadline:
                raise AssertionError("Synthetic archive validation gate timed out")
            time.sleep(.02)
    return original_verify(directory, progress=progress)
cli.verify_export = verify
raise SystemExit(web_worker.main([str(request), str(result)]))
'''


class GraphJobAdmissionTests(unittest.TestCase):
    close_server = test_web.LocalWebTests.close_server

    def setUp(self):
        test_web.LocalWebTests.setUp(self)
        self.worker = self.base / "validation-worker.py"
        self.worker.write_text(WORKER)
        self.launches = []
        command = patch("liquid_tracer.web.worker_command", side_effect=self.worker_command)
        command.start()
        self.addCleanup(command.stop)

    def worker_command(self, request, result, live):
        gate = self.base / ("validation-" + str(len(self.launches)))
        gate.mkdir()
        self.launches.append(gate)
        return [sys.executable, str(self.worker), str(request), str(result), str(gate)]

    def request(self, path, body=None):
        # A lock-held validation regression fails quickly rather than waiting
        # for the synthetic worker's much longer gate deadline.
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        headers = ({"Origin": self.server.origin, "X-Liquid-CSRF": self.server.csrf,
                    "Content-Type": "application/json"} if body is not None else {})
        try:
            connection.request("POST" if body is not None else "GET", path,
                               body=json.dumps(body) if body is not None else None, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def success(self, path, body=None, status=200):
        code, value = self.request(path, body)
        self.assertEqual(code, status, value)
        return value

    def collected(self, name="Synthetic graph"):
        case = create_investigation(self.server.root, name, seeds=["a" * 64 + ":0", "b" * 64 + ":0"])
        state, archive = saved_case(case, graph_state((("a:0", "b"),)))
        return case, archive, state["run_id"]

    def route(self, case):
        return "/api/cases/" + read_case(case)["case_id"] + "/actions"

    @staticmethod
    def body(**changes):
        return {"action": "plot", "goal": "connections", "run_id": "latest", **changes}

    def submit(self, case, body=None):
        # Separate processes import unpatched functions. The request handler
        # itself must never load or hash an evidence archive before registration.
        with patch("liquid_tracer.cli.verify_export", side_effect=AssertionError("HTTP handler hashed evidence")), \
                patch("liquid_tracer.shared_collection.load_shared_run",
                      side_effect=AssertionError("HTTP handler loaded shared evidence")), \
                patch("liquid_tracer.shared_collection._source",
                      side_effect=AssertionError("HTTP handler inspected shared source evidence")):
            return self.success(self.route(case), self.body() if body is None else body, 202)

    def observe(self, job, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        value = None
        while time.monotonic() < deadline:
            value = self.success("/api/jobs/" + job["id"])
            if predicate(value):
                return value
            time.sleep(.02)
        self.fail("Job did not reach expected state: " + json.dumps(value))

    def gated(self, job, index):
        current = self.observe(job, lambda value:
                               value.get("progress", {}).get("phase") == "verifying_files"
                               and len(self.launches) > index
                               and (self.launches[index] / "entered.json").is_file())
        self.assertEqual(current["status"], "running")
        self.assertGreater(len(self.launches), index)
        gate = self.launches[index]
        self.assertTrue((gate / "entered.json").is_file())
        self.assertFalse((gate / "release").exists())
        self.assertFalse((gate / "graph-started").exists())
        return gate

    def finished(self, job, status):
        value = self.observe(job, lambda current: current["status"] not in ("running", "cancelling"))
        self.assertEqual(value["status"], status, value)
        return value

    def assert_no_publication(self, gate, case):
        self.assertFalse((gate / "forbidden-side-effect").exists())
        self.assertFalse((gate / "graph-started").exists())
        self.assertFalse(list((case / "previews").glob("*")))

    def cancel(self, job, gate):
        self.success("/api/jobs/" + job["id"] + "/cancel", {}, 202)
        self.finished(job, "canceled")
        with self.assertRaises(ProcessLookupError):
            os.kill(read_json(gate / "launch.json")["pid"], 0)

    def test_registered_validation_allows_polling_and_unrelated_completion_and_pins_latest(self):
        first, archive, run = self.collected("First")
        second, _, _ = self.collected("Second")
        job = self.submit(first)
        gate = self.gated(job, 0)
        self.assertEqual(job["source_run_id"], run)
        self.assertFalse(job["live"])
        self.assertTrue(job["cancellable"])
        arguments = read_json(gate / "launch.json")["arguments"]
        self.assertEqual(arguments[arguments.index("--run") + 1], run)
        self.assertEqual(Path(read_json(gate / "entered.json")["directory"]), archive)
        listed = self.success("/api/jobs")["jobs"]
        self.assertEqual([value["id"] for value in listed], [job["id"]])
        self.assertNotIn("PRIVATE-SYNTHETIC", json.dumps(listed))
        self.assertNotIn(str(self.base), json.dumps(listed))
        # A subsequent collector publication cannot switch the submitted input.
        save_json(first / "case.json", {**read_case(first), "latest_run": "f" * 16})
        other = self.submit(second)
        other_gate = self.gated(other, 1)
        other_gate.joinpath("release").touch()
        self.finished(other, "succeeded")
        self.assertEqual(self.success("/api/jobs/" + job["id"])["status"], "running")
        gate.joinpath("release").touch()
        self.assertEqual(self.finished(job, "succeeded")["result"]["run_id"], run)
        self.assertEqual(read_case(first)["latest_run"], "f" * 16)

    def test_cancel_during_validation_reaps_worker_without_generating_or_publishing(self):
        case, _, _ = self.collected()
        before = (case / "case.json").read_bytes()
        job = self.submit(case)
        gate = self.gated(job, 0)
        self.cancel(job, gate)
        self.assert_no_publication(gate, case)
        self.assertEqual((case / "case.json").read_bytes(), before)
        replacement = self.submit(case)
        new_gate = self.gated(replacement, 1)
        new_gate.joinpath("release").touch()
        self.finished(replacement, "succeeded")

    def test_new_board_reservation_is_atomic_during_validation_and_released_after_failure(self):
        case, archive, _ = self.collected()
        other_case, other_archive, _ = self.collected("Independent new board")
        body = self.body(action="plot-sync", name="Synthetic new board")
        with patch("liquid_tracer.investigation_boards.list_boards",
                   side_effect=AssertionError("Fresh graph admission loaded board mappings")):
            job = self.submit(case, body)
        gate = self.gated(job, 0)
        self.assertTrue(job["live"])
        self.assertFalse(job["cancellable"])
        self.assertEqual((job["resource_kind"], job["resource_key"]), ("board", "new-board"))
        self.assertEqual(self.request(self.route(case), body)[0], 409)
        self.assertEqual(self.request("/api/jobs/" + job["id"] + "/cancel", {})[0], 409)
        # "new-board" is scoped to one case, unlike a selected physical board.
        other_job = self.submit(other_case, body)
        other_gate = self.gated(other_job, 1)
        # Corruption is rejected by the registered worker, before graph work.
        for source in (archive, other_archive):
            with (source / "trace.json").open("ab") as stream:
                stream.write(b" ")
        other_gate.joinpath("release").touch()
        self.finished(other_job, "failed")
        self.assert_no_publication(other_gate, other_case)
        self.assertEqual(self.success("/api/jobs/" + job["id"])["status"], "running")
        gate.joinpath("release").touch()
        self.finished(job, "failed")
        self.assert_no_publication(gate, case)
        replacement = self.submit(case, body)
        new_gate = self.gated(replacement, 2)
        new_gate.joinpath("release").touch()
        self.finished(replacement, "failed")
        self.assert_no_publication(new_gate, case)

    def test_selected_board_conflict_spans_cases_and_cancel_releases_it(self):
        first, _, _ = self.collected("First")
        second, _, _ = self.collected("Second")
        first_board = link_board(first, "connections", "Shared board", "SYNTHETIC_SHARED=")
        second_board = link_board(second, "connections", "Shared board", "SYNTHETIC_SHARED=")
        body = self.body(layout_mode="update", board_record_id=first_board["id"])
        job = self.submit(first, body)
        gate = self.gated(job, 0)
        self.assertTrue(job["live"])
        self.assertTrue(job["cancellable"])
        self.assertEqual(job["resource_key"], "SYNTHETIC_SHARED=")
        other_body = {**body, "board_record_id": second_board["id"]}
        self.assertEqual(self.request(self.route(second), other_body)[0], 409)
        self.cancel(job, gate)
        self.assert_no_publication(gate, first)
        replacement = self.submit(second, other_body)
        replacement_gate = self.gated(replacement, 1)
        self.cancel(replacement, replacement_gate)
        self.assert_no_publication(replacement_gate, second)

    def test_malformed_request_is_rejected_without_registering_a_worker(self):
        case, _, _ = self.collected()
        invalid = [{"run_id": "../invalid"}, {"run_id": "G" * 16}, {"run_id": None},
                   {"goal": "invalid"}, {"max_hops": -1}, {"layout_mode": "invalid"},
                   {"board_record_id": "forbidden-in-fresh"}, {"layout_settings": {}},
                   {"data_source": "shared", "dataset_id": "invalid"}]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.assertEqual(self.request(self.route(case), self.body(**changes))[0], 400)
        self.assertEqual(self.success("/api/jobs")["jobs"], [])
        self.assertEqual(self.launches, [])

    def test_corrupt_private_archive_is_a_worker_failure_without_side_effects(self):
        case, archive, _ = self.collected()
        original = (archive / "trace.json").read_bytes()
        (archive / "trace.json").write_bytes(original + b" ")
        job = self.submit(case)
        gate = self.gated(job, 0)
        gate.joinpath("release").touch()
        failure = self.finished(job, "failed")
        self.assert_no_publication(gate, case)
        self.assertNotIn(str(archive), json.dumps(failure))
        self.assertNotIn("PRIVATE-SYNTHETIC", json.dumps(failure))
        (archive / "trace.json").write_bytes(original)
        replacement = self.submit(case)
        replacement_gate = self.gated(replacement, 1)
        replacement_gate.joinpath("release").touch()
        self.finished(replacement, "succeeded")

    def shared_collection(self):
        fixture_path = self.base / "shared-fixture.json"
        save_json(fixture_path, fixture())
        first = create_investigation(self.server.root, "Shared first", seeds=[A + ":0"], fixture=fixture_path)
        second = create_investigation(self.server.root, "Shared second", seeds=[B + ":1"], fixture=fixture_path)
        prepared = prepare_collection(first, [read_case(case)["case_id"] for case in (first, second)], hops=1)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(collect_prepared(first, prepared["request_id"]), 0)
        source = json.loads(output.getvalue())
        return first, source

    def test_shared_archive_is_pinned_and_verified_only_after_registration(self):
        first, source = self.shared_collection()
        before = (first / "case.json").read_bytes()
        body = self.body(goal="full", min_hops=0, max_hops=1,
                         data_source="shared", dataset_id=source["dataset_id"])
        job = self.submit(first, body)
        gate = self.gated(job, 0)
        self.assertEqual(job["source_run_id"], source["run_id"])
        shared = dataset_path(first)
        self.assertEqual(Path(read_json(gate / "entered.json")["directory"]), shared / "runs" / source["run_id"])
        save_json(shared / "case.json", {**read_case(shared), "latest_run": "f" * 16})
        gate.joinpath("release").touch()
        result = self.finished(job, "succeeded")["result"]
        self.assertEqual(result["collection_source"]["run_id"], source["run_id"])
        self.assertEqual((first / "case.json").read_bytes(), before)
        self.assertFalse((gate / "forbidden-side-effect").exists())

    def test_corrupt_shared_archive_fails_in_worker_without_creating_a_projection(self):
        first, source = self.shared_collection()
        archive = dataset_path(first) / "runs" / source["run_id"]
        with (archive / "trace.json").open("ab") as stream:
            stream.write(b" ")
        body = self.body(goal="full", min_hops=0, max_hops=1,
                         data_source="shared", dataset_id=source["dataset_id"])
        job = self.submit(first, body)
        gate = self.gated(job, 0)
        gate.joinpath("release").touch()
        self.finished(job, "failed")
        self.assert_no_publication(gate, first)
        self.assertFalse(list((first / "runs").glob("*")))


if __name__ == "__main__":
    unittest.main()
