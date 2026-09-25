"""Local input/transaction/output neighborhoods, for presentation only.

Seed lineage alone cannot keep later forks together. These helpers work on
displayed adjacency without asserting that unrelated UTXOs at a shared address
belong to one tracing branch. Shared nodes remain single objects; return edges,
fees, and explicitly separated hubs never become forward ordering constraints.
"""

from collections import defaultdict
from statistics import median
from .hub_layout import hub_layout_view


def _structure(graph):
    graph = hub_layout_view(graph)
    excluded = {key for key, item in graph.get("fee_items", {}).items()
                if item.get("endpoint") == "shapes"}
    excluded.update(node["id"] for node in graph["nodes"] if node.get("layout_hub") is True)
    nodes = {node["id"]: node for node in graph["nodes"] if node["id"] not in excluded}
    incoming, outgoing = defaultdict(set), defaultdict(set)
    producers, consumers = defaultdict(set), defaultdict(set)
    for edge in graph["edges"]:
        a, b = edge["source"], edge["target"]
        if a not in nodes or b not in nodes:
            continue
        # Count every producer/consumer before filtering return edges. A shared
        # address must not acquire exclusive ownership by hiding a return link.
        if nodes[a]["kind"] == "transaction":
            producers[b].add(a)
        if nodes[b]["kind"] == "transaction":
            consumers[a].add(b)
        if nodes[a]["column"] < nodes[b]["column"]:
            incoming[b].add(a)
            outgoing[a].add(b)
    owners = {}
    for key, node in nodes.items():
        if node["kind"] == "transaction":
            continue
        if len(producers[key]) == 1:
            owner = next(iter(producers[key]))
            if owner in incoming[key]:
                owners[key] = ("output", owner)
        elif not producers[key] and len(consumers[key]) == 1:
            owner = next(iter(consumers[key]))
            if owner in outgoing[key]:
                owners[key] = ("input", owner)
    return nodes, incoming, outgoing, owners


def _positions(columns, nodes):
    positions = {}
    for values in columns.values():
        height = sum(nodes[key]["height"] + 80 for key in values)
        y = 0.0
        for key in values:
            size = nodes[key]["height"] + 80
            positions[key] = (y + size / 2) / height
            y += size
    return positions


def _inversions(pairs):
    """Count different-source/target rank inversions without quadratic pairs."""
    targets = {value: index + 1 for index, value in enumerate(sorted({b for _, b in pairs}))}
    tree = [0] * (len(targets) + 1)
    seen, total = 0, 0
    grouped = defaultdict(list)
    for source, target in pairs:
        grouped[source].append(targets[target])
    for source in sorted(grouped):
        for rank in grouped[source]:
            count, index = 0, rank
            while index:
                count += tree[index]
                index -= index & -index
            total += seen - count
        for rank in grouped[source]:
            seen += 1
            while rank < len(tree):
                tree[rank] += 1
                rank += rank & -rank
    return total


def _order_score(columns, nodes, outgoing):
    positions = _positions(columns, nodes)
    pairs = defaultdict(list)
    travel = 0.0
    for source in sorted(outgoing):
        for target in sorted(outgoing[source]):
            pairs[nodes[source]["column"], nodes[target]["column"]].append(
                (positions[source], positions[target]))
            travel += abs(positions[source] - positions[target])
    return sum(_inversions(values) for values in pairs.values()), round(travel, 8)


