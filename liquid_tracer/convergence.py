"""Two independent presentation signals derived from saved starting lineages.

Input convergence uses verified UTXO spends. Address convergence groups recorded
receipts by exact address *after* lineage propagation, without feeding the union
back into any UTXO. Neither signal establishes value allocation or ownership.
"""

from collections import defaultdict, deque

from .common import TraceError, output_kind
from .services import is_service_stop


def _lineage_analysis(state, catalog):
    """Return input-merge records and per-outpoint origins, respecting boundaries."""
    if len(catalog) < 2:
        return {}, {}
    transactions = state["transactions"]
    outputs = state["outputs"]
    seeds = set(state["seeds"])
    indices = {entry["key"][3:]: entry["index"] for entry in catalog}
    root_bits = {txid: 1 << (index - 1) for txid, index in indices.items()}
    stop_addresses = {label["value"] for label in state["labels"] if is_service_stop(label)}
    incoming, children = defaultdict(list), defaultdict(set)
    indegree = {txid: 0 for txid in transactions}
    # A link is usable only when the exact saved spending input agrees. Context
    # inputs and distinct outputs sharing an address do not create links here.
    for key, link in state["links"].items():
        parent, _, index = key.rpartition(":")
        child, vin_index = link["spending_txid"], link["vin"]
        if parent not in transactions or child not in transactions or key not in outputs:
            raise TraceError("Convergence requires complete saved spend evidence")
        vins = transactions[child]["data"]["vin"]
        if (type(vin_index) is not int or not 0 <= vin_index < len(vins)
                or vins[vin_index].get("is_pegin") or vins[vin_index].get("is_coinbase")
                or f'{vins[vin_index].get("txid")}:{vins[vin_index].get("vout")}' != key):
            raise TraceError("Convergence spend link does not match its saved input")
        if not index.isdecimal() or int(index) >= len(transactions[parent]["data"]["vout"]):
            raise TraceError("Convergence funding output is unavailable")
        incoming[child].append(key)
        if child not in children[parent]:
            children[parent].add(child)
            indegree[child] += 1
    by_tx = defaultdict(list)
    for key, item in outputs.items():
        by_tx[item["txid"]].append(key)
    origins = {key: root_bits.get(key.rpartition(":")[0], 0) for key in seeds}
    ready = deque(sorted(key for key, count in indegree.items() if count == 0))
    result, visited = {}, 0

    def allowed(key):
        item = outputs[key]
        output = transactions[item["txid"]]["data"]["vout"][item["vout"]]
        return output_kind(output) == "spendable" and output.get("scriptpubkey_address") not in stop_addresses

    while ready:
        txid = ready.popleft()
        visited += 1
        contributing = [(key, origins.get(key, 0)) for key in sorted(incoming[txid])
                        if origins.get(key, 0) and allowed(key)]
        input_bits = 0
        for _, bits in contributing:
            input_bits |= bits
        own = root_bits.get(txid, 0)
        groups = {bits for _, bits in contributing}
        if own:
            groups.add(own)
        combined = input_bits | own
        # Do not mark every descendant carrying an already-merged lineage. Two
        # identical inherited origin sets do not introduce a new interaction.
        if len(groups) > 1 and combined.bit_count() > 1:
            origin_numbers = _numbers(combined)
            result["tx:" + txid] = {
                "starting_transaction_indices": origin_numbers,
                "starting_transaction_keys": [entry["key"] for entry in catalog if entry["index"] in origin_numbers],
                "inputs": [{"outpoint": key, "starting_transaction_indices": _numbers(bits)} for key, bits in contributing],
                "own_starting_transaction_index": indices.get(txid),
                "basis": "Distinct starting lineages meet through verified saved UTXO spends; not value allocation or ownership.",
            }
        for key in by_tx[txid]:
            # Own-root provenance begins at selected outputs, never its context
            # siblings. Incoming provenance can reach all actually tracked outputs.
            bits = input_bits | (own if key in seeds else 0)
            if bits:
                origins[key] = bits
        for child in sorted(children[txid]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if visited != len(transactions):
        raise TraceError("Saved spend evidence contains a cycle; convergence cannot be established")
    return result, origins


def transaction_convergences(state, catalog):
    """Compatibility entry point: return only the verified input-merge signal."""
    return _lineage_analysis(state, catalog)[0]


def _numbers(bits):
    result = []
    while bits:
        low = bits & -bits
        result.append(low.bit_length())
        bits ^= low
    return result


def branch_interactions(state, catalog):
    """Compute input merges and retroactive shared-address receipts separately.

    Scan every permitted tracked receipt, including seeds and stopped arrivals.
    Do not query address history, count unselected siblings/context inputs, or
    propagate a shared address's combined origins to its unrelated UTXOs. Receipt
    records are stored once per address; senders reference only their own outputs
    so an address with many deposits does not create quadratic metadata copies.
    """
    transactions, origins = _lineage_analysis(state, catalog)
    receipts = defaultdict(list)
    for key in sorted(origins):
        bits = origins[key]
        if not bits or key not in state["outputs"]:
            continue
        item = state["outputs"][key]
        txid, vout = item["txid"], item["vout"]
        if txid not in state["transactions"]:
            continue  # A bounded run may not yet have fetched a selected start.
        outputs = state["transactions"][txid]["data"]["vout"]
        if (type(vout) is not int or not 0 <= vout < len(outputs)
                or key != f"{txid}:{vout}"):
            raise TraceError("Address convergence requires an exact saved output")
        output = outputs[vout]
        address = output.get("scriptpubkey_address")
        if (output_kind(output) != "spendable" or not isinstance(address, str)
                or not address):
            continue
        receipts[address].append((key, txid, vout, bits))

    addresses, senders = {}, defaultdict(list)
    for address, entries in sorted(receipts.items()):
        groups = {entry[3] for entry in entries}
        combined = 0
        for bits in groups:
            combined |= bits
        # Like input merges, repeating one already-merged lineage is not a new
        # intersection. There must be different observed incoming origin sets.
        if len(groups) < 2 or combined.bit_count() < 2:
            continue
        address_key = "liquid:address:" + address
        indices = _numbers(combined)
        by_sender = defaultdict(list)
        rows = []
        for key, txid, vout, bits in entries:
            row = {"outpoint": key, "transaction_key": "tx:" + txid, "vout": vout,
                   "starting_transaction_indices": _numbers(bits)}
            rows.append(row)
            by_sender["tx:" + txid].append(row)
        addresses[address_key] = {
            "address_key": address_key, "network": "liquid", "address": address,
            "starting_transaction_indices": indices,
            "receipts": rows,
            "basis": "Different starting lineages have recorded receipts at this exact address; "
                     "not a joint spend, value allocation, or ownership claim.",
        }
        for txkey, rows in sorted(by_sender.items()):
            # This includes the first sender, not just the later arrival. Never
            # mark all of its ancestors, co-inputs, or unrelated sibling outputs.
            senders[txkey].append({
                "address_key": address_key, "address": address,
                "sent_starting_transaction_indices": sorted({i for row in rows
                                                            for i in row["starting_transaction_indices"]}),
                "outpoints": [row["outpoint"] for row in rows],
            })
    return {"transactions": transactions, "addresses": addresses, "senders": dict(senders)}


def annotate_branch_interactions(graph, state):
    """Decorate a freshly generated graph; never rewrite a saved trace or edges."""
    result = branch_interactions(state, graph["activity_frames"]["starting_transactions"])
    graph["address_convergences"] = result["addresses"]
    for node in graph["nodes"]:
        key, kind = node["id"], node["kind"]
        markers, types = [], []
        if kind == "transaction":
            if key in result["transactions"]:
                node["convergence"] = result["transactions"][key]
                node["details"]["convergence"] = node["convergence"]
                markers.append("INPUT MERGE")
                types.append("input_merge")
            if key in result["senders"]:
                node["address_interactions"] = result["senders"][key]
                node["details"]["address_interactions"] = node["address_interactions"]
                markers.append("SHARED ADDRESS")
                types.append("shared_address_sender")
        elif kind == "address" and node["details"].get("network") == "liquid":
            address_key = "liquid:address:" + (node["details"].get("address") or "")
            record = result["addresses"].get(address_key)
            if record:
                # All legacy occurrences also share this summary. Full receipts
                # live once in graph.address_convergences, not in every node.
                node["address_convergence"] = {
                    "address_key": address_key,
                    "starting_transaction_indices": record["starting_transaction_indices"],
                    "receipt_count": len(record["receipts"]),
                }
                node["details"]["address_convergence"] = node["address_convergence"]
                markers.append("SHARED ADDRESS")
                types.append("shared_address_receipts")
        if markers:
            node["interaction_types"] = types
            lines = node["label"].splitlines()
            if kind == "transaction":
                # Keep chronological Starting TX numbers at the top of the box.
                lines[1:1] = markers
            else:
                lines[0:0] = markers
            node["label"] = "\n".join(lines)
