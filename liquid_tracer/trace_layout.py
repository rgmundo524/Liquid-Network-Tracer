"""Opt-in trace-first presentation without changing evidence or node identity.

A spine is one exact displayed outpoint path, not a claim that one input funds
one output. Other branches and every shared address remain visible. All helpers
are iterative and bounded by sorting the displayed nodes and edges.
"""

from collections import defaultdict
from math import hypot
from statistics import median

from .hub_layout import hub_layout_view, hub_plan
from .named_group_layout import selected_members


TRACE_LAYOUT_VERSION = 5
SPINE_STRAIGHTNESS = 96
TERMINAL_STRAIGHTNESS = 16


class _Groups:
    def __init__(self, keys):
        self.parent = {key: key for key in keys}

    def find(self, key):
        root = key
        while self.parent[root] != root:
            root = self.parent[root]
        while key != root:
            following = self.parent[key]
            self.parent[key] = root
            key = following
        return root

    def join(self, left, right):
        left, right = self.find(left), self.find(right)
        if left != right:
            self.parent[max(left, right)] = min(left, right)


def trace_structure(graph):
    """Return a deterministic spine and branch membership for layout only.

    Both sides of a continuation must match the same displayed address and exact
    outpoint. Duplicate producers/spenders and backward dependencies stay visible
    but cannot select the spine. Seed-connected paths take precedence over unrelated
    context. A requested name and explicit change designations break path choices
    before ordinary exact continuations, traced-edge counts and length. Repeated
    transfers through a many-to-many address do not make it the visual backbone;
    that one address stays peripheral. This is presentation, not ownership.
    """
    enabled = graph.get("graph_options", {}).get("layout_style") == "trace"
    empty = {"enabled": enabled, "core": set(), "members": set(), "transactions": set(),
             "edges": set(), "excluded_hubs": set(), "spine": [], "terminal_pairs": [],
             "terminal_edges": set(), "branches": [], "peripheral": set(), "shared_hubs": set(),
             "selected_name": bool(graph.get("graph_options", {}).get("center_name")),
             "matched_addresses": 0, "terminal_rows": []}
    if not enabled:
        return empty
    graph = hub_layout_view(graph)
    fees = {key for key, item in graph.get("fee_items", {}).items() if item.get("endpoint") == "shapes"}
    nodes = {node["id"]: node for node in graph["nodes"] if node["id"] not in fees}
    txs = {key for key, node in nodes.items() if node["kind"] == "transaction"}
    hubs = {key for key, node in nodes.items() if node.get("layout_hub") is True}
    named = selected_members(graph) - hubs
    empty.update(excluded_hubs=hubs, matched_addresses=len(named))
    incoming, outgoing, incident = defaultdict(list), defaultdict(list), defaultdict(list)
    outputs, inputs = defaultdict(list), defaultdict(list)
    for edge in sorted(graph["edges"], key=lambda edge: edge["id"]):
        source, target = edge["source"], edge["target"]
        if source not in nodes or target not in nodes:
            continue
        outgoing[source].append(edge)
        incoming[target].append(edge)
        incident[source].append(edge)
        incident[target].append(edge)
        outpoint = edge.get("outpoint")
        if not isinstance(outpoint, str) or not outpoint:
            continue
        if source in txs and nodes[target]["kind"] == "address" and target not in hubs:
            outputs[target, outpoint].append(edge)
        vin = edge.get("details", {}).get("vin", {})
        if (target in txs and nodes[source]["kind"] == "address" and source not in hubs
                and not vin.get("is_pegin") and not vin.get("is_coinbase")):
            inputs[source, outpoint].append(edge)
    shared_hubs = {key for key, node in nodes.items() if node["kind"] == "address"
                   and len({edge["source"] for edge in incoming[key] if edge["source"] in txs}) > 1
                   and len({edge["target"] for edge in outgoing[key] if edge["target"] in txs}) > 1}
    empty.update(shared_hubs=shared_hubs, excluded_hubs=hubs | shared_hubs)
    arcs, predecessors = [], defaultdict(list)
    for key in sorted(outputs.keys() & inputs.keys()):
        if len(outputs[key]) != 1 or len(inputs[key]) != 1:
            continue
        before, after = outputs[key][0], inputs[key][0]
        parent, child = before["source"], after["target"]
        if parent == child or nodes[parent]["column"] >= nodes[child]["column"]:
            continue
        arc = (parent, child, key[0], before["id"], after["id"],
               bool(before.get("change_output") or after.get("change_output")),
               after.get("role") == "traced_input" or bool(after.get("details", {}).get("validated_trace_link")))
        arcs.append(arc)
        predecessors[child].append(arc)
    roots = {key for key in txs if nodes[key].get("role") == "starting_transaction"
             or nodes[key].get("starting_transaction_index") is not None or nodes[key].get("is_starting") is True}
    roots.update(key for key in txs if any(edge.get("role") == "seed_output" for edge in outgoing[key]))
    named_contacts = {key: len({edge["source"] for edge in incoming[key] if edge["source"] in named}
                              | {edge["target"] for edge in outgoing[key] if edge["target"] in named}) for key in txs}
    scores, previous = {}, {}
    for key in sorted(txs, key=lambda key: (nodes[key]["column"], key)):
        if not roots or key in roots:
            scores[key] = (named_contacts[key], 0, 0, 0, 1)
        for arc in predecessors[key]:
            parent = arc[0]
            if parent not in scores:
                continue
            old = scores[parent]
            score = (old[0] + named_contacts[key], old[1] + int(arc[5]),
                     old[2] + int(arc[2] not in shared_hubs), old[3] + int(arc[6]), old[4] + 1)
            if key not in scores or score > scores[key]:
                scores[key], previous[key] = score, arc
    if not scores:
        empty["branches"] = [{key} for key in sorted(nodes)]
        return empty
    end = min(scores, key=lambda key: (tuple(-value for value in scores[key]), key))
    spine, spine_edges = [end], set()
    cursor = end
    while cursor in previous:
        arc = previous[cursor]
        spine.extend((arc[2], arc[0]))
        spine_edges.update((arc[3], arc[4]))
        cursor = arc[0]
    spine.reverse()

    def boundary(edges, field):
        choices = [edge for edge in edges if edge[field] not in hubs | shared_hubs
                   and nodes[edge[field]]["kind"] != "transaction"
                   and (edge[field] in named or len(incident[edge[field]]) == 1)
                   and not edge.get("details", {}).get("vin", {}).get("is_pegin")
                   and not edge.get("details", {}).get("vin", {}).get("is_coinbase")]
        return min(choices, key=lambda edge: (edge[field] not in named,
                   not bool(edge.get("change_output")), edge.get("role") == "context_input",
                   nodes[edge[field]]["kind"] != "address", edge["id"])) if choices else None

    first, last = boundary(incoming[spine[0]], "source"), boundary(outgoing[spine[-1]], "target")
    if first:
        spine.insert(0, first["source"])
        spine_edges.add(first["id"])
    if last and last["target"] not in spine:
        spine.append(last["target"])
        spine_edges.add(last["id"])
    # A reused address can appear in several exact continuations; one display
    # object still appears once in the core/order and is never duplicated.
    spine = list(dict.fromkeys(key for key in spine if key not in shared_hubs))
    core = set(spine)
    spine_edges = {edge["id"] for edge in graph["edges"] if edge["id"] in spine_edges
                   and edge["source"] in core and edge["target"] in core}
    terminal_pairs, terminal_edges = [], set()
    for key, node in sorted(nodes.items()):
        producers = {edge["source"] for edge in incoming[key]}
        if node["kind"] != "transaction" and len(producers) == 1 and producers <= txs and not outgoing[key]:
            terminal_pairs.append((next(iter(producers)), key))
            terminal_edges.update(edge["id"] for edge in incoming[key])
    # Build transaction branch components from exact continuations, then attach
    # exclusive local I/O. Shared context never joins otherwise separate lanes.
    groups = _Groups(txs - core)
    exact_edges = {edge for arc in arcs for edge in arc[3:5]}
    for parent, child, *_ in arcs:
        if parent not in core and child not in core:
            groups.join(parent, child)
    components = defaultdict(set)
    for key in sorted(txs - core):
        components[groups.find(key)].add(key)
    peripheral = (hubs | shared_hubs) - core
    for key in sorted(nodes.keys() - txs - core - hubs - shared_hubs):
        owners = {edge["source"] for edge in incoming[key] if edge["source"] in txs}
        owners.update(edge["target"] for edge in outgoing[key] if edge["target"] in txs)
        branch_ids = {groups.find(owner) for owner in owners - core}
        exact = all(edge["id"] in exact_edges for edge in incident[key])
        if len(branch_ids) == 1 and (len(owners) == 1 or exact):
            components[next(iter(branch_ids))].add(key)
        elif len(owners) > 1:
            peripheral.add(key)
        else:
            components[key].add(key)
    branches = [components[key] for key in sorted(components)]
    # Stack single-transaction end branches sharing an entry and a dependency
    # column. This only describes the displayed graph, never an inferred UTXO
    # status. Continuing branches stay intact; only explicit hubs reset depth.
    terminals = {source for source, _ in terminal_pairs}
    row_groups = defaultdict(list)
    for index, component in enumerate(branches):
        transactions = component & txs
        if len(transactions) != 1:
            continue
        transaction = next(iter(transactions))
        if transaction not in terminals:
            continue
        anchors = {edge["source"] for key in component for edge in incoming[key]
                   if edge["source"] not in component}
        if anchors:
            row_groups[(tuple(sorted(anchors)), nodes[transaction]["column"])].append((transaction, index))
    rows = [{"anchors": list(anchors), "transactions": [key for key, _ in sorted(items)],
             "components": [index for _, index in sorted(items)]}
            for (anchors, _), items in sorted(row_groups.items()) if len(items) > 1]
    return {**empty, "core": core, "members": core - txs, "transactions": core & txs,
            "edges": spine_edges, "spine": spine, "terminal_pairs": terminal_pairs,
            "terminal_edges": terminal_edges, "branches": branches,
            "peripheral": peripheral, "terminal_rows": rows}


