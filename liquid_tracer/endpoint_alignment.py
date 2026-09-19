"""Remove small connector jogs without moving objects or transaction ports.

ELK lays out rectangular bounds, then the adapter projects address ports onto
circles. A singleton address-side port can follow its transaction port's row
instead, provided both the saved routes and the board-routing estimate remain
at least as safe. These checks are conservative, not a prediction of Miro's
undocumented automatic routing.
"""

import copy
import math
from collections import Counter

from .attachment_order import attachment_order_metrics


ENDPOINT_ALIGNMENT_VERSION = 1
_EPS = 1e-5


def align_near_horizontal_endpoints(graph):
    """Align safe, small address-port offsets in an owned graph, in place.

    Every transaction slot and all node coordinates stay fixed. At most one
    port on either hemisphere of an address may be adjusted; busy address sides
    and explicit change rows retain their attachments. A batch is rejected if
    a bounded geometric comparison is incomplete or any collision count rises.
    """
    from .elk_layout import (_default_attachments, _percent, attachment_point,
                             layout_metrics, segment_hits_node)

    nodes = {node["id"]: node for node in graph["nodes"]}
    edges = graph["edges"]
    fees = set(graph.get("fee_items", {}))
    changes = graph.get("layout", {}).get("change_outputs", {}).get("applied", [])
    change_edges = {item.get("edge_id") for item in changes}
    change_outpoints = {item.get("outpoint") for item in changes if item.get("outpoint")}
    sides = Counter()
    geometry = []
    for edge in edges:
        source, target = nodes[edge["source"]], nodes[edge["target"]]
        attachment = edge.get("attachment") or _default_attachments(source, target)
        start = attachment_point(source, attachment["startItem"])
        end = attachment_point(target, attachment["endItem"])
        geometry.append((edge, source, target, attachment, start, end))
        # Include returns and fee connections in occupancy. An otherwise
        # excluded edge still prevents another port sharing its address side.
        for node, point in ((source, start), (target, end)):
            if node["kind"] == "address":
                offset = point["x"] - node["x"]
                side = 1 if offset > _EPS else -1 if offset < -_EPS else 0
                sides[node["id"], side] += 1

    proposals = []
    for edge, source, target, attachment, start, end in geometry:
        if (edge.get("connector_shape") != "elbowed"
                or edge.get("routing_exception") not in (None, "unchecked")
                or edge["id"] in fees or source["id"] in fees or target["id"] in fees
                or edge["id"] in change_edges or edge.get("outpoint") in change_outpoints
                or edge.get("change_output")
                or end["x"] <= start["x"] + _EPS):
            continue
        if source["kind"] == "address" and target["kind"] == "transaction":
            address, transaction, field, local, opposite, side = source, target, "startItem", start, end, 1
        elif source["kind"] == "transaction" and target["kind"] == "address":
            address, transaction, field, local, opposite, side = target, source, "endItem", end, start, -1
        else:
            continue
        if (sides[address["id"], side] != 1
                or (local["x"] - address["x"]) * side <= _EPS):
            continue
        delta = opposite["y"] - local["y"]
        # Restrict both physical movement and slope; an ordinary branch down
        # to a different row still needs its full bend.
        if not (_EPS < abs(delta) <= min(address["height"], transaction["height"]) / 4
                and abs(delta) <= (end["x"] - start["x"]) * .15):
            continue
        normalized_y = (opposite["y"] - address["y"]) / (address["height"] / 2)
        if abs(normalized_y) > .7:
            continue
        route = edge.get("route") or [start, end]
        points = [start, *route[1:-1], end]
        lower, upper = sorted((start["y"], end["y"]))
        if (any(point["y"] < lower - _EPS or point["y"] > upper + _EPS
                or point["x"] < start["x"] - _EPS or point["x"] > end["x"] + _EPS
                for point in points)
                or any(b["x"] < a["x"] - _EPS for a, b in zip(points, points[1:]))):
            continue
        port = {"position": {
            "x": _percent(50 + side * 50 * math.sqrt(1 - normalized_y * normalized_y)),
            "y": _percent(50 + normalized_y * 50),
        }}
        aligned = copy.deepcopy(attachment)
        aligned[field] = port
        a = attachment_point(source, aligned["startItem"])
        b = attachment_point(target, aligned["endItem"])
        # Rounding percentage coordinates can introduce a negligible Y error.
        # Keep the route anchored to the exact values sent to Miro.
        if (b["x"] <= a["x"] + _EPS or segment_hits_node(a, b, source)
                or segment_hits_node(a, b, target)):
            continue
        proposals.append((edge, aligned, [a, b]))

    report = {"version": ENDPOINT_ALIGNMENT_VERSION, "candidates": len(proposals),
              "applied": 0, "rejected": 0, "miro_routes_exact": False}
    if not proposals:
        graph.setdefault("layout", {})["endpoint_alignment"] = report
        return graph

    before = [layout_metrics(graph), layout_metrics(graph, midpoint_elbows=True)]
    before_ports = attachment_order_metrics(graph)
    if any(metrics["truncated"] for metrics in before):
        report.update(rejected=len(proposals), reason="geometry_checks_truncated")
        graph.setdefault("layout", {})["endpoint_alignment"] = report
        return graph

    originals = []
    for edge, attachment, route in proposals:
        originals.append((edge, copy.deepcopy(edge)))
        edge.update(attachment=attachment, route=route)
        edge.pop("label_layout", None)
    after = [layout_metrics(graph), layout_metrics(graph, midpoint_elbows=True)]
    after_ports = attachment_order_metrics(graph)
    checks = ("node_intersections", "crossings", "connector_overlaps")
    safe = (not any(metrics["truncated"] for metrics in after)
            and all(new[key] <= old[key] for old, new in zip(before, after) for key in checks)
            and all(after_ports[key] <= before_ports[key]
                    for key in ("endpoint_order_inversions", "coincident_ports")))
    if safe:
        report["applied"] = len(proposals)
    else:
        for edge, original in originals:
            edge.clear()
            edge.update(original)
        report.update(rejected=len(proposals), reason="attachment_routing_estimate")
    graph.setdefault("layout", {})["endpoint_alignment"] = report
    return graph
