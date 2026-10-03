"""Assemble independent layouts without copying shared graph objects.

Local ELK positions supply ordering hints; the full request supplies identity,
ports, dimensions, and captions.  Explicit row and column gutters keep routes
outside object rectangles.  Interval-colored tracks are reused where their
spans do not touch.  This is a bounded routing heuristic, not a global crossing
minimum: dense joins can still cross and require wide gutters.
"""

from collections import Counter, defaultdict
from copy import deepcopy
from heapq import heappop, heappush
import math

from .common import TraceError

_GAP = 22.0
_MARGIN = 24.0


def _number(value, *, positive=False):
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or (positive and value <= 0)):
        raise TraceError("Invalid section layout geometry")
    return float(value)


def _color(intervals):
    """Return reusable lanes in O(k log k), including point-touch conflicts."""
    active, available, lanes, count = [], [], {}, 0
    for start, end, key in sorted(intervals):
        while active and active[0][0] < start:
            _, lane = heappop(active)
            heappush(available, lane)
        if available:
            lane = heappop(available)
        else:
            lane, count = count, count + 1
        lanes[key] = lane
        heappush(active, (end, lane))
    return lanes, count


def _rows(children, groups, candidates, columns, backbone, order):
    rows, occupied = {}, set()
    # A shared primary row is safe only for one object in each dependency
    # column. Extra same-column backbone members remain in their own section.
    for key in sorted(backbone, key=lambda key: (order.get(key, len(order)), key)):
        if columns[key] not in occupied:
            rows[key] = 0
            occupied.add(columns[key])
    next_row = 1 if rows else 0
    for group, candidate in zip(groups, candidates):
        hints = {node["id"]: _number(node["y"]) + _number(node["height"], positive=True) / 2
                 for node in candidate.get("nodes", [])}
        if set(hints) != set(group) or len(hints) != len(candidate.get("nodes", [])):
            raise TraceError("A section layout changed its objects")
        pending = [key for key in group if key not in rows]
        if not pending:
            continue
        count = max(Counter(columns[key] for key in pending).values())
        low, high = min(hints[key] for key in pending), max(hints[key] for key in pending)
        last, highest = {}, 0
        for key in sorted(pending, key=lambda key: (hints[key], order.get(key, len(order)), key)):
            desired = round((hints[key] - low) * (count - 1) / (high - low)) if high > low else 0
            local = max(desired, last.get(columns[key], -1) + 1)
            rows[key], last[columns[key]] = next_row + local, local
            highest = max(highest, local)
        next_row += highest + 1
    return rows


def _simplify(points):
    result = []
    for point in points:
        if result and point == result[-1]:
            continue
        if len(result) > 1 and ((result[-2]["x"] == result[-1]["x"] == point["x"])
                                or (result[-2]["y"] == result[-1]["y"] == point["y"])):
            # Keep turnarounds: dropping one would change which track is used.
            axis = "y" if result[-1]["x"] == point["x"] else "x"
            if min(result[-2][axis], point[axis]) <= result[-1][axis] <= max(result[-2][axis], point[axis]):
                result.pop()
        result.append(point)
    return result if len(result) > 1 else result * 2


