"""Convergence of distinct starting-transaction lineages through saved UTXOs.

Presentation only: never infer provenance from address reuse, names, timestamps,
or weak graph connectivity. A star indicates a topology interaction, not value
allocation, common ownership, or proof that confidential assets are identical.
"""

from collections import defaultdict, deque

from .common import TraceError, output_kind
from .services import is_service_stop


def transaction_convergences(state, catalog):
    if len(catalog) < 2:
        return {}
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

    def numbers(bits):
        values = []
        while bits:
            low = bits & -bits
            values.append(low.bit_length())
            bits ^= low
        return values

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
            origin_numbers = numbers(combined)
            result["tx:" + txid] = {
                "starting_transaction_indices": origin_numbers,
                "starting_transaction_keys": [entry["key"] for entry in catalog if entry["index"] in origin_numbers],
                "inputs": [{"outpoint": key, "starting_transaction_indices": numbers(bits)} for key, bits in contributing],
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
    return result