def trace_order(graph, base_order=None, structure=None):
    """Place a central spine between compact branch bands and outer shared nodes."""
    structure = trace_structure(graph) if structure is None else structure
    if not structure["enabled"]:
        return None
    graph = hub_layout_view(graph)
    nodes = {node["id"]: node for node in graph["nodes"]}
    rank = {key: index for index, key in enumerate(base_order or sorted(nodes))}
    def ordered(keys):
        return sorted(keys, key=lambda key: (rank.get(key, len(rank)), key))
    sides, loads, totals = [[], []], [defaultdict(float), defaultdict(float)], [0., 0.]
    terminals = {target for _, target in structure["terminal_pairs"]}
    bundled = {index for row in structure["terminal_rows"] for index in row["components"]}
    units = [[structure["branches"][index] for index in row["components"]]
             for row in structure["terminal_rows"]]
    units.extend([component] for index, component in enumerate(structure["branches"]) if index not in bundled)
    for parts in sorted(units, key=lambda parts: (
            min(nodes[key]["column"] for part in parts for key in part),
            min(key for part in parts for key in part))):
        component = set().union(*parts)
        heights = defaultdict(float)
        for key in component:
            heights[nodes[key]["column"]] += nodes[key]["height"] + 80
        preferred = 0 if component & terminals else 1
        costs = [sum(abs(loads[side][column] + height - loads[1 - side][column])
                     for column, height in heights.items()) for side in (0, 1)]
        side = min((0, 1), key=lambda side: (costs[side], totals[side], side != preferred))
        # Keep the transaction and its own endpoint(s) in the same row order
        # across columns; don't split one terminal group around the main path.
        for part in parts:
            sides[side].extend(ordered(part))
        for column, height in heights.items():
            loads[side][column] += height
            totals[side] += height
    outside = [[], []]
    for key in ordered(structure["peripheral"]):
        side = min((0, 1), key=lambda side: (totals[side], side))
        outside[side].append(key)
        totals[side] += nodes[key]["height"] + 80
    order = outside[0] + sides[0] + ordered(structure["core"]) + sides[1] + outside[1]
    seen = set(order)
    order += ordered(nodes.keys() - seen)
    # An anchored hub belongs beside its exact entry, rather than in the outer
    # shared-address band. Retain the established branch order and insert hubs
    # as one deterministic block when an entry funds more than one of them.
    entries = hub_plan(graph).get("entries", {})
    by_entry = defaultdict(list)
    for hub, entry in sorted(entries.items()):
        if hub in nodes and entry in nodes:
            by_entry[entry].append(hub)
    anchored = {hub for hubs in by_entry.values() for hub in hubs}
    result = []
    for key in order:
        if key not in anchored:
            result.append(key)
            result.extend(by_entry.get(key, ()))
    return result


