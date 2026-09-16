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
| Address Entities | Underlying attribution names without generated prefixes, separated by semicolons. These are analyst attributions, not verified ownership. |
| Address Hash | Full input prevout/output address. For a peg-out, the explicit Bitcoin destination. Blank where there is no recorded address. |
| Crypto Value | Explicit integer amount in **base units**, exactly as recorded. For L-BTC these are satoshis. Confidential or missing amounts are blank, never zero. |
| USD Value | Blank. The tracer has no recorded historical USD-rate source; it does not guess prices or treat tokens as USD. |
| PegOut Value | Explicit amount of the peg-out request output, in the same base units. Blank for non-peg-outs or unavailable amounts. This is not independent confirmation of a Bitcoin payout. |
| Direction | `IN` for transaction inputs, `OUT` for transaction outputs. Amounts remain nonnegative; direction is separate. |
| Number of I/O | **Zero-based index**, not a count. `IN` uses the input's `vin` position in this transaction; `OUT` uses its `vout` position. |

`PegOut Value` is included after `USD Value`, incorporating the separately
requested peg-out column. It repeats a subset of `Crypto Value`; do not add the
two columns together. This fixed-column format has no asset column. Consult the
saved transaction for the asset identity and do not sum amounts across different
assets. No asset is assumed to be L-BTC merely because it is on Liquid.

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
