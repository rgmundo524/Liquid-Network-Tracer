# Local development and API secrets

The project uses **one main `devenv.nix`** to define Python, the Textual terminal interface, Node.js 24 for the Astro interface and local ELK layout engine, Mermaid CLI, commands, and nonsecret environment defaults. `secretspec.toml` declares credential names. Proton Pass stores their values. Investigation names, board IDs, run limits, and the latest-run reference belong in the saved investigation files.

Entering the environment and opening the interface do not access a secret provider. Selecting a live output lookup, trace, Miro board creation, or Miro sync retrieves credentials through SecretSpec for that action. The offline demo trace, navigation, and local previews need no credentials.

Explorer fetching uses up to eight concurrent requests with one shared rate limiter. The existing `devenv.nix` sets `env.LIQUID_BLOCKSTREAM_ENTERPRISE_RPS = "49"` for the paid endpoint. All interfaces inherit this actual operating target on their next launch, with at least 20.408 ms between request starts across workers. It is public configuration, so no new Proton Pass entry is needed. This selected target is not a published Blockstream quota. Public and other endpoints retain the 4 requests/second fallback. A verified allowance in `env.LIQUID_BLOCKSTREAM_API_RPS` takes precedence and uses 95% of that allowance instead. Credit balance and hourly/monthly request volume are not RPS limits. Separate processes do not share this limiter, so divide the desired total rate if running live investigations simultaneously. See [fetching controls and run limits](../README.md#trace-a-case-with-explicit-commands).

## Start the environment and interface

From the repository directory:

```bash
devenv shell
liquid-trace
```

In an interactive terminal, the command opens the Textual interface. Use the keyboard or mouse to select **New investigation**, **Continue investigation**, **Settings**, or **Exit**. The explicit launcher is:

```bash
liquid-trace menu
```

On a focused button, **↑ ↓ ← →** moves to another enabled button and **Enter** activates it. Left/right prefers the same row; up/down prefers buttons aligned above/below. When that direction has no candidate, navigation falls back to the previous/next button and stops at the ends. Focused buttons scroll into view. **Tab** and **Shift+Tab** still move between all controls; use them to enter or leave a text field or table. Arrows inside inputs, multiline text, dropdowns, and tables retain their native editing/selection behavior. **Space** toggles checkboxes and **Esc** returns to the prior screen. Footer hints appear when a button is focused.

Creating an investigation asks for a name, live or synthetic-demo source, starting outputs, an optional existing Miro board URL or ID, and numeric run limits. Enter known outputs as `HASH:NUMBER` in the multiline field, separated by whitespace or commas. `vout` is the numeric output index, not a word to enter: `:0` selects the first output and `:1` the second. To look up outputs, paste transaction hashes separated by commas into **Transaction hashes**, choose **Load outputs**, toggle the relevant rows across transactions with Enter, and choose **Use selected outputs**. The picker identifies each transaction and output separately, including outputs with the same index from different transactions. Nothing is preselected; applying a selection replaces the starting-output field. The offline demo uses its original sample seeds when this field is empty.

Lookup accepts up to 100 distinct hashes, validates the full list, and deduplicates it before requesting data. One shared client uses a batch budget of five API attempts and 30 seconds per distinct hash by default, including authentication and retries. Ten hashes therefore share a 50-attempt, 300-second maximum. Explicit CLI limits apply to the whole batch. The single-hash command retains its five-attempt, 30-second defaults.

Live credentials load only after selecting **Load outputs**, with the terminal available for provider prompts. Lookup follows no spends, saves no case, and uses a temporary report removed after loading the picker. A failure preserves the existing selection; no partial report is applied. Fees, peg-outs, and unspendable outputs cannot be selected. Unavailable amounts and assets display as `??`. Creating the investigation saves all selected references in one case directory; its bounded trace then fetches and archives evidence under shared run limits. Within that investigation, start or continue a run, review saved information, preview or sync Miro, or change its settings.

A case normally keeps the same board as its graph grows. If no board is linked, choose **Create Miro board** from the investigation menu. Review the name, visibility (Private by default), and optional team ID, then create it. SecretSpec supplies the Miro access token only after confirmation. The returned board ID is saved with the case, and its URL appears in the menu. **Preview Miro** and **Sync to Miro** use that selection and the saved run; there is no need to repeat tracing. You can also link an existing board through **Investigation settings**.

Board selection, run limits, and the **Include transaction fee flows** checkbox are saved with the case, so you do not need a board environment variable or a separate Nix file. Fees are excluded by default, including for older cases without that setting. The top-level Settings screen changes defaults for future investigations. Existing cases retain their saved settings.

**ELK layout preview** uses the selected saved run and current display settings to calculate an arrangement without contacting a secret provider or any API. It opens a self-contained HTML/SVG view with estimated before/after quality counts and full graph details. The default Miro plan uses the same local layout engine. This is a preview of proposed geometry; it does not read the board's current positions.

**Mermaid chart** creates a local preview from the latest verified run without opening the secret provider. It is enabled once a run has been saved and does not require a Miro board. Rendering runs in the interface's background worker and opens the generated HTML in your browser; the result also shows its path. Each click creates `previews/<run-id>-mermaid-<id>/` inside the case, containing `graph.mmd`, genuine Mermaid-rendered `graph.svg`, a self-contained `graph.html`, full `graph.json` details, a node-ID mapping, and renderer configuration. The saved address mode and current fee choice apply; archived runs and Miro publication state are not modified.

Mermaid provides its own automatic left-to-right layout. It retains the graph's directions and styling, including starting-transaction color priority and recorded dates, but does not copy Miro positions, fixed connection sides, or its chronological fee row. The local chart is a quick view; use Miro for interactive arrangement and annotations.

**Export CSV**, beside **Mermaid chart**, saves seven CSV tables from the latest run into a new `exports/<run-id>-csv-<id>/` directory in the case. The menu displays the directory and every CSV path. It uses the same offline worker and saved-run selection, requires no board or secret-provider session, and leaves the original archive and case settings unchanged. The original run's CSVs remain available under `runs/<run-id>/` as well.

Choose **Sync and reorganize graph** to sync the selected saved run and apply an ELK arrangement to existing and new managed objects together. It changes positions, connector appearance, and attachment points; cancelling makes no API call. Normal sync preserves manual positions and connector choices and can stop before writes if those anchors leave no space for the continuation. Reorganization does not require a separate sync afterward or reserve space for an untraced future run. Included fees remain chronological in a separate row above the flow. Repeated addresses appear as separate UTXO occurrences by default; merged address cycles can require return edges.

Starting transaction squares are purple, determined by membership in the saved seed transaction set rather than hop depth. A provided transaction keeps that color even when another provided transaction leads to it. Subsequent transaction squares are blue. ELK Layered calculates spacing and orders distinct connection points along transaction sides: inputs attach on the left and outputs on the right. The same ports appear in the ELK SVG preview and new Miro connectors. **Connector appearance** is saved as `connector_style`, with `straight` as the default and `curved` or `elbowed` as alternatives. Backward or obstructed straight connections use routed exceptions where needed; exact Miro paths may differ.

For a command without entering an interactive shell:

```bash
devenv shell -- liquid-trace menu
devenv test
```

Use a current devenv release. The project supplies its own pinned SecretSpec and Proton Pass CLI packages instead of relying on an inherited system or bundled SecretSpec. `devenv.yaml` fixes the nixpkgs input to an explicit revision. The first build generates `devenv.lock`; commit it after a successful build. To upgrade the pinned package input, deliberately change its revision and run `devenv update`, then `devenv test`.

| Command | Behavior |
| --- | --- |
| `liquid-trace` or `liquid-trace menu` | Opens the investigation interface. Retrieves credentials only for a selected live action. |
| `liquid-web` | Builds and opens the local Astro interface at `http://127.0.0.1:4321`, using the same investigations and secrets workflow. |
| `liquid-web-build` | Installs locked ELK/frontend dependencies when needed and builds the local interface without starting its server. |
| `liquid-layout-setup` | Installs the locked local ELK dependency when absent or its manifest/lockfile changes. |
| `liquid-trace layout-preview --case CASE --run latest --open` | Creates and opens an offline SVG/HTML preview of the proposed ELK arrangement. |
| `liquid-trace SUBCOMMAND ...` | Runs an explicit command using the existing process environment. |
| `liquid-live SUBCOMMAND ...` | Resolves project credentials through SecretSpec, then runs an explicit command. |
| `liquid-live inspect-tx --txid HASH` | Looks up one live transaction and prints its output numbers, addresses, and available public quantities as JSON. Does not trace spends or create a case. |
| `liquid-live inspect-txs --txids 'HASH1,HASH2'` | Looks up a batch through one API client and shared limits, returning outputs grouped by transaction. |
| `liquid-live miro-create-board --case CASE` | Creates an empty Miro board and saves it with the investigation. Uses the case name and private visibility by default. |
| `liquid-live miro-sync --case CASE --reorganize` | Syncs the selected saved run and applies ELK positions and connector choices, retaining dimensions and manual content. |
| `liquid-secrets-setup [all\|blockstream\|miro]` | Prompts for selected credentials and stores them in the configured provider; defaults to all three. |
| `liquid-toolchain-check` | Reports the pinned SecretSpec/Proton CLI versions and checks support for `info`, without accessing a vault. |
| `liquid-secrets-check [--service blockstream\|miro\|all]` | Loads project secrets and reports presence only; defaults to Blockstream. No Blockstream or Miro calls. |
| `liquid-demo` | Creates a fresh synthetic one-hop run under `LIQUID_DEMO_CASE_DIR`; no paid requests or credentials. |
| `liquid-demo-preview [--board URL_OR_ID]` | Locally previews the latest demo run for the selected or saved board. |
| `liquid-demo-sync [--board URL_OR_ID]` | Loads credentials through SecretSpec and syncs the latest demo run to the selected or saved board. |
| `liquid-test` | Runs the offline Python test suite, including terminal and browser-backend tests. |

The launchers locate the source and secret declaration using `LIQUID_TRACER_ROOT`, preserving your working directory for relative case paths. The ordinary command-line tracing engine uses Python's standard library. The terminal interface uses Textual, provided by devenv; outside devenv, install it with `python3 -m pip install -e '.[tui]'`.

## Local Astro interface

From the same devenv shell, start the alternative browser interface with:

```bash
liquid-web
```

The launcher runs `liquid-layout-setup` for the local ELK worker, installs frontend dependencies with `npm ci` when absent or the package manifest/lockfile changes, then builds the Astro assets before starting the Python server. The initial installation needs package-registry access; subsequent launches reuse installed dependencies. Node.js 24 and npm come from the existing pinned devenv input, and the frontend's package lock is committed. No additional `devenv.nix` or per-case environment variables are needed.

The default address is [http://127.0.0.1:4321](http://127.0.0.1:4321). Keep the terminal running while using the browser. Launch options are:

```bash
liquid-web --no-open
liquid-web --port 4322
liquid-web --root /absolute/path/to/investigations
liquid-web --help
```

`--no-open` leaves browser launch to you; `--help` returns without installing packages or building. The root defaults to the same `LIQUID_INVESTIGATIONS_DIR` used by the terminal interface. Cases, global defaults, run limits, seeds, latest-run pointers, immutable archives, exports, and Miro mappings use the existing Python formats. Switching interfaces requires no migration.

The browser supports new live or synthetic-demo investigations, comma-separated transaction lookup and grouped UTXO selection, bounded tracing and continuation, saved-run review, ELK and Mermaid previews, CSV downloads, and Miro board creation, plan preview, sync, and reorganization. Case settings retain the board, fee-flow checkbox, and connector appearance; global settings apply to future cases. A historical run selection controls review and exports. Continuation follows the case's latest saved run so it extends the current lineage.

ELK and Mermaid **Download SVG** buttons, layout reports, and Mermaid source links live outside the sandboxed preview iframe. CSV tables each have a download button. The case-detail endpoint rediscovers the newest complete product of each kind for each saved run, checking the case/run identity and allowed nonsymlink files. Partial rendering attempts cannot hide a completed product. Downloads survive browser or server restarts; changed display settings are shown as a mismatch with a regenerate action. Artifacts remain in their original `previews/` and `exports/` directories, and immutable run archives are untouched.

Opening the browser and performing local actions do not resolve secrets. Live jobs invoke the same SecretSpec command, project manifest, provider, and profile as the terminal workflow. Proton Pass login or unlock prompts appear in the launching terminal. API credentials are never form fields or frontend build variables. A demo trace is offline; publishing its results to Miro still requires a Miro token and network access.

One job is active per server at a time. The browser polls its status while work continues in the background. Closing the tab does not interrupt that job; reopening the local address reconnects to the running server. Pressing Ctrl+C shuts down the server and any active offline worker. While a live action owns the terminal for provider prompts, the first Ctrl+C cancels that action and returns control to the server; press Ctrl+C again to stop the server. Saved case files remain the source for later sessions. Incomplete live Miro operations use the same recovery and mapping rules as CLI operations; review the next preview before retrying an interrupted sync.

Miro sync emits optional phase/count callbacks. The CLI writes throttled progress to stderr, keeping stdout as a single JSON result. The web worker also writes an atomic, mode-0600 progress file in its private temporary job directory; the server polls it while the child runs. Only validated phases, counts, retry reasons, and bounded retry delays reach the browser. Provider output, remote item content, exceptions, and credential values are never used as progress messages. Reporting failures cannot interrupt durable Miro mapping writes. Progress files are disposable, not investigation evidence.

Repeat sync retains the complete remote preflight before mutations. Up to four requests overlap while one shared gate spaces request starts: 0.05 seconds for reads and 0.1 seconds for writes by default. This overlaps network latency without multiplying the request allowance per worker. Direct Miro connections are reused; environments using an HTTP proxy keep the existing proxy-aware transport. HTTP 429 waits and retryable read errors appear explicitly in progress, and workers share rate-limit cooldowns. Other Liquid Trace instances can still consume the same Miro user/app allowance.

Existing-item reads and edits can run concurrently. Creation and fee deletion stay serial, with their existing recovery rules. Before edits start, the intended updates are saved locally; each acknowledged mutation is checkpointed by the coordinating thread. If one request fails, already-running acknowledgments are collected and saved before the error is returned. A subsequent sync reads the board again to reconcile incomplete updates. Unchanged mapped items avoid redundant per-item fsync calls. Ordinary sync continues to preserve manual positions and edits; explicit reorganization still reasserts transaction connection points, even when their returned coordinates look unchanged.

Astro builds static interface assets, served locally by the Python backend alongside its restricted action API. Tracing runs through the existing CLI and keeps the same validation, budgets, case locks, and evidence export. The backend binds to `127.0.0.1`, checks Host and Origin, and protects writes with a per-server request token. It serves approved case artifacts through local download routes; the cases directory is not a static website folder. This is a desktop interface, not a shared or publicly deployed investigation service. Runtime frontend assets and fonts do not use a CDN. Miro remains online; local charts use the ELK geometry exporter or the independent Mermaid CLI renderer.

After editing the frontend, stop the server and run `liquid-web` again to rebuild and reopen it. Build without starting the server, then run the full checks with:

```bash
liquid-web-build
devenv test
```

`devenv test` includes the Astro check/build, Python backend and terminal tests, pinned toolchain checks, real synthetic ELK layout calculations, and a genuine synthetic Mermaid render. The suite uses no real API credentials or investigation data. `liquid-test` runs just the Python suite when frontend files have not changed.

## Reproduce the Miro sync benchmark

Run the synthetic benchmark from the repository root:

```bash
python3 scripts/benchmark_miro_sync.py
```

It compares one and four workers using the same 12 synthetic transactions, 51 native shapes/connectors, and 150 ms of simulated latency per request. Each timed reorganization reads all 51 items before any write, moves every shape, and reapplies every connector's fixed ports: 51 GETs and 51 PATCHes. Initial board creation is excluded from timing. Actual publication-state writes and fsyncs remain included. JSON output reports elapsed and phase times, maximum simultaneous requests, and checksums proving that both worker counts produced identical board items and request payloads. The script fails if the results differ, preflight overlaps writes, or publication state is incomplete.

For a different latency or an older source checkout:

```bash
python3 scripts/benchmark_miro_sync.py --workers 1 4 --latency .25
python3 scripts/benchmark_miro_sync.py --source-root /path/to/older-checkout --workers 1
```

Omitting `--interval` uses that source version's default pacing; supplying `--interval .1` compares at the same configured write interval. The fake transport is entirely in memory and never opens a network connection or accesses credentials. Timing varies with the machine and filesystem. These measurements isolate request scheduling and state checkpointing; they do not measure ELK calculation, TLS connection reuse, Miro throttling, or the board application's rendering speed. They are a reproducible comparison, not a promise of live-board completion time.

## Local ELK layout

`layout/package-lock.json` pins **elkjs 0.12.0**. The single devenv provides Node.js and runs `liquid-layout-setup` on shell entry and before browser builds, installing only when its package manifest/lockfile changes. Installation needs registry access once; layout calculations run locally with no network calls or secrets. To install outside devenv, use Node.js 22.12 or newer and run this from the repository root:

```bash
npm --prefix layout ci --ignore-scripts
```

After pulling changes into an already open devenv shell, run `liquid-layout-setup` or re-enter the shell. The ELK worker has a 30-second process timeout and graph-size limits. It calculates three deterministic candidates for graphs of at most 300 objects, or one for larger graphs, and selects by object overlaps, lines through objects, line crossings, then total connection length. These are bounded estimates, not a guarantee of a global optimum. Truncated counts are lower bounds and appear with `≥` in the local preview.

ELK controls layering, connection-point ordering, and proposed routes. Transaction inputs use left-side ports and outputs use right-side ports. Fees are ordered by saved chain chronology in a row above the main flow. Straight is the default connector appearance; returns and obstructed connections may use elbowed exceptions. Miro receives native shapes, endpoint positions, and connector appearance, but controls its own automatic route between those endpoints. Label overlaps and exact Miro curves are not scored.

The quality report compares the verified saved graph's baseline layout with the proposed ELK result, not the live board. An explicit reorganization can expand or shift the proposed arrangement after reading resized or rotated mapped objects. Merely opening a preview never changes Miro. Normal sync preserves the existing arrangement; **Sync and reorganize graph** applies the selected saved run and rearranges its managed objects in one operation.

Create a portable local preview directly:

```bash
liquid-trace layout-preview --case cases/theft-liquid --run latest --open
```

The command writes `graph.html`, `graph.svg`, `graph.json`, and `layout-report.json` under a fresh `previews/<run-id>-elk-<id>/`. HTML is published last as the completion marker. `--run`, `--out`, fee flags, and `--connector-style` allow a different saved snapshot or presentation. Existing destinations and output paths under `runs/` are rejected. SVG retains separate physical connections; full identifiers and metadata remain in graph JSON. The archive's original graph, evidence, and checksums remain unchanged. CSV exports and Mermaid use their existing independent presentation paths and do not require ELK to render.

## Saved investigations and settings

The main environment defines `LIQUID_INVESTIGATIONS_DIR` as `${config.devenv.root}/cases`. The interface stores run limits, fee visibility, and connector appearance defaults for future investigations in `cases/settings.json`. Each newly named investigation receives its own unique subdirectory.

| Path within an investigation | Purpose |
| --- | --- |
| `case.json` | Name, seeds, source, chosen board, run defaults, and latest exported run ID. |
| `runs/<run-id>/` | Evidence, CSVs, tracing checkpoint, graph plan, and checksums for one run. |
| `runs/<run-id>/investigation.json` | Name and board selection as known when that run was traced. |
| `miro/` | Per-board mapping that keeps graph items stable across continuations. |
| `miro/board-creation.json` | Board creation request, status, and returned ID/URL for reuse and recovery. |
| `miro/reports/` | Publication reports recording which run was synced to which board. |

Run IDs are saved references, not variables you must remember or re-enter. The latest pointer advances after the exports finish successfully, including saved runs paused by a budget or an error. It does not depend on shell history or directory timestamps. A continuation preserves its parent and records a new snapshot. Settings or a new board chosen later do not rewrite completed run exports.

Default Miro preview/sync verifies the archived export, then rebuilds its presentation in memory from `trace.json` using the saved address mode, current fee setting, and connector appearance, and runs the local ELK optimizer. It checks that the case, source, run, and all non-fee graph identities and endpoints still match before contacting Miro. Only fee representations proven from the saved outputs may be added or excluded. Completed run files stay byte-for-byte unchanged; sync reports record both plan hashes, presentation version, and applied display options. Explicit `--plan` uses the supplied verified export without rebuilding it.

Fee removal applies only to mapped generated fee connectors and diamonds. The publisher checks their managed fields before removal and saves deletion intent for recovery if interrupted. Other graph objects remain intact. Fees still appear in the full outputs, events, and raw evidence, and can be included again by changing the checkbox and syncing.

The explicit reorganization action records previous positions and changed connector appearance/attachments before updating items. It applies the proposed ELK positions and ports, uniformly expanding the arrangement when current resized or rotated objects need more space. Manual text and color annotations are preserved; metrics still describe the proposed local layout. It replaces manual positions and connector routing choices. Normal sync anchors new graph items near connected mapped items without relocating existing ones. Mapped items must use canvas coordinates; unrelated board content is not included in collision checks.

Miro can return an attachment's percentage coordinates without revealing whether its mode is automatic or fixed. **Sync and reorganize graph** reasserts the fixed side when the mode is unknown, including after a manual reset to automatic attachment at the same point. Repeating this explicit action can therefore repeat connector updates; it does not create duplicate objects. Ordinary sync still retains existing attachment choices.

All initial UTXOs share one tracing budget and board. A continuation extends the saved seed set; it does not add new starting transactions. To trace a different starting set, create a new investigation and paste the complete comma-separated transaction list.

The default investigation root is ignored by Git. Keep the whole case directory together when moving or backing it up. Use a different investigation root with an explicit launch argument if needed:

```bash
liquid-trace menu --investigations-dir /absolute/path/to/investigations
```

## Direct commands for scripts

The interface handles investigation selection, saved defaults, and runtime credentials. Direct subcommands remain available when you want explicit flags or automation. The examples below use a case path chosen once for the script; substitute the directory printed by the interface.

For an initial live trace:

```bash
liquid-live trace --case cases/theft-liquid \
  --seed 'LIQUID_TXID:OUTPUT_INDEX' --hops 1 \
  --max-transactions 20 --max-outpoints 100 --max-requests 30 --max-seconds 60
```

`trace`, `export`, `mermaid`, `layout-preview`, `csv-export`, and `miro-sync` accept `--include-fees` and `--exclude-fees`. These are presentation overrides; the underlying trace and fee evidence are unchanged. Without a flag, the case's `run_defaults.include_fees` applies, defaulting to `false`. `layout-preview` and `miro-sync` also accept `--connector-style straight|curved|elbowed`. A verified explicit `--plan` cannot be combined with fee or connector-style overrides; regenerate a plan when a different selection is needed.

Export CSV tables without querying an API or reading the case database:

```bash
liquid-trace csv-export --case cases/theft-liquid
```

The command defaults to `--run latest` and chooses a new directory automatically. Optional `--run RUN_ID` selects a historical snapshot, and `--out NEW_DIRECTORY` selects an explicit destination. It prints JSON containing the run ID, fee choice, directory, and CSV paths. Existing paths and destinations inside the case's `runs/` are rejected.

The graph tables, `nodes.csv` and `edges.csv`, are regenerated from the verified saved trace using the program's current display format and recorded transaction dates. Labels and address mode come from that saved run; fee visibility uses the current case setting or explicit override. Full node/edge IDs and relationships remain available even when a display label is shortened. The five detailed tables, `inputs.csv`, `outputs.csv`, `spends.csv`, `events.csv`, and `frontier.csv`, are copied byte-for-byte only after their captured bytes match explicit entries in the archive's checksum manifest. They always retain all recorded fees. Empty numeric CSV cells indicate unavailable values, while compact graph captions use `??`; nested structures remain JSON in CSV cells.

`export.json` records the case/run, source archive and file hashes, address mode, presentation version, and graph options. A new `SHA256SUMS` covers the complete CSV bundle and provenance file. These checksums detect changed bytes; they do not provide an independent signature or timestamp. The existing `export` command remains available when a complete evidence bundle, including raw observations and Miro plan, is wanted instead of CSV tables alone.

Create a local Mermaid preview with the same defaults as the menu:

```bash
liquid-trace mermaid --case cases/theft-liquid --open
```

Omit `--open` to render without launching a browser. `--run RUN_ID` selects an older snapshot and `--out NEW_DIRECTORY` chooses an explicit destination; existing directories and destinations inside the case's `runs/` are rejected. The command prints JSON with the selected run, file paths, fee visibility, and whether the browser launch succeeded. A missing desktop browser does not fail a completed export. Rendering failures return a nonzero status and keep the source and configuration for diagnosis.

The existing pinned package input supplies Mermaid CLI **11.16.0**, with its Chromium wrapper on Linux. `LIQUID_MERMAID_BIN` is set by the main `devenv.nix`; no manual environment variables, CDN, Mermaid account, or server are needed. Outside devenv, install [Mermaid CLI](https://github.com/mermaid-js/mermaid-cli) and its supported browser, with `mmdc` available on PATH. Rendering is bounded to 120 seconds and graph-size limits are configured to include every saved edge. `devenv test` checks the renderer executable and runs a real synthetic SVG rendering test alongside the offline suite.

On GitHub's hosted Ubuntu runner, that synthetic test uses a fixture-only `--no-sandbox` Chromium configuration to accommodate the runner's user-namespace restrictions. Ordinary preview commands and local `devenv test` keep Chromium's default sandbox. Test failures include renderer diagnostics from synthetic data; product errors retain private investigation text locally.

Create and save a board, then preview and sync the latest run:

```bash
liquid-live miro-create-board --case cases/theft-liquid
liquid-trace miro-sync --case cases/theft-liquid --dry-run
liquid-live miro-sync --case cases/theft-liquid
```

Optional creation flags are `--name TEXT` (1–60 characters), `--visibility private|team`, and `--team-id ID`. Private visibility restricts team, organization, and public-link access. Team visibility enables team editing while keeping organization and public-link access private. Your Miro plan and permissions must support the chosen setting. Failed requests never automatically broaden visibility.

Creation records a pending request before contacting Miro, then saves the returned board ID before updating `case.json`. A retry reuses an acknowledged board. An uncertain outcome (such as a connection failure after sending the request) blocks a second creation request: inspect Miro and link the board through **Investigation settings**. If none was created, create one in Miro and link it. This avoids duplicate boards and preserves completed run evidence.

Alternatively, supply an existing board with `--board`:

```bash
liquid-trace miro-sync --case cases/theft-liquid --dry-run \
  --board 'https://miro.com/app/board/YOUR_CASE_BOARD_ID/'
liquid-live miro-sync --case cases/theft-liquid \
  --board 'https://miro.com/app/board/YOUR_CASE_BOARD_ID/'
```

For each subsequent run, continue one more hop, review, and update the saved board:

```bash
liquid-live trace --case cases/theft-liquid \
  --resume latest --additional-hops 1 \
  --max-transactions 20 --max-outpoints 100 --max-requests 30 --max-seconds 60
liquid-trace miro-sync --case cases/theft-liquid --dry-run
liquid-live miro-sync --case cases/theft-liquid
```

For direct sync commands, board selection follows **explicit `--board` → saved case board → legacy `LIQUID_MIRO_BOARD` → interactive prompt**. Noninteractive commands fail clearly if no board is available. Local preview does not persist a new board selection. Live sync saves the selection after local validation and before a network request, so retries can reuse it even if publication fails. A saved board does not trigger publication during tracing; that requires an explicit `--miro-board` flag or a separate sync action.

`--run` defaults to `latest` for sync. Use `--run RUN_ID` for sync or export, or `--resume RUN_ID` for tracing, to select a particular historical snapshot. `export --case cases/theft-liquid --run latest --out NEW_DIRECTORY` also accepts the pointer. Existing cases without a latest pointer need an explicit run ID; the program does not guess.

The demo helpers remain useful for scripts. `liquid-demo` starts an independent root each time. After syncing it, resume with `--case "$LIQUID_DEMO_CASE_DIR" --fixture "$LIQUID_TRACER_ROOT/examples/demo-api.json" --resume latest --additional-hops 1` to extend its lineage. An independent new root cannot replace an already published lineage. The interactive interface offers continuation for a selected demo investigation automatically.

## Optional environment overrides

The normal setup needs no `devenv.local.nix`. The single committed environment supplies the investigation root, demo helper path, and SecretSpec provider/profile. Personal board IDs belong to case settings, and credentials belong to Proton Pass.

For advanced machine-specific settings, an ignored `devenv.local.nix` can override the main environment; it contributes to the same environment rather than creating another one. For example, to keep investigations outside the source directory:

```nix
{ lib, ... }:
{
  env.LIQUID_INVESTIGATIONS_DIR = lib.mkForce "/absolute/path/to/investigations";
}
```

Re-enter the shell after changing environment configuration. For a temporary override, the existing `-O` workflow also works:

```bash
devenv -O env.LIQUID_INVESTIGATIONS_DIR:string /absolute/path/to/investigations shell
```

Direct `trace`, `export`, and `miro-sync` commands still accept an existing `LIQUID_CASE_DIR` setting when `--case` is omitted. Explicit arguments take precedence. That compatibility setting does not choose the investigation in the interface. Keep secret values out of all Nix expressions: ignoring a Nix file in Git does not prevent evaluated strings from entering generated Nix artifacts.

## Store secrets in Proton Pass

Devenv provides SecretSpec **0.19.1** and Proton Pass CLI **2.3.2** from the project's pinned nixpkgs input. `LIQUID_SECRETSPEC_BIN` points the project launchers and TUI at that exact SecretSpec binary. `SECRETSPEC_PROTONPASS_CLI_PATH` tells the provider to use the matching pinned `pass-cli`. Both settings are declared in the main `devenv.nix`; no shell assignments or Home Manager changes are required for this project. Outside devenv, install compatible versions yourself. Sign in locally with `pass-cli login` if you have not already done so. With the default provider, SecretSpec uses note items in the `secretspec` vault; ensure that vault exists in Proton Pass. See the [Proton Pass provider setup](https://secretspec.dev/providers/protonpass/).

`pass-cli` 2.2.4 removed `test`; SecretSpec 0.18 and earlier still invoke it. SecretSpec 0.19 and later try `info` first, fixing the reported `unrecognized subcommand 'test'` failure. The environment rejects SecretSpec versions older than 0.19. See the provider's [compatibility notes](https://secretspec.dev/providers/protonpass/#pass-cli-compatibility).

After pulling environment changes, leave the old shell and re-enter `devenv shell`. Check the tools, then credential delivery:

```bash
liquid-toolchain-check
liquid-secrets-check
```

The first command needs no login. The second contacts the secret provider and reports `present` or `MISSING` for each requested value, without printing values. It defaults to the two Blockstream credentials; `--service miro` checks the Miro token, and `--service all` checks all three. A missing value causes exit status 1. This verifies delivery to the application, not acceptance by Blockstream or Miro. It also avoids the indentation problems of pasting a multiline Python probe into a shell.

`devenv test` runs the toolchain check, Astro check/build, and offline Python suite without vault access. GitHub Actions runs this through the actual Nix environment as well as running the Python/TUI suite separately. Neither job uses real credentials.

Inside the project shell, run the setup helper and enter each credential at the prompt:

```bash
liquid-secrets-setup
```

Use `liquid-secrets-setup blockstream` or `liquid-secrets-setup miro` to configure one service. The setup helper, menu live actions, and `liquid-live` explicitly select this project's manifest, `LIQUID_SECRET_PROVIDER`, and `LIQUID_SECRET_PROFILE`, even if your global SecretSpec defaults differ. Setting an existing entry replaces its value; rerun the relevant command when rotating a credential. Setup contacts the secret provider, but makes no Blockstream or Miro requests.

If you already stored the credentials under this project's `development` profile in Proton Pass, skip setup. Selecting a provider and profile in SecretSpec's global configuration does not itself create the credentials. Changing providers or profiles does not migrate existing values; the manifest retains the `default` profile for access to earlier entries.

The equivalent individual commands from the project directory, also useful when updating just one value, are:

```bash
secretspec set BLOCKSTREAM_CLIENT_ID --provider "$LIQUID_SECRET_PROVIDER" --profile "$LIQUID_SECRET_PROFILE"
secretspec set BLOCKSTREAM_CLIENT_SECRET --provider "$LIQUID_SECRET_PROVIDER" --profile "$LIQUID_SECRET_PROFILE"
secretspec set MIRO_ACCESS_TOKEN --provider "$LIQUID_SECRET_PROVIDER" --profile "$LIQUID_SECRET_PROFILE"
```

For a report that does not print the stored values:

```bash
secretspec --file "$LIQUID_TRACER_ROOT/secretspec.toml" check \
  --provider "$LIQUID_SECRET_PROVIDER" --profile "$LIQUID_SECRET_PROFILE"
```

Confirm that each credential you intend to use is resolved. An optional missing entry does not cause this check to fail. This checks secret resolution, not whether Blockstream or Miro accepts the credential.

```bash
liquid-live trace --case cases/theft-liquid --seed 'LIQUID_TXID:0' --hops 1 \
  --max-transactions 20 --max-outpoints 100 --max-requests 30 --max-seconds 60
liquid-live miro-sync --case cases/theft-liquid
```

The manifest marks credentials optional because different commands need different services. The application checks Blockstream credentials for authenticated tracing and the Miro token for board creation and live sync. Set only the services you use. Secret values are provided to the child process environment and are not added to the interactive parent shell or saved case files. They remain readable by the process that needs them; environment variables are a delivery mechanism, not encrypted storage.

## Optional desktop keyring storage

To use existing keyring entries in the earlier `default` profile, override both settings when entering the shell:

```bash
devenv -O env.LIQUID_SECRET_PROVIDER:string keyring \
  -O env.LIQUID_SECRET_PROFILE:string default shell
```

Use `liquid-live` normally in that shell. To store new keyring entries in `development` instead, override only the provider and run `liquid-secrets-setup`.

The [keyring provider](https://secretspec.dev/providers/keyring/) uses the operating system's credential store. Linux needs a running, unlocked Secret Service such as GNOME Keyring or KWallet. This repository does not modify your NixOS login or keyring services.

### NixOS with Hyprland

GNOME Keyring supplies the background Secret Service. **Seahorse**, shown as **Passwords and Keys**, is an optional graphical manager for that keyring. It can list entries, unlock collections and remove old credentials. SecretSpec stores and retrieves values through the service, so you do not need to enter the same values manually in Seahorse. You can use these GNOME components while keeping Hyprland as your desktop.

If KWallet, KeePassXC, or another application already supplies your Secret Service, use that existing provider. To inspect an active provider without reading stored values:

```bash
busctl --user status org.freedesktop.secrets
```

A missing active owner can also mean that an installed service has not started yet. Avoid starting competing Secret Service providers for the same session.

For a GNOME Keyring setup, add these settings to your **NixOS system configuration**, where `pkgs` is available:

```nix
services.gnome.gnome-keyring.enable = true;
environment.systemPackages = [ pkgs.seahorse ];
```

Merge the package into your existing package list rather than defining that attribute twice in one file. Apply your normal flake-based `nixos-rebuild switch` command, then log completely out and back in using your password. This system configuration belongs outside the project's `devenv.nix`: the keyring service and login integration need to persist beyond a project shell.

On NixOS 26.05, the GNOME Keyring module configures login PAM; greetd also enables its keyring PAM integration by default when the service is enabled, and SDDM uses the login PAM stack. Do not add guessed PAM entries for a display manager you do not use. Custom login configuration, autologin, fingerprint-only login, and mismatched keyring/login passwords can require additional setup or manual unlocking. Sources: [GNOME Keyring module](https://github.com/NixOS/nixpkgs/blob/nixos-26.05/nixos/modules/services/desktops/gnome/gnome-keyring.nix), [greetd module](https://github.com/NixOS/nixpkgs/blob/nixos-26.05/nixos/modules/services/display-managers/greetd.nix), [SDDM module](https://github.com/NixOS/nixpkgs/blob/nixos-26.05/nixos/modules/services/display-managers/sddm.nix), [GNOME PAM behavior](https://wiki.gnome.org/Projects%282f%29GnomeKeyring%282f%29Pam.html).

Open Seahorse with `seahorse`. Unlock the **Login** keyring. If no password keyring exists, create a password-protected one and set it as the default. For automatic login unlocking, use a Login keyring whose password matches your login password. See GNOME's [keyring creation](https://help.gnome.org/seahorse/keyring-create.html) and [unlocking](https://help.gnome.org/seahorse/keyring-unlock.html) instructions.

Return to the project and enter a shell with the keyring provider to provision credentials:

```bash
git pull
devenv -O env.LIQUID_SECRET_PROVIDER:string keyring shell
liquid-secrets-setup
```

## Optional local `.env` storage

If you prefer a local file, create it without overwriting an existing one:

```bash
umask 077
cp -n .env.example .env
chmod 600 .env
```

Edit `.env` locally and fill in only the keys you use. It is plaintext on disk and ignored by Git. `.env.example` is the empty, public template. Select SecretSpec's dotenv provider for a shell:

```bash
devenv -O env.LIQUID_SECRET_PROVIDER:string dotenv shell
```

Then use `liquid-live` normally. [SecretSpec's dotenv provider](https://secretspec.dev/providers/dotenv/) reads the `.env` beside the selected `secretspec.toml`. It reads the file at process startup. We intentionally do **not** enable `dotenv.enable`: [devenv's documented dotenv integration](https://devenv.sh/integrations/dotenv/) warns that it can copy secret values into the readable Nix store.

## How projects usually separate configuration and secrets

| Location | Commit to a public repository? | Purpose |
| --- | --- | --- |
| `devenv.nix`, `devenv.yaml`, `devenv.lock` | Yes | Environment setup, package pins and nonsecret defaults. |
| `secretspec.toml`, `.env.example` | Yes, with no secret values | Names, descriptions and setup templates. |
| OS keyring or password manager | No secret values in Git | Separate secret storage, local or cloud depending on provider. |
| Local `.env` | No | Simple plaintext storage for one development machine. |
| CI/deployment secret store | No secret values in code | Supplies credentials to the job or service that needs them. |

For CI, GitHub Actions secrets can supply environment variables directly and run the Python CLI; `liquid-trace` also accepts them inside devenv. The offline test suite needs no GitHub secrets. See [GitHub's secrets documentation](https://docs.github.com/en/actions/security-for-github-actions/security-guides/using-secrets-in-github-actions).

Other SecretSpec providers can replace Proton Pass by setting the nonsecret `LIQUID_SECRET_PROVIDER` value. Set `LIQUID_SECRET_PROFILE` to select another declared profile. Avoid placing values in `env.MIRO_ACCESS_TOKEN`, reading secret files with `builtins.readFile`, or passing credentials as `devenv -O` arguments. The supported pattern follows [devenv's runtime SecretSpec guidance](https://devenv.sh/integrations/secretspec/).

`.gitignore` prevents ordinary additions of matching untracked files; it is not encryption and does not untrack files already committed. If a credential is ever committed, revoke/rotate it. Removing the current file alone does not remove it from Git history.
