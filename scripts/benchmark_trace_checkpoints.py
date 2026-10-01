#!/usr/bin/env python3
"""Compare actual trace snapshot code with a growing synthetic UTXO tree.

No network, credentials, or investigation data are used. Both versions run
serially against the same in-memory API, keeping real JSON writes and fsync.
This isolates trace snapshot overhead; it does not measure API/evidence-store
throughput or predict a live collection run's duration.

Example: python scripts/benchmark_trace_checkpoints.py --transactions 8192 --baseline-ref HEAD
"""

import argparse
import hashlib
import importlib
import json
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def checksum(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def synthetic_tree(count):
    def txid(index):
        return hashlib.sha256(f"SYNTHETIC-checkpoint-{index}".encode()).hexdigest()

    def output(index, vout):
        return {"scriptpubkey": "0014" + "ab" * 20, "scriptpubkey_type": "v0_p2wpkh",
                "scriptpubkey_address": f"SYNTHETIC-address-{index}-{vout}",
                "valuecommitment": "08" + "bc" * 32, "assetcommitment": "0a" + "cd" * 32}

    confirmed = {"confirmed": True, "block_height": 12345, "block_time": 1700000000}
    data = {}
    for index in range(count):
        parent, vout = (index - 1) // 2, (index - 1) % 2
        inputs = ([{"txid": txid(parent), "vout": vout, "prevout": output(parent, vout)}]
                  if index else [])
        data["/tx/" + txid(index)] = {
            "txid": txid(index), "vin": inputs,
            "vout": [output(index, 0), output(index, 1)], "status": dict(confirmed)}
        data["/tx/" + txid(index) + "/outspends"] = [
            {"spent": True, "txid": txid(child), "vin": 0, "status": dict(confirmed)}
            if child < count else {"spent": False}
            for child in (2 * index + 1, 2 * index + 2)]
    return data, [txid(0) + ":0", txid(0) + ":1"]


class SyntheticAPI:
    """Deterministic responses; no network or disk-based evidence cache."""

    workers = 1
    auth = "none"
    token = None
    base = "fixture:checkpoint-benchmark"

    def __init__(self, data, budget):
        self.data, self.budget = data, budget
        self.used, self.cache = set(), {}

    def get(self, endpoint):
        self.budget.check()
        if endpoint not in self.cache:
            self.budget.request()
            oid = hashlib.sha256(endpoint.encode()).hexdigest()
            self.cache[endpoint] = self.data[endpoint], oid
        result = self.cache[endpoint]
        self.used.add(result[1])
        return result


def topology(state):
    return {key: state[key] for key in (
        "seeds", "transactions", "outputs", "links", "observations", "status", "stop_reason", "errors")}


def measure(module, name, data, seeds, count):
    from liquid_tracer.api import Budget, Limits
    from liquid_tracer.common import read_json

    limits = Limits(max_hops=count.bit_length(), max_transactions=count + 1,
                    max_outpoints=count * 2 + 1, max_requests=count * 2 + 1, max_seconds=3600)
    api = SyntheticAPI(data, Budget(limits))
    state = module.new_state(seeds, api.base, limits, [])
    writes, write_bytes, write_seconds = 0, 0, 0.0
    original_save = module.save_json

    def save(path, value):
        nonlocal writes, write_bytes, write_seconds
        started = time.perf_counter()
        original_save(path, value)
        write_seconds += time.perf_counter() - started
        writes += 1
        write_bytes += Path(path).stat().st_size

    with tempfile.TemporaryDirectory(prefix="liquid-checkpoint-benchmark-") as temporary:
        path = Path(temporary) / "trace.json"
        module.save_json = save
        started = time.perf_counter()
        try:
            result = module.trace(api, state, limits, path)
        finally:
            elapsed = time.perf_counter() - started
            module.save_json = original_save
        saved = read_json(path)
    if topology(saved) != topology(result):
        raise AssertionError("Saved trace does not match the completed investigation")
    if (result["status"] != "bounded_complete" or len(result["transactions"]) != count
            or len(result["outputs"]) != count * 2 or len(result["links"]) != count - 1):
        raise AssertionError("Synthetic tree traversal was incomplete")
    return {
        "version": name, "seconds": round(elapsed, 6),
        "checkpoint_writes": writes, "checkpoint_json_bytes_written": write_bytes,
        "checkpoint_serialization_and_fsync_seconds": round(write_seconds, 6),
        "checkpoint_wall_fraction": round(write_seconds / elapsed, 6),
        "transactions": count, "outputs": len(result["outputs"]), "links": len(result["links"]),
        "synthetic_unique_api_requests": api.budget.requests,
        "topology_sha256": checksum(topology(result)), "saved_terminal_state_matches": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transactions", type=int, default=8192,
                        help="Synthetic transactions, 1 to 32768; each has two outputs (default: 8192)")
    parser.add_argument("--baseline-ref", default="HEAD",
                        help="Git revision whose trace and checkpoint code form the baseline (default: HEAD)")
    args = parser.parse_args()
    if not 1 <= args.transactions <= 32768:
        parser.error("--transactions must be between 1 and 32768")
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    revision = subprocess.run(
        ["git", "rev-parse", "--verify", "--end-of-options", args.baseline_ref + "^{commit}"],
        cwd=root, check=True, capture_output=True, text=True).stdout.strip()
    source = subprocess.run(["git", "show", revision + ":liquid_tracer/trace.py"],
                            cwd=root, check=True, capture_output=True, text=True).stdout
    current = importlib.import_module("liquid_tracer.trace")
    baseline = types.ModuleType("liquid_tracer._checkpoint_benchmark_baseline")
    baseline.__package__ = "liquid_tracer"
    baseline.__file__ = str(root / "liquid_tracer" / "trace.py")
    exec(compile(source, baseline.__file__, "exec"), baseline.__dict__)
    checkpoint_source = None
    if hasattr(baseline, "TraceCheckpoint"):
        checkpoint_source = subprocess.run(
            ["git", "show", revision + ":liquid_tracer/trace_checkpoint.py"],
            cwd=root, check=True, capture_output=True, text=True).stdout
        checkpoint_module = types.ModuleType("liquid_tracer._checkpoint_benchmark_policy")
        exec(compile(checkpoint_source, "baseline-trace-checkpoint.py", "exec"), checkpoint_module.__dict__)
        baseline.TraceCheckpoint = checkpoint_module.TraceCheckpoint
    data, seeds = synthetic_tree(args.transactions)
    results = [measure(baseline, "baseline", data, seeds, args.transactions),
               measure(current, "current", data, seeds, args.transactions)]
    identical = results[0]["topology_sha256"] == results[1]["topology_sha256"]
    if not identical:
        raise AssertionError("Baseline and current trace produced different evidence topology")
    print(json.dumps({
        "synthetic_only": True, "baseline_revision": revision,
        "baseline_trace_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "baseline_checkpoint_sha256": (hashlib.sha256(checkpoint_source.encode()).hexdigest()
                                       if checkpoint_source else None),
        "workers": 1, "identical_topology": identical, "results": results,
        "notice": "Zero-network trace snapshot benchmark with real JSON writes and fsync. "
                  "Raw-response evidence-store cost and API timing are excluded. "
                  "Baseline trace.py and its checkpoint policy come from the baseline revision; "
                  "other shared dependencies are current. No live speedup is implied.",
    }, indent=2))


if __name__ == "__main__":
    main()
