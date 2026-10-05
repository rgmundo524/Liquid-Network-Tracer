"""Conservative connector geometry for local clearance checks.

Miro receives attachments and connector style, but not ELK bend points. These
routes estimate ordinary orthogonal routing; they do not reproduce Miro's
undocumented router. Keep the saved route as well when checking clearance.
"""

import heapq
import math


STUB_CLEARANCE = 60.0
_EPS = 1e-6
_VECTORS = {"east": (1, 0), "west": (-1, 0), "north": (0, -1), "south": (0, 1)}


def _clean(points):
    result = []
    for point in points:
        point = tuple(point)
        if result and point == result[-1]:
            continue
        if len(result) >= 2:
            a, b = result[-2:]
            if ((a[0] == b[0] == point[0] and (b[1] - a[1]) * (point[1] - b[1]) >= 0)
                    or (a[1] == b[1] == point[1] and (b[0] - a[0]) * (point[0] - b[0]) >= 0)):
                result.pop()
        result.append(point)
    return result


def _geometry(edge, nodes):
    from .elk_layout import _default_attachments, attachment_point

    source, target = nodes[edge["source"]], nodes[edge["target"]]
    attachment = edge.get("attachment") or _default_attachments(source, target)
    points = []
    for node, field in ((source, "startItem"), (target, "endItem")):
        port = attachment.get(field) or _default_attachments(source, target)[field]
        if "position" not in port:
            sides = {"left": (0, 50), "right": (100, 50), "top": (50, 0), "bottom": (50, 100)}
            values = sides.get(port.get("snapTo"))
            port = ({"position": {"x": f"{values[0]}%", "y": f"{values[1]}%"}}
                    if values else _default_attachments(source, target)[field])
        point = attachment_point(node, port)
        points.append((point["x"], point["y"]))
    return source, target, *points


def _side(node, point):
    x = (point[0] - node["x"]) / (node["width"] / 2)
    y = (point[1] - node["y"]) / (node["height"] / 2)
    if abs(x) >= abs(y):
        return "east" if x >= 0 else "west"
    return "south" if y >= 0 else "north"


def _box(node):
    return (node["x"] - node["width"] / 2, node["y"] - node["height"] / 2,
            node["x"] + node["width"] / 2, node["y"] + node["height"] / 2)


def _direction(a, b):
    if abs(a[1] - b[1]) <= _EPS and abs(a[0] - b[0]) > _EPS:
        return "east" if b[0] > a[0] else "west"
    if abs(a[0] - b[0]) <= _EPS and abs(a[1] - b[1]) > _EPS:
        return "south" if b[1] > a[1] else "north"
    return None


def _outside(points, endpoints):
    from .elk_layout import segment_hits_node

    return all(not segment_hits_node({"x": a[0], "y": a[1]}, {"x": b[0], "y": b[1]}, node)
               for a, b in zip(points, points[1:]) for node in endpoints)


def _valid(points, source, target, start_side, end_side):
    return (len(points) >= 2 and _direction(points[0], points[1]) == start_side
            and _direction(points[-1], points[-2]) == end_side
            and _outside(points, (source, target)))


def _stub(node, point, side):
    left, top, right, bottom = _box(node)
    return {"east": (right + STUB_CLEARANCE, point[1]),
            "west": (left - STUB_CLEARANCE, point[1]),
            "north": (point[0], top - STUB_CLEARANCE),
            "south": (point[0], bottom + STUB_CLEARANCE)}[side]


def _grid_route(source, target, a, b, start_side, end_side):
    """Small fixed-size fallback when an ordinary bend meets an endpoint.

    Only endpoint geometry is considered. Other objects are the caller's
    clearance checks, not obstacles that this estimate silently routes around.
    """
    boxes = [_box(source), _box(target)]
    xs = sorted({a[0], b[0], *(x + offset for box in boxes for x in (box[0], box[2])
                              for offset in (-STUB_CLEARANCE, 0, STUB_CLEARANCE))})
    ys = sorted({a[1], b[1], *(y + offset for box in boxes for y in (box[1], box[3])
                              for offset in (-STUB_CLEARANCE, 0, STUB_CLEARANCE))})
    start, end = (xs.index(a[0]), ys.index(a[1])), (xs.index(b[0]), ys.index(b[1]))
    pending = [(0.0, 0, start, "", (start,))]
    best = {(start, ""): (0.0, 0)}
    allowed = {}
    while pending:
        cost, bends, current, direction, path = heapq.heappop(pending)
        if best.get((current, direction)) != (cost, bends):
            continue
        if current == end and direction:
            return _clean((xs[x], ys[y]) for x, y in path)
        x, y = current
        for next_side, (dx, dy) in _VECTORS.items():
            nxt = (x + dx, y + dy)
            if not (0 <= nxt[0] < len(xs) and 0 <= nxt[1] < len(ys)):
                continue
            if not direction and next_side != start_side:
                continue
            p, q = (xs[x], ys[y]), (xs[nxt[0]], ys[nxt[1]])
            if nxt == end and _direction(q, p) != end_side:
                continue
            if nxt == start:
                continue
            key = tuple(sorted((current, nxt)))
            if key not in allowed:
                allowed[key] = _outside([p, q], (source, target))
            if not allowed[key]:
                continue
            turning = bool(direction and direction != next_side)
            score = (cost + math.dist(p, q) + 20 * turning, bends + turning)
            state = (nxt, next_side)
            if state not in best or score < best[state]:
                best[state] = score
                heapq.heappush(pending, (*score, nxt, next_side, (*path, nxt)))
    return None


