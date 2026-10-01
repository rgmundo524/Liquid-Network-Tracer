# Automatic address transaction counts

Address totals appear inside each address circle, below the Explorer link in
Miro, and are populated as part of the normal visual workflow. A separate
**Fetch address transaction counts** action is no longer required to obtain them.

After a successful/bounded trace or continuation, missing counts are requested
before exporting that run's graph. Existing saved runs are also enriched before
ELK, Mermaid and compact previews, and before ordinary live **Sync to Miro**.
Starter connections use saved counts without fetching additional statistics.
No blockchain retracing is involved in a count lookup.

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

`address-counts.json`, committed incremental count checkpoints, and existing
address-review observations are reused. Lookups
are deduplicated by full address. Count requests now scale automatically. A run
using the usual eight API workers starts with up to eight and can grow to 64
when response latency and the configured request rate justify more overlap.
Saved worker settings below eight remain explicit ceilings, including serial
operation at one. Older runs without a worker setting start with eight.

The automatic target uses the complete endpoint service time: network requests,
evidence recording, cache access, and response decoding. It excludes API pacing,
authentication, and retry waits so waiting for a shared quota does not inflate
the worker target. Fast early responses retain the initial eight-worker overlap
unless an explicit lower setting, resource limit, or retry pressure reduces it.
Longer service times can grow the target gradually to 64. The target aims for
configured requests per second multiplied by service time, with modest headroom.
Throttling, retryable server errors, and network errors reduce the target.
All workers still share the saved request pacing, retry cooldowns, and lookup
budget. Extra workers never increase the request-rate allowance.

The resource ceiling is rechecked during the lookup: at most eight network
workers per available CPU (respecting affinity and CPU quotas), and a planning
budget of 32 MiB per worker from one quarter of available memory. The ceiling
also respects the number of requested addresses and an absolute maximum of 64.
This is a concurrency planning allowance, not a process memory limit. If memory
cannot be measured, the memory-based ceiling is eight. Shrinking the target
lets existing requests finish before starting replacements.

`LIQUID_COUNT_WORKERS=auto` is the default. Set this nonsecret variable in
`devenv.nix` to a whole number from 1 to 64 for a fixed requested target. Resource
limits and pressure backoff still apply. This setting controls address counts;
ordinary transaction collection keeps its existing worker setting.

A separate ancillary-cache lock permits one count lookup per investigation.
Completed requests are processed immediately, without waiting for a fixed batch.
Cache writes and progress updates stay on the coordinating thread. Raw responses
are archived individually. Every 32 successes, or on the next completed response
after one second, only new count observations are committed to
`address-counts.sqlite3`. Records have case/source-bound checksums; concurrent
readers see committed snapshots. Completion, errors and orderly interruption
flush the pending batch. A compatible full `address-counts.json` is written once
at the end, then the committed journal is compacted. If that write is interrupted,
the journal remains available for recovery. Preserve the whole case directory,
including SQLite sidecars, while work is active.

The count pipeline also releases completed response bodies and futures instead
of retaining one for every requested address. Pending requests remain bounded by
the worker target; the count table itself still grows with the investigation.
The code does not recursively reacquire the trace lock when a trace immediately
exports or syncs its graph.

Automatic statistics are a separate reported API phase, using the investigation's
optional request/time settings when **Use optional run budgets** is on.
The default has no request-count or total-time cap, including for older cases
with saved finite numeric values. Explicit count CLI limits also default to
`0` (unlimited); a positive value sets a cap for that invocation. Per-request
timeouts, bounded retries, API pacing, and concurrency resource checks remain
active.
They do not consume hops, expand UTXOs, or change tracing conclusions. The result
contains an `address_counts` report with fetched/known/total/remaining/failed,
requests used, and a stop reason. Budget exhaustion or an API failure is visible
in terminal progress and the browser result. Good counts are retained; missing
ones are retried automatically on the next normal visual operation. A global
network/authentication failure stops new scheduling when observed; already
started requests drain and their successful responses are still saved, including
on an explicit lookup that raises an error. Interrupted/error traces are not
followed by a new count lookup. An abrupt process kill can leave the latest cache
checkpoint behind its archived responses; missing counts remain eligible for a
later lookup.

