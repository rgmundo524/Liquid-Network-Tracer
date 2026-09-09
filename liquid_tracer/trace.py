import copy
import heapq
import platform
import uuid
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .common import (HEX64, StopRun, TraceError, digest, match_labels, now,
                     output_kind, parse_outpoint, save_json)

TERMINAL = {"spent", "fee", "pegout", "provably_unspendable"}


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
                  "labels": labels, "status": "running", "errors": [], "stats": {}})
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


def trace(api, state, limits, checkpoint, include_unconfirmed=False, only=None):
    """Hop 0 = selected seed output; one hop = its spending transaction.

    Every spendable child output is a candidate. Other inputs are context, never
    traversed backward, clustered, or used as address-history expansion seeds.
    """
    limits.validate()
    queue = []
    count = 0
    new_transactions = 0
    labels = state["labels"]
    state["include_unconfirmed"] = include_unconfirmed
    state["selected_frontier"] = sorted(only) if only is not None else None

    def add(txid, index, depth, origin):
        key = f"{txid}:{index}"
        existing = state["outputs"].get(key)
        if existing is None:
            state["outputs"][key] = {"outpoint": key, "txid": txid, "vout": index,
                "depth": depth, "origin": origin, "status": "pending"}
            heapq.heappush(queue, (depth, key))
        elif depth < existing["depth"]:
            existing["depth"] = depth
            existing["status"] = "pending"
            heapq.heappush(queue, (depth, key))

    if state["parent_run"]:
        available = {key for key, item in state["outputs"].items() if item["status"] not in TERMINAL}
        if only is not None and not set(only).issubset(available):
            raise TraceError("Selected outpoint is not on the saved frontier")
        for key in sorted(available if only is None else only):
            state["outputs"][key]["status"] = "pending"
            heapq.heappush(queue, (state["outputs"][key]["depth"], key))
    else:
        for seed in state["seeds"]:
            txid, index = parse_outpoint(seed)
            add(txid, index, 0, "analyst_seed")

    def save():
        state["observations"] = sorted(set(state["observations"]) | api.used)
        state["stats"] = {"requests_this_run": api.budget.requests,
            "outpoints_examined_this_run": count, "new_transactions_this_run": new_transactions,
            "transactions_cumulative": len(state["transactions"]),
            "outputs_cumulative": len(state["outputs"]),
            "frontier_count": sum(item["status"] not in TERMINAL for item in state["outputs"].values())}
        save_json(checkpoint, state)

    def get_tx(txid, depth):
        nonlocal new_transactions
        cached = state["transactions"].get(txid)
        if cached is not None and cached["data"].get("status", {}).get("confirmed"):
            cached["depth"] = min(depth, cached["depth"])
            return cached["data"]
        if cached is None and new_transactions >= limits.max_transactions:
            raise StopRun("transaction_limit")
        data, oid = api.get("/tx/" + txid)
        validate_transaction(data, txid)
        state["transactions"][txid] = {"data": data, "depth": min(depth, cached["depth"]) if cached else depth,
                                       "observation_id": oid}
        if cached is None:
            new_transactions += 1
        return data

    current = None
    stop_reason = None
    try:
        save()
        while queue:
            api.budget.check()
            if count >= limits.max_outpoints:
                raise StopRun("outpoint_limit")
            depth, key = heapq.heappop(queue)
            current = state["outputs"][key]
            if depth != current["depth"] or current["status"] != "pending":
                continue
            count += 1
            tx = get_tx(current["txid"], depth)
            if current["vout"] >= len(tx["vout"]):
                raise TraceError("Seed or frontier output index does not exist: " + key)
            output = tx["vout"][current["vout"]]
            kind = output_kind(output)
            current["labels"] = match_labels(labels, key, output)
            if kind != "spendable":
                current["status"] = kind
            elif any(label.get("stop") for label in current["labels"]):
                current["status"] = "analyst_stop"
            elif not include_unconfirmed and not tx.get("status", {}).get("confirmed"):
                current["status"] = "unconfirmed_funding"
            elif depth >= limits.max_hops:
                current["status"] = "hop_limit"
            else:
                spends, oid = api.get("/tx/" + current["txid"] + "/outspends")
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
                    child = get_tx(child_id, depth + 1)
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
                            "hop": depth + 1, "relationship": "observed_utxo_spend"}
                        current["status"] = "spent"
                        for index in range(len(child["vout"])):
                            add(child_id, index, depth + 1, "candidate_descendant")
            current = None
            save()
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
        if stop_reason:
            for item in state["outputs"].values():
                if item["status"] == "pending":
                    item["status"] = stop_reason
        state["stop_reason"] = stop_reason
        state["finished_at"] = now()
        save()
    return state
