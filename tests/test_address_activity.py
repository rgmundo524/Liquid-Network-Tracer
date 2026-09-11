import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.address_activity import inspect_address, validate_address
from liquid_tracer.api import Esplora, Limits
from liquid_tracer.common import StopRun, TraceError, save_json
from liquid_tracer.store import Store


ADDRESS = "SYNTHETIC-service-candidate"
BASE = "/address/" + ADDRESS


def stats(count=3, funded=6, spent=4, mempool_count=0, mempool_funded=0, mempool_spent=0):
    return {"address": ADDRESS,
            "chain_stats": {"tx_count": count, "funded_txo_count": funded, "spent_txo_count": spent},
            "mempool_stats": {"tx_count": mempool_count, "funded_txo_count": mempool_funded,
                              "spent_txo_count": mempool_spent}}


def transaction(index, stamp=None):
    return {"txid": f"{index:064x}", "status": {"confirmed": True, "block_height": index,
            "block_hash": f"{index + 1000:064x}", "block_time": 1_700_000_000 + index if stamp is None else stamp}}


def history(count):
    data = {BASE: stats(count=count, funded=count, spent=0)}
    transactions = [transaction(index) for index in range(count, 0, -1)]
    endpoint = BASE + "/txs/chain"
    for offset in range(0, count, 25):
        page = transactions[offset:offset + 25]
        data[endpoint] = page
        endpoint = BASE + "/txs/chain/" + page[-1]["txid"]
    if count and count % 25 == 0:
        data[endpoint] = []
    return data


class AddressActivityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(self.root / "case")
        self.addCleanup(self.store.close)
        self.client_counter = 0

    def api(self, data, *, max_requests=100, run_id=None):
        self.client_counter += 1
        fixture = self.root / f"fixture-{self.client_counter}.json"
        save_json(fixture, data)
        api = Esplora(self.store, run_id or f"address-review-{self.client_counter}",
                      Limits(max_requests=max_requests), fixture=fixture)
        self.addCleanup(api.close)
        return api

    def test_path_safe_validation_preserves_base58_case_and_normalizes_uppercase_bech32(self):
        for address in ("V" + "aB3" * 11, "ex1" + "q" * 39, "tex1" + "q" * 39,
                        "ert1" + "q" * 39, "lq1" + "q" * 96, "tlq1" + "q" * 96,
                        "el1" + "q" * 96, ADDRESS):
            self.assertEqual(validate_address(" " + address + " "), address)
        self.assertEqual(validate_address("EX1" + "Q" * 39), "ex1" + "q" * 39)
        for invalid in (None, 123, "", "a" * 201, "short", ADDRESS + "/utxo", ADDRESS + "?key=x",
                        ADDRESS + "#fragment", ADDRESS + ":0", "../" + ADDRESS, ADDRESS + "\nextra",
                        ADDRESS + "\x00", "秘密" * 15, "https://example.com/address"):
            with self.subTest(invalid=invalid), self.assertRaises(TraceError):
                validate_address(invalid)

    def test_invalid_input_or_page_budget_never_calls_api(self):
        api = self.api(history(3))
        with patch.object(api, "get") as get:
            for value in (0, -1, 1.5, "5", True, None):
                with self.subTest(value=value), self.assertRaises(TraceError):
                    inspect_address(api, ADDRESS, max_pages=value)
            with self.assertRaises(TraceError):
                inspect_address(api, "secret/invalid-input")
            get.assert_not_called()

    def test_complete_history_counts_and_dates_have_durable_observation_provenance(self):
        data = history(3)
        data[BASE] = stats(mempool_count=2, mempool_funded=3, mempool_spent=1)
        api = self.api(data)
        report = inspect_address(api, ADDRESS)
        self.assertEqual(report["confirmed_tx_count"], 3)
        self.assertEqual(report["mempool_tx_count"], 2)
        self.assertEqual(report["confirmed_unspent_output_count"], 2)
        self.assertEqual(report["mempool_unspent_output_delta"], 2)
        self.assertEqual(report["unspent_output_count"], 4)
        self.assertTrue(report["history_complete"])
        self.assertEqual(report["history_stop_reason"], "complete")
        self.assertEqual(report["history_pages"], 1)
        self.assertEqual(report["history_transactions_seen"], 3)
        self.assertEqual(report["first_confirmed_activity"]["txid"], transaction(1)["txid"])
        self.assertEqual(report["latest_confirmed_activity"]["txid"], transaction(3)["txid"])
        evidence = list(self.store.observations(report["observation_ids"]))
        self.assertEqual(report["observed_at"], evidence[0]["fetched_at"])
        self.assertEqual(evidence[1]["id"], report["first_confirmed_activity"]["observation_id"])
        self.assertEqual(json.loads(evidence[1]["body"]), data[BASE + "/txs/chain"])
        self.assertEqual(api.budget.requests, 2)
        self.assertIn("not address creation", report["timeframe_basis"])
        self.assertIn("not an atomic", report["snapshot_note"])
        self.assertNotIn("balance", report)
        self.assertFalse(any("/utxo" in row["endpoint"] for row in evidence))

    def test_default_page_budget_never_scans_whole_busy_address(self):
        api = self.api(history(140))
        report = inspect_address(api, ADDRESS)
        self.assertEqual(api.budget.requests, 6)
        self.assertEqual(report["history_pages"], 5)
        self.assertEqual(report["history_transactions_seen"], 125)
        self.assertEqual(report["confirmed_tx_count"], 140)
        self.assertFalse(report["history_complete"])
        self.assertEqual(report["history_stop_reason"], "page_limit")
        self.assertIsNone(report["first_confirmed_activity"])
        self.assertEqual(report["oldest_observed_confirmed_activity"]["txid"], transaction(16)["txid"])
        self.assertEqual(report["latest_confirmed_activity"]["txid"], transaction(140)["txid"])

    def test_exact_full_page_requires_terminal_page_for_complete_history(self):
        api = self.api(history(25))
        report = inspect_address(api, ADDRESS, max_pages=1)
        self.assertFalse(report["history_complete"])
        self.assertIsNone(report["first_confirmed_activity"])
        report = inspect_address(api, ADDRESS, max_pages=2)
        self.assertTrue(report["history_complete"])
        self.assertEqual(report["history_pages"], 2)
        # Reusing the same client does not duplicate earlier API requests.
        self.assertEqual(api.budget.requests, 3)
        self.assertEqual(len(list(self.store.observations(report["observation_ids"]))), 3)

    def test_request_budget_preserves_counts_and_partial_dates(self):
        api = self.api(history(70), max_requests=2)
        report = inspect_address(api, ADDRESS)
        self.assertEqual(report["history_stop_reason"], "request_limit")
        self.assertEqual(report["history_transactions_seen"], 25)
        self.assertFalse(report["history_complete"])
        self.assertEqual(report["confirmed_tx_count"], 70)
        self.assertIsNotNone(report["oldest_observed_confirmed_activity"])
        self.assertIsNone(report["first_confirmed_activity"])
        self.assertEqual(api.budget.requests, 2)

    def test_time_and_server_budget_stop_return_partial_summary_without_false_first_date(self):
        for reason in ("time_limit", "server_retry_later", "interrupted"):
            api = self.api(history(3))
            original_get = api.get
            def get(endpoint):
                if endpoint != BASE:
                    raise StopRun(reason)
                return original_get(endpoint)
            with self.subTest(reason=reason), patch.object(api, "get", side_effect=get):
                report = inspect_address(api, ADDRESS)
            self.assertEqual(report["history_stop_reason"], reason)
            self.assertEqual(report["confirmed_tx_count"], 3)
            self.assertIsNone(report["first_confirmed_activity"])
            self.assertIsNone(report["latest_confirmed_activity"])

    def test_failure_before_statistics_returns_no_fabricated_summary(self):
        api = self.api(history(3))
        with patch.object(api, "get", side_effect=StopRun("time_limit")), self.assertRaisesRegex(TraceError, "statistics lookup stopped"):
            inspect_address(api, ADDRESS)

    def test_empty_and_mempool_only_addresses_have_no_confirmed_timeframe(self):
        for mempool_count in (0, 2):
            data = {BASE: stats(count=0, funded=0, spent=0, mempool_count=mempool_count,
                               mempool_funded=mempool_count)}
            api = self.api(data)
            report = inspect_address(api, ADDRESS)
            self.assertEqual(api.budget.requests, 1)
            self.assertEqual(report["mempool_tx_count"], mempool_count)
            self.assertEqual(report["unspent_output_count"], mempool_count)
            self.assertTrue(report["history_complete"])
            self.assertEqual(report["history_stop_reason"], "no_confirmed_transactions")
            self.assertIsNone(report["first_confirmed_activity"])
            self.assertIsNone(report["latest_confirmed_activity"])

    def test_negative_mempool_delta_is_valid_and_not_a_negative_utxo_count(self):
        data = history(3)
        data[BASE] = stats(funded=10, spent=2, mempool_count=3, mempool_funded=1, mempool_spent=4)
        report = inspect_address(self.api(data), ADDRESS)
        self.assertEqual(report["mempool_unspent_output_delta"], -3)
        self.assertEqual(report["confirmed_unspent_output_count"], 8)
        self.assertEqual(report["unspent_output_count"], 5)
        self.assertTrue(report["output_counts_consistent"])

    def test_inconsistent_counts_never_report_negative_or_invented_output_balances(self):
        for snapshot in (stats(funded=1, spent=2), stats(funded=3, spent=2, mempool_spent=5)):
            data = history(3)
            data[BASE] = snapshot
            report = inspect_address(self.api(data), ADDRESS)
            self.assertIsNone(report["unspent_output_count"])
            self.assertFalse(report["output_counts_consistent"])
            self.assertTrue(report["warnings"])
            confirmed = report["confirmed_unspent_output_count"]
            self.assertTrue(confirmed is None or confirmed >= 0)

    def test_changed_transaction_count_marks_complete_page_as_inconsistent(self):
        for reported_count in (2, 4):
            data = history(3)
            data[BASE]["chain_stats"]["tx_count"] = reported_count
            report = inspect_address(self.api(data), ADDRESS)
            self.assertFalse(report["history_complete"])
            self.assertEqual(report["history_stop_reason"], "snapshot_inconsistent")
            self.assertIsNone(report["first_confirmed_activity"])
            self.assertIsNone(report["latest_confirmed_activity"])
            self.assertTrue(report["warnings"])

    def test_repeated_page_stops_and_deduplicates_transactions_without_repeating_request(self):
        data = history(70)
        first_page = data[BASE + "/txs/chain"]
        second_endpoint = BASE + "/txs/chain/" + first_page[-1]["txid"]
        data[second_endpoint] = first_page
        api = self.api(data)
        report = inspect_address(api, ADDRESS)
        self.assertEqual(api.budget.requests, 3)
        self.assertEqual(report["history_transactions_seen"], 25)
        self.assertEqual(report["history_stop_reason"], "repeated_history")
        self.assertFalse(report["history_complete"])
        self.assertIsNone(report["first_confirmed_activity"])

    def test_overlapping_and_reordered_history_are_never_claimed_complete(self):
        for transactions in ([transaction(3), transaction(2), transaction(2)],
                             [transaction(1), transaction(3), transaction(2)]):
            data = {BASE: stats(), BASE + "/txs/chain": transactions}
            report = inspect_address(self.api(data), ADDRESS)
            self.assertFalse(report["history_complete"])
            self.assertIsNone(report["first_confirmed_activity"])
            self.assertEqual(report["history_stop_reason"], "snapshot_inconsistent")

    def test_timestamp_order_does_not_override_block_order(self):
        data = {BASE: stats(count=2), BASE + "/txs/chain": [transaction(2, 1_700_000_000), transaction(1, 1_700_000_100)]}
        report = inspect_address(self.api(data), ADDRESS)
        self.assertTrue(report["history_complete"])
        self.assertEqual(report["latest_confirmed_activity"]["block_height"], 2)
        self.assertEqual(report["first_confirmed_activity"]["block_height"], 1)

    def test_missing_block_timestamp_stays_unknown_with_warning(self):
        data = history(3)
        data[BASE + "/txs/chain"][-1]["status"]["block_time"] = None
        report = inspect_address(self.api(data), ADDRESS)
        self.assertTrue(report["history_complete"])
        self.assertIsNone(report["first_confirmed_activity"]["date_utc"])
        self.assertTrue(any("timestamp" in warning for warning in report["warnings"]))

    def test_malformed_statistics_fail_without_echoing_untrusted_body(self):
        malformed = [None, [], {"address": "SYNTHETIC-wrong-address"}, {"address": ADDRESS},
                     {"address": ADDRESS, "chain_stats": [], "mempool_stats": {}}]
        for field in ("tx_count", "funded_txo_count", "spent_txo_count"):
            for value in (None, True, -1, 1.2, "SYNTHETIC-secret"):
                for section in ("chain_stats", "mempool_stats"):
                    sample = stats()
                    sample[section][field] = value
                    malformed.append(sample)
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(TraceError) as error:
                inspect_address(self.api({BASE: value}), ADDRESS)
            self.assertNotIn("SYNTHETIC-secret", str(error.exception))

    def test_malformed_confirmed_history_does_not_publish_dates(self):
        malformed = [None, {}, [None], [transaction(i) for i in range(26)]]
        for field, value in (("confirmed", False), ("confirmed", 1), ("block_height", True),
                             ("block_height", -1), ("block_hash", "not-a-hash"),
                             ("block_time", -1), ("block_time", 10 ** 100), ("block_time", 1.5)):
            sample = transaction(1)
            sample["status"][field] = value
            malformed.append([sample])
        malformed.extend([[{"txid": "bad-hash", "status": transaction(1)["status"]}],
                          [{"txid": transaction(1)["txid"], "status": None}]])
        for page in malformed:
            data = {BASE: stats(), BASE + "/txs/chain": page}
            with self.subTest(page=page), self.assertRaises(TraceError):
                inspect_address(self.api(data), ADDRESS)

    def test_address_endpoints_refresh_between_runs_and_cache_within_same_run(self):
        data = history(3)
        calls = []
        def transport(method, url, headers, body, timeout):
            endpoint = url.split("/liquid/api", 1)[1]
            calls.append(endpoint)
            return 200, {}, json.dumps(data[endpoint]).encode()
        def client(run_id):
            api = Esplora(self.store, run_id, Limits(), base="https://synthetic.example/liquid/api",
                          auth="none", transport=transport)
            self.addCleanup(api.close)
            return api
        first = client("first-review")
        report = inspect_address(first, ADDRESS)
        same = inspect_address(first, ADDRESS)
        self.assertEqual(report["observation_ids"], same["observation_ids"])
        self.assertEqual(calls, [BASE, BASE + "/txs/chain"])
        second = inspect_address(client("second-review"), ADDRESS)
        self.assertEqual(calls, [BASE, BASE + "/txs/chain"] * 2)
        self.assertNotEqual(report["observation_ids"], second["observation_ids"])


if __name__ == "__main__":
    unittest.main()