def neighborhood_order(graph, base_order=None):
    """Sweep both ways, keeping each exclusive sibling group as one block.

    Forward sweeps carry every fork's order to its transactions and outputs;
    backward sweeps consider downstream joins and bring context inputs along.
    Merges contribute all their neighbors rather than choosing a false parent.
    A fixed number of O((V+E) log(V+E)) passes bounds work, not graph size.
    ELK still places and routes the complete graph using the preferred order.
    """
    nodes, incoming, outgoing, owners = _structure(graph)
    if not nodes:
        return None
    rank = {key: index for index, key in enumerate(base_order or sorted(
        nodes, key=lambda key: (nodes[key].get("y", 0), key)))}
    columns = defaultdict(list)
    for key in sorted(nodes, key=lambda key: (rank.get(key, len(rank)), key)):
        columns[nodes[key]["column"]].append(key)
    levels = sorted(columns)
    best, best_score = None, None
    for _ in range(4):
        for forward in (True, False):
            neighbors = incoming if forward else outgoing
            positions = _positions(columns, nodes)
            for column in levels if forward else reversed(levels):
                blocks = defaultdict(list)
                for key in columns[column]:
                    blocks[owners.get(key, ("node", key))].append(key)
                def affinity(key):
                    values = [positions[other] for other in sorted(neighbors[key])]
                    return sum(values) / len(values) if values else positions[key]
                affinities = {key: affinity(key) for key in columns[column]}
                groups = list(blocks.values())
                for group in groups:
                    group.sort(key=lambda key: (affinities[key], positions[key], rank.get(key, len(rank)), key))
                groups.sort(key=lambda group: (sum(affinities[key] for key in group) / len(group),
                                               min(positions[key] for key in group), group[0]))
                columns[column] = [key for group in groups for key in group]
                positions.update(_positions({column: columns[column]}, nodes))
            score = _order_score(columns, nodes, outgoing)
            if best_score is None or score < best_score:
                best_score = score
                best = [key for column in levels for key in columns[column]]
    # Include excluded objects to keep this a complete order for callers that
    # subsequently filter fees or apply named-group alignment.
    return best + sorted(node["id"] for node in graph["nodes"] if node["id"] not in nodes)


def neighborhood_metrics(graph):
    """Measure local interleaving and transaction proximity on every object.

    Compare endpoint ordering independently of ELK's saved routes. This catches
    reversed input/transaction rows even when bends conceal a crossing. Shared
    endpoints are not counted as an inversion. Inversions compare edges joining
    the same column pair, rather than all geometric crossings. These local
    counts are complete and bounded by sorting, so dense graphs do not turn
    truncated counts into a good score.
    """
    nodes, incoming, outgoing, owners = _structure(graph)
    from .named_group_layout import group_structure
    core = group_structure(graph)["core"]
    columns, pairs = defaultdict(list), defaultdict(list)
    for key, node in nodes.items():
        if key not in core:
            columns[node["column"]].append(key)
    interleavings = 0
    for keys in columns.values():
        extents = {}
        for index, key in enumerate(sorted(keys, key=lambda key: (nodes[key]["y"], key))):
            group = owners.get(key)
            if group is not None:
                first, _, count = extents.get(group, (index, index, 0))
                extents[group] = (first, index, count + 1)
        interleavings += sum(last - first + 1 - count for first, last, count in extents.values())
    distance, drift = 0.0, 0.0
    for key in sorted(nodes):
        node = nodes[key]
        for target in sorted(outgoing[key]):
            pairs[node["column"], nodes[target]["column"]].append((node["y"], nodes[target]["y"]))
        if node["kind"] != "transaction":
            continue
        # Equal weight per side: many outputs should not drown out the inputs.
        centers = []
        for neighbors in (incoming[key], outgoing[key]):
            if not neighbors:
                continue
            centers.append(median(nodes[other]["y"] for other in neighbors))
            distance += sum(((node["x"] - nodes[other]["x"]) ** 2
                             + (node["y"] - nodes[other]["y"]) ** 2) ** .5
                            for other in sorted(neighbors)) / len(neighbors)
        if centers:
            drift += abs(node["y"] - sum(centers) / len(centers))
    return {"sibling_interleavings": interleavings,
            "flow_order_inversions": sum(_inversions(values) for values in pairs.values()),
            "transaction_distance": round(distance, 2), "transaction_center_drift": round(drift, 2),
            "truncated": False}
