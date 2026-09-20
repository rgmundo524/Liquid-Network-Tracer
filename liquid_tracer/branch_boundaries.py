"""Presentation preferences for branches that meet at visible boundaries.

Membership is supplied by verified saved-UTXO analysis, never reconstructed by
walking the displayed address graph. These helpers only choose order and measure
geometry. They do not add evidence, move objects, or change connections.
"""

import math
from bisect import bisect_left
from collections import Counter, defaultdict


def _structure(graph):
    """Read optional lineage metadata; old or malformed exports stay neutral."""
    metadata = graph.get("branch_structure")
    if (not isinstance(metadata, dict) or type(metadata.get("version")) is not int
            or metadata.get("version") != 1):
        return None
    nodes = {node["id"]: node for node in graph["nodes"]}
    edges = {edge["id"]: edge for edge in graph["edges"]}
    roots = metadata.get("roots")
    memberships = metadata.get("node_memberships")
    edge_memberships = metadata.get("edge_memberships")
    if not isinstance(roots, list) or not isinstance(memberships, dict) or not isinstance(edge_memberships, dict):
        return None
    root_keys, indices, seen_roots = [], set(), set()
    for root in roots:
        if not isinstance(root, dict):
            return None
        key, index = root.get("key"), root.get("index")
        if (not isinstance(key, str) or not key
                or (key in nodes and nodes[key].get("kind") != "transaction")
                or type(index) is not int or index < 1 or index in indices or key in seen_roots):
            return None
        root_keys.append(key)
        seen_roots.add(key)
        indices.add(index)
    root_keys = [root["key"] for root in sorted(roots, key=lambda root: root["index"])]
    catalog = {key: index for index, key in enumerate(root_keys)}
    # Intern membership tuples so repeated mixed-lineage trunks share their
    # representation and edge comparisons do not rescan all their roots.
    groups = {}
    def read(values, keys):
        found = {}
        for key, members in values.items():
            # Connection snapshots and grouped context views retain the
            # verified metadata from the complete graph. Offscreen objects do
            # not participate in ordering or geometry measurement.
            if key not in keys:
                continue
            if (not isinstance(members, list)
                    or any(not isinstance(root, str) or root not in catalog for root in members)
                    or len(set(members)) != len(members)):
                return None
            group = tuple(sorted(members, key=catalog.__getitem__))
            found[key] = groups.setdefault(group, group)
        return found
    members = read(memberships, nodes)
    edge_members = read(edge_memberships, edges)
    if members is None or edge_members is None:
        return None
    fees = {key for key, value in graph.get("fee_items", {}).items() if value.get("endpoint") == "shapes"}
    excluded = fees | {key for key, node in nodes.items() if node.get("layout_hub") is True}
    members = {key: value for key, value in members.items() if key not in excluded and value}
    exclusive = {key: value[0] for key, value in members.items() if len(value) == 1}
    if len(set(exclusive.values())) < 2:
        return None
    used_roots = {root for group in members.values() for root in group}
    root_keys = [key for key in root_keys if key in used_roots]
    return nodes, edges, root_keys, members, edge_members, excluded, exclusive


def _lane_order(roots, members):
    """Prefer adjacent interacting roots without constructing root cliques."""
    catalog = {key: index for index, key in enumerate(roots)}
    neighbors = defaultdict(Counter)
    # A k-root interaction contributes a path of k-1 links, not k*(k-1)/2.
    for group, weight in Counter(members.values()).items():
        for first, second in zip(group, group[1:]):
            neighbors[first][second] += weight
            neighbors[second][first] += weight
    choices = {key: sorted(values, key=lambda other: (-values[other], catalog[other]))
               for key, values in neighbors.items()}
    ordered, seen = [], set()
    for start in roots:
        current = start
        while current not in seen:
            ordered.append(current)
            seen.add(current)
            current = next((other for other in choices.get(current, ()) if other not in seen), current)
    return {key: index for index, key in enumerate(ordered)}


def _contacts(nodes, edges, members, edge_members, exclusive):
    """Trace-bearing contacts of a single-branch object with a shared object."""
    contacts = defaultdict(list)
    group_sets = {group: frozenset(group) for group in set(members.values())}
    edge_sets = {group: frozenset(group) for group in set(edge_members.values())}
    for key, edge in edges.items():
        provenance = edge_members.get(key, ())
        if not provenance:
            continue
        for first, second in ((edge["source"], edge["target"]), (edge["target"], edge["source"])):
            root = exclusive.get(first)
            others = members.get(second, ())
            if (root is not None and nodes[first].get("kind") in ("transaction", "address")
                    and root in edge_sets[provenance] and others
                    and (len(others) > 1 or root not in group_sets[others])):
                contacts[first].append(second)
    return contacts


