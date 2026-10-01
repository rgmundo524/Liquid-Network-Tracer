#!/usr/bin/env python3
"""Measure real count scheduling/cache persistence with a synthetic provider.

Run this same script with --repo pointing to baseline and current checkouts.
There are no network requests or credentials. Evidence writes, request pacing,
count-cache persistence and adaptive workers remain active. Compare counts and
response fingerprints before comparing timings; this is not a live forecast.

For an adaptive run with a synthetic provider that accepts 150 requests in a
rolling second, use --shared-quota --adaptive --provider-rps 150 --addresses 2400.
Rejected requests return HTTP 429 with Retry-After: 1; all successful counts and
every response, including rejections, must still be saved exactly once.
The --shared-quota option includes the production SQLite admission coordinator,
isolated inside the benchmark's temporary directory. Without that option,
injected transports use the in-memory coordinator or local fixed pacing.
Use --evidence-commit-delay-ms and --quota-commit-delay-ms to model slow durable
storage. These delay actual SQLite COMMIT statements, so combining writes into
one transaction reduces the simulated storage cost without dropping evidence.

On Linux with a C compiler, --measure-sync counts/times real fsync/fdatasync
calls using a temporary LD_PRELOAD helper. --quota-sync-delay-ms additionally
delays each quota sync while still performing it. This distinguishes DELETE
journal and WAL costs, unlike a fixed delay per COMMIT. Unsupported platforms
can omit these optional flags; the ordinary benchmark needs no compiler.
"""

import argparse
from collections import Counter, deque
from contextlib import nullcontext
import hashlib
import importlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch


