# Starter connections: verified saved paths between starting transactions

**Starter connections** shows every selected starting transaction and selects
transactions on verified saved paths between them, then displays every input
and non-fee output of each included transaction. Choose **Shortest connections** for one minimum-hop route per
connected pair, **Within hop limit** for all routes within a bound, or
**All saved connections** for all routes without a plotting cutoff. All three modes use
the selected saved run and ignore attribution `stop_tracing` and `hop_limit`
values. Attribution labels, colors, and recorded
confirmation status remain visible.

Use **Collect data → Plots & Miro** for the main workflow described in
[the investigation workflow guide](investigation-workflow.md). The older
connection-preview and fixed-snapshot publication commands remain available.
Generating either kind of connection graph makes no blockchain requests and
does not modify the collection archive or existing boards. Address counts use
saved observations; missing counts stay unknown.

## Shortest connections

Choose **Starter connections → Connection search → Shortest connections**, then
generate a preview. This searches the selected saved evidence without a plotting
hop cutoff. For every ordered pair with a directed connection, it retains one
shortest verified route. One hop means one transaction spending an output of
the previous transaction. The first spend must use a selected starting output.
Attribution names do not reset this distance.

If equally short routes exist, a stable ordering selects one; the other equal
routes and longer alternatives are omitted. The graph combines the selected
routes and shares their overlapping transactions and addresses. This minimizes
each pair's hop count, not the total number of objects in a connecting tree.
Inputs and outputs of the retained transactions remain visible as context,
with fees hidden unless explicitly enabled. Context does not extend the search.

For example, with `A → X → B` and `A → Y → Z → B`, Shortest connections selects
`A → X → B` (two hops). All saved connections includes both routes. Connections
to other selected starters are still searched independently: A to B, A to C,
B to C, and each reverse direction where a verified directed route exists.
All starters stay visible, including any without a qualifying connection;
the preview summary reports how many have no connection within its saved scope.
A shared receiver
such as `A → X ← B` alone does not establish a directed route between A and B.

The saved preview list, graph legend, and captured query record this choice.
Switching to a different search mode creates a new preview; earlier previews
keep their captured scope.

## Example: three starters

Select outputs from starting transactions A, B, and C and collect the evidence.
Then choose **Starter connections**, **Within hop limit**, and a maximum number
of connection hops. New browser drafts start at 10; lower it for a smaller chart.
Increase the value and generate another preview or update the same board to
reveal more connections from the same saved evidence.

A verified route such as `A -> X -> Y -> B` is included. One hop means one
transaction spending an output of the previous transaction, so this route is
three hops. The transaction-to-address and address-to-transaction lines are not
two separate hops. Direct starter-to-starter spends also qualify.

At a limit of 2, this three-hop route is excluded. At 3, it is included. The
limit counts from each starter independently and does not reset when a path
reaches another starter or a named group. Named-group distances may still be
displayed as annotations. Each saved layout records the selected search scope
and limit; switching back to a board restores its saved choice. A limit of 0
cannot connect two distinct starter transactions.

With **Within hop limit**, every verified saved route within the chosen bound
is considered, not only the shortest route. **All saved connections** removes that bound. A longer saved
route remains eligible even when an intermediate address has a stop rule
or an attribution hop cap. The chart contains the union of those transactions
with their inputs and outputs, including peg-outs, unspendable outputs, and
context addresses. Fee flows are hidden by default; enable **Include transaction
fee flows** to show them in a new graph and its transaction CSV. Their original
records remain in the saved evidence. Other inputs and outputs provide local context;
they do not establish a qualifying connection or add further transactions. A
side branch therefore appears as its first output without expanding its later
transactions. Unchecked outputs are not labeled unspent merely because that
branch is not followed. If C has no saved connection to another starter, C and
its local inputs and outputs remain visible without adding a connecting path.
Even with no connected starter pair, a new preview contains all the selected
starters and can be reviewed and published. Its connection report records zero
qualifying pairs; shared context alone does not establish a directed connection.

The search is directed. `A -> X <- B` does not qualify unless X is itself another
selected starter. The source may start only through its selected seed outputs.
A shared address or matching attribution name does not establish a connection.
An input reference can establish a connection only when its exact spend can be
verified against the saved source transaction and output.
Verified saved unconfirmed links are eligible and retain their recorded status;
they are not presented as confirmed. The report lists ordered starter pairs and
their shortest verified distances.

## Search coverage and older plots