def _upstream_affinity(nodes, edges, edge_members, exclusive, pulls):
    """Keep the path to a boundary near that edge using exact UTXO matches.

    An address identity by itself cannot transfer this preference between its
    unrelated inputs. Dependency columns bound the pass and exclude display
    cycles; every matching continuation is visited once.
    """
    producers = {}
    for key, edge in edges.items():
        source, target = edge["source"], edge["target"]
        root = exclusive.get(source)
        if (root is not None and nodes[source].get("kind") == "transaction"
                and exclusive.get(target) == root and nodes[target].get("kind") == "address"
                and edge_members.get(key) == (root,) and isinstance(edge.get("outpoint"), str)):
            pair = (target, edge["outpoint"])
            # Ambiguous producers must not create unbounded Cartesian joins.
            producers[pair] = source if pair not in producers else None
    incoming = defaultdict(list)
    sums = {key: [value, 1] for key, value in pulls.items()}
    for key, edge in edges.items():
        source, target = edge["source"], edge["target"]
        root = exclusive.get(target)
        parent = producers.get((source, edge.get("outpoint")))
        # A verified input merge is reached through a single-lineage input
        # address. Seed its producer using that exact input's outpoint, so the
        # contributing transaction reaches the facing boundary as well.
        if (source in pulls and parent is not None and nodes[source].get("kind") == "address"
                and exclusive.get(parent) == exclusive.get(source)
                and edge_members.get(key) == (exclusive[source],)
                and nodes[target].get("kind") == "transaction"
                and nodes[parent]["column"] < nodes[target]["column"]):
            entry = sums.setdefault(parent, [0.0, 0])
            entry[0] += pulls[source] * 0.85
            entry[1] += 1
        if (root is not None and parent is not None and exclusive.get(source) == root
                and exclusive.get(parent) == root and nodes[target].get("kind") == "transaction"
                and edge_members.get(key) == (root,)
                and nodes[parent]["column"] < nodes[target]["column"]):
            incoming[target].append((parent, source))
    transactions = sorted((key for key in exclusive if nodes[key].get("kind") == "transaction"),
                          key=lambda key: (nodes[key]["column"], key), reverse=True)
    for key in transactions:
        if key not in sums:
            continue
        value = sums[key][0] / sums[key][1]
        for parent, address in incoming.get(key, ()):
            for other, weight in ((parent, 0.85), (address, 0.95)):
                entry = sums.setdefault(other, [0.0, 0])
                entry[0] += value * weight
                entry[1] += 1
    result = {key: value / count for key, (value, count) in sums.items()}
    # Moving one ancestor toward a boundary must carry its ordinary sibling
    # continuations along. Otherwise its untouched outputs would cross the
    # neighboring subtrees that the parent has just passed. Inherit a weaker
    # affinity forwards once, retaining each child's own stronger boundary
    # preference. Multiple exact parents contribute an average, not first-wins.
    for key in reversed(transactions):
        if key in result:
            continue
        values = [result[parent] for parent, _ in incoming.get(key, ()) if parent in result]
        if values:
            result[key] = sum(values) / len(values) * 0.8
    addresses = defaultdict(list)
    for (address, _), parent in producers.items():
        if parent in result and address not in result:
            addresses[address].append(result[parent] * 0.9)
    for address, values in addresses.items():
        result[address] = sum(values) / len(values)
    return result


