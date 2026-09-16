# Automatic address transaction counts

Address totals are now populated as part of the normal visual workflow. A separate
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
are deduplicated by full address, serialized by a separate ancillary-cache lock,
and checkpointed after each success. The code does not recursively reacquire the
trace lock when a trace immediately exports or syncs its graph.

Automatic statistics are a separate reported API phase, using the investigation's
saved request/time settings (or the selected run's settings for legacy cases).
They do not consume hops, expand UTXOs, or change tracing conclusions. The result
contains an `address_counts` report with fetched/known/total/remaining/failed,
requests used, and a stop reason. Budget exhaustion or an API failure is visible
in terminal progress and the browser result. Good counts are retained; missing
ones are retried automatically on the next normal visual operation. A global
network/authentication failure does not trigger the same failing call for every
address. Interrupted/error traces are not followed by a new count lookup.

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
