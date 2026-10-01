import copy
import heapq
import math
import platform
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .common import (HEX64, StopRun, TraceError, digest, match_labels, now,
                     output_kind, parse_outpoint, save_json)
from .services import ServiceScope, is_service_stop
from .progress import report_progress
from .trace_checkpoint import TraceCheckpoint
from .trace_fetch import FrontierFetcher, TraceConcurrency

TERMINAL = {"spent", "fee", "pegout", "provably_unspendable"}
COLLECTION_POLICY = {"schema_version": 1, "attribution_hop_limits": "ignore", "stop_tracing": "respect"}


def new_state(seeds, source, limits, labels, parent=None, case_id=None):
    state = copy.deepcopy(parent) if parent else {
        "schema_version": 1, "seeds": sorted(set(seeds)), "transactions": {},
        "outputs": {}, "links": {}, "observations": [], "source": source,
        "method": "Forward UTXO reachability; no value allocation or ownership inference",
    }
    if state["source"] != source:
        raise TraceError("Continuation must use the same API source (or identical fixture)")
    if case_id and state.get("case_id") and state["case_id"] != case_id:
        raise TraceError("The resumed run belongs to a different case")
    state["case_id"] = case_id or state.get("case_id") or uuid.uuid4().hex
    state.update({"run_id": uuid.uuid4().hex[:16], "parent_run": parent["run_id"] if parent else None,
                  "ancestor_runs": parent.get("ancestor_runs", []) + [parent["run_id"]] if parent else [],
                  "started_at": now(), "finished_at": None, "limits": asdict(limits),
                  "labels": labels, "status": "running", "errors": [], "stats": {},
                  "performance": {}, "collection_policy": dict(COLLECTION_POLICY)})
    state.setdefault("root_run_id", state["run_id"])
    files = sorted(Path(__file__).parent.glob("*.py"))
    state["software"] = {"version": __version__, "python": platform.python_version(),
        "source_sha256": digest(b"".join(p.name.encode() + b"\0" + p.read_bytes() + b"\0" for p in files))}
    return state


def validate_transaction(data, txid):
    if not isinstance(data, dict) or data.get("txid") != txid:
        raise TraceError("Explorer transaction ID mismatch")
    if not isinstance(data.get("vin"), list) or not isinstance(data.get("vout"), list):
        raise TraceError("Malformed transaction inputs/outputs")
    if any(not isinstance(v, dict) for v in data["vin"] + data["vout"]):
        raise TraceError("Malformed input/output record")
    if not isinstance(data.get("status"), dict) or type(data["status"].get("confirmed")) is not bool:
        raise TraceError("Malformed transaction confirmation status")
    for output in data["vout"]:
        if not isinstance(output.get("scriptpubkey"), str):
            raise TraceError("Missing or malformed output script")
        if output.get("value") is not None and (type(output["value"]) is not int or output["value"] < 0):
            raise TraceError("Malformed explicit output value")