The plot can only use evidence that was collected and verified. It does not
fetch missing transactions. Collection still obeys explicit `stop_tracing`
rules, its global hop ceiling, and confirmation policy. Request, time,
transaction, and output caps are optional and disabled by default. A stop that
prevented collection can therefore leave a connection unavailable even though the plotting goal ignores that stop.
“No connection found in the saved searched data” is not proof that no on-chain
connection exists.

To collect beyond an explicit address stop, update that rule and continue
collection. Use 0 additional hops to revisit eligible branches within the
existing ceiling, or add hops for a larger ceiling. Generate a new connection
plot from the resulting saved run.

Older saved Starter connections queries retain their original stop rules,
attribution caps, hop bounds, input/output selection, and starter visibility. Their reports and
publication scope do not change when the application is updated. Generate a new
layout to include every starter, plus complete transaction inputs and outputs
on selected verified saved connections. Saved preview selectors distinguish **Shortest connections**,
**All saved connections**, transaction hop limits, and older bounded snapshots.

## Browser and terminal

In the browser, open **Plots & Miro**, select the collection snapshot and
**Starter connections**, select the connection search scope (and maximum hops
for **Within hop limit**),
then choose the board destination and generate.
**Generate preview only** creates local output without publishing. Update
previews read Miro to retain existing positions; they still make no blockchain
requests.

The terminal's **Generate plot and sync** dialog has the same goal and no
Starter connections hop chooser. The older **Starter connections** dialog uses
the latest saved run. Its **Publish starter connections to Miro** dialog lets
you select a reviewed snapshot and supply a separate Miro board.

New starter connection charts use **one circle per full address per network**,
including when several UTXOs use that address. Each input and output keeps its
own connector, exact outpoint, and CSV row. Shared addresses do not establish
additional traced connections; path selection and branch lineage still use
verified UTXO spends. Unknown addresses remain separate by outpoint. Configured
role/name colors, starter transaction styling, and applicable border-only
highlights are retained.

Regenerate an older layout to combine repeated address circles. Saved layouts
keep their original presentation. If a Miro board uses the older per-UTXO nodes,
create or link a different Starter connections board for the regenerated layout;
the original board and saved layouts remain available. No new collection is
needed for this display change.

**Group isolated context inputs** can combine eligible external input addresses
into a summary while preserving each input's connector and CSV row. Transaction
fee outputs are hidden unless **Include transaction fee flows** is enabled.

## Publishing older standalone previews

Select a saved connection preview, provide a separate board URL or ID, and
confirm publication. Credentials use the existing SecretSpec/Proton Pass
workflow in the launching terminal.

The linked full-trace board is protected. Standalone publication creates an
immutable snapshot. One connection snapshot is allowed per destination board
within an investigation. Repeating publication reuses acknowledged items; a
different preview requires a different board. Existing graphs are not deleted
to switch views. Interrupted or uncertain writes retain their publication
journal and recovery requirements.

Saved previews retain their original publication checks. If changed inputs
invalidate an older preview, generate and review a new one. The main managed
board workflow supports repeated updates from new layouts instead.

## CLI

```sh
liquid-trace plot --case /path/to/investigation --goal connections --run latest --open
liquid-trace plot --case /path/to/investigation --goal connections --run latest \
  --connection-scope shortest --open
liquid-trace plot --case /path/to/investigation --goal connections --run latest \
  --connection-scope hop_limited --max-hops 3 --open
liquid-trace connections --case /path/to/investigation --run latest --open
liquid-trace connections-publish --case /path/to/investigation \
  --preview RUN_ID-connections-PREVIEW_ID --board SEPARATE_BOARD_ID
```

The standalone `connections` command returns the actual `preview_id` for
`connections-publish`. CLI commands keep **all saved connections** as their
default for compatibility. Add `--connection-scope shortest` to select one
shortest saved route per connected pair. Add `--connection-scope hop_limited` to use
`--max-hops` with `plot` / `plot-sync`, or `--hops` with `connections`.
`--min-hops` does not filter Starter connections. These options also work when
the main plot commands use `--data-source shared`.

Standalone previews are saved in the case's `previews/` directory with
`graph.json`, `graph.svg`, `graph.html`, `graph.mmd`, `transactions.csv`,
`connections.json`, `miro-plan.json`, a layout report, and a checksum manifest.
The standalone Miro plan remains a schema-1 snapshot and cannot enter normal
cumulative sync.

Connections establish UTXO reachability, not ownership, common control, the asset
or amount of a confidential output, or allocation of stolen value. Transaction
CSV downloads contain every input and output of each selected transaction, using
the [transaction CSV schema](transaction-csv.md). Context grouping does not
collapse the individual CSV rows. Historical snapshots remain
available with their original scope and files.
