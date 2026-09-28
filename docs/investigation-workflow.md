# Collect data, create plot layouts, download or sync

An investigation owns one set of saved collection runs and can have several Miro
boards. Each board has a name and a plotting goal. Use the same collected data
to make a full investigation chart, starter-connection chart, and peg-out chart.
In the browser, the sequence is **Collect data → Plot Layouts → Miro boards**.
Upload investigation CSVs before collection, then update them as your analysis
develops. Choose the destination workflow before generating each layout.

Create a new investigation with its name, blockchain, and selected starting
outputs. **Liquid Network** is currently the only supported blockchain. The
browser's creation form has no Miro board field or starting-preferences panel;
create or link boards later in **Miro boards**.

## 1. Collect data

Select the investigation's seed UTXOs, then collect transaction data with the
hop allowance and budgets you need. Collection retrieves and archives evidence.
It does not publish to Miro. Continuing a run resumes its saved branches and
adds the selected **additional hops** to the previous hop ceiling.

Check the collection status. A request, transaction, output, or time budget can
stop collection before the hop ceiling is reached. Service stop rules,
attribution hop limits, and the confirmation policy still apply. For a chart
covering up to 10 hops, collect enough data to cover that depth before plotting.
Choosing 10 in a plotting form does not fetch missing transactions.

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

The **Collect data** tab contains collection actions. Use the **Plot Layouts**
tab for the next step and the investigation heading to open settings.

## 2. Create Plot Layouts from saved data

In the browser, open **Plot Layouts** and select the saved run. In the terminal,
choose **Choose plotting goal** under **Plot saved data**. The available goals
are shown together:

| Goal | What it plots |
| --- | --- |
| Full investigation / Full trace | Saved activity reachable under the current stop/hop rules, using the current display settings. |
| Starter connections | Verified paths between the starting transactions, within the selected maximum hops. |
| Paths to peg-outs | Verified paths from selected seed UTXOs to peg-out requests, optionally also unspent UTXOs and unspendable outputs, within an inclusive hop range. |

Choose **New board** for a fresh full arrangement, or **Update existing board**
and a destination with the same plotting goal. Both modes use the selected
collection run and current attribution, colors, and change-output rules.
New-board plotting is offline. Update plotting also reads Miro's current items,
connectors and positions; it does not write to the board. Neither mode fetches
transactions or address statistics. Missing data must be collected separately.
Changing a plotting goal or tracing rule never modifies the archived evidence.

An update preserves retained nodes at their indexed Miro positions. ELK arranges
only new graph nodes in a clear area beyond the board's current contents, and
new connections link that section to existing objects. After syncing, you can
manually merge that section into the investigation's working arrangement.
Full-trace plots also reapply current stop/hop rules: tightening them excludes
branches; loosening them restores only activity already in the saved evidence.

**Paths to peg-outs** uses one circle per full address per network. Repeated
UTXOs retain their separate connectors and transaction CSV rows. Unknown
addresses remain separate by outpoint, and peg-out request diamonds remain
separate even when their Bitcoin destinations match. Only verified saved
spends establish qualifying paths; sharing a circle does not create a new spend.

Under this goal, **Include unspent UTXOs** and **Include unspendable outputs**
add those endpoint types while retaining peg-outs. Both are off by default.
The selected hop range and current attribution stop/hop limits apply to every
path. Unspent endpoints require a saved unspent observation for the exact UTXO;
unchecked, stopped, or hop-limited outputs do not qualify merely because no
spending transaction was collected. A saved spending input overrides an older
unspent observation. This is the state observed in the selected run, not a live
balance. Unspendable event diamonds remain separate by output, and fee outputs
are excluded.

The saved plot records its endpoint choices and counts by type, so the ELK SVG
and Miro sync use the same selection. These choices belong to the generated
layout, like its hop range. To change them, generate another layout and select
it for the managed board. Plotting does not fetch fresh spend observations.

**Include context addresses** adds other input addresses and spendable sibling
outputs around the transactions already on matching paths. It is off by default.
Context uses thinner arrows and is marked `CONTEXT` in the transaction CSV. The
extra addresses do not create traced links, change hop counts or endpoint matches,
or cause earlier/later transactions to be added. No fee outputs or additional
event outputs are included as context. A shared address keeps one circle, with
each traced or context UTXO retaining its own arrow. The saved layout records
this choice for both SVG export and Miro sync. Generate a new layout to change it.

Set layout attempts, connector appearance, attribution arrow coloring, and
named-group centering here. **Full trace** also supports **Separate branch hubs**,
**Group isolated context inputs**, and **Include transaction fee flows**.
**Group isolated context inputs** is also available for **Paths to peg-outs**
when **Include context addresses** is enabled. It combines at least two eligible
external input addresses used only by one transaction into a context summary.
Traced, shared, attributed, and otherwise protected addresses remain separate,
as do sibling outputs. Every input retains its exact UTXO connector and CSV row;
the full member addresses stay available in local details. Endpoint matches,
hop limits, and trace evidence are unchanged.