def trace(api, state, limits, checkpoint, include_unconfirmed=False, only=None, *, progress=None):
    """Hop 0 = selected seed output; one hop = its spending transaction.

    Every spendable child output is a candidate. Other inputs are context, never
    traversed backward, clustered, or used as address-history expansion seeds.
    Attribution hop limits belong to plotting; collection respects explicit
    stops, the run's global hop ceiling and its request/resource budgets.
    """
    limits.validate()
    started = time.monotonic()
    fetch_wait_seconds = 0.
    checkpoint_writer = TraceCheckpoint()
    # Timings describe this collection only, never the resumed parent run.
    state["performance"] = {}
    workers = getattr(api, "workers", 1)
    worker_ceiling = getattr(api, "worker_ceiling", workers)
    adaptive = worker_ceiling > workers
    policy = (TraceConcurrency(workers, limits.max_outpoints,
                               getattr(api, "effective_rps", 0.) if not getattr(api, "fixture", None) else 0.)
              if adaptive else None)
    target_workers = policy.target({}) if policy is not None else workers
    last_progress_at = started

    def metrics():
        callback = getattr(api, "request_metrics", None)
        return callback() if callable(callback) else {}

    def concurrency():
        nonlocal target_workers
        target_workers = min(worker_ceiling, policy.target(metrics())) if policy is not None else workers
        return target_workers

    def performance():
        elapsed = max(0., time.monotonic() - started)
        result = {"schema_version": 1, "tracing_seconds": elapsed, "fetch_wait_seconds": fetch_wait_seconds,
                  "checkpoint_seconds": checkpoint_writer.write_seconds,
                  "checkpoint_count": checkpoint_writer.writes,
                  "processing_seconds": max(0., elapsed - fetch_wait_seconds - checkpoint_writer.write_seconds),
                  "request_count": api.budget.requests,
                  "worker_peak": policy.peak if policy is not None else workers,
                  "worker_limit": policy.ceiling if policy is not None else workers}
        result.update(metrics())
        # Optional API snapshots can include descriptive shared-wait reasons;
        # only numeric measurements belong to the saved performance table.
        state["performance"] = {key: value for key, value in result.items()
                                if type(value) in (int, float) and value >= 0 and math.isfinite(value)}

    def get(endpoint):
        nonlocal fetch_wait_seconds
        waiting = time.monotonic()
        try:
            return api.get(endpoint)
        finally:
            fetch_wait_seconds += max(0., time.monotonic() - waiting)

    queue = []
    count = 0
    new_transactions = 0
    labels = state["labels"]
    state["include_unconfirmed"] = include_unconfirmed
    state["collection_policy"] = dict(COLLECTION_POLICY)
    state["selected_frontier"] = sorted(only) if only is not None else None
    from .hop_limits import HopScope, has_hop_limits
    from .group_hops import reference_name, refresh_reference_hops, refresh_seed_depths
    named_hops = bool(reference_name(state))
    hop_progress = {"hop_reference_name": reference_name(state)} if named_hops else {}
    was_named = any("reference_hops" in record for record in state["transactions"].values())
    if named_hops or was_named:
        refresh_seed_depths(state)
    # Rebuild legacy capped paths without their display allowances. Keep the
    # linear address-stop scope for ordinary runs, and use HopScope when named
    # distances, saved allowances or script/outpoint stops need reconstruction.
    detailed_scope = (named_hops or was_named or has_hop_limits(labels)
                      or any(label.get("stop") is True and label.get("kind") != "address" for label in labels))
    scope = HopScope(state, respect_attribution_hops=False) if detailed_scope else ServiceScope(state)

    def within_hops(depth):
        return depth <= limits.max_hops if named_hops else depth < limits.max_hops

    def add(txid, index, depth, origin, parent=None):
        key = f"{txid}:{index}"
        existing = state["outputs"].get(key)
        if existing is None:
            state["outputs"][key] = {"outpoint": key, "txid": txid, "vout": index,
                "depth": depth, "origin": origin, "status": "pending"}
            scope.register(key)
        elif depth < existing["depth"]:
            existing["depth"] = depth
            existing["status"] = "pending"
        if scope.active:
            # A longer path can carry more allowance than an exhausted short
            # path. Offer every verified arrival, not only depth improvements.
            released = scope.admit(key, depth, parent=parent)
            for candidate in set(released) | ({key} if existing is None or existing["status"] == "pending" else set()):
                item = state["outputs"][candidate]
                if not scope.blocked(candidate) and item["status"] not in TERMINAL:
                    item["status"] = "pending"
                    heapq.heappush(queue, (scope.depth(item), candidate))
        elif existing is None or existing["status"] == "pending":
            heapq.heappush(queue, (depth, key))

    if state["parent_run"]:
        available = {key for key, item in state["outputs"].items() if item["status"] not in TERMINAL}
        if only is not None and not set(only).issubset(available):
            raise TraceError("Selected outpoint is not on the saved frontier")
        for key in sorted(available if only is None else only):
            if scope.blocked(key):
                continue
            state["outputs"][key]["status"] = "pending"
            heapq.heappush(queue, (scope.depth(state["outputs"][key]), key))
    else:
        for seed in state["seeds"]:
            txid, index = parse_outpoint(seed)
            add(txid, index, 0, "analyst_seed")

    def write_state():
        refresh_reference_hops(state, scope)
        state["observations"] = sorted(set(state["observations"]) | api.used)
        state["stats"] = {"requests_this_run": api.budget.requests,
            "outpoints_examined_this_run": count, "new_transactions_this_run": new_transactions,
            "transactions_cumulative": len(state["transactions"]),
            "outputs_cumulative": len(state["outputs"]),
            "frontier_count": sum(item["status"] not in TERMINAL for item in state["outputs"].values())}
        if scope.active:
            state["stats"]["service_stopped_outputs"] = sum(
                item.get("trace_control", {}).get("reason") == "suspected_service_stop"
                for item in state["outputs"].values())
            state["stats"]["held_behind_service_outputs"] = sum(
                item.get("trace_control", {}).get("reason") == "held_behind_service"
                for item in state["outputs"].values())
            state["stats"]["attribution_hop_limited_outputs"] = sum(
                item.get("trace_control", {}).get("reason") == "attribution_hop_limit"
                for item in state["outputs"].values())
            state["stats"]["active_frontier_count"] = sum(
                item["status"] not in TERMINAL and not item.get("trace_control")
                for item in state["outputs"].values())
        performance()
        save_json(checkpoint, state)

    def save(*, force=False):
        checkpoint_writer.save(write_state, force=force, completed=not force)

    def get_tx(txid, depth):
        nonlocal new_transactions
        cached = state["transactions"].get(txid)
        if cached is not None and cached["data"].get("status", {}).get("confirmed"):
            cached["depth"] = min(depth, cached["depth"])
            return cached["data"]
        if cached is None and new_transactions >= limits.max_transactions:
            raise StopRun("transaction_limit")
        data, oid = get("/tx/" + txid)
        validate_transaction(data, txid)
        state["transactions"][txid] = {"data": data, "depth": min(depth, cached["depth"]) if cached else depth,
                                       "observation_id": oid}
        if cached is None:
            new_transactions += 1
        return data

    prepared = set()

    def prepare_frontier():
        """Prefetch a bounded same-hop window, advancing each ready dependency.

        Only selected eligible outputs supply child endpoints. Near a budget
        boundary the ordinary serial path retains the next output's slots.
        Traversal remains ordered and starts only after this window drains.
        """
        nonlocal fetch_wait_seconds
        if (worker_ceiling <= 1 or not callable(getattr(api, "submit", None)) or not queue
                or queue[0][1] in prepared):
            return
        target = concurrency()
        if target <= 1:
            return
        depth = queue[0][0]
        window, seen, funding_seen = [], set(), set()
        # Count distinct funding endpoints, not merely eight adjacent outputs
        # from one large transaction. The output cap bounds speculative scope.
        unique_limit = min(worker_ceiling, max(workers, target * 2))
        scan_limit = min(256, limits.max_outpoints - count)
        for candidate_depth, key in heapq.nsmallest(scan_limit, queue):
            item = state["outputs"][key]
            if (candidate_depth != depth or candidate_depth != scope.depth(item)
                    or item["status"] != "pending" or key in seen or scope.blocked(key)):
                continue
            if item["txid"] not in funding_seen and len(funding_seen) >= unique_limit:
                break
            seen.add(key)
            funding_seen.add(item["txid"])
            window.append(item)
        if not window:
            return
        funding_ids = list(dict.fromkeys(item["txid"] for item in window))
        missing = [txid for txid in funding_ids if txid not in state["transactions"]]
        possible_children = len(window) if within_hops(depth) else 0
        if len(missing) + possible_children > limits.max_transactions - new_transactions:
            return
        fetch_ids = {txid for txid in funding_ids
                     if not state["transactions"].get(txid, {}).get("data", {}).get("status", {}).get("confirmed")}
        nominal_requests = len(fetch_ids) + (len(funding_ids) + possible_children if possible_children else 0)
        if api.auth == "blockstream" and not api.token:
            nominal_requests += 1
        if nominal_requests > limits.max_requests - api.budget.requests:
            return
        prepared.update(seen)
        fetching = FrontierFetcher(api, concurrency, report_activity)
        by_funding = {}
        for item in window:
            by_funding.setdefault(item["txid"], []).append(item)

        def eligible(item, transaction):
            if (scope.blocked(item["outpoint"]) or not within_hops(scope.depth(item))
                    or item["vout"] >= len(transaction["vout"])):
                return False
            output = transaction["vout"][item["vout"]]
            return (output_kind(output) == "spendable" and scope.permits(item, output)
                    and not any(label.get("stop") for label in match_labels(labels, item["outpoint"], output))
                    and (include_unconfirmed or transaction["status"]["confirmed"]))

        def spending_ready(txid, transaction, result):
            if isinstance(result, Exception):
                return
            rows = result[0]
            if not isinstance(rows, list) or len(rows) != len(transaction["vout"]):
                return
            for item in by_funding[txid]:
                if not eligible(item, transaction):
                    continue
                spend = rows[item["vout"]]
                if (not isinstance(spend, dict) or spend.get("spent") is not True
                        or (not include_unconfirmed and
                            (not isinstance(spend.get("status"), dict) or not spend["status"].get("confirmed")))):
                    continue
                child_id, vin = spend.get("txid"), spend.get("vin")
                if (not isinstance(child_id, str) or not HEX64.fullmatch(child_id)
                        or type(vin) is not int or vin < 0 or child_id == txid):
                    continue
                if not state["transactions"].get(child_id, {}).get("data", {}).get("status", {}).get("confirmed"):
                    fetching.add("/tx/" + child_id)

        def funding_ready(txid, result):
            if isinstance(result, Exception):
                return
            transaction = result[0]
            try:
                validate_transaction(transaction, txid)
            except TraceError:
                return  # Ordered get_tx() surfaces the saved failure later.
            if any(eligible(item, transaction) for item in by_funding[txid]):
                fetching.add("/tx/" + txid + "/outspends",
                             lambda rows: spending_ready(txid, transaction, rows))

        for txid in funding_ids:
            if txid in fetch_ids:
                fetching.add("/tx/" + txid, lambda result, txid=txid: funding_ready(txid, result))
            else:
                funding_ready(txid, (state["transactions"][txid]["data"], None))
        waiting = time.monotonic()
        try:
            fetching.run()
        finally:
            fetch_wait_seconds += max(0., time.monotonic() - waiting)

    current = None
    stop_reason = None
    # This is the frontier being processed, not the deepest prefetched child or
    # an estimate of remaining requests. A resumed service scope can revisit a
    # shallower hop. The heap already supplies the next hop without a scan of
    # the growing evidence graph on every output.
    had_frontier = bool(queue)
    current_hop = min(limits.max_hops, queue[0][0]) if queue else 0

    def telemetry():
        feedback = metrics()
        elapsed = max(.001, time.monotonic() - started)
        return {"worker_count": target_workers,
                "worker_limit": policy.ceiling if policy is not None else workers,
                "observed_rps": api.budget.requests / elapsed,
                **{key: value for key, value in feedback.items() if key.startswith("shared_api_")}}

    def report_activity():
        nonlocal last_progress_at
        timestamp = time.monotonic()
        if timestamp - last_progress_at >= 1:
            last_progress_at = timestamp
            report_progress(progress, "collecting", current_hop, limits.max_hops,
                            **hop_progress, **telemetry())

    def report_hop(depth):
        nonlocal current_hop
        depth = min(limits.max_hops, depth)
        if depth != current_hop:
            current_hop = depth
            report_progress(progress, "collecting", current_hop, limits.max_hops, **hop_progress, **telemetry())

    if had_frontier:
        report_progress(progress, "collecting", current_hop, limits.max_hops, **hop_progress, **telemetry())
    try:
        save(force=True)
        while queue:
            api.budget.check()
            if count >= limits.max_outpoints:
                raise StopRun("outpoint_limit")
            pending_depth, pending_key = queue[0]
            pending = state["outputs"][pending_key]
            if (pending_depth == scope.depth(pending) and pending["status"] == "pending"
                    and not scope.blocked(pending_key)):
                report_hop(pending_depth)
            prepare_frontier()
            depth, key = heapq.heappop(queue)
            current = state["outputs"][key]
            if depth != scope.depth(current) or current["status"] != "pending" or scope.blocked(key):
                continue
            count += 1
            tx = get_tx(current["txid"], current["depth"] if named_hops else depth)
            if current["vout"] >= len(tx["vout"]):
                raise TraceError("Seed or frontier output index does not exist: " + key)
            output = tx["vout"][current["vout"]]
            for released in scope.refresh(key) or ():
                item = state["outputs"][released]
                if released != key and not scope.blocked(released) and item["status"] not in TERMINAL:
                    item["status"] = "pending"
                    heapq.heappush(queue, (scope.depth(item), released))
            depth = scope.depth(current)
            if scope.blocked(key):
                current = None
                save()
                continue
            report_hop(depth)
            kind = output_kind(output)
            current["labels"] = match_labels(labels, key, output)
            if kind != "spendable":
                current["status"] = kind
            elif any(is_service_stop(label) for label in current["labels"]):
                scope.mark_stop(current, output.get("scriptpubkey_address"))
            elif any(label.get("stop") for label in current["labels"]):
                current["status"] = "analyst_stop"
            elif not include_unconfirmed and not tx.get("status", {}).get("confirmed"):
                current["status"] = "unconfirmed_funding"
            elif not within_hops(depth):
                current["status"] = "hop_limit"
            else:
                spends, oid = get("/tx/" + current["txid"] + "/outspends")
                if not isinstance(spends, list) or len(spends) != len(tx["vout"]):
                    raise TraceError("Outspends length does not match funding transaction")
                spend = spends[current["vout"]]
                if not isinstance(spend, dict) or type(spend.get("spent")) is not bool:
                    raise TraceError("Malformed outspend response")
                current["spend_observation_id"] = oid
                current["observed_spend"] = spend
                if not spend["spent"]:
                    current["status"] = "unspent_at_observation"
                elif not include_unconfirmed and not spend.get("status", {}).get("confirmed"):
                    current["status"] = "unconfirmed_spend"
                else:
                    child_id, vin = spend.get("txid", ""), spend.get("vin")
                    if not isinstance(child_id, str) or not HEX64.fullmatch(child_id) or type(vin) is not int or vin < 0:
                        raise TraceError("Malformed spending transaction reference")
                    if child_id == current["txid"]:
                        raise TraceError("Invalid self-spending transaction")
                    child_depth = current["depth"] + 1 if named_hops else depth + 1
                    child = get_tx(child_id, child_depth)
                    if vin >= len(child["vin"]):
                        raise TraceError("Outspend input index does not exist")
                    actual = child["vin"][vin]
                    if actual.get("txid") != current["txid"] or actual.get("vout") != current["vout"] or actual.get("is_pegin"):
                        raise TraceError("Outspend reference disagrees with spending transaction input")
                    if not include_unconfirmed and not child.get("status", {}).get("confirmed"):
                        current["status"] = "unconfirmed_spend"
                    else:
                        state["links"][key] = {"outpoint": key, "spending_txid": child_id, "vin": vin,
                            "observation_id": oid, "spending_tx_observation_id": state["transactions"][child_id]["observation_id"],
                            "hop": child_depth, "relationship": "observed_utxo_spend"}
                        current["status"] = "spent"
                        for index in range(len(child["vout"])):
                            add(child_id, index, child_depth, "candidate_descendant", parent=key)
            current = None
            save()
            report_activity()
        state["status"] = "bounded_complete"
    except StopRun as error:
        stop_reason = str(error)
        state["status"] = "paused"
    except TraceError as error:
        stop_reason = "error"
        state["status"] = "error"
        state["errors"].append(str(error))
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        stop_reason = "error"
        state["status"] = "error"
        state["errors"].append("Malformed API data or checkpoint: " + type(error).__name__)
    except KeyboardInterrupt:
        stop_reason = "interrupted"
        state["status"] = "paused"
    finally:
        # No response may update the evidence store after the final snapshot.
        drain = getattr(api, "drain_pending", None)
        if callable(drain):
            try:
                drain(cancel=bool(stop_reason))
            except KeyboardInterrupt:
                # The API drains before surfacing repeated interrupts. Still
                # preserve the final recoverable frontier after that signal.
                if stop_reason is None:
                    stop_reason = "interrupted"
                    state["status"] = "paused"
        if named_hops or was_named:
            refresh_seed_depths(state)
        if stop_reason:
            for item in state["outputs"].values():
                if item["status"] == "pending":
                    item["status"] = stop_reason
        state["stop_reason"] = stop_reason
        state["finished_at"] = now()
        save(force=True)
        performance()
        phase = {"bounded_complete": "collection_complete", "paused": "collection_paused",
                 "error": "collection_error"}.get(state["status"], "collection_error")
        if not had_frontier and state["status"] == "bounded_complete":
            report_progress(progress, "collection_empty", 0, 0, **hop_progress)
        else:
            report_progress(progress, phase, current_hop, limits.max_hops, **hop_progress, **telemetry())
    return state
