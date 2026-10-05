"""Cheap footprint and connector travel measurements for Trace candidates.

These are presentation-only estimates. Bounds include node rectangles, the
polyline actually drawn by the preview, and visible caption rectangles. Route
length and excess travel compare the attached perimeter endpoints, not node
centers. Curved connectors use their control polyline as an estimate; Miro's
own routing is not predicted here. Fee-row objects and connectors are excluded.
"""

import math

from .common import TraceError
from .edge_labels import caption_box, caption_text


COMPACTNESS_METRICS_VERSION = 1


def _number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise TraceError("Invalid geometry for layout compactness measurement")
    return float(value)


def _sum(values):
    try:
        return _number(math.fsum(values))
    except OverflowError as exc:
        raise TraceError("Invalid geometry for layout compactness measurement") from exc


def _p95(values):
    """Deterministic nearest-rank 95th percentile, including singleton lists."""
    return sorted(values)[math.ceil(len(values) * .95) - 1] if values else 0.0


def compactness_metrics(graph):
    """Measure an owned or shared graph without mutation or pair comparisons.

    Work is O(nodes + route points + caption text + edges log edges), retaining
    one edge's points, node areas, and two connector scalar lists for percentiles.
    ``node_box_density`` is summed node-box area / canvas area, not polygon
    union coverage; overlapping boxes can make it exceed one. Collision checks
    remain separate, higher-priority selection gates.
    """
    # Lazy import avoids the layout adapter's metrics dependency cycle.
    from .elk_layout import _default_attachments, attachment_point

    fees = set(graph.get("fee_items", {}))
    nodes = {node["id"]: node for node in graph["nodes"] if node["id"] not in fees}
    left = top = math.inf
    right = bottom = -math.inf
    areas, lengths, detours = [], [], []

    def include(x1, y1, x2, y2):
        nonlocal left, top, right, bottom
        x1, y1, x2, y2 = map(_number, (x1, y1, x2, y2))
        left, top = min(left, x1), min(top, y1)
        right, bottom = max(right, x2), max(bottom, y2)

    for node in nodes.values():
        x, y, width, height = (_number(node[field]) for field in ("x", "y", "width", "height"))
        if width <= 0 or height <= 0:
            raise TraceError("Invalid geometry for layout compactness measurement")
        include(x - width / 2, y - height / 2, x + width / 2, y + height / 2)
        areas.append(_number(width * height))
    for edge in graph["edges"]:
        if edge["id"] in fees or edge["source"] not in nodes or edge["target"] not in nodes:
            continue
        source, target = nodes[edge["source"]], nodes[edge["target"]]
        attachment = edge.get("attachment") or _default_attachments(source, target)
        first = attachment_point(source, attachment["startItem"])
        last = attachment_point(target, attachment["endItem"])
        route = edge.get("route") or []
        interior = route[1:-1] if edge.get("connector_shape", "straight") != "straight" else []
        points = [(_number(point["x"]), _number(point["y"])) for point in (first, *interior, last)]
        for x, y in points:
            include(x, y, x, y)
        if caption_text(edge):
            include(*caption_box(edge, points))
        length = _sum(math.dist(a, b) for a, b in zip(points, points[1:]))
        lengths.append(length)
        detours.append(max(0.0, length - math.dist(points[0], points[-1])))
    width = right - left if nodes else 0.0
    height = bottom - top if nodes else 0.0
    area = _number(width * height)
    node_area = _sum(areas)
    scale = math.sqrt(node_area / len(nodes)) if nodes else 0.0
    result = {
        "version": COMPACTNESS_METRICS_VERSION,
        "node_count": len(nodes), "edge_count": len(lengths),
        "canvas_width": width, "canvas_height": height, "canvas_area": area,
        "node_box_area": node_area,
        "area_per_node_area": area / node_area if node_area else 0.0,
        "node_box_density": node_area / area if area else 0.0,
        "node_scale": scale,
        "connector_length_total": _sum(lengths),
        "connector_length_p95": _p95(lengths),
        "connector_length_p95_normalized": _p95(lengths) / scale if scale else 0.0,
        "connector_detour_mean_normalized": _sum(detours) / len(detours) / scale if detours and scale else 0.0,
        "connector_detour_p95_normalized": _p95(detours) / scale if scale else 0.0,
    }
    # Fail closed rather than serializing NaN/Infinity from malformed geometry.
    return {key: round(_number(value), 6) if isinstance(value, float) else value
            for key, value in result.items()}


def compactness_score(metrics):
    """Only break ties after the existing safety and path-clarity criteria."""
    if metrics is None:
        return ()
    return (metrics["area_per_node_area"], metrics["connector_detour_p95_normalized"],
            metrics["connector_length_p95_normalized"], metrics["connector_detour_mean_normalized"])
