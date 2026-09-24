"""Presentation dependency resets at explicitly selected Liquid address hubs.

Each exact displayed spend through a selected hub may start a new layout tree.
The transaction evidence, saved dependency columns, addresses, and connectors
remain unchanged. Other inputs retain their ordinary transaction dependencies.
"""

from collections import defaultdict
from statistics import median

from .layout import transaction_ranks


_VIEW_MARKER = object()


def _outpoint(vin):
    parent, index = vin.get("txid"), vin.get("vout")
    if not isinstance(parent, str) or not parent or type(index) is not int or index < 0:
        return None
    return f"{parent}:{index}"


def hub_plan(graph):
    """Return layout columns and exact vin exemptions without mutating graph.

    Only a canonical input, its displayed hub source, and the exact displayed
    parent output together establish a reset. An ambiguous or missing match stays
    an ordinary dependency. The rank pass reuses the iterative cycle handling of
    the initial dependency layout and does not traverse address-level value flows.
    """
    if graph.get("_hub_layout_view") is _VIEW_MARKER:
        return graph["_hub_layout_plan"]
    fees = {key for key, item in graph.get("fee_items", {}).items()
            if item.get("endpoint") == "shapes"}
    nodes = {node["id"]: node for node in graph["nodes"] if node["id"] not in fees}
    hubs = {key for key, node in nodes.items()
            if node["kind"] == "address" and node.get("layout_hub") is True
            and node.get("details", {}).get("network", "liquid") == "liquid"}
    plan = {"columns": {}, "cut_inputs": set(), "hubs": sorted(hubs), "roots": {}}
    if not hubs:
        return plan

    transactions = {key: node for key, node in nodes.items() if node["kind"] == "transaction"}
    inputs, outputs = defaultdict(list), defaultdict(list)
    incoming, outgoing = defaultdict(set), defaultdict(set)
    for edge in graph["edges"]:
        source, target = edge["source"], edge["target"]
        if source not in nodes or target not in nodes:
            continue
        incoming[target].add(source)
        outgoing[source].add(target)
        if target in transactions:
            inputs[edge["id"]].append(edge)
        if source in transactions:
            outputs[source, edge.get("outpoint")].append(edge)

    roots = {key: set() for key in hubs}
    records = {}
    for key, node in transactions.items():
        kept = []
        transaction = node.get("details", {}).get("transaction", {})
        for index, vin in enumerate(transaction.get("vin", [])):
            if vin.get("is_pegin") or vin.get("is_coinbase"):
                continue
            parent = "tx:" + str(vin.get("txid", ""))
            outpoint = _outpoint(vin)
            child_txid = key.removeprefix("tx:")
            matches = inputs.get(f"in:{child_txid}:{index}", ())
            hub = None
            if outpoint is not None and len(matches) == 1:
                edge = matches[0]
                edge_vin = edge.get("details", {}).get("vin", {})
                if (edge["target"] == key and edge["source"] in hubs
                        and edge.get("outpoint") == outpoint
                        and _outpoint(edge_vin) == outpoint
                        and not edge_vin.get("is_pegin") and not edge_vin.get("is_coinbase")):
                    hub = edge["source"]
                    roots[hub].add(key)
            previous = outputs.get((parent, outpoint), ())
            if (hub is not None and parent in transactions and len(previous) == 1
                    and previous[0]["target"] == hub
                    and previous[0]["id"] == "out:" + outpoint):
                plan["cut_inputs"].add((key, index))
            elif parent in transactions:
                # Every non-cut vin remains independently represented. Another
                # input from this same parent can still prevent a rank reset.
                kept.append({"txid": parent})
        records[key] = {"data": {"vin": kept}}

    columns = {key: node["column"] for key, node in nodes.items()}
    if plan["cut_inputs"]:
        ranks, _ = transaction_ranks(records)
        columns.update({key: 2 * rank + 1 for key, rank in ranks.items()})
        for key, node in nodes.items():
            if key in transactions or key in hubs:
                continue
            before = sorted(columns[parent] for parent in incoming[key] if parent in transactions)
            after = sorted(columns[child] for child in outgoing[key] if child in transactions)
            if before and after:
                lower, upper = max(before) + 1, min(after) - 1
                columns[key] = ((lower + upper) // 2 if lower <= upper
                                else round(median(before + after)))
            elif before:
                columns[key] = max(before) + 1
            elif after:
                columns[key] = min(after) - 1
    # The original floor keeps established hub lanes stable. Include rebased
    # columns so a filtered graph with a late original rank still starts left.
    first_column = min([node["column"] for node in nodes.values()]
                       + [column for key, column in columns.items() if key not in hubs])
    columns.update({key: first_column - 1 for key in hubs})
    plan.update(columns=columns, roots={key: sorted(roots[key]) for key in sorted(hubs)})
    return plan


def hub_layout_view(graph):
    """Return an ephemeral column view for ordering and geometry preferences.

    Callers must return/persist their original graph or candidate, never this view.
    The internal plan avoids repeating the dependency analysis in nested helpers.
    """
    if graph.get("_hub_layout_view") is _VIEW_MARKER:
        return graph
    plan = hub_plan(graph)
    if not plan["hubs"]:
        return graph
    return {**graph, "_hub_layout_view": _VIEW_MARKER, "_hub_layout_plan": plan,
            "nodes": [{**node, "column": plan["columns"].get(node["id"], node["column"])}
                      for node in graph["nodes"]]}
