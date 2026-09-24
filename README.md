# Liquid UTXO Tracer

A local Python program for bounded forward tracing on Liquid, per-run CSV/evidence exports, and incremental updates to an editable Miro graph. Version 0.2.0; Python 3.11+ on Linux or macOS. Choose the Textual terminal interface or the local Astro browser interface; both use the same Python tracer and saved investigations. Live case integration still needs validation with your credentials and starting outputs.

Miro is the investigation workspace. The program retrieves blockchain data and keeps the evidence and object mapping; it can be run from your terminal without a Miro plugin. CSV files document each run. The saved graph plan supplies native Miro shapes and connectors directly through the REST API, so a generic CSV-to-graph importer is unnecessary.

It follows **exact output spends**, preserves confidential quantities as unknown, saves unfinished branches, and extends them in later runs. The graph uses address circles, transaction squares, and separate diamonds for unspendable outputs, peg-out requests, and optionally transaction fees.

Graph captions use `vin/vout number · amount asset`. For example, `vout 0 · ?? ??` means neither amount nor asset is available in the public data; `?? L-BTC` retains an explicitly identified asset with an unavailable amount. Explicit L-BTC amounts are shown in L-BTC, dividing their base-unit value by 100,000,000 without rounding (for example, 10,000,000 base units becomes `0.1 L-BTC`). Other assets and unidentified assets retain base-unit amounts. Raw evidence and transaction CSV numeric columns remain exact base units. Other asset IDs are shortened for display and retained in full in the evidence. Address circles show the address and any analyst attribution; the creating transaction hash and exact UTXO reference remain in the saved details instead of being repeated on the circle or input caption.

The graph legend uses the same palette as its nodes and connectors:

| Appearance | Meaning |
| --- | --- |
| Purple squares | Provided starting transactions, including those also reached through another starting transaction. |
| Blue squares | Subsequent transactions outside the provided starting set. |
| Red circles | Selected starting outputs. |
| Yellow circles | Reachable candidate outputs. |
| Orange circles | A traced branch ends at an output observed unspent. |
| Light gray circles | Context addresses. |
| Assigned name colors | Case-insensitive per-name colors chosen after import; not confidence categories. |
| Pink diamonds | Fees, unspendable outputs, or peg-out requests. |
| Dark teal / gray arrows | Traced UTXO links / context connections. |

Starting-transaction color comes from the saved seed transaction hashes, independently of hop depth, and remains purple across continuations. Where circle roles overlap, selected seed red takes priority, then an assigned name color, unspent endpoint, candidate, and context. Confidence never selects a color. This also applies when addresses are merged. Colors describe graph roles; they do not establish ownership or allocate stolen value. The full seed list and run details stay in the saved run data; no per-run summary box is added to Miro.

**Unspent endpoint** marks the specific traced UTXO where a branch stops because the explorer reported it unspent. It does not highlight an address merely because it holds other unspent outputs. Hop limits, errors, analyst stops, and unselected outputs do not qualify. The output needs a recorded spending-status observation, and a known spending input in the saved graph overrides stale unspent evidence. Seed red and assigned name colors retain priority, with the endpoint label still shown. In merged-address mode, the label identifies how many displayed tracked endpoints qualify; it does not imply the whole address is dormant.

