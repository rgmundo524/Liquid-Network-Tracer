# Trace to peg-outs

This guide describes the older standalone search and its recovery commands.
For the shared **Collect data → Plot Layouts → Miro boards** workflow, use
[the investigation workflow guide](investigation-workflow.md). Its peg-out plots
read saved collection data, and its boards can be updated with later plots.

Use **Trace to peg-outs** in the investigation workspace or terminal menu to
search forward from all of the investigation's originally selected seed UTXOs
and plot paths reaching peg-out requests. The search is available before the
first full run.

1. Enter the minimum and maximum number of transaction hops.
2. Choose **Trace and plot peg-outs**.
3. Review the search status, peg-out report, and graph. If a budget interrupted
   the search, choose **Resume search** to continue its saved frontier.

Each starting transaction is **hop 0**. Only its selected seed outputs start
the search; unselected sibling outputs are excluded, including at hop 0.
One forward UTXO spend adds one hop, and both bounds are inclusive. A range of
0–0 finds requests among the selected seed outputs themselves.
A range of 2–4 includes any verified path of two, three, or four spends ending in
a peg-out request. If a request is also reachable in one hop, its qualifying
longer paths still appear. All saved seed UTXOs participate, including selections
from multiple starting transactions.

For an independent search, enable **Use a different starting transaction** and
enter a transaction ID. This optional mode considers **all outputs** of that
transaction, including peg-out requests at hop 0, as before. It does not change
the investigation's seeds. If the investigation has no selected seeds, choose
this mode explicitly; the search will not substitute all outputs automatically.

The search fetches transactions using the investigation's API source, confirmed
transaction cache, request throttling, and bounded parallel fetching. Each
search or continuation uses the saved transaction, output, API attempt, and time
budgets. CLI flags can override those budgets for one invocation. Stop rules,
attribution hop limits, and the investigation's confirmation policy apply.
Address reuse and other inputs do not create traversal links.

Only paths reaching matching requests appear in the plot, with one circle per
full address per network. Each UTXO keeps its own input and output connectors;
unknown addresses stay separate by outpoint, and each peg-out request keeps its
own diamond even when destinations repeat. Sharing a circle does not establish
a spend between unrelated outputs. All fetched evidence is retained in the search archive.
Every displayed connector belongs to a qualifying path; combining those paths
visually can also form longer or shorter routes outside the chosen range. The
JSON report records the qualifying hop counts for each request.

## Limits and recovery

A paused search can contain useful matches while additional requests remain
undiscovered. A completed bounded search is limited to its selected hop range,
confirmation policy, stop rules, and observed spends. Zero results are not proof
that no peg-out exists.

Select a saved search to resume with the same saved seed selection or custom
transaction and range. Saved searches show their starting scope. Later changes
to investigation seeds do not change a saved search. To change the range or
starting scope, start a new search. Its transaction responses can reuse the existing
case cache. A failed layout does not erase tracing progress: choose **Refresh
peg-out preview** to render its archived evidence again without fetching more
transactions. If interruption prevented the archive from finishing, recover and
resume the checkpoint first.

Searches are saved under `pegouts/` with their own IDs, query, trace, raw response
evidence, and checksums. Graphs are saved under `previews/`. These searches have
their own history and do not change the full investigation's latest run or seeds.

## CLI

Run these from the project environment. Replace `CASE_DIRECTORY`, `TXID`, and
saved IDs with your values. For live searches, use `liquid-live` in place of
`python3 -m liquid_tracer` to retrieve the normal local credentials.

```sh
python3 -m liquid_tracer pegouts --case CASE_DIRECTORY --min-hops 2 --max-hops 4
python3 -m liquid_tracer pegouts --case CASE_DIRECTORY --resume SEARCH_ID
python3 -m liquid_tracer pegouts-preview --case CASE_DIRECTORY --search SEARCH_ID
```

Omit `--txid` to use the saved selected seed UTXOs. Use `--txid` only for the
optional custom transaction mode:

```sh
python3 -m liquid_tracer pegouts --case CASE_DIRECTORY --txid TXID --min-hops 2 --max-hops 4
```

The search accepts `--max-transactions`, `--max-outpoints`, `--max-requests`, and
`--max-seconds` overrides. Add `--open` to open a completed local preview.

## Miro

After reviewing the preview, publish its immutable snapshot to a **separate Miro
board**. The main investigation board and boards with existing full-trace or
starter-connection mappings are protected. The saved item budget applies.
Repeating the same publication reuses acknowledged items; a different snapshot
needs a different board.

To replace an older preview with duplicate address circles, refresh its preview
from the saved search and publish it to a different board. Existing snapshots
and their boards retain their original presentation.

```sh
python3 -m liquid_tracer pegouts-publish --case CASE_DIRECTORY --preview PREVIEW_ID --board BOARD_ID --max-items 750
```

The diamonds identify **PEG-OUT REQUESTS** in the Liquid transaction data. They
do not establish that a separate Bitcoin payout occurred. UTXO reachability does
not establish ownership or allocate confidential values.
