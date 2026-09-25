"""Exact forward paths from selected outputs or a transaction to peg-out requests.

Reachability uses saved, verified UTXO spends. A displayed peg-out request is
not evidence that a Bitcoin payout occurred or that confidential value followed
any particular path.
"""
from collections import defaultdict, deque
from copy import deepcopy

from .common import HEX64, TraceError, output_kind, parse_outpoint
from .hop_limits import output_budget
from .trace import validate_transaction

SCOPE = ("Search scope: verified forward UTXO spends from the chosen transaction. "
         "Paused, stopped, unconfirmed or unsearched branches may contain undiscovered peg-outs; "
         "no result is not proof that no peg-out exists. Address reuse and other transaction "
         "inputs do not establish a path. A Liquid peg-out request does not confirm a Bitcoin payout.")
SEED_SCOPE = SCOPE.replace("from the chosen transaction", "from the selected seed outputs")


def validate_query(txid=None, min_hops=0, max_hops=10, *, seeds=None,
                   include_unspent=False, include_unspendable=False):
    if seeds is not None:
        if txid is not None:
            raise TraceError("Choose either selected seed outputs or one transaction for a peg-out search")
        if not isinstance(seeds, list) or not seeds or any(not isinstance(seed, str) for seed in seeds):
            raise TraceError("Peg-out search requires a nonempty list of selected seed outputs")
        normalized = sorted({f"{origin}:{index}" for origin, index in map(parse_outpoint, seeds)})
        origin = {"seeds": normalized}
    else:
        if not isinstance(txid, str) or not HEX64.fullmatch(txid.strip()):
            raise TraceError("Peg-out search requires selected seed outputs or a 64-character transaction hash")
        origin = {"txid": txid.strip().lower()}
    if any(type(value) is not int or not 0 <= value <= 2147483647
           for value in (min_hops, max_hops)):
        raise TraceError("Peg-out hops must be whole numbers from 0 to 2147483647")
    if min_hops > max_hops:
        raise TraceError("Minimum peg-out hops cannot exceed maximum hops")
    options = {"include_unspent": include_unspent, "include_unspendable": include_unspendable}
    if any(type(value) is not bool for value in options.values()):
        raise TraceError("Unspent and unspendable endpoints must be enabled or disabled")
    # Omitting disabled options preserves the identity of existing saved queries.
    return {**origin, "min_hops": min_hops, "max_hops": max_hops,
            **{key: value for key, value in options.items() if value}}


def _pegout(output):
    """Accept Esplora's peg-out metadata, never generic OP_RETURN outputs."""
    value = output.get("pegout")
    if value is None:
        return False
    if not isinstance(value, dict):
        raise TraceError("Malformed saved peg-out request metadata")
    if not value:
        return False
    for field in ("scriptpubkey", "scriptpubkey_address"):
        if field in value and (not isinstance(value[field], str) or not value[field]):
            raise TraceError("Malformed saved peg-out request destination")
    if "genesis_hash" in value and (not isinstance(value["genesis_hash"], str)
                                    or not HEX64.fullmatch(value["genesis_hash"])):
        raise TraceError("Malformed saved peg-out request genesis hash")
    return output_kind(output) == "pegout"


def _evidence(state):
    """Fail closed on corrupt evidence, including links excluded by stop rules."""
    forward, incoming = defaultdict(list), defaultdict(list)
    transactions, outputs = state["transactions"], state["outputs"]
    indegree = {txid: 0 for txid in transactions}
    pegouts = {}
    for txid, record in transactions.items():
        if not isinstance(txid, str) or not HEX64.fullmatch(txid) or txid != txid.lower():
            raise TraceError("Malformed saved peg-out search transaction ID")
        data = record["data"]
        validate_transaction(data, txid)
        pegouts[txid] = [index for index, output in enumerate(data["vout"]) if _pegout(output)]
        for index, vin in enumerate(data["vin"]):
            if not vin.get("is_pegin") and not vin.get("is_coinbase"):
                incoming[f"{vin.get('txid')}:{vin.get('vout')}"].append((txid, index))
    for key, link in sorted(state["links"].items()):
        parent, index = parse_outpoint(key)
        child, vin = link["spending_txid"], link["vin"]
        tracked = outputs[key]
        funding, spending = transactions[parent]["data"], transactions[child]["data"]
        if (key != f"{parent}:{index}" or tracked["txid"] != parent
                or type(tracked["vout"]) is not int or tracked["vout"] != index
                or tracked.get("outpoint", key) != key or index >= len(funding["vout"])
                or type(vin) is not int or not 0 <= vin < len(spending["vin"])):
            raise TraceError("Peg-out search requires exact saved output and input indices")
        actual = spending["vin"][vin]
        if (actual.get("is_pegin") or actual.get("is_coinbase") or parent == child
                or actual.get("txid") != parent or type(actual.get("vout")) is not int
                or actual["vout"] != index or incoming[key] != [(child, vin)]):
            raise TraceError("Peg-out spend link disagrees with its saved transaction input")
        output = funding["vout"][index]
        if output_kind(output) != "spendable":
            raise TraceError("Peg-out spend link references a non-spendable output")
        prevout = actual.get("prevout")
        if prevout is not None and (not isinstance(prevout, dict) or any(
                field in prevout and prevout[field] != output.get(field)
                for field in ("scriptpubkey", "scriptpubkey_type", "scriptpubkey_address", "value", "valuecommitment",
                              "asset", "assetcommitment", "pegout"))):
            raise TraceError("Peg-out spending input disagrees with its saved funding output")
        forward[parent].append((child, key, output_budget(state["labels"], key, output)))
        indegree[child] += 1
    ready = deque(txid for txid, count in indegree.items() if not count)
    visited = 0
    while ready:
        parent = ready.popleft()
        visited += 1
        for child, _, _ in forward[parent]:
            indegree[child] -= 1
            if not indegree[child]:
                ready.append(child)
    if visited != len(transactions):
        raise TraceError("Saved UTXO spends contain a cycle; peg-out search cannot proceed")
    return forward, pegouts


