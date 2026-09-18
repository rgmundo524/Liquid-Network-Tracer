import unittest

from liquid_tracer.common import LBTC, TraceError, bitcoin_units, display_amount, quantity
from liquid_tracer.export import graph_quantity


class AmountUnitsTests(unittest.TestCase):
    def test_exact_bitcoin_units_include_zero_one_satoshi_and_large_integers(self):
        for value, expected in (
            (0, "0.00000000"), (1, "0.00000001"), (99_999_999, "0.99999999"),
            (100_000_000, "1.00000000"), (123_456_789, "1.23456789"),
            (9_007_199_254_740_993, "90071992.54740993"),
            (18_446_744_073_709_551_615, "184467440737.09551615"),
        ):
            with self.subTest(value=value):
                self.assertEqual(bitcoin_units(value), expected)
                output = {"value": value, "asset": LBTC.upper()}
                self.assertEqual(display_amount(output), expected)
                self.assertEqual(graph_quantity(output), expected + " L-BTC")
                self.assertEqual(quantity(output), expected + "; L-BTC")

    def test_invalid_bitcoin_amounts_are_not_silently_rounded(self):
        for value in (True, False, -1, 1.5, 100_000_000.0, "100000000", None):
            with self.subTest(value=value), self.assertRaises(TraceError):
                bitcoin_units(value)

    def test_asset_identity_is_required_for_lbtc_conversion(self):
        for asset in (None, "12" * 32, "L-BTC", "LBTC", "BTC", LBTC[:-1]):
            with self.subTest(asset=asset):
                output = {"value": 123_456_789, "asset": asset}
                self.assertEqual(display_amount(output), "123456789 base units")
                self.assertTrue(graph_quantity(output).startswith("123456789 base units "))
        self.assertEqual(graph_quantity({"asset": LBTC, "valuecommitment": "08abcd"}), "?? L-BTC")
        self.assertEqual(graph_quantity({"assetcommitment": "0aabcd"}), "?? ??")

    def test_explicit_bitcoin_pegin_is_displayed_in_btc(self):
        self.assertEqual(graph_quantity({"value": 123_456_789}, pegin=True), "1.23456789 BTC")
        self.assertEqual(graph_quantity({}, pegin=True), "?? BTC")


if __name__ == "__main__":
    unittest.main()
