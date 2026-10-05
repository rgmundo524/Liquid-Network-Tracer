# Collect data, preview a plot, then write it to Miro

An investigation owns one set of saved collection runs and can have several Miro
boards. Each board has a name and a plotting goal. Use the same collected data
to make a full investigation chart, starter-connection chart, and peg-out chart.
Investigations can also use a workspace **Shared collection** while keeping
their own seeds, investigation data, layouts, and boards.
In the browser, the sequence is **Collect data → Plots & Miro**.
Upload investigation CSVs before collection, then update them as your analysis
develops. Choose the destination workflow before generating each layout.

Create a new investigation with its name, blockchain, and selected starting
outputs. **Liquid Network** is currently the only supported blockchain. The
browser's creation form has no Miro board field or starting-preferences panel;
create or link boards later in **Plots & Miro**.

Opening an existing investigation loads its name, settings, starting outputs,
and selected-run summary first. Collection information, shared snapshots, saved
plots, board status, and history load independently as their tabs need them.
A section that is still loading does not block navigation or settings.

New collection runs save small display summaries when they finish. Older runs
build these summaries once in the background, using bounded memory; the cache
survives service restarts. Only the selected run is scheduled when opening an
investigation. Selecting another snapshot or opening History requests its older
summaries. While a summary is being prepared, its counts show **Loading**, not
zero or an empty investigation. Source changes invalidate cached summaries.

The **Saved previews** library reads small reports in bounded pages. Browsing or
selecting a preview does not load its graph or perform evidence verification.
**Open preview** opens it in another tab; **Prepare for Miro** checks the selected
preview before enabling publication. Previews, downloads, and Miro actions retain
their full evidence validation; display summaries never authorize publication.
Opening an investigation does not fetch blockchain data or contact Miro. Opening
or preparing a large preview can still take time without blocking other
investigation tabs.

### Delete an investigation

Open its **Investigation workspace** and choose **Delete investigation** beside
**Investigation settings**. Review the name and ID, then type the name exactly
and choose **Permanently delete investigation**. Cancel leaves it unchanged.
Deletion appears in Tasks and closes that investigation’s saved browser tab when
complete. Wait for its active tasks and shared collection tasks to finish first.

Deletion permanently removes that investigation’s local collection runs,
previews, exports, annotations, and settings. Download any exports
you need beforehand. Shared collection data, other investigations, and remote
Miro boards are retained; deleting a Miro board is a separate action. Deletion
cannot be undone or canceled after it starts. If file cleanup is incomplete,
the task reports it while the investigation stays removed from the workspace.
A small deletion receipt is retained in the workspace's `.deleted-investigations`
directory. If cleanup is interrupted, the remaining files stay there and the
launching terminal identifies their location.

### Compare analysis scopes and saved previews

Use **Analysis** to compare hop cutoffs against saved evidence, inspect recorded
endpoints and open branches, and download the supporting records. **Use this
scope for plots** transfers the selected snapshot and hop limit into the plotting
form; it does not generate a graph automatically.

In **Plots & Miro**, generate a **Full trace** preview with the chosen maximum
analysis hops and **Original starting transactions** as its hop basis. The limit
applies to that preview while the deeper saved collection remains available.
Compare saved previews and download a selected SVG for a report, or write it to
Miro. Keep the larger preview as a reference for activity outside the smaller
figure. A display boundary does not mean a branch is unspent or has terminated.

## 1. Collect data

Select the investigation's seed UTXOs, then collect transaction data with the
hop allowance you need. Collection retrieves and archives evidence.
It does not publish to Miro. Continuing a run resumes its saved branches and
adds the selected **additional hops** to the previous hop ceiling.

Collection has no request, transaction, output, or total-time cap by default.
Miro actions likewise have no application item-count cap. In **Investigation
settings**, **Use optional run budgets** activates the saved optional caps; a
value of **0 means unlimited** for that budget. The values remain saved when
the control is off. Existing investigations without this setting also default
to unlimited, even when they retain older finite values. Workspace defaults
sets the starting preference for future investigations.

Check the collection status. An explicitly enabled budget can stop collection
before the hop ceiling is reached. Active `stop_tracing` rules
and the confirmation policy still apply. Collection ignores attribution
`hop_limit` values, including `0`; these caps remain available for Full trace
layouts. For a chart covering up to 10 hops, collect
enough data to cover that depth before plotting. Choosing 10 in a plotting form
does not fetch missing transactions.

