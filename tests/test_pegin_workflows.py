"""Synthetic multi-output, batch, and reverse peg-in workflows."""
import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.pegin_matching import (HISTORY, batch_query, list_reports, match_liquid_pegin,
    match_pegin, match_pegin_batch, query, read_report, reverse_query, verified_file)
from tests.test_pegin_matching import BTC, CLAIM, OTHER, bitcoin, claim

BTC2, BTC3, CLAIM2 = "e" * 64, "f" * 64, "1" * 64


def btc_transaction(txid):
    value = bitcoin()
    value["txid"] = txid
    return value


class PeginWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = self.root / "cases"
        self.bitcoin_fixture, self.liquid_fixture = self.root / "btc.json", self.root / "liquid.json"
        self.options = dict(bitcoin_fixture=self.bitcoin_fixture, liquid_fixture=self.liquid_fixture, public=True)
        self.btc = {"/tx/" + txid: btc_transaction(txid) for txid in (BTC, BTC2, BTC3)}
        self.liquid = {}

    def fixtures(self):
        save_json(self.bitcoin_fixture, self.btc)
        save_json(self.liquid_fixture, self.liquid)

    def batch_fixture(self):
        first, second = claim(), claim(CLAIM2, BTC2)
        self.liquid = {"/tx/" + CLAIM: first, "/tx/" + CLAIM2: second, HISTORY: [first, second],
                       HISTORY + "/" + CLAIM2: []}
        self.fixtures()

    def rows(self, report):
        path = verified_file(None, report["id"], "matches.csv", workspace=self.workspace)
        return list(csv.DictReader(io.StringIO(path.read_text())))

    def test_multiple_selected_outputs_continue_past_first_matching_page(self):
        first, second = claim(vout=0), claim(CLAIM2, vout=1)
        self.liquid = {HISTORY: [first], HISTORY + "/" + CLAIM: [second],
                       "/tx/" + CLAIM: first, "/tx/" + CLAIM2: second}
        self.fixtures()
        report = match_pegin(None, BTC, workspace=self.workspace, vouts=[1, 0, 1], **self.options)
        self.assertEqual(report["status"], "matched")
        self.assertEqual(report["query"]["bitcoin_vouts"], [0, 1])
        self.assertEqual({item["bitcoin_vout"] for item in report["matches"]}, {0, 1})
        self.assertEqual(report["coverage"]["pages_this_task"], 2)
        self.assertEqual(report["coverage"]["unmatched_vouts"], [])

    def test_partial_selection_does_not_claim_all_outputs_matched(self):
        first = claim(vout=0)
        self.liquid = {HISTORY: [first], HISTORY + "/" + CLAIM: [], "/tx/" + CLAIM: first}
        self.fixtures()
        report = match_pegin(None, BTC, workspace=self.workspace, vouts=[0, 1], **self.options)
        self.assertEqual(report["status"], "partial")
        self.assertFalse(report["coverage"]["requested_outpoint_matched"])
        self.assertEqual(report["coverage"]["unmatched_vouts"], [1])

    def test_empty_selection_means_any_and_legacy_vout_still_selects_one(self):
        self.liquid = {"/tx/" + CLAIM: claim()}
        self.fixtures()
        any_report = match_pegin(None, BTC, workspace=self.workspace, vouts=[], liquid_txid=CLAIM, **self.options)
        self.assertEqual(any_report["status"], "matched")
        selected = match_pegin(None, BTC, workspace=self.workspace, vout=0, liquid_txid=CLAIM, **self.options)
        self.assertEqual(selected["status"], "no_match")

    def test_batch_deduplicates_and_reuses_discovery_with_one_page_budget(self):
        self.batch_fixture()
        report = match_pegin_batch(self.workspace, [BTC, BTC2, BTC.upper()], max_pages=1, **self.options)
        self.assertEqual(report["status"], "matched")
        self.assertEqual(report["query"]["bitcoin_txids"], [BTC, BTC2])
        self.assertEqual(report["query"]["mode"], "batch")
        self.assertEqual(set(report["bitcoins"]), {BTC, BTC2})
        self.assertEqual(report["coverage"]["pages_this_task"], 1)
        self.assertEqual(report["coverage"]["requests_this_task"], 5)
        self.assertEqual([row["Status"] for row in self.rows(report)], ["matched", "matched"])
        self.assertEqual({row["Liquid transaction"] for row in self.rows(report)}, {CLAIM, CLAIM2})
        self.assertEqual(read_report(None, report["id"], workspace=self.workspace), report)
        self.assertEqual(len(list_reports(workspace=self.workspace)), 1)

    def test_batch_request_limit_saves_matches_paused_and_unprocessed_rows(self):
        self.batch_fixture()
        report = match_pegin_batch(self.workspace, [BTC, BTC2, BTC3], max_requests=4, **self.options)
        self.assertEqual(report["status"], "paused")
        self.assertEqual(report["stop_reason"], "request_limit")
        self.assertEqual(report["coverage"]["requests_this_task"], 4)
        self.assertEqual([row["status"] for row in report["results"]], ["matched", "paused", "unprocessed"])
        self.assertEqual([row["Status"] for row in self.rows(report)], ["matched", "paused", "unprocessed"])
        self.assertEqual({row["Bitcoin transaction"] for row in self.rows(report)}, {BTC, BTC2, BTC3})

    def test_batch_page_limit_is_shared_and_never_omits_unprocessed_hashes(self):
        first = claim()
        self.liquid = {HISTORY: [first], "/tx/" + CLAIM: first}
        self.fixtures()
        report = match_pegin_batch(self.workspace, [BTC, BTC2, BTC3], max_pages=1, **self.options)
        self.assertEqual(report["coverage"]["pages_this_task"], 1)
        self.assertEqual(report["stop_reason"], "page_limit")
        self.assertEqual([row["status"] for row in report["results"]], ["matched", "paused", "unprocessed"])

    def test_batch_errors_continue_and_are_exported(self):
        self.batch_fixture()
        self.btc["/tx/" + BTC2]["vout"] = "bad explorer response"
        self.fixtures()
        report = match_pegin_batch(self.workspace, [BTC2, BTC], **self.options)
        self.assertEqual(report["status"], "partial")
        self.assertEqual([row["status"] for row in report["results"]], ["error", "matched"])
        self.assertTrue(any(row["Status"] == "error" and row["Bitcoin transaction"] == BTC2 for row in self.rows(report)))

    def test_batch_cancellation_preserves_completed_and_unprocessed_rows(self):
        self.batch_fixture()
        starts = 0
        def cancel_second(message):
            nonlocal starts
            if message.get("phase") == "pegin_bitcoin":
                starts += 1
                if starts == 2:
                    raise KeyboardInterrupt
        # Progress callbacks are message dictionaries in the shared worker protocol.
        with self.assertRaises(KeyboardInterrupt):
            match_pegin_batch(self.workspace, [BTC, BTC2, BTC3], progress=cancel_second, **self.options)
        saved, = list_reports(workspace=self.workspace)
        report = read_report(None, saved["id"], workspace=self.workspace)
        self.assertEqual(report["status"], "cancelled")
        self.assertEqual([row["status"] for row in report["results"]], ["matched", "cancelled", "unprocessed"])
        self.assertEqual(len(self.rows(report)), 3)

    def test_batch_continuation_skips_completed_rows_and_preserves_observation_dates(self):
        self.batch_fixture()
        previous = match_pegin_batch(self.workspace, [BTC, BTC2], max_requests=4, **self.options)
        original_match = previous["matches"][0].copy()
        resumed = match_pegin_batch(self.workspace, [BTC, BTC2], max_requests=2,
                                   resume_report=previous["id"], **self.options)
        self.assertEqual(resumed["status"], "matched")
        self.assertNotEqual(resumed["id"], previous["id"])
        self.assertEqual(resumed["coverage"]["requests_this_task"], 2)
        self.assertEqual(resumed["matches"][0], original_match)
        self.assertEqual(resumed["results"][0]["checked_at"], previous["results"][0]["checked_at"])
        self.assertEqual(resumed["results"][0]["carried_from_report"], previous["id"])
        self.assertEqual(resumed["bitcoins"][BTC], previous["bitcoins"][BTC])
        self.assertEqual(read_report(None, previous["id"], workspace=self.workspace), previous)

    def test_batch_continuation_rejects_changed_hashes_window_and_unverified_files(self):
        self.batch_fixture()
        previous = match_pegin_batch(self.workspace, [BTC, BTC2], max_requests=4, **self.options)
        for hashes, options in (([BTC2, BTC], {}), ([BTC, BTC2], {"start_minutes": -5, "end_minutes": 15})):
            with self.subTest(hashes=hashes, options=options), self.assertRaises(TraceError):
                match_pegin_batch(self.workspace, hashes, resume_report=previous["id"], **options, **self.options)
        path = verified_file(None, previous["id"], "matches.csv", workspace=self.workspace)
        path.write_text("altered")
        with self.assertRaisesRegex(TraceError, "checksums"):
            match_pegin_batch(self.workspace, [BTC, BTC2], resume_report=previous["id"], **self.options)

    def test_batch_continuation_accepts_equivalent_legacy_hour_and_minute_windows(self):
        self.batch_fixture()
        for btc, liquid in ((BTC, CLAIM), (BTC2, CLAIM2)):
            match_pegin(None, btc, workspace=self.workspace, liquid_txid=liquid, **self.options)
        previous = match_pegin_batch(self.workspace, [BTC, BTC2], start_hours=-.5, end_hours=72, **self.options)
        self.assertEqual(previous["status"], "matched")
        resumed = match_pegin_batch(self.workspace, [BTC, BTC2], resume_report=previous["id"],
                                   start_minutes=-30, end_minutes=4320, **self.options)
        self.assertEqual(resumed["status"], "matched")
        self.assertEqual(resumed["coverage"]["requests_this_task"], 0)
        self.assertTrue(all(row["carried_from_report"] == previous["id"] for row in resumed["results"]))

    def test_batch_continuation_retains_each_shared_claim_verification_observation(self):
        liquid = claim()
        liquid["vin"].append({"is_pegin": True, "txid": BTC2, "vout": 0})
        self.liquid = {HISTORY: [liquid], "/tx/" + CLAIM: liquid}
        self.fixtures()
        previous = match_pegin_batch(self.workspace, [BTC, BTC2], max_requests=3, **self.options)
        self.assertEqual(previous["status"], "paused")
        resumed = match_pegin_batch(self.workspace, [BTC, BTC2], max_requests=2,
                                   resume_report=previous["id"], **self.options)
        self.assertEqual(resumed["status"], "matched")
        first, second = (item["observation_id"] for item in resumed["matches"])
        self.assertNotEqual(first, second)
        evidence = read_json(verified_file(None, resumed["id"], "evidence.json", workspace=self.workspace))
        self.assertEqual(set(evidence["liquid_checks"]), {str(first), str(second)})
        self.assertEqual(evidence["liquid_checks"][str(first)]["observation_id"], previous["matches"][0]["observation_id"])
        self.assertEqual(resumed["liquid_output_observation_ids"][CLAIM], second)

    def test_batch_time_budget_does_not_restart_for_each_transaction(self):
        self.batch_fixture()
        clock = [0.0]
        def advance_after_first(message):
            if message.get("phase") == "pegin_batch" and message.get("completed") == 1:
                clock[0] = 301.0
        with patch("liquid_tracer.api.time.monotonic", side_effect=lambda: clock[0]):
            report = match_pegin_batch(self.workspace, [BTC, BTC2, BTC3], max_seconds=300,
                                      progress=advance_after_first, **self.options)
        self.assertEqual(report["stop_reason"], "time_limit")
        self.assertEqual([row["status"] for row in report["results"]], ["matched", "paused", "unprocessed"])
        self.assertEqual(report["coverage"]["requests_this_task"], 3)

    def test_reverse_verifies_multiple_bitcoin_transactions_without_history(self):
        liquid = claim()
        liquid["vin"].append({"is_pegin": True, "txid": BTC2, "vout": 0})
        self.liquid = {"/tx/" + CLAIM: liquid}
        self.fixtures()
        report = match_liquid_pegin(self.workspace, CLAIM, **self.options)
        self.assertEqual(report["status"], "matched")
        self.assertEqual(report["query"]["bitcoin_txids"], [BTC, BTC2])
        self.assertEqual({(row["bitcoin_txid"], row["liquid_vin"]) for row in report["matches"]}, {(BTC, 0), (BTC2, 1)})
        self.assertEqual(report["coverage"]["pages_this_task"], 0)
        self.assertEqual(report["coverage"]["requests_this_task"], 3)
        self.assertEqual(len(self.rows(report)), 2)

    def test_reverse_non_pegin_transaction_is_explicit_no_match(self):
        liquid = claim()
        liquid["vin"][0]["is_pegin"] = False
        self.liquid = {"/tx/" + CLAIM: liquid}
        self.fixtures()
        report = match_liquid_pegin(self.workspace, CLAIM, **self.options)
        self.assertEqual(report["status"], "no_match")
        self.assertEqual(report["stop_reason"], "liquid_transaction_has_no_pegin_inputs")
        self.assertEqual(report["coverage"]["requests_this_task"], 1)
        self.assertEqual(self.rows(report)[0]["Status"], "no_match")

    def test_reverse_invalid_outpoint_keeps_verified_other_inputs(self):
        liquid = claim()
        liquid["vin"].append({"is_pegin": True, "txid": BTC2, "vout": 999})
        self.liquid = {"/tx/" + CLAIM: liquid}
        self.fixtures()
        report = match_liquid_pegin(self.workspace, CLAIM, **self.options)
        self.assertEqual(report["status"], "partial")
        self.assertEqual([row["status"] for row in report["results"]], ["matched", "error"])
        self.assertEqual([row["Status"] for row in self.rows(report)], ["matched", "error"])

    def test_reverse_budget_stop_records_pending_references(self):
        liquid = claim()
        liquid["vin"].append({"is_pegin": True, "txid": BTC2, "vout": 0})
        liquid["vin"].append({"is_pegin": True, "txid": BTC3, "vout": 0})
        self.liquid = {"/tx/" + CLAIM: liquid}
        self.fixtures()
        report = match_liquid_pegin(self.workspace, CLAIM, max_requests=2, **self.options)
        self.assertEqual(report["status"], "paused")
        self.assertEqual([row["status"] for row in report["results"]], ["matched", "paused", "unprocessed"])
        self.assertEqual(report["coverage"]["requests_this_task"], 2)

    def test_reverse_cancellation_preserves_verified_and_unprocessed_inputs(self):
        liquid = claim()
        liquid["vin"] += [{"is_pegin": True, "txid": target, "vout": 0} for target in (BTC2, BTC3)]
        self.liquid = {"/tx/" + CLAIM: liquid}
        self.fixtures()
        starts = 0
        def cancel_second(message):
            nonlocal starts
            if message.get("phase") == "pegin_bitcoin":
                starts += 1
                if starts == 2:
                    raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            match_liquid_pegin(self.workspace, CLAIM, progress=cancel_second, **self.options)
        saved, = list_reports(workspace=self.workspace)
        report = read_report(None, saved["id"], workspace=self.workspace)
        self.assertEqual(report["status"], "cancelled")
        self.assertEqual([row["status"] for row in report["results"]], ["matched", "cancelled", "unprocessed"])
        self.assertEqual(len(self.rows(report)), 3)

    def test_reverse_changed_claim_records_no_match_and_final_checked_evidence(self):
        from liquid_tracer.api import Esplora
        from liquid_tracer.common import canonical
        self.liquid = {"/tx/" + CLAIM: claim()}
        self.fixtures()
        original = Esplora.get
        reads = 0
        def changed(api, endpoint, *args, **kwargs):
            nonlocal reads
            if api.blockchain == "liquid" and endpoint == "/tx/" + CLAIM:
                reads += 1
                if reads == 2:
                    body = claim()
                    body["vin"][0]["is_pegin"] = False
                    # Keep the real observation writer so evidence hashes describe the changed response.
                    api.budget.request()
                    oid = api.store.observe(api.run_id, api.base, endpoint, canonical(body))
                    return body, oid
            return original(api, endpoint, *args, **kwargs)
        with patch.object(Esplora, "get", changed):
            report = match_liquid_pegin(self.workspace, CLAIM, **self.options)
        self.assertEqual(report["status"], "no_match")
        self.assertEqual(report["results"][0]["status"], "no_match")
        self.assertEqual(report["results"][0]["match_count"], 0)
        self.assertEqual(report["matches"], [])
        evidence = read_json(verified_file(None, report["id"], "evidence.json", workspace=self.workspace))
        self.assertFalse(evidence["liquid"][CLAIM]["response"]["vin"][0]["is_pegin"])
        self.assertEqual(len(evidence["liquid_checks"]), 2)


class PeginWorkflowQueryTests(unittest.TestCase):
    def test_validation_rejects_invalid_selections_and_lists(self):
        for selection in (True, "1", [True], [-1], [1.5], [0x40000000]):
            with self.subTest(selection=selection), self.assertRaises(TraceError):
                query(BTC, vouts=selection)
        with self.assertRaises(TraceError):
            query(BTC, vout=0, vouts=[])
        for hashes in ([], BTC, [BTC] * 501, [BTC, "bad"], [None]):
            with self.subTest(hashes=hashes), self.assertRaises(TraceError):
                batch_query(hashes)
        for value in (None, "bad", [CLAIM]):
            with self.subTest(value=value), self.assertRaises(TraceError):
                reverse_query(value)


if __name__ == "__main__":
    unittest.main()
