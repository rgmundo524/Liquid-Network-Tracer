# Trace to peg-outs

Use **Trace to peg-outs** in the investigation workspace or terminal menu to
search forward from a Liquid transaction and plot paths reaching peg-out
requests. The transaction does not need to be a starting transaction in the
main investigation, and the search is available before the first full run.

1. Enter the transaction ID.
2. Enter the minimum and maximum number of transaction hops.
3. Choose **Trace and plot peg-outs**.
4. Review the search status, peg-out report, and graph. If a budget interrupted
   the search, choose **Resume search** to continue its saved frontier.

The origin transaction is **hop 0**. One forward UTXO spend adds one hop, and both
bounds are inclusive. A range of 0–0 finds requests in the origin transaction.
A range of 2–4 includes any verified path of two, three, or four spends ending in
a peg-out request. If a request is also reachable in one hop, its qualifying
longer paths still appear. All origin outputs are considered.

The search fetches transactions using the investigation's API source, confirmed
transaction cache, request throttling, and bounded parallel fetching. Each
search or continuation uses the saved transaction, output, API attempt, and time
budgets. CLI flags can override those budgets for one invocation. Stop rules,
attribution hop limits, and the investigation's confirmation policy apply.
Address reuse and other inputs do not create traversal links.

Only paths reaching matching requests appear in the plot, with separate circles
for individual UTXOs. All fetched evidence is retained in the search archive.
Every displayed connector belongs to a qualifying path; combining those paths
visually can also form longer or shorter routes outside the chosen range. The
JSON report records the qualifying hop counts for each request.

## Limits and recovery

A paused search can contain useful matches while additional requests remain
undiscovered. A completed bounded search is limited to its selected hop range,
confirmation policy, stop rules, and observed spends. Zero results are not proof
that no peg-out exists.

Select a saved search to resume with the same transaction and range. To change
the range, start a new search. Its transaction responses can reuse the existing
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
python3 -m liquid_tracer pegouts --case CASE_DIRECTORY --txid TXID --min-hops 2 --max-hops 4
python3 -m liquid_tracer pegouts --case CASE_DIRECTORY --resume SEARCH_ID
python3 -m liquid_tracer pegouts-preview --case CASE_DIRECTORY --search SEARCH_ID
```

The search accepts `--max-transactions`, `--max-outpoints`, `--max-requests`, and
`--max-seconds` overrides. Add `--open` to open a completed local preview.

## Miro

After reviewing the preview, publish its immutable snapshot to a **separate Miro
board**. The main investigation board and boards with existing full-trace or
starter-connection mappings are protected. The saved item budget applies.
Repeating the same publication reuses acknowledged items; a different snapshot
needs a different board.

```sh
python3 -m liquid_tracer pegouts-publish --case CASE_DIRECTORY --preview PREVIEW_ID --board BOARD_ID --max-items 750
```

The diamonds identify **PEG-OUT REQUESTS** in the Liquid transaction data. They
do not establish that a separate Bitcoin payout occurred. UTXO reachability does
not establish ownership or allocate confidential values.
