#!/usr/bin/env python3
"""Measure INITIAL Miro publication using a synthetic, latency-injected board.

No network, API secrets, third-party packages, or investigation files are used.
Invoke this script separately for each --source-root to compare actual sync
implementations. The measured sync keeps its real on-disk checkpoints and fsync.
This is a transport/storage benchmark, not an estimate of live Miro throughput.
"""

import argparse
import copy
import hashlib
import inspect
import json
import math
import os
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def checksum(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def synthetic_graph(transactions):
    nodes, edges = [], []
    for index in range(transactions * 2 + 1):
        transaction = index % 2 == 1
        key = f"tx:{index // 2}" if transaction else f"addr:{index // 2}"
        nodes.append({
            "id": key, "label": f"Synthetic {'transaction' if transaction else 'address'} {index // 2}",
            "kind": "transaction" if transaction else "address",
            "color": "#93c5fd" if transaction else "#facc15",
            "x": index * 360, "y": 120, "width": 160, "height": 160,
        })
        if index:
            edges.append({
                "id": f"edge:{index}", "source": nodes[-2]["id"], "target": key,
                "label": "vin 0" if transaction else "vout 0", "quantity": "amount ??; asset ??",
                "role": "traced_input" if transaction else "candidate_output", "connector_shape": "straight",
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
        "connector_attachment": "transaction_ports_v2", "layout": {"algorithm": "elk_layered_v1"},
        "graph_options": {"connector_style": "straight"}, "presentation_version": 6,
    }


class SyntheticMiro:
    """Atomic bulk creation, unordered acknowledgments, and thread-safe reads.

    The same smooth, weighted 95,000-credit/minute synthetic admission gate
    applies to old and new sources. It models the production gate even for an
    older source that does not implement it. --unpaced-credit is available to
    isolate transport and storage; neither mode measures live Miro throughput.
    """

    prefix = "/v2/boards/synthetic-benchmark/"

    def __init__(self, latency=0., credits_per_minute=95000, pace_credits=True):
        self.items = {}
        self.latency = latency
        self.allowance = credits_per_minute
        self.remaining = credits_per_minute
        self.reset_at = time.time() + 60
        self.pace_credits = pace_credits
        self.next_admission = 0.
        self.calls = []
        self.counter = 0
        self.active = 0
        self.max_active = 0
        self.active_by_endpoint = Counter()
        self.max_by_endpoint = Counter()
        self.lock = threading.Lock()

    def _admit(self, credits):
        if not self.pace_credits:
            return
        # Waiting happens outside the lock. Reserving an admission before
        # sleeping serializes starts without serializing network latency.
        with self.lock:
            admitted_at = max(time.perf_counter(), self.next_admission)
            self.next_admission = admitted_at + credits * 60 / self.allowance
        delay = admitted_at - time.perf_counter()
        if delay > 0:
            time.sleep(delay)

    def _create(self, body, kind):
        self.counter += 1
        item_id = f"synthetic-{self.counter:08d}"
        item = copy.deepcopy(body)
        item.update(id=item_id, type=kind)
        self.items[item_id] = item
        return item

    def __call__(self, method, url, headers, body, timeout):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "api.miro.com" or not parsed.path.startswith(self.prefix):
            raise AssertionError("Unexpected endpoint; the benchmark never delegates to HTTP")
        path = parsed.path[len(self.prefix):]
        query = parse_qs(parsed.query)
        payload = json.loads(body) if body is not None else None
        if method == "POST" and path == "items/bulk":
            if not isinstance(payload, list) or not 1 <= len(payload) <= 20:
                raise AssertionError("Bulk shape request must contain 1 to 20 items")
            if any(item.get("type") != "shape" for item in payload):
                raise AssertionError("Only shapes may use bulk creation")
            cost = 100 * len(payload)
        else:
            cost = 50 if method == "GET" else 100
        endpoint = path.split("/")[0] if path != "items/bulk" else path
        self._admit(cost)
        started = time.perf_counter()
        with self.lock:
            self.active += 1
            self.active_by_endpoint[endpoint] += 1
            self.max_active = max(self.max_active, self.active)
            self.max_by_endpoint[endpoint] = max(self.max_by_endpoint[endpoint], self.active_by_endpoint[endpoint])
        try:
            if self.latency:
                time.sleep(self.latency)
            with self.lock:
                if time.time() >= self.reset_at:
                    self.remaining, self.reset_at = self.allowance, time.time() + 60
                response_headers = {"X-RateLimit-Reset": str(self.reset_at)}
                if self.remaining < cost:
                    response_headers.update({"Retry-After": str(max(1., self.reset_at - time.time())),
                                             "X-RateLimit-Remaining": str(self.remaining)})
                    status, result = 429, {}
                else:
                    self.remaining -= cost
                    response_headers["X-RateLimit-Remaining"] = str(self.remaining)
                    status, result = self._request(method, path, query, payload)
                self.calls.append({"method": method, "path": path, "body": payload, "status": status,
                                   "credits": cost, "started": started, "finished": time.perf_counter()})
                return status, response_headers, encoded(result)
        finally:
            with self.lock:
                self.active -= 1
                self.active_by_endpoint[endpoint] -= 1

    def _request(self, method, path, query, payload):
        if method == "POST":
            if path == "items/bulk":
                # The mapping must use stable request/response properties rather
                # than assuming a bulk acknowledgment follows request order.
                return 201, {"data": list(reversed([self._create(item, "shape") for item in payload]))}
            if path not in ("shapes", "connectors"):
                raise AssertionError("Unknown synthetic creation endpoint")
            if path == "connectors":
                for field in ("startItem", "endItem"):
                    if payload[field]["id"] not in self.items:
                        raise AssertionError("Connector created before its endpoint exists")
            return 201, self._create(payload, "shape" if path == "shapes" else "connector")
        if method == "GET" and path in ("items", "connectors"):
            kind = "connector" if path == "connectors" else query.get("type", [None])[0]
            items = [item for item in self.items.values()
                     if (item["type"] == kind if kind else item["type"] != "connector")]
            cursor = int(query.get("cursor", ["0"])[0])
            limit = int(query.get("limit", ["50"])[0])
            result = {"data": items[cursor:cursor + limit], "size": len(items[cursor:cursor + limit]),
                      "limit": limit, "total": len(items)}
            if cursor + limit < len(items):
                result["cursor"] = str(cursor + limit)
            return 200, result
        item_id = path.rsplit("/", 1)[-1]
        if item_id not in self.items:
            return 404, {}
        if method != "GET":
            raise AssertionError("Initial publication and unchanged retry must not modify existing items")
        return 200, self.items[item_id]


def process_write_characters():
    """Linux logical write bytes, including the actual journal/snapshot writes."""
    try:
        for line in Path("/proc/self/io").read_text().splitlines():
            if line.startswith("wchar:"):
                return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


def logical_value(value, reverse_ids):
    if isinstance(value, dict):
        return {key: (reverse_ids.get(item, item) if key == "id" and isinstance(item, str)
                      else logical_value(item, reverse_ids)) for key, item in value.items()}
    if isinstance(value, list):
        return [logical_value(item, reverse_ids) for item in value]
    return value


def normalized_board_and_mapping(board, saved):
    items = saved["items"]
    reverse = {record["id"]: key for key, record in items.items()}
    if len(reverse) != len(items) or set(reverse) != set(board.items):
        raise AssertionError("Saved mapping is incomplete or contains duplicate remote IDs")
    normalized_board = {reverse[item_id]: logical_value(item, reverse) for item_id, item in board.items.items()}
    normalized_mapping = logical_value(items, reverse)
    return normalized_board, normalized_mapping


def load_state(module, state_path, plan):
    # Load through the selected version's own recovery path, not just the JSON
    # snapshot. Current versions may have durable records in a sidecar journal.
    return module._load_sync_state(state_path, "synthetic-benchmark", plan["namespace"])


def measure(module, args, workers):
    board = SyntheticMiro(args.latency, pace_credits=not args.unpaced_credit)
    plan = module.make_plan(synthetic_graph(args.transactions))
    item_count = len(plan["shapes"]) + len(plan["connectors"])
    settings = {"token": "synthetic-unused-token", "transport": board,
                "max_items": item_count, "interval": args.interval, "workers": workers}
    phases = defaultdict(float)
    current_phase, phase_started = None, None

    def progress(event):
        nonlocal current_phase, phase_started
        if event["phase"] != current_phase:
            current = time.perf_counter()
            if current_phase is not None:
                phases[current_phase] += current - phase_started
            current_phase, phase_started = event["phase"], current

    with tempfile.TemporaryDirectory(prefix="liquid-miro-creation-") as temporary:
        state_path = Path(temporary) / "miro.json"
        fsync_calls = 0
        original_fsync = os.fsync

        def observed_fsync(fd):
            nonlocal fsync_calls
            fsync_calls += 1
            return original_fsync(fd)

        writes_before = process_write_characters()
        os.fsync = observed_fsync
        started = time.perf_counter()
        try:
            report = module.sync(plan, "synthetic-benchmark", state_path, progress=progress, **settings)
        finally:
            elapsed = time.perf_counter() - started
            os.fsync = original_fsync
        writes_after = process_write_characters()
        if current_phase is not None:
            phases[current_phase] += time.perf_counter() - phase_started
        saved = load_state(module, state_path, plan)
        if (saved.get("pending") or saved.get("pending_creates") or saved.get("pending_updates")
                or saved.get("active_run_id")):
            raise AssertionError("Publication finished with incomplete durable state")
        if report["created"] != item_count or len(saved["items"]) != item_count:
            raise AssertionError("Publication did not retain every planned native object")
        normalized_board, normalized_mapping = normalized_board_and_mapping(board, saved)
        creation_calls = list(board.calls)
        max_active, max_by_endpoint = board.max_active, dict(board.max_by_endpoint)
        storage_bytes = sum(path.stat().st_size for path in Path(temporary).iterdir() if path.is_file())

        # Excluded from timing: load persisted state into another invocation and
        # verify a retry reuses IDs, retains manual edits, and sends zero POSTs.
        manual_key = "addr:0"
        manual_id = saved["items"][manual_key]["id"]
        board.items[manual_id]["data"]["content"] = "<p>Manual investigator annotation</p>"
        manual_board = copy.deepcopy(board.items)
        board.calls.clear()
        board.latency = 0
        board.pace_credits = False
        # Treat validation as a later session with a fresh allowance. Its reads
        # are deliberately excluded from the creation-time comparison.
        board.remaining, board.reset_at = board.allowance, time.time() + 60
        settings["interval"] = 0
        repeated = module.sync(plan, "synthetic-benchmark", state_path, **settings)
        if repeated["created"] or board.items != manual_board or any(call["method"] != "GET" for call in board.calls):
            raise AssertionError("Retry duplicated items or failed to retain an investigator's manual edit")
        retried = load_state(module, state_path, plan)
        if {key: value["id"] for key, value in retried["items"].items()} != {
                key: value["id"] for key, value in saved["items"].items()}:
            raise AssertionError("Retry changed saved remote IDs")

    counts = Counter(f"{call['method']} {call['path']}" for call in creation_calls)
    posts = [call for call in creation_calls if call["method"] == "POST"]
    if len(posts) != len(creation_calls) or any(call["status"] != 201 for call in posts):
        raise AssertionError("Initial publication unexpectedly read, edited, or retried remote data")
    shapes = [call for call in posts if call["path"] in ("shapes", "items/bulk")]
    connectors = [call for call in posts if call["path"] == "connectors"]
    if connectors and max(call["finished"] for call in shapes) > min(call["started"] for call in connectors):
        raise AssertionError("A connector started before the complete shape stage was acknowledged")
    return {
        "workers": workers, "seconds": round(elapsed, 4), "native_items": item_count,
        "shapes": len(plan["shapes"]), "connectors": len(plan["connectors"]),
        "shape_post_requests": len(shapes), "connector_post_requests": len(connectors),
        "request_counts": dict(sorted(counts.items())),
        "phase_seconds": {key: round(value, 4) for key, value in phases.items() if value >= .0001},
        "max_inflight": max_active, "max_inflight_by_endpoint": max_by_endpoint,
        "checkpoint_process_write_bytes": None if writes_before is None or writes_after is None else writes_after - writes_before,
        "python_fsync_calls": fsync_calls, "retained_state_bytes": storage_bytes,
        "board_sha256": checksum(normalized_board), "mapping_sha256": checksum(normalized_mapping),
        "durable_state_complete": True, "retry_reuses_ids": True, "manual_edit_preserved": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", nargs="+", type=int, choices=(1, 2, 3, 4), default=[4])
    parser.add_argument("--latency", type=float, default=.05, help="Synthetic latency per request in seconds (default: .05)")
    parser.add_argument("--interval", type=float, default=.02,
                        help="Same explicit request pacing for each source; zero isolates transport and storage (default: .02)")
    parser.add_argument("--unpaced-credit", action="store_true",
                        help="Disable synthetic smooth credit pacing to isolate network/storage; not production throughput")
    parser.add_argument("--transactions", type=int, default=50,
                        help="Synthetic transactions, 1 to 200; native objects = 4*N+3 (default: 50)")
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    for name in ("latency", "interval"):
        value = getattr(args, name)
        if not math.isfinite(value) or not 0 <= value <= 5:
            parser.error(f"--{name} must be finite and between 0 and 5 seconds")
    if not 1 <= args.transactions <= 200:
        parser.error("--transactions must be between 1 and 200")
    source_root = args.source_root.resolve()
    if not (source_root / "liquid_tracer" / "miro.py").is_file():
        parser.error("--source-root must contain liquid_tracer/miro.py")
    sys.path.insert(0, str(source_root))
    from liquid_tracer import miro
    if "workers" not in inspect.signature(miro.sync).parameters:
        parser.error("Selected source does not support the worker option")
    results = [measure(miro, args, workers) for workers in args.workers]
    identical = all(result[field] == results[0][field]
                    for result in results for field in ("board_sha256", "mapping_sha256"))
    if not identical:
        raise AssertionError("Worker configurations produced different logical boards or mappings")
    print(json.dumps({
        "synthetic_only": True, "source_root": str(source_root), "transactions": args.transactions,
        "latency_seconds": args.latency, "request_interval_seconds": args.interval,
        "synthetic_credits_per_minute": 95000,
        "synthetic_credit_pacing": "none" if args.unpaced_credit else "smooth weighted admission",
        "production_shared_quota_measured": False,
        "checkpoint_write_metric": "Linux process wchar bytes during sync; null if /proc/self/io is unavailable",
        "fsync_metric": "Python fsync calls only; SQLite native sync calls are excluded, so this is not a durability comparison",
        "identical_results": identical, "results": results,
        "notice": "Initial-publication comparison with real durable storage and simulated Miro admission; not live Miro timings.",
    }, indent=2))


if __name__ == "__main__":
    main()
