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
when endpoint service time and the current request target justify more overlap.
Saved worker settings below eight remain explicit ceilings, including serial
operation at one. Older runs without a worker setting start with eight.

The automatic target uses the complete endpoint service time: network requests,
evidence recording, cache access, and response decoding. It excludes API pacing,
authentication, and retry waits so waiting for a shared quota does not inflate
the worker target. Fast early responses retain the initial eight-worker overlap
unless an explicit lower setting, resource limit, or retry pressure reduces it.
Longer service times can grow the target gradually to 64. The target aims for
current requests-per-second target multiplied by service time, with modest headroom.
Throttling, retryable server errors, and network errors reduce the target.
All workers share API pacing, retry cooldowns, and the lookup budget. Enterprise
auto mode learns a higher target as successful requests demonstrate demand;
explicit fixed rates and verified account allowances remain enforced.

`LIQUID_BLOCKSTREAM_ENTERPRISE_RPS=auto` is the Enterprise default. The initial
49 requests/second is a warmup target, not a ceiling. Before throttling, clean
two-second windows with sufficient demand raise the target by 50%. Pressure
reduces it by 30%, once per wave of related rejections, and imposes a shared
cooldown. Subsequent probes use 5% increases per clean ten-second window. The
learned target is shared and retained across processes; no API keys or addresses
are stored in the rate coordinator. Older workers and explicit fixed-rate clients
on the same host can constrain auto clients, so restart older API jobs when
enabling this mode. Positive numeric values select fixed pacing instead. A
verified `LIQUID_BLOCKSTREAM_API_RPS` allowance takes precedence at 95% of its value.

When one adaptive client is active, workers reuse a brief local denial while
waiting for the next shared admission time. This avoids repeatedly committing
the same waiting state to the coordinator. Every granted start still checks the
shared database for other clients and cooldowns; a cached result cannot grant a
request. Long waits periodically renew the client registration and flush feedback.

The coordinator reuses one SQLite connection and uses WAL with FULL synchronous
commits on supported SQLite versions. This reduces repeated file opens and disk
synchronizations while preserving committed admissions and cooldowns. Every
granted request still checks shared state in an atomic transaction. The quota
cache belongs on a local filesystem.

SQLite's [WAL-reset fix](https://www.sqlite.org/wal.html#walreset) is required to
enable this mode: version 3.51.3 or later, or the 3.44.6/3.50.7 maintenance branches
and their later patch releases. Older runtimes retain DELETE/FULL. If an older
runtime encounters a WAL cache held open by another instance, it reports the
required runtime update or instance restart instead of waiting indefinitely.
Diagnostics report the actual journal mode and SQLite version; all instances
sharing a WAL cache should use a patched runtime.

Saved runs previously recorded the derived 49 RPS interval as well as the source
of that rate. That specific implicit default is ignored for later count lookups
under the new configuration. Explicit minimum intervals, verified allowances and
other saved numeric targets remain respected; historical archives are unchanged.

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
are archived individually. Each received response and its request outcome commit
together in one evidence transaction; the request-start record is still committed
before sending the HTTP request. A response is delivered to the count pipeline
only after its evidence transaction commits. Every 32 successes, or on the next completed response
after one second, only new count observations are committed to
`address-counts.sqlite3`. Records have case/source-bound checksums; concurrent
readers see committed snapshots. Completion, errors and orderly interruption
flush the pending batch. A compatible full `address-counts.json` is written once
at the end, then the committed journal is compacted. If that write is interrupted,
the journal remains available for recovery. Preserve the whole case directory,
including SQLite sidecars, while work is active.

The mutable evidence database also uses WAL/FULL on patched SQLite runtimes.
It selects the journal mode on the first evidence write, so merely opening an
existing evidence store for an offline export does not migrate it. Existing
observations, checksums and request-attempt records are retained. Older runtimes
use DELETE/FULL; if an existing reader prevents a safe switch to WAL, that lookup
also retains DELETE/FULL. Both request-start and response commits remain durable
before the pipeline proceeds. Keep the case on a local filesystem and preserve
its SQLite sidecars while a worker is active; completed run archives are unchanged.

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

Progress shows the current concurrency target and observed counts per second,
separately from the adaptive or fixed API target.
The result also records automatic/fixed mode, peak requested concurrency,
resource ceiling, elapsed time, checkpoint count/time, and observed throughput. These are measurements
of this lookup, not a promise of provider performance or a published API quota.

Counts remain dated observations, not a live feed. `0` is an observed zero; `??`
means no usable count is available. The optional explicit count command supports
`--refresh` for already-cached values. Bitcoin peg-in context circles are still
not queried using a Liquid API and retain `??`.

