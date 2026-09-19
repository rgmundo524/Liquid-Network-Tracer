"""Estimate attachment conflicts independently of ELK's connector bends.

Miro chooses its own intermediate routes. Endpoint order therefore provides a
useful, deterministic comparison between layouts, but cannot predict the exact
number of crossings or overlapping segments that Miro will draw.
"""

from collections import Counter, defaultdict
from itertools import groupby


ATTACHMENT_ORDER_VERSION = 1
_EPS = 1e-6


def _inversions(rows):
    """Count strict inversions of local and opposite y coordinates in O(n log n).

    Equal local positions are scored separately as coincident ports. Equal
    opposite positions provide no geometric preference and are not inversions.
    Each incident endpoint pair is counted, so the same two edges may contribute
    at both of their common objects. These are not counts of actual crossings.
    """
    if len(rows) < 2:
        return 0
    ranks = {value: index + 1 for index, value in enumerate(sorted({row[1] for row in rows}))}
    tree = [0] * (len(ranks) + 1)
    total, seen = 0, 0
    for _, equal_local in groupby(sorted(rows), key=lambda row: row[0]):
        group = list(equal_local)
        # Query an entire equal-local group before inserting any of it.
        for _, opposite, _ in group:
            index, smaller_or_equal = ranks[opposite], 0
            while index:
                smaller_or_equal += tree[index]
                index -= index & -index
            total += seen - smaller_or_equal
        for _, opposite, _ in group:
            index = ranks[opposite]
            while index < len(tree):
                tree[index] += 1
                index += index & -index
            seen += 1
    return total


def attachment_order_metrics(graph):
    """Compare ports on each object's east/west perimeter without mutation.

    Only forward physical connections participate. Fee connectors, recorded
    returns, and connectors whose endpoints run backward are excluded because
    their routes intentionally use different lanes. Opposite *attachment*
    coordinates are used, rather than object centers, preserving distinctions
    between multiple UTXOs at one shared address.

    The pass uses O(E log E) time and O(E + V) memory, including for busy shared
    addresses. It does not limit or omit graph evidence.
    """
    # The ELK adapter consumes this metric, so import its geometry helpers lazily.
    from .elk_layout import _default_attachments, attachment_point

    nodes = {node["id"]: node for node in graph["nodes"]}
    fee_items = graph.get("fee_items", {})
    fee_nodes = {key for key, item in fee_items.items() if item.get("endpoint") == "shapes"}
    fee_edges = {key for key, item in fee_items.items() if item.get("endpoint") == "connectors"}
    sides, skipped = defaultdict(list), 0
    for edge in graph["edges"]:
        source, target = nodes[edge["source"]], nodes[edge["target"]]
        if (edge["id"] in fee_edges or source["id"] in fee_nodes or target["id"] in fee_nodes
                or edge.get("routing_exception") in ("return", "fee")):
            skipped += 1
            continue
        attachment = edge.get("attachment") or _default_attachments(source, target)
        start = attachment_point(source, attachment["startItem"])
        end = attachment_point(target, attachment["endItem"])
        if target["x"] <= source["x"] + _EPS or end["x"] <= start["x"] + _EPS:
            skipped += 1
            continue
        for node, local, opposite in ((source, start, end), (target, end, start)):
            # Circle and diamond ports occupy arcs, not vertical rectangle
            # edges. Their x position determines the physical hemisphere.
            offset = local["x"] - node["x"]
            if abs(offset) <= _EPS or (opposite["x"] - local["x"]) * offset <= 0:
                continue
            point = (round(local["x"], 6), round(local["y"], 6))
            side = "east" if offset > 0 else "west"
            sides[node["id"], side].append((point[1], round(opposite["y"], 6), point))
    inversions, coincident = 0, 0
    for rows in sides.values():
        inversions += _inversions(rows)
        coincident += sum(count * (count - 1) // 2 for count in Counter(row[2] for row in rows).values())
    return {
        "version": ATTACHMENT_ORDER_VERSION,
        "endpoint_order_inversions": inversions,
        "coincident_ports": coincident,
        "compared_ports": sum(map(len, sides.values())),
        "skipped_edges": skipped,
        "method": "opposite_endpoint_order",
        "estimated": True,
        "miro_routes_exact": False,
    }
