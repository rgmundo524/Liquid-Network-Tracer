#!/usr/bin/env python3
"""Measure trace scheduling against synthetic responses with uneven latency.

No network, credentials, or investigation data are used. The real Esplora
client, evidence store, and checkpoints remain enabled. Run this same script
with --repo pointing at separate baseline and current checkouts, then compare
the topology and request hashes before comparing timings. This benchmark does
not simulate a live API quota or predict a live collection run's speedup.

Example: python scripts/benchmark_trace_pipeline.py --repo /tmp/baseline
"""

import argparse
import hashlib
import importlib
import json
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path


def digest(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1],
                        help="Checkout to import, including its tests (default: this checkout)")
    parser.add_argument("--branches", type=int, default=128,
                        help="Selected independent branches converging at hop two (default: 128)")
    parser.add_argument("--workers", type=int, default=8,
                        help="Fixed synthetic request concurrency, 1 to 8 (default: 8)")
    parser.add_argument("--delay-ms", type=float, default=5,
                        help="Typical response latency in milliseconds (default: 5)")
    parser.add_argument("--tail-delay-ms", type=float, default=150,
                        help="Additional latency for every Nth funding request (default: 150)")
    parser.add_argument("--tail-every", type=int, default=8,
                        help="Every Nth selected root has a slow funding response (default: 8)")
    args = parser.parse_args()
    if args.branches < 1 or not 1 <= args.workers <= 8 or args.tail_every < 1:
        parser.error("--branches and --tail-every must be positive; --workers must be 1 to 8")
    if not all(0 <= value < float("inf") for value in (args.delay_ms, args.tail_delay_ms)):
        parser.error("Response delays must be finite and non-negative")
    repo = args.repo.resolve()
    if not (repo / "liquid_tracer" / "trace.py").is_file():
        parser.error("--repo must contain a Liquid Tracer checkout")
    sys.path.insert(0, str(repo))
    harness = importlib.import_module("tests.test_trace_concurrency")
    if not Path(harness.__file__).resolve().is_relative_to(repo):
        raise RuntimeError("Synthetic harness was imported from a different checkout")
    data, roots, branches, joined, _ = harness.converging_fixture(args.branches)
    tails = {"/tx/" + txid for txid in roots[args.tail_every - 1::args.tail_every]}

    class UnevenTransport(harness.RecordingTransport):
        """Only the fixture transport runs; no external connection is opened."""

        def __init__(self, *values, **options):
            super().__init__(*values, **options)
            self.busy_seconds = 0.0

        def __call__(self, method, url, headers, body, timeout):
            if method != "GET" or not url.startswith(harness.ENTERPRISE + "/"):
                raise AssertionError("Unexpected synthetic request")
            endpoint = url.removeprefix(harness.ENTERPRISE)
            with self.lock:
                self.calls.append(endpoint)
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
            started = time.perf_counter()
            try:
                delay = args.delay_ms + (args.tail_delay_ms if endpoint in tails else 0)
                if delay:
                    time.sleep(delay / 1000)
                return 200, {}, harness.canonical(self.data[endpoint])
            finally:
                with self.lock:
                    self.busy_seconds += time.perf_counter() - started
                    self.active -= 1

    harness.RecordingTransport = UnevenTransport
    seeds = [txid + ":0" for txid in roots]
    with tempfile.TemporaryDirectory(prefix="liquid-pipeline-benchmark-") as temporary:
        started = time.perf_counter()
        state, transport = harness.synthetic_trace(temporary, data, seeds, workers=args.workers)
        elapsed = time.perf_counter() - started
    expected = Counter({"/tx/" + txid: 1 for txid in roots + branches + [joined]})
    expected.update({"/tx/" + txid + "/outspends": 1 for txid in roots + branches})
    requests = Counter(transport.calls)
    if requests != expected:
        raise AssertionError("Actual requests differ from the exact synthetic endpoint set")
    if (state["status"] != "bounded_complete" or len(state["transactions"]) != 2 * args.branches + 1
            or len(state["links"]) != 2 * args.branches or len(state["observations"]) != sum(expected.values())
            or state["stats"]["requests_this_run"] != sum(expected.values())):
        raise AssertionError("Trace did not preserve complete synthetic evidence")
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                              capture_output=True, text=True, check=False)
    print(json.dumps({
        "synthetic_only": True, "repository": str(repo),
        "git_head": revision.stdout.strip() if revision.returncode == 0 else None,
        "trace_source_sha256": hashlib.sha256((repo / "liquid_tracer" / "trace.py").read_bytes()).hexdigest(),
        "scheduler_source_sha256": hashlib.sha256((repo / "liquid_tracer" / "trace_fetch.py").read_bytes()).hexdigest(),
        "branches": args.branches, "requested_worker_ceiling": args.workers,
        "delay_ms": args.delay_ms, "tail_delay_ms": args.tail_delay_ms, "tail_every": args.tail_every,
        "seconds": round(elapsed, 6), "requests": len(transport.calls),
        "requests_per_second": round(len(transport.calls) / elapsed, 3),
        "transport_peak_concurrency": transport.maximum_active,
        "mean_active_transport_requests": round(transport.busy_seconds / elapsed, 3),
        "transactions": len(state["transactions"]), "links": len(state["links"]),
        "observations": len(state["observations"]),
        "normalized_topology_sha256": digest(harness.evidence_topology(state)),
        "request_multiset_sha256": digest(sorted(requests.items())),
        "exact_expected_requests": True, "performance": state.get("performance", {}),
        "notice": "Injected synthetic responses, uneven latency, and effectively unpaced requests. "
                  "Real evidence writes and checkpoints are enabled. Compare both hashes before "
                  "timings. This does not predict live throughput or change any API quota.",
    }, indent=2))


if __name__ == "__main__":
    main()
