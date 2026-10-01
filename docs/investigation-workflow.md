# Collect data, then generate and sync a plot

An investigation owns one set of saved collection runs and can have several Miro
boards. Each board has a name and a plotting goal. Use the same collected data
to make a full investigation chart, starter-connection chart, and peg-out chart.
In the browser, the sequence is **Collect data → Plots & Miro**.
Upload investigation CSVs before collection, then update them as your analysis
develops. Choose the destination workflow before generating each layout.

Create a new investigation with its name, blockchain, and selected starting
outputs. **Liquid Network** is currently the only supported blockchain. The
browser's creation form has no Miro board field or starting-preferences panel;
create or link boards later in **Plots & Miro**.

Opening an existing investigation displays its name with an **Opening** status
in the header while the app loads its saved runs, plots, and board records. Larger
investigations can take longer because saved files are checked before they are
offered for reuse. Opening does not fetch blockchain data or contact Miro.

## 1. Collect data

Select the investigation's seed UTXOs, then collect transaction data with the
hop allowance and budgets you need. Collection retrieves and archives evidence.
It does not publish to Miro. Continuing a run resumes its saved branches and
adds the selected **additional hops** to the previous hop ceiling.

Check the collection status. A request, transaction, output, or time budget can
stop collection before the hop ceiling is reached. Active `stop_tracing` rules
and the confirmation policy still apply. Collection ignores attribution
`hop_limit` values, including `0`; these caps remain available for Full trace
layouts. For a chart covering up to 10 hops, collect
enough data to cover that depth before plotting. Choosing 10 in a plotting form
does not fetch missing transactions.

If an older run stopped branches at attribution hop caps, continue collection
with **0 additional hops** to fill eligible gaps within its current global
ceiling, or add hops to increase that ceiling. Collection still respects
explicit address stops and resource budgets. Then generate new plots from the
new run. Existing saved reports and plots retain their original results.

During collection, the header's progress bar shows **Processing hop N of M**.
The target is the run's cumulative hop limit, including additional hops on a
continuation. Starting outputs are hop 0. Progress reflects the hop currently
being processed, not a time estimate or a guarantee that every branch has
reached that hop. A continuation can revisit earlier unfinished branches.
If collection stops early, the bar keeps the actual processing hop instead of
filling to the target. Address-count lookup and saving use their own stages.

The **Collected data** summary shows **Hops collected** for the selected snapshot,
separately from its configured collection hop limit. This is the deepest recorded
transaction hop, with starting transactions at hop 0. Some branches may stop
earlier, so the number does not indicate complete coverage at every hop. If an
older snapshot has no usable transaction depths, the summary shows **Not recorded**.

The **Collect data** tab contains collection actions. Use the **Plots & Miro**
tab for the next step and the investigation heading to open settings.

### Count hops from a named group

In the collection dialog, enter an existing attribution name in **Count hops
from named group**. Leave it blank to count from the starting outputs as before.
This collection choice is independent of **Center named group**, which controls
the visual layout. The starting transaction/output selections stay the same.
Names match enabled address attributions exactly, ignoring case and surrounding
spaces. Selected seed outputs start at hop 0; reaching the named group resets
the count for that output branch.

| Traced activity | Hop count |
| --- | --- |
| Selected seed output received by Perp | 0 |
| Perp to another Perp address | 0 |
| Perp to Unknown A | 1 |
| Unknown A to Unknown B | 2 |
| Unknown B back to Perp | 0 |
| Perp sends outward again | 1 |

Each output has its own hop count. A transaction with a Perp output and an
outside output can therefore produce hops 0 and 1. The transaction label
summarizes the highest in-range output hop; output details retain each branch's
count. Other inputs are context and do not reset a traced branch merely because
one belongs to the named group.

At the maximum hop, collection checks the immediate spending transaction for
returning outputs. Outputs returning to the group reset to 0; outside outputs
beyond the limit are saved as inspected boundary evidence and are not followed.
The tracer cannot discover a return behind an outside output beyond that
boundary. A maximum of 0 can follow internal group transfers. Transaction,
output, request, and time budgets still bound the work. Stop-tracing rules still
apply. Attribution hop caps do not restrict collection or peg-out tracing.
For Full trace layouts, local attribution allowances still decrease on every
spend, including internal transfers; a named-group reset does not replenish
those local allowances. Starter connections ignores both attribution stops
and caps and searches all verified connections already saved.

