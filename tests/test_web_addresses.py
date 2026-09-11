"""Address review routes exercise the real loopback server and saved case workflow."""

import json
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.web import public_address_activity
from tests import test_web


class WebAddressTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    wait = test_web.LocalWebTests.wait
    create = test_web.LocalWebTests.create

    def test_list_filter_pagination_and_service_decisions_preserve_saved_run(self):
        _, case = self.create()
        route = "/api/cases/" + case["id"]
        traced = self.wait(self.success(route + "/actions", {"action": "trace"}, 202))
        path, _ = self.server.case(case["id"])
        archive = path / "runs" / traced["run_id"]
        before = {str(file.relative_to(archive)): file.read_bytes() for file in archive.iterdir() if file.is_file()}
        page = self.success(route + "/addresses", {"run_id": traced["run_id"], "limit": 1})
        self.assertEqual(page["run_id"], traced["run_id"])
        self.assertEqual(len(page["rows"]), 1)
        self.assertGreater(page["total"], 1)
        second = self.success(route + "/addresses", {"offset": 1, "limit": 1})
        self.assertNotEqual(page["rows"][0]["address"], second["rows"][0]["address"])
        address = page["rows"][0]["address"]
        designation = self.success(route + "/services", {"address": address, "name": "Reviewed service",
                                  "rationale": "Repeated consolidation.\nInvestigative assessment only.", "enabled": True})
        self.assertTrue(designation["service"]["enabled"])
        self.assertEqual(designation["revision"], 1)
        selected = self.success(route + "/addresses", {"suspected_only": True, "query": "Reviewed service"})
        self.assertEqual([row["address"] for row in selected["rows"]], [address])
        detail = self.success(route + "/address", {"address": address})
        self.assertEqual(detail["service"], designation["service"])
        self.assertIsNone(detail["activity"])
        removed = self.success(route + "/services", {"address": address, "enabled": False,
                               "name": designation["service"]["name"], "rationale": designation["service"]["rationale"]})
        self.assertFalse(removed["service"]["enabled"])
        self.assertEqual(self.success(route + "/addresses", {"suspected_only": True})["total"], 0)
        self.assertEqual(before, {str(file.relative_to(archive)): file.read_bytes() for file in archive.iterdir() if file.is_file()})
        self.assertEqual(len(read_json(path / "services.json")["history"]), 2)

    def test_pasted_address_works_without_a_run_or_network_lookup(self):
        _, case = self.create()
        route = "/api/cases/" + case["id"]
        with patch.object(self.server, "start_job") as start:
            result = self.success(route + "/address", {"address": "SYNTHETIC-outside-trace"})
            self.assertEqual(result, {"address": "SYNTHETIC-outside-trace", "activity": None, "service": None})
            self.success(route + "/services", {"address": result["address"], "enabled": True})
            rows = self.success(route + "/addresses", {})["rows"]
            self.assertEqual(rows[0]["run_output_count"], 0)
            start.assert_not_called()

    def test_lookup_argv_is_fixed_bounded_and_uses_saved_source(self):
        _, case = self.create()
        route = "/api/cases/" + case["id"]
        path, _ = self.server.case(case["id"])
        fake = {"id": "synthetic", "status": "running"}
        with patch.object(self.server, "start_job", return_value=fake) as start:
            body = {"action": "address-inspect", "address": "SYNTHETIC-reviewed-address", "max_pages": 99999,
                    "max_requests": 99999, "max_seconds": 99999, "source": "https://attacker.invalid",
                    "arguments": ["--shell"], "fixture": "/private/sentinel"}
            self.success(route + "/actions", body, 202)
            self.assertEqual(start.call_args.args[0], ["address-inspect", "--case", str(path),
                             "--address", "SYNTHETIC-reviewed-address", "--run", "latest", "--max-pages", "5",
                             "--max-requests", "10", "--max-seconds", "60"])
            self.assertFalse(start.call_args.kwargs["live"])
            # Fixture is immutable from the browser; construct live metadata locally.
            metadata = read_json(path / "case.json")
            metadata["fixture"] = None
            save_json(path / "case.json", metadata)
            self.success(route + "/actions", body, 202)
            self.assertTrue(start.call_args.kwargs["live"])

    def test_actual_bounded_lookup_saves_then_reopens_safe_summary(self):
        _, case = self.create()
        route = "/api/cases/" + case["id"]
        path, metadata = self.server.case(case["id"])
        address = "SYNTHETIC-reviewed-address"
        fixture = self.base / "address-fixture.json"
        save_json(fixture, {"/address/" + address: {"address": address,
            "chain_stats": {"tx_count": 2, "funded_txo_count": 3, "spent_txo_count": 1},
            "mempool_stats": {"tx_count": 1, "funded_txo_count": 0, "spent_txo_count": 1}},
            "/address/" + address + "/txs/chain": [
                {"txid": "b" * 64, "status": {"confirmed": True, "block_height": 2, "block_time": 1700000010}},
                {"txid": "a" * 64, "status": {"confirmed": True, "block_height": 1, "block_time": 1700000000}},
            ]})
        metadata["fixture"] = str(fixture)
        save_json(path / "case.json", metadata)
        result = self.wait(self.success(route + "/actions", {"action": "address-inspect", "address": address}, 202))
        self.assertEqual(result["confirmed_tx_count"], 2)
        self.assertEqual(result["mempool_tx_count"], 1)
        self.assertEqual(result["confirmed_unspent_output_count"], 2)
        self.assertEqual(result["mempool_unspent_output_delta"], -1)
        self.assertEqual(result["unspent_output_count"], 1)
        self.assertTrue(result["history_complete"])
        self.assertEqual(result["first_confirmed_activity"]["txid"], "a" * 64)
        self.assertNotIn(str(fixture), json.dumps(result))
        self.assertNotIn("source", result)
        self.assertNotIn("directory", result)
        self.assertEqual(self.success(route + "/address", {"address": address})["activity"], result)
        self.assertEqual(self.success(route + "/addresses", {})["rows"][0]["activity"], result)
        self.assertEqual(len(list((path / "address-reviews").glob("*.json"))), 2)

    def test_selected_snapshot_is_used_for_saved_and_live_address_review(self):
        _, case = self.create()
        route = "/api/cases/" + case["id"]
        traced = self.wait(self.success(route + "/actions", {"action": "trace"}, 202))
        path, _ = self.server.case(case["id"])
        address = "SYNTHETIC-reviewed-address"
        with patch("liquid_tracer.address_review.saved_activity", return_value=None) as saved:
            self.success(route + "/address", {"address": address, "run_id": traced["run_id"]})
            saved.assert_called_once_with(path, address, run_id=traced["run_id"])
        with patch.object(self.server, "start_job", return_value={"id": "synthetic", "status": "running"}) as start:
            self.success(route + "/actions", {"action": "address-inspect", "address": address,
                                              "run_id": traced["run_id"]}, 202)
            argv = start.call_args.args[0]
            self.assertEqual(argv[argv.index("--run") + 1], traced["run_id"])
            start.reset_mock()
            for bad_run in ("../../private", "x" * 16, 12):
                self.assertEqual(self.request(route + "/actions", {"action": "address-inspect", "address": address,
                                                                   "run_id": bad_run})[0], 400)
            start.assert_not_called()

    def test_routes_require_csrf_idle_and_valid_bounded_inputs(self):
        _, case = self.create()
        route = "/api/cases/" + case["id"]
        for suffix, body in (("addresses", {}), ("address", {"address": "SYNTHETIC-safe"}),
                             ("services", {"address": "SYNTHETIC-safe", "enabled": True}),
                             ("actions", {"action": "address-inspect", "address": "SYNTHETIC-safe"})):
            self.assertEqual(self.request(route + "/" + suffix, body, headers={"X-Liquid-CSRF": "wrong"})[0], 403)
            self.server.active_job = "synthetic-busy"
            try:
                self.assertEqual(self.request(route + "/" + suffix, body)[0], 409)
            finally:
                self.server.active_job = None
        for body in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"offset": -1}, {"offset": 1.5},
                     {"suspected_only": "yes"}, {"query": "x" * 257}, {"run_id": "../../private"}):
            self.assertEqual(self.request(route + "/addresses", body)[0], 400, body)
        with patch.object(self.server, "start_job") as start:
            for address in ("../../private", "--shell", "https://example.invalid/addr", "SYNTHETIC a", None):
                self.assertEqual(self.request(route + "/actions", {"action": "address-inspect", "address": address})[0], 400)
                self.assertEqual(self.request(route + "/services", {"address": address, "enabled": True})[0], 400)
            start.assert_not_called()
        self.assertEqual(self.request(route + "/services", {"address": "SYNTHETIC-safe", "enabled": "true"})[0], 400)
        with patch("liquid_tracer.address_review.saved_activity", side_effect=TraceError("SYNTHETIC-SECRET /private/key")):
            status, result, _ = self.request(route + "/address", {"address": "SYNTHETIC-safe"})
            self.assertEqual(status, 400)
            self.assertNotIn("SYNTHETIC-SECRET", json.dumps(result))
            self.assertNotIn("/private/key", json.dumps(result))

    def test_success_summary_whitelist_does_not_forward_raw_errors_or_paths(self):
        summary = {"address": "SYNTHETIC-safe", "observed_at": "/private/sentinel", "directory": "/private/sentinel",
                   "source": "https://user:SYNTHETIC-SECRET@example.invalid", "raw_error": "SYNTHETIC-SECRET",
                   "history_stop_reason": "SYNTHETIC-SECRET", "observation_ids": [1, "SYNTHETIC-SECRET"],
                   "warnings": ["SYNTHETIC-SECRET"], "confirmed_tx_count": True,
                   "mempool_unspent_output_delta": -2,
                   "latest_confirmed_activity": {"txid": "a" * 64, "date_utc": "/private/sentinel", "body": "SYNTHETIC-SECRET"}}
        result = public_address_activity(summary)
        self.assertNotIn("SYNTHETIC-SECRET", json.dumps(result))
        self.assertNotIn("/private/sentinel", json.dumps(result))
        self.assertIsNone(result["confirmed_tx_count"])
        self.assertEqual(result["mempool_unspent_output_delta"], -2)
        self.assertEqual(result["observation_ids"], [1])


if __name__ == "__main__":
    unittest.main()
