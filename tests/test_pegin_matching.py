"""Peg-in discovery is bounded, resumable and uses exact cross-chain references."""
from copy import deepcopy
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import LBTC, TraceError, save_json
from liquid_tracer.investigations import create_investigation, read_case
from liquid_tracer.pegin_matching import (HISTORY, match_pegin, query, read_report, list_reports,
                                           verified_file, _index, _page)
from liquid_tracer.pegin_api import start_match
from liquid_tracer.job_resources import job_resources, conflicts

BTC, CLAIM, OTHER = ("a" * 64, "b" * 64, "c" * 64)
STATUS = {"confirmed": True, "block_height": 42, "block_hash": "d" * 64, "block_time": 1700000000}


def output(address="synthetic-liquid", value=None, asset=None):
    result = {"scriptpubkey": "0014" + "11" * 20, "scriptpubkey_type": "v0_p2wpkh",
              "scriptpubkey_address": address}
    if value is not None:
        result["value"] = value
    if asset is not None:
        result["asset"] = asset
    return result


def claim(txid=CLAIM, bitcoin=BTC, vout=1):
    return {"txid": txid, "vin": [{"txid": bitcoin, "vout": vout, "is_pegin": True}],
            "vout": [output(), output("second", 123, LBTC),
                     {"scriptpubkey": "", "scriptpubkey_type": "fee", "value": 100, "asset": LBTC}],
            "status": dict(STATUS)}


def bitcoin():
    return {"txid": BTC, "vin": [], "vout": [output("change", 30_000), output("deposit", 60_000_000)],
            "status": dict(STATUS)}


class PeginMatchingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.case = create_investigation(self.root / "cases", "Bitcoin case", blockchain="bitcoin")
        self.btc_fixture, self.liquid_fixture = self.root / "btc.json", self.root / "liquid.json"
        self.data = {"/tx/" + CLAIM: claim(), HISTORY: [claim()], HISTORY + "/" + CLAIM: []}
        self.write_fixtures()

    def write_fixtures(self, btc=None):
        save_json(self.btc_fixture, {"/tx/" + BTC: btc or bitcoin()})
        save_json(self.liquid_fixture, self.data)

    def run_match(self, **kwargs):
        return match_pegin(self.case, BTC, bitcoin_fixture=self.btc_fixture,
                           liquid_fixture=self.liquid_fixture, **kwargs)

    def test_exact_reference_and_output_list_are_saved_without_value_allocation(self):
        result = self.run_match(vout=1, liquid_txid=CLAIM)
        self.assertEqual(result["status"], "matched")
        item, = result["matches"]
        self.assertEqual((item["bitcoin_outpoint"], item["liquid_txid"], item["liquid_vin"]),
                         (BTC + ":1", CLAIM, 0))
        self.assertEqual(item["bitcoin_value_sats"], 60_000_000)
        self.assertEqual(item["verification"], "confirmed_reference")
        self.assertIsNone(result["liquid_outputs"][CLAIM][0]["value"])
        self.assertEqual(len(result["liquid_outputs"][CLAIM]), 3)
        self.assertIn("not established", result["notice"])
        self.assertEqual(read_report(self.case, result["id"]), result)
        matches = list(csv.DictReader(io.StringIO(verified_file(self.case, result["id"], "matches.csv").read_text())))
        outputs = list(csv.DictReader(io.StringIO(verified_file(self.case, result["id"], "liquid-outputs.csv").read_text())))
        self.assertEqual(len(matches), 1)
        self.assertEqual(len(outputs), 3)
        self.assertTrue(all(row["Allocation"] == "Not determined" for row in outputs))
        self.assertEqual(outputs[0]["Public value (base units)"], "")
        evidence = json.loads(verified_file(self.case, result["id"], "evidence.json").read_text())
        self.assertEqual(evidence[CLAIM]["response"], self.data["/tx/" + CLAIM])
        self.assertEqual(list_reports(self.case)[0]["match_count"], 1)

    def test_wrong_vout_and_non_pegin_do_not_match_even_when_amounts_or_hash_agree(self):
        result = self.run_match(vout=0, liquid_txid=CLAIM)
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["stop_reason"], "candidate_does_not_reference_deposit")
        self.data["/tx/" + CLAIM]["vin"][0]["is_pegin"] = False
        self.write_fixtures()
        result = self.run_match(liquid_txid=CLAIM)
        self.assertFalse(result["matches"])
        self.assertFalse(result["liquid_outputs"])

    def test_unconfirmed_claim_or_deposit_is_never_reported_confirmed(self):
        for side in ("liquid", "bitcoin"):
            with self.subTest(side=side):
                self.data["/tx/" + CLAIM] = claim()
                btc = bitcoin()
                (self.data["/tx/" + CLAIM] if side == "liquid" else btc)["status"] = {"confirmed": False}
                self.write_fixtures(btc)
                result = self.run_match(liquid_txid=CLAIM)
                self.assertEqual(result["matches"][0]["verification"], "unconfirmed_reference")

    def test_one_claim_can_reference_multiple_bitcoin_outputs_and_deduplicates_output_export(self):
        self.data["/tx/" + CLAIM]["vin"].append({"txid": BTC, "vout": 0, "is_pegin": True})
        self.write_fixtures()
        result = self.run_match(liquid_txid=CLAIM)
        self.assertEqual({m["bitcoin_vout"] for m in result["matches"]}, {0, 1})
        self.assertEqual(len(result["liquid_outputs"]), 1)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(verified_file(
            self.case, result["id"], "liquid-outputs.csv").read_text())))), 3)

    def test_history_checkpoint_resumes_and_shared_index_serves_other_cases(self):
        recent = claim(OTHER, "e" * 64)
        self.data = {HISTORY: [recent], HISTORY + "/" + OTHER: [claim()],
                     HISTORY + "/" + CLAIM: [], "/tx/" + CLAIM: claim()}
        self.write_fixtures()
        first = self.run_match(vout=1, max_pages=1)
        self.assertEqual((first["status"], first["stop_reason"]), ("paused", "page_limit"))
        self.assertEqual(first["coverage"]["history_cursor"], OTHER)
        second = self.run_match(vout=1, max_pages=2)
        self.assertEqual(second["status"], "matched")
        self.assertEqual(second["coverage"]["pages_this_task"], 2)
        self.assertEqual(second["coverage"]["history_pages_processed"], 3)
        oldcase = self.case
        self.case = create_investigation(self.root / "cases", "Second", blockchain="bitcoin")
        third = self.run_match(vout=1)
        self.assertEqual(third["status"], "matched")
        self.assertEqual(third["coverage"]["pages_this_task"], 0)
        self.assertEqual(len(list_reports(oldcase)), 2)
        self.assertEqual(len(list_reports(self.case)), 1)

    def test_one_page_continuations_eventually_advance_to_older_claims(self):
        self.data = {HISTORY: [claim(OTHER, "e" * 64)], HISTORY + "/" + OTHER: [claim()],
                     HISTORY + "/" + CLAIM: [], "/tx/" + CLAIM: claim()}
        self.write_fixtures()
        results = [self.run_match(max_pages=1) for _ in range(3)]
        self.assertEqual([r["status"] for r in results], ["paused", "paused", "matched"])
        self.assertTrue(all(r["coverage"]["pages_this_task"] == 1 for r in results))

    def test_empty_history_is_refreshed_on_a_later_search(self):
        from liquid_tracer.pegin_matching import _history_page
        self.data[HISTORY] = []
        self.write_fixtures()
        self.assertEqual(self.run_match()["stop_reason"], "history_exhausted")
        original = _history_page
        def growing(api, endpoint):
            return ([claim()], 1) if endpoint == HISTORY else original(api, endpoint)
        with patch("liquid_tracer.pegin_matching._history_page", growing):
            self.assertEqual(self.run_match()["status"], "matched")

    def test_budget_and_cancellation_preserve_previously_verified_claims(self):
        from liquid_tracer.api import Esplora
        self.data[HISTORY] = [claim(), claim(OTHER, vout=0)]
        self.data["/tx/" + OTHER] = claim(OTHER, vout=0)
        self.write_fixtures()
        # Bitcoin, one history page, then the first claim. The second exceeds the budget.
        result = self.run_match(max_requests=3)
        self.assertEqual(result["status"], "paused")
        self.assertEqual([m["liquid_txid"] for m in result["matches"]], [CLAIM])
        self.assertIn(CLAIM, result["liquid_outputs"])
        original = Esplora.get
        def interrupted(api, endpoint):
            if api.blockchain == "liquid" and endpoint == "/tx/" + OTHER:
                raise KeyboardInterrupt
            return original(api, endpoint)
        with patch.object(Esplora, "get", interrupted), self.assertRaises(KeyboardInterrupt):
            self.run_match()
        saved = read_report(self.case, list_reports(self.case)[0]["id"])
        self.assertEqual(saved["status"], "cancelled")
        self.assertEqual([m["liquid_txid"] for m in saved["matches"]], [CLAIM])
        final = self.run_match()
        self.assertEqual({m["liquid_txid"] for m in final["matches"]}, {CLAIM, OTHER})

    def test_direct_verification_populates_shared_index_and_rejects_malformed_flags(self):
        self.run_match(liquid_txid=CLAIM)
        self.assertEqual(self.run_match()["coverage"]["pages_this_task"], 0)
        self.data["/tx/" + CLAIM]["vin"][0]["is_pegin"] = 1
        self.write_fixtures()
        with self.assertRaisesRegex(TraceError, "peg-in flag"):
            self.run_match(liquid_txid=CLAIM)
        self.assertEqual(list_reports(self.case)[0]["status"], "error")

    def test_cached_index_hint_is_reverified_instead_of_trusted(self):
        self.run_match()
        from liquid_tracer.api import Esplora
        original = Esplora.get
        def altered(api, endpoint):
            value, oid = original(api, endpoint)
            if api.blockchain == "liquid" and endpoint == "/tx/" + CLAIM:
                value = deepcopy(value)
                value["vin"][0]["is_pegin"] = False
            return value, oid
        with patch.object(Esplora, "get", altered):
            result = self.run_match(max_pages=2)
        self.assertFalse(result["matches"])

    def test_budget_stop_remains_incomplete_and_malformed_candidate_is_rejected(self):
        result = self.run_match(max_requests=1)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["stop_reason"], "request_limit")
        self.assertFalse(result["matches"])
        self.data["/tx/" + CLAIM]["vin"][0]["vout"] = 99
        self.write_fixtures()
        with self.assertRaisesRegex(TraceError, "missing Bitcoin"):
            self.run_match(liquid_txid=CLAIM)
        self.assertEqual(list_reports(self.case)[0]["status"], "error")

    def test_cancellation_saves_checkpoint_and_report_without_losing_completed_pages(self):
        original = _page
        def interrupt(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt
        with patch("liquid_tracer.pegin_matching._page", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_match()
        cancelled = list_reports(self.case)[0]
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["coverage"]["history_pages_processed"], 1)
        result = self.run_match()
        self.assertEqual(result["status"], "matched")

    def test_incomplete_refresh_resumes_before_older_history(self):
        # Exercise new pages since the previous search without changing source.
        from liquid_tracer.pegin_matching import _history_page
        self.data = {HISTORY: [claim(OTHER, "e" * 64)], HISTORY + "/" + OTHER: []}
        self.write_fixtures()
        self.run_match(max_pages=1)
        original = _history_page
        newhead = "f" * 64
        def growing(api, endpoint):
            if api.blockchain == "liquid" and endpoint == HISTORY:
                return [claim(newhead, "e" * 64)], 1
            if endpoint == HISTORY + "/" + newhead:
                return [claim(OTHER, "e" * 64)], 1
            return original(api, endpoint)
        with patch("liquid_tracer.pegin_matching._history_page", growing):
            paused = self.run_match(max_pages=1)
            self.assertTrue(paused["coverage"]["refresh_pending"])
            resumed = self.run_match(max_pages=2)
        self.assertFalse(resumed["coverage"]["refresh_pending"])
        self.assertEqual(resumed["stop_reason"], "history_exhausted")

    def test_workspace_matches_reuse_index_without_creating_or_requiring_an_investigation(self):
        import shutil
        self.run_match(liquid_txid=CLAIM)
        workspace = self.case.parent
        before = sorted(workspace.glob("*/case.json"))
        result = match_pegin(None, BTC, workspace=workspace, vout=1,
                             bitcoin_fixture=self.btc_fixture, liquid_fixture=self.liquid_fixture)
        self.assertIsNone(result["case_id"])
        self.assertEqual(result["scope"], "workspace")
        self.assertEqual(result["coverage"]["pages_this_task"], 0)
        self.assertEqual(sorted(workspace.glob("*/case.json")), before)
        self.assertEqual(list_reports(workspace=workspace)[0]["id"], result["id"])
        shutil.rmtree(self.case)  # Only this test's temporary investigation.
        self.assertEqual(read_report(None, result["id"], workspace=workspace), result)
        self.assertTrue(verified_file(None, result["id"], "matches.csv", workspace=workspace).is_file())

    def test_workspace_and_legacy_jobs_share_only_the_index_reservation(self):
        workspace = self.case.parent
        resource = job_resources(["pegin-match", "--investigations-dir", str(workspace)], "pegin-match")
        legacy = job_resources(["pegin-match", "--case", str(self.case)], "pegin-match", self.case)
        self.assertEqual(resource["resource_key"], legacy["resource_key"])
        self.assertTrue(conflicts({"case_id": None, **resource}, {"case_id": "one", **legacy}))
        self.assertFalse(conflicts({"case_id": None, **resource},
                                   {"case_id": "one", "resource_kind": "investigation_delete"}))
        with self.assertRaisesRegex(TraceError, "not both"):
            match_pegin(self.case, BTC, workspace=workspace)

    def test_cli_works_in_empty_workspace_and_does_not_inherit_case_environment(self):
        import contextlib
        from liquid_tracer.cli import main
        workspace = self.root / "empty-workspace"
        stdout = io.StringIO()
        with patch.dict("os.environ", {"LIQUID_CASE_DIR": str(self.case)}), contextlib.redirect_stdout(stdout):
            status = main(["pegin-match", "--investigations-dir", str(workspace), "--txid", BTC,
                           "--liquid-txid", CLAIM, "--bitcoin-fixture", str(self.btc_fixture),
                           "--liquid-fixture", str(self.liquid_fixture)], progress=lambda _: None)
        self.assertEqual(status, 0)
        result = json.loads(stdout.getvalue())
        self.assertIsNone(result["case_id"])
        self.assertEqual(result["status"], "matched")
        self.assertEqual(list(workspace.glob("*/case.json")), [])

    def test_inputs_and_limits_rejected_before_network_or_index_writes(self):
        invalid = [dict(vout=True), dict(vout=-1), dict(max_pages=True), dict(max_pages=0),
                   dict(max_seconds=float("nan")), dict(max_requests=0), dict(liquid_txid="../../")]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(TraceError):
                self.run_match(**kwargs)
        with self.assertRaises(TraceError):
            query("bad")
        self.assertFalse((self.case.parent / ".pegin-index").exists())
        liquid_case = create_investigation(self.root / "cases", "Liquid")
        with self.assertRaisesRegex(TraceError, "Bitcoin investigation"):
            match_pegin(liquid_case, BTC)

    def test_saved_download_integrity_and_symlinks_are_checked(self):
        result = self.run_match(liquid_txid=CLAIM)
        path = verified_file(self.case, result["id"], "matches.csv")
        path.write_text("altered")
        with self.assertRaisesRegex(TraceError, "checksums"):
            read_report(self.case, result["id"])
        with self.assertRaises(TraceError):
            verified_file(self.case, "../", "matches.csv")
        result = self.run_match(liquid_txid=CLAIM)
        path = verified_file(self.case, result["id"], "matches.csv")
        path.unlink()
        path.symlink_to(self.btc_fixture)
        with self.assertRaisesRegex(TraceError, "symbolic"):
            verified_file(self.case, result["id"], "matches.csv")

    def test_shared_index_lock_blocks_conflicting_workers_but_not_plot_admission(self):
        folder = self.root / "index"
        with _index(folder):
            with self.assertRaisesRegex(TraceError, "Another"):
                with _index(folder):
                    pass
        resource = job_resources(["pegin-match", "--case", str(self.case)], "pegin-match", self.case)
        first = {"case_id": "first", **resource}
        self.assertTrue(conflicts(first, {"case_id": "second", **resource}))
        self.assertFalse(conflicts(first, {"case_id": "first", "resource_kind": "plot"}))
        self.assertTrue(conflicts(first, {"case_id": "first", "resource_kind": "investigation_delete"}))

    def test_action_registers_worker_arguments_without_doing_validation_io(self):
        class Server:
            def start_job(self, args, **kwargs):
                return args, kwargs
        with patch("liquid_tracer.pegin_matching.match_pegin", side_effect=AssertionError("worker only")):
            args, options = start_match(Server(), self.case, read_case(self.case),
                {"action": "pegin-match", "txid": BTC, "vout": 1, "max_pages": 20})
        self.assertEqual(args[0], "pegin-match")
        self.assertEqual(args[-2:], ["--vout", "1"])
        self.assertTrue(options["live"])
        with self.assertRaises(TraceError):
            start_match(Server(), self.case, read_case(self.case),
                {"action": "pegin-match", "txid": BTC, "max_pages": 20, "base_url": "https://untrusted"})


if __name__ == "__main__":
    unittest.main()
