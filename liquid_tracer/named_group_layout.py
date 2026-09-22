"""Arrange a selected attribution name through the middle of the full graph.

This is a presentation preference only. Membership comes from existing Liquid
address assessments. A connecting transaction must actually have both a named
input and a named output; no address-level traversal invents a value flow.
"""

from collections import defaultdict
from statistics import median


NAMED_GROUP_LAYOUT_VERSION = 1
CORE_STRAIGHTNESS = 64


def selected_members(graph):
    name = graph.get("graph_options", {}).get("center_name", "")
    if not isinstance(name, str) or not name.strip():
        return set()
    name = name.strip().casefold()
    selected = set()
    for node in graph["nodes"]:
        details = node.get("details", {})
        if node["kind"] != "address" or details.get("network", "liquid") != "liquid":
            continue
        for assessment in details.get("address_attributions", []):
            value = assessment.get("entity") or assessment.get("name")
            if isinstance(value, str) and value.strip().casefold() == name:
                selected.add(node["id"])
                break
    return selected


def group_structure(graph):
    """Return the displayed members and direct internal transaction structure."""
    selected = selected_members(graph)
    hubs = {node["id"] for node in graph["nodes"] if node.get("layout_hub") is True}
    members = selected - hubs
    nodes = {node["id"]: node for node in graph["nodes"]}
    incoming, outgoing = set(), set()
    for edge in graph["edges"]:
        source, target = nodes[edge["source"]], nodes[edge["target"]]
        vin = edge.get("details", {}).get("vin", {})
        if (source["id"] in members and target["kind"] == "transaction"
                and not vin.get("is_pegin") and not vin.get("is_coinbase")):
            incoming.add(target["id"])
        if source["kind"] == "transaction" and target["id"] in members:
            outgoing.add(source["id"])
    transactions = incoming & outgoing
    core = members | transactions
    edges = {edge["id"] for edge in graph["edges"]
             if (edge["source"] in members and edge["target"] in transactions
                 and not edge.get("details", {}).get("vin", {}).get("is_pegin")
                 and not edge.get("details", {}).get("vin", {}).get("is_coinbase"))
             or (edge["source"] in transactions and edge["target"] in members)}
    return {"members": members, "transactions": transactions, "core": core, "edges": edges,
            "excluded_hubs": selected & hubs}


def center_order(graph, base_order=None, structure=None):
    """Keep outside components together and balance them around the named core.

    Connected components after removing the core are layout neighborhoods, not
    tracing relationships. Their size is measured per dependency column, and
    they are assigned above/below the core with a deterministic greedy balance.
    ELK retains those vertical orders while routing every original connection.
    """
    structure = group_structure(graph) if structure is None else structure
    if not structure["members"]:
        return None
    fees = {key for key, item in graph.get("fee_items", {}).items() if item.get("endpoint") == "shapes"}
    nodes = {node["id"]: node for node in graph["nodes"] if node["id"] not in fees}
    core = structure["core"] & nodes.keys()
    rank = {key: index for index, key in enumerate(base_order or sorted(nodes))}
    adjacency = defaultdict(set)
    for edge in graph["edges"]:
        source, target = edge["source"], edge["target"]
        if source in nodes and target in nodes and source not in core and target not in core:
            adjacency[source].add(target)
            adjacency[target].add(source)
    remaining = set(nodes) - core
    components = []
    for start in sorted(remaining):
        if start not in remaining:
            continue
        remaining.remove(start)
        found, pending = {start}, [start]
        heights = defaultdict(float)
        while pending:
            key = pending.pop()
            node = nodes[key]
            heights[node["column"]] += node["height"] + 80
            for other in adjacency[key] & remaining:
                remaining.remove(other)
                found.add(other)
                pending.append(other)
        components.append((max(heights.values()), min(found), found, heights))
    sides = [[], []]
    loads = [defaultdict(float), defaultdict(float)]
    totals = [0.0, 0.0]
    for _, _, component, heights in sorted(components, key=lambda value: (-value[0], value[1])):
        costs = [sum(abs(loads[side][column] + height - loads[1 - side][column])
                     for column, height in heights.items()) for side in (0, 1)]
        side = min((0, 1), key=lambda value: (costs[value], totals[value], value))
        sides[side].append(component)
        for column, height in heights.items():
            loads[side][column] += height
            totals[side] += height
    def ordered(values):
        return sorted(values, key=lambda key: (rank.get(key, len(rank)), key))
    return ([key for component in sides[0] for key in ordered(component)]
            + ordered(core) + [key for component in reversed(sides[1]) for key in ordered(component)])


def center_metrics(graph, structure=None):
    """Measure central band drift after collision safety in candidate selection."""
    structure = group_structure(graph) if structure is None else structure
    nodes = {node["id"]: node for node in graph["nodes"]}
    columns, core_columns = defaultdict(list), defaultdict(list)
    fees = {key for key, item in graph.get("fee_items", {}).items() if item.get("endpoint") == "shapes"}
    for key, node in nodes.items():
        if key in fees:
            continue
        columns[node["column"]].append(node)
        if key in structure["core"]:
            core_columns[node["column"]].append(node)
    def midpoint(values):
        return (min(node["y"] - node["height"] / 2 for node in values)
                + max(node["y"] + node["height"] / 2 for node in values)) / 2
    centers = [midpoint(values) for values in core_columns.values()]
    middle = median(centers) if centers else 0
    return {"version": NAMED_GROUP_LAYOUT_VERSION,
            "matched_addresses": len(structure["members"]),
            "excluded_hubs": len(structure["excluded_hubs"]),
            "connecting_transactions": len(structure["transactions"]),
            "alignment_deviation": round(sum(abs(value - middle) for value in centers), 2),
            "center_offset": round(sum(abs(midpoint(values) - midpoint(columns[column]))
                                       for column, values in core_columns.items()), 2)}
