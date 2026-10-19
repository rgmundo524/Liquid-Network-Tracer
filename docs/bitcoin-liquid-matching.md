# Bitcoin and Liquid bridge matching

Open **Liquid Network** in the workspace sidebar. No investigation or seed
transactions are required. The tool supports native Liquid mainnet peg-ins and
peg-outs using Blockstream's public or authenticated APIs.

## Browser workflow

First choose **Peg-in** or **Peg-out**, then choose the network of the transaction
you are providing. These are separate choices:

| Transfer | Transaction provided | Search result |
| --- | --- | --- |
| Peg-in | Bitcoin deposit | Liquid claim inputs that reference its exact Bitcoin outpoint |
| Peg-in | Liquid claim | Bitcoin deposits referenced by its peg-in inputs |
| Peg-out | Liquid request | Candidate Bitcoin payout outputs matching its destination script and amount |
| Peg-out | Bitcoin payout | Candidate Liquid requests matching its Bitcoin output scripts and amounts |

Use **Single transaction** to paste a hash, or **Upload CSV** to provide a column
of hashes from the selected network. A file may be up to 1 MiB with 500 unique
hashes. Review the detected column, duplicates, and validation errors before
starting. Other CSV columns stay local. Uploading alone does not start a search.

**Load outputs** shows outputs from the network you selected. With no selection,
the search checks any eligible output. Checkbox selection narrows Bitcoin deposit
or payout searches, or selects native Liquid peg-out request outputs. Changing
the network, transfer, hash, or API source clears old output selections.

For a Liquid peg-in claim, the Bitcoin references are in its inputs. The displayed
Liquid outputs are informational: selecting one cannot establish which Bitcoin
deposit funded that output. The tool checks the claim's peg-in references without
pretending to allocate value to individual Liquid outputs.

Search settings are inside the collapsed **Advanced settings** panel. They
include public versus authenticated APIs, a page budget, and timestamp offsets
in minutes from the supplied transaction's confirmed block timestamp. Searches
forward from Bitcoin peg-ins or Liquid peg-outs default to -5 through +4320
minutes. Searches backward from Bitcoin peg-out payouts default to -4320 through
+5 minutes. Direct Liquid peg-in lookups need no historical search window.
Negative offsets allow timestamp tolerance; these are search bounds, not a claim
that Bitcoin and Liquid blocks share a clock or a confirmation schedule.

Starting a search creates a visible, cancellable task. Independent plots and
collections remain available. Results appear as transaction cards and, for
batches, a status table. There is no past-search library. The current result
remains available while moving between tool tabs in this browser session.
Reloading the page starts a fresh UI session. New report files are temporary and
are removed when the local service session closes. Existing archived reports
are not deleted. The shared transaction index remains reusable across sessions.

When every transaction in a CSV batch has been processed, the browser is asked
to download **matches.csv** automatically, once for that job. This includes
terminal error, unmatched, and ambiguous rows. Paused, canceled, or unprocessed
batches do not trigger the completion download. The current result retains a
CSV link if automatic downloading is blocked or partial results are needed.
Historical task discovery and reopening the tab never trigger old downloads.

The CSV identifies both chain transactions and their relevant vouts or vins,
along with the matching basis and each search outcome. Multiple candidates
produce multiple rows. Unmatched or unprocessed rows retain their source hash;
an empty counterpart hash does not prove that no bridge transaction exists.
Batches share one overall page, request, and time budget.

If a limit pauses a search, **Continue / refresh same window** resumes the current
session result. Completed batch rows retain their dated checks while remaining
rows receive a fresh task budget. Saved history cursors are reused only after
checking their chain snapshot. A fresh search does not carry earlier results.

