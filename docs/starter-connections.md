# Starter connections: plot only paths between starting transactions

This guide describes the older connection-preview and fixed-snapshot publication
commands. For the shared **Collect data → Plot views → Miro boards** workflow,
use [the investigation workflow guide](investigation-workflow.md). Its plots are
fully offline, and its boards can be updated with later plots.

The **Starter connections** option builds a separate, connection-only graph from
an existing saved run. The ordinary full graph and original trace archives stay
unchanged. Path selection uses saved evidence and does not retrace transactions.
Missing transaction counts for displayed addresses are fetched automatically;
these statistics requests can use Blockstream credits. See [automatic address
counts](automatic-address-counts.md). An empty connection graph makes no requests.

## Example: three starters and ten hops

Select outputs from starting transactions A, B and C and perform a bounded trace
to the needed depth. Then choose **Starter connections**, enter **10**, and select
**Plot connections** (terminal) or **Plot starter connections** (browser).

A route such as `A -> X -> Y -> B` is included. One hop means one transaction
spending an output of the previous transaction, so this route is three hops.
The transaction-to-address and address-to-transaction lines on the chart are not
two separate hops. Direct starter-to-starter spends also qualify.

Every route of up to ten hops is considered, not only the shortest route. The
chart contains the union of their transactions and exact connecting UTXOs.
Dead-end branches, unrelated co-inputs, extra outputs, fees and isolated starters
are omitted. If C is not connected to another starter within the bound, C is not
plotted. If no starter pair connects, the graph and publication plan contain no
nodes or connectors, and publication makes no Miro requests.

This is a directed search. `A -> X <- B` does not qualify unless X is itself
another selected starter. Reusing an address does not establish a connection.
The source may start only through its selected seed outputs. Stops are respected,
and passing another starter does not reset a path's hop count. Each displayed
edge belongs to a qualifying bounded path; combining edges from different paths
in the union can also form a longer route. The JSON report lists the ordered
starter pairs and their shortest verified distances.

## Search coverage

**The hop field filters saved evidence. It does not fetch missing transactions.**
First run or continue the normal trace to a sufficient depth and review whether
it stopped early because of time, request, transaction or outpoint limits.
Continuation hop settings can add to the previous ceiling; the connection field
is always an absolute maximum for each starter-to-starter path.

“No connection found in the saved searched data” is not proof that no on-chain
connection exists. Limited coverage, active tracing stops and the run's
unconfirmed-transaction policy can leave connections undiscovered. Saved raw
context inputs are not promoted to verified spends just to complete a path.

## Browser and terminal

The browser investigation workspace has a **Starter connections** panel. It uses
the selected saved snapshot. Set the maximum hops, plot, inspect the local chart,
and download SVG, Mermaid source or the connection report. Complete previews are
rediscovered when the investigation is reopened.

The terminal investigation menu has **Starter connections**, using the latest
saved run. An additional **Publish starter connections to Miro** dialog lets you
select a reviewed connection snapshot and provide a separate Miro board.

Connection-only charts use one address circle per **connecting UTXO**, including
when several UTXOs use the same address. This prevents address merging from
visually inventing cross-spends between unrelated outputs. The ordinary graph's
merged-address setting is not changed. Configured role/name colors, starter
transaction styling and applicable border-only highlights are retained.

## Publishing to Miro

In the browser, expand **Publish this reviewed snapshot to Miro**, enter a
separate board URL or ID, confirm that you reviewed the snapshot, and publish.
The terminal dialog offers the same confirmation. Credentials remain in the
launching terminal through the existing SecretSpec/Proton Pass workflow.

The linked full-trace board is protected. This feature publishes an immutable
snapshot, not a replacement for the cumulative graph. One connection snapshot
is allowed per destination board within an investigation. Repeating the same
publication reuses acknowledged items; a different preview requires a different
board. No existing graph is deleted to switch views. Interrupted/uncertain Miro
writes retain the existing publication journal and require the usual explicit
reconciliation rather than blind replay.

Changes to colors or stop rules invalidate publication approval for an older
preview. Generate and review a fresh preview after those changes.

## CLI

```sh
liquid-trace connections --case /path/to/investigation --run latest --hops 10 --open
liquid-trace connections-publish --case /path/to/investigation \
  --preview RUN_ID-connections-PREVIEW_ID --board SEPARATE_BOARD_ID --max-items 750
```

The first command returns the actual `preview_id` for the second command. Each
preview is saved under the case's `previews/` directory with `graph.json`,
`graph.svg`, `graph.html`, `graph.mmd`, `transactions.csv`, `connections.json`,
`miro-plan.json`, a layout report and a checksum manifest. The Miro plan is a
schema-1 snapshot and deliberately cannot enter normal cumulative sync.

Connections establish UTXO reachability, not ownership, common control, the asset
or amount of a confidential output, or allocation of stolen value.

Transaction CSV downloads contain only the input/output arrows in this filtered
view, using the [transaction CSV schema](transaction-csv.md). Historical snapshots
with node/edge CSVs remain valid; regenerate them to get the new transaction table.
