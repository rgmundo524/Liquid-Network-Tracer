"""Bounded removal of redundant bends between adjacent trace columns.

Only assembler-approved forward connections are considered. Existing contact
points on unchanged segments may remain; every newly drawn segment and caption
must be clear. The original route wins whenever the bounded checks cannot prove
that a simpler route is safe. This does not move objects or change attachments.
"""

from copy import deepcopy
import math


MAX_INDEX_ITEMS = 100_000
MAX_CHECKS = 2_000_000
MAX_EDGE_CHECKS = 4_096
_LABEL_GAP = 7.0
_CLEARANCE = 2.0


class _BudgetExceeded(Exception):
    pass


class _IndexLimitExceeded(Exception):
    pass


class _Budget:
    def __init__(self, maximum, per_edge):
        self.maximum, self.per_edge = maximum, per_edge
        self.total = self.edge = 0

    def spend(self):
        if self.total >= self.maximum or self.edge >= self.per_edge:
            raise _BudgetExceeded
        self.total += 1
        self.edge += 1


def _box(a, b):
    return min(a["x"], b["x"]), min(a["y"], b["y"]), max(a["x"], b["x"]), max(a["y"], b["y"])


def _rectangle(value, margin=0):
    return (value["x"] - margin, value["y"] - margin,
            value["x"] + value["width"] + margin, value["y"] + value["height"] + margin)


def _touch(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _union(boxes):
    return min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)


class _Index:
    """Balanced bounding-box tree, with refits after accepted route changes.

    Long segments occupy one record, never thousands of grid cells. Item count
    caps construction; every visited branch/record counts against query budgets.
    """
    def __init__(self, records):
        self.records = records
        self.leaves = {}
        self.tree = self._build(list(records))

    def _build(self, keys, parent=None):
        boxes = [self.records[key]["box"] for key in keys]
        node = {"box": _union(boxes), "parent": parent}
        if len(keys) <= 8:
            node["keys"] = keys
            for key in keys:
                self.leaves[key] = node
        else:
            bounds = node["box"]
            axis = 0 if bounds[2] - bounds[0] >= bounds[3] - bounds[1] else 1
            keys.sort(key=lambda key: (self.records[key]["box"][axis] + self.records[key]["box"][axis + 2], key))
            middle = len(keys) // 2
            node["children"] = [self._build(keys[:middle], node), self._build(keys[middle:], node)]
        return node

    def query(self, box, budget):
        pending = [self.tree]
        while pending:
            budget.spend()
            node = pending.pop()
            if node["box"] is None or not _touch(box, node["box"]):
                continue
            if "children" in node:
                pending.extend(reversed(node["children"]))
                continue
            for key in node["keys"]:
                budget.spend()
                record = self.records[key]
                if record["box"] is not None and _touch(box, record["box"]):
                    yield record

    def replace(self, key, box, **values):
        self.records[key].update(box=box, **values)
        node = self.leaves[key]
        while node is not None:
            boxes = ([child["box"] for child in node["children"]] if "children" in node else
                     [self.records[item]["box"] for item in node["keys"]])
            boxes = [box for box in boxes if box is not None]
            node["box"] = _union(boxes) if boxes else None
            node = node["parent"]


def _points(edge):
    if len(edge.get("sections", [])) != 1:
        return None
    section = edge["sections"][0]
    return [section["startPoint"], *section.get("bendPoints", []), section["endPoint"]]


def _segments(points):
    return [(a, b) for a, b in zip(points, points[1:]) if a != b]


def _length(points):
    return sum(abs(a["x"] - b["x"]) + abs(a["y"] - b["y"]) for a, b in _segments(points))


def _two_tracks(points):
    if len(points) != 6:
        return False
    a, b, c, d, e, f = points
    return (a["y"] == b["y"] and b["x"] == c["x"] and c["y"] == d["y"]
            and d["x"] == e["x"] and e["y"] == f["y"]
            and a["x"] < min(b["x"], d["x"]) <= max(b["x"], d["x"]) < f["x"]
            and c["y"] != b["y"] and e["y"] != d["y"])


def _new_parts(points, original):
    """Subtract old collinear spans, retaining only newly occupied geometry."""
    old = _segments(original)
    for a, b in _segments(points):
        fixed, axis = ("y", "x") if a["y"] == b["y"] else ("x", "y")
        parts = [sorted((a[axis], b[axis]))]
        for c, d in old:
            if c[fixed] != d[fixed] or c[fixed] != a[fixed]:
                continue
            low, high = sorted((c[axis], d[axis]))
            remaining = []
            for start, stop in parts:
                if low >= stop or high <= start:
                    remaining.append((start, stop))
                else:
                    if start < low:
                        remaining.append((start, low))
                    if high < stop:
                        remaining.append((high, stop))
            parts = remaining
        for start, stop in parts:
            yield {fixed: a[fixed], axis: start}, {fixed: a[fixed], axis: stop}