def assemble(request, groups, candidates, backbone_ids):
    """Return a complete raw ELK candidate using canonical request identity.

    ``groups`` is an exact partition of request children; ``candidates`` has
    one local raw candidate per group. No evidence is read and inputs are never
    modified. The number of routing records is linear in nodes plus edges.
    """
    groups = [list(group) for group in groups]
    candidates = list(candidates)
    children = {node["id"]: node for node in request.get("children", [])}
    keys = [key for group in groups for key in group]
    if (len(children) != len(request.get("children", [])) or len(groups) != len(candidates)
            or len(keys) != len(set(keys)) or set(keys) != set(children)):
        raise TraceError("Section layouts must preserve every object exactly once")
    if not children:
        if request.get("edges"):
            raise TraceError("Section layout has connections without objects")
        return {"seed": 1, "nodes": [], "edges": [], "inputOrderPolicy": "geometry"}
    try:
        original_columns = {key: _number(float(node.get("layoutOptions", {}).get("elk.partitioning.partition", 0)))
                            for key, node in children.items()}
    except (TypeError, ValueError) as exc:
        raise TraceError("Invalid section dependency column") from exc
    ranks = {column: index for index, column in enumerate(sorted(set(original_columns.values())))}
    columns = {key: ranks[column] for key, column in original_columns.items()}
    backbone = set(backbone_ids) & children.keys()
    order = {key: index for index, key in enumerate(request.get("centerNodeOrder", []))}
    rows = _rows(children, groups, candidates, columns, backbone, order)
    row_count, column_count = max(rows.values()) + 1, len(ranks)
    row_heights, column_widths = [0.0] * row_count, [0.0] * column_count
    ports = {}
    for key, node in children.items():
        row_heights[rows[key]] = max(row_heights[rows[key]], _number(node["height"], positive=True))
        column_widths[columns[key]] = max(column_widths[columns[key]], _number(node["width"], positive=True))
        for port in node.get("ports", []):
            port_id = port["id"]
            side = port.get("layoutOptions", {}).get("elk.port.side", "EAST")
            if port_id in ports or side not in {"WEST", "EAST", "NORTH", "SOUTH"}:
                raise TraceError("Invalid or duplicate section connector port")
            ports[port_id] = {"node": key, "side": side, "raw": port, "neighbors": []}
    edges, edge_ids = [], set()
    max_caption_width = 0.0
    for raw in request.get("edges", []):
        if (raw["id"] in edge_ids or len(raw.get("sources", [])) != 1 or len(raw.get("targets", [])) != 1
                or raw["sources"][0] not in ports or raw["targets"][0] not in ports):
            raise TraceError("Invalid section layout connection")
        edge_ids.add(raw["id"])
        source, target = raw["sources"][0], raw["targets"][0]
        a, b = ports[source]["node"], ports[target]["node"]
        ports[source]["neighbors"].append(b)
        ports[target]["neighbors"].append(a)
        labels = raw.get("labels", [])
        if len(labels) > 1:
            raise TraceError("Section layout requires at most one caption per connection")
        for label in labels:
            max_caption_width = max(max_caption_width, _number(label["width"], positive=True))
            _number(label["height"], positive=True)
        edges.append({"raw": raw, "source": source, "target": target,
                      "gap": min(row_count, (rows[a] + rows[b]) // 2 + 1)})
    direct, direct_ports = {}, set()
    # Preserve the primary path as a straight lane across adjacent columns.
    # There is at most one selected primary object per column and one direct
    # connector per gap. Additional parallel transfers keep separate routes.
    for index, edge in sorted(enumerate(edges), key=lambda pair: pair[1]["raw"]["id"]):
        source, target = ports[edge["source"]], ports[edge["target"]]
        a, b = source["node"], target["node"]
        if (a in backbone and b in backbone and rows[a] == rows[b] == 0
                and ("backboneEdges" not in request or edge["raw"]["id"] in request["backboneEdges"])
                and columns[b] == columns[a] + 1 and source["side"] == "EAST" and target["side"] == "WEST"
                and columns[b] not in direct):
            direct[columns[b]] = index
            direct_ports.update((edge["source"], edge["target"]))
    direct_indices = set(direct.values())
    # Facing ports share their available span. Assigning each node's ports
    # independently would put opposing stubs on the very same line across a
    # gutter, even when the longer routes used separate tracks.
    facing = defaultdict(list)
    for port_id, port in ports.items():
        key, side = port["node"], port["side"]
        if side in {"NORTH", "SOUTH"}:
            facing["vertical", columns[key], rows[key] + (side == "SOUTH")].append(port_id)
        else:
            facing["horizontal", columns[key] + (side == "EAST"), rows[key]].append(port_id)
    for (direction, _, _), values in facing.items():
        dimension = "width" if direction == "vertical" else "height"
        span = min(children[ports[port_id]["node"]][dimension] for port_id in values)
        def neighbor_position(port_id):
            neighbors = ports[port_id]["neighbors"]
            return min(((rows[other], columns[other], other) for other in neighbors), default=(-1, -1, "")), port_id
        ordered = sorted(values, key=neighbor_position)
        secondary = [port_id for port_id in ordered if port_id not in direct_ports]
        fractions = {port_id: index / (len(values) + 1) for index, port_id in enumerate(ordered, 1)}
        if any(port_id in direct_ports for port_id in values):
            fractions.update({port_id: .5 + .5 * index / (len(secondary) + 1)
                              for index, port_id in enumerate(secondary, 1)})
            fractions.update({port_id: .5 for port_id in values if port_id in direct_ports})
        for port_id in ordered:
            port = ports[port_id]
            key, side = port["node"], port["side"]
            half_width, half_height = children[key]["width"] / 2, children[key]["height"] / 2
            offset = span * (fractions[port_id] - .5)
            dx, dy = ((offset / half_width, -1 if side == "NORTH" else 1)
                      if direction == "vertical" else (-1 if side == "WEST" else 1, offset / half_height))
            kind = request.get("nodeShapes", {}).get(key)
            if kind in {"address", "event"}:
                # Preserve the assigned facing-axis coordinate while moving
                # onto the actual perimeter. Reprojection in _apply_candidate
                # is idempotent, preserving horizontal/vertical stubs.
                fraction = dx if direction == "vertical" else dy
                extent = math.sqrt(max(0, 1 - fraction * fraction)) if kind == "address" else 1 - abs(fraction)
                if direction == "vertical":
                    dy *= extent
                else:
                    dx *= extent
            port["local"] = {"x": half_width * (1 + dx), "y": half_height * (1 + dy)}

    horizontal, vertical = defaultdict(list), defaultdict(list)
    line_heights, endpoints = {}, {}
    for index, edge in enumerate(edges):
        if index in direct_indices:
            continue
        gutters = []
        for end, port_id, other_id in ((0, edge["source"], edge["target"]),
                                       (1, edge["target"], edge["source"])):
            port = ports[port_id]
            key, side = port["node"], port["side"]
            col, row = columns[key], rows[key]
            other_col = columns[ports[other_id]["node"]]
            gutter = col + (side == "EAST" or (side in {"NORTH", "SOUTH"} and other_col >= col))
            stub_gap = row if side == "NORTH" else row + 1 if side == "SOUTH" else None
            endpoint = (index, end)
            endpoints[endpoint] = {"gutter": gutter, "stub_gap": stub_gap, "port": port_id}
            start = 2 * stub_gap if stub_gap is not None else 2 * row + 1
            stop = 2 * edge["gap"]
            vertical[gutter].append((min(start, stop) - .25, max(start, stop) + .25, endpoint))
            if stub_gap is not None:
                low, high = sorted((col + .5, gutter))
                horizontal[stub_gap].append((low - .1, high + .1, (index, end)))
                line_heights[index, end] = 0.0
            gutters.append(gutter)
        # Captions may extend beyond a short elbow. Conservatively claim the
        # neighboring cells too; labels sharing a lane then cannot collide.
        low, high = sorted(gutters)
        horizontal[edge["gap"]].append((low - 1, high + 1, (index, 2)))
        line_heights[index, 2] = max((float(label["height"]) for label in edge["raw"].get("labels", [])), default=0.0)

    horizontal_lanes, vertical_lanes, gap_heights, gutter_widths = {}, {}, {}, {}
    lane_heights, lane_offsets = {}, {}
    for gap in range(row_count + 1):
        assigned, count = _color(horizontal[gap])
        horizontal_lanes.update(assigned)
        heights = [_GAP] * count
        for key, lane in assigned.items():
            heights[lane] = max(heights[lane], line_heights[key] + _GAP)
        offset = _MARGIN
        for lane, height in enumerate(heights):
            lane_offsets[gap, lane], lane_heights[gap, lane] = offset, height
            offset += height
        gap_heights[gap] = max(80.0, offset + _MARGIN)
    if direct:
        direct_height = max((float(label["height"]) for index in direct_indices
                             for label in edges[index]["raw"].get("labels", [])), default=0.0)
        gap_heights[0] = max(gap_heights[0], direct_height + 7 + _MARGIN)
    caption_margin = max_caption_width / 2 + _MARGIN
    for gutter in range(column_count + 1):
        assigned, count = _color(vertical[gutter])
        vertical_lanes.update(assigned)
        leading = max_caption_width + 2 * _MARGIN if gutter in direct else caption_margin
        gutter_widths[gutter] = max(80.0, leading + caption_margin + (count + 1) * _GAP)

    column_left, gutter_left, cursor = {}, {}, 0.0
    for gutter in range(column_count + 1):
        gutter_left[gutter] = cursor
        cursor += gutter_widths[gutter]
        if gutter < column_count:
            column_left[gutter] = cursor
            cursor += column_widths[gutter]
    total_width = cursor
    row_top, gap_top, cursor = {}, {}, 0.0
    for gap in range(row_count + 1):
        gap_top[gap] = cursor
        cursor += gap_heights[gap]
        if gap < row_count:
            row_top[gap] = cursor
            cursor += row_heights[gap]
    total_height = cursor
    nodes, positioned = [], {}
    for key, original in children.items():
        node = {"id": key, "width": original["width"], "height": original["height"],
                "x": column_left[columns[key]] + (column_widths[columns[key]] - original["width"]) / 2,
                "y": row_top[rows[key]] + (row_heights[rows[key]] - original["height"]) / 2,
                "ports": []}
        for raw in original.get("ports", []):
            port = ports[raw["id"]]
            local = port["local"]
            node["ports"].append({**deepcopy(raw), **local})
            positioned[raw["id"]] = {"x": node["x"] + local["x"], "y": node["y"] + local["y"]}
        nodes.append(node)

    def line_y(gap, key):
        lane = horizontal_lanes[key]
        return gap_top[gap] + lane_offsets[gap, lane] + lane_heights[gap, lane] - _GAP / 2

    routed = []
    for index, edge in enumerate(edges):
        if index in direct_indices:
            a, b = positioned[edge["source"]], positioned[edge["target"]]
            gutter = columns[ports[edge["target"]]["node"]]
            labels = [{**deepcopy(label),
                       "x": gutter_left[gutter] + _MARGIN + (max_caption_width - label["width"]) / 2,
                       "y": a["y"] - label["height"] - 7}
                      for label in edge["raw"].get("labels", [])]
            routed.append({"id": edge["raw"]["id"],
                           "sections": [{"startPoint": a, "endPoint": b}], "labels": labels})
            continue
        main_y = line_y(edge["gap"], (index, 2))
        halves = []
        for end in (0, 1):
            endpoint = endpoints[index, end]
            anchor = positioned[endpoint["port"]]
            gutter = endpoint["gutter"]
            leading = max_caption_width + 2 * _MARGIN if gutter in direct else caption_margin
            x = gutter_left[gutter] + leading + (vertical_lanes[index, end] + 1) * _GAP
            if endpoint["stub_gap"] is None:
                points = [anchor, {"x": x, "y": anchor["y"]}]
            else:
                y = line_y(endpoint["stub_gap"], (index, end))
                points = [anchor, {"x": anchor["x"], "y": y}, {"x": x, "y": y}]
            points.append({"x": x, "y": main_y})
            halves.append(points)
        points = _simplify(halves[0] + list(reversed(halves[1])))
        labels = [{**deepcopy(label),
                   "x": (halves[0][-1]["x"] + halves[1][-1]["x"] - label["width"]) / 2,
                   "y": main_y - label["height"] - 7}
                  for label in edge["raw"].get("labels", [])]
        routed.append({"id": edge["raw"]["id"],
                       "sections": [{"startPoint": points[0], "bendPoints": points[1:-1], "endPoint": points[-1]}],
                       "labels": labels})
    return {"seed": candidates[0].get("seed", 1) if candidates else 1,
            "branchProfile": "flow_weighted", "branchBoundary": bool(request.get("boundaryOrdering", False)),
            "inputOrderPolicy": "geometry", "nodes": nodes, "edges": routed,
            "width": total_width, "height": total_height,
            "sectionGeometry": {"rows": row_count, "columns": column_count,
                                "routing": "interval_colored_gutters", "backbone_row": 0 if backbone else None}}
