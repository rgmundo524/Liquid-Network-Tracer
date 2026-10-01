"""Shared evidence crosses the real HTTP/worker boundary without replacing private runs."""
import json
from unittest.mock import patch
import unittest

from liquid_tracer.common import read_json
from liquid_tracer.investigations import create_investigation, read_case, update_case
from liquid_tracer.job_resources import job_resources
from tests import test_web


class SharedCollectionWebTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    wait = test_web.LocalWebTests.wait
    create = test_web.LocalWebTests.create

    def investigations(self):
        txid, info = self.create()
        first, metadata = self.server.case(info["id"])
        settings = {**metadata["run_defaults"], "layout_attempts": 1, "max_transactions": 100,
                    "max_outpoints": 100, "max_requests": 100, "max_seconds": 30}
        update_case(first, {"run_defaults": settings})
        fixture = read_json(test_web.SYNTHETIC_API)
        child = fixture["/tx/" + txid + "/outspends"][0]["txid"]
        second = create_investigation(self.server.root, "Individual second seed", fixture=test_web.SYNTHETIC_API,
                                      seeds=[child + ":0"], run_defaults=settings)
        return first, second, txid, child

    def collect(self, first, second, hops=2):
        ids = [read_case(path)["case_id"] for path in (first, second)]
        job = self.success("/api/cases/" + ids[0] + "/actions", {
            "action": "shared-trace", "mode": "collect", "case_ids": ids,
            "hops": hops, "hop_reference_name": ""}, 202)
        return self.wait(job)

    def test_shared_collect_and_continue_leave_private_data_and_old_revision_untouched(self):
        first, second, txid, child = self.investigations()
        before = {path: (path / "case.json").read_bytes() for path in (first, second)}
        result = self.collect(first, second)
        shared = result["shared_collection"]
        self.assertEqual(result["data_source"], "shared")
        self.assertEqual(set(shared["seeds"]), {txid + ":0", child + ":0"})
        self.assertEqual(shared["latest_run"], result["run_id"])
        self.assertEqual(shared["runs"][0]["max_hops"], 2)
        self.assertNotIn(str(self.base), json.dumps(result))
        archive = self.server.root / ".shared-collection" / "runs" / result["run_id"]
        original = {p.name: p.read_bytes() for p in archive.iterdir() if p.is_file()}
        job = self.success("/api/cases/" + read_case(second)["case_id"] + "/actions", {
            "action": "shared-trace", "mode": "continue", "run_id": result["run_id"],
            "hops": 1, "hop_reference_name": ""}, 202)
        continued = self.wait(job)
        self.assertEqual(continued["shared_collection"]["runs"][0]["max_hops"], 3)
        self.assertEqual(continued["shared_collection"]["seeds"], shared["seeds"])
        self.assertEqual(original, {p.name: p.read_bytes() for p in archive.iterdir() if p.is_file()})
        self.assertEqual(len(self.success("/api/session")["cases"]), 2)
        for case in (first, second):
            detail = self.success("/api/cases/" + read_case(case)["case_id"])
            self.assertEqual(detail["runs"], [])
            self.assertIsNone(detail["latest_run"])
            self.assertEqual(detail["shared_collection"]["latest_run"], continued["run_id"])
            self.assertEqual((case / "case.json").read_bytes(), before[case])
        # The internal dataset is not another investigation route.
        self.assertEqual(self.request("/api/cases/" + shared["dataset_id"])[0], 404)

    def test_shared_plot_pins_source_without_requiring_a_private_run(self):
        first, second, _, _ = self.investigations()
        result = self.collect(first, second)
        route = "/api/cases/" + read_case(first)["case_id"] + "/actions"
        dataset = result["shared_collection"]["dataset_id"]
        with patch.object(self.server, "start_job", return_value={"id": "plot"}) as start:
            self.success(route, {"action": "plot", "goal": "connections", "run_id": "latest",
                "data_source": "shared", "dataset_id": dataset}, 202)
            args = start.call_args.args[0]
            self.assertEqual(args[args.index("--run") + 1], result["run_id"])
            self.assertEqual(args[args.index("--dataset-id") + 1], dataset)
            self.assertEqual(args[args.index("--data-source") + 1], "shared")
            self.assertFalse(start.call_args.kwargs["live"])
            start.reset_mock()
            self.assertEqual(self.request(route, {"action": "plot", "goal": "connections", "run_id": "latest",
                "data_source": "shared", "dataset_id": "f" * 32})[0], 400)
            start.assert_not_called()

    def test_one_shared_writer_conflicts_across_cases_but_private_collection_remains_independent(self):
        first, second, _, _ = self.investigations()
        identity = read_case(first)["case_id"]
        self.server.jobs["busy"] = {"id": "busy", "case_id": identity, "status": "running", "action": "shared-trace",
            **job_resources(["shared-collect", "--case", str(first)], "shared-trace", first)}
        for case in (first, second):
            case_id = read_case(case)["case_id"]
            route = "/api/cases/" + case_id + "/actions"
            with patch("liquid_tracer.shared_collection.prepare_collection") as prepare:
                self.assertEqual(self.request(route, {"action": "shared-trace", "mode": "collect",
                    "case_ids": [case_id], "hops": 1})[0], 409)
                prepare.assert_not_called()
            with patch.object(self.server, "start_job", return_value={"id": "private"}) as start:
                self.success(route, {"action": "trace", "hops": 1}, 202)
                self.assertEqual(start.call_args.kwargs["action"], "trace")
        self.server.jobs.clear()

    def test_real_shared_plot_and_csv_use_recipient_seeds_without_changing_private_history(self):
        first, second, txid, child = self.investigations()
        result = self.collect(first, second)
        route = "/api/cases/" + read_case(first)["case_id"]
        job = self.success(route + "/actions", {"action": "plot", "goal": "pegouts",
            "run_id": result["run_id"], "data_source": "shared",
            "dataset_id": result["shared_collection"]["dataset_id"], "min_hops": 0, "max_hops": 3,
            "include_unspent": True, "include_unspendable": True}, 202)
        plotted = self.wait(job)
        detail = self.success(route)
        self.assertEqual(detail["runs"], [])
        self.assertIsNone(detail["latest_run"])
        plot = next(item for item in detail["plots"] if item["preview_id"] == plotted["preview_id"])
        self.assertEqual(plot["collection_source"]["run_id"], result["run_id"])
        self.assertNotEqual(plot["run_id"], result["run_id"])
        state = read_json(first / "runs" / plot["run_id"] / "trace.json")
        self.assertEqual(state["seeds"], [txid + ":0"])
        self.assertEqual(state["transactions"][child]["depth"], 1)
        links = {item["name"]: item["url"] for item in plot["artifact"]["downloads"]}
        self.assertIn(b"Source Seed Outpoints", self.success(links["endpoints.csv"]))
        self.assertIn(b"Transaction Hash", self.success(links["transactions.csv"]))

    def test_invalid_shared_scope_rejected_without_creating_a_dataset(self):
        first, second, _, _ = self.investigations()
        first_id, second_id = (read_case(case)["case_id"] for case in (first, second))
        route = "/api/cases/" + first_id + "/actions"
        for members in ([], [second_id], ["../../"], [first_id, "f" * 32]):
            self.assertEqual(self.request(route, {"action": "shared-trace", "mode": "collect",
                "case_ids": members, "hops": 1})[0], 400)
        self.assertFalse((self.server.root / ".shared-collection").exists())
        self.assertIsNone(read_case(first).get("latest_run"))
        self.assertEqual(self.request(route, {"action": "shared-trace", "mode": "continue",
            "case_ids": [first_id], "run_id": "a" * 16, "hops": 1})[0], 400)


if __name__ == "__main__":
    unittest.main()
