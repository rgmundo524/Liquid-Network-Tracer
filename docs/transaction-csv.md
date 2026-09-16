# Transaction input/output CSV

**Create CSV export** now produces one `transactions.csv`, not CSV copies of
Miro nodes, connectors, frames, colors, positions, or other drawing objects.
Each data row corresponds to one displayed transaction input/output arrow.
The separate `export.json` and `SHA256SUMS` describe provenance and completion.

## Columns

| Column | Meaning |
| --- | --- |
| Block | Confirmed block height of this row's transaction. Blank if unavailable. |
| Time | That transaction's block timestamp, UTC `YYYY-MM-DDTHH:MM:SSZ`. No run time or guessed unconfirmed time. |
| Transaction Label | `Starting TX N` for a numbered starter; blank for other transactions, which currently have no separate analyst transaction-label field. |
| Transaction Hash | Full hash of the transaction receiving the input or creating the output. |
| Address Label | Applicable saved/imported attribution names, with the existing `Suspected ` prefix when appropriate. |
| Address Flags | Applicable facts and controls: selected seed, context, STOP TRACING, unspent at observation, confidential amount/asset, peg-in, peg-out request, fee, unspendable or coinbase. Not a risk score or ownership finding. |
| Address Hash | Full input prevout/output address. For a peg-out, the explicit Bitcoin destination. Blank where there is no recorded address. |
| Asset Value | Explicit integer amount in **base units**, exactly as recorded. For L-BTC these are satoshis. Confidential or missing amounts are blank, never zero. |
| Asset | `L-BTC` for its recognized explicit asset ID; `BTC` for a Bitcoin peg-in input; otherwise the full explicit Liquid asset ID. Blank when confidential or unavailable. No asset registry lookup or guessed ticker. |
| PegOut Value | Explicit amount of the peg-out request output, in the same base units. Blank for non-peg-outs or unavailable amounts. This is not independent confirmation of a Bitcoin payout. |
| Direction | `IN` for transaction inputs, `OUT` for transaction outputs. Amounts remain nonnegative; direction is separate. |
| Number of I/O | **Zero-based index**, not a count. `IN` uses the input's `vin` position in this transaction; `OUT` uses its `vout` position. |

`Asset` appears immediately after `Asset Value`. The CSV does not include
`Address Entities`, `Crypto Value`, or `USD Value`. Attribution names remain in
`Address Label`; the underlying saved/imported assessments are unchanged.

`PegOut Value` follows `Asset` and repeats a subset of `Asset Value`; do not add
the two value columns together. Values remain in exact base units, not decimal
token amounts. Do not sum amounts across different assets or treat a blank
asset as L-BTC. Asset identity is independent of whether the amount is public.
A peg-out row identifies its Liquid request-output asset, not a guessed Bitcoin
payout asset. Historical exports and connection snapshots keep their original
headers; create a fresh export or connection preview to use the new columns.

```csv
Block,Time,Transaction Label,Transaction Hash,Address Label,Address Flags,Address Hash,Asset Value,Asset,PegOut Value,Direction,Number of I/O
```

A UTXO created at `A:vout 7` and spent by input 2 of B appears as `A, OUT, 7` and
`B, IN, 2` when both arrows are displayed. Those are two transaction occurrences,
not duplicate records. Repeated addresses never collapse I/O rows. Attributions
are matched to each exact occurrence, so an outpoint-only annotation does not
leak to another output that happens to reuse its address.

Rows sort by UTC transaction time, block height, full transaction hash, IN before
OUT, and numeric index. Unknown timestamps sort last. The output contains full
hashes, normal CSV quoting and Unicode, and spreadsheet-formula protection.
Import hash/address columns as text in spreadsheet applications to preserve IDs.
The file never parses abbreviated captions or uses visual styling as evidence.

## Full graph versus starter connections

Use the ordinary **Create CSV export** for the selected full-trace snapshot.
Its current fee visibility applies: a hidden fee arrow has no exported row.
Current local attribution settings apply. Manual edits made only in Miro are
not imported, and a browser-only temporary visibility toggle is not a saved
export selection. No API calls, ELK recalculation or retracing are needed.

For a connection-only graph, regenerate **Starter connections** and select its
**Transaction CSV** download. Only its connecting arrows are exported. No-match
results have the header and zero data rows. Existing connection snapshots remain
readable/publishable; regenerate a preview to obtain its new transaction CSV.

New trace archives also include `transactions.csv`. Internal legacy graph and
raw evidence tables stay in those archives for reproducibility and compatibility;
they are no longer copied into the user-facing CSV export. Historical run
archives and prior exports are never rewritten or deleted by this change.

```sh
liquid-trace csv-export --case /path/to/investigation --run latest
```

After updating, restart the application. Rebuild a compiled browser frontend.
Create a fresh CSV export; an old graph-table bundle is not silently converted.