If an older run stopped branches at attribution hop caps, continue collection
with **0 additional hops** to fill eligible gaps within its current global
ceiling, or add hops to increase that ceiling. Collection still respects
explicit address stops and any enabled resource budgets. Then generate new
plots from the new run. Existing saved analyses and plots retain their original results.

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

### Collect once for several investigations

Open the investigations whose seed outputs should be included, then start
**Shared collection** from **Collect data**. The job captures the union of their
configured seeds and the focused investigation's collection policy at launch.
Review that policy, especially stop-tracing rules and the hop allowance. A
different investigation's attribution CSV does not silently change the shared
collector's policy. Opening or closing tabs after launch does not change the run.

Continue the latest shared snapshot to extend its existing seeds and branches.
To include a new seed set, start a fresh shared collection from the desired open
investigations. Private collection runs remain available and are not imported
into or replaced by the shared dataset.

In **Plots & Miro**, choose **Data source → Shared collection** and a saved
**Shared snapshot**. The current investigation's own seeds and rules determine
its plot. A case with all ten seeds produces a combined chart; a case with one
of those seeds produces its individual chart from the same saved evidence.
Neither plot triggers another collection. See [shared collection](shared-collection.md)
for coverage limits, source compatibility, and provenance.

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
output, request, and time caps apply only when resource budgets are enabled.
Stop-tracing rules still apply. Attribution hop caps do not restrict collection or peg-out tracing.
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

## 2. Preview and publish from saved data

Open **Plots & Miro** and select the saved run, plotting goal, and board
destination. The available goals are shown together:

| Goal | What it plots |
| --- | --- |
| Full investigation / Full trace | Saved activity reachable under the current stop/hop rules, using the current display settings. |
| Starter connections | Transactions on verified saved paths between selected starting transactions, optionally limited by maximum transaction hops. Attribution stops and hop caps are ignored. Every selected transaction displays all its inputs and outputs. Existing labels and confirmation status are preserved. |
| Paths to peg-outs | Transactions on verified paths from selected seed UTXOs to qualifying endpoints within the global hop range, respecting explicit stop rules and ignoring attribution hop caps. Every selected transaction displays all its inputs and outputs. |

Choose **New board** and enter a name for a fresh arrangement, or choose
**Update existing board** and a destination with the same plotting goal.
Set the appearance and, for peg-out paths, the hop range, then choose **Generate
preview**. The result appears in **Saved previews**. Use **Open preview** to
inspect it in another tab. Select the saved version you want, choose **Prepare
for Miro**, then **Write to Miro** to create its first private board or sync its
captured existing-board destination. Publication uses that saved layout without
running ELK again. After first publication, the destination switches to that
board for future update previews.

The optional **Advanced: generate and publish in one step** section retains the
combined generation/publication actions. Those actions generate a new layout;
use **Write to Miro** to publish the selected saved preview.

Both modes use the selected collection run and current attribution, colors,
and change-output rules. Neither fetches transactions or address statistics.
Missing data must be collected separately. Changing a plotting goal or tracing
rule never modifies the archived evidence. A saved preview, SVG, and transaction
CSV are still produced for later inspection and download.

**Generate preview** also works for local-only output. Fresh previews are offline.
Update previews read Miro's current objects, connectors, and positions without
writing to the board. Changing the generation form does not change an existing
preview; generate another preview to capture those changes.

An update preserves retained nodes at their indexed Miro positions. ELK arranges
only new graph nodes in a clear area beyond the board's current contents, and
new connections link that section to existing objects. After syncing, you can
manually merge that section into the investigation's working arrangement.
Full-trace plots also reapply current stop/hop rules: tightening them excludes
branches; loosening them restores only activity already in the saved evidence.

**Starter connections** keeps every selected starting transaction visible and
searches for connections between every pair. Choose **Shortest connections**
to show one minimum-hop route for each pair connected by verified saved spends.
Equal-length routes use a stable choice. A starter with no qualifying path stays
visible with its local inputs and outputs; its other branches are not expanded.
Zero connected pairs therefore still produces a chart of the selected starters.
Older previews retain their original membership until regenerated.