def branch_order(graph):
    """Return a full deterministic node order, or None without two core branches.

    Fee nodes and explicitly separated hubs do not acquire branch preferences.
    They remain in the returned order so callers can filter to their ELK nodes.
    A context object may follow its sole incident transaction for presentation;
    that affinity is local to this helper and never becomes lineage metadata.
    """
    structure = _structure(graph)
    if structure is None:
        return None
    nodes, edges, roots, members, edge_members, excluded, exclusive = structure
    lanes = _lane_order(roots, members)
    summaries = {group: (sum(lanes[root] for root in group), len(group), frozenset(group))
                 for group in set(members.values())}
    ranks = {}
    for key, group in members.items():
        total, count, _ = summaries[group]
        # Mixed lineages sit in a gap between lanes, not on another core's
        # centerline when their arithmetic mean happens to be an integer.
        ranks[key] = total / count if count == 1 else math.floor(total / count) + 0.5
    contacts = _contacts(nodes, edges, members, edge_members, exclusive)
    pulls = {}
    for key, neighbors in contacts.items():
        root = exclusive[key]
        own = lanes[root]
        target, count = 0.0, 0
        for other in neighbors:
            total, size, roots_in_group = summaries[members[other]]
            contains = root in roots_in_group
            target += (total - (own if contains else 0)) / (size - contains)
            count += 1
        direction = target / count - own
        pulls[key] = 0.35 if direction > 0 else -0.35 if direction < 0 else 0
    for key, value in _upstream_affinity(nodes, edges, edge_members, exclusive, pulls).items():
        ranks[key] = lanes[exclusive[key]] + value
    context_neighbors = defaultdict(set)
    for edge in edges.values():
        for first, second in ((edge["source"], edge["target"]), (edge["target"], edge["source"])):
            if (first not in members and first not in excluded
                    and nodes[first].get("kind") in ("address", "context_group")
                    and nodes[second].get("kind") == "transaction"):
                context_neighbors[first].add(second)
    for key, neighbors in context_neighbors.items():
        if len(neighbors) == 1:
            owner = next(iter(neighbors))
            if owner in ranks:
                ranks[key] = ranks[owner]
    def order(key):
        node = nodes[key]
        return (key not in ranks, ranks.get(key, 0), node.get("y", 0), node.get("column", 0), key)
    return sorted(nodes, key=order)


def boundary_metrics(graph):
    """Complete, inexpensive branch scores; no quadratic collision scan.

    Core interleaving counts foreign core objects between a branch's first and
    last objects within a dependency column. Boundary depth measures how far a
    joining object is buried from a facing edge/corridor. If its own column has
    no core objects, the nearest populated column supplies that branch's span.
    All edges and memberships are inspected, without sampling or truncation.
    """
    neutral = {"enabled": False, "interleavings": 0, "boundary_depth": 0.0,
               "interbranch_travel": 0.0, "truncated": False}
    structure = _structure(graph)
    if structure is None:
        return neutral
    nodes, edges, _, members, edge_members, _, exclusive = structure
    contacts = _contacts(nodes, edges, members, edge_members, exclusive)
    columns = defaultdict(list)
    spans = defaultdict(dict)
    for key, root in exclusive.items():
        if key in contacts:
            continue
        node = nodes[key]
        column, y, half = node["column"], node["y"], node["height"] / 2
        columns[column].append((y, key, root))
        old = spans[root].get(column)
        spans[root][column] = (y - half, y + half) if old is None else (min(old[0], y - half), max(old[1], y + half))
    interleavings = 0
    for values in columns.values():
        extent = {}
        for index, (_, _, root) in enumerate(sorted(values)):
            first, _, count = extent.get(root, (index, index, 0))
            extent[root] = (first, index, count + 1)
        interleavings += sum(last - first + 1 - count for first, last, count in extent.values())
    populated = {root: sorted(values) for root, values in spans.items()}
    def nearest(root, column):
        available = populated.get(root, ())
        if not available:
            return None
        index = bisect_left(available, column)
        adjacent = available[max(0, index - 1):index + 1]
        closest = min(adjacent, key=lambda value: (abs(value - column), value))
        return spans[root][closest]
    depth = 0.0
    for key, neighbors in contacts.items():
        node = nodes[key]
        span = nearest(exclusive[key], node["column"])
        if span is None:
            continue
        target = sum(nodes[other]["y"] for other in neighbors) / len(neighbors)
        middle = (span[0] + span[1]) / 2
        if target > middle:
            depth += max(0, span[1] - (node["y"] - node["height"] / 2))
        elif target < middle:
            depth += max(0, node["y"] + node["height"] / 2 - span[0])
        else:
            depth += min(max(0, span[1] - node["y"]), max(0, node["y"] - span[0]))
    for key, group in members.items():
        if len(group) < 2:
            continue
        node = nodes[key]
        intervals = sorted(span for root in group if (span := nearest(root, node["column"])) is not None)
        if len(intervals) < 2:
            continue
        half = node["height"] / 2
        best = math.inf
        for first, second in zip(intervals, intervals[1:]):
            low, high = first[1] + half, second[0] - half
            if low <= high:
                distance = max(low - node["y"], 0, node["y"] - high)
            else:
                distance = abs(node["y"] - (low + high) / 2) + (low - high) / 2
            best = min(best, distance)
        depth += best
    travel = 0.0
    for key, edge in edges.items():
        first, second = members.get(edge["source"]), members.get(edge["target"])
        if first and second and first is not second and edge_members.get(key):
            travel += abs(nodes[edge["source"]]["y"] - nodes[edge["target"]]["y"])
    return {"enabled": True, "interleavings": interleavings, "boundary_depth": round(depth, 2),
            "interbranch_travel": round(travel, 2), "truncated": False}