Progress shows the current concurrency target and observed counts per second.
The result also records automatic/fixed mode, peak requested concurrency,
resource ceiling, elapsed time, checkpoint count/time, and observed throughput. These are measurements
of this lookup, not a promise of provider performance or a published API quota.

Counts remain dated observations, not a live feed. `0` is an observed zero; `??`
means no usable count is available. The optional explicit count command supports
`--refresh` for already-cached values. Bitcoin peg-in context circles are still
not queried using a Liquid API and retain `??`.

## Recover a collection stopped during address counts

A collection interrupted during its count phase can have a finished transaction
checkpoint without a completed export manifest. Ordinary continuation cannot use
that unsealed run. Once its worker has exited, recover it offline from the project
directory:

```sh
liquid-trace recover-collection --case cases/.shared-collection
```

For a private collection, use its investigation directory instead. The command
auto-selects only a single eligible run; if several exist, it lists their IDs and
requires `--run RUN_ID`. It does not call an API or unlock credentials. Active
collection/count locks prevent it from modifying a running job.

Recovery verifies the finished transaction checkpoint against saved response
evidence and its investigation/parent identity, overlays the validated count
cache, preserves the original checkpoint, and seals the export. It records how
many counts remain missing. It does not claim that unfinished tracing completed
or overwrite an existing historical archive. A collection that has advanced to
a different parent is rejected.

This supports an initial collection or a continuation from the current latest
run. It deliberately rejects a fresh replacement collection over an older latest
run: older collection requests did not record the baseline needed to verify that
replacement safely.

After recovery, restart the UI and choose **Continue shared data** with
**Additional hops = 0** and the same hop reference. Saved confirmed transactions
and count-cache entries are reused; only missing counts need lookup. Normal
continuation still refreshes eligible spending observations. Alternatively,
fetch only missing statistics with `liquid-live address-counts --case
cases/.shared-collection --run RUN_ID` without `--refresh`.

For an older worker still fetching counts, send SIGINT only to the identified
collection worker and wait for it to finish cleanup before restarting the server.
The launching terminal reports `Local web action interrupted.`. The server's
shutdown fallback can forcibly terminate a slow worker after ten seconds, so
stopping the worker first gives a large cache time to finish its final save.

## Offline and immutable operations

`build_graph`, `saved_graph`, transaction CSV export, read-only workspace loading,
and Miro `--dry-run` remain offline. Publishing an already reviewed immutable
connection/compact snapshot does not change its counts or its approved content.
New previews obtain counts automatically; old SVGs and saved snapshots are not
rewritten. The ordinary sync path still protects manual Miro edits and can reuse
a matching completed ELK layout after count hydration.

Tests use synthetic cases and mocked public/Enterprise statistics. No private case
addresses, provider credentials or live Miro writes are required for the suite.

`python scripts/benchmark_address_counts.py --repo /path/to/checkout` measures
the same synthetic workload against different checkouts with real request pacing,
evidence writes and count-cache saves. Its defaults fetch 512 addresses while
reusing 20,000 saved counts, with a 49 requests/second target and 40 ms simulated
responses. One local comparison against `061dcc0` improved measured lookup
throughput from 35.7 to 46.3 counts/second. Full JSON cache writes fell from 16
to 1, bytes written from 90.6 MB to 5.7 MB, and retained endpoint futures from
512 to 8. Count and response-evidence fingerprints matched. This is a synthetic
measurement, not a prediction for a live provider or a million-address case.

## Inline display and migration

New graphs use one native circle containing its address label, Explorer link
when available, and final `TX: ...` row. There is no separate count object
and no grouping phase or group API call. The automatic retrieval and calculation
above have not changed. See `inline-address-counts.md` for old-board migration
and immutable-preview compatibility.