## Measure throughput with useful count requests

After updating and restarting API workers, run a bounded sample against a
completed saved collection:

```sh
env LIQUID_BLOCKSTREAM_ENTERPRISE_RPS=auto liquid-live explorer-probe \
  --case cases/.shared-collection --seconds 60
```

Use a private investigation's directory for its own collected data, or `--run`
to choose a particular completed run. This uses the normal authenticated
`GET /address/:address` API and saves only missing counts. It does not scrape
webpages or repeat cached requests to create test traffic. Stop any existing
collection/count worker for the selected dataset first; normal locks apply.
If an old collection stopped during counts without a finished export, use the
recovery command below before probing it.

The default sample admits requests for up to 60 seconds or 10,000 HTTP attempts,
including OAuth and retries. `--max-requests` changes this probe-only budget;
ordinary collection remains unlimited by default. Archive validation and final
durable saves can extend the command's total wall time. The JSON result includes
overall counts/second, peak/latest five-second windows, learned/effective targets,
worker counts and throttling responses. Compare `counts_per_second` and the
measurement windows with the requested target; a target alone is not achieved
throughput. A short or already-cached sample cannot establish available capacity.
The probe never certifies a provider's maximum or overrides an explicit allowance.

A final request's socket timeout may be shortened to fit the remaining probe
budget. A known transport timeout after that local deadline is recorded as
`local_time_limit`, counted in `local_deadline_timeouts`, and ends the probe
without lowering the learned API target. Early timeouts, normal twenty-second
timeouts, disconnects, HTTP 429 and retryable server errors still provide pressure
feedback. This distinction also applies to other bounded collection requests.
Previously saved backoff is retained because its cause cannot be inferred safely.
Use a longer sample to let that target recover gradually:

```sh
env LIQUID_BLOCKSTREAM_ENTERPRISE_RPS=auto liquid-live explorer-probe \
  --case cases/.shared-collection --seconds 180 --max-requests 30000
```

The result includes cumulative network, evidence-store, pacing and retry seconds,
completed requests/endpoints, and `peak_in_flight` (actual simultaneous HTTP calls).
`peak_workers` is the largest requested worker count, which can include workers
waiting for pacing or local storage. `latency_seconds` and
`service_latency_seconds` are recent weighted averages, not totals. Service time
includes evidence and decoding but excludes admission and retry waits. The worker
timing totals overlap and must not be added to calculate elapsed time.
`quota_reserve_calls`, `quota_admitted` and `quota_denied` count coordinator checks,
including inexpensive cached denials; `quota_reserve_seconds` measures time
inside those checks and is already included in pacing time.
`quota_sqlite_version`, `quota_journal_mode`, `quota_journal_mode_requested`,
`quota_synchronous` and `quota_connection_mode` identify the coordinator storage
configuration without exposing paths or credentials.
The matching `evidence_*` storage fields identify the evidence database's mode
and runtime. Evidence diagnostics separate read/write lock waiting, time holding
the lock, and commit time. Commit time is included in write time; these values
are not additional elapsed time to add to the other totals.

`window_series` retains the latest 120 complete five-second measurement windows.
Each includes start/end times, completed counts, throughput, worker and actual
HTTP concurrency, available rate/storage gauges, and timing-counter deltas.
Missing measurements and counters that reset within a window are omitted instead
of reported as zero. `window_series_dropped` identifies older windows omitted by
the bound. `shared_api_active_clients`, `shared_api_peak_active_clients` and
`shared_api_window_peak_active_clients` report current and observed peak local
client counts sharing the coordinator; registrations can remain briefly after
work stops, and a client can be missed between samples. The window series helps
distinguish a late slowdown from a healthy early
peak. Overlapping worker timing deltas are still not wall-time percentages.
`probe_outside_lookup_seconds` separates archive verification, state/cache
preparation and cleanup from the measured lookup; this is local work, not an API
rate-limit delay.

A target higher than achieved throughput with zero rate-limit responses does not
establish a provider limit. For example, a 110 RPS target with 38 completed counts
per second leaves local storage, admission overhead and response latency to
investigate. Raising the target or worker count alone may not improve it. A
`time_limit` stop on a bounded probe is expected, and successful counts remain
saved. Once recovery has already sealed a run, repeating `recover-collection` can
report that no finished unsealed collection was found; use the saved run directly.

Restart the UI in an existing shell with:

```sh
env LIQUID_BLOCKSTREAM_ENTERPRISE_RPS=auto liquid-web
```

This explicitly replaces an old shell's exported `49` value. A newly loaded
devenv shell gets `auto` from the updated configuration. Verified account limits
and other active fixed-rate workers still apply. The learned target and saved
counts from the probe are reused by subsequent work.

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