def trace_priorities(graph, structure=None):
    structure = trace_structure(graph) if structure is None else structure
    if not structure["enabled"]:
        return {}
    return {**{key: TERMINAL_STRAIGHTNESS for key in structure["terminal_edges"]},
            **{key: SPINE_STRAIGHTNESS for key in structure["edges"]}}


def trace_metrics(graph, structure=None):
    """Average geometry costs in typical node-spacing units, not graph-size sums."""
    structure = trace_structure(graph) if structure is None else structure
    graph = hub_layout_view(graph)
    nodes = {node["id"]: node for node in graph["nodes"]}
    scale = median([max(node["width"], node["height"]) + 80 for node in nodes.values()]) if nodes else 1
    core = [nodes[key] for key in structure["core"]]
    middle = median([node["y"] for node in core]) if core else 0
    drift = sum(abs(node["y"] - middle) for node in core) / (max(1, len(core)) * scale)
    distances = []
    for source, target in structure["terminal_pairs"]:
        a, b = nodes[source], nodes[target]
        dx = max(0, abs(a["x"] - b["x"]) - (a["width"] + b["width"]) / 2)
        dy = max(0, abs(a["y"] - b["y"]) - (a["height"] + b["height"]) / 2)
        distances.append(hypot(dx, dy) / scale)
    memberships = {key: index for index, group in enumerate(structure["branches"]) for key in group}
    columns = defaultdict(list)
    # Core/peripheral nodes can interrupt a branch's siblings too. They do not
    # add their own branch penalty, but must remain in the vertical scan.
    visible = set(memberships) | structure["core"] | structure["peripheral"]
    for key in visible:
        columns[nodes[key]["column"]].append((nodes[key]["y"], key, memberships.get(key)))
    extra_runs = 0
    for column in columns.values():
        runs, seen, previous = 0, set(), None
        for _, _, group in sorted(column):
            if group is not None and group != previous:
                runs += 1
            previous = group
            if group is not None:
                seen.add(group)
        extra_runs += runs - len(seen)
    row_drift = []
    for row in structure["terminal_rows"]:
        positions = [nodes[key]["x"] for key in row["transactions"]]
        center = median(positions)
        row_drift.extend(abs(value - center) / scale for value in positions)
    return {"version": TRACE_LAYOUT_VERSION, "enabled": structure["enabled"],
            "edge_count": len(graph["edges"]),
            "spine_nodes": len(core), "spine_transactions": len(structure["transactions"]),
            "matched_addresses": structure["matched_addresses"], "terminal_pairs": len(distances),
            "branch_count": len(structure["branches"]), "peripheral_nodes": len(structure["peripheral"]),
            "terminal_row_groups": len(structure["terminal_rows"]),
            "terminal_row_transactions": len(row_drift),
            "terminal_column_drift": round(sum(row_drift) / max(1, len(row_drift)), 6),
            "spine_alignment": round(drift, 6),
            "terminal_distance": round(sum(distances) / max(1, len(distances)), 6),
            "branch_interleaving": round(extra_runs / max(1, len(memberships)), 6)}
