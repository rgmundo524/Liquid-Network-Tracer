# Automatic address transaction counts

Address totals appear inside each address circle, below the Explorer link in
Miro, and are populated as part of the normal visual workflow. A separate
**Fetch address transaction counts** action is no longer required to obtain them.

After a successful/bounded trace or continuation, missing counts are requested
before exporting that run's graph. Existing saved runs are also enriched before
ELK, Mermaid, compact and starter-connection previews, and before ordinary live
**Sync to Miro**. The connection view requests only its displayed Liquid addresses.
An empty connection view makes no requests. No blockchain retracing is involved.

## Source and calculation

Each distinct full Liquid address uses one statistics endpoint:

```text
GET {saved_run_api_base}/address/{full_address}
count = chain_stats.tx_count + mempool_stats.tx_count
```

This is Esplora's documented address-statistics API, not the HTML page, transaction
history pagination, number of chart edges, or number of UTXOs. Parsing requires
both explicit nonnegative integer totals and the matching address. Unrelated
funded/spent TXO statistics are no longer prerequisites for displaying a TX count.
No missing field is interpreted as zero. Confidential asset/value data does not
prevent reading these address transaction totals.

Public-API runs continue using their public API; Enterprise runs continue using
Enterprise authentication. There is no silent switch of provider or network and
no HTML scraping. The browser and terminal route previews through the existing
SecretSpec flow when missing Enterprise counts require credentials. API secrets
never go into browser responses or the ELK/Mermaid renderer environment.

## Caching, limits and failures

`address-counts.json` and existing address-review observations are reused. Lookups
are deduplicated by full address. Requests run concurrently using the selected
run's saved API worker count, from 1 to 8; older runs without a worker setting
use 8. All workers share the saved request pacing, retry cooldowns, and lookup
budget, so concurrency does not multiply the configured request-rate limit.

A separate ancillary-cache lock permits one count lookup per investigation.
Requests run in batches of at most the worker count, while cache writes and
progress updates stay on the coordinating thread. Each successful result is
checkpointed before the next batch. The code does not recursively reacquire the
trace lock when a trace immediately exports or syncs its graph.

Automatic statistics are a separate reported API phase, using the investigation's
saved request/time settings (or the selected run's settings for legacy cases).
They do not consume hops, expand UTXOs, or change tracing conclusions. The result
contains an `address_counts` report with fetched/known/total/remaining/failed,
requests used, and a stop reason. Budget exhaustion or an API failure is visible
in terminal progress and the browser result. Good counts are retained; missing
ones are retried automatically on the next normal visual operation. A global
network/authentication failure stops new batches after the current batch drains;
successful responses from that batch are still saved, including on an explicit
lookup that raises an error. Interrupted/error traces are not followed by a new
count lookup.

Counts remain dated observations, not a live feed. `0` is an observed zero; `??`
means no usable count is available. The optional explicit count command supports
`--refresh` for already-cached values. Bitcoin peg-in context circles are still
not queried using a Liquid API and retain `??`.

## Offline and immutable operations

`build_graph`, `saved_graph`, transaction CSV export, read-only workspace loading,
and Miro `--dry-run` remain offline. Publishing an already reviewed immutable
connection/compact snapshot does not change its counts or its approved content.
New previews obtain counts automatically; old SVGs and saved snapshots are not
rewritten. The ordinary sync path still protects manual Miro edits and can reuse
a matching completed ELK layout after count hydration.

Tests use synthetic cases and mocked public/Enterprise statistics. No private case
addresses, provider credentials or live Miro writes are required for the suite.

## Inline display and migration

New graphs use one native circle containing its address label, Explorer link
when available, and final `TX: ...` row. There is no separate count object
and no grouping phase or group API call. The automatic retrieval and calculation
above have not changed. See `inline-address-counts.md` for old-board migration
and immutable-preview compatibility.
