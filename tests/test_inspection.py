import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.api import ENTERPRISE, TOKEN_URL, Esplora
from liquid_tracer.cli import main
from liquid_tracer.common import LBTC, TraceError, save_json
from liquid_tracer.inspection import inspect_transaction, inspect_transactions, parse_transaction_hashes

from tests.fixtures import A, B, C, D, fixture, output


class TransactionInspectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.fixture_path = self.root / "synthetic.json"
        save_json(self.fixture_path, fixture())

    def invoke(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = main(["inspect-tx", *arguments])
        return status, output.getvalue(), errors.getvalue()

    def test_invalid_hash_and_limits_fail_before_storage_provider_or_network(self):
        with patch("liquid_tracer.inspection.TemporaryDirectory") as directory, \
                patch("liquid_tracer.inspection.Esplora") as api:
            for txid in (A + ":vout", A + ":0", A + "\\:vout,", A[:-1], "g" * 64, "", A + "\n"):
                with self.subTest(txid=txid):
                    status, output, errors = self.invoke("--txid", txid)
                    self.assertEqual(status, 1)
                    self.assertEqual(output, "")
                    self.assertIn("64 hexadecimal characters", errors)
            for option, value in (("--max-requests", "0"), ("--max-seconds", "nan"),
                                  ("--max-seconds", "-1")):
                self.assertEqual(self.invoke("--txid", A, option, value)[0], 1)
            directory.assert_not_called()
            api.assert_not_called()

    def test_fixture_inspection_preserves_exact_indices_and_hidden_fields(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("urllib.request.build_opener", side_effect=AssertionError("Unexpected network")):
            result = inspect_transaction(A.upper(), fixture=self.fixture_path, max_requests=1)
        self.assertEqual(result["txid"], A)
        self.assertEqual(result["outputs"], [
            {"vout": 0, "outpoint": A + ":0", "address": "SYNTHETIC-victim-deposit", "value": 1_000_000,
             "asset": LBTC, "script_type": "v0_p2wpkh", "selectable": True, "reason": None},
            {"vout": 1, "outpoint": A + ":1", "address": "SYNTHETIC-unrelated-seed-sibling", "value": None,
             "asset": None, "script_type": "v0_p2wpkh", "selectable": True, "reason": None},
            {"vout": 2, "outpoint": A + ":2", "address": None, "value": 100,
             "asset": LBTC, "script_type": "fee", "selectable": False, "reason": "fee"},
        ])
        # Merely inspecting siblings never turns any of them into selected seeds.
        self.assertNotIn("seeds", result)
        self.assertEqual(list(self.root.iterdir()), [self.fixture_path])

    def test_pegouts_and_provably_unspendable_outputs_cannot_be_selected(self):
        for txid, index, reason in ((C, 1, "provably_unspendable"), (D, 0, "pegout")):
            with self.subTest(reason=reason):
                row = inspect_transaction(txid, fixture=self.fixture_path)["outputs"][index]
                self.assertFalse(row["selectable"])
                self.assertEqual(row["reason"], reason)
                self.assertEqual(row["outpoint"], txid + ":" + str(index))

    def test_cli_requires_no_case_and_writes_only_requested_report(self):
        report = self.root / "outputs.json"
        ignored_case = self.root / "must-not-create-case"
        with patch.dict(os.environ, {"LIQUID_CASE_DIR": str(ignored_case)}, clear=True):
            status, output, errors = self.invoke("--txid", A, "--fixture", str(self.fixture_path))
            self.assertEqual((status, errors), (0, ""))
            expected = json.loads(output)
            status, output, errors = self.invoke("--txid", A, "--fixture", str(self.fixture_path),
                                                 "--output", str(report))
        self.assertEqual((status, output, errors), (0, "Transaction outputs saved.\n", ""))
        self.assertEqual(json.loads(report.read_text()), expected)
        self.assertFalse(ignored_case.exists())
        report.write_text("preserve existing evidence")
        with patch("liquid_tracer.cli.inspect_transaction") as inspect:
            status, _, errors = self.invoke("--txid", A, "--fixture", str(self.fixture_path), "--output", str(report))
            inspect.assert_not_called()
        self.assertEqual(status, 1)
        self.assertIn("already exists", errors)
        self.assertEqual(report.read_text(), "preserve existing evidence")

    def test_output_preflight_avoids_paid_lookup_for_invalid_parent(self):
        regular_file = self.root / "regular-file"
        regular_file.write_text("not a directory")
        for parent in (self.root / "missing-directory", regular_file):
            with self.subTest(parent=parent), patch("liquid_tracer.cli.inspect_transaction") as inspect:
                status, output, errors = self.invoke("--txid", A, "--output", str(parent / "report.json"))
                self.assertEqual((status, output), (1, ""))
                self.assertIn("parent must be an existing directory", errors)
                inspect.assert_not_called()

    def test_invalid_hash_with_report_path_performs_no_filesystem_checks(self):
        with patch("pathlib.Path.exists") as exists, patch("pathlib.Path.is_dir") as is_dir, \
                patch("pathlib.Path.is_symlink") as is_symlink, \
                patch("liquid_tracer.inspection.TemporaryDirectory") as directory:
            status, output, errors = self.invoke("--txid", A + ":vout", "--output", "unused.json")
            self.assertEqual((status, output), (1, ""))
            self.assertIn("64 hexadecimal characters", errors)
            exists.assert_not_called()
            is_dir.assert_not_called()
            is_symlink.assert_not_called()
            directory.assert_not_called()

    def test_response_identity_and_types_are_checked_without_echoing_bad_data(self):
        sentinel = "SYNTHETIC-private-server-content"
        invalid = [None, [], {"txid": B, "vout": []}, {"txid": A, "vout": sentinel},
                   {"txid": A, "vout": [sentinel]}, {"txid": A, "vout": [{"scriptpubkey": "00", "value": sentinel}]},
                   {"txid": A, "vout": [{"scriptpubkey": "00", "value": True}]},
                   {"txid": A, "vout": [{"scriptpubkey": None}]},
                   {"txid": A, "vout": [{}]},
                   {"txid": A, "vout": [{"scriptpubkey_address": {"invalid": sentinel}}]}]
        for response in invalid:
            with self.subTest(response=response):
                save_json(self.fixture_path, {"/tx/" + A: response})
                report = self.root / "must-not-create.json"
                status, output, errors = self.invoke("--txid", A, "--fixture", str(self.fixture_path),
                                                     "--output", str(report))
                self.assertEqual(status, 1)
                self.assertEqual(output, "")
                self.assertNotIn(sentinel, errors)
                self.assertFalse(report.exists())

    def live_api(self, transport, directories):
        def make_api(store, *args, **kwargs):
            directories.append(store.case)
            return Esplora(store, *args, **kwargs, min_interval=0, transport=transport)
        return patch("liquid_tracer.inspection.Esplora", side_effect=make_api)

    def test_paid_lookup_uses_oauth_and_fetches_only_one_transaction(self):
        calls, directories = [], []
        def transport(method, url, headers, body, timeout):
            calls.append((method, url))
            self.assertLessEqual(timeout, 30)
            if url == TOKEN_URL:
                return 200, {}, b'{"access_token":"SYNTHETIC-token","expires_in":300}'
            self.assertEqual(headers["Authorization"], "Bearer SYNTHETIC-token")
            return 200, {}, json.dumps(fixture()["/tx/" + A]).encode()
        environment = {"BLOCKSTREAM_CLIENT_ID": "SYNTHETIC-client", "BLOCKSTREAM_CLIENT_SECRET": "SYNTHETIC-secret"}
        with patch.dict(os.environ, environment, clear=True), self.live_api(transport, directories):
            status, output, errors = self.invoke("--txid", A)
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(json.loads(output)["txid"], A)
        self.assertEqual(calls, [("POST", TOKEN_URL), ("GET", ENTERPRISE + "/tx/" + A)])
        self.assertTrue(directories)
        self.assertTrue(all(not path.exists() for path in directories))
        for secret in (*environment.values(), "SYNTHETIC-token"):
            self.assertNotIn(secret, output + errors)

    def test_request_budget_counts_authentication_and_does_not_fetch_extra_endpoints(self):
        calls, directories = [], []
        def transport(method, url, headers, body, timeout):
            calls.append((method, url))
            return 200, {}, b'{"access_token":"SYNTHETIC-token"}'
        with patch.dict(os.environ, {"BLOCKSTREAM_CLIENT_ID": "SYNTHETIC-client", "BLOCKSTREAM_CLIENT_SECRET": "SYNTHETIC-secret"}, clear=True), \
                self.live_api(transport, directories):
            status, output, errors = self.invoke("--txid", A, "--max-requests", "1")
        self.assertEqual((status, output), (1, ""))
        self.assertIn("request limit reached", errors)
        self.assertEqual(calls, [("POST", TOKEN_URL)])
        self.assertTrue(all(not path.exists() for path in directories))

    def test_missing_credentials_and_untrusted_origin_do_not_make_requests(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("Unexpected network request")
        with patch.dict(os.environ, {}, clear=True), self.live_api(forbidden, []):
            status, output, errors = self.invoke("--txid", A)
            self.assertEqual((status, output), (1, ""))
            self.assertIn("BLOCKSTREAM_CLIENT_ID", errors)
            status, output, errors = self.invoke("--txid", A, "--base-url", "https://other.example/liquid/api")
            self.assertEqual((status, output), (1, ""))
            self.assertIn("only be sent to enterprise.blockstream.info", errors)

    def batch_fixture(self, count=10):
        txids = [hashlib.sha256(f"SYNTHETIC-batch-{index}".encode()).hexdigest() for index in range(count)]
        transactions = {"/tx/" + txid: {"txid": txid, "vout": [output(f"SYNTHETIC-batch-address-{index}")]}
                        for index, txid in enumerate(txids)}
        save_json(self.fixture_path, transactions)
        return txids, transactions

    def test_batch_parser_normalizes_separators_and_deduplicates_in_input_order(self):
        self.assertEqual(parse_transaction_hashes(f" {B.upper()}, {A}\n{B}\t{C}, "), [B, A, C])
        self.assertEqual(parse_transaction_hashes([B.upper(), A, B]), [B, A])
        self.assertEqual(parse_transaction_hashes((A,)), [A])

    def test_batch_validates_every_hash_and_limits_before_storage_or_requests(self):
        too_many, _ = self.batch_fixture(101)
        invalid_values = [None, "", " , \n ", [], [A, None], [A, "accidentally-pasted-secret"],
                          A + ", " + B + ":0", A + "," + "g" * 64, too_many]
        with patch("liquid_tracer.inspection.TemporaryDirectory") as directory, \
                patch("liquid_tracer.inspection.Esplora") as api:
            for value in invalid_values:
                with self.subTest(value=value), self.assertRaises(TraceError) as error:
                    inspect_transactions(value)
                self.assertNotIn("accidentally-pasted-secret", str(error.exception))
            for options in ({"max_requests": 0}, {"max_seconds": float("nan")}, {"max_seconds": -1}):
                with self.subTest(options=options), self.assertRaises(TraceError):
                    inspect_transactions([A, B], **options)
            directory.assert_not_called()
            api.assert_not_called()
        # The cap counts distinct hashes, so duplicates do not consume it.
        self.assertEqual(len(parse_transaction_hashes(too_many[:100] + [too_many[0]])), 100)

    def test_ten_transaction_fixture_uses_one_client_and_exact_output_indices(self):
        txids, _ = self.batch_fixture()
        original_api = Esplora
        clients = []
        def client(*args, **kwargs):
            instance = original_api(*args, **kwargs)
            clients.append(instance)
            return instance
        with patch("liquid_tracer.inspection.Esplora", side_effect=client), \
                patch("urllib.request.build_opener", side_effect=AssertionError("Unexpected network")):
            result = inspect_transactions(", ".join(txids + [txids[0].upper()]), fixture=self.fixture_path)
        self.assertEqual(list(result), ["transactions"])
        self.assertEqual([item["txid"] for item in result["transactions"]], txids)
        self.assertEqual([item["outputs"][0]["outpoint"] for item in result["transactions"]],
                         [txid + ":0" for txid in txids])
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0].budget.requests, 10)
        self.assertEqual(clients[0].budget.limits.max_requests, 50)
        self.assertEqual(clients[0].budget.limits.max_seconds, 300)
        self.assertFalse(clients[0].store.case.exists())
        self.assertEqual(list(self.root.iterdir()), [self.fixture_path])

    def test_ten_paid_lookups_share_one_oauth_token_without_tracing(self):
        txids, transactions = self.batch_fixture()
        calls, directories = [], []
        def transport(method, url, headers, body, timeout):
            calls.append((method, url))
            if url == TOKEN_URL:
                return 200, {}, b'{"access_token":"SYNTHETIC-token","expires_in":300}'
            self.assertEqual(headers["Authorization"], "Bearer SYNTHETIC-token")
            return 200, {}, json.dumps(transactions[url.removeprefix(ENTERPRISE)]).encode()
        with patch.dict(os.environ, {"BLOCKSTREAM_CLIENT_ID": "SYNTHETIC-client", "BLOCKSTREAM_CLIENT_SECRET": "SYNTHETIC-secret"}, clear=True), \
                self.live_api(transport, directories):
            result = inspect_transactions(txids)
        self.assertEqual([item["txid"] for item in result["transactions"]], txids)
        self.assertEqual(calls, [("POST", TOKEN_URL)] + [("GET", ENTERPRISE + "/tx/" + txid) for txid in txids])
        self.assertEqual(len(directories), 1)
        self.assertFalse(directories[0].exists())
        for secret in ("SYNTHETIC-client", "SYNTHETIC-secret", "SYNTHETIC-token"):
            self.assertNotIn(secret, json.dumps(result))

    def test_batch_request_budget_includes_auth_and_earlier_transactions(self):
        calls, directories = [], []
        def transport(method, url, headers, body, timeout):
            calls.append((method, url))
            if url == TOKEN_URL:
                return 200, {}, b'{"access_token":"SYNTHETIC-token"}'
            return 200, {}, json.dumps(fixture()["/tx/" + A]).encode()
        with patch.dict(os.environ, {"BLOCKSTREAM_CLIENT_ID": "SYNTHETIC-client", "BLOCKSTREAM_CLIENT_SECRET": "SYNTHETIC-secret"}, clear=True), \
                self.live_api(transport, directories), self.assertRaises(TraceError) as error:
            inspect_transactions([A, B], max_requests=2)
        self.assertIn(B, str(error.exception))
        self.assertIn("request limit reached", str(error.exception))
        self.assertEqual(calls, [("POST", TOKEN_URL), ("GET", ENTERPRISE + "/tx/" + A)])
        self.assertFalse(directories[0].exists())

    def test_batch_time_budget_does_not_restart_for_each_transaction(self):
        elapsed = [0.0]
        calls, directories = [], []
        def transport(method, url, headers, body, timeout):
            calls.append((method, url))
            if url == TOKEN_URL:
                return 200, {}, b'{"access_token":"SYNTHETIC-token"}'
            elapsed[0] = 2.0
            return 200, {}, json.dumps(fixture()["/tx/" + A]).encode()
        with patch.dict(os.environ, {"BLOCKSTREAM_CLIENT_ID": "SYNTHETIC-client", "BLOCKSTREAM_CLIENT_SECRET": "SYNTHETIC-secret"}, clear=True), \
                self.live_api(transport, directories), \
                patch("liquid_tracer.api.time.monotonic", side_effect=lambda: elapsed[0]), \
                self.assertRaises(TraceError) as error:
            inspect_transactions([A, B], max_seconds=1)
        self.assertIn(B, str(error.exception))
        self.assertIn("time limit reached", str(error.exception))
        self.assertEqual(calls, [("POST", TOKEN_URL), ("GET", ENTERPRISE + "/tx/" + A)])
        self.assertFalse(directories[0].exists())

    def test_batch_failure_identifies_transaction_and_leaves_no_partial_report(self):
        data = fixture()
        # The API must never silently attach another transaction's outputs.
        data["/tx/" + B] = {"txid": C, "vout": []}
        save_json(self.fixture_path, data)
        with self.assertRaises(TraceError) as error:
            inspect_transactions([A, B], fixture=self.fixture_path)
        self.assertIn(B, str(error.exception))
        self.assertIn("does not match", str(error.exception))
        self.assertNotIn(C, str(error.exception))
        self.assertEqual(list(self.root.iterdir()), [self.fixture_path])
        calls, directories = [], []
        def transport(method, url, headers, body, timeout):
            calls.append(url)
            if url.endswith(A):
                return 200, {}, json.dumps(fixture()["/tx/" + A]).encode()
            return 404, {}, b'SYNTHETIC-private-server-content'
        with self.live_api(transport, directories), self.assertRaises(TraceError) as error:
            inspect_transactions([A, B, C], auth="none")
        self.assertIn(B, str(error.exception))
        self.assertIn("HTTP 404", str(error.exception))
        self.assertNotIn("SYNTHETIC-private-server-content", str(error.exception))
        self.assertEqual(calls, [ENTERPRISE + "/tx/" + A, ENTERPRISE + "/tx/" + B])
        self.assertFalse(directories[0].exists())


if __name__ == "__main__":
    unittest.main()