The search uses verified links in the selected saved run.
New plots ignore both `stop_tracing` and attribution `hop_limit` values. Choose
**Within hop limit** and set **Maximum connection hops**, then increase the
limit and generate another preview or board update to reveal longer routes.
The limit counts ordinary transaction steps from each selected starter to
another, without resetting at named groups or intermediate starters. Choose
**All saved connections** to remove this plotting cutoff. They preserve
attribution names, colors, and confirmation
status, including verified unconfirmed links already saved. A connection must
start through a selected seed output and follow an exact, verified UTXO spend;
shared addresses or matching names do not create connections. Every selected
transaction displays all its inputs and non-fee outputs, including context inputs
and the heads of unrelated side branches. Fee outputs are included only when
**Include transaction fee flows** is enabled. These additional objects do not create
qualifying connections or expand those branches further. The transaction CSV
contains the same complete input/output accounting. No blockchain
requests are made. Missing links still require collection, which continues to
respect its explicit stops, global hop ceiling, and any enabled resource budgets.

Older saved Starter connections plots keep their recorded stop/hop rules,
bounds, and input/output selection. Generate a new layout to use complete
transactions on all saved connections; syncing an old layout does not silently
change its scope.

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
are excluded from endpoint selection. They appear as transaction context only
when **Include transaction fee flows** is enabled.

The saved plot records its endpoint choices and counts by type, so the ELK SVG
and Miro sync use the same selection. These choices belong to the generated
layout, like its hop range. To change them, generate another layout and select
it for the managed board. Plotting does not fetch fresh spend observations.

### Stop at a cumulative peg-out amount

Under **Paths to peg-outs → Peg-out selection**, the default **All matching
peg-outs** searches the selected hop range as before. Choose **Stop at cumulative
L-BTC amount** and enter a positive **Cumulative L-BTC target** to stop after a
specified total of disclosed L-BTC peg-out output values. Decimal amounts support
up to eight places; zero does not mean unlimited here. Return to **All matching
peg-outs** to remove the amount limit.

Selection follows verified paths from the selected seed outputs. It visits
endpoints nearest ordinary transaction distance from any selected seed first,
then orders equal-distance endpoints by transaction ID and numeric output index.
An output counts only once even when several seeds or paths reach it. The selected
minimum and maximum hops, named-group hop rules, confirmation policy, and explicit
stop-tracing rules still determine which endpoints qualify. Named-group resets
do not change the ordinary seed-distance ordering used for the amount limit.

The entire endpoint that reaches or crosses the target is selected; it is never
split or partially valued. The result records the target, counted total, excess,
stopping output, and ordinary stopping distance. For example, endpoints of 40
and 35 L-BTC meet a 60 L-BTC target with 75 L-BTC counted and 15 L-BTC excess.
The traversal completes the predecessor relationships at that distance so joins
retain their qualifying paths, then stops without expanding a later frontier.
Later paths to an already selected endpoint are not added after the cutoff.
If the target is not reached, the result says the qualifying saved paths were
exhausted; this is limited by the current evidence, filters, and stopping rules.

Only peg-outs with a known L-BTC asset and an explicit integer value contribute
to the total. Encountered peg-outs with unknown amounts or unknown assets remain
selected before the cutoff, but their values are not treated as zero or inferred;
the report counts them separately. Non-LBTC outputs and transaction fees do not
contribute. Optional unspent and unspendable endpoints can still be selected
before the cutoff, but they also do not increase the peg-out total.

The **Endpoints only CSV** contains the same selected endings as the plot's
endpoint report. Seed provenance is calculated within that single global
selection and cutoff, without giving each seed a separate amount allowance.
The path and endpoint tables append **Pegout L-BTC Limit**, **Selected Pegout
L-BTC**, **Pegout Limit Excess L-BTC**, and **Pegout Limit Stop Reason**. These
are whole-selection values repeated for provenance on each row; do not sum
these columns across rows. They are blank for layouts without an amount limit.
The complete transaction view and **All trace transactions CSV** can include
other outputs of selected transactions, including additional peg-outs after the
amount cutoff. Those context outputs are not counted in the selected total.

The target sums full disclosed endpoint values. It does not allocate value from
the starting outputs, establish ownership, or confirm a Bitcoin payout. It can
reduce path exploration and the resulting layout, while source checksums and
saved-evidence validation still run. No additional blockchain data is fetched,
and the amount setting does not alter collection or any existing archive. The
query and result are captured in the saved layout; generate a new preview to
change the target.

For a local preview using an existing collection:

```sh
liquid-trace plot --case CASE_DIRECTORY --goal pegouts --run latest --min-hops 0 --max-hops 10 --pegout-lbtc-limit 60 --open
```