The status refers to the saved observation, not a real-time balance or a minimum inactivity period. Continuing and rechecking that branch can remove its orange color when it is spent. Unspent status uses existing saved evidence. Separately, previews and ordinary Miro sync automatically fetch any missing address transaction counts before rendering. Detailed graph/CSV data retains the qualifying outpoints. See [Esplora's spending-status endpoints](https://github.com/Blockstream/esplora/blob/master/API.md#get-txtxidoutspendvout).

Miro presentations now use **ELK Layered**, a layout engine running locally through the pinned `elkjs` library. It places transactions and nearby inputs/outputs from left to right, orders connection points to reduce crossings, and separates disconnected components. Each full address has one circle per network, shared across starting transactions and hops. Address reuse can introduce return edges that cannot all point right; transaction dependency order remains preserved. The evidence archive retains its original baseline layout; ELK calculates a separate presentation without changing the recorded relationships.

Optionally open **Change outputs** to select a transaction's change `vout` using
transaction lookup or a reviewed `Txid,ChangeVout,Notes` CSV import. ELK aligns
that change output with its transaction and places the other outputs below.
Transactions without a designation keep normal ELK rules. Shared-address
conflicts are reported in the preview. Regenerate a preview or use **Sync and
reorganize Miro graph** to apply the arrangement. See [change-output selections
and CSV import](docs/change-outputs.md).

Transaction connectors attach to fixed sides: inputs enter the left and outputs leave the right, with separate attachment points ordered by ELK. **Connector appearance** in settings defaults to **Straight**; **Curved** and **Elbowed** are also available. Straight connections that return backward or would pass through objects use elbowed routes where needed. Miro controls the final connector paths, so the local preview estimates its appearance. New connectors use the selected setting; choose **Sync and reorganize graph** to apply the calculated positions and connector appearance to an existing graph. Normal sync preserves manual positions and connector choices.

**Include transaction fee flows** is a checkbox in investigation settings and the defaults for new investigations. It starts unchecked. When included, fees appear in a horizontal row above the main flow, ordered by available chain chronology with deterministic tie handling. Excluding fees changes the graph only: fee amounts, events, and API observations remain in the evidence exports.

The pink diamonds identify special outputs. **PEG-OUT REQUEST** records Esplora's parsed withdrawal instruction to the parent chain. On Liquid mainnet, a normal L-BTC peg-out burns L-BTC and requests a Bitcoin payout to the encoded destination. The tracer has not matched or confirmed that separate Bitcoin payout, and the label does not identify an Avalanche bridge. See [Blockstream's peg-out explanation](https://help.blockstream.com/liquid-network/faqs/what-is-a-liquid-peg-out) and the [Esplora transaction fields](https://github.com/blockstream/esplora/blob/master/API.md#transaction-format).

**UNSPENDABLE** means the recorded script is identified as `OP_RETURN`, preventing that output from being spent later. The individual output branch ends there; other spendable outputs from the transaction can still be traced. Such an output may carry data or represent a burn, but Liquid can also add zero-value unspendable outputs when constructing confidential transactions. When the amount or asset is `??`, do not infer a positive-value L-BTC burn from the diamond alone. See the [Elements blinding documentation](https://elementsproject.org/en/doc/22.0.0/rpc/wallet/blindrawtransaction/) and [null-data script explanation](https://developer.bitcoin.org/devguide/transactions.html#null-data).

## Convergence highlighting and attribution notes

Transactions where different starting lineages meet through verified saved UTXO
spends use a **12-pixel red border** (ordinary borders are 2 pixels). Starting
transactions can qualify. A shared address circle alone is not a convergence.
There are no separate star objects or Miro attribution cards. Names, stop labels,
seed-red priority and assigned name colors are unchanged; source and notes stay
in Address review and local HTML/JSON/CSV exports. Normal **Sync to Miro** safely
retires proven, unmodified old cards/stars and updates the transaction borders
without retracing. Preserve manual annotations/comments before cleanup. See
[exact trigger conditions and upgrade behavior](docs/address-attributions-and-convergence.md).

## Start the program

The project has **one main `devenv.nix`** for Python, Node.js, commands, and environment defaults. SecretSpec retrieves API credentials from Proton Pass when a selected action needs them. Investigation names, board IDs, and run history are saved as case data; you do not need another Nix file or shell variables for each investigation.

The bundled Proton Pass CLI supports Linux on x86_64 or ARM64 and macOS on Apple Silicon. Clone the project and enter its devenv shell. Install [devenv](https://devenv.sh/getting-started/) first if needed:

```bash
git clone https://github.com/rgmundo524/Liquid-Network-Tracer.git
cd Liquid-Network-Tracer
devenv shell
liquid-trace
```

In an interactive terminal, `liquid-trace` opens a Textual interface with **New investigation**, **Continue investigation**, **Settings**, and **Exit**. Select actions with the keyboard or mouse. You can also launch it explicitly with `liquid-trace menu`. Opening the menu does not load credentials or call Blockstream or Miro.

When a button is highlighted, use **↑ ↓ ← →** to move between buttons and **Enter** to activate it. **Tab** and **Shift+Tab** move between all controls. Text fields, dropdowns, and tables keep their normal arrow-key behavior. **Space** toggles a checkbox; **Esc** returns to the previous screen. Disabled buttons are skipped, and navigation scrolls the focused button into view.

1. Choose **New investigation** and give it a name. The program creates a unique subdirectory under `cases/`.
2. Paste one or more bare Liquid transaction hashes, separated by commas, into **Transaction hashes** and choose **Load outputs**. New investigations use Live Liquid. Review the outputs grouped by transaction, use Enter to toggle the relevant rows across transactions, then choose **Use selected outputs**. This fills the starting-output field; it replaces any existing entries. Alternatively enter known outputs directly as `HASH:NUMBER`, separated by whitespace or commas. Optionally provide an existing Miro board URL or ID; you can add it later.
3. Choose **Collect transaction data** and review its hop and request limits. Collection retrieves credentials through SecretSpec when the configured API requires them. Later collections use **additional hops** from the previous saved ceiling.
4. Choose **Choose plotting goal** under **Plot saved data**. Select a saved run and a goal: full investigation, starter connections, or paths to peg-outs. These plots use saved evidence only and make no API requests.
5. Choose **View / create / sync boards**. Create or link a named board for a goal, select its saved plot, and sync it. Each investigation can have several boards, and each board can be refreshed from later plots.
6. Next time, launch `liquid-trace`, choose **Continue investigation**, and select the saved case. Collect more data if needed, or plot and sync what is already saved.

The investigation settings let you change its name, legacy full-graph board, run limits, fee visibility, and connector appearance. Top-level **Settings** changes defaults for new investigations. Existing investigations keep their own saved defaults. **Miro boards** manages the investigation's separate named boards and their plotting goals. Creating a board makes an empty destination; publishing a saved plot is a separate sync action. See the [investigation workflow guide](docs/investigation-workflow.md).

Choose **ELK layout preview** after saving a run to inspect the proposed positions and routes locally, compare estimated crossings and overlaps, and download its SVG. It needs no Miro board. Missing address transaction counts are fetched first, using credentials when the configured API requires them; layout calculation remains local. The preview uses the same ELK calculation as the default Miro plan, while actual Miro routes and existing manual arrangements can differ.

For an independent local view, choose **Mermaid chart** in the investigation menu after saving a run. The button creates a Mermaid source file, renders an SVG, and opens an HTML preview in your browser. It uses the latest saved run and current fee setting, needs no Miro board, and saves each preview in a new directory under the investigation's `previews/`. Like the ELK preview, chart preparation can require credentials to fetch missing address counts. The menu shows the file path if a browser cannot be opened. Miro remains the editable investigation board.

Choose **Export CSV** beside **Mermaid chart** to save tables from the latest run. Each click creates a new directory under the investigation's `exports/` and displays its file paths. This offline action requires a saved run, with no Miro board or secret-provider session. Completed runs already contain CSVs; the menu action makes a separate export for review or use in another application.

`vout` means the output's numeric index, starting at zero. In `HASH:0`, `0` selects the first output; `HASH:1` selects the second. Do not type the literal word `vout` or a backslash before the colon. Choose the output connected to your investigation; output 0 is only an example. The picker starts with nothing selected and excludes fees, peg-outs, and unspendable outputs. Hidden amounts and assets remain unknown.

**Load outputs** accepts up to 100 distinct transaction hashes. Commas, spaces, or newlines separate hashes; duplicates are removed and the full list is validated before lookup. One credential session and API client serve the batch. The shared lookup budget defaults to five API attempts and 30 seconds per distinct transaction, including authentication and retries. For 10 transactions that means at most 50 attempts and 300 seconds across the batch. A failed lookup preserves the existing starting-output field; a partial result is not applied.

Lookup creates no investigation or trace and follows no subsequent spends. The selected output references are saved when you create the investigation; the later trace fetches and archives its own evidence. Selected UTXOs from all starting transactions share one investigation and set of run limits, with separate boards for different plotting goals. Shared descendants are represented once. Continuing a bounded run resumes its saved branches; adding different starting transactions currently requires a new investigation.

## Local browser interface

For the Astro alternative, enter the same environment and launch:

```bash
devenv shell
liquid-web
```

This builds the interface and opens [http://127.0.0.1:4321](http://127.0.0.1:4321) in your browser. The first launch installs the locked frontend packages; later launches reuse that installation unless the package manifest or lockfile changes. Leave the launching terminal open. Use `liquid-web --no-open` to open the address yourself, `--port 4322` to choose another port, or `--root /absolute/path/to/investigations` to select another investigation directory. `liquid-web --help` shows the options without building the interface.

The browser and terminal interfaces share the same `cases/`, defaults, run history, and Miro mappings. An investigation created in either interface can be reopened in the other without importing or migrating it.

1. Open a saved investigation, or create a new Live Liquid investigation. Paste comma-separated transaction hashes, load their outputs, and select the relevant UTXOs grouped by transaction.
2. Open **Collect data**, set the hop allowance for this run, and start collection. Other limits use the saved defaults. A later run resumes the latest saved snapshot with additional hops.
3. Open **Plot views** and choose a goal: **Full trace**, **Starter connections**, or **Paths to peg-outs**. Select a saved collection run, set any hop filter, and generate the plot locally. Plotting fetches no new evidence or address statistics. Review its source coverage and layout.
4. Open **Miro boards**. Create or link a board for that goal, select the saved plot, then choose **Sync to Miro** or **Sync and reorganize**. Each board has its own mapping and status; later plots can refresh the same board.
5. Use **Downloads** for saved SVGs, tables, layout reports, and supporting files. Older products remain available after reopening the investigation; changed display settings identify plots that need to be regenerated.

**Investigation settings** collects tracing limits, graph layout, Miro configuration, and colors. **Workspace defaults** supplies starting preferences for new investigations. Changing the hop allowance in a run dialog applies only to that run. Imports use **Preview → Apply**; changing a file or import option requires a fresh preview.

The task tabs separate **Collect data**, **Plot views**, **Miro boards**, **Investigation data**, **Downloads**, and **History**. The selected saved snapshot stays selected across tabs. The main plotting workflow is fully offline; collection is the separate fetching phase. Older ELK and Mermaid tools can still fetch missing address counts before local rendering. Miro board creation and sync contact Miro and require its access token. Linking and listing boards are local actions.

The usual cycle is **Collect data → Plot views → Sync selected boards**. Create or link each goal's board once. Normal sync preserves the positions of retained objects. If the existing positions leave no room, or you want a fresh arrangement, choose **Sync and reorganize**. Obsolete generated objects are removed only after checks protect analyst changes and unrelated connections. Each board's status and history remain separate, while the underlying collection data is shared. You may also tidy the native shapes in Miro by hand.

During sync, the browser and launching terminal show the current stage and completed/total item counts. Existing-item checks finish before any board changes, so the board itself may initially appear unchanged. The display distinguishes checking, layout preparation, updates, new items, and temporary API retry waits. Counts belong to the current stage, not a predicted completion time. A failed action retains its last stage; consult the terminal for the specific API or mapping error before retrying.

Live actions retrieve credentials through the existing SecretSpec/Proton Pass setup. If Proton Pass needs login or unlocking, respond in the **launching terminal**; do not enter API keys in the browser. Only one job runs at a time in each local server. Closing a browser tab does not cancel its job: reopen the address to see progress. **Ctrl+C** stops the server and its active offline worker. While a live action owns the terminal for provider prompts, the first Ctrl+C cancels that action; press it again after the prompt returns to stop the server. Restart `liquid-web` to reopen saved investigations from disk.

Astro supplies the local interface; Python serves it and runs the existing tracer commands. The built interface and ELK/Mermaid previews need no CDN or hosted frontend. Use the local previews to review layouts and Miro for online editing and collaboration. See [local browser development](docs/development.md#local-astro-interface) for build and test commands.

## Plot peg-outs within a hop range

Collect the relevant transaction data, then choose **Paths to peg-outs** in
**Plot views**. Enter an inclusive minimum and maximum hop count. Each starting
transaction is hop 0; each spending transaction adds one hop. The plot follows
the investigation's originally selected seed UTXOs and excludes unselected
siblings. It reads the saved collection run without fetching more transactions.
Use **Miro boards** to initialize and maintain a separate board for this goal.

For a 10-hop view, first collect enough evidence to cover that depth and check
whether a budget or stop rule limited coverage. Missing matches in saved data
do not prove that no peg-out exists. A request is not confirmation of its Bitcoin
payout. The [investigation workflow guide](docs/investigation-workflow.md)
explains the shared data and multi-board workflow.

Earlier standalone **Trace to peg-outs** searches remain available under
**History**, with their original scope and resume controls. These legacy searches
can still fetch independently and retain their separate snapshot publication.
See the [legacy peg-out search guide](docs/pegout-search.md) for recovery and CLI
commands.

## Where investigations and runs are saved

The menu uses `LIQUID_INVESTIGATIONS_DIR`, configured by devenv as the repository's `cases/` directory. To select another location explicitly, use:

```bash
liquid-trace menu --investigations-dir /absolute/path/to/investigations
```

| Location | Contents |
| --- | --- |
| `cases/settings.json` | Run limits, fee visibility, and connector appearance defaults for new investigations. |
| `cases/<investigation>/case.json` | Investigation name, seeds, source, board selection, run defaults, and latest exported run ID. |
| `cases/<investigation>/runs/<run-id>/` | A separate evidence and export directory for each run. |
| `cases/<investigation>/previews/<run-id>-elk-<id>/` | Local ELK SVG/HTML preview, complete graph geometry, and layout quality report. |
| `cases/<investigation>/previews/<run-id>-mermaid-<id>/` | Local Mermaid source, SVG/HTML preview, graph details, and node identifiers. |
| `cases/<investigation>/exports/<run-id>-csv-<id>/` | CSV tables with export provenance and checksums. |
| `cases/<investigation>/miro/` | Saved mapping between graph objects and items on each Miro board. |
| `cases/<investigation>/miro/board-creation.json` | Board creation request and receipt, retained to recover interrupted creation without duplicating a board. |
| `cases/<investigation>/miro/reports/` | Sync reports identifying the run and actual board used. |

One investigation normally keeps the same board across continuations. Each new run preserves the previous run and its evidence. A run ID distinguishes the snapshots, but the program saves and selects that ID for you. The latest reference survives closing the shell and restarting the computer.

New run exports include an `investigation.json` snapshot of the investigation name and board selection as known when tracing occurred. If you choose or change the board later, the saved case settings and sync reports record that choice; completed run exports are not rewritten. The board ID is case configuration. Actual API credentials remain in the secret provider and are not written into these files.

The default `cases/` directory is ignored by Git. Preserve the whole investigation directory when backing up or moving a case, including its `case.json` and `miro/` mapping. A custom investigation directory has its own storage and Git rules.

## Explicit commands

Either interface can be the normal starting point. All existing subcommands remain available for scripts and advanced tracing. `liquid-live` loads credentials before running a direct command; the interfaces handle that step when you select a live action. `liquid-test` runs the offline Python suite.

To continue an existing live investigation, preview its saved graph, and update its linked board, replace `cases/theft-liquid` with your case directory:

```bash
liquid-live trace --case cases/theft-liquid \
  --resume latest --additional-hops 1 \
  --max-transactions 20 --max-outpoints 100 --max-requests 30 --max-seconds 60
liquid-trace miro-sync --case cases/theft-liquid --dry-run
liquid-live miro-sync --case cases/theft-liquid
```

The dry run makes no network calls, needs no token, and writes no state. It cannot detect remote manual edits or deleted items; live sync performs that preflight. Supply `--board URL_OR_ID` to select a board that is not already saved with the case. Live sync saves the selected board after local validation so later commands can reuse it. See [explicit tracing commands](#trace-a-case-with-explicit-commands) to start a new case.

A new independent root cannot overwrite an already published graph's lineage. Continue with `--resume latest` to extend it; the interactive menu offers continuation within the saved investigation.

If needed, add `--offline-preview` to `trace` or `export` to also save an HTML inspector and SVG. These are optional inspection files; normal runs use Miro for visual review.

To review address activity and record suspected services, open an investigation and choose **Address review** in either the local browser or terminal interface. Search addresses already observed in a selected saved run, or paste an address. The list is paginated; opening a review reads saved data and does not fetch every address automatically.

1. Choose **Refresh activity** (the terminal calls it **Refresh address activity**). This explicitly retrieves statistics and bounded confirmed history through the existing Blockstream client and Proton Pass workflow. Defaults are five history pages, ten HTTP attempts including authentication/retries, and sixty seconds.
2. Review confirmed transaction count, separate mempool count, confirmed unspent-output count, mempool output delta, combined general unspent-output count, and confirmed activity dates. These output counts include all indexed assets at the address. They are not an L-BTC balance or a count of only the investigation's UTXOs.
3. Check history coverage. Transaction counts come from the explorer's address statistics without scanning every transaction. The first confirmed activity date is shown only when the complete paginated history agrees with those statistics. Otherwise, the oldest retrieved activity is explicitly partial. Block dates describe observed confirmed activity, not when a wallet or address was created. Statistics and history are separate observations, not an atomic chain snapshot.
4. If your analysis supports the decision, enable the assessment, choose **Stop tracing through this address** independently, select **Suspected** or **Confirmed** confidence, enter an optional name and notes, and save. This records your assessment; the program does not infer ownership from activity counts or turn it into verified attribution.
5. Continue the investigation. A designated address stops forward traversal before fetching its spend. If an earlier run already expanded beyond it, downstream frontier reachable only through that boundary is held too. Independent starting outputs and other unblocked paths remain eligible, subject to their currently permitted hop depths. Historical transactions, links, depths and observations stay intact.
6. Refresh the local preview or use normal Miro sync to apply updated attribution labels and stop indicators. Choose **Assign name colors** after import to color all addresses with the same name, case-insensitively. Selected seed outputs remain red; confidence only controls the name prefix. Current decisions also appear in fresh CSV graph tables. Disable the designation later to allow subsequent continuation again; its notes and revision history are retained.

Decisions are saved in `services.json` with an audit history, separately from archived tracing runs. Each new run snapshots the current rules without copying the full decision history. Rules cannot change during an active trace. Address reviews are immutable, checksum-referenced JSON reports under `address-reviews/`; their raw API responses remain in `evidence.sqlite`. Refreshes create new observations and retain older reviews. Graph previews can reflect current decisions without rewriting the archived evidence. Service boundaries do not erase previously plotted downstream activity or classify that activity's owners.

The same controls are available directly:

```bash
liquid-trace address-list --case cases/theft-liquid --query exchange
liquid-live address-inspect --case cases/theft-liquid --address ADDRESS
liquid-trace service-set --case cases/theft-liquid --address ADDRESS \
  --name "Possible exchange" --rationale "Investigator's reasons"
liquid-trace service-set --case cases/theft-liquid --address ADDRESS --disable
```

`address-inspect --run RUN_ID` uses that saved snapshot's API source. To inspect more history, explicitly increase `--max-pages`, `--max-requests`, and `--max-seconds`; every HTTP attempt still consumes the existing budget and rate controls. Increasing the page budget does not trace those transactions or add them to the funds-flow graph. Address spellings are not ownership clusters: use the address shown by the explorer/graph, because confidential and unconfidential representations are not automatically linked.

### Per-name colors

After importing attributions, choose **Assign name colors** in the browser or terminal import screen. Each unique name appears once, case-insensitively. Choose and save a color; later imports with the same name inherit it. Confidence does not choose a color. **Selected seed outputs always take red priority**, including shared-address nodes and named or unspent seeds. Source, notes, stop indicators and unspent labels remain available. Use **Sync to Miro** or regenerate previews to apply the display changes without retracing. See [name colors and seed priority](docs/name-colors.md).

For bulk assignments, choose **Import CSV files** and select a `Name,Color` CSV,
optionally together with the address attributions and change-output files.
The importer detects each type and processes new attribution names before
colors automatically. Review one batch and apply all files once. Names and
headers match case-insensitively; colors use `#RRGGBB`. Existing entries are kept
unless you choose replacement. Pasted and JSON imports remain in the advanced
importers. See the [CSV template](examples/name-colors-template.csv) and
[combined CSV workflow](docs/input-csv-exports.md#import-csv-files-together).

Choose **Export saved CSV** in the attribution, name-color, or change-output
screen to edit current saved entries in their import format. **Export input CSVs**
in the investigation exports all three together as a ZIP, even before a trace.
Exports include every saved row, with large tables split into importable CSV
parts. See [exporting and reimporting CSV inputs](docs/input-csv-exports.md).

### Bulk address attributions before the first run

Create or open an investigation, then choose **Import CSV files** in the browser
or terminal. Select one CSV or the attribution, name-color, and change-output
CSVs together. Preview the complete batch and approve it once. For JSON or pasted
address lists, use the advanced attribution importer. No saved run, credentials, blockchain request or Miro
update is required. Existing decisions are kept unless replacement is explicitly
reviewed. Invalid rows block the entire batch; unchanged reimports do not write.
When revising an existing address's hop limit or stop flag, select replacement
so the uploaded decision replaces the saved one.

The standard CSV is now:

```csv
Address,Name,confidence,stop_tracing,source,notes
```

Confidence has just **suspected** and **confirmed**. A suspected entry is displayed
as **Suspected Example Exchange**; confirmed is **Example Exchange** with no added
qualifier. **stop_tracing** is independent and accepts true/false. Active stops
show **STOP TRACING**. Classification has been removed. Source and notes appear
in the local HTML attribution register and JSON/CSV exports, not in Miro cards. Templates
are in `examples/address-attributions-template.csv` and `.json`.

Only Address is required. Address-only lists default to suspected confidence and
stop_tracing=true. Explicitly set false to keep tracing through a named address.
Names, sources and notes are free text; notes supports multiple lines. Optional
enabled and observed_at fields remain available. Limits: 5,000 rows / 512 KiB.
Import the exact public address spelling from the trace/explorer; address text is
validated offline but network/checksum and confidential aliases are not resolved.

Existing saved assessments retain their explicit stop decisions; legacy
candidate/corroborated confidence is interpreted as suspected without rewriting
archived evidence. Remove classification and choose the new confidence spelling
when preparing a new CSV upload. See [explicit attributions and convergence
highlighting](docs/address-attributions-and-convergence.md) for output details,
compatibility, and safe Miro updates.

```bash
liquid-trace address-import --case CASE_DIRECTORY --file attributions.csv --dry-run
liquid-trace address-import --case CASE_DIRECTORY --file attributions.csv --approve-plan APPROVAL_SHA256
```

The approval hash binds the case, content, options and current assessments. Stops
affect first runs and continuations; selected seed outputs are not exempt.

To inspect the ELK layout for a saved run:

```bash
liquid-trace layout-preview --case cases/theft-liquid --run latest --open
```

This action fetches missing address transaction counts, computes placement locally, and saves `graph.html`, `graph.svg`, `graph.json`, and `layout-report.json` in a new `previews/<run-id>-elk-<id>/` directory. Optional `--connector-style straight|curved|elbowed`, `--include-fees`/`--exclude-fees`, `--run RUN_ID`, and `--out NEW_DIRECTORY` override the selected preview without changing its archive or case settings.

In the ELK preview, click a transaction or address node, or focus it with Tab and press Enter, to open its **Explorer** link in a new tab. These links also work when the downloaded SVG is opened directly in a browser. They are limited to Blockstream's Liquid and Liquid testnet explorers; saved synthetic evidence and special event nodes have no live links. Preview generation can contact the statistics API for missing address counts. Once saved, previews remain self-contained and render offline; opening an Explorer link is a separate browser request. See [automatic address counts](docs/automatic-address-counts.md). Choose **Refresh layout preview** to add links and current presentation colors to an older saved run.

Before layout, the tracer estimates each main-graph connector caption's full width and height at its displayed font size, including `vin`/`vout`, explicit change labels, amounts and assets. ELK receives these dimensions so it can reserve label space while arranging nodes and routes. The separate chronological fee row remains outside ELK's placement. The estimate includes padding and does not require a browser or additional font packages. It is not an exact measurement of Miro's font rendering. Reserving this space can make the graph wider. Refresh an older layout preview to calculate the new spacing; use **Sync and reorganize Miro graph** to apply new node positions to an existing board.

The local SVG preview draws ELK's calculated connector bends for elbowed lines. Miro's documented [REST connector API](https://developers.miro.com/reference/create-connector-1) and [Web SDK](https://developers.miro.com/docs/websdk-reference-connector) expose endpoint attachments and connector style, but no arbitrary intermediate bend points. Miro therefore chooses its own routes and caption positions. Reserving space improves the layout input, but cannot guarantee that the board matches the local preview.

New ELK layouts automatically remove redundant horizontal padding around connector captions. ELK can allocate the full between-layer gap on both sides of a caption; the tracer closes only globally empty vertical strips after layout. Nodes and labels retain their dimensions, vertical positions and order, while connector bends move with them. The default horizontal gap between separated node columns is now 150 units, reduced by 25% from 200, in base layouts, ELK and local address compaction. Protected space preserves label room and connector channels. Object sizes, text sizes and vertical spacing settings stay unchanged, so total graph width may shrink by less than 25%. Crowded regions with no empty strips stay in place, so this does not solve every long cross-branch connection. The layout report records the removed width under `horizontal_spacing`. Refresh older previews and choose **Sync and reorganize Miro graph** to apply the new positions to an existing board. Ordinary sync still preserves manual positions.

Connector attachment placement now compares ELK's natural port ordering with the previous traced-first alternative. It scores lines through objects, crossings, overlapping connector segments, reversed endpoint order and coincident attachment points before choosing the shorter layout. A simple midpoint-elbow estimate also checks patterns that Miro may draw differently from the local ELK routes, including vertical lines through unrelated objects. This is an estimate, not a guarantee of Miro's final routing. The existing ELK calculations are reused; up to two alternatives per seed are retained for comparison.

Inputs spending an exact earlier displayed output still have a traced-first preference when layout quality and proximity scores are equal. Physical attachment order may differ when it avoids crossings, so an upper context address can connect above a lower traced address. The original `vin` numbers, transaction records, labels and CSV order never change. Merely reusing an address does not give an unrelated UTXO priority. Explicit change objects keep their aligned rows and one centered continuation; other inputs use distinct slots. Shared address circles retain one identity and their individual connections. Refresh older previews and choose **Sync and reorganize Miro graph** to apply the new attachment placement to existing objects. Ordinary sync preserves manual positions and ports.

Branch placement now gives exact displayed UTXO continuations more weight than external input context, with explicit change paths receiving the highest priority. ELK compares arrangements that favor shorter vertical travel along those flows. Node overlap, lines through nodes and connector crossings still take precedence over shorter travel; related branches are not guaranteed to become adjacent when other connections prevent it. A subsequent local pass brings isolated context input circles closer to their transaction only where node, caption and connector clearances permit. These steps preserve every node and connection and do not infer ownership or value allocation.

External context circles and context summaries also receive a final connector-clearance check. This includes input-only addresses shared by several transactions, while selected hubs, named-group core objects, designated change rows and displayed output addresses retain their positions. The check reserves space around both saved ELK paths and estimated Miro bends, including return and fee connections. Blocked context objects move to a nearby safe position where one can be established; clearing a line takes priority over a smaller footprint and may require longer or crossing context connections. Every changed input connection is checked against the other shapes, with its original attachment, `vin`, outpoint and summary membership preserved. Local compaction retains these safeguards. The report records unresolved objects and incomplete checks under `branch_organization.context_clearance`; Miro's final routing can still differ. Refresh older previews and choose **Sync and reorganize Miro graph** to apply the new positions to an existing board.

Traced connections use thicker lines and context connections use thinner lines in Miro and local exports. The existing dark teal and gray meanings remain. Individual input connections stay visible. Sync updates previously managed widths while preserving analyst changes to connector widths, colors and captions, including during reorganization.

**Group isolated context inputs** is an optional investigation display setting and is **off by default**. It replaces two or more eligible external input addresses belonging only to one transaction with a neutral rectangle showing the address and input counts. Each original `vin`, outpoint, quantity and input connector remains separate. Complete original address records stay in the summary's `details.members` in `graph.json` and the local detail views. Addresses connected to another displayed branch, displayed outputs, marked change, seeds, attributed addresses and other protected investigation objects remain individual. The summary indicates presentation only, not common ownership. CLI preview actions also accept `--group-context-inputs` and `--ungroup-context-inputs` without changing saved evidence.

Context summaries use a compact 240 × 160 rectangle regardless of input count. For groups above eight inputs, diagrams omit the repetitive connector captions so hundreds of text boxes do not push a branch far away. Every connector remains, and its original label, amount, `vin` and outpoint remain in `graph.json`, the edge CSV, and local SVG hover details. Small summaries and other connections retain their captions. Turning grouping off restores the individual address display and captions. Refresh the preview and choose **Sync and reorganize Miro graph** to shrink an unchanged generated summary on an existing board. Edited captions and manually resized, rotated or annotated summaries stay protected.

After changing grouping, refresh the preview and use **Sync and reorganize** to apply the reviewed arrangement. Replacing existing board objects when grouping or ungrouping requires this explicit reorganization; ordinary sync refuses the change to preserve manual positions and connector attachments. Turning grouping off restores individual address circles. Miro synchronization replaces the affected generated circles or summary and their input connectors, so those affected Miro item IDs can change; transaction and unaffected item IDs remain. The preflight blocks retirement of objects with manual text or style edits, resizing, grouping, or unmanaged connections instead of silently discarding those edits. Preserve any attached Miro comments separately before toggling grouping because the API cannot inspect them. First publication and subsequent syncs with unchanged grouping continue to work normally.

**Separate branch hubs** is a manual list in investigation settings. Enter one full Liquid address per line. Selected visible addresses become layout roots in a separate entry column. Transactions spending from a hub can share a vertical column, with their outputs and later activity branching right. A verified spend through that hub restarts the layout depth; other inputs can still require a transaction to sit farther right. Each address retains one identity and every connection, including incoming return connections. No activity-count threshold selects hubs automatically, and selection does not classify an address as a service or change the tracing seeds, hops or fetched data. Review the preview before applying the setting. Remove an address from the list to return it to ordinary placement.

New layout and compact previews also include **Overview and detail pages** in `details.html`, with the page map saved as `details.json`. The atlas contains the complete overview, activity maps and overlapping detail pages at a consistent readable scale. Connection indexes identify which pages contain each original connector; boundary connections remain visible across neighboring pages. Empty tiles are omitted, while tiles containing only connector segments are retained. Open the atlas link beside the preview to inspect it or print to PDF using A3 landscape, 100% scale and browser headers and footers off. This is a local export of the calculated drawing; it does not reproduce Miro's automatically chosen bends. The complete SVG and graph remain available even if an unusually large drawing exceeds the optional atlas's page budget.

The report compares the saved graph's baseline arrangement with the proposed ELK arrangement, **not the current live Miro board**. It estimates line crossings, object overlaps, and lines through unrelated objects. Counts marked `≥` are lower bounds when the comparison limit is reached. These collision counts exclude label boxes and Miro's automatic curves; zero estimated crossings does not guarantee a collision-free board. ELK now tries 25 deterministic layout seeds by default for every graph size, including graphs with thousands of objects. Each seed can yield a natural-port and a traced-first candidate, so the default compares up to 50 arrangements. The first seed runs alone and measures its peak process RAM. Remaining seeds run in bounded parallel batches sized from that measurement and retain the best-scoring result. The calibration seed counts toward the requested attempts and can be the winner. Only the current batch and the winning layout are retained, so memory use does not grow with the total attempt count. The comparison budget limits quality measurement work, never the accepted graph size or number of rendered objects. Optimization makes no external layout requests and uses no secrets.

Set **Investigation settings → Graph layout → Layout attempts** in the browser or terminal to choose 1–1,000 attempts. More attempts take longer and may find a better arrangement; they do not add tracing hops or API requests. Large graphs no longer fall back to a single seed. The seed sequence is deterministic and extends the same prefix when the count increases. Progress shows each active attempt, the worker count and shared heap allowance. Cancellation stops all active workers and leaves saved evidence and completed previews intact. Layout reports record the attempted seeds and selected winner.

If a worker fails on one seed, ELK keeps earlier valid candidates and continues through the remaining requested attempts. A completed result shows how many attempts succeeded or failed and uses the best verified candidate. Every graph object and connection remains included. A compatible preview with failed attempts can still be reused during sync; the warning and counts remain visible. If every attempt fails, no new layout is saved or sent to Miro. Invalid input, setup errors, malformed results and cancellation still stop the entire search instead of publishing a partial search result.

For a one-time override, use `liquid-trace layout-preview --case cases/theft-liquid --layout-attempts 50`. The same option works with `compact-preview` and fresh `miro-sync` layouts. A matching completed ELK preview is reused during sync; changing the attempt count or using an older preview requires a new calculation. Refresh the preview and use **Sync and reorganize** to apply the selected arrangement to an existing Miro board.

The search keeps branches together at later forks as well as at starting transactions. It groups each transaction’s exclusive outputs, orders transactions near their connected inputs and outputs, and carries that order through later branches. Branch grouping takes priority over vertical date order. The canvas may expand to separate branches; shorter connections and compact spacing are preferred once the arrangement is clean. The first attempt uses this branch ordering, including a one-attempt search. Later attempts alternate with unconstrained ELK arrangements so real joins can choose a better result. Named-group centering also uses the local branch order.

Branch membership follows the saved UTXO lineage of each starting transaction. Multiple selected outputs of one starting transaction remain one branch. An address receiving unrelated outputs from different branches remains one shared circle; its combined display membership is not propagated into unrelated spending transactions. Context objects can stay near their associated transaction without becoming traced evidence. No objects or connections are duplicated or removed.

Candidate selection still prioritizes object collisions, arrows through objects, crossings, attachment conflicts and overlapping lines. After those checks and named-group alignment, it prefers fewer interleaved branch interiors and connecting transactions closer to the relevant branch boundaries. It then compares local input/output ordering, sibling grouping and transaction proximity before remaining connection-length and input-policy tie-breakers. These are presentation preferences: heavily merged graphs or branches connecting several distant trees may not permit completely separate lanes. ELK recalculates all connector routes for the proposed arrangement; Miro still chooses its final bends.

Refresh the layout preview after updating, then use **Sync and reorganize graph** to apply the new arrangement to an existing board. Ordinary sync preserves manual positions. Create or update frames separately when the arrangement is finished.

After aligning an explicitly designated change output, the other outputs attach below the change line in destination order. Original `vout` numbers stay unchanged. Small connector steps can also be removed by sliding an isolated attachment along its address circle to match the transaction port's height. Transaction input spacing and object positions stay fixed. This adjustment applies only when complete geometric checks show no increase in measured crossings, overlaps or object intersections; normal bends between different rows remain. Refresh older previews to use these attachment corrections. Miro still chooses its own intermediate bends, so several elbowed connectors can share or cross a routing lane.

To remove unnecessary space after ELK, choose **Compact graph** in either interface:

1. Select the saved run and calculate the local compact preview. This uses the existing ELK engine, followed by address and disconnected-component compaction. It makes no Blockstream or Miro requests.
2. Review the comparison page. It reports width, height, area, total connection length, and address-to-transaction distance before and after compaction. Open the original ELK drawing separately to compare it with the compact drawing. The baseline is a fresh local ELK layout, not your manually arranged Miro board.
3. Choose **Apply compact layout to Miro** and confirm that you reviewed it. The program applies that exact saved proposal, including its fee and connector settings, without rerunning ELK. This explicitly replaces managed object positions. Normal **Sync to Miro** continues to preserve manual positions.

Local compaction brings eligible terminal output addresses toward their creating transactions and external input addresses toward their spending transactions. An address between one producer and one spender can move away from its original display column while preserving forward order. Disconnected activity components move as intact groups, including their connector routes, with space reserved for activity-frame padding and titles. Transaction shapes only move with their whole activity component. Existing node sizes and minimum clearances remain unchanged; captions, neighboring objects and connector channels constrain which moves are accepted. Cyclic or ambiguous merged-address occurrences are left in place when a safe move cannot be established.

If fee flows are displayed, the chronological fee row stays fixed. Components connected to that row retain their positions during component packing; eligible address moves can still be made. Hiding fee flows permits additional component-packing opportunities. Existing main-graph and full-drawing dimensions are reported separately because the fee row and legend can determine the overall extent. A shorter local connection does not necessarily reduce the entire drawing's width.

Compaction uses spatial indexes and bounded candidate checks instead of comparing every object with every other object. Unchecked or unsafe moves are skipped, and the report says when optimization work was truncated. This does not cap graph size or remove any node, edge, or evidence. An already compact or crowded layout may remain unchanged. Caption bounds are estimates, and Miro chooses its final connector routes.

Each comparison is saved under `previews/<run-id>-compact-<id>/` with complete before/after graph files, SVGs, HTML, a layout report, and the exact Miro plan. A checksum manifest marks completion and binds the proposal to its archived run and the current service assessments. Incomplete, modified, wrong-case, or stale-service previews cannot be applied. Changing ordinary display defaults does not modify a previously reviewed proposal; calculate another preview to use the new defaults. Preview discovery and application also work after reopening the investigation. Service assessments stay locked during compact application so the reviewed labels cannot change mid-sync.

Manually resized or rotated Miro shapes keep their live dimensions when those dimensions fit the reviewed positions. An incompatible shape stops compact application before board writes, with instructions to restore its previewed dimensions or use ordinary reorganization to accommodate the current sizes. A single enlarged shape does not scale the entire compact proposal. When the arrangement is finished, use **Create / update Miro frames** to fit export frames to the resulting geometry.

The direct commands are:

```bash
liquid-trace compact-preview --case cases/theft-liquid --run latest --open
liquid-live miro-sync --case cases/theft-liquid --run RUN_ID \
  --compact-preview PREVIEW_ID --reorganize --max-new-items 750
```

Use the run and preview IDs printed by the first command. Optional `--include-fees`/`--exclude-fees` and `--connector-style` belong on `compact-preview`; application uses the frozen preview settings. Add `--dry-run` to the second command for local publication counts. Archived tracing runs remain unchanged throughout this workflow.

ELK now attempts the complete selected graph without an application-imposed node count, connection count, input/output size, coordinate, or elapsed-time ceiling. The earlier 10,000-object / 30,000-connection guards and 30-second timeout have been removed. A large graph is not automatically switched to the dependency layout. ELK finishes, reports an engine error, or stops when you cancel. Actual capacity still depends on graph complexity, available RAM, the JavaScript runtime, and processing time; accepting 100,000 objects does not guarantee a fast layout. Previously saved dependency-layout fallback previews remain readable.

The existing `devenv.nix` now sets `LIQUID_RENDER_HEAP_MB = "auto"`. At each calculation, the tracer requests a JavaScript heap allowance equal to 90% of the currently available memory, accounting for host memory and process/ancestor limits on standard Linux cgroup v1/v2 mounts. If memory detection is unavailable, auto uses 1,024 MiB. ELK receives the budget directly through Node's [max-old-space-size option](https://nodejs.org/docs/latest-v24.x/api/cli.html#--max-old-space-sizesize-in-mib). Arbitrary `NODE_OPTIONS` and credentials remain excluded from the ELK worker. Its progress reports graph size, each worker’s heap budget and the shared allowance. Concurrent ELK workers divide one total allowance; each worker does not receive 90% independently.

The existing `devenv.nix` also sets `LIQUID_ELK_WORKERS = "auto"`. After the first measured attempt, auto selects the worker count from available CPUs, Linux CPU quotas, remaining attempts and memory, with a maximum of 64. Each worker’s share must cover twice the largest observed peak RAM use, with a minimum share of 1,024 MiB. This headroom allows for variation between seeds. A smaller total budget still permits one worker. Later measurements can reduce concurrency; unavailable measurements keep execution serial. Set `LIQUID_ELK_WORKERS = "1";` for serial execution, or a whole number from 1 to 64 as the concurrency ceiling. CPU and memory limits still apply. This changes execution speed, not the number of layout seeds or the scoring rules. Completed compatible previews remain reusable across worker settings.

For a fixed allowance, edit `LIQUID_RENDER_HEAP_MB` in the existing `devenv.nix`, for example `LIQUID_RENDER_HEAP_MB = "32768";` for a 32 GiB total ELK heap allowance on a 64 GiB workstation, divided among the active workers. Mermaid continues to use this as its single-renderer allowance. Stop the application and re-enter `devenv shell` after changing it. No additional environment file or API secret is needed. This is a requested heap allowance, not a memory reservation or a total-process limit; Python, native buffers, Chromium and other applications also consume RAM. Explicit budgets are not silently reduced. Auto recalculates the total before each batch, after the previous workers exit, so closing other applications can raise the next batch’s allowance. It may still choose less than 90% of installed RAM. Peak process RAM is measured using Node’s [resource usage high-water mark](https://nodejs.org/api/process.html#processresourceusage), including native memory as well as the JavaScript heap.

Signal 6 by itself does not prove an out-of-memory failure. Renderer errors now distinguish recognized heap exhaustion, call-stack exhaustion, browser startup failures, and browser protocol timeouts without printing investigation labels. If a parallel attempt reports memory exhaustion, the current batch finishes and that seed retries once alone with the full freshly calculated allowance. Remaining attempts then run serially. A killed parallel worker receives the same retry without claiming that memory was the cause. Successful retries count once toward the requested attempts; final failures retain the existing visible warnings and counts. An abrupt worker kill reports that memory exhaustion is possible but unconfirmed. Closing applications can raise an automatic allowance for the next batch or serial retry; it does not change the cap of a worker already running. Unrecognized failures remain qualified. A heap abort during an eight-hop calculation after a successful one-hop preview is consistent with runtime memory pressure, but graph size and topology matter more than hop count alone.

The ELK worker also reports fixed error categories and calculation stages when available. Saved search metadata includes failed seeds and safe error codes, without raw exception text or investigation labels. An unrecognized exit still has an unknown cause; retaining another seed's successful result does not diagnose or repair the engine failure itself.

For an offline ELK or Mermaid preview, use **Cancel calculation** in the browser or terminal menu. The terminal menu also supports **Ctrl+X**; a direct CLI command can be interrupted with **Ctrl+C**. Cancellation stops the calculation and its renderer children. Saved runs and existing completed previews stay intact. ELK reports elapsed time while working, without claiming a percentage complete. Explicit Miro reorganization still replaces managed positions, while ordinary sync retains its existing manual-position rules and publication budgets.

The single devenv installs the locked ELK dependency when entering the shell or building the browser interface. After pulling changes, re-enter `devenv shell`; `liquid-layout-setup` can also refresh the installation. Outside devenv, install Node.js 22.12 or newer and run `npm --prefix layout ci --ignore-scripts` from the project root. The [elkjs library](https://github.com/kieler/elkjs) calculates layout; Miro remains the renderer and editable board.

To create a Mermaid chart directly from a saved investigation:

```bash
liquid-trace mermaid --case cases/theft-liquid --open
```

The command selects `latest` automatically. Optional `--run RUN_ID` selects a historical snapshot; `--out NEW_DIRECTORY` chooses a new destination. `--include-fees` and `--exclude-fees` override visibility for this preview only. Each invocation verifies the saved evidence before rendering and leaves archived runs and Miro mappings unchanged. The existing `devenv.nix` supplies Mermaid CLI and Chromium on Linux; leave an older shell and enter `devenv shell` after updating the project.

Mermaid preserves the graph's arrows, shapes, colors, dates, and compact quantity labels, but computes its own left-to-right layout. Fixed Miro connection sides, manually arranged positions, and the chronological fee row are not copied into a Mermaid-rendered chart. It now runs for the full selected graph without the former 1,000-object / 2,000-connection bypass or 120-second deadline. Mermaid's source-size and edge settings are sized to the actual graph. Browser rendering can still take substantial time or exhaust runtime resources. The complete `graph.mmd`, `graph.json`, and `mermaid-node-map.json` are saved before rendering, and completed HTML remains self-contained for offline viewing. Previously saved direct SVG fallback previews remain available.

The generated `puppeteer-config.json` also disables Puppeteer's [default 180-second protocol timeout](https://pptr.dev/api/puppeteer.connectoptions), which otherwise applies to the browser call containing the entire Mermaid calculation. Browser startup retains its normal timeout and sandbox behavior. The same requested heap budget is supplied to the Mermaid CLI's Node process and Chromium, while the Nix wrapper still supplies Chromium's executable. A Chromium build can impose a smaller internal heap limit even when more RAM is available; ELK's separate Node worker is the preferred layout path for very large investigations. Increasing RAM does not guarantee that Mermaid will render them.

On a Mermaid renderer failure, a bounded diagnostic tail is saved locally as `mermaid-renderer.log` with owner-only permissions when possible. It can contain graph labels and is intentionally excluded from browser downloads. The displayed error uses fixed diagnostic categories and points to the saved source; a generic exit 1 no longer claims the installation is broken. Retry the selected saved run after updating, without another Blockstream trace.

The local SVG exporter also has no fixed object, connection, coordinate-range, or route-point ceiling. It still validates finite geometry and retains every displayed connection. Very large SVGs may be slow to open in a browser; **Create CSV export** remains available for analysis. A saved continuation is cumulative: reducing the next run's hop count will not shrink the existing graph. You can retry either preview from a saved run without fetching Blockstream data again. Trace hop/request/time budgets and Miro's per-sync publication budget still use the investigator's settings.

To export CSV tables directly:

```bash
liquid-trace csv-export --case cases/theft-liquid
```

`--run RUN_ID` selects an older saved snapshot; `--out NEW_DIRECTORY` chooses a destination. Without either flag, the command uses the saved `latest` pointer and creates its own export directory. Existing directories and destinations inside `runs/` are rejected. The output contains:

| File | Contents |
| --- | --- |
| `nodes.csv` | Graph objects, full logical IDs, labels, colors, and details. |
| `edges.csv` | Directed graph links, source/target IDs, outpoints, roles, and quantities. |
| `inputs.csv` | Transaction inputs, previous outputs, addresses, and public values/assets. |
| `outputs.csv` | Transaction outputs, addresses, public values/assets, commitments, and trace status. |
| `spends.csv` | Recorded links from an output to its spending transaction and input. |
| `events.csv` | Fees, peg-outs, unspendable outputs, peg-ins, and issuance events recorded in the run. |
| `frontier.csv` | Unresolved branches and their stopping reasons. |

`nodes.csv` and `edges.csv` use the current shared-address presentation and case fee setting. Optional `--include-fees` or `--exclude-fees` affects those two graph tables for this export only. The other five tables are exact copies of verified archived CSVs and retain all recorded fee data. Unavailable numeric values remain empty fields in detailed tables; graph captions use `??`. Nested details remain JSON within quoted CSV cells. `export.json` records provenance and display options, and `SHA256SUMS` covers the new bundle. Exporting does not modify saved runs, settings, or Miro mappings.

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

Transaction inspection and tracing overlap up to **eight independent explorer requests**. A shared limiter spaces request starts across all workers, including OAuth and retries. Concurrent branches requesting the same endpoint share one response and evidence record. One `/tx/:txid/outspends` response serves all outputs of its transaction within a run. The [documented Esplora API](https://github.com/Blockstream/esplora/blob/master/API.md) has no arbitrary transaction-hash batch lookup, so this uses concurrent individual GETs.

The paid `enterprise.blockstream.info` endpoint uses the selected operating target of **49 requests/second**, shared across all workers: at least **20.408 ms between request starts**. The nonsecret `LIQUID_BLOCKSTREAM_ENTERPRISE_RPS = "49"` setting in the existing `devenv.nix` persists this target for every interface. The client also defaults to 49 for this endpoint outside devenv. Public and other endpoints retain the 4 requests/second fallback.

The 49 RPS target is an investigator-selected setting. Blockstream's [paid API documentation](https://help.blockstream.com/blockstream-explorer-api/set-up-explorer-api/create-and-manage-your-api-keys) describes higher limits without publishing a numeric requests-per-second allowance. A verified account allowance supplied through `LIQUID_BLOCKSTREAM_API_RPS` overrides the operating target and uses **95% of that allowance**; `--api-rate-limit NUMBER` overrides that allowance for one CLI trace. These settings use requests per second, not hourly quotas or credit balances. The shared 429 cooldown remains active if the service throttles requests.

`--api-workers 1` disables overlapping trace requests; the default is 8. `--min-interval` can impose a longer gap, but cannot raise the rate ceiling. Run evidence records the effective fetching settings in `trace.json`. Fixtures bypass network pacing. The limiter coordinates one client/run, not separate Liquid-trace processes sharing the same API account.

HTTP 429 responses pause all workers. Transient failures retry within the same request/time budget; retries and token refreshes are intentional additional attempts. A requested cooldown above 30 seconds stops the run for later continuation. Successful in-flight responses are recorded before returning a failure or interruption. Near a traversal budget, fetching becomes serial to preserve the remaining work allowance. Completed transaction bodies can be reused from the evidence cache; spend status is refreshed for each new run so continuation can discover newly spent outputs. Duplicate suppression applies within a lookup or run, not across these deliberate refreshes.

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

For the unified multi-board workflow, use **Miro boards** and the
[investigation workflow guide](docs/investigation-workflow.md). The following
commands describe the existing full-graph board tools, which remain available
for compatibility and recovery.

Obtain an access token with **`boards:read` and `boards:write`** scopes and access to your Miro team. Follow [Miro's REST API quickstart](https://developers.miro.com/docs/rest-api-build-your-first-hello-world-app). Store the token with the setup helper. If it expires, replace it and retry the selected action; this program does not refresh Miro tokens automatically.

```bash
liquid-secrets-setup miro
```

In the investigation menu, choose **Create Miro board**. The name defaults to the investigation name, limited to 60 characters. Visibility defaults to **Private**. **Team members can edit** enables team access; public-link and organization access remain private. An optional Miro team ID selects a destination team. Availability depends on your Miro plan and team permissions; a rejected private request does not automatically become team-visible. Creation uses [Miro's board endpoint](https://developers.miro.com/reference/create-board-1) and its `boards:write` scope.

Confirming creation retrieves the token through SecretSpec, creates an empty board, and saves its ID in `case.json`. The board's name, URL, and creation receipt are saved under `miro/board-creation.json`. The menu then reuses that board for previews and syncing. Opening or cancelling the form does not load credentials. For the same action from the CLI:

```bash
liquid-live miro-create-board --case cases/theft-liquid
liquid-trace miro-sync --case cases/theft-liquid --dry-run
liquid-live miro-sync --case cases/theft-liquid
```

Creation accepts `--name TEXT`, `--visibility private|team`, and `--team-id ID`. Repeating the command reuses a linked board or a saved successful creation receipt. If a connection failure leaves the outcome uncertain, it blocks another creation request. Inspect Miro, then link the created board through **Investigation settings**. If no board exists, create one in Miro and link it there. Completed run evidence remains unchanged.

To start over with the updated graph, choose **Rebuild on new board** in the Miro panel or investigation menu. This creates a new private board, publishes the selected saved run with current labels and display settings, and makes it the investigation's linked board. The previous board, its comments and manual edits, and its local publication mapping remain intact. Frames are still created separately with **Create / update Miro frames** after the graph is finished.

The rebuild prepares and validates the layout before creating the board. Its editable new-item allowance must cover the complete fresh graph, including shapes and connectors; an insufficient allowance stops before board creation. Increasing this one-action allowance does not change the investigation defaults. Existing uncertain items on the previous board do not prevent rebuilding on a separate board.

```bash
liquid-live miro-rebuild-board --case cases/theft-liquid --run latest \
  --source-board 'CURRENT_BOARD_ID' --name 'Updated investigation graph' \
  --max-new-items 5000
```

The source-board selection identifies this rebuild. If interrupted, **Resume board rebuild** or the same CLI command reuses its saved graph and acknowledged new board instead of creating another. If Miro did not confirm board creation, inspect Miro and link the recovered board in settings before using normal sync; an uncertain creation request is never automatically repeated. Once a rebuild is complete, another deliberate rebuild starts from the newly linked board. Earlier board URLs remain recorded with the rebuild.

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

Normal **Preview Miro** and **Sync to Miro** rebuild the current graph presentation in memory from the verified saved trace, current fee setting, and connector appearance, then calculate the ELK layout locally. This applies display changes without another Blockstream request or changes to archived run files. Live sync updates managed labels and colors while retaining manual edits and positions. Reports record the archived and rendered plan hashes and presentation version. An explicit `--plan` continues to use that verified plan as supplied.

Transaction squares show the recorded block date as `YYYY-MM-DD UTC`, using Esplora's saved `status.block_time` for confirmed transactions. This is the containing block's date, not an exact transaction creation time. Unconfirmed transactions show `Unconfirmed`; missing or invalid confirmed dates show `Date ??`. These labels reflect the saved observation. To add dates to an existing board, reopen the investigation and choose **Sync to Miro**; no new trace is needed. Manually edited transaction labels are preserved and reported as conflicts instead of being overwritten.

To add the selected saved run and rearrange the board together, choose **Sync and reorganize graph**. After confirmation, it applies an ELK arrangement to existing and new managed objects and updates their connector appearance and attachment points. This replaces manual positions and connector routing choices; existing dimensions and manual text/color annotations remain intact. Previous coordinates and changed connector choices are recorded with the sync state for review. The operation considers mapped items; unrelated board content is not an obstacle in its calculations.

The equivalent direct command is:

```bash
liquid-live miro-sync --case cases/theft-liquid --reorganize
```

`trace`, `export`, and `miro-sync` accept `--include-fees` or `--exclude-fees` as an explicit display override. Otherwise they use the selected case's setting, defaulting to excluded. These flags do not alter tracing or erase fee evidence. A supplied `--plan` has its own reviewed fee selection; regenerate an export to change it instead of combining that plan with a fee override.

Use the **same case directory and board** for later runs. Sync creates native [shapes](https://developers.miro.com/reference/create-shape-item-1) and [connectors](https://developers.miro.com/reference/create-connector-1), checks existing items, and updates compatible managed fields. It does not call Blockstream. A per-board JSON snapshot under `case/miro/` maps stable graph IDs to remote item IDs. While publishing, a private SQLite journal beside that snapshot stores incremental changes; successful completion or a handled interruption folds committed changes back into the JSON snapshot. Keep the entire directory when copying a case, including any active journal files. `case/miro/reports/` retains sync reports separately from immutable run exports.

- Repeating the same run creates no duplicate acknowledged objects. A newer continuation adds new graph objects; already mapped circles and squares are reused. Per-run summary boxes are no longer added. Normal sync removes previously generated run summaries after checking for manual edits or attached connectors. Run metadata remains in the saved local history and exports. Historical explicit plans containing run summaries require a fresh preview before publication.
- Normal sync preserves existing positions, dimensions, and manual connector choices. New items are placed relative to connected mapped items and avoid mapped shape bounds. If those anchors cannot accommodate a continuation, sync stops before writes and offers reorganization. **Sync and reorganize graph** changes managed positions, connector appearance, and attachment points while preserving dimensions. Explicit legacy plans retain their original placement and attachment behavior.
- Existing generated frames remain unchanged during graph sync and reorganization. Shapes that must move can be detached from their generated frame while preserving canvas coordinates. The separate frame action checks for unmapped children before changing a frame; move those annotations outside the generated frame and retry. Shapes inside unrelated frames or unsupported containers still require moving back to the canvas.
- Existing content, captions and styles are updated only where the current value still matches the program's saved baseline. Analyst edits are retained and listed as conflicts in the report, except for positions and connector appearance/attachments explicitly replaced by reorganization. Avoid simultaneous content/style edits during a live sync: the API check and update are separate requests.
- Excluding fees removes only generated fee connectors and diamonds identified from the saved trace. Edited fee labels, captions, or managed styles stop removal before board writes. Extra comments and unmapped connectors attached to fee diamonds are outside those checks; retain fees when these annotations need to remain attached. Enabling fees again recreates their representations. Other mapped objects are retained; an unexpectedly missing object or changed connector endpoint stops sync for inspection. Interrupted fee removals retain recovery state.
- A board mapping is bound to one case, API source, and address mode. After a run has been synced, extend that run (or a later descendant). Older snapshots, sibling branches and independent roots cannot overwrite newer graph classifications. You may skip intermediate unpublished runs.
- Finish an interrupted sync before switching to another run. Resolving a pending creation alone does not finish that sync.
- The default cap is **750 new shapes and connectors per graph sync**, not total board size. The separate frame action applies its own new-frame cap. Use `--max-new-items` to change either action's limit. Existing-object checks can still take time on a large graph.

### Export the whole graph or an activity group

Frames are a separate finishing step. After syncing and arranging the graph, choose **Create / update Miro frames** in the browser or terminal interface. This creates one **Complete graph** frame and one **Activity** frame for each connected part of the published graph. Three disconnected starting trees produce four frames. If a continuation connects two trees, the next explicit frame action expands their retained activity frame, removes the obsolete generated frame, and leaves three frames in total. Repeating the action reuses the frame IDs. The outer frame also includes the legend. After syncing away old run summaries, run this frame action to fit the reduced drawing. Normal sync, reorganization, and compact-layout application do not create, resize, or remove frames.

Starting transactions are numbered globally by recorded confirmation time, oldest first. Frames list their actual numbers, for example **Activity 1 · Starting transactions 1, 4, 7**, and the corresponding boxes show **Starting TX 1**, **Starting TX 4**, and **Starting TX 7**. Multiple seed outputs of one transaction share one number. Equal timestamps use full transaction keys as a deterministic tie-breaker; undated or unconfirmed starts sort last. **Sync to Miro** refreshes starting-transaction labels; **Create / update Miro frames** refreshes generated frame titles while preserving manual edits and existing frame IDs, without retracing. See [chronological starting-transaction numbers](docs/starting-transaction-index.md) for the saved mapping and historical-preview behavior.

A **12-pixel red border** now distinguishes two independent signals with text:
**INPUT MERGE** marks transactions where distinct starting lineages meet through
verified UTXO inputs. **SHARED ADDRESS** marks an address receiving outputs from
different starting branches, and **all participating sending transactions**,
including the earlier sender. This second check does not require a joint spend.
It runs across the whole saved graph after UTXO lineage propagation, so it can
update earlier objects without falsely propagating one output's origins through
another output at the same address. Starting transactions can qualify. Fill
colors, selected-seed red priority, and evidence remain unchanged.
Miro attribution cards and separate stars are no longer generated. Source and notes
remain in local exports and Address review. Normal sync retires unmodified old
annotations and refreshes borders without retracing. See
[attribution and convergence behavior](docs/address-attributions-and-convergence.md).

Groups follow visible UTXO connections, including displayed context links. They do not infer common ownership or identify services. The default shared-address display joins activity groups through a common address circle. This visual connection does not establish a spend between unrelated UTXOs. Explicit legacy outpoint exports keep repeated address occurrences separate. Fee visibility follows the investigation setting.

Use **Sync to Miro** after selecting a saved continuation, or **Sync and reorganize graph** to also recalculate object placement. Once the graph is finished, run **Create / update Miro frames**. It uses the latest completed graph sync and its saved display choices, reads live object positions and dimensions, and fits frames around your manual arrangement. It does not run ELK, fetch blockchain data, or add missing graph objects. Sync an unsynced continuation first. Older mappings without a saved framing snapshot may need one ordinary graph sync before this action is available.

```bash
liquid-live miro-frames --case cases/theft-liquid --run latest
```

Retained manual frame titles and colors are preserved; generated frame geometry is recalculated. Obsolete generated frames are removed, while unrelated frames remain untouched. If you extend or rearrange the graph later, run the frame action again when ready to export.

Select a frame by its title, open its **three-dot menu**, and choose an export option. Select the Complete graph frame for the entire graph, or an Activity frame for a section. Miro also supports exporting a selection of frames. See [Miro's frame export instructions](https://help.miro.com/hc/en-us/articles/360018261813-Frames).

These are transparent, visually nested rectangles, without a native frame-parent hierarchy. Shape creation omits the optional `parent` field, restoring the request format used before export frames were added. If Miro assigns a new shape to a frame, sync verifies its canvas coordinates, saves its acknowledged ID, then detaches that shape while preserving its visible position. Each creation batch finishes this work before the next batch, preventing generated frames from accumulating attached children toward the API's 5,000-item limit. The parent frame itself is not edited by this cleanup. Frame bounds cover shape extents plus padding; Miro routes its own connectors, so long curves or captions can extend beyond those bounds. Manually interleaved groups or the shared horizontal fee row can also produce overlapping frame regions. Check the frame preview before exporting and adjust the arrangement if needed. See [Miro's parent relationship API](https://developers.miro.com/reference/update-item-position-or-parent-1).

Repeat sync checks all mapped items before writing. Existing-item reads and edits use up to **four concurrent requests**, with HTTPS connections reused where a proxy is not required. Large connector sets are checked in [pages of up to 50](https://developers.miro.com/reference/get-connectors-1), using individual lookups when a response lacks fields needed for comparison. Shapes retain detailed individual reads: Miro's general item-list response omits styles, which are needed to protect analyst colors and annotations.

New shapes use [bulk creation](https://developers.miro.com/reference/create-items) with up to **20 shapes per request**. Once their remote IDs are recorded, connectors are created with up to four concurrent requests. Response identity is checked rather than assuming that a bulk response has the same order as its request. Unchanged objects need no PATCH, and changed fields on an object share one PATCH. Fee removals remain ordered, connectors before their fee shapes. Explicit reorganization still reasserts connector attachment points because Miro's returned coordinates cannot reliably distinguish automatic and fixed attachment modes.

Interrupted cleanup of an acknowledged new shape resumes through reads and a journaled detach before ordinary sync preflight. It never repeats the creation POST. A changed parent or visible position blocks cleanup so an intervening manual move is not overwritten. Unreadable or unsupported parent geometry leaves creation unresolved for inspection. This behavior also applies when Miro assigns a new shape to an unrelated frame; only the newly created, acknowledged shape can be detached.

Each request intent is durably recorded before dispatch. Accepted object mappings and updates are saved as small journal records, replacing the previous full-file rewrite per object. If an operation fails or is interrupted, sync stops dispatching additional requests and drains responses already in flight. Known unsent or rejected creations can be retried; an uncertain POST is retained for explicit reconciliation. Retrying the same saved run checks the current board and preserves intervening manual content/style edits. A damaged or missing active journal blocks publication rather than silently resuming from an older mapping.

The shared scheduler targets **95,000 API credits per minute**, below Miro's documented standard allowance of 100,000. Costs depend on the endpoint and batch size: a batch of 20 shapes uses 2,000 credits, each connector creation uses 100, each connector-list page uses 100, and individual item reads use 50. Frame creation and updates use 100 credits each; frame deletion uses 500. Batching reduces network round trips; it does not reduce Miro's per-item credit charges. An additional 0.02-second minimum request gap applies by default; an explicit slower interval remains an extra restriction. Response headers can reduce the available allowance and HTTP 429 responses pause workers. Valid rate-reset waits, including a full minute, remain interruptible and no longer fail merely because the wait exceeds 30 seconds; retries still have a bounded attempt count.

A private, token-scoped quota file under the user's cache directory coordinates **processes on the same machine using the same Miro access token**, including different investigations and boards. It stores a one-way token identifier and scheduling data, never the token or board contents. Different tokens or machines have separate local schedulers even when Miro assigns them the same user/application quota; server response headers still apply. These improvements are used automatically by **Sync to Miro** and **Sync and reorganize graph** through the existing devenv and SecretSpec workflow.

Miro also has a separate [100,000-object maximum per board](https://help.miro.com/hc/en-us/articles/360013588560-Board-performance-and-loading-issues), and large boards can become slow before that point. This service limit is independent of the uncapped local ELK and SVG outputs. Publication does not automatically split an investigation or discard objects.

A network-free initial-publication benchmark with 103 shapes, 100 connectors, 150 ms of simulated response latency, and the same 95,000-credit/minute admission rate took **31.95 seconds before these changes and 12.94 seconds afterward (2.47× faster)** here. Shape creation used six requests instead of 103; connector creation still used 100 requests, with overlapping work. Normalized final board contents and mappings were identical, and repeated sync preserved a manual annotation without creating new objects. With 50 ms of simulated latency, both versions took 12.84 seconds because the shared credit allowance was already the bottleneck. In a separate 803-object run with latency and pacing disabled, measured process write bytes fell from about 596 MB to 24 MB; this isolates local overhead and is not a live throughput estimate. The real checkpoint code and disk synchronization remain enabled during measurement.

Run the benchmark from the project environment:

```bash
python3 scripts/benchmark_miro_creation.py --transactions 50 --latency 0.15 --interval 0.02
```

Use `--source-root PATH_TO_CHECKOUT` to compare another version with the same parameters. The existing `scripts/benchmark_miro_sync.py` separately measures reorganization.

Keep **`case.json` and the entire `miro/` directory** with the case. Losing or replacing the mapping can cause duplicates; do not delete state as a retry mechanism. Live sync changes board content, without changing board sharing or inviting people.

To trace and sync in one command:

```bash
liquid-live trace \
  --case cases/theft-liquid \
  --resume latest --additional-hops 1 \
  --max-transactions 20 --max-outpoints 100 --max-requests 30 --max-seconds 60 \
  --miro-board 'https://miro.com/app/board/YOUR_CASE_BOARD_ID/'
```

The run and checksums are saved before Miro updates. A partial publication retains acknowledged progress. Follow the error's recovery instruction and reuse the same saved run and Miro mapping.

If Miro rejects an existing-item update with **HTTP 400 or 422**, repeating the unchanged request is not a fix. The error now identifies the item and endpoint, submitted field names, and recognized rejected fields, error codes and request IDs when available. It excludes field values, board text, raw server messages and credentials. Share those diagnostics to identify the rejected request; an older generic `Miro PATCH returned HTTP 400` log does not contain enough information to determine its exact cause.

Temporary update failures use bounded recovery. Sync reads the item after an uncertain PATCH and accepts a confirmed update, or retries only when the relevant fields still match their previous state. Intervening edits and ambiguous attachment, parent or frame changes stop automatic replay. Rate limits still wait automatically. Shape moves finish before connector updates, while independent updates remain parallel. New-item POSTs retain their separate recovery rules.

Live sync saves a validated ELK preview before publishing. Retrying the same run with the same graph and presentation settings reuses that layout instead of repeating the entire layout search. Changed evidence, labels or layout settings invalidate reuse; saved evidence is never overwritten.

If ELK finishes and Miro fails at **Adding new Miro items** with HTTP 500, layout succeeded and the failure occurred during publication. The error alone does not identify the server's underlying cause or demonstrate a graph-size limit. [Miro supports up to 20 items per bulk request](https://developers.miro.com/reference/create-items), which is the default batch size. Creation errors now include the endpoint, item count, and recognized error codes or request IDs when available, without printing submitted board content or raw server messages.

The export-frame implementation had two request regressions. It added `parent: {"id": null}` to initial shape POSTs; a before/after comparison using the same plan showed this was the only creation-payload difference. That field is now omitted on creation. Miro documents null parents as supported, so the server's internal reason for HTTP 500 remains unverified. The recovery and frame-empty checks also used `limit=1`, below the [documented minimum of 10](https://developers.miro.com/reference/get-items-1). Those checks now request 10 items and validate the complete empty result; they do not mistake a failed or partial response for an empty board.

A POST timeout or server error can leave its outcome uncertain. The publisher preserves the pending request to avoid duplicates. Subsequent sync and preview attempts check this state **before running ELK**. Keep the saved investigation and its Miro mapping; deleting the mapping would discard duplicate-prevention information.

If the error instead occurs during **Updating graph export frames**, use **Recover interrupted frame** in the browser. It reviews existing frames on the populated board, lets you explicitly adopt a matching frame or confirm that this specific frame is absent, and checks the review again before updating the local mapping. The interrupted snapshot is selected so **Create / update Miro frames** can resume. For an older failure that happened inside graph sync, first finish one ordinary **Sync to Miro**, then run the frame action. Recovery itself makes no board changes. See [frame recovery](docs/miro-frame-recovery.md) for details and the `miro-frame-review` / `miro-frame-recover` terminal commands.

For a failed **initial publication**, when no items have been acknowledged and the linked board is empty:

1. Open the linked Miro board after the failed request has finished and inspect it.
2. In the local web UI, choose **Recover empty-board sync**. Check the confirmation box only if you verified that board is empty.
3. Recovery checks both the board items and connectors through Miro. If either contains anything, or the response is incomplete, it leaves the pending batch unchanged. A successful check clears that initial pending batch locally and records your confirmation and the API check in the recovery history. It creates or deletes no board objects.
4. Select the originally failed saved run and choose **Sync to Miro**. Recovery does not start a sync automatically or trace again.

The equivalent terminal command uses the existing SecretSpec/Proton Pass setup:

```bash
liquid-live miro-recover --case cases/YOUR_CASE_DIRECTORY --confirm-empty
```

Empty-board recovery switches that board's saved `shape_batch_size` to `1`, so retry uses individual shape requests instead of the bulk endpoint. This is slower but can avoid a bulk-specific server failure. Other boards keep normal batching. HTTP 500 never automatically triggers this switch or repeats a creation request. An empty API read alone cannot prove an earlier request will never finish later, which is why recovery requires your explicit inspection and confirmation. If a request fails again, its endpoint and safe diagnostic identifiers help distinguish the failure.

When objects exist, or the failed request belongs to an existing publication, use per-item reconciliation. Inspect the board and pending state, then reconcile using the state path printed by the preview or sync report:

```bash
python3 -m liquid_tracer miro-resolve --state PATH_TO_STATE.json --item-id EXISTING_MIRO_ITEM_ID
# Or, only if you have verified that the pending item was not created:
python3 -m liquid_tracer miro-resolve --state PATH_TO_STATE.json --absent
```

When several creations have uncertain outcomes, the error shows their count and a few example keys. The complete logical keys are retained under `pending_creations` in the sync state. Reconcile each one with `--key`, matching it to the correct object on the board:

```bash
python3 -m liquid_tracer miro-resolve --state PATH_TO_STATE.json --key 'LOGICAL_ITEM_KEY' --item-id EXISTING_MIRO_ITEM_ID
# Or, after verifying that this specific item is absent:
python3 -m liquid_tracer miro-resolve --state PATH_TO_STATE.json --key 'LOGICAL_ITEM_KEY' --absent
```

Then repeat `miro-sync` on the same saved run. An accepted bulk request can lose its response, leaving multiple creations to reconcile. Use the actual existing item ID or a verified absence for each key; do not delete state or mark a whole uncertain batch absent without inspecting it.

The older `miro-publish` command remains for resuming version 0.1 snapshot publications; its per-plan mapping is not interchangeable with incremental state. For an older saved run, regenerate an export into a new directory with `export --case CASE --run RUN_ID --out NEW_DIRECTORY`, then select its plan using `miro-sync --case CASE --run RUN_ID --board BOARD --plan NEW_DIRECTORY/miro-plan.json`. This does not adopt objects from an old snapshot; use a fresh board for that migration.

Default circles are **unique full addresses, scoped by network**. Two starting transactions paying the same address connect to one circle. This identity is reused across hops, previews, and Miro continuations. Inputs and outputs keep separate connectors and exact outpoints/indexes; the node retains their individual evidence occurrences. Unknown addresses remain distinct by outpoint, even if their scripts match. Short display labels never determine identity. Sharing a circle does not allocate value, merge UTXOs, infer ownership, or permit tracing between unrelated outputs. Address reuse can create apparent display cycles; tracing and transaction ordering remain UTXO-based. New traces, continuations, and regenerated saved previews use shared addresses. `--merge-addresses` remains a compatible explicit spelling; `--separate-outpoints` on `trace` or `export` requests a legacy occurrence export. Archived runs are never rewritten.


### Convert an existing Miro graph without retracing

Old Miro mappings retain their original address mode. Ordinary sync refuses to silently switch an occurrence mapping. Do not delete the mapping or restart the trace to work around this safeguard.

1. Open **Merge duplicate addresses** in the investigation's terminal menu or browser Miro panel. The offline preview reports the last completed **synced run**, address counts, redundant circles and connectors to redirect. It does not fetch blockchain data, contact Miro, or load credentials.
2. Preserve any comments attached to circles being removed, stop concurrent board editing, and approve the reviewed conversion. It reuses a deterministic existing circle per address, redirects all managed connectors, then removes only verified redundant circles. Connector IDs and UTXO records remain intact. A full paginated connector inventory protects unmanaged attachments; modified redundant circles, grouped shapes, missing objects, pending syncs and stale approvals block unsafe conversion.
3. Choose **Sync to Miro** to refresh labels. Choose **Sync and reorganize Miro graph** when you also approve applying a fresh ELK layout. When finished, choose **Create / update Miro frames**. Compaction previews should be regenerated for the shared-address graph.

The converter saves the original mapping and accessible live item fields under `miro/migrations/` before board writes. PATCH/DELETE attempts are journaled. An interrupted conversion blocks ordinary sync: reopen **Merge duplicate addresses** and approve the same current preview to resume/reconcile it. Do not remove its checkpoint. This is recovery support, not an automatic undo operation. Miro does not provide an atomic cross-item transaction; avoid simultaneous board edits. Comments are not available in the REST item snapshot and cannot be backed up by this conversion.

CLI equivalent (inside the project environment):

```bash
python -m liquid_tracer miro-merge-addresses --case CASE_DIRECTORY --dry-run
liquid-live miro-merge-addresses --case CASE_DIRECTORY --approve-plan APPROVAL_SHA256
```

`APPROVAL_SHA256` is the exact value from the reviewed preview. The live command uses Miro credentials through the normal SecretSpec workflow. Conversion does not call Blockstream, change seeds or service-stop decisions, or rewrite `runs/`. New boards start in shared-address mode and do not need conversion.

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

Tests cover seed precision, hop boundaries, split/merge paths, continuation, budgets, spend-status freshness, confidential fields, event stops, reference validation, labeling, graph identity, evidence checksums, OAuth refresh, board creation and persistence, incremental Miro updates, manual-edit preservation, local ELK placement and route estimates, offline SVG export, retries, and uncertain creation recovery. Automated API checks use synthetic fixtures under `tests/data/` and mock transports. The fixture adapter supports regression tests and previously saved synthetic evidence; new investigations in the interfaces use Live Liquid. Live authentication and publication are verified locally with the operator's credentials.

Source separates the API client, evidence store, tracing engine, export, Miro publication, command parsing, and interactive investigation workflow. The CLI is separate from tracing, so a notebook or case-management interface can call the same engine later.
