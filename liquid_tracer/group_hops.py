"""Named-address hop measurements over verified forward UTXO paths."""

from collections import defaultdict, deque

from .networks import blockchain
from .common import TraceError, output_kind, parse_outpoint


def normalize_reference_name(value):
    if not isinstance(value, str):
        raise TraceError("Hop reference name must be text")
    value = value.strip()
    if len(value) > 120 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise TraceError("Hop reference name must be at most 120 characters without control characters")
    return value


def reference_name(state):
    return normalize_reference_name(state.get("hop_reference_name", ""))


def reference_addresses(state):
    name = reference_name(state).casefold()
    if not name:
        return set()
    return {label["value"] for label in state.get("labels", [])
            if label.get("kind") == "address" and label.get("enabled", True) is True
            and isinstance(label.get("entity"), str)
            and label["entity"].strip().casefold() == name}


def restore_scope_outputs(state):
    """Recover output bookkeeping only from seeds and verified saved spends.

    A checkpoint can contain a child transaction/link before all its output
    records were added. Raw transaction inputs never introduce new roots.
    """
    for key in state["seeds"]:
        txid, index = parse_outpoint(key)
        state["outputs"].setdefault(key, {"outpoint": key, "txid": txid, "vout": index,
            "depth": 0, "origin": "analyst_seed", "status": "pending"})
    for key, link in list(state["links"].items()):
        parent = state["outputs"].get(key)
        record = state["transactions"].get(link.get("spending_txid"))
        if parent is None or record is None:
            continue
        vin = link.get("vin")
        inputs = record["data"].get("vin", [])
        if (type(vin) is not int or not 0 <= vin < len(inputs)
                or inputs[vin].get("is_pegin") or inputs[vin].get("is_coinbase")
                or inputs[vin].get("txid") != parent["txid"]
                or inputs[vin].get("vout") != parent["vout"]):
            raise TraceError("Saved spend link disagrees with its transaction input")
        txid = link["spending_txid"]
        for index in range(len(record["data"]["vout"])):
            child = f"{txid}:{index}"
            state["outputs"].setdefault(child, {"outpoint": child, "txid": txid, "vout": index,
                "depth": parent["depth"] + 1, "origin": "candidate_descendant", "status": "pending"})


def refresh_seed_depths(state):
    """Preserve ordinary seed distances independently of the active hop basis."""
    by_tx = defaultdict(list)
    for key, item in state["outputs"].items():
        by_tx[item["txid"]].append(key)
    depths, transaction_depths = {}, {}
    queue = deque((key, 0) for key in state["seeds"] if key in state["outputs"])
    while queue:
        key, depth = queue.popleft()
        if depth >= depths.get(key, float("inf")):
            continue
        depths[key] = depth
        item = state["outputs"][key]
        item["depth"] = depth
        transaction_depths[item["txid"]] = min(depth, transaction_depths.get(item["txid"], depth))
        link = state["links"].get(key)
        if link:
            link["hop"] = depth + 1
            child_id = link["spending_txid"]
            transaction_depths[child_id] = min(depth + 1, transaction_depths.get(child_id, depth + 1))
            queue.extend((child, depth + 1) for child in by_tx[child_id])
    for txid, depth in transaction_depths.items():
        if txid in state["transactions"]:
            state["transactions"][txid]["depth"] = depth


def refresh_reference_hops(state, scope=None):
    """Set display hop summaries without replacing raw seed-depth evidence.

    Inspected boundary outputs can be one above the ceiling; they do not count
    as collected coverage. A boundary-only transaction has a null summary.
    """
    for record in state["transactions"].values():
        record.pop("reference_hops", None)
    if not reference_name(state):
        return state
    ceiling = state.get("limits", {}).get("max_hops", float("inf"))
    for record in state["transactions"].values():
        record["reference_hops"] = None
    depths = defaultdict(list)
    for key, item in state["outputs"].items():
        if scope is not None and key not in scope.reachable:
            continue
        record = state["transactions"].get(item["txid"])
        if record is None or not 0 <= item["vout"] < len(record["data"]["vout"]):
            continue
        if output_kind(record["data"]["vout"][item["vout"]], blockchain(state)) == "fee":
            continue
        depth = scope.depth(item) if scope is not None else item.get("trace_scope_depth")
        if type(depth) is int and depth <= ceiling:
            depths[item["txid"]].append(depth)
    for txid, values in depths.items():
        state["transactions"][txid]["reference_hops"] = max(values)
    return state