`plot-sync --goal pegouts` accepts the same `--pegout-lbtc-limit` option. Omit the
flag to select all matching peg-outs within the hop range.

Every transaction selected for a new peg-out plot automatically shows its
**inputs and outputs**, including outputs on branches that are not followed.
**Include transaction fee flows** controls fee visibility; hiding fees also omits
their rows from the matching transaction CSV, while the source evidence retains
them. There is no context-display toggle. The endpoint and hop filters decide
which transactions belong to the plot. An excluded branch retains its initial
output/address without extending that branch or treating it as an endpoint match.

Context uses thinner arrows and is marked `CONTEXT` in the transaction CSV.
Context does not create traced links, change hop counts or endpoint matches,
or cause earlier/later transactions to be added. A shared address keeps one
circle, with traced UTXOs retaining their own arrows. Eligible isolated context
inputs can share a counted connector when grouping is enabled. SVG export
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
**Group isolated context inputs**, and **Include transaction fee flows**. The fee
setting applies to all three plot types and is off by default.
**Group isolated context inputs** is also available for **Paths to peg-outs**
and **Starter connections**.
It combines at least two eligible external input addresses used only by one
transaction into a context summary.
Traced, shared, attributed, and otherwise protected addresses remain separate,
as do sibling outputs. New layouts use one counted connector from each context
summary to its transaction, reducing the ports and routes that ELK must arrange.
It also bundles multiple context inputs from the same visible address to the same
transaction into one counted arrow. The address remains visible, including its
name and hub designation. Traced inputs and exact displayed UTXO continuations
keep separate arrows, even when they share that address. A bundle's original
inputs are listed in `details.html`; no total amount is inferred.
Every original input retains its exact UTXO record in `graph.json` and its own
CSV row; full member addresses and inputs stay available in local details.
Seed, traced, candidate, and endpoint connections remain separate. Endpoint
matches, hop limits, and trace evidence are unchanged. Summary connectors do
not imply common ownership or a known total for confidential inputs. Older
saved previews keep their original connectors; generate a new preview to use
the reduced display.

Grouping is saved as an investigation layout preference and captured with each
generated layout. Separate branch hubs apply to Full trace and Paths to peg-outs.
The optional fee-flow toggle applies to Full trace, Paths to peg-outs, and Starter
connections. Fees remain in saved evidence even when hidden from the graph and
its transaction CSV. Existing saved layouts keep their captured
fee choice; generate another layout to apply a different choice.
Generate an update preview, then use **Write to Miro** to apply grouping changes. Replaced
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

Every generated plot remains in the **Saved previews** library under **Plots &
Miro**, with its local preview, **Download ELK SVG**, and supporting files. Files
are stored under `cases/<investigation>/previews/<preview-id>/`; reopening an
investigation does not regenerate or remove them. The library shows scope,
source, destination, layout style, creation date, and counts. Load older entries
to find earlier versions. Your selection is remembered in this browser, even
when the selected preview is older than the first page.

The library labels plots **Preview 1**, **Preview 2**, and so on within each
investigation. These numbers stay attached to their original preview IDs across
sorting, pagination, and service restarts. On first use, existing completed
previews receive numbers in report-file timestamp order; later completed plots
receive increasing numbers. Deleted numbers are not reused. A separate numbering
record preserves these labels without changing the checksummed preview files.

**Open preview** opens a separate tab. Large previews use the section browser
described below; opening its pages verifies their saved checksums without
rereading the complete graph. Publishing still validates the complete evidence. The investigation
page does not embed the graph, which avoids automatically loading huge SVGs.
Choose **Select for Miro**, then **Prepare for Miro** to verify the desired
version. Its publication controls use that exact saved preview, including its
captured board destination. Changing the generation form does not change what
will be published. Existing recovery controls remain available; publishing a
saved preview does not run ELK again.

Miro publication uses the saved graph and layout. **Write to Miro** publishes a
fresh layout to its private board or applies the preview bound to an existing
board. An already synced preview offers **Open synced Miro board**. Interrupted
operations keep their saved-board recovery controls. Miro chooses connector routes, so exact bends can differ
from the ELK SVG.

### Manage boards

The **Plots & Miro** workspace keeps a separate block for every created or linked
board, including historical boards. Each block has its own name, goal, status,
Miro link, and saved layout information. Managed boards also have their own
compatible layout picker and sync controls.

1. Choose **New board**, its name, tracing goal, and settings, then **Generate
   preview**. Review it and choose **Write to Miro** to create and populate the board.
