#!/usr/bin/env python3
"""Measure real count scheduling/cache persistence with a synthetic provider.

Run this same script with --repo pointing to baseline and current checkouts.
There are no network requests or credentials. Evidence writes, request pacing,
count-cache persistence and adaptive workers remain active. Compare counts and
response fingerprints before comparing timings; this is not a live forecast.
"""

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--addresses", type=int, default=512)
    parser.add_argument("--cached-addresses", type=int, default=20000)
    parser.add_argument("--requests-per-second", type=float, default=49)
    parser.add_argument("--network-delay-ms", type=float, default=40)
    parser.add_argument("--evidence-delay-ms", type=float, default=0)
    args = parser.parse_args()
    if not 1 <= args.addresses <= 100000 or not 0 <= args.cached_addresses <= 1000000:
        parser.error("Choose 1–100000 fetched addresses and 0–1000000 cached addresses")
    if not 0 < args.requests_per_second <= 100000 or any(
            not 0 <= value <= 1000 for value in (args.network_delay_ms, args.evidence_delay_ms)):
        parser.error("Rate/delay outside synthetic benchmark bounds")
    sys.path.insert(0, str(args.repo.resolve()))
    counts_module = importlib.import_module("liquid_tracer.address_counts")
    from liquid_tracer.api import Esplora
    from liquid_tracer.common import canonical, digest, save_json
    from liquid_tracer.investigations import create_investigation, read_case
    from liquid_tracer.store import Store

    source = "https://example.invalid/liquid/api"
    cached = [f"SYNTHETIC-cached-{index:07d}" for index in range(args.cached_addresses)]
    fresh = [f"SYNTHETIC-new-{index:07d}" for index in range(args.addresses)]
    requests, active, peak_active, peak_retained = [], 0, 0, 0
    writes, json_bytes, save_seconds = 0, 0, 0.
    lock = threading.Lock()

    def transport(method, url, headers, body, timeout):
        nonlocal active, peak_active
        if method != "GET" or not url.startswith(source + "/address/"):
            raise AssertionError("Unexpected synthetic request")
        address = url.removeprefix(source + "/address/")
        with lock:
            requests.append(address)
            active += 1
            peak_active = max(peak_active, active)
        try:
            time.sleep(args.network_delay_ms / 1000)
            return 200, {}, canonical({"address": address,
                "chain_stats": {"tx_count": 19}, "mempool_stats": {"tx_count": 2}})
        finally:
            with lock:
                active -= 1

    original_submit = Esplora.submit

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
                 "fetch_options": {"workers": 8, "advertised_rps": args.requests_per_second / .95}}
        records = {address: {"address": address, "source": source,
            "observed_at": "2026-01-01T00:00:00+00:00", "confirmed_tx_count": 19,
            "mempool_tx_count": 2, "observation_ids": []} for address in cached}
        data = {"schema_version": 1, "case_id": state["case_id"], "source": source, "counts": records}
        data["sha256"] = digest(canonical(data))
        save_json(case / "address-counts.json", data)
        with patch.dict(os.environ, {"LIQUID_COUNT_WORKERS": "auto"}), \
             patch("liquid_tracer.count_concurrency._available_bytes", return_value=8 * 1024 ** 3), \
             patch("liquid_tracer.count_concurrency._available_cpu_count", return_value=8), \
             patch.object(counts_module, "save_json", side_effect=write), \
             patch.object(Esplora, "submit", submit), \
             patch.object(Store, "attempt", delayed(Store.attempt)), \
             patch.object(Store, "observe", delayed(Store.observe)):
            started = time.monotonic()
            report = counts_module._collect_counts(case, state, cached + fresh,
                max_requests=0, max_seconds=0, transport=transport)
            elapsed = time.monotonic() - started
        saved = counts_module.apply_saved_counts(case, state)
        normalized = sorted((key, value["confirmed_tx_count"], value["mempool_tx_count"])
                            for key, value in saved.items())
        with sqlite3.connect(case / "evidence.sqlite") as database:
            evidence = database.execute("SELECT endpoint, sha256 FROM observations ORDER BY endpoint").fetchall()
        if (report["fetched"] != len(fresh) or report["remaining"] or report["known"] != len(cached) + len(fresh)
                or sorted(requests) != sorted(fresh) or len(evidence) != len(fresh)):
            raise AssertionError("Count coverage or request uniqueness changed")
        print(json.dumps({"synthetic_only": True, "seconds": round(elapsed, 6),
            "addresses_fetched": len(fresh), "addresses_already_cached": len(cached),
            "configured_requests_per_second": args.requests_per_second,
            "network_delay_ms": args.network_delay_ms, "evidence_delay_ms": args.evidence_delay_ms,
            "lookup_report": report, "peak_active_transport_requests": peak_active,
            "peak_retained_endpoint_futures": peak_retained, "full_json_cache_writes": writes,
            "full_json_cache_bytes_written": json_bytes, "full_json_save_seconds": round(save_seconds, 6),
            "counts_sha256": hashlib.sha256(canonical(normalized)).hexdigest(),
            "evidence_sha256": hashlib.sha256(canonical(evidence)).hexdigest(),
            "notice": "Synthetic provider; real evidence/cache writes and pacing. Not a live throughput forecast."
        }, indent=2))


if __name__ == "__main__":
    main()