Public APIs are selected by default and need no token. Unchecking that setting
uses the configured authenticated connection and terminal credential flow.
Opening the tool and viewing session results do not load credentials. Both modes
expose the public blockchain fields used for matching; authentication does not
reveal confidential Liquid values or establish ownership. Public access may be
rate limited. See [Blockstream's API announcement](https://blog.blockstream.com/enhanced-analytics-and-infrastructure-improvements-for-the-blockstream-explorer-api/).

## Peg-out evidence and limitations

A native Liquid peg-out output publishes a Bitcoin destination script, explicit
L-BTC amount, and parent-chain genesis hash. The corresponding Bitcoin payout
format does not publish a Liquid request outpoint. Consequently, matching the
destination script, amount, and time window produces a **candidate**, not the
exact outpoint-reference verification available for peg-ins. Multiple agreeing
outputs or requests are flagged as ambiguous. Even one candidate does not prove
that the Bitcoin payment was made by the Liquid federation or establish ownership.

Only native L-BTC requests for Bitcoin mainnet are eligible. Transfers to an
exchange or swap service's Liquid address are not native peg-out requests. Those
require separate service evidence. Window limits, incomplete explorer history,
unconfirmed transactions, or delayed payouts can leave a search unresolved.

The fields and distinction above follow the [Esplora transaction format](https://github.com/Blockstream/esplora/blob/master/API.md#transaction-format)
and [Liquid transaction differences](https://docs.liquid.net/docs/liquid-and-bitcoin-transaction-differences).
The federation's payout process is described in the [Liquid technical overview](https://docs.liquid.net/docs/technical-overview).

## Legacy peg-in CLI and archived evidence

The following sections document the existing peg-in CLI and its durable report
format. New browser searches use the session workflow described above. Legacy
CLI commands and previously archived reports retain their existing behavior.

## Deposit and claim are separate steps

A native peg-in requires a Bitcoin deposit followed by a Liquid claim. After
102 Bitcoin confirmations, the depositor's wallet or service can submit the
claim with the deposit evidence. Wallets may automate this step, but the
Bitcoin deposit alone does not create spendable L-BTC. A claim can be delayed
or remain unsubmitted. The 72-hour default is a search window, not a protocol
claim deadline. See the [Liquid peg-in workflow](https://docs.liquid.net/docs/advanced-pegin-pegout).

Do not infer that a deposit remains claimable forever. Blockstream advises
minimizing the delay and states that peg-in addresses are not long-term durable.
See the [official peg-in guide](https://help.blockstream.com/liquid-network/perform-advanced-liquid-operations/peg-in-btc-to-the-liquid-network).

## What establishes a match

The matcher requires a Liquid input with `is_pegin: true` whose Bitcoin `txid`
and `vout` exactly match the requested output. It checks the Bitcoin output
exists and fetches the candidate Liquid transaction again before presenting a
match, even when discovery used the local index. An equal amount, reused
address, or ordinary Liquid input does not establish a match.

Confirmed exact reference means both transaction responses report confirmation.
Unconfirmed references are identified separately. These are explorer-backed
reference checks, not independent validation of the peg-in witness, Bitcoin
merkle proof, or consensus rules.

The exact reference identifies the Liquid claim transaction, not a one-to-one
allocation to its outputs. A claim can include other inputs and several outputs.
Output values, asset identifiers, and ownership are not inferred when absent
from public data. The Bitcoin deposit amount is retained in satoshis. The recorded block-time
difference is shown when both block timestamps are available; it is not a
measurement of when either transaction was first broadcast.

## Discovery, limits, and continuation

Discovery uses the requested date interval to locate nearby Liquid blocks. It
binary-searches recorded median block time, then adds two hours of boundary
padding and eleven earlier blocks. This does not equate Bitcoin blocks with
Liquid blocks or turn confirmations into a fixed duration. The individual
Liquid transaction's recorded block timestamp must be inside the requested
interval before it can qualify as a discovered match.

Individual timestamps are not monotone. The padded block range is a practical
search boundary, not a proof that every transaction timestamp in the interval
falls inside those heights. Reports show both the requested UTC interval and
the selected block heights. Complete bounded coverage means those heights were
searched. It does not mean that every possible claim has been excluded.

Starting at the upper bound, the matcher skips coinbase-only blocks and examines
nearby block transaction pages until it finds a peg-in or native-asset peg-out.
It finishes that entire anchor block, then follows Esplora's filtered L-BTC
history backwards. Those pages contain up to 25 peg-in, peg-out, or burn
transactions. Ordinary transfer history is omitted after the anchor is found.
If no anchor occurs in the range, discovery stops at the lower bound. It never
falls back to downloading history from today's tip or from genesis.

The browser defaults to 100 transaction pages, 1,200 API attempts, and five minutes
per task. Pages include bootstrap block pages and filtered history pages. Header
lookups, verification requests, authentication and retries count toward the shared
request/time budgets. A task may pause before the page limit. Continue / refresh
same window reuses completed ranges and partial-block checkpoints. Adjust / expand
window fills the controls with a wider interval for review; it does not start a
request until Find Liquid claim is selected. Overlapping windows reuse the shared
index and search the gaps. Downloaded evidence and committed checkpoints survive
cancellation, limits, and later failures.

Coverage is retained by block height, including the fact that the last block on
a filtered page may still be incomplete. Before reuse, the previous snapshot is
checked against the current chain. Before certifying completion, the new snapshot
is checked again. A detected chain change invalidates height-based coverage and
pauses the search. Immutable responses identified by block hash remain reusable.

An any-output search stops after finding matching claims among the indexed
candidates. Other outputs of that Bitcoin transaction may have separate claims.
Select all relevant Bitcoin outputs explicitly when they must each be checked;
a multiple-output search continues until all selected outputs are matched or
the bounded search ends. Partial results remain saved if a limit interrupts
verification of several candidates. History processing counts include repeated
entries when recent pages are refreshed; they are not unique-transaction totals.

No match in searched data does not prove a deposit was never claimed. It can
reflect a limited scan, an as-yet-unclaimed deposit, an unconfirmed claim, or
incomplete explorer data. Automatic discovery searches confirmed history.
A known Liquid transaction can be checked directly while unconfirmed.
Saved results reflect their recorded observations; rerun to check current status.

## Saved results and shared index

Load saved matches opens the workspace's most recent 50 searches, including
bounded and interrupted attempts and earlier investigation-based searches. Each completed report includes:

- `match.json`: query, sources, status, coverage, Bitcoin output data, exact
  references, and Liquid claim outputs.
- `matches.csv`: one row per Bitcoin outpoint to Liquid claim-input reference,
  with recorded Bitcoin/Liquid block timestamps and their difference in seconds.
  Batch exports additionally retain explicit status rows for transactions without
  a verified match, including unprocessed transactions after a limit.
- `liquid-outputs.csv`: each matched claim's outputs, with allocation explicitly
  not determined. A claim's outputs are exported once even when it references
  several Bitcoin deposits.
- `evidence.json`: raw Bitcoin and checked Liquid transaction responses, source
  endpoints, observation times, and response hashes.
- `SHA256SUMS`: hashes checked before reports or downloads are served.

New reports live in `cases/.pegin-matching/pegin-matches/<report-id>/`. They are
independent of investigation seeds and remain available when an investigation
is deleted. They do not replace collections, previews, attribution, or seeds.

Earlier reports in `cases/<investigation>/pegin-matches/<report-id>/` remain
readable in the same library, labeled with their original investigation. They
are read in place without rewriting evidence, so deleting that original
investigation also deletes its legacy reports. Download any needed copies first.

The resumable index and fetched history observations remain in
`cases/.pegin-index/<source-digest>/`. Searches under the same workspace root
reuse that index when they use the same API sources. Public, enterprise, and fixture
sources have separate indexes. One writer holds the index lock, including
when matching is started from another service instance or the CLI.

## CLI

Use `liquid-trace ... --public` for public APIs, or the existing live launcher
to obtain configured enterprise credentials.
`--investigations-dir` selects the workspace; omitting it uses the configured
investigations directory. The legacy `--case` option remains available for
existing scripts, but cannot be combined with `--investigations-dir`.
The matcher does not inherit `LIQUID_CASE_DIR`.

```bash
liquid-live pegin-match --investigations-dir cases \
  --txid BITCOIN_TRANSACTION_ID --vout 0 --start-minutes -5 --end-minutes 4320 --max-pages 100
```

Look up a Liquid claim's Bitcoin deposit references without supplying any
Bitcoin transaction or scanning history:

```bash
liquid-trace pegin-match --investigations-dir cases \
  --from-liquid LIQUID_CLAIM_TRANSACTION_ID --public
```

Match several Bitcoin transactions with one combined report:

```bash
liquid-trace pegin-match --investigations-dir cases \
  --txids BITCOIN_TRANSACTION_ID_1,BITCOIN_TRANSACTION_ID_2 --public
```

For existing scripts, verify a known candidate without history scanning:

```bash
liquid-live pegin-match --investigations-dir cases \
  --txid BITCOIN_TRANSACTION_ID --vout 0 --liquid-txid LIQUID_CLAIM_TRANSACTION_ID
```

To use Blockstream's official public mainnet APIs without credentials:

```bash
liquid-trace pegin-match --investigations-dir cases \
  --txid BITCOIN_TRANSACTION_ID --vout 0 --public \
  --start-minutes 720 --end-minutes 2160 \
  --max-pages 100 --max-requests 1200 --max-seconds 300
```

The legacy `--start-hours` and `--end-hours` flags remain supported for existing
scripts. Do not mix hour and minute flags in one request. With no offset flags,
new searches use -5 and +4320 minutes.

Rerun a discovery command to continue its window, or change its offsets to expand it. CLI limits allow up to 1,000 history
pages, 10,000 API attempts, and 3,600 seconds. A bounded search returns a saved
`paused` report instead of claiming the history was exhausted. Ctrl-C preserves
completed index work and a cancellation report once the lookup has started.

## Source definitions

The [Esplora API](https://github.com/Blockstream/esplora/blob/master/API.md)
defines Liquid transaction inputs and native asset history. The
[Elements transaction structures](https://github.com/ElementsProject/elements/blob/master/src/primitives/transaction.h)
define the peg-in input flag and previous outpoint. These reference fields
are distinct from Bitcoin payout matching for a Liquid peg-out, which remains
a separate operation.
