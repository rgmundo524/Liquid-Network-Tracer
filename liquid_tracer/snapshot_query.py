"""Read only the indexed shared evidence needed by one investigation plot.

Topology has been validated when the index was built. Searches use its small
spend records; complete transaction bodies are fetched only after selection.
Source transaction/output depths are retained here and rebased by projection.
"""
from collections import deque
from copy import deepcopy

from .common import TraceError, output_kind, parse_outpoint
from .connections import validate_connection_scope, validate_hops
from .progress import report_progress


def _reachable(index, seeds, max_hops, progress):
    """Selected first outputs, then every exact child output within the bound.

    Keep arrival distance separate from seed distance: another seed transaction
    can also be reached by a path, making its unselected outputs traversable.
    """
    selected = {parse_outpoint(key)[0] for key in seeds}
    arrivals = {}
    pending = deque((key, 0) for key in sorted(seeds))
    examined = 0
    while pending:
        key, depth = pending.popleft()
        if max_hops is not None and depth >= max_hops:
            continue
        link = index.link(key)
        if link is None:
            continue
        child, next_depth = link["spending_txid"], depth + 1
        if next_depth >= arrivals.get(child, float("inf")):
            continue
        arrivals[child] = next_depth
        selected.add(child)
        if max_hops is None or next_depth < max_hops:
            pending.extend((item["outpoint"], next_depth) for item in index.outgoing(child))
        examined += 1
        if examined % 256 == 0:
            report_progress(progress, "projecting_collection", 0, 1)
    return selected


def _connections(index, seeds, max_hops, progress):
    """Retain all bounded starter paths, using topology rather than tx bodies.

    An edge u->v belongs to some qualifying path iff shortest(source,u) + 1
    + shortest(v,any other starter) is within the limit. Reverse reachability
    discards unrelated downstream tails before the forward search begins.
    """
    roots = {parse_outpoint(key)[0] for key in seeds}
    if len(roots) < 2:
        raise TraceError("Choose outputs from at least two distinct starting transactions")
    selected = set(roots)
    for number, root in enumerate(sorted(roots)):
        report_progress(progress, "projecting_collection", number, len(roots))
        upstream = dict.fromkeys(roots - {root}, 0)
        pending = deque(sorted(upstream))
        examined = 0
        while pending:
            child = pending.popleft()
            examined += 1
            if examined % 256 == 0:
                report_progress(progress, "projecting_collection", number, len(roots))
            if max_hops is not None and upstream[child] >= max_hops:
                continue
            for link in index.incoming(child):
                parent = parse_outpoint(link["outpoint"])[0]
                if parent not in upstream:
                    upstream[parent] = upstream[child] + 1
                    pending.append(parent)
        downstream = {root: 0}
        pending = deque([root])
        while pending:
            parent = pending.popleft()
            examined += 1
            if examined % 256 == 0:
                report_progress(progress, "projecting_collection", number, len(roots))
            if max_hops is not None and downstream[parent] >= max_hops:
                continue
            for link in index.outgoing(parent):
                key, child = link["outpoint"], link["spending_txid"]
                if parent == root and key not in seeds:
                    continue
                if child not in upstream:
                    continue
                path_length = downstream[parent] + 1 + upstream[child]
                if max_hops is not None and path_length > max_hops:
                    continue
                selected.update((parent, child))
                if child not in downstream:
                    downstream[child] = downstream[parent] + 1
                    pending.append(child)
        report_progress(progress, "projecting_collection", number + 1, len(roots))
    return selected


