"""Assemble independent layouts without copying shared graph objects.

Current Trace candidates preserve local geometry inside section envelopes.
The gutter primitive below places both those envelopes and bounded local
fallbacks; legacy callers use local positions as ordering hints. The complete
request supplies identity, ports, dimensions and captions. Interval-colored
tracks are reused where their spans do not touch. This is a bounded routing
heuristic, not a global crossing minimum: dense joins can still cross.
"""

from bisect import bisect_left
from collections import Counter, defaultdict
from copy import deepcopy
from heapq import heappop, heappush
import math

from .common import TraceError

_GAP = 22.0
_MARGIN = 24.0
_PLACEMENT_DELTAS = (0, 1, -1, 2, -2, 4, -4, 8, -8)
_PLACEMENT_PROBE_LIMIT = 16
_ADDRESS_PROBE_LIMIT = 16
_ADDRESS_ROW_DELTAS = (0, 1, -1, 2, -2, 4, -4)


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


def _rows(children, groups, candidates, columns, backbone, order, *, neighbors=None, stats=None):
    """Pack local ordering profiles with a fixed number of occupancy probes.

    Each object owns one (column, row) cell; different columns may reuse rows.
    Whole sections translate without changing their local ordering. Neighbor
    attachments suggest offsets, and per-column bounds supply two guaranteed
    clear fallbacks. No section scans all prior rows or all other objects.
    Node/edge indexing plus heap ordering and a fixed number of profile probes
    bound the work to O((N + E) log(N + E)); stored occupancy is O(N).

    This packs objects, not connector corridors. Dense joins may still widen
    the later global gutters; a short section is not guaranteed its optimum
    position when all nearby candidates are occupied.
    """
    neighbors = neighbors or {}
    rows, primary_columns = {}, set()
    # A shared primary row is safe only for one object in each dependency
    # column. Extra same-column backbone members remain in their own section.
    for key in sorted(backbone, key=lambda key: (order.get(key, len(order)), key)):
        if columns[key] not in primary_columns:
            rows[key] = 0
            primary_columns.add(columns[key])
    profiles, owners = [], {}
    for group, candidate in zip(groups, candidates):
        hints = {node["id"]: _number(node["y"]) + _number(node["height"], positive=True) / 2
                 for node in candidate.get("nodes", [])}
        if set(hints) != set(group) or len(hints) != len(candidate.get("nodes", [])):
            raise TraceError("A section layout changed its objects")
        pending = [key for key in group if key not in rows]
        if not pending:
            profiles.append({})
            continue
        count = max(Counter(columns[key] for key in pending).values())
        low, high = min(hints[key] for key in pending), max(hints[key] for key in pending)
        last, local_rows = {}, {}
        for key in sorted(pending, key=lambda key: (hints[key], order.get(key, len(order)), key)):
            desired = round((hints[key] - low) * (count - 1) / (high - low)) if high > low else 0
            local = max(desired, last.get(columns[key], -1) + 1)
            local_rows[key], last[columns[key]] = local, local
            owners[key] = len(profiles)
        profiles.append(local_rows)

    occupied, column_low, column_high = defaultdict(set), {}, {}
    for key, row in rows.items():
        col = columns[key]
        occupied[col].add(row)
        column_low[col] = column_high[col] = row
    low, high = (0, 0) if rows else (None, None)
    remaining = {index for index, profile in enumerate(profiles) if profile}
    contacts, priorities, queue = {}, {}, []
    for index in sorted(remaining):
        profile = profiles[index]
        contacts[index] = sum(other in rows for key in profile for other in neighbors.get(key, ()))
        priorities[index] = (min(columns[key] for key in profile),
                             min(order.get(key, len(order)) for key in profile), index)
        heappush(queue, (-contacts[index], *priorities[index]))
    probes = maximum_probes = 0
    while remaining:
        previous_contacts, _, _, index = heappop(queue)
        if index not in remaining or -previous_contacts != contacts[index]:
            continue
        profile = profiles[index]
        anchors = sorted(rows[other] - local for key, local in profile.items()
                         for other in neighbors.get(key, ()) if other in rows)
        local_low, local_high = min(profile.values()), max(profile.values())
        preferred = anchors[(len(anchors) - 1) // 2] if anchors else -(local_low + local_high) // 2
        profile_low, profile_high = {}, {}
        for key, local in profile.items():
            col = columns[key]
            profile_low[col] = min(profile_low.get(col, local), local)
            profile_high[col] = max(profile_high.get(col, local), local)
        above = min((column_low[col] - 1 - profile_high[col]
                     for col in profile_low if col in column_low), default=preferred)
        below = max((column_high[col] + 1 - profile_low[col]
                     for col in profile_low if col in column_high), default=preferred)
        offsets = {preferred + delta for delta in _PLACEMENT_DELTAS} | {0, above, below}
        if anchors:
            offsets.update(anchors[position] for position in (0, len(anchors) // 4,
                                                             3 * len(anchors) // 4, len(anchors) - 1))
        assert len(offsets) <= _PLACEMENT_PROBE_LIMIT
        probes += len(offsets)
        maximum_probes = max(maximum_probes, len(offsets))
        prefix = [0]
        for value in anchors:
            prefix.append(prefix[-1] + value)
        best = None
        for offset in sorted(offsets):
            if any(local + offset in occupied[columns[key]] for key, local in profile.items()):
                continue
            new_low = min(low, local_low + offset) if low is not None else local_low + offset
            new_high = max(high, local_high + offset) if high is not None else local_high + offset
            growth = new_high - new_low + 1 - (high - low + 1 if low is not None else 0)
            split = bisect_left(anchors, offset)
            distance = (offset * split - prefix[split] + prefix[-1] - prefix[split]
                        - offset * (len(anchors) - split)) if anchors else abs(offset - preferred)
            # A new row must justify its footprint through shorter attachments.
            # Exact integer costs avoid dependence on edge iteration order.
            cost = (2 * growth * max(1, len(anchors)) + distance, growth,
                    abs(new_low + new_high), abs(offset - preferred), offset < preferred, offset)
            if best is None or cost < best[0]:
                best = cost, offset, new_low, new_high
        # The per-column above/below bounds always provide a clear candidate.
        _, offset, low, high = best
        remaining.remove(index)
        updates = defaultdict(int)
        for key, local in profile.items():
            col, row = columns[key], local + offset
            rows[key] = row
            occupied[col].add(row)
            column_low[col] = min(column_low.get(col, row), row)
            column_high[col] = max(column_high.get(col, row), row)
            for other in neighbors.get(key, ()):
                owner = owners.get(other)
                if owner in remaining:
                    updates[owner] += 1
        for owner, count in updates.items():
            contacts[owner] += count
            heappush(queue, (-contacts[owner], *priorities[owner]))
    # Empty global row numbers do not carry information or reserve geometry.
    normalized = {row: index for index, row in enumerate(sorted(set(rows.values())))}
    rows = {key: normalized[row] for key, row in rows.items()}
    if stats is not None:
        stats.update(packing="neighbor_anchored_occupancy", placement_probes=probes,
                     max_section_probes=maximum_probes, placement_probe_limit=_PLACEMENT_PROBE_LIMIT,
                     backbone_row=normalized[0] if primary_columns else None)
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


def _address_hints(request, children, edges, ports, columns, backbone):
    """Check nominated continuations against the complete routed topology."""
    hints = request.get("addressNeighbors", {})
    if not isinstance(hints, dict):
        raise TraceError("Invalid address-neighbor layout hints")
    if not hints:
        return {}
    incoming, outgoing = defaultdict(list), defaultdict(list)
    for edge in edges:
        source, target = ports[edge["source"]]["node"], ports[edge["target"]]["node"]
        outgoing[source].append(target)
        incoming[target].append(source)
    kinds = request.get("nodeShapes", {})
    for key, pair in hints.items():
        if (not isinstance(key, str) or key not in children or key in backbone
                or not isinstance(pair, list) or len(pair) != 2
                or any(not isinstance(item, str) or item not in children for item in pair)
                or kinds.get(key) != "address" or any(kinds.get(item) != "transaction" for item in pair)
                or incoming[key] != pair[:1] or outgoing[key] != pair[1:]
                or not columns[pair[0]] < columns[key] < columns[pair[1]]):
            raise TraceError("Address-neighbor layout hints disagree with graph topology")
    return hints


def _refine_address_rows(rows, columns, hints, *, stats=None):
    """Repair two-edge outliers with bounded probes before any routing exists.

    Only nominated addresses change cells. Transactions and every other object
    keep their assigned rows; no new row numbers are introduced or renumbered.
    This is a local proximity improvement, not a global crossing guarantee.
    """
    result = dict(rows)
    existing = set(rows.values())
    occupied = defaultdict(set)
    for key, row in rows.items():
        occupied[columns[key]].add(row)
    report = {"version": 1, "eligible": len(hints), "outliers": 0, "moved": 0,
              "probes": 0, "max_address_probes": 0, "probe_limit": _ADDRESS_PROBE_LIMIT,
              "row_distance_removed": 0}
    for key in sorted(hints, key=lambda key: (columns[key], key)):
        before, after = (result[other] for other in hints[key])
        low, high = min(before, after), max(before, after)
        current, col = result[key], columns[key]
        if low <= current <= high:
            continue
        report["outliers"] += 1
        middle = (low + high) // 2
        candidates = ({middle + delta for delta in _ADDRESS_ROW_DELTAS}
                      | {low - 1, low, low + 1, high - 1, high, high + 1, (low + high + 1) // 2})
        assert len(candidates) <= _ADDRESS_PROBE_LIMIT
        report["probes"] += len(candidates)
        report["max_address_probes"] = max(report["max_address_probes"], len(candidates))
        old_distance = abs(current - before) + abs(current - after)
        best = None
        for row in sorted(candidates):
            if row not in existing or row in occupied[col]:
                continue
            distance = abs(row - before) + abs(row - after)
            if distance >= old_distance:
                continue
            cost = (distance, max(abs(row - before), abs(row - after)), abs(row - current), row)
            if best is None or cost < best[0]:
                best = cost, row
        if best is None:
            continue
        cost, row = best
        occupied[col].remove(current)
        occupied[col].add(row)
        result[key] = row
        report["moved"] += 1
        report["row_distance_removed"] += old_distance - cost[0]
    if stats is not None:
        stats.update(report)
    return result


def assemble(request, groups, candidates, backbone_ids):
    """Use compact local envelopes for current section jobs.

    The gutter primitive remains useful for bounded local fallbacks and the
    much smaller graph of section boundaries, and for historical callers.
    """
    if request.get("localSectionGeometry"):
        from .trace_section_local import assemble_local
        return assemble_local(request, groups, candidates, backbone_ids)
    return assemble_gutters(request, groups, candidates, backbone_ids)


def assemble_gutters(request, groups, candidates, backbone_ids):
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
    ports = {}
    for key, node in children.items():
        _number(node["height"], positive=True)
        _number(node["width"], positive=True)
        for port in node.get("ports", []):
            port_id = port["id"]
            side = port.get("layoutOptions", {}).get("elk.port.side", "EAST")
            if port_id in ports or side not in {"WEST", "EAST", "NORTH", "SOUTH"}:
                raise TraceError("Invalid or duplicate section connector port")
            ports[port_id] = {"node": key, "side": side, "raw": port, "neighbors": []}
    edges, edge_ids, neighbors = [], set(), defaultdict(set)
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
        if a != b:
            neighbors[a].add(b)
            neighbors[b].add(a)
        labels = raw.get("labels", [])
        if len(labels) > 1:
            raise TraceError("Section layout requires at most one caption per connection")
        for label in labels:
            max_caption_width = max(max_caption_width, _number(label["width"], positive=True))
            _number(label["height"], positive=True)
        edges.append({"raw": raw, "source": source, "target": target})
    packing = {}
    rows = _rows(children, groups, candidates, columns, backbone, order, neighbors=neighbors, stats=packing)
    address_hints = _address_hints(request, children, edges, ports, columns, backbone)
    address_placement = {}
    rows = _refine_address_rows(rows, columns, address_hints, stats=address_placement)
    packing["address_placement"] = address_placement
    row_count, column_count = max(rows.values()) + 1, len(ranks)
    row_heights, column_widths = [0.0] * row_count, [0.0] * column_count
    for key, node in children.items():
        row_heights[rows[key]] = max(row_heights[rows[key]], node["height"])
        column_widths[columns[key]] = max(column_widths[columns[key]], node["width"])
    for edge in edges:
        a, b = ports[edge["source"]]["node"], ports[edge["target"]]["node"]
        edge["gap"] = min(row_count, (rows[a] + rows[b]) // 2 + 1)
    direct, direct_ports = {}, set()
    # Preserve the primary path as a straight lane across adjacent columns.
    # There is at most one selected primary object per column and one direct
    # connector per gap. Additional parallel transfers keep separate routes.
    for index, edge in sorted(enumerate(edges), key=lambda pair: pair[1]["raw"]["id"]):
        source, target = ports[edge["source"]], ports[edge["target"]]
        a, b = source["node"], target["node"]
        if (a in backbone and b in backbone and rows[a] == rows[b] == packing["backbone_row"]
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
        primary_row = packing["backbone_row"]
        # Branches may now sit above the primary path. Reserve a separate strip
        # below that gap's ordinary tracks for captions extending above its row.
        gap_heights[primary_row] += max(0.0, direct_height + 7 - row_heights[primary_row] / 2) + _MARGIN
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
    from .trace_route_cleanup import cleanup_routes
    short_routes = {}
    for edge in edges:
        source, target = ports[edge["source"]], ports[edge["target"]]
        a, b = source["node"], target["node"]
        if (source["side"] == "EAST" and target["side"] == "WEST"
                and columns[b] == columns[a] + 1):
            short_routes[edge["raw"]["id"]] = (a, b)
    routed, route_cleanup = cleanup_routes(nodes, routed, short_routes,
        node_shapes=request.get("nodeShapes", {}), width=total_width, height=total_height)
    return {"seed": candidates[0].get("seed", 1) if candidates else 1,
            "branchProfile": "flow_weighted", "branchBoundary": bool(request.get("boundaryOrdering", False)),
            "inputOrderPolicy": "geometry", "nodes": nodes, "edges": routed,
            "width": total_width, "height": total_height,
            "sectionGeometry": {"rows": row_count, "columns": column_count,
                                "routing": "interval_colored_gutters", "route_cleanup": route_cleanup, **packing}}
