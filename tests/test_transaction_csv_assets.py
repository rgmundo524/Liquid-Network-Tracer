"""Asset-aware transaction CSV keeps exact values and occurrence semantics."""
import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import LBTC, TraceError, canonical
from liquid_tracer.export import build_graph
from liquid_tracer.transaction_csv import (
    TRANSACTION_CSV_FIELDS, VALUE_NOTICE, _asset, transaction_csv_rows, write_transaction_csv,
)
from tests.test_transaction_csv import find, state_fixture
from tests.fixtures import A, B, C, D


class TransactionAssetCSVTests(unittest.TestCase):
    def setUp(self):
        self.state = state_fixture()

    def rows(self, **kwargs):
        return transaction_csv_rows(build_graph(self.state, **kwargs), self.state)

    def test_new_header_order_and_removed_columns_in_every_row(self):
        expected = ("Block", "Time", "Transaction Label", "Transaction Hash", "Address Label",
                    "Address Flags", "Address Hash", "Asset Value", "Asset", "PegOut Value",
                    "Direction", "Number of I/O")
        self.assertEqual(TRANSACTION_CSV_FIELDS, expected)
        self.assertEqual(expected.index("Asset"), expected.index("Asset Value") + 1)
        for row in self.rows():
            self.assertEqual(tuple(row), expected)
            self.assertFalse({"Address Entities", "Crypto Value", "USD Value"}.intersection(row))
        self.assertNotIn("USD Value", VALUE_NOTICE)
        self.assertNotIn("Crypto Value", VALUE_NOTICE)

    def test_known_asset_on_both_sides_of_the_same_spend(self):
        rows = self.rows()
        for txid, direction in ((A, "OUT"), (B, "IN")):
            row = find(rows, txid, direction, 0)
            self.assertEqual(row["Asset"], "L-BTC")
            self.assertEqual(row["Asset Value"], "0.01000000")

    def test_asset_and_amount_confidentiality_are_independent(self):
        output = self.state["transactions"][B]["data"]["vout"][0]
        output["asset"] = LBTC
        row = find(self.rows(), B, "OUT", 0)
        self.assertEqual((row["Asset Value"], row["Asset"]), ("", "L-BTC"))
        output.pop("asset")
        output["value"] = 123
        row = find(self.rows(), B, "OUT", 0)
        self.assertEqual((row["Asset Value"], row["Asset"]), (123, ""))
        self.assertIn("CONFIDENTIAL ASSET", row["Address Flags"])
        output.pop("assetcommitment")
        self.assertEqual(find(self.rows(), B, "OUT", 0)["Asset"], "")

    def test_other_assets_use_full_explicit_id_and_exact_base_units_offline(self):
        asset, value = "ab" * 32, 2 ** 63 - 1
        self.state["transactions"][D]["data"]["vout"][0].update(asset=asset, value=value)
        graph = build_graph(self.state)
        before = canonical((self.state, graph))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transactions.csv"
            with patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("No asset lookup")):
                write_transaction_csv(path, graph, self.state)
            reader = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8")))
            self.assertEqual(tuple(reader.fieldnames), TRANSACTION_CSV_FIELDS)
            row = next(r for r in reader if r["Transaction Hash"] == D and r["Direction"] == "OUT")
        self.assertEqual((row["Asset Value"], row["Asset"], row["PegOut Value"]), (str(value), asset, str(value)))
        self.assertEqual(canonical((self.state, graph)), before)

    def test_pegin_is_bitcoin_not_a_guessed_liquid_asset(self):
        vin = self.state["transactions"][A]["data"]["vin"][0]
        vin["is_pegin"] = True
        vin["prevout"].pop("asset", None)
        vin["prevout"].pop("assetcommitment", None)
        vin["prevout"]["value"] = 321
        row = find(self.rows(), A, "IN", 0)
        self.assertEqual((row["Asset Value"], row["Asset"]), ("0.00000321", "BTC"))
        self.assertIn("PEG-IN", row["Address Flags"])

    def test_pegout_keeps_request_asset_and_coinbase_does_not_invent_one(self):
        row = find(self.rows(), D, "OUT", 0)
        self.assertEqual((row["Asset"], row["PegOut Value"]), ("L-BTC", "0.00001234"))
        self.state["transactions"][A]["data"]["vin"] = [{"is_coinbase": True}]
        row = find(self.rows(), A, "IN", 0)
        self.assertEqual((row["Asset Value"], row["Asset"]), ("", ""))

    def test_fees_and_zero_valued_unspendable_output_keep_asset(self):
        rows = self.rows(include_fees=True)
        for row in rows:
            if "FEE" in row["Address Flags"]:
                self.assertEqual(row["Asset"], "L-BTC")
                self.assertEqual(row["Asset Value"], "0.00000100")
        self.assertEqual((find(rows, C, "OUT", 1)["Asset Value"], find(rows, C, "OUT", 1)["Asset"]), ("0.00000000", "L-BTC"))

    def test_lbtc_values_convert_exactly_without_floats_or_evidence_changes(self):
        output = self.state["transactions"][D]["data"]["vout"][0]
        for value, expected in ((0, "0.00000000"), (1, "0.00000001"),
                                (100_000_000, "1.00000000"),
                                (9_007_199_254_740_993, "90071992.54740993"),
                                (2 ** 63 - 1, "92233720368.54775807")):
            with self.subTest(value=value):
                output.update(asset=LBTC.upper(), value=value)
                graph = build_graph(self.state)
                before = canonical((self.state, graph))
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "transactions.csv"
                    write_transaction_csv(path, graph, self.state)
                    rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))
                row = next(r for r in rows if r["Transaction Hash"] == D and r["Direction"] == "OUT")
                self.assertEqual((row["Asset Value"], row["Asset"], row["PegOut Value"]),
                                 (expected, "L-BTC", expected))
                self.assertEqual(canonical((self.state, graph)), before)

    def test_unknown_pegout_asset_never_assumes_bitcoin_precision(self):
        output = self.state["transactions"][D]["data"]["vout"][0]
        output.pop("asset")
        output.update(assetcommitment="SYNTHETIC-confidential", value=100_000_000)
        row = find(self.rows(), D, "OUT", 0)
        self.assertEqual((row["Asset Value"], row["Asset"], row["PegOut Value"]),
                         (100_000_000, "", 100_000_000))
        self.assertIn("PEG-OUT REQUEST", row["Address Flags"])
        self.assertIn("CONFIDENTIAL ASSET", row["Address Flags"])

    def test_pegin_whole_btc_conversion_preserves_large_values(self):
        vin = self.state["transactions"][A]["data"]["vin"][0]
        vin["is_pegin"] = True
        vin["prevout"].pop("asset", None)
        vin["prevout"]["value"] = 9_007_199_254_740_993
        row = find(self.rows(), A, "IN", 0)
        self.assertEqual((row["Asset Value"], row["Asset"]), ("90071992.54740993", "BTC"))

    def test_asset_validation_and_case_preserve_full_ids(self):
        self.assertEqual(_asset({"asset": LBTC.upper()}), "L-BTC")
        self.assertEqual(_asset({"asset": "AB" * 32}), "AB" * 32)
        self.assertEqual(_asset({}), "")
        for invalid in (True, 1, {}, [], "", "BTC", "zz" * 32, "ab" * 31, "ab" * 32 + "\n"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(TraceError):
                    _asset({"asset": invalid})


if __name__ == "__main__":
    unittest.main()
