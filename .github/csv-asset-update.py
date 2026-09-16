"""Apply the reviewed CSV-only schema change to its exact source revision."""
from pathlib import Path

changes = {}


def replace(path, old, new, count=1):
    text = changes.get(path, Path(path).read_text(encoding="utf-8"))
    actual = text.count(old)
    if (count is None and actual == 0) or (count is not None and actual != count):
        raise RuntimeError(f"Unexpected source in {path}: expected {count}, got {actual}: {old!r}")
    changes[path] = text.replace(old, new)


module = "liquid_tracer/transaction_csv.py"
replace(module, "from .common import TraceError, canonical, output_kind", "from .common import HEX64, LBTC, TraceError, canonical, output_kind")
replace(module,
    '    "Address Flags", "Address Entities", "Address Hash", "Crypto Value",\n    "USD Value", "PegOut Value", "Direction", "Number of I/O",',
    '    "Address Flags", "Address Hash", "Asset Value", "Asset",\n    "PegOut Value", "Direction", "Number of I/O",')
replace(module,
    '    "Crypto Value and PegOut Value are exact explicit base units (satoshis for L-BTC), "\n'
    '    "not inferred whole-token amounts. This fixed-column format has no asset column; "\n'
    '    "consult the saved transaction for the asset and do not sum different assets. "\n'
    '    "Blank amounts are unknown, not zero. USD Value is blank because no historical "\n'
    '    "USD valuation source is recorded. PegOut Value repeats the peg-out request output "\n'
    '    "amount; it is not a second transfer or proof of a Bitcoin payout."',
    '    "Asset Value and PegOut Value are exact explicit base units (satoshis for BTC/L-BTC), "\n'
    '    "not inferred whole-token amounts. Asset is L-BTC for its recognized explicit asset ID, "\n'
    '    "BTC for a Bitcoin peg-in input, or the full explicit Liquid asset ID otherwise. "\n'
    '    "Confidential or missing asset identities are blank, never inferred from an amount, "\n'
    '    "address label or graph color. Do not sum different assets. Blank amounts are unknown, "\n'
    '    "not zero. PegOut Value repeats the peg-out request output amount; it is not a second "\n'
    '    "transfer or proof of a Bitcoin payout."')
replace(module, '\ndef _block_time(transaction):', '''
def _asset(output, *, pegin=False):
    """Identify this occurrence's asset without consulting labels or a registry."""
    if pegin:
        # This occurrence is the Bitcoin prevout consumed by a Liquid peg-in,
        # not an L-BTC output minted in the receiving transaction.
        return "BTC"
    asset = output.get("asset")
    if asset is None:
        return ""
    if not isinstance(asset, str) or not HEX64.fullmatch(asset):
        raise TraceError("Transaction CSV requires a full explicit hexadecimal asset ID")
    return "L-BTC" if asset.lower() == LBTC else asset


def _block_time(transaction):''')
replace(module,
    '            entities = sorted({label.get("entity") or label.get("name") for label in matches\n'
    '                               if label.get("entity") or label.get("name")})\n', '')
replace(module,
    '                "; ".join(names), "; ".join(flags), "; ".join(entities), address,\n'
    '                amount, "", amount if pegout else "", direction.upper(), io_index,',
    '                "; ".join(names), "; ".join(flags), address,\n'
    '                amount, _asset(output, pegin=pegin), amount if pegout else "", direction.upper(), io_index,')

tests = "tests/test_transaction_csv.py"
replace(tests,
    '            "Address Flags", "Address Entities", "Address Hash", "Crypto Value", "USD Value",',
    '            "Address Flags", "Address Hash", "Asset Value", "Asset",')
replace(tests, '["Crypto Value"]', '["Asset Value"]', count=None)
replace(tests, '        self.assertEqual(row["USD Value"], "")', '        self.assertNotIn("USD Value", row)')
replace(tests, '        self.assertTrue(all(row["USD Value"] == "" for row in rows))',
    '        self.assertTrue(all("USD Value" not in row for row in rows))')
replace(tests, '        self.assertEqual(first["Address Entities"], "Only output zero")',
    '        self.assertNotIn("Address Entities", first)')
replace("tests/test_csv_export.py", '["Crypto Value"]', '["Asset Value"]', count=None)
replace("tests/test_service_presentation.py", '            self.assertEqual(row["Address Entities"], "\'" + label["entity"])',
    '            self.assertNotIn("Address Entities", row)')
replace("tests/test_service_presentation.py", '            self.assertEqual(row["Address Entities"], "Synthetic A; Synthetic Z")',
    '            self.assertNotIn("Address Entities", row)')

docs = "docs/transaction-csv.md"
replace(docs, '| Address Entities | Underlying attribution names without generated prefixes, separated by semicolons. These are analyst attributions, not verified ownership. |\n', '')
replace(docs, '| Crypto Value |', '| Asset Value |')
replace(docs, '| USD Value | Blank. The tracer has no recorded historical USD-rate source; it does not guess prices or treat tokens as USD. |',
    '| Asset | `L-BTC` for its recognized explicit asset ID; `BTC` for a Bitcoin peg-in input; otherwise the full explicit Liquid asset ID. Blank when confidential or unavailable. No asset registry lookup or guessed ticker. |')
replace(docs,
    '`PegOut Value` is included after `USD Value`, incorporating the separately\n'
    'requested peg-out column. It repeats a subset of `Crypto Value`; do not add the\n'
    'two columns together. This fixed-column format has no asset column. Consult the\n'
    'saved transaction for the asset identity and do not sum amounts across different\n'
    'assets. No asset is assumed to be L-BTC merely because it is on Liquid.',
    '`Asset` appears immediately after `Asset Value`. The CSV does not include\n'
    '`Address Entities`, `Crypto Value`, or `USD Value`. Attribution names remain in\n'
    '`Address Label`; the underlying saved/imported assessments are unchanged.\n\n'
    '`PegOut Value` follows `Asset` and repeats a subset of `Asset Value`; do not add\n'
    'the two value columns together. Values remain in exact base units, not decimal\n'
    'token amounts. Do not sum amounts across different assets or treat a blank\n'
    'asset as L-BTC. Asset identity is independent of whether the amount is public.\n'
    'A peg-out row identifies its Liquid request-output asset, not a guessed Bitcoin\n'
    'payout asset. Historical exports and connection snapshots keep their original\n'
    'headers; create a fresh export or connection preview to use the new columns.\n\n'
    '```csv\n'
    'Block,Time,Transaction Label,Transaction Hash,Address Label,Address Flags,Address Hash,Asset Value,Asset,PegOut Value,Direction,Number of I/O\n'
    '```')

for path, text in changes.items():
    Path(path).write_text(text, encoding="utf-8")
print("Updated " + ", ".join(sorted(changes)))