def _paths(state, query):
    try:
        forward, pegouts = _evidence(state)
        endpoints = {txid: {index: "pegout" for index in indices} for txid, indices in pegouts.items()}
        if query.get("include_unspent"):
            from .export import _unspent_endpoints
            # Consult the complete archive before pruning. An input on an
            # excluded branch still disproves an older unspent observation.
            for key in _unspent_endpoints(state):
                txid, index = parse_outpoint(key)
                item = state["outputs"][key]
                rows = state["transactions"][txid]["data"]["vout"]
                if (key != f"{txid}:{index}" or item["txid"] != txid
                        or type(item["vout"]) is not int or item["vout"] != index
                        or item.get("outpoint", key) != key or index >= len(rows)):
                    raise TraceError("Unspent endpoints require exact saved output indices")
                if output_kind(rows[index]) == "spendable":
                    endpoints[txid][index] = "unspent"
        if query.get("include_unspendable"):
            for txid, record in state["transactions"].items():
                endpoints[txid].update({index: "unspendable" for index, output in enumerate(record["data"]["vout"])
                                       if output_kind(output) == "provably_unspendable"})
        confirmed = {txid for txid, record in state["transactions"].items()
                     if state.get("include_unconfirmed", False)
                     or record["data"]["status"]["confirmed"] is True}
        limit = min(query["max_hops"], max(0, len(state["transactions"]) - 1))
        seeds = set(query["seeds"]) if "seeds" in query else None
        roots = {parse_outpoint(key)[0] for key in seeds} if seeds is not None else {query["txid"]}
        if seeds is not None:
            for key in seeds:
                txid, index = parse_outpoint(key)
                if txid in state["transactions"] and index >= len(state["transactions"][txid]["data"]["vout"]):
                    raise TraceError("Selected seed output does not exist in its saved transaction")
        starts = {(txid, 0, limit) for txid in roots & confirmed}
        queue = deque(sorted(starts))
        seen, predecessors, successful = set(starts), defaultdict(set), set()
        distances = defaultdict(set)
        while queue:
            point = queue.popleft()
            parent, depth, remaining = point
            if depth >= query["min_hops"]:
                for index in endpoints[parent]:
                    key = f"{parent}:{index}"
                    if seeds is not None and depth == 0 and key not in seeds:
                        continue
                    successful.add(point)
                    distances[key].add(depth)
            if depth >= limit or remaining <= 0:
                continue
            for child, key, cap in forward[parent]:
                if seeds is not None and depth == 0 and key not in seeds:
                    continue
                budget = min(remaining, cap)
                if budget <= 0 or child not in confirmed:
                    continue
                following = (child, depth + 1, budget - 1)
                predecessors[following].add((point, key))
                if following not in seen:
                    seen.add(following)
                    queue.append(following)
        kept, marked, stack = set(), set(successful), list(successful)
        depths = defaultdict(set)
        while stack:
            point = stack.pop()
            depths[point[0]].add(point[1])
            for previous, key in predecessors[point]:
                kept.add(key)
                if previous not in marked:
                    marked.add(previous)
                    stack.append(previous)
        matches = []
        for key, hops in sorted(distances.items(), key=lambda item: parse_outpoint(item[0])):
            txid, index = parse_outpoint(key)
            output = state["transactions"][txid]["data"]["vout"][index]
            kind = endpoints[txid][index]
            match = {"outpoint": key, "txid": txid, "vout": index,
                     "hops": sorted(hops), "kind": kind}
            if kind == "pegout":
                match["pegout"] = deepcopy(output["pegout"])
            else:
                match["output"] = deepcopy(output)
                match["trace"] = deepcopy(state["outputs"].get(key))
                if kind == "unspent":
                    match.update({field: deepcopy(state["outputs"][key][field])
                                  for field in ("observed_spend", "spend_observation_id")})
            matches.append(match)
        return kept, matches, depths
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
        raise TraceError("Peg-out search needs complete, consistent saved spend evidence") from exc


