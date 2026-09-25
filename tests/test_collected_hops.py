"""Collected depth reflects saved transactions, independently of run limits."""

import unittest

from liquid_tracer.common import read_json
from liquid_tracer.web import collected_hops
from tests import test_web


class CollectedDepthTests(unittest.TestCase):
    def test_recorded_transactions_exclude_context_frontier_and_configured_limit(self):
        state = {"limits": {"max_hops": 10}, "stats": {"collected_hops": 9},
                 "transactions": {
                     "seed": {"depth": 0, "data": {"vin": [{"depth": 99}]}},
                     "child": {"depth": 2}},
                 "outputs": {"pending": {"depth": 8, "status": "pending"}},
                 "observations": [{"depth": 20}]}
        self.assertEqual(collected_hops(state), 2)
        self.assertEqual(collected_hops({"transactions": {"seed": {"depth": 0}}}), 0)

    def test_missing_or_malformed_depth_is_unknown_not_zero_or_partial_maximum(self):
        for transactions in (None, [], {}, {"seed": {}}, {"seed": None},
                             {"seed": {"depth": -1}}, {"seed": {"depth": True}},
                             {"seed": {"depth": "1"}}, {"seed": {"depth": 1.5}},
                             {"seed": {"depth": 2 ** 53}},
                             {"seed": {"depth": 0}, "child": {}}):
            with self.subTest(transactions=transactions):
                self.assertIsNone(collected_hops({"transactions": transactions}))
        self.assertIsNone(collected_hops({}))


class CollectedDepthWebTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    wait = test_web.LocalWebTests.wait
    create = test_web.LocalWebTests.create

    def test_partial_collection_and_historic_snapshot_keep_their_own_depth(self):
        _, case = self.create()
        route = "/api/cases/" + case["id"]
        first = self.wait(self.success(route + "/actions", {
            "action": "trace", "settings": {"hops": 10, "max_transactions": 2}}, 202))
        self.assertEqual((first["status"], first["stop_reason"]), ("paused", "transaction_limit"))
        partial = self.success(route)
        self.assertEqual(partial["latest"]["max_hops"], 10)
        self.assertEqual(partial["latest"]["collected_hops"], 1)
        path, _ = self.server.case(case["id"])
        archive = path / "runs" / first["run_id"]
        before = {str(file.relative_to(archive)): file.read_bytes()
                  for file in archive.rglob("*") if file.is_file()}

        second = self.wait(self.success(route + "/actions", {
            "action": "trace", "settings": {"hops": 10, "max_transactions": 20}}, 202))
        self.assertEqual(second["status"], "bounded_complete")
        detail = self.success(route)
        runs = {run["id"]: run for run in detail["runs"]}
        self.assertEqual(runs[first["run_id"]]["collected_hops"], 1)
        self.assertEqual(runs[second["run_id"]]["collected_hops"], 3)
        self.assertEqual(detail["latest"], runs[second["run_id"]])
        session = self.success("/api/session")
        self.assertEqual(session["cases"][0]["latest"]["collected_hops"], 3)
        self.assertEqual(before, {str(file.relative_to(archive)): file.read_bytes()
                                  for file in archive.rglob("*") if file.is_file()})
        self.assertNotIn("collected_hops", read_json(archive / "trace.json")["stats"])

    def test_hop_zero_is_reported_and_no_saved_collection_remains_unknown(self):
        _, case = self.create()
        route = "/api/cases/" + case["id"]
        self.assertNotIn("latest", self.success(route))
        self.wait(self.success(route + "/actions", {"action": "trace", "hops": 0}, 202))
        detail = self.success(route)
        self.assertEqual(detail["latest"]["collected_hops"], 0)
        self.assertEqual(detail["runs"][0]["collected_hops"], 0)
        self.assertEqual(detail["latest"]["transaction_count"], 1)


if __name__ == "__main__":
    unittest.main()
