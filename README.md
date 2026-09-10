# Liquid UTXO Tracer

A local Python program for bounded forward tracing on Liquid, per-run CSV/evidence exports, and incremental updates to an editable Miro graph. Version 0.2.0; Python 3.11+ on Linux or macOS. The interactive interface uses Textual; explicit tracing commands use the Python standard library. Live case integration still needs validation with your credentials and starting outputs.

Miro is the investigation workspace. The program retrieves blockchain data and keeps the evidence and object mapping; it can be run from your terminal without a Miro plugin. CSV files document each run. The saved graph plan supplies native Miro shapes and connectors directly through the REST API, so a generic CSV-to-graph importer is unnecessary.

It follows **exact output spends**, preserves confidential quantities as unknown, saves unfinished branches, and extends them in later runs. The graph uses address circles, transaction squares, and separate diamonds for unspendable outputs, peg-out requests, and optionally transaction fees.

Graph captions use `vin/vout number · amount asset`. For example, `vout 0 · ?? ??` means neither amount nor asset is available in the public data; `?? L-BTC` retains an explicitly identified asset with an unavailable amount. Known amounts remain exact base-unit quantities. Other asset IDs are shortened for display and retained in full in the evidence. Address circles show the address and any analyst attribution; the creating transaction hash and exact UTXO reference remain in the saved details instead of being repeated on the circle or input caption.

The graph legend uses the same palette as its nodes and connectors:

| Appearance | Meaning |
| --- | --- |
| Purple squares | Provided starting transactions, including those also reached through another starting transaction. |
| Blue squares | Subsequent transactions outside the provided starting set. |
| Red circles | Selected starting outputs. |
| Yellow circles | Reachable candidate outputs. |
| Light gray circles | Context addresses. |
| Green circles | Analyst attribution; read its confidence label. |
| Pink diamonds | Fees, unspendable outputs, or peg-out requests. |
| Dark teal / gray arrows | Traced UTXO links / context connections. |

Starting-transaction color comes from the saved seed transaction hashes, independently of hop depth, and remains purple across continuations. Where circle roles overlap, attribution takes precedence over seed, then candidate, then context. This also applies when addresses are merged. Colors describe graph roles; they do not establish ownership or allocate stolen value. Run notes show counts of starting outputs and transactions while the full seed list stays in the saved run data.

The default layout follows recorded transaction dependencies from left to right, including related transactions selected together as starting points. Inputs sit to the left of their transaction and outputs to the right, with connected items grouped into nearby rows. Columns are spaced 360 units apart and rows have at least 240 units between node centers, leaving more room around 160-unit nodes. Separate components use separate lanes. Reused addresses remain distinct UTXO occurrences by default; merging them can introduce return edges that cannot all point right.

Transaction connectors attach to fixed sides: inputs enter the left and outputs leave the right, including outputs connected to the fee row. These attachment sides remain the same when a related address is moved above, below, or behind the transaction. New connectors use this convention automatically. Choose **Organize Miro graph** to apply it to existing connectors; normal sync preserves their existing attachment choices.

**Include transaction fee flows** is a checkbox in investigation settings and the defaults for new investigations. It starts unchecked. When included, fees appear in a horizontal row above the main flow, ordered by available chain chronology with deterministic tie handling. Excluding fees changes the graph only: fee amounts, events, and API observations remain in the evidence exports.

