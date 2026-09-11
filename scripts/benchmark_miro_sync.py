#!/usr/bin/env python3
"""Measure Miro reorganization against an in-memory, latency-injected board.

No network calls, API credentials, third-party packages, or case data are used.
Run from any directory; --source-root can select an older checkout for comparison.
This isolates sync I/O and durable state writes, not ELK calculation or TLS setup.
"""

import argparse
import copy
import hashlib
import inspect
import json
import math
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def checksum(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def synthetic_graph(transactions):
    """Fixed valid geometry and ports avoid measuring the layout engine itself."""
    nodes, edges = [], []
    for index in range(transactions * 2 + 1):
        is_transaction = index % 2 == 1
        key = f"tx:{index // 2}" if is_transaction else f"addr:{index // 2}"
        nodes.append({
            "id": key,
            "label": f"Synthetic {'transaction' if is_transaction else 'address'} {index // 2}",
            "kind": "transaction" if is_transaction else "address",
            "color": "#93c5fd" if is_transaction else "#facc15",
            "x": index * 360, "y": 120, "width": 160, "height": 160,
        })
        if index:
            edges.append({
                "id": f"edge:{index}", "source": nodes[-2]["id"], "target": key,
                "label": "vin 0" if is_transaction else "vout 0", "quantity": "amount ??; asset ??",
                "role": "traced_input" if is_transaction else "candidate_output",
                "connector_shape": "straight",
                "attachment": {
                    "startItem": {"position": {"x": "100%", "y": "50%"}},
                    "endItem": {"position": {"x": "0%", "y": "50%"}},
                },
            })
    return {
        "namespace": {"case_id": "synthetic-benchmark", "source": "fixture:benchmark", "address_mode": "merged"},
        "run_id": "synthetic-run", "simulated": True,
        "notice": "Synthetic performance benchmark; no investigation data.",
        "nodes": nodes, "edges": edges,
        "run": {"parent_run": None, "ancestor_runs": [], "limits": {"max_hops": transactions}},
        "connector_attachment": "transaction_ports_v2",
        "layout": {"algorithm": "elk_layered_v1"},
        "graph_options": {"connector_style": "straight"},
        "presentation_version": 6,
    }


class SyntheticMiro:
    """Thread-safe transport with a fixed delay outside its item-store lock."""

    def __init__(self):
        self.items = {}
        self.latency = 0.
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.active_by_method = Counter()
        self.max_by_method = Counter()
        self._lock = threading.Lock()

    def __call__(self, method, url, headers, body, timeout):
        # This callable is the entire transport; it never delegates to HTTP.
        prefix = "https://api.miro.com/v2/boards/synthetic-benchmark/"
        if not url.startswith(prefix):
            raise AssertionError("Benchmark received an unexpected synthetic endpoint")
        path = url[len(prefix):]
        payload = json.loads(body) if body is not None else None
        started = time.perf_counter()
        with self._lock:
            self.active += 1
            self.active_by_method[method] += 1
            self.max_active = max(self.max_active, self.active)
            self.max_by_method[method] = max(self.max_by_method[method], self.active_by_method[method])
        try:
            if self.latency:
                time.sleep(self.latency)
            with self._lock:
                if method == "POST":
                    item_id = f"synthetic-{len(self.items) + 1:04d}"
                    result = copy.deepcopy(payload)
                    result.update(id=item_id, type="shape" if path == "shapes" else "connector")
                    self.items[item_id] = result
                    status = 201
                else:
                    item_id = path.rsplit("/", 1)[-1]
                    if item_id not in self.items:
                        raise AssertionError("Benchmark requested an unknown synthetic item")
                    result = self.items[item_id]
                    if method == "PATCH":
                        for key, value in payload.items():
                            if isinstance(value, dict):
                                result.setdefault(key, {}).update(copy.deepcopy(value))
                            else:
                                result[key] = copy.deepcopy(value)
                    elif method != "GET":
                        raise AssertionError("Benchmark expected only GET, PATCH, or setup POST")
                    status = 200
                response = encoded(result)
                self.calls.append({
                    "method": method, "path": path, "body": payload,
                    "started": started, "finished": time.perf_counter(),
                })
            return status, {}, response
        finally:
            with self._lock:
                self.active -= 1
                self.active_by_method[method] -= 1

    def begin_measurement(self, latency):
        for index, item in enumerate(self.items.values()):
            if item["type"] == "shape":
                item["position"]["x"] += 123 + index
                item["position"]["y"] += 83 + index
        self.calls.clear()
        self.max_active = 0
        self.max_by_method.clear()
        self.latency = latency


def measure(sync, make_plan, args, workers, supports_workers):
    board = SyntheticMiro()
    plan = make_plan(synthetic_graph(args.transactions))
    item_count = len(plan["shapes"]) + len(plan["connectors"])
    settings = {"token": "synthetic-unused-token", "transport": board, "max_items": item_count}
    if supports_workers:
        settings["workers"] = workers
    phases = defaultdict(float)
    current_phase, phase_started = None, None

    def progress(event):
        nonlocal current_phase, phase_started
        phase = event["phase"]
        if phase != current_phase:
            current = time.perf_counter()
            if current_phase is not None:
                phases[current_phase] += current - phase_started
            current_phase, phase_started = phase, current

    with tempfile.TemporaryDirectory(prefix="liquid-miro-benchmark-") as temporary:
        state_path = Path(temporary) / "miro.json"
        # Setup is excluded from timing and has neither pacing nor fake latency.
        sync(plan, "synthetic-benchmark", state_path, interval=0, **settings)
        board.begin_measurement(args.latency)
        if args.interval is not None:
            settings["interval"] = args.interval
        started = time.perf_counter()
        report = sync(plan, "synthetic-benchmark", state_path, reorganize=True, progress=progress, **settings)
        elapsed = time.perf_counter() - started
        if current_phase is not None:
            phases[current_phase] += time.perf_counter() - phase_started
        saved = json.loads(state_path.read_text())
        if saved.get("pending") or saved.get("pending_updates") or saved.get("active_run_id"):
            raise AssertionError("Benchmark finished with incomplete publication state")

    counts = Counter(call["method"] for call in board.calls)
    expected = {"GET": item_count, "PATCH": item_count}
    if dict(counts) != expected or report["created"] != 0 or report["updated"] != item_count:
        raise AssertionError(f"Benchmark did not perform the expected full reorganization: {dict(counts)}")
    reads = [call for call in board.calls if call["method"] == "GET"]
    edits = [call for call in board.calls if call["method"] == "PATCH"]
    if max(call["finished"] for call in reads) > min(call["started"] for call in edits):
        raise AssertionError("A board write started before complete remote preflight")
    requests = sorted(
        ({key: call[key] for key in ("method", "path", "body")} for call in board.calls),
        key=lambda call: (call["method"], call["path"]),
    )
    return {
        "workers": workers, "seconds": round(elapsed, 4), "native_items": item_count,
        "request_counts": dict(sorted(counts.items())),
        "phase_seconds": {key: round(value, 4) for key, value in phases.items() if value >= .0001},
        "max_inflight": board.max_active, "max_inflight_by_method": dict(board.max_by_method),
        "board_sha256": checksum(board.items), "requests_sha256": checksum(requests),
        "complete_preflight_before_writes": True, "durable_state_complete": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", nargs="+", type=int, choices=(1, 2, 3, 4),
                        help="Worker counts to compare; default: 1 4, or 1 for an older serial implementation")
    parser.add_argument("--latency", type=float, default=.15, help="Synthetic latency in seconds per request (default: .15)")
    parser.add_argument("--interval", type=float, help="Override request pacing; omitted uses the selected source version's default")
    parser.add_argument("--transactions", type=int, default=12, help="Synthetic transactions, 8–100 (default: 12, giving 51 items)")
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="Repository source to import, useful for comparing an older checkout")
    args = parser.parse_args()
    if not math.isfinite(args.latency) or not 0 <= args.latency <= 5:
        parser.error("--latency must be a finite number between 0 and 5")
    if args.interval is not None and (not math.isfinite(args.interval) or not 0 <= args.interval <= 5):
        parser.error("--interval must be a finite number between 0 and 5")
    if not 8 <= args.transactions <= 100:
        parser.error("--transactions must be between 8 and 100")
    source_root = args.source_root.resolve()
    if not (source_root / "liquid_tracer" / "miro.py").is_file():
        parser.error("--source-root must contain liquid_tracer/miro.py")
    sys.path.insert(0, str(source_root))
    from liquid_tracer.miro import make_plan, sync

    signature = inspect.signature(sync)
    supports_workers = "workers" in signature.parameters
    workers = args.workers or ([1, 4] if supports_workers else [1])
    if not supports_workers and any(count != 1 for count in workers):
        parser.error("The selected source supports serial sync only; use --workers 1")
    results = [measure(sync, make_plan, args, count, supports_workers) for count in workers]
    identical = all(
        result[field] == results[0][field]
        for result in results for field in ("board_sha256", "requests_sha256", "request_counts")
    )
    if not identical:
        raise AssertionError("Worker configurations produced different boards or requests")
    output = {
        "synthetic_only": True, "transactions": args.transactions, "latency_seconds": args.latency,
        "write_interval_seconds": args.interval if args.interval is not None else signature.parameters["interval"].default,
        "identical_results": identical, "results": results,
    }
    if len(results) > 1:
        output["first_to_last_speedup"] = round(results[0]["seconds"] / results[-1]["seconds"], 3)
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