Grouping is saved as an investigation layout preference and captured with each
generated layout. Disabling context or choosing Starter connections preserves
the preference without applying it. Separate branch hubs and fee flows remain
Full trace-only. Generate an update layout and choose **Update board** to apply
grouping changes. Replaced context objects join the newly arranged additions.

**Save layout settings** persists the preferences with this investigation, even
before collection. **Generate** saves pending layout edits before generating the
plot. Unsaved edits survive tab navigation within the browser session; saved
settings survive closing the browser or restarting the server. A failed save
keeps the edits and does not start plotting.

Every plot records its source run, goal, and coverage. Review the preview before
downloading or syncing. A plot with no matching paths does not prove that no connection or
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

## 3. Download the plot or sync with Miro

The saved browser plot offers **Download ELK SVG** and **Sync with Miro**.
Downloading returns that plot's SVG immediately. **Sync with Miro** opens the
board manager with the same saved plot selected. Neither action requires you to
generate another plot.

Miro publication uses the saved graph and layout. **Create & sync** publishes a
fresh layout to a new private board. **Update board** applies the preview bound
to that existing board. Miro chooses connector routes, so exact bends can differ
from the ELK SVG.

### Manage boards

Open **Miro boards** in the browser, or **View / create / sync boards** in the
terminal. The browser keeps a separate block visible for every created or linked
board, including historical boards. Each block has its own name, goal, status,
Miro link, and saved layout information. Managed boards also have their own
compatible layout picker and sync controls.

1. For a new graph, select a reviewed **New board** layout and enter a board name.
2. Choose **Create & sync**. Creation and initial publication are one action.
   If publication is interrupted, retry the same layout to resume that board.
   A different fresh layout creates a separate board, even if its name matches.
3. To maintain a board, choose **Prepare update** in its block, or select it under
   **Update existing board** in Plot Layouts. Generate and review the update.
4. Choose **Update board** in the same block. Existing positions stay intact;
   additions arrive in the separate ELK-arranged area and connect to retained
   objects. A provided board can first be linked, then used for an update layout.

Changing one board's layout selection does not change another board's selection,
including boards with the same goal. Draft choices survive tab navigation;
reopening the application restores each board's saved plot binding. Interrupted
syncs stay bound to their recorded plot until recovered. A background operation
temporarily disables other write actions, while all board blocks remain visible.

Each board keeps its own saved item mapping. The update is checked against a new
board inventory before writes. If you move or edit board content after preparing
the layout, regenerate it; the program will not apply stale positions. Its own
acknowledged partial-sync changes are tracked for safe retries. Obsolete managed
items are removed when the new tracing rules exclude them, including nodes
previously moved by hand. Manual text/style changes or unrelated attachments
can block removal so that work can be preserved. Unrelated objects stay intact.
Older plots retain their legacy sync controls separately.

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

The browser has four task tabs: **Collect data**, **Plot Layouts**, **Miro boards**,
and **History & downloads**. The last tab combines saved-run history with SVGs,
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
Edit a current investigation's layout preferences in **Plot Layouts**;
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
and board steps. Use the main **Plot Layouts** workflow for a new view of collected
investigation data.

## CLI

Collection keeps its existing command and limits:

```sh
liquid-live trace --case CASE_DIRECTORY --resume latest --additional-hops 3
```

Generate local plots from the saved collection run:

```sh
liquid-trace plot --case CASE_DIRECTORY --goal full --run latest --open
liquid-trace plot --case CASE_DIRECTORY --goal connections --run latest --max-hops 10 --open
liquid-trace plot --case CASE_DIRECTORY --goal pegouts --run latest --min-hops 0 --max-hops 10 --open
```

Create and publish the reviewed fresh layout, or link a provided board:

```sh
liquid-live investigation-board-create-sync --case CASE_DIRECTORY --preview PREVIEW_ID --name "Peg-out paths" --max-items 750
liquid-trace investigation-board-link --case CASE_DIRECTORY --goal connections --name "Starter connections" --board BOARD_ID
liquid-trace investigation-boards --case CASE_DIRECTORY
```

Use the board record ID to prepare an update, then apply its returned preview ID:

```sh
liquid-live plot --case CASE_DIRECTORY --goal full --run latest --layout-mode update --board-record-id BOARD_RECORD_ID --open
liquid-live investigation-board-sync --case CASE_DIRECTORY --record BOARD_RECORD_ID --preview PREVIEW_ID --max-items 750
```