def measured_sync_run(args, parser):
    """Run one isolated offline child with Linux-only syscall instrumentation."""
    if sys.platform != "linux" or not Path("/proc/self/fd").is_dir():
        parser.error("Optional sync measurement needs Linux /proc; omit --measure-sync and sync-delay options")
    compiler = shlex.split(os.environ.get("CC", "cc"))
    if not compiler or not shutil.which(compiler[0]):
        parser.error("Optional sync measurement needs a C compiler; install cc or omit sync measurement options")
    source = Path(__file__).with_name("benchmark_sqlite_sync.c")
    with tempfile.TemporaryDirectory(prefix="liquid-sync-benchmark-") as directory:
        helper = Path(directory) / "sqlite-sync.so"
        report = Path(directory) / "sync-report.json"
        built = subprocess.run(compiler + ["-shared", "-fPIC", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                                          "-o", str(helper), str(source), "-ldl", "-pthread"],
                               capture_output=True, text=True)
        if built.returncode:
            parser.error("Optional sync helper could not compile: " + built.stderr.strip())
        environment = dict(os.environ)
        previous_preload = environment.get("LD_PRELOAD", "")
        environment.update(LIQUID_BENCH_SYNC_ACTIVE="1", LIQUID_BENCH_SYNC_REPORT=str(report),
                           LIQUID_BENCH_QUOTA_SYNC_DELAY_MS=str(args.quota_sync_delay_ms),
                           LIQUID_BENCH_EVIDENCE_SYNC_DELAY_MS=str(args.evidence_sync_delay_ms),
                           LD_PRELOAD=str(helper) + (":" + previous_preload if previous_preload else ""))
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
                               env=environment, capture_output=True, text=True)
        if child.stderr:
            sys.stderr.write(child.stderr)
        if child.returncode:
            raise SystemExit(child.returncode)
        result = json.loads(child.stdout)
        if not report.exists():
            raise RuntimeError("The sync helper did not report; no measured result is available")
        syncs = json.loads(report.read_text())
        if args.shared_quota and not sum(syncs["by_kind"]["quota"][key] for key in ("fsync", "fdatasync")):
            raise RuntimeError("The shared quota's sync calls were not intercepted")
        if any(value["errors"] for value in syncs["by_kind"].values()):
            raise RuntimeError("A measured fsync/fdatasync failed")
        result["sqlite_sync_syscalls"] = syncs
        print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--addresses", type=int, default=512)
    parser.add_argument("--cached-addresses", type=int, default=20000)
    parser.add_argument("--requests-per-second", type=float, default=49)
    parser.add_argument("--adaptive", action="store_true",
                        help="Use Enterprise auto pacing instead of a fixed request target")
    parser.add_argument("--provider-rps", type=int, default=0,
                        help="Synthetic provider rolling-second allowance; 0 means unlimited")
    parser.add_argument("--shared-quota", action="store_true",
                        help="Include production SQLite request admission in an isolated temporary directory")
    parser.add_argument("--network-delay-ms", type=float, default=40)
    parser.add_argument("--evidence-delay-ms", type=float, default=0,
                        help="Legacy delay per evidence-write method; use commit delay to model durable storage")
    parser.add_argument("--evidence-commit-delay-ms", type=float, default=0,
                        help="Additional latency per actual evidence COMMIT")
    parser.add_argument("--quota-commit-delay-ms", type=float, default=0,
                        help="Additional latency per actual shared-quota COMMIT")
    parser.add_argument("--measure-sync", action="store_true",
                        help="Count/time actual fsync/fdatasync calls; optional Linux mode requiring a C compiler")
    parser.add_argument("--evidence-sync-delay-ms", type=float, default=0,
                        help="Linux helper: additional latency per evidence file sync")
    parser.add_argument("--quota-sync-delay-ms", type=float, default=0,
                        help="Linux helper: additional latency per quota file/directory sync")
    args = parser.parse_args()
    if not 1 <= args.addresses <= 100000 or not 0 <= args.cached_addresses <= 1000000:
        parser.error("Choose 1–100000 fetched addresses and 0–1000000 cached addresses")
    if not 0 < args.requests_per_second <= 100000 or any(
            not 0 <= value <= 1000 for value in (args.network_delay_ms, args.evidence_delay_ms,
                                               args.evidence_commit_delay_ms, args.quota_commit_delay_ms,
                                               args.evidence_sync_delay_ms, args.quota_sync_delay_ms)):
        parser.error("Rate/delay outside synthetic benchmark bounds")
    if not 0 <= args.provider_rps <= 100000:
        parser.error("Choose a synthetic provider allowance of 0–100000 requests per second")
    if ((args.measure_sync or args.evidence_sync_delay_ms or args.quota_sync_delay_ms)
            and os.environ.get("LIQUID_BENCH_SYNC_ACTIVE") != "1"):
        return measured_sync_run(args, parser)
    sys.path.insert(0, str(args.repo.resolve()))
    counts_module = importlib.import_module("liquid_tracer.address_counts")
    from liquid_tracer.api import ENTERPRISE, Esplora
    from liquid_tracer.common import canonical, digest, save_json
    from liquid_tracer.explorer_quota import SharedExplorerQuota
    from liquid_tracer.investigations import create_investigation, read_case
    from liquid_tracer.store import Store

    source = ENTERPRISE if args.adaptive else "https://example.invalid/liquid/api"
    cached = [f"SYNTHETIC-cached-{index:07d}" for index in range(args.cached_addresses)]
    fresh = [f"SYNTHETIC-new-{index:07d}" for index in range(args.addresses)]
    requests, active, peak_active, peak_retained = [], 0, 0, 0
    admitted, successful_responses, rate_samples = deque(), [], []
    writes, json_bytes, save_seconds = 0, 0, 0.
    quota_reservations, quota_admitted, quota_reserve_seconds = 0, 0, 0.
    quota_clients = []
    commits = {kind: {"count": 0, "seconds": 0., "injected_wait_seconds": 0.}
               for kind in ("evidence", "quota")}
    lock = threading.Lock()

    def progress(event):
        target = event.get("api_target_rps")
        if target is not None and (not rate_samples or target != rate_samples[-1]["target_rps"]):
            rate_samples.append({"seconds": round(time.monotonic() - started, 3),
                                 "target_rps": target, "fetched": event.get("fetched", 0),
                                 "workers": event.get("worker_count", 0)})

    def transport(method, url, headers, body, timeout):
        nonlocal active, peak_active
        if method != "GET" or not url.startswith(source + "/address/"):
            raise AssertionError("Unexpected synthetic request")
        address = url.removeprefix(source + "/address/")
        with lock:
            timestamp = time.monotonic()
            while admitted and admitted[0] <= timestamp - 1:
                admitted.popleft()
            status = 429 if args.provider_rps and len(admitted) >= args.provider_rps else 200
            if status == 200:
                admitted.append(timestamp)
            requests.append((address, status))
            active += 1
            peak_active = max(peak_active, active)
        try:
            time.sleep(args.network_delay_ms / 1000)
            if status == 429:
                return 429, {"Retry-After": "1"}, canonical({"error": "Synthetic provider rate limit"})
            with lock:
                successful_responses.append(time.monotonic())
            return 200, {}, canonical({"address": address,
                "chain_stats": {"tx_count": 19}, "mempool_stats": {"tx_count": 2}})
        finally:
            with lock:
                active -= 1

    original_submit = Esplora.submit
    original_init = Esplora.__init__
    original_connect = sqlite3.connect

    class MeasuredConnection(sqlite3.Connection):
        benchmark_kind = None

        def _measure_commit(self, operation, *values):
            begun = time.monotonic()
            try:
                return operation(*values)
            finally:
                if self.benchmark_kind:
                    with lock:
                        commits[self.benchmark_kind]["seconds"] += time.monotonic() - begun

        def commit(self):
            if self.in_transaction:
                return self._measure_commit(super().commit)
            return super().commit()

        def __exit__(self, *values):
            if values[0] is None and self.in_transaction:
                return self._measure_commit(super().__exit__, *values)
            return super().__exit__(*values)

        def execute(self, sql, *values):
            if sql.strip().upper().rstrip(";") in ("COMMIT", "END"):
                return self._measure_commit(super().execute, sql, *values)
            return super().execute(sql, *values)

    def connect(database, *values, **options):
        path = Path(database)
        kind = "evidence" if path.name == "evidence.sqlite" else "quota" if path.parent.name == "quota" else None
        if not kind:
            return original_connect(database, *values, **options)
        options["factory"] = MeasuredConnection
        connection = original_connect(database, *values, **options)
        connection.benchmark_kind = kind
        delay = getattr(args, kind + "_commit_delay_ms") / 1000

        def trace_statement(sql):
            if sql.strip().upper().rstrip(";") != "COMMIT":
                return
            begun = time.monotonic()
            if delay:
                time.sleep(delay)
            with lock:
                commits[kind]["count"] += 1
                if delay:
                    commits[kind]["injected_wait_seconds"] += time.monotonic() - begun

        connection.set_trace_callback(trace_statement)
        return connection

    class BenchmarkSharedQuota(SharedExplorerQuota):
        def reserve(self):
            nonlocal quota_reservations, quota_admitted, quota_reserve_seconds
            begun = time.monotonic()
            admission = super().reserve()
            with lock:
                quota_reservations += 1
                quota_admitted += int(admission.admitted)
                quota_reserve_seconds += time.monotonic() - begun
            return admission

    def initialize(client, *values, **options):
        original_init(client, *values, **options)
        if args.shared_quota:
            if client._shared_quota is not None:
                client._shared_quota.close()
            client._shared_quota = BenchmarkSharedQuota(
                client.base, client.min_interval, directory=Path(directory) / "quota",
                adaptive=client.api_rate_mode == "adaptive")
            quota_clients.append(client._shared_quota)

    def submit(client, *values, **options):
        nonlocal peak_retained
        result = original_submit(client, *values, **options)
        peak_retained = max(peak_retained, len(client._results))
        return result

    def write(path, data):
        nonlocal writes, json_bytes, save_seconds
        started = time.monotonic()
        save_json(path, data)
        save_seconds += time.monotonic() - started
        writes += 1
        json_bytes += Path(path).stat().st_size

    def delayed(original):
        def wrapped(store, *values, **options):
            with store._lock:
                if args.evidence_delay_ms:
                    time.sleep(args.evidence_delay_ms / 1000)
                return original(store, *values, **options)
        return wrapped

    with tempfile.TemporaryDirectory(prefix="liquid-count-benchmark-") as directory:
        case = create_investigation(Path(directory), "Synthetic count benchmark")
        state = {"case_id": read_case(case)["case_id"], "run_id": "0123456789abcdef",
                 "source": source, "transactions": {},
                 "fetch_options": {"workers": 8}}
        if not args.adaptive:
            state["fetch_options"]["advertised_rps"] = args.requests_per_second / .95
        records = {address: {"address": address, "source": source,
            "observed_at": "2026-01-01T00:00:00+00:00", "confirmed_tx_count": 19,
            "mempool_tx_count": 2, "observation_ids": []} for address in cached}
        data = {"schema_version": 1, "case_id": state["case_id"], "source": source, "counts": records}
        data["sha256"] = digest(canonical(data))
        save_json(case / "address-counts.json", data)
        with patch.dict(os.environ, {"LIQUID_COUNT_WORKERS": "auto",
                                      "LIQUID_BLOCKSTREAM_API_RPS": "",
                                      "LIQUID_BLOCKSTREAM_ENTERPRISE_RPS": "auto"}), \
             patch("liquid_tracer.count_concurrency._available_bytes", return_value=8 * 1024 ** 3), \
             patch("liquid_tracer.count_concurrency._available_cpu_count", return_value=8), \
             patch.object(counts_module, "save_json", side_effect=write), \
             patch.object(Esplora, "__init__", initialize), \
             patch.object(Esplora, "submit", submit), \
             patch.object(Esplora, "bearer", return_value="synthetic-not-a-credential"), \
             patch.object(sqlite3, "connect", connect), \
             patch.object(Store, "attempt", delayed(Store.attempt)), \
             (patch.object(Store, "record_response", delayed(Store.record_response))
              if hasattr(Store, "record_response") else nullcontext()), \
             patch.object(Store, "observe", delayed(Store.observe)):
            started = time.monotonic()
            report = counts_module._collect_counts(case, state, cached + fresh,
                max_requests=0, max_seconds=0, transport=transport, progress=progress)
            elapsed = time.monotonic() - started
        saved = counts_module.apply_saved_counts(case, state)
        normalized = sorted((key, value["confirmed_tx_count"], value["mempool_tx_count"])
                            for key, value in saved.items())
        with sqlite3.connect(case / "evidence.sqlite") as database:
            evidence = database.execute("SELECT endpoint, sha256 FROM observations WHERE status=200 ORDER BY endpoint").fetchall()
            all_evidence = database.execute("SELECT endpoint, status, sha256, body FROM observations ORDER BY id").fetchall()
            attempts = database.execute("SELECT kind, endpoint, status FROM attempts ORDER BY kind, endpoint, status").fetchall()
        successful_requests = [address for address, status in requests if status == 200]
        if (report["fetched"] != len(fresh) or report["remaining"] or report["known"] != len(cached) + len(fresh)
                or sorted(successful_requests) != sorted(fresh) or len(evidence) != len(fresh)):
            raise AssertionError("Count coverage or request uniqueness changed")
        if (Counter(("/address/" + address, status) for address, status in requests)
                != Counter((endpoint, status) for endpoint, status, _, _ in all_evidence)
                or any(digest(body) != checksum for _, _, checksum, body in all_evidence)):
            raise AssertionError("A response is missing, duplicated, or corrupted in evidence")
        if args.adaptive and report.get("api_rate_mode") != "adaptive":
            raise AssertionError("The selected checkout did not use adaptive pacing")
        if args.shared_quota and quota_admitted != len(requests):
            raise AssertionError("Shared admission did not account for every HTTP attempt")
        expected_attempts = Counter(("esplora", "/address/" + address, label)
                                    for address, status in requests for label in ("started", str(status)))
        if Counter(attempts) != expected_attempts:
            raise AssertionError("An HTTP attempt start or outcome was lost, duplicated, or changed")
        rolling = deque()
        peak_successful_rps = 0
        for timestamp in sorted(successful_responses):
            while rolling and rolling[0] <= timestamp - 1:
                rolling.popleft()
            rolling.append(timestamp)
            peak_successful_rps = max(peak_successful_rps, len(rolling))
        print(json.dumps({"synthetic_only": True, "seconds": round(elapsed, 6),
            "addresses_fetched": len(fresh), "addresses_already_cached": len(cached),
            "configured_requests_per_second": None if args.adaptive else args.requests_per_second,
            "adaptive": args.adaptive, "synthetic_provider_rps": args.provider_rps or None,
            "quota_coordinator": "shared_sqlite" if args.shared_quota else "local",
            "sqlite_runtime_version": sqlite3.sqlite_version,
            "quota_storage": next((quota.storage_metrics() for quota in quota_clients
                                    if hasattr(quota, "storage_metrics")), None),
            "quota_reservations": quota_reservations, "quota_admitted": quota_admitted,
            "quota_denied_reservations": quota_reservations - quota_admitted,
            "quota_reserve_seconds": round(quota_reserve_seconds, 6),
            "http_requests": len(requests), "http_429_responses": sum(status == 429 for _, status in requests),
            "successful_evidence_observations": len(evidence), "all_evidence_observations": len(all_evidence),
            "peak_successful_responses_in_rolling_second": peak_successful_rps,
            "rate_target_changes": rate_samples,
            "network_delay_ms": args.network_delay_ms, "evidence_delay_ms": args.evidence_delay_ms,
            "evidence_commit_delay_ms": args.evidence_commit_delay_ms,
            "quota_commit_delay_ms": args.quota_commit_delay_ms,
            "evidence_sync_delay_ms": args.evidence_sync_delay_ms,
            "quota_sync_delay_ms": args.quota_sync_delay_ms,
            "sqlite_commits": {kind: {key: round(value, 6) if isinstance(value, float) else value
                                      for key, value in metrics.items()} for kind, metrics in commits.items()},
            "lookup_report": report, "peak_active_transport_requests": peak_active,
            "peak_retained_endpoint_futures": peak_retained, "full_json_cache_writes": writes,
            "full_json_cache_bytes_written": json_bytes, "full_json_save_seconds": round(save_seconds, 6),
            "counts_sha256": hashlib.sha256(canonical(normalized)).hexdigest(),
            "evidence_sha256": hashlib.sha256(canonical(evidence)).hexdigest(),
            "http_attempt_rows": len(attempts),
            "attempts_sha256": hashlib.sha256(canonical(attempts)).hexdigest(),
            "notice": "Synthetic provider; real evidence/cache writes and pacing. Not a live throughput forecast."
        }, indent=2))


if __name__ == "__main__":
    main()
