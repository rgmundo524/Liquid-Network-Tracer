"""Presentation dependency resets at explicitly selected Liquid address hubs.

Each exact displayed spend through a selected hub may start a new layout tree.
The transaction evidence, saved dependency columns, addresses, and connectors
remain unchanged. Trace layout anchors hubs after their first seed-connected
deposit; other inputs retain their ordinary transaction dependencies.
"""

from .networks import blockchain, is_primary
from collections import defaultdict, deque
from statistics import median

from .layout import transaction_ranks


_VIEW_MARKER = object()


def _outpoint(vin):
    parent, index = vin.get("txid"), vin.get("vout")
    if not isinstance(parent, str) or not parent or type(index) is not int or index < 0:
        return None
    return f"{parent}:{index}"


def _trace_entries(graph, transactions, outputs, children, records, hubs):
    """Find the first exact seed-connected deposit into each displayed hub.

    Walk transaction links, never a reused address's neighbors. Original ranks
    distinguish later returns from entry deposits. This single multi-source walk
    is iterative and does not rescan the graph once per seed or hub.
    """
    ranks, cycles = transaction_ranks(records)
    cyclic = {key for component in cycles for key in component}
    seeds = {key for key, node in transactions.items()
             if node.get("role") == "starting_transaction"
             or node.get("starting_transaction_index") is not None
             or node.get("is_starting") is True}
    seeds.update(edge["source"] for edge in graph["edges"]
                 if edge.get("role") == "seed_output" and edge["source"] in transactions)
    reached = set(seeds)
    pending = deque(seeds)
    while pending:
        parent = pending.popleft()
        for child in children[parent]:
            if child not in reached:
                reached.add(child)
                pending.append(child)
    entries = {}
    for (parent, outpoint), edges in outputs.items():
        if parent not in reached or parent in cyclic or len(edges) != 1 or not isinstance(outpoint, str):
            continue
        edge = edges[0]
        hub = edge["target"]
        if (hub not in hubs or edge["id"] != "out:" + outpoint
                or outpoint.rpartition(":")[0] != parent.removeprefix("tx:")):
            continue
        old = entries.get(hub)
        if old is None or (ranks[parent], parent) < (ranks[old], old):
            entries[hub] = parent
    return entries, ranks


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
            and is_primary(node, graph)}
    plan = {"columns": {}, "cut_inputs": set(), "hubs": sorted(hubs), "roots": {}}
    if not hubs:
        return plan

    transactions = {key: node for key, node in nodes.items() if node["kind"] == "transaction"}
    trace_enabled = graph.get("graph_options", {}).get("layout_style") == "trace"
    inputs, outputs, spenders = defaultdict(list), defaultdict(list), defaultdict(list)
    incoming, outgoing = defaultdict(set), defaultdict(set)
    for edge in graph["edges"]:
        source, target = edge["source"], edge["target"]
        if source not in nodes or target not in nodes:
            continue
        incoming[target].add(source)
        outgoing[source].add(target)
        if target in transactions:
            inputs[edge["id"]].append(edge)
            if trace_enabled:
                spenders[edge.get("outpoint")].append(edge)
        if source in transactions:
            outputs[source, edge.get("outpoint")].append(edge)

    roots = {key: set() for key in hubs}
    records, original_records = {}, {}
    exact_children = defaultdict(set)
    for key, node in transactions.items():
        kept, original = [], []
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
            if trace_enabled and parent in transactions:
                original.append({"txid": parent})
            # Reachability requires both exact displayed sides of this vin.
            # Another output at the same address cannot connect a seed to it.
            if (trace_enabled and outpoint is not None and parent in transactions
                    and len(matches) == len(previous) == len(spenders[outpoint]) == 1):
                edge, produced = matches[0], previous[0]
                edge_vin = edge.get("details", {}).get("vin", {})
                if (edge["target"] == key and edge.get("outpoint") == outpoint
                        and _outpoint(edge_vin) == outpoint
                        and not edge_vin.get("is_pegin") and not edge_vin.get("is_coinbase")
                        and produced["id"] == "out:" + outpoint
                        and produced["target"] == edge["source"]):
                    exact_children[parent].add(key)
            if (hub is not None and parent in transactions and len(previous) == 1
                    and previous[0]["target"] == hub
                    and previous[0]["id"] == "out:" + outpoint):
                plan["cut_inputs"].add((key, index))
            elif parent in transactions:
                # Every non-cut vin remains independently represented. Another
                # input from this same parent can still prevent a rank reset.
                kept.append({"txid": parent})
        records[key] = {"data": {"vin": kept}}
        if trace_enabled:
            original_records[key] = {"data": {"vin": original}}

    entries = {}
    if trace_enabled:
        entries, original_ranks = _trace_entries(
            graph, transactions, outputs, exact_children, original_records, hubs)
        forward_roots = {hub: sorted(children) for hub, children in roots.items()}
        for hub, entry in entries.items():
            # A hub can also fund earlier context or the entry transaction
            # itself. Those edges loop back; they must not create a new cycle.
            later = sorted(child for child in roots[hub]
                           if original_ranks[child] > original_ranks[entry])
            forward_roots[hub] = later
            for child in later:
                records[child]["data"]["vin"].append({"txid": entry})
        plan.update(entries=entries, forward_roots=forward_roots)

    columns = {key: node["column"] for key, node in nodes.items()}
    if plan["cut_inputs"] or entries:
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
    columns.update({key: columns[entries[key]] + 1 if key in entries else first_column - 1
                    for key in hubs})
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