def _route_clear(index, budget, identity, points, original):
    from .elk_layout import segment_hits_node

    for a, b in _new_parts(points, original):
        bounds = _box(a, b)
        for record in index.query(bounds, budget):
            if record["kind"] == "node":
                if segment_hits_node(a, b, record["node"]):
                    return False
            elif record["owner"] != identity:
                # Assembled segments are orthogonal, so touching their bounding
                # boxes is an exact crossing/touch/overlap test, including bends.
                return False
    return True


def _labels(points, original):
    if not original:
        yield []
        return
    label = original[0]
    # Favor long runs; deterministic ties favor the source-side stub.
    horizontal = sorted(((abs(b["x"] - a["x"]), order, a, b)
                         for order, (a, b) in enumerate(_segments(points)) if a["y"] == b["y"]),
                        key=lambda value: (-value[0], value[1]))
    for length, _, a, b in horizontal:
        if length < label["width"] + 2 * _LABEL_GAP:
            continue
        x = (a["x"] + b["x"] - label["width"]) / 2
        for y in (a["y"] - label["height"] - _LABEL_GAP, a["y"] + _LABEL_GAP):
            if x >= 0 and y >= 0:
                yield [{**deepcopy(label), "x": x, "y": y}]


def _labels_clear(index, budget, identity, labels, points, width, height):
    for label in labels:
        bounds = _rectangle(label, _CLEARANCE)
        if bounds[0] < 0 or bounds[1] < 0 or bounds[2] > width or bounds[3] > height:
            return False
        if any(_touch(bounds, _box(a, b)) for a, b in _segments(points)):
            return False
        for record in index.query(bounds, budget):
            if record["kind"] == "node" or record["owner"] != identity:
                return False
    return True


def _shortcuts(original):
    for x in dict.fromkeys((original[1]["x"], original[3]["x"])):
        points = [original[0], {"x": x, "y": original[0]["y"]},
                  {"x": x, "y": original[-1]["y"]}, original[-1]]
        points = [point for number, point in enumerate(points) if number == 0 or point != points[number - 1]]
        if points[0]["y"] == points[-1]["y"]:
            points = [points[0], points[-1]]
        yield points


def _node_record(node, shapes):
    return {"kind": "node", "owner": node["id"], "box": _rectangle(node),
            "node": {**node, "x": node["x"] + node["width"] / 2,
                     "y": node["y"] + node["height"] / 2, "kind": shapes.get(node["id"], "transaction")}}


def _edge_records(edge):
    for number, (a, b) in enumerate(_segments(_points(edge))):
        yield (("segment", edge["id"], number),
               {"kind": "segment", "owner": edge["id"], "box": _box(a, b)})
    for number, label in enumerate(edge.get("labels", [])):
        yield (("label", edge["id"], number),
               {"kind": "label", "owner": edge["id"], "box": _rectangle(label, _CLEARANCE)})


def _coarse_edge(edge):
    points = _points(edge)
    # Compute bounds without allocating one record per segment. Degenerate
    # zero-length routes retain a harmless stable broad-phase entry.
    bounds = _box(points[0], points[0])
    for point in points[1:]:
        bounds = (min(bounds[0], point["x"]), min(bounds[1], point["y"]),
                  max(bounds[2], point["x"]), max(bounds[3], point["y"]))
    for label in edge.get("labels", []):
        box = _rectangle(label, _CLEARANCE)
        bounds = (min(bounds[0], box[0]), min(bounds[1], box[1]),
                  max(bounds[2], box[2]), max(bounds[3], box[3]))
    return {"kind": "edge", "owner": edge["id"], "box": bounds, "edge": edge}


def _region_index(coarse, edge, budget, maximum, stats):
    original = _points(edge)
    boxes = [record["box"] for _, record in _edge_records(edge)]
    for points in _shortcuts(original):
        boxes.extend(_box(a, b) for a, b in _segments(points))
        for labels in _labels(points, edge.get("labels", [])):
            boxes.extend(_rectangle(label, _CLEARANCE) for label in labels)
    region = _union(boxes)
    records = {}

    def add(key, record):
        if not _touch(region, record["box"]):
            return
        if len(records) >= maximum:
            raise _IndexLimitExceeded
        records[key] = record
        stats["index_items"] = max(stats["index_items"], len(records))

    for owner in coarse.query(region, budget):
        if owner["kind"] == "node":
            add(("node", owner["owner"], 0), owner)
        else:
            for key, record in _edge_records(owner["edge"]):
                budget.spend()
                add(key, record)
    return _Index(records)


