"""Presentation preferences for compact branches and nearby transaction context.

Exact displayed UTXO continuations receive greater straightness weight than
external input context. These are soft layout preferences, never ownership
claims or changes to the graph's evidence. ELK still routes every connection.
"""

import math
from collections import defaultdict


BRANCH_LAYOUT_VERSION = 2


def hub_nodes(graph):
    """Explicit presentation hubs only; address activity never selects them."""
    return {node["id"] for node in graph["nodes"]
            if node["kind"] == "address" and node.get("layout_hub") is True}


def edge_priorities(graph):
    """Return ELK straightness weights, matching continuations by outpoint.

    An address reused by unrelated UTXOs is insufficient. Display cycles remain
    in the graph, but do not become forward transaction dependencies. An explicit
    change designation has priority over ordinary continuation preferences.
    """
    nodes = {node["id"]: node for node in graph["nodes"]}
    producers = defaultdict(list)
    priorities = {}
    for edge in graph["edges"]:
        source, target = nodes[edge["source"]], nodes[edge["target"]]
        priorities[edge["id"]] = 1
        if (source["kind"] == "transaction" and target["kind"] == "address"
                and isinstance(edge.get("outpoint"), str) and edge["outpoint"]):
            producers[edge["target"], edge["outpoint"]].append(edge)
            priorities[edge["id"]] = 12 if edge.get("change_output") else 2
    for edge in graph["edges"]:
        source, target = nodes[edge["source"]], nodes[edge["target"]]
        vin = edge.get("details", {}).get("vin", {})
        if (source["kind"] != "address" or target["kind"] != "transaction"
                or source.get("details", {}).get("network", "liquid") != "liquid"
                or vin.get("is_pegin") or vin.get("is_coinbase")):
            continue
        for previous in producers.get((edge["source"], edge.get("outpoint")), ()):
            parent = nodes[previous["source"]]
            if parent["id"] == target["id"] or parent["column"] >= target["column"]:
                continue
            weight = 12 if previous.get("change_output") else 8
            priorities[previous["id"]] = max(priorities[previous["id"]], weight)
            priorities[edge["id"]] = max(priorities[edge["id"]], weight)
    return priorities


def organization_metrics(graph, priorities=None):
    """Linear-time travel estimates used after the existing collision gates."""
    from .elk_layout import _default_attachments, attachment_point

    priorities = edge_priorities(graph) if priorities is None else priorities
    nodes = {node["id"]: node for node in graph["nodes"]}
    weighted, continuing, context = 0.0, 0.0, 0.0
    for edge in graph["edges"]:
        source, target = nodes[edge["source"]], nodes[edge["target"]]
        attachment = edge.get("attachment") or _default_attachments(source, target)
        route = [attachment_point(source, attachment["startItem"]),
                 attachment_point(target, attachment["endItem"])]
        if edge.get("connector_shape") in ("elbowed", "curved") and edge.get("route"):
            route = [route[0], *edge["route"][1:-1], route[-1]]
        vertical = sum(abs(b["y"] - a["y"]) for a, b in zip(route, route[1:]))
        priority = priorities.get(edge["id"], 1)
        weighted += vertical * priority
        if priority >= 8:
            continuing += vertical
        elif target["kind"] == "transaction":
            context += math.hypot(target["x"] - source["x"], target["y"] - source["y"])
    return {"weighted_vertical_travel": round(weighted, 2),
            "continuation_vertical_travel": round(continuing, 2),
            "context_distance": round(context, 2)}


def compact_context_inputs(graph):
    """Move isolated external inputs closer only when geometry proves it safe.

    Reuse the address compactor's route, caption, clearance and bounded-work
    checks. All transactions, output addresses, shared addresses, fee items and
    explicit change rows are locked. Each original context object stays visible.
    """
    # Lazy imports avoid the ELK/compaction adapter dependency cycle.
    from .compaction import _Budget, _compact_addresses, _footprint, _points

    nodes = {node["id"]: node for node in graph["nodes"]}
    edges = {edge["id"]: edge for edge in graph["edges"]}
    adjacent = defaultdict(list)
    for edge in edges.values():
        adjacent[edge["source"]].append(edge)
        adjacent[edge["target"]].append(edge)
    fee_ids = {key for key, item in graph.get("fee_items", {}).items()
               if item.get("endpoint") == "shapes"}
    locked = set(graph.get("layout", {}).get("change_outputs", {}).get("locked_nodes", []))
    eligible = set()
    for key, node in nodes.items():
        incident = adjacent[key]
        if (node["kind"] != "address" or node.get("layout_hub") is True
                or key in fee_ids or key in locked or len(incident) != 1):
            continue
        edge = incident[0]
        if edge["source"] == key and nodes[edge["target"]]["kind"] == "transaction":
            eligible.add(key)
    before = organization_metrics(graph)
    moved, truncated = 0, False
    if eligible:
        points = {key: _points(edge, nodes) for key, edge in edges.items()}
        bounds = _footprint(nodes.values(), edges.values(), points)
        budget = _Budget(len(nodes) + len(edges))
        # The existing compactor uses bounded spatial queries and refuses
        # uncertain moves; its budget never removes or limits graph objects.
        moved, _ = _compact_addresses(nodes, edges, points, adjacent, fee_ids, bounds,
                                       budget, lambda *_: None, set(nodes) - eligible)
        truncated = budget.truncated
    main = [node for key, node in nodes.items() if key not in fee_ids]
    layout = graph.setdefault("layout", {})
    if main:
        layout["main_top"] = min(node["y"] - node["height"] / 2 for node in main)
        layout["main_bottom"] = max(node["y"] + node["height"] / 2 for node in main)
    layout.setdefault("branch_organization", {}).update({
        "version": BRANCH_LAYOUT_VERSION, "context_candidates": len(eligible),
        "context_inputs_moved": moved, "context_checks_truncated": truncated,
        "context_before": before, "context_after": organization_metrics(graph),
        "miro_routes_exact": False,
    })
    return graph
