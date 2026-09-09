# Liquid UTXO Tracer

A local Python program for bounded forward tracing on Liquid, per-run CSV/evidence exports, and incremental updates to an editable Miro graph. Version 0.2.0; Python 3.11+ on Linux or macOS; no third-party runtime packages. Live case integration still needs validation with your credentials and starting outputs.

Miro is the investigation workspace. The program retrieves blockchain data and keeps the evidence and object mapping; it can be run from your terminal without a Miro plugin. CSV files document each run. The saved graph plan supplies native Miro shapes and connectors directly through the REST API, so a generic CSV-to-graph importer is unnecessary.

It follows **exact output spends**, preserves confidential quantities as unknown, saves unfinished branches, and extends them in later runs. The graph uses address circles, transaction squares, and separate diamonds for fees, unspendable outputs, and peg-out requests.

## Start with the offline demo

Clone the project and enter its devenv shell. Install [devenv](https://devenv.sh/getting-started/) first if needed. The shell provides Python 3.12 and project commands; no pip install or credentials are needed for the demo:

```bash
git clone https://github.com/rgmundo524/Liquid-Network-Tracer.git
cd Liquid-Network-Tracer
devenv shell
```

```bash
python3 -m liquid_tracer trace \
  --case ./demo-case \
  --fixture examples/demo-api.json \
  --seeds-file examples/demo-seeds.txt \
  --hops 1
```

The command prints its run ID and output directory. Inspect `nodes.csv`, `edges.csv`, `frontier.csv`, and `RUN.md`. All demo hashes and addresses are synthetic. Preview the number of Miro objects using the printed run ID:

```bash
python3 -m liquid_tracer miro-sync \
  --case ./demo-case --run RUN_ID \
  --board DEMO_BOARD --dry-run
```

This preview makes no network calls, needs no token, and writes no state. `DEMO_BOARD` is only a placeholder. It cannot detect remote manual edits or deleted items; live sync performs that preflight. Extend the demo using `trace --case ./demo-case --fixture examples/demo-api.json --resume RUN_ID --additional-hops 2`. The full demo ends in a **synthetic peg-out request**, not an actual Bitcoin payout or Avalanche transaction.

If needed, add `--offline-preview` to `trace` or `export` to also save an HTML inspector and SVG. These are optional inspection files; normal runs use Miro for visual review.

Inside the shell, `liquid-demo` runs the same synthetic trace. `liquid-trace` is the normal CLI; `liquid-live` loads credentials at runtime before calling it. `liquid-test` runs the offline suite. Outside devenv, Python 3.11+ still works with `python3 -m liquid_tracer`; optional installation is `python3 -m pip install -e .`.

Public defaults and command definitions live in `devenv.nix`. The package input is pinned in `devenv.yaml`; commit the generated `devenv.lock` after the first successful shell build. See [development and secrets](docs/development.md) for local overrides, keyring setup and the optional `.env` provider.

## Configure the paid Blockstream API

From the project directory inside `devenv shell`, store these values in your desktop keyring. Each command prompts for its value, so the credential does not appear in the command or shell history:

```bash
liquid-secrets-setup blockstream
```

Run API commands using `liquid-live`. It retrieves the values when the process starts and provides the environment variables the Python client already expects. The public `secretspec.toml` contains names and descriptions only. On Linux, keyring storage requires a running Secret Service implementation such as GNOME Keyring or KWallet. The [development guide](docs/development.md) also covers an ignored local `.env` file loaded at runtime. The Python application itself does not read `.env` files.

The default base is `https://enterprise.blockstream.info/liquid/api`. The client exchanges your credentials for a bearer token and refreshes it before expiry. These settings follow [Blockstream's authentication documentation](https://help.blockstream.com/blockstream-explorer-api/use-explorer-api/make-a-rest-api-request-with-your-api-keys). No credentials were supplied or used while developing this project.

## Trace a case

Create `case-seeds.txt` with **one exact Liquid outpoint per line**:

```text
# Replace the placeholder with a real Liquid transaction hash.
YOUR_64_CHARACTER_LIQUID_TXID:0
```

An outpoint is a transaction hash plus a zero-based output index. Seed only the relevant outputs. Seeding every output of a funding transaction would also include its unrelated recipients and change.

```bash
liquid-live trace \
  --case ./cases/theft-liquid \
  --seeds-file case-seeds.txt \
  --hops 3 \
  --max-transactions 250 \
  --max-outpoints 2000 \
  --max-requests 600 \
  --max-seconds 300
```

You can repeat `--seed 'TXID:VOUT'` instead of using a file. A Bitcoin deposit into Liquid must first be linked to its Liquid transaction/output; a Bitcoin txid alone is not a Liquid seed. This version intentionally does not expand complete address histories.

| Limit | Meaning |
| --- | --- |
| `--hops 3` | Seed outputs are hop 0; follow at most three spending transactions from them. |
| `--max-transactions 250` | At most 250 newly added transactions this run, including seed funding transactions. |
| `--max-outpoints 2000` | At most 2,000 output examinations this run, including terminal outputs. |
| `--max-requests 600` | At most 600 HTTP attempts, including token requests and retries. This is **not a count of Blockstream credits**. |
| `--max-seconds 300` | Stops traversal and bounds HTTP timeouts. Checkpoint/export filesystem work can finish afterward. |

GET requests are spaced by `--min-interval`, default 0.25 seconds. Transient server errors retry at most three times, subject to the same budget. One outspends response serves all outputs of its transaction within a run.

## Continue after review

Use the run ID printed by the prior command:

```bash
liquid-live trace \
  --case ./cases/theft-liquid \
  --resume PRIOR_RUN_ID \
  --additional-hops 3
```

This creates a **new** run, preserves the parent, increases the absolute hop ceiling from 3 to 6, and reconsiders its unfinished branches. Request, time, transaction and output budgets reset for each run. The snapshot and graph are cumulative.

To finish a run stopped by its request budget without increasing depth, use `--resume PRIOR_RUN_ID` without `--additional-hops`. To extend selected branches, repeat `--only 'TXID:VOUT'`. Unselected frontier entries remain documented. Any new spending transaction exposes all of its outputs as candidates, even during selective continuation.

`frontier.csv` records why each branch stopped: hop limit, unspent at observation, unconfirmed funding/spend, analyst stop, request/time/transaction/output limit, error, or interruption. `bounded_complete` means all currently selected tasks were examined within the hop ceiling; it does not mean every branch has a known destination.

## Label services and stop branches

Copy `examples/labels.json` and add supported attributions:

```json
[
  {
    "kind": "outpoint",
    "value": "REAL_LIQUID_TXID:0",
    "entity": "Service name",
    "source": "case-record:service-response-2026-09-09",
    "confidence": "corroborated",
    "observed_at": "2026-09-09",
    "stop": true,
    "notes": "Describe the supporting evidence and its applicable date."
  }
]
```

Use `--labels my-labels.json` on a new run or continuation. `kind` may be `outpoint`, `address`, or `script`; confidence may be `candidate`, `corroborated`, or `confirmed`. Confidence records the analyst's assessment. The program does not independently verify labels. `stop: true` is an explicit branch stop, regardless of confidence. To continue it, supply a revised label file without that stop.

Prefer outpoint labels when attribution applies to a particular payment. Address labels apply to every matching occurrence in that run. Match the address representation returned by Esplora, or use script/outpoint matching when your source has a confidential address encoding. The program does not decode confidential addresses or derive spend keys.

`docs/bridge-research.md` explains the current leads. `examples/handoffs.csv` is a **manual research ledger**, not an automated cross-chain matcher or input to this version's graph. No Liquid service addresses are preloaded as attributed facts.

## Keep one editable Miro graph up to date

Create or choose a Miro board and obtain an access token with **`boards:read` and `boards:write`** scopes and access to that board. Follow [Miro's REST API quickstart](https://developers.miro.com/docs/rest-api-build-your-first-hello-world-app). Keep the token locally. If it expires, replace it and rerun sync; this program does not refresh Miro tokens automatically.

```bash
liquid-secrets-setup miro

liquid-trace miro-sync \
  --case ./cases/theft-liquid --run RUN_ID \
  --board 'https://miro.com/app/board/YOUR_BOARD_ID/' --dry-run

liquid-live miro-sync \
  --case ./cases/theft-liquid --run RUN_ID \
  --board 'https://miro.com/app/board/YOUR_BOARD_ID/'
```

Use the **same case directory and board** for later runs. Sync creates native [shapes](https://developers.miro.com/reference/create-shape-item-1) and [connectors](https://developers.miro.com/reference/create-connector-1), checks existing items, and updates compatible managed fields. It does not call Blockstream. A per-board state file under `case/miro/` maps stable graph IDs to remote item IDs, and `case/miro/reports/` retains sync reports separately from immutable run exports.

- Repeating the same run creates no duplicate acknowledged objects. A newer continuation adds new objects and a run note; already mapped circles and squares are reused.
- Existing positions and dimensions are never patched. New batches are placed to the right of mapped shapes. This placement does not account for unrelated board content.
- Keep mapped shapes on the board canvas. Items with frame/group-relative coordinates stop sync before writes; nested layouts are not supported in this version.
- Existing content, captions and styles are updated only where the current value still matches the program's saved baseline. Analyst edits are retained and listed as conflicts in the report. Avoid simultaneous content/style edits during a live sync: the API check and update are separate requests.
- No objects are deleted. A missing mapped object or changed connector endpoint stops sync before writes; restore it or repair the mapping after inspection.
- A board mapping is bound to one case, API source, and address mode. After a run has been synced, extend that run (or a later descendant). Older snapshots, sibling branches and independent roots cannot overwrite newer graph classifications. You may skip intermediate unpublished runs.
- Finish an interrupted sync before switching to another run. Resolving a pending creation alone does not finish that sync.
- The default cap is **750 new shapes plus connectors per sync**, not total board size. Use `--max-new-items` to change it. Existing-object checks can still take time on a large graph.

Keep **`case.json` and the entire `miro/` directory** with the case. Losing or replacing the mapping can cause duplicates; do not delete state as a retry mechanism. Live sync changes board content, without changing board sharing or inviting people.

To trace and sync in one command:

```bash
liquid-live trace \
  --case ./cases/theft-liquid --resume PRIOR_RUN_ID --additional-hops 3 \
  --miro-board 'https://miro.com/app/board/YOUR_BOARD_ID/'
```

The run and checksums are saved before Miro updates. If sync fails, its run ID and retry details are printed; use `miro-sync` on that saved run without spending more explorer requests. A partial publication retains acknowledged progress.

If a POST times out or returns a server error, its outcome can be uncertain. The publisher records the pending request and pauses to avoid duplicates. Inspect the board and pending state, then reconcile using the state path printed by the preview or sync report:

```bash
python3 -m liquid_tracer miro-resolve --state PATH_TO_STATE.json --item-id EXISTING_MIRO_ITEM_ID
# Or, only if you have verified that the pending item was not created:
python3 -m liquid_tracer miro-resolve --state PATH_TO_STATE.json --absent
```

Then repeat `miro-sync`. This explicit recovery is necessary because an accepted creation request can lose its response. Reconciliation must use the actual existing item ID, or a verified absence.

The older `miro-publish` command remains for resuming version 0.1 snapshot publications; its per-plan mapping is not interchangeable with incremental state. For an older saved run, regenerate an export into a new directory with `export --case CASE --run RUN_ID --out NEW_DIRECTORY`, then select its plan using `miro-sync --case CASE --run RUN_ID --board BOARD --plan NEW_DIRECTORY/miro-plan.json`. This does not adopt objects from an old snapshot; use a fresh board for that migration.

Default circles are **address occurrences tied to individual outpoints**. Reused addresses can appear more than once. Each input/output edge retains its outpoint and index; full identifiers remain in CSV/JSON evidence, with explorer links on live graph nodes. `--merge-addresses` gives one circle per address/script for a compact summary, but can create apparent cycles and paths between unrelated UTXOs. Use the default for evidence review. Continuation inherits its parent's address mode.

## Files saved per run

| File | Purpose |
| --- | --- |
| `trace.json` | Full checkpoint, seeds, limits, parent, lineage, frontier and labels. |
| `nodes.csv`, `edges.csv` | Graph objects and directed input/output relationships with stable IDs, roles and details. |
| `inputs.csv`, `outputs.csv` | Address/script, public values or commitments, asset fields and evidence IDs. |
| `spends.csv` | Exact output-to-spending-input links validated against both API responses. |
| `events.csv` | Peg-ins, issuance/reissuance, peg-outs, fees and provably unspendable outputs. |
| `frontier.csv`, `frontier.json` | Unfinished branches and stopping reasons. |
| `evidence/`, `evidence-index.json` | API response bytes, source, timestamp, HTTP status and SHA-256. |
| `graph.json` | Complete graph data with case namespace and run metadata. |
| `miro-plan.json` | Native Miro item sync plan, with a checksum. |
| `graph.html`, `graph.svg` | Optional inspection files, created only with `--offline-preview`. |
| `RUN.md`, `SHA256SUMS` | Run summary and file checksums. |

The case-level `evidence.sqlite` holds cached responses and request-attempt records. Preserve the **whole case directory** for continuation. Resume, export, and sync verify the saved run's checksum manifest before using it. On Linux, `sha256sum -c SHA256SUMS` also checks files manually. Do not edit completed run exports; put new attribution in a separate labels file and create a continuation. An abrupt process kill before export finishes may leave an incomplete checkpoint without a manifest; start from the last completed run in that situation.

## Interpretation and limitations

Liquid confidential transactions can hide **asset type as well as amount**. The program retains explicit values when present and leaves absent values unknown. It does not assume every output is L-BTC. Ordinary UTXO consumption/creation is distinct from monetary issuance and burning. See the [Esplora API specification](https://github.com/blockstream/esplora/blob/master/API.md).

After a transaction consumes a selected output, all spendable outputs become **candidate descendants**. This establishes UTXO reachability, not value allocation. Swaps, fees, co-inputs, batching and other assets make blanket “stolen funds” attribution inappropriate. The software performs no common-input ownership clustering, change detection, percentage taint calculation or FIFO accounting.

Peg-out requests expose a requested parent-chain destination when available; they do not prove a particular Bitcoin payout. Service swaps may exit Liquid without a corresponding protocol burn in the customer's transaction. No automatic Bitcoin, Lightning, Avalanche or service-order matching is implemented.

Confirmed transaction JSON is cached for 24 hours between fresh runs by default. Unspent/spend-status responses are refreshed on each run; within-run status remains an observation at its recorded time. Continuation intentionally preserves already recorded historical links. It is not a fully atomic chain-tip snapshot or reorg reconciliation engine. To re-observe the original path, start a fresh run from the original seeds with `--tx-cache-seconds 0`. Unconfirmed transactions are excluded from expansion by default; `--include-unconfirmed` includes them provisionally.

Evidence is retrieved explorer JSON, not independently verified raw transaction serialization, consensus validation or cryptographic proof of chain inclusion. Hashes detect byte changes against the saved manifest; they are not signatures or trusted timestamps. Preserve originals and validate material case findings against additional records.

## Validation and development

```bash
python3 -m unittest discover -v
```

Tests cover seed precision, hop boundaries, split/merge paths, continuation, budgets, spend-status freshness, confidential fields, event stops, reference validation, labeling, graph identity, evidence checksums, OAuth refresh, incremental Miro updates, manual-edit preservation, retries, and uncertain creation recovery. API behavior is tested with fixtures and mock transports. **Paid Blockstream access and live Miro publication have not been exercised** because no case seeds or credentials were provided.

Source is organized into `api.py`, `store.py`, `trace.py`, `export.py`, `miro.py`, and `cli.py`. The CLI is separate from tracing, so a notebook or case-management interface can call the same engine later.