def cleanup_routes(nodes, edges, eligible, *, node_shapes=None, width, height,
                   max_checks=MAX_CHECKS, max_edge_checks=MAX_EDGE_CHECKS, max_index_items=MAX_INDEX_ITEMS):
    """Return routes plus diagnostics; never mutate the supplied raw candidate.

    ``eligible`` maps edge IDs to source/target IDs selected by the assembler:
    adjacent columns, EAST source and WEST target. Nodes may be on different
    rows: joining two independently assigned tracks can add an unnecessary
    step even when both vertical segments travel in the same direction.
    All other edges, including return loops and the straight primary backbone,
    remain unchanged.
    """
    stats = {"version": 3, "eligible": 0, "applied": 0, "rejected": 0, "budget_exhausted": 0,
             "checks": 0, "check_limit": max_checks, "per_edge_check_limit": max_edge_checks,
             "index_items": 0, "index_item_limit": max_index_items, "index_limit_reached": False,
             "length_removed": 0.0, "bends_removed": 0, "steps_applied": 0,
             "index_mode": "none", "total_index_items": 0, "coarse_index_items": 0,
             "regions_processed": 0, "regions_skipped": 0, "index_limit_skipped": 0,
             "candidates_skipped": 0, "partial": False}
    selected = {edge["id"] for edge in edges if edge["id"] in eligible
                and (points := _points(edge)) is not None and _two_tracks(points) and len(edge.get("labels", [])) <= 1}
    stats["eligible"] = len(selected)
    if not selected:
        return edges, stats
    # Count and validate once before choosing a collision index. A large graph
    # must not silently lose hazards that occur after the first full page.
    primitive_count = len(nodes)
    for edge in edges:
        points = _points(edge)
        if points is None or any(a["x"] != b["x"] and a["y"] != b["y"] for a, b in _segments(points)):
            stats.update(unsupported_geometry=True, candidates_skipped=len(selected),
                         rejected=len(selected), partial=True)
            return edges, stats
        primitive_count += len(_segments(points)) + len(edge.get("labels", []))
    regional = primitive_count > max_index_items
    stats.update(index_mode="regional" if regional else "global", total_index_items=primitive_count)
    node_shapes = node_shapes or {}
    if regional:
        # One broad-phase entry per object/edge, rather than one per bend and
        # caption. The exact collision index is allocated only for one bounded
        # region at a time. Full route bounds retain cross-region connections.
        coarse = _Index({**{("node", node["id"], 0): _node_record(node, node_shapes) for node in nodes},
                         **{("edge", edge["id"], 0): _coarse_edge(edge) for edge in edges}})
        stats["coarse_index_items"] = len(coarse.records)
        index = None
    else:
        records = {("node", node["id"], 0): _node_record(node, node_shapes) for node in nodes}
        for edge in edges:
            records.update(_edge_records(edge))
        stats["index_items"] = len(records)
        index = _Index(records)
    budget = _Budget(max_checks, max_edge_checks)
    result = list(edges)
    positions = sorted((position for position, edge in enumerate(edges) if edge["id"] in selected),
                       key=lambda position: edges[position]["id"])
    for order, position in enumerate(positions):
        edge = edges[position]
        identity = edge["id"]
        original = _points(edge)
        budget.edge = 0
        accepted = None
        if budget.total >= budget.maximum:
            remaining = len(positions) - order
            stats["budget_exhausted"] += remaining
            stats["rejected"] += remaining
            stats["candidates_skipped"] += remaining
            break
        try:
            if regional:
                stats["regions_processed"] += 1
                index = _region_index(coarse, edge, budget, max_index_items, stats)
            for points in _shortcuts(original):
                length, original_length = _length(points), _length(original)
                # A monotone step already has minimal Manhattan length. It
                # still benefits from two fewer bends, provided the same
                # collision/caption checks prove the replacement safe.
                equal_length = math.isclose(length, original_length, rel_tol=0, abs_tol=1e-7)
                if ((length > original_length and not equal_length)
                        or (equal_length and len(points) >= len(original))):
                    continue
                if not _route_clear(index, budget, identity, points, original):
                    continue
                for labels in _labels(points, edge.get("labels", [])):
                    if _labels_clear(index, budget, identity, labels, points, width, height):
                        accepted = points, labels
                        break
                if accepted is not None:
                    break
        except _BudgetExceeded:
            stats["budget_exhausted"] += 1
            stats["candidates_skipped"] += 1
            stats["regions_skipped"] += bool(regional)
        except _IndexLimitExceeded:
            stats["index_limit_reached"] = True
            stats["index_limit_skipped"] += 1
            stats["candidates_skipped"] += 1
            stats["regions_skipped"] += 1
        if accepted is None:
            stats["rejected"] += 1
            continue
        points, labels = accepted
        new = deepcopy(edge)
        new["sections"] = [{**new["sections"][0], "startPoint": deepcopy(points[0]),
                            "bendPoints": deepcopy(points[1:-1]), "endPoint": deepcopy(points[-1])}]
        new["labels"] = labels
        result[position] = new
        if regional:
            coarse.replace(("edge", identity, 0), **_coarse_edge(new))
        else:
            segments = _segments(points)
            for number, _ in enumerate(_segments(original)):
                bounds = _box(*segments[number]) if number < len(segments) else None
                index.replace(("segment", identity, number), bounds)
            for number, label in enumerate(labels):
                index.replace(("label", identity, number), _rectangle(label, _CLEARANCE))
        stats["applied"] += 1
        stats["length_removed"] += max(0.0, _length(original) - _length(points))
        stats["bends_removed"] += len(original) - len(points)
        stats["steps_applied"] += ((original[2]["y"] - original[1]["y"])
                                   * (original[4]["y"] - original[3]["y"]) > 0)
    stats["checks"] = budget.total
    stats["partial"] = bool(stats.get("candidates_skipped"))
    stats["length_removed"] = round(stats["length_removed"], 4)
    return result, stats