The pink diamonds identify special outputs. **PEG-OUT REQUEST** records Esplora's parsed withdrawal instruction to the parent chain. On Liquid mainnet, a normal L-BTC peg-out burns L-BTC and requests a Bitcoin payout to the encoded destination. The tracer has not matched or confirmed that separate Bitcoin payout, and the label does not identify an Avalanche bridge. See [Blockstream's peg-out explanation](https://help.blockstream.com/liquid-network/faqs/what-is-a-liquid-peg-out) and the [Esplora transaction fields](https://github.com/blockstream/esplora/blob/master/API.md#transaction-format).

**UNSPENDABLE** means the recorded script is identified as `OP_RETURN`, preventing that output from being spent later. The individual output branch ends there; other spendable outputs from the transaction can still be traced. Such an output may carry data or represent a burn, but Liquid can also add zero-value unspendable outputs when constructing confidential transactions. When the amount or asset is `??`, do not infer a positive-value L-BTC burn from the diamond alone. See the [Elements blinding documentation](https://elementsproject.org/en/doc/22.0.0/rpc/wallet/blindrawtransaction/) and [null-data script explanation](https://developer.bitcoin.org/devguide/transactions.html#null-data).

## Start the program

The project has **one main `devenv.nix`** for Python, commands, and environment defaults. SecretSpec retrieves API credentials from Proton Pass when a selected action needs them. Investigation names, board IDs, and run history are saved as case data; you do not need another Nix file or shell variables for each investigation.

The bundled Proton Pass CLI supports Linux on x86_64 or ARM64 and macOS on Apple Silicon. Clone the project and enter its devenv shell. Install [devenv](https://devenv.sh/getting-started/) first if needed:

```bash
git clone https://github.com/rgmundo524/Liquid-Network-Tracer.git
cd Liquid-Network-Tracer
devenv shell
liquid-trace
```

In an interactive terminal, `liquid-trace` opens a Textual interface with **New investigation**, **Continue investigation**, **Settings**, and **Exit**. Select actions with the keyboard or mouse. You can also launch it explicitly with `liquid-trace menu`. Opening the menu does not load credentials or call Blockstream or Miro.

1. Choose **New investigation** and give it a name. The program creates a unique subdirectory under `cases/`.
2. Select **Live Liquid** or the offline synthetic demo. Paste one or more bare Liquid transaction hashes, separated by commas, into **Transaction hashes** and choose **Load outputs**. Review the outputs grouped by transaction, use Enter to toggle the relevant rows across transactions, then choose **Use selected outputs**. This fills the starting-output field; it replaces any existing entries. Alternatively enter known outputs directly as `HASH:NUMBER`, separated by whitespace or commas. Optionally provide an existing Miro board URL or ID; you can add it later.
3. Start the first bounded run from the investigation menu. Review the hop and request limits before running it. A live trace retrieves credentials through SecretSpec; a demo trace needs none.
4. Review the saved run summary and exported file locations. If the case has no board, choose **Create Miro board**, review its name and visibility, and create it. Then choose **Preview Miro** and **Sync to Miro** to publish the saved run.
5. Next time, launch `liquid-trace`, choose **Continue investigation**, and select the saved case. Continue its latest run with another bounded hop allowance, or review and sync what is already saved.

The investigation settings let you change its name, board, run limits, and fee visibility. Top-level **Settings** changes defaults for new investigations. Existing investigations keep their own saved defaults. Creating a board saves its ID with the case and shows its URL in the investigation menu. It creates an empty board; publishing the traced graph is a separate **Sync to Miro** action. A completed run can be synced after creating or linking a board without tracing again.

For a quick local view, choose **Mermaid chart** in the investigation menu after saving a run. The button creates a Mermaid source file, renders an SVG, and opens an HTML preview in your browser. It uses the latest saved run and current fee setting, needs no API credentials or Miro board, and saves each preview in a new directory under the investigation's `previews/`. The menu shows the file path if a browser cannot be opened. Miro remains the editable investigation board.

`vout` means the output's numeric index, starting at zero. In `HASH:0`, `0` selects the first output; `HASH:1` selects the second. Do not type the literal word `vout` or a backslash before the colon. Choose the output connected to your investigation; output 0 is only an example. The picker starts with nothing selected and excludes fees, peg-outs, and unspendable outputs. Hidden amounts and assets remain unknown.

**Load outputs** accepts up to 100 distinct transaction hashes. Commas, spaces, or newlines separate hashes; duplicates are removed and the full list is validated before lookup. One credential session and API client serve the batch. The shared lookup budget defaults to five API attempts and 30 seconds per distinct transaction, including authentication and retries. For 10 transactions that means at most 50 attempts and 300 seconds across the batch. A failed lookup preserves the existing starting-output field; a partial result is not applied.

Lookup creates no investigation or trace and follows no subsequent spends. The selected output references are saved when you create the investigation; the later trace fetches and archives its own evidence. Selected UTXOs from all starting transactions share one investigation, board, and set of run limits. Shared descendants are represented once. Continuing a bounded run resumes its saved branches; adding different starting transactions currently requires a new investigation.

For your first trial, select the **offline demo** and use a separate empty Miro test board. Demo hashes and addresses are synthetic. Its full path ends in a synthetic peg-out request, not an actual Bitcoin payout or Avalanche transaction. You can navigate, trace the demo, and preview a Miro plan without credentials. Creating a Miro board and live sync require your access token, even for a demo investigation.

## Where investigations and runs are saved

The menu uses `LIQUID_INVESTIGATIONS_DIR`, configured by devenv as the repository's `cases/` directory. To select another location explicitly, use:

```bash
liquid-trace menu --investigations-dir /absolute/path/to/investigations
```

| Location | Contents |
| --- | --- |
| `cases/settings.json` | Run limits and fee visibility defaults for new investigations. |
| `cases/<investigation>/case.json` | Investigation name, seeds, source, board selection, run defaults, and latest exported run ID. |
| `cases/<investigation>/runs/<run-id>/` | A separate evidence and export directory for each run. |
| `cases/<investigation>/previews/<run-id>-mermaid-<id>/` | Local Mermaid source, SVG/HTML preview, graph details, and node identifiers. |
| `cases/<investigation>/miro/` | Saved mapping between graph objects and items on each Miro board. |
| `cases/<investigation>/miro/board-creation.json` | Board creation request and receipt, retained to recover interrupted creation without duplicating a board. |
| `cases/<investigation>/miro/reports/` | Sync reports identifying the run and actual board used. |

One investigation normally keeps the same board across continuations. Each new run preserves the previous run and its evidence. A run ID distinguishes the snapshots, but the program saves and selects that ID for you. The latest reference survives closing the shell and restarting the computer.

New run exports include an `investigation.json` snapshot of the investigation name and board selection as known when tracing occurred. If you choose or change the board later, the saved case settings and sync reports record that choice; completed run exports are not rewritten. The board ID is case configuration. Actual API credentials remain in the secret provider and are not written into these files.

The default `cases/` directory is ignored by Git. Preserve the whole investigation directory when backing up or moving a case, including its `case.json` and `miro/` mapping. A custom investigation directory has its own storage and Git rules.

## Explicit commands and demo helpers

The menu is the normal starting point. All existing subcommands remain available for scripts and advanced tracing. `liquid-live` loads credentials before running a direct command; the menu handles that step when you select a live action. `liquid-test` runs the offline suite.

For a quick fixture run without the menu:

```bash
liquid-demo
liquid-demo-preview --board 'https://miro.com/app/board/YOUR_TEST_BOARD_ID/'
liquid-demo-sync --board 'https://miro.com/app/board/YOUR_TEST_BOARD_ID/'
```

The preview makes no network calls, needs no token, and writes no state. It cannot detect remote manual edits or deleted items; live sync performs that preflight. Live sync saves the selected board after local validation so subsequent `liquid-demo-preview` and `liquid-demo-sync` commands can reuse it. In a terminal, a missing board can be entered at the first-use prompt. For scripts, supply `--board` or use a case that already has one saved.

`liquid-demo` starts a fresh independent run each time. To extend a published demo graph, resume its lineage instead of creating another independent root:

```bash
liquid-trace trace \
  --case "$LIQUID_DEMO_CASE_DIR" \
  --fixture "$LIQUID_TRACER_ROOT/examples/demo-api.json" \
  --resume latest --additional-hops 1
liquid-demo-preview
liquid-demo-sync
```

A new independent root cannot overwrite an already published graph's lineage. The interactive menu avoids this manual selection by offering continuation within the saved investigation.

If needed, add `--offline-preview` to `trace` or `export` to also save an HTML inspector and SVG. These are optional inspection files; normal runs use Miro for visual review.

To create a Mermaid chart directly from a saved investigation:

```bash
liquid-trace mermaid --case cases/theft-liquid --open
```

The command selects `latest` automatically. Optional `--run RUN_ID` selects a historical snapshot; `--out NEW_DIRECTORY` chooses a new destination. `--include-fees` and `--exclude-fees` override visibility for this preview only. Each invocation verifies the saved evidence before rendering and leaves archived runs and Miro mappings unchanged. The existing `devenv.nix` supplies Mermaid CLI and Chromium on Linux; leave an older shell and enter `devenv shell` after updating the project.

Mermaid preserves the graph's arrows, shapes, colors, dates, and compact quantity labels, but computes its own left-to-right layout. Fixed Miro connection sides, manually arranged positions, and the chronological fee row are not copied. Large cumulative graphs can still take time to lay out; rendering stops after 120 seconds and keeps `graph.mmd` if it fails. The generated HTML is self-contained and can be viewed offline. Full identifiers remain in `graph.json` and `mermaid-node-map.json` beside the chart.

Outside devenv, Python 3.11+ works with `python3 -m liquid_tracer`; use explicit paths and install SecretSpec and its provider CLI before selecting live menu actions. Install the package with its interactive interface using `python3 -m pip install -e '.[tui]'`, or use `python3 -m pip install -e .` for explicit commands only.

Public defaults and command definitions live in `devenv.nix`. The package input is pinned in `devenv.yaml`; commit the generated `devenv.lock` after the first successful shell build. See [development and secrets](docs/development.md) for Proton Pass setup and optional environment overrides.

## Configure the paid Blockstream API

The project defaults to the SecretSpec `protonpass` provider and `development` profile. Devenv supplies SecretSpec 0.19.1 and Proton Pass CLI 2.3.2 from the existing pinned package input. The launchers use these exact executables, avoiding an older system SecretSpec that calls the removed `pass-cli test` command. Sign in with `pass-cli login` if needed, then use this helper inside `devenv shell` to store the Blockstream credentials. It prompts for each value, so credentials do not appear in the command or shell history:

```bash
liquid-secrets-setup blockstream
```

If your credentials are already saved, verify the local tool versions and credential delivery without displaying their values:

```bash
liquid-toolchain-check
liquid-secrets-check
```

The first command checks executable versions and support for `pass-cli info` without accessing a vault. The second resolves the project secrets through SecretSpec and reports whether the two Blockstream values reached Python. Use `liquid-secrets-check --service miro` for the Miro token, or `--service all` for all three values. Missing values return a nonzero exit status. These checks do not contact Blockstream or Miro; live API authentication is a separate check.

The interface retrieves these credentials when you select a live trace. For direct API commands, use `liquid-live`; it retrieves the values from the same provider and profile when the process starts and provides the environment variables the Python client already expects. The public `secretspec.toml` contains names and descriptions only. If the credentials are already stored for this project in Proton Pass's `development` profile, skip setup. The [development guide](docs/development.md) covers CLI compatibility and alternative providers. The Python application itself does not read `.env` files.

The default base is `https://enterprise.blockstream.info/liquid/api`. The client exchanges your credentials for a bearer token and refreshes it before expiry. These settings follow [Blockstream's authentication documentation](https://help.blockstream.com/blockstream-explorer-api/use-explorer-api/make-a-rest-api-request-with-your-api-keys). No credentials were supplied or used while developing this project.

## Trace a case with explicit commands

The menu supplies the selected investigation path automatically. When scripting, supply its case directory with `--case`. The examples below use `cases/theft-liquid`; replace that with the directory printed by the menu, or use it to start a case directly. An existing `LIQUID_CASE_DIR` setting remains a fallback when `--case` is omitted.

For a first live trial, select **one exact Liquid outpoint** and use small budgets:

```bash
liquid-live trace \
  --case cases/theft-liquid \
  --seed 'YOUR_64_CHARACTER_LIQUID_TXID:0' \
  --hops 1 \
  --max-transactions 20 \
  --max-outpoints 100 \
  --max-requests 30 \
  --max-seconds 60
```

For multiple seeds, create `case-seeds.txt` with one outpoint per line:

```text
# Replace the placeholder with a real Liquid transaction hash.
YOUR_64_CHARACTER_LIQUID_TXID:0
```

An outpoint is a transaction hash plus a zero-based output index. Seed only the relevant outputs. Seeding every output of a funding transaction would also include its unrelated recipients and change.

Replace `--seed ...` with `--seeds-file case-seeds.txt`, or repeat `--seed 'TXID:VOUT'`. A Bitcoin deposit into Liquid must first be linked to its Liquid transaction/output; a Bitcoin txid alone is not a Liquid seed. This version intentionally does not expand complete address histories.

| Limit | Meaning |
| --- | --- |
| `--hops 1` | Seed outputs are hop 0; follow at most one spending transaction from them. |
| `--max-transactions 20` | At most 20 newly added transactions this run, including seed funding transactions. |
| `--max-outpoints 100` | At most 100 output examinations this run, including terminal outputs. |
| `--max-requests 30` | At most 30 HTTP attempts, including token requests and retries. This is **not a count of Blockstream credits**. |
| `--max-seconds 60` | Stops traversal and bounds HTTP timeouts. Checkpoint/export filesystem work can finish afterward. |

GET requests are spaced by `--min-interval`, default 0.25 seconds. Transient server errors retry at most three times, subject to the same budget. One outspends response serves all outputs of its transaction within a run.

## Continue after review

In the menu, choose **Continue investigation**, select the case, and continue its latest run. The equivalent direct command is:

```bash
liquid-live trace \
  --case cases/theft-liquid \
  --resume latest \
  --additional-hops 1 \
  --max-transactions 20 \
  --max-outpoints 100 \
  --max-requests 30 \
  --max-seconds 60
```

This creates a **new** run, preserves the parent, increases the first trial's absolute hop ceiling from 1 to 2, and reconsiders its unfinished branches. Request, time, transaction and output budgets reset for each run. The snapshot and graph are cumulative.

`latest` resolves the run ID recorded in that case's `case.json`; it does not guess from file timestamps. The pointer advances only after a run's exports are saved, including runs that paused at a limit or recorded an error. Existing cases without the pointer need an explicit `--resume RUN_ID`. For a reproducible selection of a particular snapshot, use its explicit ID instead of `latest`.

To finish a run stopped by its request budget without increasing depth, use `--resume latest` without `--additional-hops`, retaining the desired budgets. To extend selected branches, repeat `--only 'TXID:VOUT'`. Unselected frontier entries remain documented. Any new spending transaction exposes all of its outputs as candidates, even during selective continuation.

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

Obtain an access token with **`boards:read` and `boards:write`** scopes and access to your Miro team. Follow [Miro's REST API quickstart](https://developers.miro.com/docs/rest-api-build-your-first-hello-world-app). Store the token with the setup helper. If it expires, replace it and retry the selected action; this program does not refresh Miro tokens automatically.

```bash
liquid-secrets-setup miro
```

In the investigation menu, choose **Create Miro board**. The name defaults to the investigation name, limited to 60 characters; demo names start with `SYNTHETIC DEMO`. Visibility defaults to **Private**. **Team members can edit** enables team access; public-link and organization access remain private. An optional Miro team ID selects a destination team. Availability depends on your Miro plan and team permissions; a rejected private request does not automatically become team-visible. Creation uses [Miro's board endpoint](https://developers.miro.com/reference/create-board-1) and its `boards:write` scope.

Confirming creation retrieves the token through SecretSpec, creates an empty board, and saves its ID in `case.json`. The board's name, URL, and creation receipt are saved under `miro/board-creation.json`. The menu then reuses that board for previews and syncing. Opening or cancelling the form does not load credentials. For the same action from the CLI:

```bash
liquid-live miro-create-board --case cases/theft-liquid
liquid-trace miro-sync --case cases/theft-liquid --dry-run
liquid-live miro-sync --case cases/theft-liquid
```

Creation accepts `--name TEXT`, `--visibility private|team`, and `--team-id ID`. Repeating the command reuses a linked board or a saved successful creation receipt. If a connection failure leaves the outcome uncertain, it blocks another creation request. Inspect Miro, then link the created board through **Investigation settings**. If no board exists, create one in Miro and link it there. Completed run evidence remains unchanged.

To use an existing board, save its URL or ID through **Investigation settings**, or pass it directly:

```bash
liquid-trace miro-sync --case cases/theft-liquid --dry-run \
  --board 'https://miro.com/app/board/YOUR_CASE_BOARD_ID/'

liquid-live miro-sync --case cases/theft-liquid \
  --board 'https://miro.com/app/board/YOUR_CASE_BOARD_ID/'
```

The menu uses the selected investigation and its saved board. For direct sync commands, board selection is resolved in this order: explicit `--board`, the case's saved board, legacy `LIQUID_MIRO_BOARD`, then a first-use prompt in an interactive terminal. A noninteractive command with no board exits with an actionable error. `--run` defaults to the latest saved run; use `--run RUN_ID` to select a particular snapshot.

A dry run does not save a new board selection. Live sync saves it in `case.json` after local validation and before contacting Miro, so a network failure can be retried with the same selection. After saving it once, the direct commands become:

```bash
liquid-trace miro-sync --case cases/theft-liquid --dry-run
liquid-live miro-sync --case cases/theft-liquid
```

A configured board alone does not publish anything during tracing; run sync after reviewing the exports.

Normal **Preview Miro** and **Sync to Miro** rebuild the current graph presentation in memory from the verified saved trace and current fee setting. This applies display changes without another Blockstream request or changes to archived run files. Live sync updates managed labels and colors while retaining manual edits and positions. Reports record the archived and rendered plan hashes and presentation version. An explicit `--plan` continues to use that verified plan as supplied.

Transaction squares show the recorded block date as `YYYY-MM-DD UTC`, using Esplora's saved `status.block_time` for confirmed transactions. This is the containing block's date, not an exact transaction creation time. Unconfirmed transactions show `Unconfirmed`; missing or invalid confirmed dates show `Date ??`. These labels reflect the saved observation. To add dates to an existing board, reopen the investigation and choose **Sync to Miro**; no new trace is needed. Manually edited transaction labels are preserved and reported as conflicts instead of being overwritten.

To reorganize an existing board, choose **Organize Miro graph** from the investigation menu. Review the action and confirm it to arrange the managed graph from left to right using the saved run. This explicitly changes positions and transaction connector attachment sides; normal sync preserves your manual arrangement. Existing dimensions and manual annotations remain intact. Previous coordinates and changed connector attachments are recorded with the sync state for review. The operation considers mapped items; it cannot guarantee separation from unrelated content elsewhere on the board.

The equivalent direct command is:

```bash
liquid-live miro-sync --case cases/theft-liquid --reorganize
```

`trace`, `export`, and `miro-sync` accept `--include-fees` or `--exclude-fees` as an explicit display override. Otherwise they use the selected case's setting, defaulting to excluded. These flags do not alter tracing or erase fee evidence. A supplied `--plan` has its own reviewed fee selection; regenerate an export to change it instead of combining that plan with a fee override.

Use the **same case directory and board** for later runs. Sync creates native [shapes](https://developers.miro.com/reference/create-shape-item-1) and [connectors](https://developers.miro.com/reference/create-connector-1), checks existing items, and updates compatible managed fields. It does not call Blockstream. A per-board state file under `case/miro/` maps stable graph IDs to remote item IDs, and `case/miro/reports/` retains sync reports separately from immutable run exports.

- Repeating the same run creates no duplicate acknowledged objects. A newer continuation adds new objects and a run note; already mapped circles and squares are reused.
- Normal sync preserves existing positions, dimensions, and connector attachment choices. New items from the current layout are placed relative to connected mapped items and avoid mapped shape bounds. Explicit **Organize Miro graph** changes managed positions and applies the current plan's transaction attachment sides while preserving dimensions. Legacy plans retain their original placement and attachment behavior.
- Keep mapped shapes on the board canvas. Items with frame/group-relative coordinates stop sync before writes; nested layouts are not supported in this version.
- Existing content, captions and styles are updated only where the current value still matches the program's saved baseline. Analyst edits are retained and listed as conflicts in the report. Avoid simultaneous content/style edits during a live sync: the API check and update are separate requests.
- Excluding fees removes only generated fee connectors and diamonds identified from the saved trace. Edited fee labels, captions, or managed styles stop removal before board writes. Extra comments and unmapped connectors attached to fee diamonds are outside those checks; retain fees when these annotations need to remain attached. Enabling fees again recreates their representations. Other mapped objects are retained; an unexpectedly missing object or changed connector endpoint stops sync for inspection. Interrupted fee removals retain recovery state.
- A board mapping is bound to one case, API source, and address mode. After a run has been synced, extend that run (or a later descendant). Older snapshots, sibling branches and independent roots cannot overwrite newer graph classifications. You may skip intermediate unpublished runs.
- Finish an interrupted sync before switching to another run. Resolving a pending creation alone does not finish that sync.
- The default cap is **750 new shapes plus connectors per sync**, not total board size. Use `--max-new-items` to change it. Existing-object checks can still take time on a large graph.

Keep **`case.json` and the entire `miro/` directory** with the case. Losing or replacing the mapping can cause duplicates; do not delete state as a retry mechanism. Live sync changes board content, without changing board sharing or inviting people.

To trace and sync in one command:

```bash
liquid-live trace \
  --case cases/theft-liquid \
  --resume latest --additional-hops 1 \
  --max-transactions 20 --max-outpoints 100 --max-requests 30 --max-seconds 60 \
  --miro-board 'https://miro.com/app/board/YOUR_CASE_BOARD_ID/'
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
| `investigation.json` | Investigation name and board selection as known at trace time; included in new runs. |
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

Tests cover seed precision, hop boundaries, split/merge paths, continuation, budgets, spend-status freshness, confidential fields, event stops, reference validation, labeling, graph identity, evidence checksums, OAuth refresh, board creation and persistence, incremental Miro updates, manual-edit preservation, retries, and uncertain creation recovery. Automated API checks use synthetic fixtures and mock transports. Live authentication and publication are verified locally with the operator's credentials.

Source separates the API client, evidence store, tracing engine, export, Miro publication, command parsing, and interactive investigation workflow. The CLI is separate from tracing, so a notebook or case-management interface can call the same engine later.
