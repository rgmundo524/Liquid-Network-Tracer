"""Staged investigation views never perform whole-investigation audits."""
import http.client
import json
import threading
import unittest
from unittest.mock import patch

from liquid_tracer.common import read_json, save_json
from liquid_tracer.investigations import read_case
from liquid_tracer.web import LocalServer
from tests import test_web


class InvestigationViewTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    create = test_web.LocalWebTests.create

    def collected(self):
        _, detail = self.create()
        case, metadata = self.server.case(detail["id"])
        run = "a" * 16
        metadata["latest_run"] = run
        save_json(case / "case.json", metadata)
        directory = case / "runs" / run
        directory.mkdir(parents=True)
        (directory / "SHA256SUMS").write_text("synthetic")
        # Even a multi-GiB sparse trace must not be read to open this page.
        with (directory / "trace.json").open("wb") as stream:
            stream.truncate(3 * 1024 ** 3)
        return case, metadata, "/api/cases/" + detail["id"]

    def test_overview_and_session_do_not_load_histories_graphs_or_trace_json(self):
        case, metadata, route = self.collected()
        with patch("liquid_tracer.run_summaries.get_run_summary", return_value=(None, "loading")) as summary, \
             patch.object(self.server, "case_summary", side_effect=AssertionError("full detail")), \
             patch.object(self.server, "saved_artifacts", side_effect=AssertionError("artifacts")), \
             patch.object(self.server, "pegout_searches", side_effect=AssertionError("searches")), \
             patch.object(self.server, "shared_collection_summary", side_effect=AssertionError("shared")), \
             patch("liquid_tracer.workflow_api.case_workflow", side_effect=AssertionError("plots")):
            overview = self.success(route + "/overview")
            self.assertEqual(overview["id"], metadata["case_id"])
            self.assertEqual(overview["seeds"], metadata["seeds"])
            self.assertEqual(overview["runs"][0]["id"], metadata["latest_run"])
            self.assertTrue(overview["runs"][0]["summary_pending"])
            self.assertEqual(set(overview["sections"].values()), {"unloaded"})
            self.assertNotIn("plots", overview)
            self.assertNotIn("artifacts", overview)
            self.assertNotIn("shared_collection", overview)
            summary.assert_called_once_with(case / "runs" / metadata["latest_run"], metadata["case_id"], schedule=True, priority=True)
            summary.reset_mock()
            session = self.success("/api/session")
            self.assertEqual(session["cases"][0]["id"], metadata["case_id"])
            summary.assert_called_once_with(case / "runs" / metadata["latest_run"], metadata["case_id"], schedule=False, priority=True)

    def test_settings_responses_do_not_run_full_detail_under_job_lock(self):
        _, detail = self.create()
        route = "/api/cases/" + detail["id"]
        with patch.object(self.server, "case_summary", side_effect=AssertionError("full detail")):
            result = self.success(route + "/settings", {"name": "Renamed"})
            self.assertEqual(result["name"], "Renamed")
            result = self.success(route + "/plot-settings", {"settings": {"include_fees": True}})
            self.assertTrue(result["run_defaults"]["include_fees"])
            self.assertNotIn("plots", result)

    def test_collection_uses_summaries_filters_known_projections_and_keeps_pending_ids(self):
        case, metadata, route = self.collected()
        known, pending = "b" * 16, "c" * 16
        for run in (known, pending):
            (case / "runs" / run).mkdir()
        summary = {"id": metadata["latest_run"], "status": "bounded_complete", "seeds": metadata["seeds"],
                   "transaction_count": 7, "source": "private-source"}
        def load(archive, case_id, **kwargs):
            self.assertEqual(case_id, metadata["case_id"])
            if archive.name == metadata["latest_run"]:
                return summary, "ready"
            if archive.name == known:
                return {"id": known, "collection_source": {"kind": "shared"}}, "ready"
            return None, "loading"
        with patch("liquid_tracer.run_summaries.get_run_summary", side_effect=load):
            result = self.success(route + "/collection")
        self.assertEqual({run["id"] for run in result["runs"]}, {metadata["latest_run"], pending})
        self.assertEqual(result["latest"]["transaction_count"], 7)
        self.assertEqual(result["sections"], {"collection": "ready"})
        self.assertNotIn("private-source", json.dumps(result))

    def test_only_selected_run_is_scheduled_until_history_is_requested(self):
        case, metadata, route = self.collected()
        older = "b" * 16
        (case / "runs" / older).mkdir()
        calls = []
        def load(archive, case_id, **options):
            calls.append((archive.name, options))
            if archive.name == metadata["latest_run"]:
                return {"id": archive.name, "status": "bounded_complete"}, "ready"
            return None, "loading"
        with patch("liquid_tracer.run_summaries.get_run_summary", side_effect=load):
            result = self.success(route + "/collection")
            self.assertEqual(result["sections"], {"collection": "ready"})
            self.assertIn((older, {"schedule": False, "priority": False}), calls)
            calls.clear()
            result = self.success(route + "/collection/" + older)
            self.assertEqual(result["sections"], {"collection": "loading"})
            self.assertIn((older, {"schedule": True, "priority": True}), calls)
            calls.clear()
            with patch.object(self.server, "saved_artifacts", return_value={}), \
                 patch.object(self.server, "pegout_searches", return_value=[]):
                result = self.success(route + "/history")
            self.assertEqual(result["sections"], {"history": "loading"})
            self.assertIn((older, {"schedule": True, "priority": False}), calls)
            self.assertEqual({run["id"] for run in result["runs"]}, {older, metadata["latest_run"]})

    def test_unsafe_latest_archive_does_not_hide_investigation_settings(self):
        _, detail = self.create()
        case, metadata = self.server.case(detail["id"])
        run = "b" * 16
        metadata["latest_run"] = run
        save_json(case / "case.json", metadata)
        (case / "runs").mkdir(exist_ok=True)
        (case / "runs" / run).symlink_to(self.base, target_is_directory=True)
        result = self.success("/api/cases/" + detail["id"] + "/overview")
        self.assertEqual(result["status"], "Saved run unavailable")
        self.assertEqual(result["seeds"], metadata["seeds"])
        self.assertEqual(result["name"], metadata["name"])

    def test_workflow_and_board_endpoints_are_independent(self):
        _, detail = self.create()
        route = "/api/cases/" + detail["id"]
        with patch("liquid_tracer.workflow_api.case_workflow", return_value={"plots": []}) as workflow, \
             patch("liquid_tracer.investigation_views.boards", side_effect=AssertionError("boards")):
            self.assertEqual(self.success(route + "/workflow"), {"plots": [], "sections": {"workflow": "ready"}})
            self.assertTrue(workflow.call_args.kwargs["lightweight"])
        with patch("liquid_tracer.workflow_api.case_workflow", side_effect=AssertionError("plots")), \
             patch("liquid_tracer.workflow_api.case_boards", return_value={"boards": []}), \
             patch("liquid_tracer.cli.miro_recovery_status", return_value=None), \
             patch("liquid_tracer.board_rebuild.rebuild_status", return_value=None):
            result = self.success(route + "/boards")
            self.assertEqual(result["boards"], [])
            self.assertEqual(result["sections"], {"boards": "ready"})
            self.assertIsNone(result["miro_rebuild"])

    def test_slow_selected_plot_verification_does_not_block_overview_or_jobs(self):
        _, detail = self.create()
        route = "/api/cases/" + detail["id"]
        entered, release = threading.Event(), threading.Event()
        errors = []
        def selected(case, preview):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release verification")
            return {"preview_id": preview, "reviewable": True}
        def request_selected():
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=8)
            try:
                connection.request("GET", route + "/plots/" + "a" * 16 + "-plots-" + "b" * 8)
                response = connection.getresponse()
                if response.status != 200:
                    errors.append(response.read())
                else:
                    response.read()
            except Exception as error:
                errors.append(error)
            finally:
                connection.close()
        with patch("liquid_tracer.workflow_api.selected_plot", side_effect=selected):
            worker = threading.Thread(target=request_selected)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.success(route + "/overview")["id"], detail["id"])
                self.assertEqual(self.success("/api/jobs"), {"jobs": []})
            finally:
                release.set()
                worker.join(8)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_shared_view_reads_cached_runs_without_compatibility_archive_scan(self):
        from liquid_tracer.api import ENTERPRISE
        case, metadata, route = self.collected()
        shared = self.server.root / ".shared-collection"
        shared.mkdir()
        dataset = {**metadata, "case_id": "d" * 32, "shared_dataset": True, "source": ENTERPRISE}
        save_json(shared / "case.json", dataset)
        (shared / "runs" / metadata["latest_run"]).mkdir(parents=True)
        private = {"id": metadata["latest_run"], "source": ENTERPRISE}
        shared_summary = {"id": metadata["latest_run"], "status": "bounded_complete", "seeds": metadata["seeds"],
                          "transaction_count": 20, "shared_collection": {"dataset_id": dataset["case_id"],
                          "members": [{"id": metadata["case_id"], "name": metadata["name"]}],
                          "policy_case_name": metadata["name"]}}
        def load(archive, case_id, **kwargs):
            return (shared_summary if case_id == dataset["case_id"] else private), "ready"
        with patch("liquid_tracer.run_summaries.get_run_summary", side_effect=load), \
             patch("liquid_tracer.shared_collection._compatible", side_effect=AssertionError("full source read")):
            result = self.success(route + "/shared-collection")
        self.assertEqual(result["sections"], {"shared": "ready"})
        self.assertTrue(result["shared_collection"]["compatible"])
        self.assertEqual(result["shared_collection"]["runs"][0]["transaction_count"], 20)
        self.assertNotIn(ENTERPRISE, json.dumps(result))
        self.assertNotIn(str(case), json.dumps(result))


if __name__ == "__main__":
    unittest.main()