The chosen name is saved with each run and shown in collection history. For the
same hop origin, continuation adds the entered hops to the previous ceiling.
When changing the origin, the entered value is the new maximum under that
origin. Continuation recalculates eligible paths using current attribution and
can reuse already collected transactions. Earlier saved runs remain unchanged.
The original distance from the seeds stays in the evidence alongside the
group-relative measurement.

Full and peg-out layouts use the selected collection run's hop origin. Starter
connections reports verified transaction distances without applying a plotting
hop cutoff. Updated attribution can change which saved outputs reset to 0 when
generating another layout. Plotting uses saved data only; run collection again
to retrieve newly eligible activity. Boundary-only inspections do not increase
the **Hops collected** measurement. The progress indicator may return to 0 as
branches re-enter the group, so it remains a depth indicator, not a completion
percentage.

## 2. Generate and sync from saved data

Open **Plots & Miro** and select the saved run, plotting goal, and board
destination. The available goals are shown together:

| Goal | What it plots |
| --- | --- |
| Full investigation / Full trace | Saved activity reachable under the current stop/hop rules, using the current display settings. |
| Starter connections | All verified saved paths between selected starting transactions, without attribution stops, attribution hop caps, or a plotting hop cutoff. Existing labels and confirmation status are preserved. |
| Paths to peg-outs | Transactions on verified paths from selected seed UTXOs to qualifying endpoints within the global hop range, respecting explicit stop rules and ignoring attribution hop caps. Every selected transaction displays all its inputs and outputs. |

Choose **New board** and enter a name for a fresh arrangement, or choose
**Update existing board** and a destination with the same plotting goal.
Set the appearance and, for peg-out paths, the hop range, then choose **Generate & create board** or
**Generate & update board**. Each runs layout generation and Miro publication
as one job. There is no required preview-review or separate sync step.

Both modes use the selected collection run and current attribution, colors,
and change-output rules. Neither fetches transactions or address statistics.
Missing data must be collected separately. Changing a plotting goal or tracing
rule never modifies the archived evidence. A saved preview, SVG, and transaction
CSV are still produced for later inspection and download.

**Generate preview only** remains available for local output or an optional review before
publication. Fresh previews are offline. Update previews read Miro's current
objects, connectors, and positions without writing to the board.

An update preserves retained nodes at their indexed Miro positions. ELK arranges
only new graph nodes in a clear area beyond the board's current contents, and
new connections link that section to existing objects. After syncing, you can
manually merge that section into the investigation's working arrangement.
Full-trace plots also reapply current stop/hop rules: tightening them excludes
branches; loosening them restores only activity already in the saved evidence.

**Starter connections** searches all verified links in the selected saved run.
New plots ignore both `stop_tracing` and attribution `hop_limit` values and have
no maximum-hop field. They preserve attribution names, colors, and confirmation
status, including verified unconfirmed links already saved. A connection must
start through a selected seed output and follow an exact, verified UTXO spend;
shared addresses or matching names do not create connections. Other inputs and
unrelated side branches are not added just to join starters. No blockchain
requests are made. Missing links still require collection, which continues to
respect its explicit stops, global hop ceiling, and resource budgets.

Older saved Starter connections plots keep their recorded stop/hop rules and
bounds. Generate a new layout to use all saved connections; syncing an old
layout does not silently change its scope.

**Paths to peg-outs** uses one circle per full address per network. Repeated
UTXOs retain their separate connectors and transaction CSV rows. Unknown
addresses remain separate by outpoint, and peg-out request diamonds remain
separate even when their Bitcoin destinations match. Only verified saved
spends establish qualifying paths; sharing a circle does not create a new spend.

Under this goal, **Include unspent UTXOs** and **Include unspendable outputs**
add those endpoint types while retaining peg-outs. Both are off by default.
The selected global hop range and active `stop_tracing` rules apply to every
path. Attribution `hop_limit` values, including `0`, are ignored. Unspent
endpoints require a saved unspent observation for the exact UTXO;
unchecked, stopped, or hop-limited outputs do not qualify merely because no
spending transaction was collected. A saved spending input overrides an older
unspent observation. This is the state observed in the selected run, not a live
balance. Unspendable event diamonds remain separate by output. Fee outputs
are excluded from endpoint selection, but displayed as transaction context.

