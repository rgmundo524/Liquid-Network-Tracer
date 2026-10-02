"""Plot admission is quick; expensive evidence review belongs to visible workers."""

import sys
import time
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.investigation_boards import generate_and_sync
from liquid_tracer.investigations import read_case
from liquid_tracer.plots import preview_plot
from tests import test_shared_collection_web, test_web
from tests.test_attribution_convergence import graph_state
from tests.test_connections import saved_case


class PlotSubmissionTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    wait = test_web.LocalWebTests.wait
    create = test_web.LocalWebTests.create
    investigations = test_shared_collection_web.SharedCollectionWebTests.investigations
    collect = test_shared_collection_web.SharedCollectionWebTests.collect

    def local(self):
        _, detail = self.create()
        case, _ = self.server.case(detail["id"])
        state, archive = saved_case(case, graph_state((("a:0", "b"),)))
        return case, "/api/cases/" + detail["id"] + "/actions", state["run_id"], archive

    def shared(self):
        case, other, _, _ = self.investigations()
        result = self.collect(case, other)
        return (case, "/api/cases/" + read_case(case)["case_id"] + "/actions",
                result["run_id"], result["shared_collection"]["dataset_id"])

    def test_local_plot_admission_does_not_hash_or_read_the_saved_graph(self):
        case, route, run, _ = self.local()
        with patch.object(self.server, "run_job") as worker, \
                patch("liquid_tracer.cli.verify_export", side_effect=AssertionError("Admission hashed evidence")), \
                patch("liquid_tracer.plots._source", side_effect=AssertionError("Admission loaded graph")), \
                patch("liquid_tracer.investigation_boards.list_boards", side_effect=AssertionError("Fresh job read board mappings")):
            for action in ("plot", "plot-sync"):
                body = {"action": action, "goal": "connections", "run_id": "latest"}
                if action == "plot-sync":
                    body["name"] = "New connection board"
                job = self.success(route, body, 202)
                self.assertEqual(job["status"], "running")
                self.assertEqual(job["source_run_id"], run)
                self.assertEqual(job["case_id"], read_case(case)["case_id"])
                self.assertEqual(job["live"], action == "plot-sync")
                arguments = worker.call_args.args[1]
                self.assertEqual(arguments[arguments.index("--run") + 1], run)
                self.assertIn(job["id"], {value["id"] for value in self.success("/api/jobs")["jobs"]})

    def test_shared_plot_admission_pins_metadata_without_loading_shared_evidence(self):
        _, route, run, dataset = self.shared()
        with patch.object(self.server, "run_job") as worker, \
                patch("liquid_tracer.cli.verify_export", side_effect=AssertionError("Admission hashed evidence")), \
                patch("liquid_tracer.shared_collection.load_shared_run", side_effect=AssertionError("Admission loaded shared run")), \
                patch("liquid_tracer.shared_collection._compatible", side_effect=AssertionError("Admission loaded private run")):
            for action in ("plot", "plot-sync"):
                body = {"action": action, "goal": "connections", "run_id": "latest",
                        "data_source": "shared", "dataset_id": dataset}
                if action == "plot-sync":
                    body["name"] = "Shared connection board"
                job = self.success(route, body, 202)
                arguments = worker.call_args.args[1]
                self.assertEqual(arguments[arguments.index("--run") + 1], run)
                self.assertEqual(arguments[arguments.index("--dataset-id") + 1], dataset)
                self.assertEqual(job["source_run_id"], run)

    def test_invalid_run_and_dataset_ids_are_rejected_before_admission(self):
        _, local_route, _, _ = self.local()
        _, shared_route, shared_run, dataset = self.shared()
        with patch.object(self.server, "run_job") as worker:
            for route, extra in ((local_route, {}), (shared_route, {"data_source": "shared", "dataset_id": dataset})):
                for run in ("../trace.json", "/tmp/run", "a" * 17, "missing", "f" * 16, None, True):
                    with self.subTest(route=route, run=run):
                        self.assertEqual(self.request(route, {"action": "plot", "goal": "connections",
                                                             "run_id": run, **extra})[0], 400)
            for invalid in ("f" * 32, "../dataset", "", None, True):
                self.assertEqual(self.request(shared_route, {"action": "plot", "goal": "connections",
                    "run_id": shared_run, "data_source": "shared", "dataset_id": invalid})[0], 400)
            worker.assert_not_called()

    def test_minimal_archive_checks_reject_missing_and_symlinked_inputs(self):
        _, local_route, local_run, local_archive = self.local()
        _, shared_route, shared_run, dataset = self.shared()
        shared_archive = self.server.root / ".shared-collection" / "runs" / shared_run
        with patch.object(self.server, "run_job") as worker:
            for route, run, archive, extra in (
                    (local_route, local_run, local_archive, {}),
                    (shared_route, shared_run, shared_archive, {"data_source": "shared", "dataset_id": dataset})):
                for name in ("trace.json", "SHA256SUMS"):
                    original, saved = archive / name, archive / (name + ".saved")
                    original.rename(saved)
                    body = {"action": "plot", "goal": "connections", "run_id": run, **extra}
                    try:
                        self.assertIn(self.request(route, body)[0], (400, 404))
                        original.symlink_to(saved)
                        self.assertIn(self.request(route, body)[0], (400, 404))
                    finally:
                        if original.is_symlink():
                            original.unlink()
                        saved.rename(original)
            worker.assert_not_called()

    def test_client_request_id_is_echoed_on_the_job_and_never_added_to_worker_arguments(self):
        _, route, _, _ = self.local()
        correlation = "plot-Request_123"
        with patch.object(self.server, "run_job") as worker:
            job = self.success(route, {"action": "plot", "goal": "connections", "run_id": "latest",
                                       "client_request_id": correlation}, 202)
            self.assertEqual(job["client_request_id"], correlation)
            self.assertEqual(self.success("/api/jobs/" + job["id"])["client_request_id"], correlation)
            self.assertNotIn(correlation, worker.call_args.args[1])
            worker.reset_mock()
            for invalid in ("", "a" * 65, "two words", "../id", "<script>", None, True, []):
                self.assertEqual(self.request(route, {"action": "plot", "goal": "connections", "run_id": "latest",
                    "client_request_id": invalid})[0], 400)
            self.assertEqual(self.request(route, {"action": "plot", "goal": "connections", "run_id": "latest",
                "client_request_id": "valid"}, headers={"X-Liquid-CSRF": "wrong"})[0], 403)
            worker.assert_not_called()

    def test_slow_evidence_review_is_visible_and_does_not_block_polling_or_cancellation(self):
        _, route, _, archive = self.local()
        before = (archive / "trace.json").read_bytes()
        gate = self.base / "verification-started"
        worker_script = self.base / "slow-review.py"
        worker_script.write_text('''
import sys, time
from pathlib import Path
from liquid_tracer import cli, web_worker
def slow_review(directory):
    Path(sys.argv[3]).touch()
    while True:
        time.sleep(.05)
cli.verify_export = slow_review
raise SystemExit(web_worker.main(sys.argv[1:3]))
''')
        with patch("liquid_tracer.web.worker_command", side_effect=lambda request, result, live:
                   [sys.executable, str(worker_script), str(request), str(result), str(gate)]):
            job = self.success(route, {"action": "plot", "goal": "connections", "run_id": "latest",
                                       "client_request_id": "slow-review"}, 202)
            deadline = time.monotonic() + 8
            while not gate.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(gate.exists(), "Worker did not reach evidence review")
            while time.monotonic() < deadline:
                current = self.success("/api/jobs/" + job["id"])
                if current.get("progress", {}).get("phase") == "loading_collection":
                    break
                time.sleep(.02)
            self.assertEqual(current["progress"]["phase"], "loading_collection")
            self.assertEqual(current["status"], "running")
            self.assertEqual(current["client_request_id"], "slow-review")
            self.assertTrue(self.success("/api/jobs")["jobs"])
            self.success("/api/jobs/" + job["id"] + "/cancel", {}, 202)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                current = self.success("/api/jobs/" + job["id"])
                if current["status"] == "canceled":
                    break
                time.sleep(.02)
            self.assertEqual(current["status"], "canceled")
            self.assertEqual((archive / "trace.json").read_bytes(), before)

    def test_corrupted_evidence_is_rejected_by_worker_before_any_miro_request(self):
        local_case, _, local_run, local_archive = self.local()
        shared_case, _, shared_run, dataset = self.shared()
        shared_archive = self.server.root / ".shared-collection" / "runs" / shared_run
        for case, run, archive, extra in (
                (local_case, local_run, local_archive, {}),
                (shared_case, shared_run, shared_archive, {"data_source": "shared", "dataset_id": dataset})):
            with self.subTest(source=extra.get("data_source", "investigation")):
                trace = archive / "trace.json"
                trace.write_bytes(trace.read_bytes() + b"\n")
                events = []
                with patch("liquid_tracer.elk_layout.optimize_graph") as layout, \
                        patch("liquid_tracer.investigation_boards.create_and_sync") as publish, \
                        patch("liquid_tracer.api.Esplora.get") as fetch:
                    transport = unittest.mock.Mock(side_effect=AssertionError("Miro request before verification"))
                    with self.assertRaisesRegex(TraceError, "checksum"):
                        generate_and_sync(case, "connections", run, token="synthetic-token",
                                          transport=transport, progress=events.append, **extra)
                    self.assertIn("loading_collection", [event["phase"] for event in events])
                    layout.assert_not_called()
                    publish.assert_not_called()
                    fetch.assert_not_called()
                    transport.assert_not_called()

    def test_worker_reports_source_loading_before_graph_preparation_and_layout(self):
        case, _, run, _ = self.local()
        phases = []

        def layout(graph, **kwargs):
            phases.append("layout-call")
            return graph

        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=layout):
            preview_plot(case, "connections", run, progress=lambda event: phases.append(event["phase"]))
        self.assertLess(phases.index("loading_collection"), phases.index("preparing_plot"))
        self.assertLess(phases.index("preparing_plot"), phases.index("layout-call"))


if __name__ == "__main__":
    unittest.main()
