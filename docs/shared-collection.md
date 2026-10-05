# Shared collection across investigations

Shared collection retrieves transaction evidence once so several investigations
can generate their own plots from it. Each investigation keeps its own selected
seed outputs, attribution and change-output CSVs, colors, layout preferences,
and Miro boards. Existing private collections stay available.

## One collection, combined and individual charts

For ten starting transactions with separate investigations:

1. Open the ten investigations and verify their selected transaction outputs.
2. Focus the investigation whose collection policy you want to use. In
   **Collect data**, start **Shared collection** and review the selected
   investigations, hop allowance, optional resource budgets, and policy source.
3. Collect the shared evidence. Repeated seed outpoints are collected once.
4. In each investigation's **Plots & Miro**, choose **Data source → Shared
   collection** and the same saved **Shared snapshot**. Choose the plotting goal
   and generate its preview or Miro board.
5. To create a combined chart, create or open an investigation containing all
   ten selected seed sets. Choose the same shared snapshot and generate its plot.

The combined investigation can use different annotations and presentation
settings from the individual investigations. Generating another plot reuses
saved blockchain evidence; it does not repeat transaction or address-count
requests. Updating or publishing a Miro board still uses the Miro API.

Address counts completed later with `address-counts --case cases/.shared-collection`
are available to newly generated plots, including investigations that already
have a saved projection of that shared snapshot. Plots use the newest saved
count for each relevant address from the matching shared dataset or the
investigation. Saved plots keep their captured counts; generate a new preview
or board update to include newer counts. No additional count requests or
changes to the sealed transaction snapshot are needed.

## Reusing large snapshots efficiently

The first plot from a saved snapshot verifies the complete archive and builds
an on-disk transaction index in that collection's `indexes/` directory. Progress
shows verification, loading, and indexing. Other jobs using the same snapshot
wait for that one builder; cancellation remains available. The first build can
still take time and temporary memory proportional to the collection.

Later plots reuse the index and follow exact transaction-output spends from the
investigation's selected outputs. Ordinary hop limits are applied before loading
transaction bodies and creating the case-scoped archive. Search stops when no
saved child remains. Missing evidence remains unknown, and an output known to
be spent beyond the selected range is not reported as unspent. Complete inputs
and outputs, original observations, and necessary funding context stay in the
saved plot evidence and CSVs.

Starter connections use indexed forward and reverse spend links to select all
qualifying paths. **All saved evidence** still searches beyond the displayed hop
limit; **Transaction-hop limit** restricts paths to that limit. Named-group peg-out
queries currently retain the full reachable evidence before applying group-hop
rules, because a group can reset the distance far beyond the ordinary hop limit.
These searches can still cost more than a short ordinary-hop query.

Index reuse uses the validated working snapshot, not a fresh checksum audit of
every original byte on every plot. Source manifest and file identity, size, and
change-time metadata invalidate stale indexes; selected indexed records and
copied observations are checksum-checked. A changed archive triggers full
verification and rebuilding, and failed verification prevents publication.
Indexes are disposable derived data; sealed archives remain authoritative.
Existing saved plots keep their own sealed evidence and remain usable without
the original shared archive. No redownload is required to build an index.

## Collection policy and continuation

A fresh shared collection captures the union of configured seeds in the selected
open investigations. The focused investigation supplies its collection limits,
stop-tracing rules, and optional named-group hop origin. The job records that
policy and its source investigation. Other cases' rules are not combined into
an implicit policy.

Shared collection has no transaction, output, request, or total-time cap by
default, including continuations. The focused investigation's **Use optional run
budgets** setting activates its saved numeric caps; each `0` remains unlimited.
The request captures that choice and its values at launch. Existing finite
values do not silently limit a case that has not enabled budgets. API rate
pacing, request timeouts, retries, and cancellation still apply.

Opening or closing investigation tabs, or editing another investigation after
launch, does not change the captured request. Closing the focused tab does not
cancel collection.

Continuation pins the latest shared snapshot and keeps that snapshot's seed
set and membership. It extends the saved branches using the policy shown for
the new collection request. With the same hop origin, additional hops increase
the previous ceiling. Changing the hop origin uses the entered maximum for
that origin. To add other seed outputs, start a fresh shared collection with the
desired investigations open.