2. To maintain a board, select **Update existing board** and that target, generate
   a preview, then **Write to Miro**. Existing positions stay intact; additions
   arrive in the separate ELK-arranged area and connect to retained objects.
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

Collection uses the selected hop allowance with unlimited resource budgets by default:

```sh
liquid-live trace --case CASE_DIRECTORY --resume latest --additional-hops 3
```

Generate and publish a new board, or generate and apply an existing-board update:

```sh
liquid-live plot-sync --case CASE_DIRECTORY --goal pegouts --run latest --max-hops 10 --name "Peg-out paths"
liquid-live plot-sync --case CASE_DIRECTORY --goal full --run latest --layout-mode update --board-record-id BOARD_RECORD_ID
```

Add a positive `--max-transactions`, `--max-outpoints`, `--max-requests`,
`--max-seconds`, or `--max-items` to explicitly cap the corresponding CLI
action. These flags use `0` for unlimited. API rate pacing, per-request
timeouts, retries, cancellation, and layout resource coordination remain active.

Large Trace previews preserve usable local section geometry, pack section envelopes
near their connected splits and joins, and route connections between sections
through outer corridors. The backbone stays aligned, terminal objects remain
distinct, and a shared address is still one object. Unsafe local arrangements use
a bounded fallback. Sections whose joins cannot preserve forward transaction
order can be split without another ELK calculation; existing-board alignment
uses conservative placement. This does not prune paths, change amounts, or infer
ownership. Section membership and fallback counts appear under
`layout.section_geometry` in the layout report.

Routing cleanup checks unnecessary detours and sideways steps against nearby
shapes, routes, and captions. Large graphs use bounded regional checks, so the
whole cleanup pass is no longer skipped just because the total obstacle index is
too large. A dense region or exhausted budget leaves its route unchanged. Inspect
`layout.section_geometry.route_cleanup` for accepted shortcuts, skipped regions,
and verification limits. Genuine return paths remain; not all bends or crossings
can be removed.

At 2,000 display objects or 5,000 display connections, a new saved preview opens
**Trace section overview** instead of putting the entire drawing into the browser.
Open a numbered section and follow its connection links into neighboring sections.
Each drawing has at most 128 objects; exact input/output records are paginated at
200 per page, including individual inputs behind bundled connectors. Sections
are navigation aids, not ownership labels. The complete SVG and graph data remain
downloadable, and **Write to Miro** still uses the complete preview. The section
pages live in the saved preview directory and are checksummed with its other
artifacts. Opening a section verifies only that page and the navigation index;
publication still verifies the entire saved evidence. Small previews and the
separate before/after compaction comparison keep their existing views.

Generate a fresh Trace preview after restarting the service to use the new
packing and routing. Existing previews remain available; older Trace geometry is not reused
for a newly requested layout. Updating an existing Miro board still preserves
its existing object positions. Use the explicit reorganization workflow if the
existing board itself needs a new arrangement.

The downloaded `layout-report.json` contains `layout.compactness` measurements for the
final layout and candidate measurements under `layout.search.compactness_candidates`.
Area includes nodes, routed lines, and captions. Connector detour is excess
polyline length over the straight distance between its attached endpoints.
Footprint is a tie-break after safety and path-order checks, not a reason to
accept overlapping objects or ambiguous shared connector segments.

Generate previews without publication when needed:

```sh
liquid-trace plot --case CASE_DIRECTORY --goal full --run latest --open
liquid-trace plot --case CASE_DIRECTORY --goal connections --run latest --open
liquid-trace plot --case CASE_DIRECTORY --goal pegouts --run latest --min-hops 0 --max-hops 10 --open
```

Publish or resume a saved fresh layout, or link a provided board:

```sh
liquid-live investigation-board-create-sync --case CASE_DIRECTORY --preview PREVIEW_ID --name "Peg-out paths"
liquid-trace investigation-board-link --case CASE_DIRECTORY --goal connections --name "Starter connections" --board BOARD_ID
liquid-trace investigation-boards --case CASE_DIRECTORY
```

To review an update separately, prepare it without publication, then apply its
returned preview ID. The sync command also resumes that saved update after an
interruption:

```sh
liquid-live plot --case CASE_DIRECTORY --goal full --run latest --layout-mode update --board-record-id BOARD_RECORD_ID --open
liquid-live investigation-board-sync --case CASE_DIRECTORY --record BOARD_RECORD_ID --preview PREVIEW_ID
```
