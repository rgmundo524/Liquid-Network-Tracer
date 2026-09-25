# Collect data, create plot layouts, download or sync

An investigation owns one set of saved collection runs and can have several Miro
boards. Each board has a name and a plotting goal. Use the same collected data
to make a full investigation chart, starter-connection chart, and peg-out chart.
In the browser, the usual sequence is **Collect data → Plot Layouts → Download ELK
SVG or sync with Miro**. Generate one plot, then choose its output destination.

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

The **Collect data** tab contains collection actions. Use the **Plot Layouts**
tab for the next step and the investigation heading to open settings.

## 2. Create Plot Layouts from saved data

In the browser, open **Plot Layouts** and select the saved run. In the terminal,
choose **Choose plotting goal** under **Plot saved data**. The available goals
are shown together:

| Goal | What it plots |
| --- | --- |
| Full investigation / Full trace | The saved investigation graph, using the current display settings. |
| Starter connections | Verified paths between the starting transactions, within the selected maximum hops. |
| Paths to peg-outs | Verified paths from selected seed UTXOs to peg-out requests, within an inclusive hop range. |

Plotting is offline. It reads the selected collection run and saved address
statistics, creates a separate local preview, and makes no Blockstream or Miro
requests. Missing address statistics stay unavailable until collected separately.
Changing the plotting goal does not alter seeds or collected evidence.

**Paths to peg-outs** uses one circle per full address per network. Repeated
UTXOs retain their separate connectors and transaction CSV rows. Unknown
addresses remain separate by outpoint, and peg-out request diamonds remain
separate even when their Bitcoin destinations match. Only verified saved
spends establish qualifying paths; sharing a circle does not create a new spend.

Set layout attempts, connector appearance, attribution arrow coloring, and
named-group centering here. **Full trace** also supports **Separate branch hubs**,
**Group isolated context inputs**, and **Include transaction fee flows**. These
three controls are disabled for filtered goals, which retain their saved values
for the next full trace.

**Save layout settings** persists the preferences with this investigation, even
before collection. **Generate** saves pending layout edits before generating the
plot. Unsaved edits survive tab navigation within the browser session; saved
settings survive closing the browser or restarting the server. A failed save
keeps the edits and does not start plotting.

Every plot records its source run, goal, and coverage. Review the preview before
downloading or syncing. A plot with no matching paths does not prove that no connection or
peg-out exists outside the saved coverage. A peg-out request does not establish
which Bitcoin transaction paid it.

A plot with no matching activity remains available to review, but its Miro sync
is disabled. It does not clear an existing board.

Each newly generated layout captures its display settings. Changing preferences
later leaves that saved layout available for download and sync with its original
appearance. Changed evidence or attribution inputs require regeneration. Older
plots without captured settings may need to be regenerated once.

## 3. Download the plot or sync with Miro

The saved browser plot offers **Download ELK SVG** and **Sync with Miro**.
Downloading returns that plot's SVG immediately. **Sync with Miro** opens the
board manager with the same saved plot selected. Neither action requires you to
generate another plot.

Miro publication uses the saved graph and layout. Normal sync preserves retained
objects' current positions, while **Sync and reorganize** applies the saved
layout. Miro chooses its connector routes, so the board's appearance can differ
from the ELK SVG.

### Manage boards

Open **Miro boards** in the browser, or **View / create / sync boards** in the
terminal. This shows the investigation's boards, their goals, saved status,
and links to open them.

1. Choose a goal and a board name.
2. Choose **Create Miro board** to create a private board, or paste an existing
   board URL or ID and choose **Link existing board**. You can initialize a board
   before generating a plot. Creating a board does not publish it.
3. Select the board and a compatible saved plot. The plot's goal must match the
   board's goal.
4. Review that plot, then choose **Sync to Miro** or **Sync and reorganize**.

Normal sync retains existing item positions. Reorganize applies the selected
plot's layout to the managed graph. Each board keeps its own saved Miro item
mapping. Later collection runs can produce new plots for the same board, so a
new board is not required each time a chart is regenerated. Obsolete generated
items are removed only after checking them for analyst changes or unrelated
connections. If those checks find changes, sync stops so you can preserve that
work first. Unrelated board objects remain untouched.

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

Create or link a goal-specific board and list the registry:

```sh
liquid-live investigation-board-create --case CASE_DIRECTORY --goal pegouts --name "Peg-out paths"
liquid-trace investigation-board-link --case CASE_DIRECTORY --goal connections --name "Starter connections" --board BOARD_ID
liquid-trace investigation-boards --case CASE_DIRECTORY
```

Use the board record ID and plot preview ID returned by those commands:

```sh
liquid-live investigation-board-sync --case CASE_DIRECTORY --record BOARD_RECORD_ID --preview PREVIEW_ID --max-items 750
liquid-live investigation-board-sync --case CASE_DIRECTORY --record BOARD_RECORD_ID --preview PREVIEW_ID --max-items 750 --reorganize
```
