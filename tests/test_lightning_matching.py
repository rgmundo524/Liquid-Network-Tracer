import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.api import Esplora
from liquid_tracer.common import StopRun, TraceError, save_json
from liquid_tracer.lightning_matching import (
    BITCOIN_API, MEMPOOL_API, NOTICE, _index_sample_report, _indexed_channels,
    _reconcile_index_samples, lookup_lightning, validate_lightning_report,
)

FUNDING = "a" * 64
CLOSING = "b" * 64
OTHER = "c" * 64
BLOCK = "d" * 64
SCRIPT = "0020" + "e" * 64
KEY_X = "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
HEIGHT = 800000


def channel(txid=FUNDING, vout=0, *, closing=None, height=HEIGHT, index=0, capacity=250000):
    return {"id": str((height << 40) | (index << 16) | vout),
            "short_id": f"{height}x{index}x{vout}", "capacity": capacity,
            "transaction_id": txid, "transaction_vout": vout,
            "closing_transaction_id": closing,
            "node_left": {"public_key": "02" + KEY_X, "alias": "Synthetic node <left>"},
            "node_right": {"public_key": "03" + KEY_X, "alias": "Synthetic node right"}}


def status(confirmed=True, height=HEIGHT):
    return ({"confirmed": True, "block_height": height, "block_hash": BLOCK, "block_time": 1700000000}
            if confirmed else {"confirmed": False})


def transaction(txid=FUNDING, *, vin=None, vout=None, confirmed=True, height=HEIGHT):
    return {"txid": txid,
            "vin": vin if vin is not None else [{"txid": OTHER, "vout": 0, "is_coinbase": False}],
            "vout": vout if vout is not None else [{"value": 250000, "scriptpubkey": SCRIPT}],
            "status": status(confirmed, height)}


class LightningMatchingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mempool_fixture = self.root / "mempool.json"
        self.bitcoin_fixture = self.root / "bitcoin.json"

    def lookup(self, *, txid=FUNDING, index=None, bitcoin=None, **kwargs):
        if index is None:
            index = [{"inputs": {}, "outputs": {"0": channel()}}]
        if bitcoin is None:
            bitcoin = {"/tx/" + FUNDING: transaction()}
        save_json(self.mempool_fixture, {"/lightning/channels/txids?txId[]=" + txid: index})
        save_json(self.bitcoin_fixture, bitcoin)
        return lookup_lightning(txid, mempool_fixture=self.mempool_fixture,
                                bitcoin_fixture=self.bitcoin_fixture, **kwargs)

    def closing_data(self, *, queried=CLOSING):
        previous = {"value": 250000, "scriptpubkey": SCRIPT}
        inputs = [{"txid": OTHER, "vout": 7}, {"txid": FUNDING, "vout": 0, "prevout": previous}]
        return ([{"inputs": {"0": channel(closing=queried)}, "outputs": {}}],
                {"/tx/" + queried: transaction(queried, vin=inputs, vout=[{"value": 245000, "scriptpubkey": "0014" + "f" * 40}]),
                 "/tx/" + FUNDING: transaction()})

    def test_funding_uses_exact_output_and_capacity_with_small_public_report(self):
        report = self.lookup()
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["classifications"], ["indexed_channel_funding"])
        match = report["matches"][0]
        self.assertEqual((match["funding_txid"], match["funding_vout"], match["capacity_sat"]), (FUNDING, 0, 250000))
        self.assertEqual(match["funding_scriptpubkey"], SCRIPT)
        self.assertIsNone(match["bitcoin_input_index"])
        self.assertEqual(report["limits"]["requests_used"], 5)
        self.assertEqual(report["index_observations"]["positive"], 3)
        self.assertTrue(report["index_observations"]["consistent"])
        self.assertEqual({row["provider"] for row in report["sources"]}, {"Mempool Lightning", "Blockstream Bitcoin"})
        self.assertTrue(all(row["observed_at"] for row in report["sources"]))
        self.assertNotIn("vin", report["bitcoin"])
        self.assertEqual(report["notice"], NOTICE)

    def test_funding_map_can_return_multiple_exact_outputs(self):
        outputs = [{"value": 250000, "scriptpubkey": SCRIPT}, {"value": 900000, "scriptpubkey": "5120" + "f" * 64}]
        report = self.lookup(index=[{"inputs": {}, "outputs": {"0": channel(), "1": channel(vout=1, capacity=900000)}}],
                             bitcoin={"/tx/" + FUNDING: transaction(vout=outputs)})
        self.assertEqual(report["status"], "complete")
        self.assertEqual([row["funding_vout"] for row in report["matches"]], [0, 1])
        self.assertEqual(report["limits"]["requests_used"], 5)

    def test_closure_finds_actual_bitcoin_input_instead_of_nominal_api_key_zero(self):
        index, bitcoin = self.closing_data()
        report = self.lookup(txid=CLOSING, index=index, bitcoin=bitcoin)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["classifications"], ["indexed_channel_closure"])
        self.assertEqual(report["matches"][0]["bitcoin_input_index"], 1)
        self.assertEqual(report["matches"][0]["closing_txid"], CLOSING)
        self.assertEqual(report["limits"]["requests_used"], 6)

    def test_transaction_can_close_one_channel_and_fund_another(self):
        old = channel(txid=CLOSING, closing=FUNDING, height=HEIGHT - 1)
        index = [{"inputs": {"0": old}, "outputs": {"0": channel()}}]
        bitcoin = {"/tx/" + FUNDING: transaction(vin=[{"txid": OTHER, "vout": 7},
                                                      {"txid": CLOSING, "vout": 0,
                                                       "prevout": {"value": 250000, "scriptpubkey": SCRIPT}}]),
                   "/tx/" + CLOSING: transaction(CLOSING, height=HEIGHT - 1)}
        report = self.lookup(index=index, bitcoin=bitcoin)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["classifications"], ["indexed_channel_funding", "indexed_channel_closure"])
        self.assertEqual([row["role"] for row in report["matches"]], ["funding", "closure"])

    def test_empty_valid_index_is_narrow_negative_even_for_unconfirmed_transaction(self):
        report = self.lookup(index=[{"inputs": {}, "outputs": {}}],
                             bitcoin={"/tx/" + FUNDING: transaction(confirmed=False)})
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["classifications"], ["no_indexed_channel_found"])
        self.assertEqual(report["matches"], [])
        self.assertEqual(report["bitcoin"]["status"], {"confirmed": False})
        self.assertIn("private", report["notice"])
        self.assertIn("only one closed channel", report["notice"])
        self.assertIn("gossip signatures", report["notice"])

    def test_any_positive_index_sample_wins_and_disagreement_is_disclosed(self):
        positive = _indexed_channels([{"inputs": {}, "outputs": {"0": channel()}}], FUNDING)
        samples = [([], "2026-10-06T01:00:00+00:00"),
                   (positive, "2026-10-06T01:00:01+00:00"),
                   ([], "2026-10-06T01:00:02+00:00")]
        selected, observed = _reconcile_index_samples(samples)
        summary = _index_sample_report(samples)
        self.assertEqual(selected, positive)
        self.assertEqual(observed, "2026-10-06T01:00:01+00:00")
        self.assertEqual((summary["attempts"], summary["positive"], summary["empty"]), (3, 1, 2))
        self.assertFalse(summary["consistent"])

    def test_conflicting_positive_channel_identities_are_rejected(self):
        first = _indexed_channels([{"inputs": {}, "outputs": {"0": channel()}}], FUNDING)
        second = _indexed_channels([{"inputs": {}, "outputs": {"0": channel(capacity=250001)}}], FUNDING)
        with self.assertRaisesRegex(TraceError, "conflicting channel identities"):
            _reconcile_index_samples([(first, "2026-10-06T01:00:00+00:00"),
                                      (second, "2026-10-06T01:00:01+00:00")])

    def test_no_script_or_amount_heuristic_discovers_channel(self):
        report = self.lookup(index=[{"inputs": {}, "outputs": {}}])
        self.assertEqual(report["classifications"], ["no_indexed_channel_found"])

    def test_invalid_response_never_returns_no_indexed_channel_found(self):
        for response in ([], {}, [{"inputs": {}, "outputs": []}], [{"inputs": {}}],
                         [{"inputs": {}, "outputs": {}}, {"inputs": {}, "outputs": {}}],
                         [{"inputs": {}, "outputs": {}, "error": "not available"}]):
            with self.subTest(response=response):
                report = self.lookup(index=response)
                self.assertEqual(report["status"], "error")
                self.assertEqual(report["classifications"], [])
                self.assertEqual(report["matches"], [])

    def test_misleading_index_rows_are_rejected_including_capacity_ids_and_pubkeys(self):
        mutations = [lambda row: row.update(transaction_id=OTHER),
                     lambda row: row.update(transaction_vout=1),
                     lambda row: row.update(capacity=250001),
                     lambda row: row.update(capacity=True),
                     lambda row: row.update(id="0"),
                     lambda row: row.update(short_id="800000x0x1"),
                     lambda row: row["node_left"].update(public_key="02" + "f" * 64),
                     lambda row: row["node_left"].update(public_key=row["node_right"]["public_key"]),
                     lambda row: row["node_left"].update(alias="bad\nnode"),
                     lambda row: row.update(closing_transaction_id=FUNDING)]
        for mutate in mutations:
            row = channel()
            mutate(row)
            with self.subTest(row=row):
                report = self.lookup(index=[{"inputs": {}, "outputs": {"0": row}}])
                self.assertEqual(report["status"], "error")
                self.assertEqual(report["classifications"], [])

    def test_funding_must_have_exact_block_confirmation_and_witness_output(self):
        transactions = [transaction(confirmed=False), transaction(height=HEIGHT - 1),
                        transaction(vout=[{"value": 250000, "scriptpubkey": "0014" + "f" * 40}]),
                        transaction(vout=[{"value": "250000", "scriptpubkey": SCRIPT}]),
                        transaction(OTHER)]
        for raw in transactions:
            with self.subTest(transaction=raw):
                report = self.lookup(bitcoin={"/tx/" + FUNDING: raw})
                self.assertEqual(report["status"], "error")
                self.assertEqual(report["matches"], [])

    def test_closure_checks_hash_funding_outpoint_and_previous_value_and_script(self):
        for failure in ("closing_hash", "outpoint", "previous_value", "previous_script", "duplicate_input"):
            index, bitcoin = self.closing_data()
            if failure == "closing_hash":
                index[0]["inputs"]["0"]["closing_transaction_id"] = OTHER
            elif failure == "outpoint":
                bitcoin["/tx/" + CLOSING]["vin"][1]["vout"] = 1
            elif failure == "previous_value":
                bitcoin["/tx/" + CLOSING]["vin"][1]["prevout"]["value"] += 1
            elif failure == "previous_script":
                bitcoin["/tx/" + CLOSING]["vin"][1]["prevout"]["scriptpubkey"] = "0020" + "f" * 64
            else:
                bitcoin["/tx/" + CLOSING]["vin"].append(copy.deepcopy(bitcoin["/tx/" + CLOSING]["vin"][1]))
            with self.subTest(failure=failure):
                report = self.lookup(txid=CLOSING, index=index, bitcoin=bitcoin)
                self.assertEqual(report["status"], "error")
                self.assertEqual(report["classifications"], [])

    def test_error_after_valid_first_match_does_not_publish_successful_subset(self):
        report = self.lookup(index=[{"inputs": {}, "outputs": {"0": channel(), "1": channel(vout=1, capacity=900000)}}],
                             bitcoin={"/tx/" + FUNDING: transaction(vout=[{"value": 250000, "scriptpubkey": SCRIPT},
                                                                       {"value": 800000, "scriptpubkey": SCRIPT}])})
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["matches"], [])

    def test_schema_count_and_duplicate_bounds_are_errors(self):
        for outputs in ({str(vout): channel(vout=vout) for vout in range(101)},
                        {"0": channel(), "01": channel(vout=1)},
                        {"0": channel(), "1": channel(vout=1, index=0)}):
            if len(outputs) == 2 and "1" in outputs:
                outputs["1"]["id"] = outputs["0"]["id"]
            with self.subTest(keys=list(outputs)):
                self.assertEqual(self.lookup(index=[{"inputs": {}, "outputs": outputs}])["status"], "error")

    def test_request_budget_is_shared_across_public_apis(self):
        report = self.lookup(max_requests=1)
        self.assertEqual(report["status"], "paused")
        self.assertEqual(report["stop_reason"], "request_limit")
        self.assertEqual(report["limits"]["requests_used"], 1)
        self.assertEqual(report["classifications"], [])
        index, bitcoin = self.closing_data()
        report = self.lookup(txid=CLOSING, index=index, bitcoin=bitcoin, max_requests=2)
        self.assertEqual(report["status"], "paused")
        self.assertEqual(report["limits"]["requests_used"], 2)
        self.assertEqual(report["matches"], [])

    def test_time_limit_and_cancellation_do_not_make_negative_classifications(self):
        original = Esplora.get
        calls = []

        def deadline(api, endpoint):
            calls.append(endpoint)
            if len(calls) == 2:
                raise StopRun("time_limit")
            return original(api, endpoint)

        with patch.object(Esplora, "get", deadline):
            report = self.lookup()
        self.assertEqual(report["status"], "paused")
        self.assertEqual(report["stop_reason"], "time_limit")
        self.assertEqual(report["classifications"], [])

        def cancel(_event):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.lookup(progress=cancel)

    def test_public_clients_have_fixed_endpoints_no_credentials_and_no_retained_store(self):
        original = Esplora.__init__
        clients, stores = [], []

        def capture(api, store, run_id, limits, **kwargs):
            clients.append(kwargs)
            stores.append(store)
            return original(api, store, run_id, limits, **kwargs)

        with patch.object(Esplora, "__init__", capture):
            self.lookup()
        self.assertEqual([client["base"] for client in clients],
                         [BITCOIN_API] + [MEMPOOL_API] * 3)
        self.assertTrue(all(client["auth"] == "none" and client["tx_cache_seconds"] == 0 for client in clients))
        self.assertTrue(all(store._closed for store in stores))
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["bitcoin.json", "mempool.json"])

    def test_fixture_failure_and_empty_index_missing_bitcoin_are_never_negative(self):
        for index in ([{"inputs": {}, "outputs": {}}], [{"inputs": {}, "outputs": {"0": channel()}}]):
            report = self.lookup(index=index, bitcoin={})
            self.assertEqual(report["status"], "error")
            self.assertEqual(report["classifications"], [])

    def test_inputs_and_limits_validate_before_clients_or_network(self):
        with patch("liquid_tracer.lightning_matching.Esplora") as client:
            for txid in ("", "f" * 63, FUNDING + "\n", [], None):
                with self.subTest(txid=txid), self.assertRaises(TraceError):
                    lookup_lightning(txid)
            for kwargs in ({"max_requests": 0}, {"max_requests": True}, {"max_requests": 101},
                           {"max_seconds": 0}, {"max_seconds": float("inf")}, {"max_seconds": 121},
                           {"mempool_fixture": self.mempool_fixture}):
                with self.subTest(kwargs=kwargs), self.assertRaises(TraceError):
                    lookup_lightning(FUNDING, **kwargs)
            client.assert_not_called()

    def test_report_validation_drops_unknown_fields_and_rejects_identity_urls_and_false_negatives(self):
        valid = self.lookup()
        valid["private_data"] = {"credential": "never exposed"}
        valid["matches"][0]["unknown"] = "never exposed"
        clean = validate_lightning_report(valid, FUNDING)
        self.assertNotIn("private_data", clean)
        self.assertNotIn("unknown", clean["matches"][0])
        mutations = [lambda report: report["query"].update(bitcoin_txid=OTHER),
                     lambda report: report.update(classifications=["no_indexed_channel_found"]),
                     lambda report: report["matches"][0].update(channel_url="https://evil.example/channel"),
                     lambda report: report["matches"][0]["nodes"][0].update(explorer_url="javascript:alert(1)"),
                     lambda report: report["sources"][0].update(url="https://evil.example/api"),
                     lambda report: report.update(status="error"),
                     lambda report: report.update(bitcoin=None)]
        for mutate in mutations:
            report = copy.deepcopy(valid)
            mutate(report)
            with self.subTest(report=report), self.assertRaises(TraceError):
                validate_lightning_report(report, FUNDING)


if __name__ == "__main__":
    unittest.main()