def estimated_miro_route(edge, nodes):
    """Return an attachment-aware orthogonal estimate as ``[(x, y), ...]``.

    Same-side attachments use an outside U bend, including return connections.
    Reversed and perpendicular attachments use outward stubs. Straight styles
    retain their exact straight segment. No claim of exact Miro routing is made.
    """
    source, target, a, b = _geometry(edge, nodes)
    if edge.get("connector_shape", "straight") == "straight":
        return [a, b]
    start_side, end_side = _side(source, a), _side(target, b)
    candidates = []
    if start_side in ("east", "west") and end_side in ("east", "west"):
        if start_side == end_side:
            lane = (max(_box(source)[2], _box(target)[2]) + STUB_CLEARANCE
                    if start_side == "east" else min(_box(source)[0], _box(target)[0]) - STUB_CLEARANCE)
        else:
            lane = (a[0] + b[0]) / 2
        candidates.append([a, (lane, a[1]), (lane, b[1]), b])
    elif start_side in ("north", "south") and end_side in ("north", "south"):
        if start_side == end_side:
            lane = (max(_box(source)[3], _box(target)[3]) + STUB_CLEARANCE
                    if start_side == "south" else min(_box(source)[1], _box(target)[1]) - STUB_CLEARANCE)
        else:
            lane = (a[1] + b[1]) / 2
        candidates.append([a, (a[0], lane), (b[0], lane), b])
    for candidate in candidates:
        points = _clean(candidate)
        if _valid(points, source, target, start_side, end_side):
            return points
    first, last = _stub(source, a, start_side), _stub(target, b, end_side)
    alternatives = [[a, first, (first[0], last[1]), last, b],
                    [a, first, (last[0], first[1]), last, b]]
    for lane in (min(_box(source)[0], _box(target)[0]) - STUB_CLEARANCE,
                 max(_box(source)[2], _box(target)[2]) + STUB_CLEARANCE):
        alternatives.append([a, first, (lane, first[1]), (lane, last[1]), last, b])
    for lane in (min(_box(source)[1], _box(target)[1]) - STUB_CLEARANCE,
                 max(_box(source)[3], _box(target)[3]) + STUB_CLEARANCE):
        alternatives.append([a, first, (first[0], lane), (last[0], lane), last, b])
    valid = [points for candidate in alternatives
             if _valid(points := _clean(candidate), source, target, start_side, end_side)]
    if valid:
        return min(valid, key=lambda points: (sum(math.dist(p, q) for p, q in zip(points, points[1:])),
                                              len(points), points))
    fallback = _grid_route(source, target, a, b, start_side, end_side)
    if fallback is not None:
        return fallback
    # Overlapping endpoint shapes or malformed interior ports may have no safe
    # orthogonal exit. Retain evidence of the obstruction for clearance checks.
    return [a, b]


def route_variants(edge, nodes):
    """Return distinct saved and estimated routes, anchored to current ports."""
    _, _, a, b = _geometry(edge, nodes)
    if edge.get("connector_shape", "straight") == "straight":
        return [[a, b]]
    saved = edge.get("route") or []
    result = []
    if saved:
        # Collinear intermediate points participate in the ELK caption's route
        # signature. Preserve them so the original reserved label box remains
        # valid, while still anchoring endpoints to current attachments.
        result.append([a, *((point["x"], point["y"]) for point in saved[1:-1]), b])
    estimate = estimated_miro_route(edge, nodes)
    if not any(_clean(route) == _clean(estimate) for route in result):
        result.append(estimate)
    return result
