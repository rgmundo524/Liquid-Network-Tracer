"""Offline HTTP/worker roundtrip and prompt admission for peg-in matching."""
import threading
import time
import sys
import unittest
from unittest.mock import Mock, patch

from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.inspection import transaction_outputs
from liquid_tracer.investigations import create_investigation, read_case
from tests import test_web as web_tests
from tests.test_pegin_matching import BTC, CLAIM, OTHER, HISTORY, claim, bitcoin
from tests.test_pegin_window import Chain, block_hash


class PeginOutputReportValidationTests(unittest.TestCase):
    def test_bitcoin_wrapper_cannot_expose_a_liquid_transaction(self):
        transaction = transaction_outputs(BTC, bitcoin(), blockchain="bitcoin")
        report = {"blockchain": "bitcoin", "transactions": [transaction]}
        validated = web_tests.LocalServer.public_result(None, report, "pegin-outputs", None, [BTC])
        self.assertEqual(validated["transactions"][0]["blockchain"], "bitcoin")
        self.assertEqual(validated["transactions"][0]["outputs"][1]["value_text"], "60000000")
        transaction["blockchain"] = "liquid"
        with self.assertRaisesRegex(TraceError, "different blockchain"):
            web_tests.LocalServer.public_result(None, report, "pegin-outputs", None, [BTC])