The saved plot records its endpoint choices and counts by type, so the ELK SVG
and Miro sync use the same selection. These choices belong to the generated
layout, like its hop range. To change them, generate another layout and select
it for the managed board. Plotting does not fetch fresh spend observations.

Every transaction selected for a new peg-out plot automatically shows **all
inputs and outputs**, including fees and outputs on branches that are not
followed. There is no context-display toggle. The endpoint and hop filters decide
which transactions belong to the plot, not which objects of an included
transaction are visible. An excluded branch retains its initial output/address
without extending that branch or treating it as an endpoint match.

Context uses thinner arrows and is marked `CONTEXT` in the transaction CSV.
Context does not create traced links, change hop counts or endpoint matches,
or cause earlier/later transactions to be added. A shared address keeps one
circle, with each traced or context UTXO retaining its own arrow. SVG export
and Miro sync use the same saved graph. Older saved layouts retain their
original context choices; regenerate a layout to show complete transactions.

### CSVs for paths and endpoints

**Paths to peg-outs** offers two CSV downloads under **Saved plot layouts**
and **History & downloads → Plot downloads**. Earlier standalone peg-out
previews also offer both downloads.

| Download | Rows included |
| --- | --- |
| **All trace transactions CSV** (`transactions.csv`) | Every input and output of the transactions displayed in the peg-out trace, including context. Older saved layouts retain their original scope. |
| **Endpoints only CSV** (`endpoints.csv`) | One row per matched ending output, including its qualifying source seeds. Intermediate transactions are excluded. |

The endpoint table starts with the example-style columns **Source**, **Source
Value**, **Deposit/Peg-out Tx**, **Address/Peg-out Address**, **Receiving Entity**,
**Status**, and **Pegout LBTC**. Further columns retain exact output indices,
source outpoints, hop counts, asset values, and observation provenance.

**Hops from Seed** counts transaction steps from the seed transaction (hop 0)
to the endpoint transaction. It does not reset at a named address group. When
multiple seeds or paths reach an endpoint, it reports the shortest qualifying
distance. **Source Seed Hops** preserves the shortest qualifying distance for
each source seed outpoint as JSON. Both follow the saved trace's selected paths
and rules; context inputs and excluded shortcuts do not supply distances.
The separate **Hop Counts** and **Hop Reference** columns retain the plot's
chosen counting basis, which may be a named group.

`Source Value` describes a selected source UTXO when its value and asset are
public. It does not infer an external Bitcoin deposit total or allocate value
from that source to the endpoint. Multiple source values are mapped to their
exact seed outpoints within the cell. Unknown or confidential values stay blank.
`Pegout LBTC` is populated only for peg-out requests with a known L-BTC amount.

Statuses are `Pegout`, `OP_Return`, `Unspendable`, and `Dormant`.
`Dormant` means unspent at the archived observation time, not a live balance.
Stopped, unchecked, and hop-limited branches are not classified as dormant.
A service deposit is not inferred from a label or a tracing stop. To include
unspent and unspendable endpoints, select those options when creating the trace.

Endpoint tables are generated on download from the verified saved trace and
its original attribution rules. Existing layouts get the second download
without recollecting data, rerunning ELK, or syncing Miro. Later attribution
changes do not rewrite a historical trace's endpoint table. Create a new plot
to use updated rules. A layout that is no longer eligible to sync can still
provide its CSVs when its saved snapshot and source archive remain intact.

**Full-run CSV downloads** remains the broader collection view. The earlier
`path-transactions.csv` and `trace-endpoints.csv` files remain in archives that
contain them; the peg-out interface presents only the two downloads above.

Set layout attempts, connector appearance, attribution arrow coloring, and
named-group centering here. **Full trace** also supports **Separate branch hubs**,
**Group isolated context inputs**, and **Include transaction fee flows**.
**Group isolated context inputs** is also available for **Paths to peg-outs**.
It combines at least two eligible external input addresses used only by one
transaction into a context summary.
Traced, shared, attributed, and otherwise protected addresses remain separate,
as do sibling outputs. Every input retains its exact UTXO connector and CSV row;
the full member addresses stay available in local details. Endpoint matches,
hop limits, and trace evidence are unchanged.