def select_snapshot(index, seeds, *, max_hops=None, connection_scope=None, progress=None):
    """Return a detached source-state subset without loading unrelated tails.

    ``None`` is deliberately unbounded, including for named-group hop policies:
    an ordinary-depth bound cannot safely approximate group distance resets.
    ``saved_transactions`` holds immediate funding context outside the selected
    graph. Consumers merge it with ``transactions`` only for resolving vin facts,
    never for traversal. Source metadata/depths otherwise remain unchanged.
    """
    scope = validate_connection_scope(connection_scope)
    if max_hops is not None:
        validate_hops(max_hops)
    if scope == "hop_limited" and max_hops is None:
        validate_hops(max_hops)
    seeds = sorted({f"{txid}:{number}" for txid, number in map(parse_outpoint, seeds)})
    if not seeds:
        raise TraceError("Choose this investigation's starting outputs before plotting shared data")
    transactions = {}
    for key in seeds:
        txid, number = parse_outpoint(key)
        if txid not in transactions:
            record = index.transaction(txid)
            if record is None:
                raise TraceError("Shared collection is missing a selected starting transaction; collect shared data for these seeds first")
            transactions[txid] = deepcopy(record)
        if number >= len(transactions[txid]["data"]["vout"]):
            raise TraceError("A selected starting output does not exist in the shared transaction evidence")

    report_progress(progress, "projecting_collection", 0, 1)
    if scope == "shortest":
        from .shortest_paths import shortest_routes
        routes = shortest_routes(seeds, lambda child: (
            (parse_outpoint(link["outpoint"])[0], link["outpoint"]) for link in index.incoming(child)),
            progress=progress)
        selected = {parse_outpoint(key)[0] for key in seeds}
        selected.update(parse_outpoint(key)[0] for route in routes for key in route["outpoints"])
    elif scope in ("all_saved", "hop_limited"):
        selected = _connections(index, set(seeds), None if scope == "all_saved" else max_hops, progress)
    else:
        selected = _reachable(index, set(seeds), max_hops, progress)
    for number, txid in enumerate(sorted(selected), 1):
        if txid not in transactions:
            record = index.transaction(txid)
            if record is None:
                raise TraceError("Indexed shared spend is missing its transaction evidence")
            transactions[txid] = deepcopy(record)
        report_progress(progress, "loading_collection", number, len(selected))

    outputs, links, context, addresses = {}, {}, {}, set()
    checked_funding = set(transactions)
    for txid, record in transactions.items():
        for number, raw in enumerate(record["data"]["vout"]):
            key = f"{txid}:{number}"
            item = index.output(key)
            item = deepcopy(item) if item is not None else {
                "outpoint": key, "txid": txid, "vout": number, "depth": record.get("depth", 0),
                "origin": "saved_transaction_input", "status": "not_observed",
            }
            link = index.link(key)
            if link is not None:
                # Keep global negative evidence even if the spending body is
                # outside this view. Otherwise stale unspent observations can
                # turn a truncated boundary into a false endpoint.
                if output_kind(raw) == "spendable" and item.get("status") != "spent":
                    item["status"] = "spent_in_saved_evidence"
                if link["spending_txid"] in selected:
                    links[key] = deepcopy(link)
            outputs[key] = item
            if raw.get("scriptpubkey_address"):
                addresses.add(raw["scriptpubkey_address"])
        for vin in record["data"]["vin"]:
            parent = vin.get("txid")
            if parent and parent not in checked_funding and not (
                    vin.get("is_coinbase") or vin.get("is_pegin")):
                checked_funding.add(parent)
                funding = index.transaction(parent)
                if funding is not None:
                    context[parent] = deepcopy(funding)
            raw = vin.get("prevout") or {}
            if parent in context and not (vin.get("is_coinbase") or vin.get("is_pegin")):
                from .saved_inputs import saved_input_output
                raw = saved_input_output(context, vin)
            if raw.get("scriptpubkey_address"):
                addresses.add(raw["scriptpubkey_address"])
    state = deepcopy(index.metadata)
    state.update(seeds=seeds, transactions=transactions, outputs=outputs, links=links,
                 address_tx_counts=deepcopy(index.counts(addresses)))
    if context:
        state["saved_transactions"] = context
    else:
        state.pop("saved_transactions", None)
    state.pop("observations", None)
    report_progress(progress, "projecting_collection", 1, 1)
    return state