def pegout_graph(state, query, *, color_attribution_arrows=None, center_name=None):
    """Filter a copied graph to the union of all qualifying bounded paths."""
    from .export import build_graph
    from .layout import arrange
    from .miro_frames import activity_frames

    if not isinstance(query, dict):
        raise TraceError("Choose selected seed outputs or a transaction and an inclusive peg-out hop range")
    query = validate_query(query.get("txid"), query.get("min_hops", 0), query.get("max_hops", 10),
                           seeds=query.get("seeds"), include_unspent=query.get("include_unspent", False),
                           include_unspendable=query.get("include_unspendable", False))
    outpoints, endpoint_matches, depths = _paths(state, query)
    matches = [{key: value for key, value in match.items() if key != "kind"}
               for match in endpoint_matches if match["kind"] == "pegout"]
    selected = set(depths)
    endpoints = {match["outpoint"] for match in endpoint_matches}
    reduced = deepcopy(state)
    reduced["transactions"] = {key: value for key, value in reduced["transactions"].items() if key in selected}
    for txid, record in reduced["transactions"].items():
        record["depth"] = min(depths[txid])
    reduced["outputs"] = {key: value for key, value in reduced["outputs"].items() if key in outpoints | endpoints}
    for match in endpoint_matches:
        # The full transaction can already prove a terminal request or script when the
        # bounded trace paused before classifying this individual output.
        reduced["outputs"].setdefault(match["outpoint"], {
            "outpoint": match["outpoint"], "txid": match["txid"], "vout": match["vout"],
            "depth": min(match["hops"]),
            "status": "provably_unspendable" if match["kind"] == "unspendable" else match["kind"]})
    reduced["links"] = {key: value for key, value in reduced["links"].items() if key in outpoints}
    if "seeds" in query:
        reduced["seeds"] = sorted(set(query["seeds"]) & (outpoints | endpoints))
    else:
        reduced["seeds"] = sorted(key for key in outpoints | endpoints if key.startswith(query["txid"] + ":"))
    edge_ids = {"out:" + key for key in outpoints | endpoints}
    edge_ids.update(f"in:{state['links'][key]['spending_txid']}:{state['links'][key]['vin']}" for key in outpoints)
    graph = build_graph(reduced, merge_addresses=True, include_fees=False,
                        color_attribution_arrows=color_attribution_arrows, center_name=center_name,
                        edge_ids=edge_ids)
    node_ids = {edge[field] for edge in graph["edges"] for field in ("source", "target")}
    graph["nodes"] = [node for node in graph["nodes"] if node["id"] in node_ids]
    graph["fee_items"] = {}
    graph["layout"] = arrange({node["id"]: node for node in graph["nodes"]}, graph["edges"], reduced["transactions"], {})
    graph["activity_frames"] = activity_frames(graph)
    scope = SEED_SCOPE if "seeds" in query else SCOPE
    extra_endpoints = query.get("include_unspent") or query.get("include_unspendable")
    if query.get("include_unspent"):
        scope += (" Unspent endpoints require a saved unspent observation with no saved spending input; "
                  "this is their status at observation, not a current balance. Unchecked or bounded outputs "
                  "are not treated as unspent.")
    if query.get("include_unspendable"):
        scope += " Unspendable endpoints are proven by the saved output script; fees are excluded."
    origin_notice = ("each seed transaction is hop 0, with only its selected outputs starting a path. "
                     if "seeds" in query else "the chosen transaction is hop 0. ")
    graph["pegouts"] = {"schema_version": 1, "query": query, "matches": matches,
                        "match_count": len(matches), "outpoints": sorted(outpoints),
                        "transaction_count": len(selected), "scope": scope,
                        "source_run_status": state.get("status"), "source_stop_reason": state.get("stop_reason"),
                        "status": "pegouts_found" if matches else "no_pegout_found"}
    if extra_endpoints:
        counts = {kind: sum(match["kind"] == kind for match in endpoint_matches)
                  for kind in ("pegout", "unspent", "unspendable")}
        graph["pegouts"].update(endpoint_matches=endpoint_matches, endpoint_count=len(endpoint_matches),
                               endpoint_counts=counts)
        if not matches:
            graph["pegouts"]["status"] = "endpoints_found" if endpoint_matches else "no_endpoints_found"
    graph["graph_options"].update(view="pegout_paths", pegout_query=deepcopy(query))
    summary = f"{len(matches)} peg-out request(s)"
    if query.get("include_unspent"):
        summary += f", {counts['unspent']} observed unspent UTXO(s)"
    if query.get("include_unspendable"):
        summary += f", {counts['unspendable']} unspendable output(s)"
    graph["notice"] = (summary + f" found within {query['min_hops']} to {query['max_hops']} "
                       "transaction hops, inclusive; " + origin_notice + scope +
                       " One circle per full address per network; UTXO occurrences and connectors remain separate. "
                       "Every displayed edge belongs to a qualifying path; "
                       "their union may also form routes outside the selected range. "
                       "UTXO reachability does not prove ownership or allocate confidential values.")
    return graph