Grouping is saved as an investigation layout preference and captured with each
generated layout. Choosing Starter connections preserves the preference without
applying it. Separate branch hubs and the optional fee-flow toggle remain
Full trace-only; new peg-out layouts always display their transaction fees.
**Generate & update board** applies grouping changes. Replaced
context objects join the newly arranged additions.

**Save layout settings** persists the preferences with this investigation, even
before collection. **Generate** saves pending layout edits before generating the
plot. Unsaved edits survive tab navigation within the browser session; saved
settings survive closing the browser or restarting the server. A failed save
keeps the edits and does not start plotting.

Every plot records its source run, goal, and coverage. Inspect its saved preview
and downloads as needed. A plot with no matching paths does not prove that no connection or
peg-out exists outside the saved coverage. A peg-out request does not establish
which Bitcoin transaction paid it.

A fresh plot with no matching activity cannot create a board. An empty update
can remove a managed projection excluded by the revised rules, subject to the
same ownership and manual-content checks as other removals. It does not clear
unrelated board content.

Each newly generated layout captures its display settings. Changing preferences
later leaves that saved layout available for download and sync with its original
appearance. Changed evidence or attribution inputs require regeneration. Older
plots without captured settings may need to be regenerated once.

## Saved plots and board management

The generated plot remains available under saved layouts with **Download ELK
SVG**, its local preview, and supporting files. Saved-layout publication controls
remain available for preview-only plots and recovery; using them does not run
ELK again.

Miro publication uses the saved graph and layout. **Create & sync** publishes a
fresh layout to a new private board. **Update board** applies the preview bound
to that existing board. Miro chooses connector routes, so exact bends can differ
from the ELK SVG.

### Manage boards

The **Plots & Miro** workspace keeps a separate block for every created or linked
board, including historical boards. Each block has its own name, goal, status,
Miro link, and saved layout information. Managed boards also have their own
compatible layout picker and sync controls.

1. Choose **New board**, its name, tracing goal, and settings, then **Generate &
   create board**. The layout is saved before the board is created and synced.
2. To maintain a board, select **Update existing board** and that target, then
   **Generate & update board**. Existing positions stay intact; additions arrive
   in the separate ELK-arranged area and connect to retained objects.
3. If publication is interrupted, use the board's saved-layout resume control.
   It reuses that layout and board. Generating a different fresh layout creates
   a separate board, even if its name matches.
4. A provided board can first be linked here, then selected as an update target.

New outputs from the same forward transaction stage share a vertical column in
the staged update area. This includes new outputs whose transaction is already
on the board. Successive stages advance left to right, with space reserved for
connector captions. Dependency order takes precedence over collection hop
numbers when starting transactions overlap. An address reused across several
stages remains one object at its join, and separate branch hubs and fee rows
keep their special placement. Existing board positions remain unchanged.

Changing one board's layout selection does not change another board's selection,
including boards with the same goal. Draft choices survive tab navigation;
reopening the application restores each board's saved plot binding. Interrupted
syncs stay bound to their recorded plot until recovered. A background operation
temporarily disables other write actions in that investigation, while all board
blocks remain visible. Open another investigation to run independent work in the
same server. The header task list shows progress and outcomes across investigations
and browser tabs; finishing background work does not switch your current view.

Each board keeps its own saved item mapping. The update is checked against a new
board inventory before writes. If you move or edit board content after preparing
the layout, regenerate it; the program will not apply stale positions. Its own
acknowledged partial-sync changes are tracked for safe retries. Obsolete managed
items are removed when the new tracing rules exclude them, including nodes
previously moved by hand. Manual text/style changes or unrelated attachments
can block removal so that work can be preserved. Unrelated objects stay intact.
Older plots retain their legacy sync controls separately.

If text or style differences block context regrouping or removal of an excluded
object, the failure details identify the affected Miro objects. **Open object in
Miro** takes you to each item; the report lists the changed fields and their
last-synced and current values. Text formatting is shown explicitly, so a small
formatting change is not hidden. The terminal prints the same diagnostic details.
The report does not revert anything. For an accidental edit, restore the listed
saved value in Miro, then generate the board update again to capture that change.
Long values use labeled excerpts around the difference; use them to locate the
change, not to replace an object's complete text.
If the edit matters, preserve it before replacing the generated object.
Miro may omit empty captions and their unused font size from connector responses.
Those omissions do not count as edits when the saved connector also has no
caption. Added or removed nonempty captions and explicit style changes are still
protected. Previously saved layouts can be resumed with this comparison fix.