class PeginWebTests(unittest.TestCase):
    close_server = web_tests.LocalWebTests.close_server
    request = web_tests.LocalWebTests.request
    success = web_tests.LocalWebTests.success
    wait = web_tests.LocalWebTests.wait

    def setUp(self):
        web_tests.LocalWebTests.setUp(self)
        self.fixture = self.base / "matching-api.json"
        save_json(self.fixture, {"/tx/" + BTC: bitcoin(), "/tx/" + CLAIM: claim(),
                                 HISTORY: [claim()], HISTORY + "/" + CLAIM: []})
        self.case = create_investigation(self.server.root, "Bitcoin matching", blockchain="bitcoin",
                                         fixture=self.fixture)
        self.identity = read_case(self.case)["case_id"]
        self.route = "/api/cases/" + self.identity

    def test_real_worker_saves_matching_results_and_integrity_checked_downloads(self):
        job = self.success(self.route + "/actions",
                           {"action": "pegin-match", "txid": BTC, "vout": 1,
                            "liquid_txid": CLAIM, "max_pages": 10}, 202)
        self.assertTrue(job["cancellable"])
        result = self.wait(job)
        self.assertEqual(result["status"], "matched")
        self.assertEqual((result["query"]["start_minutes"], result["query"]["end_minutes"]), (-5, 4320))
        self.assertEqual(result["matches"][0]["bitcoin_value_sats"], "60000000")
        self.assertEqual(result["liquid_outputs"][CLAIM][1]["value"], "123")
        listing = self.success(self.route + "/pegin-matches")
        self.assertEqual(listing["reports"][0]["id"], result["id"])
        selected = self.success(self.route + "/pegin-matches/" + result["id"])
        self.assertEqual(selected, result)
        for download in result["downloads"]:
            with self.subTest(download=download["name"]):
                status, body, response = self.request(download["url"])
                self.assertEqual(status, 200)
                if download["name"] == "matches.csv":
                    self.assertIn(CLAIM.encode(), body)
                    self.assertIn(b"Bitcoin deposit value", body)
        (self.case / "pegin-matches" / result["id"] / "matches.csv").write_text("altered")
        self.assertEqual(self.request(result["downloads"][0]["url"])[0], 400)
        bad = self.route + "/pegin-matches/" + "f" * 32
        self.assertIn(self.request(bad)[0], (400, 404))

    def test_slow_worker_does_not_block_jobs_and_reserves_shared_index_immediately(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(identity, *args, **kwargs):
            entered.set()
            release.wait(5)
            with self.server.job_lock:
                self.server.jobs[identity].update(status="succeeded", cancellable=False)
        self.addCleanup(release.set)
        with patch.object(self.server, "run_job", side_effect=blocked):
            job = self.success(self.route + "/actions",
                               {"action": "pegin-match", "txid": BTC, "max_pages": 10}, 202)
            self.assertTrue(entered.wait(1))
            start = time.monotonic()
            jobs = self.success("/api/jobs")
            self.assertLess(time.monotonic() - start, 1)
            self.assertIn(job["id"], str(jobs))
            other = create_investigation(self.server.root, "Other BTC", blockchain="bitcoin", fixture=self.fixture)
            route = "/api/cases/" + read_case(other)["case_id"] + "/actions"
            self.assertEqual(self.request(route, {"action": "pegin-match", "txid": BTC, "max_pages": 10})[0], 409)
            release.set()

    def test_rejects_unknown_parameters_and_liquid_investigations(self):
        self.assertEqual(self.request(self.route + "/actions", {"action": "pegin-match", "txid": BTC,
            "max_pages": 10, "fixture": "/untrusted/path"})[0], 400)
        case = create_investigation(self.server.root, "Liquid")
        route = "/api/cases/" + read_case(case)["case_id"]
        self.assertEqual(self.request(route + "/actions",
            {"action": "pegin-match", "txid": BTC, "max_pages": 10})[0], 400)
        self.assertEqual(self.request(route + "/pegin-matches")[0], 400)


class WorkspacePeginWebTests(unittest.TestCase):
    close_server = web_tests.LocalWebTests.close_server
    request = web_tests.LocalWebTests.request
    success = web_tests.LocalWebTests.success
    wait = web_tests.LocalWebTests.wait

    def setUp(self):
        web_tests.LocalWebTests.setUp(self)
        self.fixture = self.base / "matching-api.json"
        save_json(self.fixture, {"/tx/" + BTC: bitcoin(), "/tx/" + CLAIM: claim(),
                                 HISTORY: [claim()], HISTORY + "/" + CLAIM: []})
        self.route = "/api/tools/pegin-matches"
        self.body = {"action": "pegin-match", "txid": BTC, "vout": 1,
                     "liquid_txid": CLAIM, "max_pages": 10}

    def test_empty_workspace_roundtrip_and_downloads_do_not_create_a_case(self):
        self.assertEqual(self.success(self.route), {"reports": []})
        def offline(request, result, live=False):
            self.assertFalse(live, "Public matching must not invoke the credential provider")
            payload = read_json(request)
            self.assertNotIn("terminal_handoff", payload)
            arguments = payload["arguments"]
            self.assertNotIn("--case", arguments)
            self.assertIn("--public", arguments)
            arguments += ["--bitcoin-fixture", str(self.fixture), "--liquid-fixture", str(self.fixture)]
            save_json(request, payload)
            return web_tests.worker_command(request, result, live=False)
        credential_lock = Mock(acquire=Mock(side_effect=AssertionError("Public matching must not unlock credentials")))
        with patch("liquid_tracer.web.worker_command", side_effect=offline), \
                patch.object(self.server, "credential_lock", credential_lock):
            job = self.success(self.route, {**self.body, "start_hours": -3.5, "end_hours": 6.25}, 202)
            self.assertIsNone(job["case_id"])
            self.assertTrue(job["live"])
            result = self.wait(job)
        credential_lock.acquire.assert_not_called()
        self.assertEqual(result["status"], "matched")
        self.assertEqual((result["query"]["start_hours"], result["query"]["end_hours"]), (-3.5, 6.25))
        self.assertIsNone(result["case_id"])
        self.assertEqual(self.success("/api/session")["cases"], [])
        self.assertEqual(self.success(self.route)["reports"][0]["id"], result["id"])
        self.assertEqual(self.success(self.route + "/" + result["id"]), result)
        for download in result["downloads"]:
            self.assertTrue(download["url"].startswith("/files/pegin-matches/"))
            self.assertEqual(self.request(download["url"])[0], 200)
        (self.server.root / ".pegin-matching" / "pegin-matches" / result["id"] / "matches.csv").write_text("altered")
        self.assertEqual(self.request(result["downloads"][0]["url"])[0], 400)

    def test_workspace_library_keeps_earlier_case_results_accessible_without_changing_them(self):
        from liquid_tracer.pegin_matching import match_pegin
        case = create_investigation(self.server.root, "Earlier case", blockchain="bitcoin")
        metadata = (case / "case.json").read_bytes()
        result = match_pegin(case, BTC, liquid_txid=CLAIM,
                             bitcoin_fixture=self.fixture, liquid_fixture=self.fixture)
        report_path = case / "pegin-matches" / result["id"] / "match.json"
        before = report_path.read_bytes()
        row, = self.success(self.route)["reports"]
        self.assertEqual(row["source_case_id"], read_case(case)["case_id"])
        self.assertEqual(row["source_case_name"], "Earlier case")
        old_route = "/api/cases/" + row["source_case_id"] + "/pegin-matches/" + row["id"]
        self.assertEqual(self.success(old_route)["id"], result["id"])
        self.assertEqual(report_path.read_bytes(), before)
        self.assertEqual((case / "case.json").read_bytes(), metadata)

    def test_slow_workspace_job_is_visible_cancellable_and_conflicts_with_case_matcher(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(identity, *args, **kwargs):
            entered.set()
            release.wait(5)
        self.addCleanup(release.set)
        with patch.object(self.server, "run_job", side_effect=blocked):
            job = self.success(self.route, self.body, 202)
            self.assertTrue(entered.wait(1))
            self.assertTrue(job["cancellable"])
            self.assertEqual(job["resource_kind"], "pegin_index")
            self.assertIsNone(job["case_id"])
            self.assertTrue(any(j["id"] == job["id"] for j in self.success("/api/jobs")["jobs"]))
            self.assertEqual(self.request(self.route, self.body)[0], 409)
            case = create_investigation(self.server.root, "BTC", blockchain="bitcoin", fixture=self.fixture)
            self.assertEqual(self.request("/api/cases/" + read_case(case)["case_id"] + "/actions", self.body)[0], 409)
            canceled = self.success("/api/jobs/" + job["id"] + "/cancel", {}, 202)
            self.assertEqual(canceled["status"], "cancelling")
            release.set()

    def test_workspace_matching_refuses_browser_paths_and_scope_injection(self):
        for key, value in (("case_id", "a"*32), ("investigations_dir", "/tmp"), ("fixture", str(self.fixture)),
                           ("base_url", "https://untrusted.invalid"), ("action", "trace")):
            self.assertEqual(self.request(self.route, {**self.body, key: value})[0], 400)
        self.assertIn(self.request("/files/pegin-matches/" + "a"*32 + "/case.json")[0], (400, 404))

    def test_invalid_window_and_public_selection_are_rejected_before_job_admission(self):
        changes = ({"start_hours": None, "end_hours": None}, {"start_hours": 12, "end_hours": 6},
                   {"start_hours": True}, {"end_hours": 87601}, {"public": "false"}, {"public": 1},
                   {"start_minutes": None}, {"end_minutes": 5256001}, {"start_minutes": True},
                   {"start_minutes": 15, "end_minutes": -5}, {"start_minutes": -5, "end_hours": 72})
        with patch.object(self.server, "start_job") as start:
            for fields in changes:
                with self.subTest(fields=fields):
                    self.assertEqual(self.request(self.route, {**self.body, **fields})[0], 400)
        start.assert_not_called()
        self.assertEqual(self.success("/api/jobs")["jobs"], [])

    def test_minute_window_is_saved_without_hour_conversion_in_query(self):
        def offline(request, result, live=False):
            self.assertFalse(live)
            payload = read_json(request)
            payload["arguments"] += ["--bitcoin-fixture", str(self.fixture), "--liquid-fixture", str(self.fixture)]
            save_json(request, payload)
            return web_tests.worker_command(request, result, live=False)
        with patch("liquid_tracer.web.worker_command", side_effect=offline):
            job = self.success(self.route, {**self.body, "start_minutes": -5, "end_minutes": 15}, 202)
            result = self.wait(job)
        self.assertEqual(result["status"], "matched")
        self.assertEqual((result["query"]["start_minutes"], result["query"]["end_minutes"]), (-5, 15))
        self.assertNotIn("start_hours", result["query"])
        self.assertEqual(self.success(self.route + "/" + result["id"]), result)


class WorkspacePeginWorkflowWebTests(unittest.TestCase):
    close_server = web_tests.LocalWebTests.close_server
    request = web_tests.LocalWebTests.request
    success = web_tests.LocalWebTests.success
    wait = web_tests.LocalWebTests.wait

    def setUp(self):
        web_tests.LocalWebTests.setUp(self)
        self.route = "/api/tools/pegin-matches"
        self.fixture = self.base / "workflow-api.json"
        chain = Chain(10)
        first = chain.add(5, CLAIM)
        first["vin"].append({"txid": BTC, "vout": 0, "is_pegin": True})
        first["vin"].append({"txid": OTHER, "vout": 1, "is_pegin": True})
        other = bitcoin()
        other["txid"] = OTHER
        other["vout"][1]["value"] = 4567890123
        fixture = {"/tx/" + BTC: bitcoin(), "/tx/" + OTHER: other, "/tx/" + CLAIM: first,
                   HISTORY: [first], HISTORY + "/" + CLAIM: [], "/blocks": chain.response("/blocks")}
        for height in chain.headers:
            fixture["/blocks/" + str(height)] = chain.response("/blocks/" + str(height))
            fixture["/block/" + block_hash(height) + "/status"] = {"in_best_chain": True}
            fixture["/block/" + block_hash(height) + "/txs"] = chain.transactions.get(height, [
                {"txid": format(height + 20000, "064x"), "vin": [], "vout": [],
                 "status": {"confirmed": True, "block_height": height,
                            "block_hash": block_hash(height), "block_time": chain.headers[height]["timestamp"]}}])
        save_json(self.fixture, fixture)

    def offline(self, request, result, live=False):
        self.assertFalse(live, "Public workflows must not unlock credentials")
        payload = read_json(request)
        self.assertNotIn("terminal_handoff", payload)
        self.assertNotIn("--case", payload["arguments"])
        payload["arguments"] += ["--bitcoin-fixture", str(self.fixture), "--liquid-fixture", str(self.fixture)]
        save_json(request, payload)
        return web_tests.worker_command(request, result, live=False)

    def test_batch_reverse_and_selected_outputs_complete_as_workspace_jobs(self):
        requests = [
            ({"mode": "batch", "txids": [BTC, OTHER]}, 3),
            ({"mode": "reverse", "liquid_txid": CLAIM}, 3),
            ({"txid": BTC, "vouts": [0, 1], "liquid_txid": CLAIM, "max_pages": 10}, 2),
        ]
        credential_lock = Mock(acquire=Mock(side_effect=AssertionError("No credentials for public fixture work")))
        with patch("liquid_tracer.web.worker_command", side_effect=self.offline), \
                patch.object(self.server, "credential_lock", credential_lock):
            for fields, expected_count in requests:
                with self.subTest(mode=fields.get("mode", "single")):
                    job = self.success(self.route, {"action": "pegin-match", **fields}, 202)
                    self.assertIsNone(job["case_id"])
                    self.assertEqual(job["resource_kind"], "pegin_index")
                    self.assertTrue(job["cancellable"])
                    result = self.wait(job)
                    self.assertEqual(len(result["matches"]), expected_count, result)
                    self.assertEqual(result["status"], "matched")
                    self.assertTrue(all(isinstance(row["bitcoin_value_sats"], str) for row in result["matches"]))
                    self.assertEqual(result["liquid_outputs"][CLAIM][1]["value"], "123")
                    if "mode" in fields:
                        self.assertEqual(result["query"]["mode"], fields["mode"])
                        self.assertEqual(result["bitcoins"][OTHER]["outputs"][1]["value"], "4567890123")
                    self.assertEqual(self.success(self.route + "/" + result["id"]), result)
                    for download in result["downloads"]:
                        code, data, _ = self.request(download["url"])
                        self.assertEqual(code, 200)
                        if download["name"] == "matches.csv":
                            self.assertIn(CLAIM.encode(), data)
                            self.assertIn(BTC.encode(), data)
                            self.assertIn(b"Status", data)
        credential_lock.acquire.assert_not_called()
        self.assertEqual(self.success("/api/session")["cases"], [])
        self.assertEqual(len(self.success(self.route)["reports"]), 3)

    def test_full_batch_fits_http_body_limit_and_new_modes_share_one_lock(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def blocked(identity, *args, **kwargs):
            entered.set()
            release.wait(5)
        with patch.object(self.server, "run_job", side_effect=blocked):
            body = {"action": "pegin-match", "mode": "batch",
                    "txids": [format(number, "064x") for number in range(500)]}
            job = self.success(self.route, body, 202)
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.request(self.route, {"action": "pegin-match", "mode": "reverse",
                                                       "liquid_txid": CLAIM})[0], 409)
            self.assertEqual(self.request(self.route, {"action": "pegin-match", "txid": BTC,
                                                       "vouts": [0, 1], "max_pages": 10})[0], 409)
            canceled = self.success("/api/jobs/" + job["id"] + "/cancel", {}, 202)
            self.assertEqual(canceled["status"], "cancelling")
            release.set()

    def test_batch_resume_reuses_completed_rows_and_keeps_both_report_downloads(self):
        body = {"action": "pegin-match", "mode": "batch", "txids": [BTC, OTHER]}
        with patch("liquid_tracer.web.worker_command", side_effect=self.offline):
            original = self.wait(self.success(self.route, body, 202))
            resumed = self.wait(self.success(self.route, {**body, "resume_report": original["id"],
                                                          "max_requests": 1}, 202))
        self.assertEqual(resumed["status"], "matched")
        self.assertNotEqual(original["id"], resumed["id"])
        self.assertEqual(resumed["matches"], original["matches"])
        self.assertEqual(resumed["coverage"]["requests_this_task"], 0)
        self.assertTrue(all(row["carried_from_report"] == original["id"] for row in resumed["results"]))
        self.assertEqual(self.success(self.route + "/" + original["id"]), original)
        self.assertEqual(self.success(self.route + "/" + resumed["id"]), resumed)
        for report in (original, resumed):
            for download in report["downloads"]:
                self.assertEqual(self.request(download["url"])[0], 200)
        self.assertEqual(self.success("/api/session")["cases"], [])

    def test_real_workers_cancel_and_keep_inspectable_reports_without_a_case(self):
        # The worker uses real matching/storage code; only its fixture fetch is
        # paused so cancellation has a deterministic point to interrupt.
        marker = self.base / "fetch-started"
        script = ("import pathlib, sys, time\n"
                  "from liquid_tracer.api import Esplora\n"
                  "from liquid_tracer.web_worker import main\n"
                  "original = Esplora.get\n"
                  "def delayed(self, *args, **kwargs):\n"
                  "    pathlib.Path(sys.argv[3]).touch()\n"
                  "    time.sleep(30)\n"
                  "    return original(self, *args, **kwargs)\n"
                  "Esplora.get = delayed\n"
                  "raise SystemExit(main(sys.argv[1:3]))\n")
        def slow_worker(request, result, live=False):
            self.offline(request, result, live)
            return [sys.executable, "-c", script, str(request), str(result), str(marker)]
        for identifiers in ({"mode": "batch", "txids": [BTC, OTHER]},
                            {"mode": "reverse", "liquid_txid": CLAIM}):
            with self.subTest(mode=identifiers["mode"]), \
                    patch("liquid_tracer.web.worker_command", side_effect=slow_worker):
                marker.unlink(missing_ok=True)
                job = self.success(self.route, {"action": "pegin-match", **identifiers}, 202)
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists(), "Synthetic worker did not reach fixture lookup")
                self.success("/api/jobs/" + job["id"] + "/cancel", {}, 202)
                while time.monotonic() < deadline:
                    terminal = self.success("/api/jobs/" + job["id"])
                    if terminal["status"] not in ("running", "cancelling"):
                        break
                    time.sleep(.03)
                self.assertEqual(terminal["status"], "canceled", terminal)
                report = next(row for row in self.success(self.route)["reports"]
                              if row["query"].get("mode") == identifiers["mode"])
                self.assertEqual(report["status"], "cancelled")
                selected = self.success(self.route + "/" + report["id"])
                self.assertTrue(selected["results"])
                self.assertTrue(all(self.request(download["url"])[0] == 200 for download in selected["downloads"]))
        self.assertEqual(self.success("/api/session")["cases"], [])


class WorkspacePeginOutputsWebTests(unittest.TestCase):
    close_server = web_tests.LocalWebTests.close_server
    request = web_tests.LocalWebTests.request
    success = web_tests.LocalWebTests.success
    wait = web_tests.LocalWebTests.wait

    def setUp(self):
        web_tests.LocalWebTests.setUp(self)
        self.fixture = self.base / "bitcoin-output-api.json"
        save_json(self.fixture, {"/tx/" + BTC: bitcoin()})
        self.route = "/api/tools/pegin-matches/outputs"

    def test_public_output_worker_returns_exact_values_without_credentials_or_case(self):
        def offline(request, result, live=False):
            self.assertFalse(live, "Public output loading must bypass the credential provider")
            payload = read_json(request)
            self.assertNotIn("terminal_handoff", payload)
            self.assertEqual(payload["arguments"], ["inspect-txs", "--txids", BTC, "--blockchain", "bitcoin",
                                                   "--auth", "none", "--base-url", "https://blockstream.info/api"])
            payload["arguments"] += ["--fixture", str(self.fixture)]
            save_json(request, payload)
            return web_tests.worker_command(request, result, live=False)
        credential_lock = Mock(acquire=Mock(side_effect=AssertionError("Public output loading must not unlock credentials")))
        with patch("liquid_tracer.web.worker_command", side_effect=offline), \
                patch.object(self.server, "credential_lock", credential_lock):
            job = self.success(self.route, {"txid": BTC}, 202)
            self.assertEqual(job["action"], "pegin-outputs")
            self.assertIsNone(job["case_id"])
            self.assertTrue(job["live"])
            self.assertTrue(job["cancellable"])
            result = self.wait(job)
        credential_lock.acquire.assert_not_called()
        self.assertEqual(result["blockchain"], "bitcoin")
        transaction, = result["transactions"]
        self.assertEqual(transaction["txid"], BTC)
        self.assertEqual(transaction["outputs"][1]["value_text"], "60000000")
        self.assertEqual(transaction["outputs"][1]["outpoint"], BTC + ":1")
        self.assertEqual(self.success("/api/session")["cases"], [])
        self.assertEqual(self.success("/api/tools/pegin-matches"), {"reports": []})

    def test_enterprise_output_worker_preserves_credential_routing(self):
        def offline(request, result, live=False):
            self.assertTrue(live)
            payload = read_json(request)
            self.assertTrue(payload.pop("terminal_handoff"))
            self.assertIsInstance(payload.pop("server_pid"), int)
            self.assertEqual(payload["arguments"], ["inspect-txs", "--txids", BTC, "--blockchain", "bitcoin"])
            payload["arguments"] += ["--fixture", str(self.fixture)]
            save_json(request, payload)
            return web_tests.worker_command(request, result, live=False)
        gate = Mock(wraps=threading.Lock())
        with patch("liquid_tracer.web.worker_command", side_effect=offline), \
                patch("liquid_tracer.web.handoff_terminal", return_value=None) as handoff, \
                patch.object(self.server, "credential_lock", gate):
            job = self.success(self.route, {"txid": BTC, "public": False}, 202)
            result = self.wait(job)
        self.assertEqual(result["blockchain"], "bitcoin")
        gate.acquire.assert_called_once()
        gate.release.assert_called_once()
        self.assertTrue(handoff.call_args.args[1])

    def test_output_admission_is_prompt_and_worker_job_remains_cancellable(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(identity, *args, **kwargs):
            entered.set()
            release.wait(5)
        self.addCleanup(release.set)
        with patch.object(self.server, "run_job", side_effect=blocked):
            start = time.monotonic()
            job = self.success(self.route, {"txid": BTC}, 202)
            self.assertLess(time.monotonic() - start, 1)
            self.assertTrue(entered.wait(1))
            start = time.monotonic()
            self.assertTrue(any(item["id"] == job["id"] for item in self.success("/api/jobs")["jobs"]))
            self.assertLess(time.monotonic() - start, 1)
            canceled = self.success("/api/jobs/" + job["id"] + "/cancel", {}, 202)
            self.assertEqual(canceled["status"], "cancelling")
            release.set()

    def test_output_route_rejects_browser_paths_credentials_scope_and_invalid_txids(self):
        invalid = [{}, {"txid": "not-a-hash"}, {"txid": [BTC]}, {"txid": BTC, "public": "false"},
                   *({"txid": BTC, key: value} for key, value in (
                       ("case_id", "a" * 32), ("fixture", str(self.fixture)), ("base_url", "https://example.com"),
                       ("auth", "none"), ("client_secret", "not-accepted"), ("blockchain", "liquid"),
                       ("action", "lookup"), ("max_seconds", 3600)))]
        with patch.object(self.server, "start_job") as start:
            for body in invalid:
                with self.subTest(body=body):
                    self.assertEqual(self.request(self.route, body)[0], 400)
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