After a gracefully saved error or interruption, choose **Continue shared data**
and set **Additional hops** to **0** to finish the existing hop ceiling. Keep
the same hop origin and policy settings. For example, a run stopped at hop 12
of 15 resumes toward 15; entering 15 additional hops would instead target 30.
The continuation reuses saved confirmed transactions and resumes unfinished
outputs, while refreshing spending observations. The failed run remains an
unchanged historical snapshot.

If another collection completes before a prepared continuation starts, the
stale continuation is rejected. Select the new latest snapshot before retrying.
Historical shared snapshots remain available for plotting.

There is one shared dataset per workspace. Its collections must use a compatible
blockchain and API source. Synthetic investigations must use the same fixture.
This release does not import or merge existing private archives into the shared
dataset; start a shared collection and reuse its completed snapshots.

## What a shared snapshot can and cannot show

| Question | Behavior |
| --- | --- |
| Which seeds start an individual chart? | The current investigation's selected outputs, never all shared seeds automatically. |
| Which annotations affect the chart? | The current investigation's rules and presentation settings. Each plotting goal retains its normal rules. |
| What if a needed branch was never collected? | Plotting cannot supply it. Continue or recollect shared data with suitable limits and stop rules. |
| Does a collected hop count guarantee complete coverage? | No. It records depth reached; confirmation policy, explicit stops, failures, interruption, or an enabled resource budget can leave gaps. |
| Is an unchecked branch an unspent endpoint? | No. Unspent status requires saved spending-status evidence. |
| Do private collections change? | No. Their saved runs, latest private run selection, and existing plots remain available. |

The shared collection's hop measurement starts from its union of selected seeds.
Individual plots use distances from their own selected seeds, with the applicable
named-group basis. These distances can differ from the shared summary. A Full
trace uses the selected shared snapshot's hop ceiling measured from its own
seeds and applies its investigation's display rules. Starter connections and
Paths to peg-outs retain their own documented selection rules. Sharing evidence
does not bypass a plotting goal's scope.

New Starter connections and Paths to peg-outs plots select transactions within
that investigation's scope, then include every input and output of those
transactions in their graph and transaction CSV. Side-branch outputs remain
visible as context without expanding unrelated transactions. Selecting shared
data does not automatically include every shared seed or export the whole pool.

A stop applied during shared collection can prevent evidence from being fetched
for every investigation using that dataset. Removing the stop in an individual
plot only exposes evidence that is already saved; it cannot fill the missing
branch. Choose the shared collection policy with all intended charts in mind.

## Saved layouts, exports, and concurrent work

A plot pins the shared dataset identity and exact shared run. It saves a
case-scoped evidence snapshot with the investigation's seeds and a record of its
shared source. This keeps existing CSV, layout, and board workflows tied to one
investigation. Creating a newer shared collection does not rewrite an earlier
plot or silently change the source used to retry its publication.

**Export endpoints** across open investigations uses each tab's selected data
source and revision. A Shared selection contributes its latest completed
peg-out plot for that shared snapshot. If none exists, the investigation is
reported as skipped instead of substituting a private or newer shared trace.
CSV rows retain investigation and plot provenance, the case-scoped run, and
the shared dataset/run identifiers. Repeated endpoints stay separate by
investigation; they are not automatically deduplicated into one financial total.

One collector writes the shared dataset at a time. Private collection, plotting
from completed snapshots, and jobs on separate boards can continue subject to
their normal locks and resource limits. Shared and private collectors use the
same Explorer request allowance. Sharing evidence avoids repeated collection;
it does not increase the API quota. Each layout still uses local CPU and memory,
and each board publication still has its own Miro work.

## Terminal commands

Collect from explicit investigation IDs using one case's policy:

```sh
liquid-live shared-collect --case POLICY_CASE --members-json '["CASE_A_ID","CASE_B_ID"]' --hops 10
liquid-live shared-collect --case POLICY_CASE --resume SHARED_RUN_ID --hops 2
```

The second command adds two hops when the hop origin is unchanged. Use the
dataset and run identifiers returned by shared collection to make a local plot:

```sh
liquid-trace plot --case INDIVIDUAL_CASE --goal pegouts --data-source shared --dataset-id DATASET_ID --run SHARED_RUN_ID --max-hops 10 --open
```

Use the same source options with `plot-sync` to generate and publish a board.
The target investigation supplies the selected seed outputs and plot rules.