Original full-trace boards from before the board registry can still receive
additions and content updates. If a revised layout would remove their older
objects, the program stops because those mappings lack the creation records
needed for safe retirement. Use a **New board** layout and **Create & sync**
for that revised graph; the original board remains available.

Older peg-out boards used separate address circles for each UTXO. To adopt
shared address circles, generate a new **Paths to peg-outs** layout and create
or link a different board for its first sync. The old board can still sync its
compatible saved layouts. Later layouts using shared addresses can update the
new board normally; this transition does not require collecting data again.

Board creation and sync use the existing SecretSpec/Proton Pass credentials.
Linking a board records its destination locally without contacting Miro. If an
action stops, inspect the saved board status before retrying; uncertain writes
must be reconciled through the existing recovery flow.

## Settings, history, and downloads

The browser has three task tabs: **Collect data**, **Plots & Miro**, and
**History & downloads**. The last tab combines saved-run history with SVGs,
tables, reports, and other saved files.

Use **Investigation settings → Investigation data** to import CSVs, review
addresses, manage change outputs, and export saved input CSVs. Colors are also
in Investigation settings. Imports retain their **Preview → Apply** flow, and
color edits save through their own controls. **Save settings** saves the
investigation preferences.

Both **Investigation settings** and **Workspace defaults** persist across
sessions. Investigation settings apply to the current case. Workspace defaults
set collection limits, all seven plot layout preferences, and the Miro sync
budget copied into future cases, without changing existing investigations.
Edit a current investigation's layout preferences in **Plots & Miro**;
Workspace defaults supplies their starting values only.

## Existing investigations and older searches

The existing full-investigation board appears in the board list. The previous
full-graph tools, rebuild controls, and recovery actions remain available.

Older standalone peg-out searches retain their saved search scope, resume
controls, and evidence. Older connection and peg-out publications were immutable
snapshots and remain available to open. Create a managed board for that goal to
use the new repeatable plot-and-sync workflow. Existing boards and search archives
are not silently replaced.

In the browser, older standalone search controls are under **History & downloads**. In the
terminal, they remain explicitly labeled below the primary collection, plotting,
and board controls. Use the main **Plots & Miro** workflow for a new view of collected
investigation data.

## CLI

Collection keeps its existing command and limits:

```sh
liquid-live trace --case CASE_DIRECTORY --resume latest --additional-hops 3
```

Generate and publish a new board, or generate and apply an existing-board update:

```sh
liquid-live plot-sync --case CASE_DIRECTORY --goal pegouts --run latest --max-hops 10 --name "Peg-out paths" --max-items 750
liquid-live plot-sync --case CASE_DIRECTORY --goal full --run latest --layout-mode update --board-record-id BOARD_RECORD_ID --max-items 750
```

Generate previews without publication when needed:

```sh
liquid-trace plot --case CASE_DIRECTORY --goal full --run latest --open
liquid-trace plot --case CASE_DIRECTORY --goal connections --run latest --open
liquid-trace plot --case CASE_DIRECTORY --goal pegouts --run latest --min-hops 0 --max-hops 10 --open
```

Publish or resume a saved fresh layout, or link a provided board:

```sh
liquid-live investigation-board-create-sync --case CASE_DIRECTORY --preview PREVIEW_ID --name "Peg-out paths" --max-items 750
liquid-trace investigation-board-link --case CASE_DIRECTORY --goal connections --name "Starter connections" --board BOARD_ID
liquid-trace investigation-boards --case CASE_DIRECTORY
```

To review an update separately, prepare it without publication, then apply its
returned preview ID. The sync command also resumes that saved update after an
interruption:

```sh
liquid-live plot --case CASE_DIRECTORY --goal full --run latest --layout-mode update --board-record-id BOARD_RECORD_ID --open
liquid-live investigation-board-sync --case CASE_DIRECTORY --record BOARD_RECORD_ID --preview PREVIEW_ID --max-items 750
```