The same benchmark can simulate throttling and exercise the production shared
SQLite coordinator without making network requests:

```sh
python scripts/benchmark_address_counts.py --shared-quota --provider-rps 150 --addresses 2400
python scripts/benchmark_address_counts.py --shared-quota --adaptive --provider-rps 150 --addresses 2400
```

With 2,400 fresh addresses, 20,000 cached counts and 40 ms simulated responses,
one local comparison completed in 50.92 seconds at fixed 49 RPS and 25.00 seconds
in adaptive mode. Measured successful lookup throughput was 47.32 versus 96.85
counts/second, including warmup and cooldown. Adaptive mode retried two HTTP 429
responses and preserved all 2,402 response observations. Counts and successful
response fingerprints matched. The provider's 150 RPS capacity is synthetic;
this test verifies feedback, persistence and scheduling rather than predicting
Blockstream performance.

To measure sensitivity to durable-storage latency, compare checkouts with:

```sh
python scripts/benchmark_address_counts.py --shared-quota --adaptive \
  --provider-rps 150 --addresses 1200 --network-delay-ms 150 \
  --evidence-commit-delay-ms 8 --quota-commit-delay-ms 2
```

Against revision `582a4b8`, one local comparison improved from 35.98 to 53.88
counts/second, with wall time falling from 33.64 to 22.60 seconds. Evidence
commits fell from 3,600 to 2,400 while retaining all 1,200 responses and 2,400
request-attempt rows. Counts, response hashes and attempt fingerprints matched.
The baseline reproduced a 110.25 RPS target without HTTP 429 responses, despite
achieving only about 36 counts/second. This demonstrates how local storage can
produce that pattern; it does not diagnose a particular machine.

With no injected storage delay, 1,600 addresses and the same simulated network
latency, throughput was essentially unchanged (133.38 versus 131.61 counts/second).
Quota database commits still fell from 8,815 to 3,167, and time inside reservation
calls fell from 6.55 to 3.23 seconds. This second comparison used no synthetic
provider cap. Reservation-call counts include cached denials; the benchmark's
actual commit counters distinguish them from database writes.

On Linux with a C compiler, the optional sync helper measures actual `fsync` and
`fdatasync` calls rather than assigning the same cost to every COMMIT. It can add
latency per synchronization while still executing the real disk operation:

```sh
python scripts/benchmark_address_counts.py --shared-quota --adaptive \
  --addresses 1200 --network-delay-ms 40 --evidence-commit-delay-ms 4 \
  --measure-sync --quota-sync-delay-ms 2
```

The helper is compiled and loaded only into the temporary offline benchmark
process. It separates evidence file syncs from coordinator file/directory syncs,
preserves real synchronization calls, and rejects runs with failed syncs.
Ordinary collection never loads it.

Against `69b8166`, a local comparison using the above delays improved from 66.26
to 83.85 counts/second (18.32 to 14.54 seconds). Coordinator sync calls fell from
5,352 to 1,544 and time inside admission checks from 12.26 to 4.33 seconds. All
1,200 response hashes, counts and 2,400 request-attempt rows matched; evidence
sync counts were unchanged. Without injected storage delays, throughput was
essentially unchanged (117.52 versus 117.21 counts/second), while coordinator
syncs fell from 9,600 to 2,416. These synthetic results isolate coordinator I/O;
the probe on the investigator's machine measures the actual effect.

To isolate the evidence-store journal change, compare against `27cd0cb` using
1,200 addresses, 40 ms network latency, `--measure-sync`,
`--evidence-sync-delay-ms 2` and `--quota-sync-delay-ms 1`, with no per-COMMIT
delay. One local comparison improved from 63.33 to 111.42 counts/second, with
wall time falling from 19.16 to 11.01 seconds. Evidence file syncs fell from
7,209 to 2,430; both versions still committed 2,400 evidence transactions.
All counts, response bytes and request-attempt fingerprints matched. The
benchmark observes the actual journal and synchronous modes before closing
the Store, and rejects durability below FULL. The optional zero-delay method
wrapper now leaves production lock acquisition untouched so measured Store
lock waits are not hidden by an instrumentation lock.
Without injected storage delays, throughput was essentially unchanged
(118.39 versus 118.28 counts/second), while evidence file syncs still fell
from 7,209 to 2,430. This isolates a storage improvement; it does not establish
the cause of any particular slowdown on a live machine.

## Inline display and migration

New graphs use one native circle containing its address label, Explorer link
when available, and final `TX: ...` row. There is no separate count object
and no grouping phase or group API call. The automatic retrieval and calculation
above have not changed. See `inline-address-counts.md` for old-board migration
and immutable-preview compatibility.
