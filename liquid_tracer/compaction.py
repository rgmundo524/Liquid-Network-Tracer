"""Conservative local compaction of an existing ELK display graph.

This is a presentation pass. It neither changes graph topology nor infers
ownership. Only geometrically checked moves are accepted; dense or exceptionally
long geometry can exhaust a local work budget and is left in place. The budget
limits optimization work, never the size or completeness of the exported graph.
"""

import copy
import math
import time
from collections import defaultdict
from itertools import product

from .common import TraceError
from .layout import HORIZONTAL_NODE_GAP
from .miro_frames import _padded
from .elk_layout import ALGORITHM, _default_attachments, _validate_graph, attachment_point, segment_hits_node, layout_metrics
from .layout_search_reporting import public_search_counts
from .edge_labels import caption_box, translate_label

ALGORITHM_COMPACTION = "local_address_components_v1"
LINKED_HORIZONTAL = float(HORIZONTAL_NODE_GAP)
NODE_SPACING = 80.0
COMPONENT_SPACING = 120.0
# ELK uses 35 within a layer and 60 between layers. Addresses can leave their
# original columns during this pass, so use the larger clearance for every
# changed node or route rather than accidentally reducing a between-layer gap.
EDGE_NODE_SPACING = 60.0
EDGE_SPACING = 22.0
_EPS = 1e-6
_CELL = 512.0
_ENTRY_CELLS = 64
_QUERY_CELLS = 4096
_QUERY_ITEMS = 4096


def _box(node):
    return (node["x"] - node["width"] / 2, node["y"] - node["height"] / 2,
            node["x"] + node["width"] / 2, node["y"] + node["height"] / 2)


def _expand(box, amount):
    return box[0] - amount, box[1] - amount, box[2] + amount, box[3] + amount


def _touch(a, b):
    return a[0] < b[2] - _EPS and b[0] < a[2] - _EPS and a[1] < b[3] - _EPS and b[1] < a[3] - _EPS


def _envelope(boxes):
    boxes = iter(boxes)
    first = next(boxes, None)
    if first is None:
        return None
    left, top, right, bottom = first
    for a, b, c, d in boxes:
        left, top, right, bottom = min(left, a), min(top, b), max(right, c), max(bottom, d)
    return left, top, right, bottom


def _within(box, container):
    return container is not None and (box[0] >= container[0] - _EPS and box[1] >= container[1] - _EPS
                                      and box[2] <= container[2] + _EPS and box[3] <= container[3] + _EPS)


def _segment_box(a, b):
    return min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])


def _point_segment(point, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    denominator = dx * dx + dy * dy
    t = max(0, min(1, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / denominator)) if denominator else 0
    return math.hypot(point[0] - a[0] - t * dx, point[1] - a[1] - t * dy)


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_meet(a, b, c, d):
    # Closed segments, including overlapping collinear routes and T junctions.
    if (_cross(a, b, c) * _cross(a, b, d) < -_EPS
            and _cross(c, d, a) * _cross(c, d, b) < -_EPS):
        return True
    return min(_point_segment(a, c, d), _point_segment(b, c, d),
               _point_segment(c, a, b), _point_segment(d, a, b)) < _EPS


def _segment_distance(a, b, c, d):
    if _segments_meet(a, b, c, d):
        return 0
    return min(_point_segment(a, c, d), _point_segment(b, c, d),
               _point_segment(c, a, b), _point_segment(d, a, b))


def _hits_box(a, b, box):
    """Closed segment versus open rectangle, with exact axis clipping."""
    lower, upper = 0.0, 1.0
    for start, delta, lo, hi in ((a[0], b[0] - a[0], box[0], box[2]),
                                (a[1], b[1] - a[1], box[1], box[3])):
        if abs(delta) < _EPS:
            if start <= lo + _EPS or start >= hi - _EPS:
                return False
        else:
            limits = sorted(((lo + _EPS - start) / delta, (hi - _EPS - start) / delta))
            lower, upper = max(lower, limits[0]), min(upper, limits[1])
    return lower < upper - _EPS


class _Budget:
    def __init__(self, size):
        self.remaining = max(250000, size * 40)
        self.truncated = False

    def spend(self):
        self.remaining -= 1
        if self.remaining < 0:
            self.truncated = True
            return False
        return True


class _Index:
    """Uniform cells with bounded overflow scans; incomplete queries fail closed."""
    def __init__(self, budget):
        self.budget = budget
        self.boxes = {}
        self.cells = defaultdict(set)
        self.memberships = {}
        self.overflow = set()

    @staticmethod
    def _cells(box, maximum):
        x1, y1, x2, y2 = (math.floor(box[0] / _CELL), math.floor(box[1] / _CELL),
                          math.floor(box[2] / _CELL), math.floor(box[3] / _CELL))
        if (x2 - x1 + 1) * (y2 - y1 + 1) > maximum:
            return None
        return [(x, y) for x in range(x1, x2 + 1) for y in range(y1, y2 + 1)]

    def add(self, key, box):
        self.remove(key)
        self.boxes[key] = box
        cells = self._cells(box, _ENTRY_CELLS)
        self.memberships[key] = cells
        if cells is None:
            self.overflow.add(key)
        else:
            for cell in cells:
                self.cells[cell].add(key)

    def remove(self, key):
        if key not in self.boxes:
            return
        cells = self.memberships.pop(key)
        if cells is None:
            self.overflow.discard(key)
        else:
            for cell in cells:
                self.cells[cell].discard(key)
                if not self.cells[cell]:
                    del self.cells[cell]
        del self.boxes[key]

    def query(self, box):
        cells = self._cells(box, _QUERY_CELLS)
        if cells is None or self.budget.remaining < 0:
            self.budget.truncated = True
            return None
        seen, found = set(), []
        # Sorted values ensure a reproducible accepted prefix if the budget is
        # exhausted; bounded buckets avoid materializing a huge dense query.
        buckets = [self.overflow, *(self.cells.get(cell, ()) for cell in cells)]
        for bucket in buckets:
            if len(bucket) > _QUERY_ITEMS:
                self.budget.truncated = True
                return None
            for key in sorted(bucket):
                if key in seen:
                    continue
                seen.add(key)
                if len(seen) > _QUERY_ITEMS or not self.budget.spend():
                    self.budget.truncated = True
                    return None
                candidate = self.boxes[key]
                if (candidate[0] <= box[2] + _EPS and box[0] <= candidate[2] + _EPS
                        and candidate[1] <= box[3] + _EPS and box[1] <= candidate[3] + _EPS):
                    found.append(key)
        return found


def _points(edge, nodes):
    attachment = edge.get("attachment") or _default_attachments(nodes[edge["source"]], nodes[edge["target"]])
    first = attachment_point(nodes[edge["source"]], attachment["startItem"])
    last = attachment_point(nodes[edge["target"]], attachment["endItem"])
    route = edge.get("route", [])
    raw = [first, *(route[1:-1] if edge.get("connector_shape", "straight") != "straight" else []), last]
    return [(point["x"], point["y"]) for point in raw]


def _length(points):
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


def _caption(edge, points):
    # Preserve ELK's reserved label box and the midpoint estimate used for
    # Miro. Miro chooses its own final route and caption position, so the
    # combined footprint is deliberately conservative.
    return _envelope([caption_box(edge, points), caption_box(edge, points, use_layout=False)])


def _edge_boxes(edge, points):
    boxes = [_segment_box(a, b) for a, b in zip(points, points[1:])]
    caption = _caption(edge, points)
    return [*boxes, *([caption] if caption else [])]


def _footprint(nodes, edges, point_map):
    return _envelope([*(_box(node) for node in nodes),
                      *(box for edge in edges for box in _edge_boxes(edge, point_map[edge["id"]]))])


def _dimensions(box):
    if box is None:
        return {"width": 0.0, "height": 0.0, "area": 0.0}
    width, height = max(0, box[2] - box[0]), max(0, box[3] - box[1])
    return {"width": round(width, 2), "height": round(height, 2), "area": round(width * height, 2)}


def _preferred(node, incident, nodes):
    producers = [nodes[edge["source"]] for edge in incident if edge["target"] == node["id"]
                 and nodes[edge["source"]]["kind"] == "transaction"]
    others = [nodes[edge["target"]] for edge in incident if edge["source"] == node["id"]
              and nodes[edge["target"]]["kind"] == "transaction"]
    return min(producers or others, key=lambda item: (math.hypot(item["x"] - node["x"], item["y"] - node["y"]), item["id"]), default=None)


def _measure(nodes, edges, points, fee_ids, adjacent, annotations, frame_groups):
    main_nodes = [node for key, node in nodes.items() if key not in fee_ids]
    main_edges = [edge for edge in edges.values() if edge["source"] not in fee_ids and edge["target"] not in fee_ids]
    main_box = _footprint(main_nodes, main_edges, points)
    board_boxes = [box for box in [_footprint(nodes.values(), edges.values(), points), *annotations] if box is not None]
    if frame_groups:
        board_boxes.extend(_padded(_envelope(_box(nodes[key]) for key in keys)) for keys in frame_groups if keys)
        board_boxes.append(_padded(_envelope(board_boxes)))
    board_box = _envelope(board_boxes)
    distance = 0.0
    for node in main_nodes:
        if node["kind"] == "address":
            preferred = _preferred(node, adjacent[node["id"]], nodes)
            if preferred:
                distance += math.hypot(node["x"] - preferred["x"], node["y"] - preferred["y"])
    return {"main": {**_dimensions(main_box), "edge_length": round(sum(_length(points[e["id"]]) for e in main_edges), 2),
                     "address_distance": round(distance, 2)}, "board": _dimensions(board_box)}, main_box


def _routes(edge, nodes, original_points):
    points = _points(edge, nodes)
    if edge.get("connector_shape", "straight") == "straight":
        return [points]
    first, last = points[0], points[-1]
    variants = []
    if 3 <= len(original_points) <= 64:
        repaired = [first, *original_points[1:-1], last]
        a, b = original_points[:2]
        if abs(a[1] - b[1]) < _EPS:
            repaired[1] = repaired[1][0], first[1]
        elif abs(a[0] - b[0]) < _EPS:
            repaired[1] = first[0], repaired[1][1]
        a, b = original_points[-2:]
        if abs(a[1] - b[1]) < _EPS:
            repaired[-2] = repaired[-2][0], last[1]
        elif abs(a[0] - b[0]) < _EPS:
            repaired[-2] = last[0], repaired[-2][1]
        variants.append(repaired)
    middle = (first[0] + last[0]) / 2
    variants.append([first, (middle, first[1]), (middle, last[1]), last])
    return [[point for i, point in enumerate(route) if not i or point != route[i - 1]] for route in variants]


def _shared_port_only(a, b, c, d, shared_ports):
    """Allow connectors to meet at the same port, never overlap beyond it."""
    if not _segments_meet(a, b, c, d):
        return True
    for point in shared_ports:
        if min(math.dist(point, a), math.dist(point, b)) > _EPS or min(math.dist(point, c), math.dist(point, d)) > _EPS:
            continue
        other1 = b if math.dist(point, a) < _EPS else a
        other2 = d if math.dist(point, c) < _EPS else c
        if abs(_cross(point, other1, other2)) > _EPS:
            return True
        dot = (other1[0] - point[0]) * (other2[0] - point[0]) + (other1[1] - point[1]) * (other2[1] - point[1])
        if dot <= 0:
            return True
    return False


class _Geometry:
    def __init__(self, nodes, edges, points, budget):
        self.nodes, self.edges, self.points = nodes, edges, points
        self.budget = budget
        self.node_index, self.segment_index, self.caption_index = (_Index(budget) for _ in range(3))
        self.segment_keys = {}
        for key, node in nodes.items():
            self.node_index.add(key, _box(node))
        for key in edges:
            self.update_edge(key)

    def update_edge(self, key):
        for segment in self.segment_keys.get(key, []):
            self.segment_index.remove(segment)
        route = self.points[key]
        self.segment_keys[key] = [(key, i) for i in range(len(route) - 1)]
        for i, (a, b) in enumerate(zip(route, route[1:])):
            self.segment_index.add((key, i), _segment_box(a, b))
        caption = _caption(self.edges[key], route)
        self.caption_index.remove(key)
        if caption:
            self.caption_index.add(key, caption)

    def safe(self, key, proposed, bounds):
        node = self.nodes[key]
        node_box = _box(node)
        changed = set(proposed)
        if not _within(node_box, bounds):
            return False
        neighbors = self.node_index.query(_expand(node_box, NODE_SPACING))
        if neighbors is None or any(other != key and _touch(_expand(node_box, NODE_SPACING), self.node_index.boxes[other]) for other in neighbors):
            return False
        segments = self.segment_index.query(_expand(node_box, EDGE_NODE_SPACING))
        if segments is None:
            return False
        for edge_id, i in segments:
            if edge_id not in changed and _hits_box(*self.points[edge_id][i:i + 2], _expand(node_box, EDGE_NODE_SPACING)):
                return False
        captions = self.caption_index.query(node_box)
        if captions is None or any(edge_id not in changed and _touch(node_box, self.caption_index.boxes[edge_id]) for edge_id in captions):
            return False
        new_captions = {}
        for edge_id, route in proposed.items():
            edge = self.edges[edge_id]
            if any(not _within(box, bounds) for box in _edge_boxes(edge, route)):
                return False
            endpoints = {edge["source"], edge["target"]}
            # Repaired bends must not fold a connector back over itself. This
            # loop is bounded because route repair keeps at most 64 old points.
            segments = list(zip(route, route[1:]))
            for i, (a, b) in enumerate(segments):
                for j in range(i + 1, len(segments)):
                    if not self.budget.spend():
                        return False
                    c, d = segments[j]
                    if not _shared_port_only(a, b, c, d, [b] if j == i + 1 else []):
                        return False
            for a, b in segments:
                candidates = self.node_index.query(_expand(_segment_box(a, b), EDGE_NODE_SPACING))
                if candidates is None:
                    return False
                for other in candidates:
                    if other == key:
                        continue
                    if other not in endpoints and _hits_box(a, b, _expand(self.node_index.boxes[other], EDGE_NODE_SPACING)):
                        return False
                # A repaired line must leave and arrive at its own physical
                # shapes instead of doubling back through their interior.
                for endpoint in endpoints:
                    if segment_hits_node({"x": a[0], "y": a[1]}, {"x": b[0], "y": b[1]}, self.nodes[endpoint]):
                        return False
                candidates = self.segment_index.query(_expand(_segment_box(a, b), EDGE_SPACING))
                if candidates is None:
                    return False
                for other_id, i in candidates:
                    if other_id in changed:
                        continue
                    other = self.edges[other_id]
                    c, d = self.points[other_id][i:i + 2]
                    shared = endpoints & {other["source"], other["target"]}
                    ports = []
                    for shared_id in shared:
                        own = route[0] if edge["source"] == shared_id else route[-1]
                        their = self.points[other_id][0] if other["source"] == shared_id else self.points[other_id][-1]
                        if math.dist(own, their) < _EPS:
                            ports.append(own)
                    if shared:
                        if not _shared_port_only(a, b, c, d, ports):
                            return False
                    elif _segment_distance(a, b, c, d) < EDGE_SPACING - _EPS:
                        return False
                candidates = self.caption_index.query(_segment_box(a, b))
                if candidates is None or any(other not in changed and _hits_box(a, b, self.caption_index.boxes[other]) for other in candidates):
                    return False
            caption = _caption(edge, route)
            if caption:
                new_captions[edge_id] = caption
                if _touch(caption, node_box):
                    return False
                candidates = self.node_index.query(caption)
                if candidates is None or any(_touch(caption, node_box if other == key else self.node_index.boxes[other]) for other in candidates):
                    return False
                candidates = self.caption_index.query(caption)
                if candidates is None or any(other not in changed and _touch(caption, self.caption_index.boxes[other]) for other in candidates):
                    return False
                candidates = self.segment_index.query(caption)
                if candidates is None or any(other not in changed and _hits_box(*self.points[other][i:i + 2], caption) for other, i in candidates):
                    return False
        # At most two incident edges are moved. Check those against each other
        # directly rather than excluding them from the index and losing checks.
        keys = sorted(proposed)
        if len(keys) == 2:
            one, two = keys
            for a, b in zip(proposed[one], proposed[one][1:]):
                for c, d in zip(proposed[two], proposed[two][1:]):
                    shared = [p for p in (proposed[one][0], proposed[one][-1]) if p in (proposed[two][0], proposed[two][-1])]
                    if not _shared_port_only(a, b, c, d, shared):
                        return False
                    if two in new_captions and _hits_box(a, b, new_captions[two]):
                        return False
                    if one in new_captions and _hits_box(c, d, new_captions[one]):
                        return False
            if one in new_captions and two in new_captions and _touch(new_captions[one], new_captions[two]):
                return False
        return True


def _eligible(node, incident, nodes, fee_ids):
    if node["kind"] != "address" or node["id"] in fee_ids or not 1 <= len(incident) <= 2:
        return None
    if any(edge.get("routing_exception") in ("return", "fee") for edge in incident):
        return None
    producers, consumers = [], []
    for edge in incident:
        source, target = nodes[edge["source"]], nodes[edge["target"]]
        other = source if target["id"] == node["id"] else target
        if other["kind"] != "transaction" or other["id"] in fee_ids or source["x"] >= target["x"]:
            return None
        (producers if edge["target"] == node["id"] else consumers).append(other)
    if len(producers) > 1 or len(consumers) > 1 or (producers and consumers and producers[0]["id"] == consumers[0]["id"]):
        return None
    low = producers[0]["x"] + producers[0]["width"] / 2 + LINKED_HORIZONTAL + node["width"] / 2 if producers else -math.inf
    high = consumers[0]["x"] - consumers[0]["width"] / 2 - LINKED_HORIZONTAL - node["width"] / 2 if consumers else math.inf
    if low > high + _EPS or not low - _EPS <= node["x"] <= high + _EPS:
        return None
    preferred = (producers or consumers)[0]
    return low if producers else high, preferred, low, high


def _sibling_order(nodes, adjacent):
    limits = defaultdict(list)
    # Compaction shortens an established layout, not its branch ordering.
    # Preserve neighbors from the entire dependency column as well as siblings
    # so an address cannot slip between another transaction's output group.
    columns = defaultdict(list)
    for key, node in nodes.items():
        if "column" in node:
            columns[node["column"]].append(key)
    for values in columns.values():
        ordered = sorted(values, key=lambda key: (nodes[key]["y"], key))
        for i, key in enumerate(ordered):
            limits[key].append((ordered[i - 1] if i else None,
                                ordered[i + 1] if i + 1 < len(ordered) else None))
    for node in nodes.values():
        if node["kind"] != "transaction":
            continue
        groups = defaultdict(set)
        for edge in adjacent[node["id"]]:
            outgoing = edge["source"] == node["id"]
            other = edge["target"] if outgoing else edge["source"]
            if nodes[other]["kind"] == "address":
                groups[outgoing].add(other)
        for siblings in groups.values():
            ordered = sorted(siblings, key=lambda key: (nodes[key]["y"], nodes[key]["x"], key))
            for i, key in enumerate(ordered):
                limits[key].append((ordered[i - 1] if i else None, ordered[i + 1] if i + 1 < len(ordered) else None))
    return limits


def _compact_addresses(nodes, edges, points, adjacent, fee_ids, bounds, budget, notify, locked_nodes=()):
    geometry = _Geometry(nodes, edges, points, budget)
    siblings = _sibling_order(nodes, adjacent)
    moved, skipped = 0, 0
    candidates = sorted((node for node in nodes.values() if node["kind"] == "address"), key=lambda n: (n["x"], n["y"], n["id"]))
    for index, node in enumerate(candidates):
        notify(index, len(candidates), "Compacting local address positions")
        key = node["id"]
        if key in locked_nodes:
            skipped += 1
            continue
        eligible = _eligible(node, adjacent[key], nodes, fee_ids)
        if not eligible or budget.remaining < 0:
            skipped += 1
            continue
        desired_x, preferred, low, high = eligible
        old_x, old_y = node["x"], node["y"]
        desired_y = preferred["y"]
        for before, after in siblings[key]:
            if before:
                desired_y = max(desired_y, nodes[before]["y"])
            if after:
                desired_y = min(desired_y, nodes[after]["y"])
        old_length = sum(_length(points[e["id"]]) for e in adjacent[key])
        old_distance = math.hypot(old_x - preferred["x"], old_y - preferred["y"])
        proposals = []
        # Small final steps retain useful reductions where a nearby connector
        # leaves less room than the earlier quarter-distance candidate allows.
        for x_fraction, y_fraction in ((1, 1), (1, 0), (.75, .75), (.5, .5), (.5, 0),
                                       (0, 1), (0, .5), (.25, .25), (.125, .125), (.0625, .0625)):
            x, y = old_x + (desired_x - old_x) * x_fraction, old_y + (desired_y - old_y) * y_fraction
            if (abs(x - old_x) + abs(y - old_y) > _EPS and low - _EPS <= x <= high + _EPS
                    and (x, y) not in proposals):
                proposals.append((x, y))
        accepted = False
        for x, y in proposals:
            if math.hypot(x - preferred["x"], y - preferred["y"]) >= old_distance - _EPS:
                continue
            node.update(x=x, y=y)
            route_options = [(edge["id"], _routes(edge, nodes, points[edge["id"]])) for edge in adjacent[key]]
            # Only two routes per incident edge, and at most two incident edges.
            for variant in product(*(options for _, options in route_options)):
                proposed = {edge_id: route for (edge_id, _), route in zip(route_options, variant)}
                if sum(_length(route) for route in proposed.values()) > old_length + _EPS:
                    continue
                if geometry.safe(key, proposed, bounds):
                    for edge_id, route in proposed.items():
                        points[edge_id] = route
                        edges[edge_id]["route"] = [{"x": a, "y": b} for a, b in route]
                        # The safety check used the new route's midpoint box.
                        # A previous ELK label position belongs to the old path.
                        edges[edge_id].pop("label_layout", None)
                        geometry.update_edge(edge_id)
                    geometry.node_index.add(key, _box(node))
                    moved += 1
                    accepted = True
                    break
            if accepted:
                break
            node.update(x=old_x, y=old_y)
        if not accepted:
            node.update(x=old_x, y=old_y)
            skipped += 1
    return moved, skipped


def _components(nodes, edges, fee_ids):
    parents = {key: key for key in nodes if key not in fee_ids}
    def find(key):
        root = key
        while root != parents[root]:
            root = parents[root]
        while key != root:
            next_key = parents[key]
            parents[key] = root
            key = next_key
        return root
    for edge in edges.values():
        a, b = edge["source"], edge["target"]
        if a in parents and b in parents:
            left, right = find(a), find(b)
            if left != right:
                parents[max(left, right)] = min(left, right)
    groups = defaultdict(list)
    for key in parents:
        groups[find(key)].append(key)
    owners = {key: owner for owner, keys in groups.items() for key in keys}
    group_edges, fixed = defaultdict(list), set()
    for edge in edges.values():
        a, b = owners.get(edge["source"]), owners.get(edge["target"])
        if a is not None and a == b:
            group_edges[a].append(edge["id"])
        else:
            # Fee objects remain a chronological row. Their incident main
            # components stay in place until global fee rerouting is available.
            fixed.update(owner for owner in (a, b) if owner is not None)
    return groups, group_edges, fixed


def _pack_components(nodes, edges, points, fee_ids, bounds, budget, notify, frame_groups, preserved_nodes=()):
    groups, group_edges, fixed = _components(nodes, edges, fee_ids)
    fee_components = len(fixed)
    # Named members may share a central row across disconnected components.
    # Translating those components independently would undo that organization.
    preserved_nodes = set(preserved_nodes)
    fixed.update(owner for owner, keys in groups.items() if preserved_nodes.intersection(keys))
    if len(groups) < 2 or bounds is None:
        return 0, 0, fee_components
    geometry_boxes = {owner: _footprint((nodes[key] for key in keys), (edges[key] for key in group_edges[owner]), points)
                      for owner, keys in groups.items()}
    frame_for_member = {}
    for members in frame_groups:
        frame = _padded(_envelope(_box(nodes[key]) for key in members)) if members else None
        for key in members:
            frame_for_member[key] = frame
    boxes = {}
    for owner, keys in groups.items():
        frames = {frame_for_member[key] for key in keys if key in frame_for_member}
        boxes[owner] = _envelope([geometry_boxes[owner], *frames])
    # Component envelopes include real generated frame padding and title room.
    # The original graph footprint remains a separate non-growth constraint.
    packing_bounds = _envelope(boxes.values())
    index = _Index(budget)
    for owner, box in boxes.items():
        index.add(owner, box)
    # Fee connectors are obstacles even for components unrelated to the fee.
    obstacle_index = _Index(budget)
    for edge in edges.values():
        if edge["source"] in fee_ids or edge["target"] in fee_ids:
            for i, box in enumerate(_edge_boxes(edge, points[edge["id"]])):
                obstacle_index.add((edge["id"], i), box)
    anchor_x, anchor_y = packing_bounds[:2]
    corners = [(anchor_x, anchor_y)]
    moved, skipped = 0, 0
    ordered = sorted(boxes, key=lambda key: (boxes[key][1], boxes[key][0], key))
    for number, owner in enumerate(ordered):
        notify(number, len(ordered), "Packing disconnected activity components")
        original = boxes[owner]
        if owner in fixed or budget.remaining < 0:
            skipped += 1
            continue
        width, height = original[2] - original[0], original[3] - original[1]
        original_score = ((original[2] - anchor_x) * (original[3] - anchor_y), original[1], original[0])
        options = [(anchor_x, original[1]), (original[0], anchor_y), *corners[-32:]]
        # A bounded push past blocking rectangles finds available shelf starts
        # without all-pairs component comparisons or graph copies per attempt.
        inspected, best = set(), None
        for x, y in options:
            for _ in range(6):
                if (x, y) in inspected:
                    break
                inspected.add((x, y))
                candidate = (x, y, x + width, y + height)
                score = ((candidate[2] - anchor_x) * (candidate[3] - anchor_y), y, x)
                dx, dy = x - original[0], y - original[1]
                moved_geometry = tuple(value + (dx if i % 2 == 0 else dy) for i, value in enumerate(geometry_boxes[owner]))
                if not _within(candidate, packing_bounds) or not _within(moved_geometry, bounds) or score >= original_score:
                    break
                contacts = index.query(_expand(candidate, COMPONENT_SPACING))
                if contacts is None:
                    break
                blockers = [key for key in contacts if key != owner and _touch(_expand(candidate, COMPONENT_SPACING), boxes[key])]
                if not blockers:
                    obstacles = obstacle_index.query(_expand(candidate, EDGE_NODE_SPACING))
                    if obstacles is not None and not obstacles and (best is None or score < best[0]):
                        best = score, candidate
                    break
                blocker = min(blockers, key=lambda key: (boxes[key][3], boxes[key][2], key))
                right = boxes[blocker][2] + COMPONENT_SPACING
                below = boxes[blocker][3] + COMPONENT_SPACING
                # Prefer preserving the component's left alignment, then try
                # a horizontal shelf if a vertical placement cannot fit.
                if below + height <= packing_bounds[3] + _EPS:
                    y = below
                elif right + width <= packing_bounds[2] + _EPS:
                    x = right
                else:
                    break
        if best is None:
            skipped += 1
            box = original
        else:
            _, box = best
            dx, dy = box[0] - original[0], box[1] - original[1]
            for key in groups[owner]:
                nodes[key]["x"] += dx
                nodes[key]["y"] += dy
            for key in group_edges[owner]:
                points[key] = [(x + dx, y + dy) for x, y in points[key]]
                # Translate all stored bends too, including bends ignored by
                # current straight mode and used if curves are selected later.
                if edges[key].get("route"):
                    edges[key]["route"] = [{"x": p["x"] + dx, "y": p["y"] + dy} for p in edges[key]["route"]]
                    translate_label(edges[key], dx, dy)
            boxes[owner] = box
            index.add(owner, box)
            moved += 1
        corners.extend(((box[2] + COMPONENT_SPACING, box[1]), (box[0], box[3] + COMPONENT_SPACING)))
    return moved, skipped, fee_components


def compact_graph(graph, progress=None):
    """Return a complete deep copy with checked address/component compaction.

    Transactions move only with their entire connected component. Percent ports,
    identities, labels, dimensions, fee order and evidence metadata survive.
    The ELK algorithm identity remains intact; this pass has a separate report.
    """
    if not isinstance(graph, dict) or graph.get("layout", {}).get("algorithm") != ALGORITHM:
        raise TraceError("Compact graph requires an existing ELK layout; create an ELK preview first")
    _validate_graph(graph, graph.get("graph_options", {}).get("connector_style", "straight"))
    result = copy.deepcopy(graph)
    nodes = {node["id"]: node for node in result["nodes"]}
    edges = {edge["id"]: edge for edge in result["edges"]}
    fee_ids = {key for key, item in result.get("fee_items", {}).items() if item.get("endpoint") == "shapes" and key in nodes}
    adjacent = defaultdict(list)
    try:
        points = {key: _points(edge, nodes) for key, edge in edges.items()}
        for route in points.values():
            if any(not math.isfinite(value) for point in route for value in point):
                raise ValueError("nonfinite route")
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError) as exc:
        raise TraceError("Cannot compact invalid connector geometry") from exc
    for edge in edges.values():
        adjacent[edge["source"]].append(edge)
        adjacent[edge["target"]].append(edge)
    # Include the same row-based legend dimensions used by the Miro plan.
    from .legend_miro import bounds as legend_bounds
    annotations = [(x - width / 2, y - height / 2, x + width / 2, y + height / 2)
                   for x, y, width, height in legend_bounds(result)]
    try:
        frame_groups = [tuple(group["shape_keys"]) for group in result.get("activity_frames", {}).get("activities", [])]
        if any(key not in nodes for group in frame_groups for key in group):
            raise ValueError("missing frame member")
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise TraceError("Cannot compact invalid activity frame metadata") from exc
    before, bounds = _measure(nodes, edges, points, fee_ids, adjacent, annotations, frame_groups)
    before_metrics = layout_metrics(graph)
    budget = _Budget(len(nodes) + len(edges))
    started, last = time.monotonic(), [0.0]
    def notify(completed, total, message, force=False):
        now = time.monotonic()
        if progress and (force or now - last[0] >= 5):
            try:
                progress({"phase": "compacting", "message": message, "completed": completed, "total": total,
                          "elapsed_seconds": int(now - started)})
            except Exception:
                pass
            last[0] = now
    notify(0, len(nodes), "Compacting the saved ELK layout", True)
    locked_nodes = set(result["layout"].get("change_outputs", {}).get("locked_nodes", []))
    # A manual hub owns its separate entry lane. Whole connected components
    # may still translate, but a local address move must not pull the hub back
    # into the branch it was explicitly separated from.
    locked_nodes.update(key for key, node in nodes.items() if node.get("layout_hub") is True)
    from .named_group_layout import center_metrics, group_structure
    centered_nodes = group_structure(result)["core"]
    locked_nodes.update(centered_nodes)
    moved_addresses, skipped_addresses = _compact_addresses(nodes, edges, points, adjacent, fee_ids, bounds, budget, notify, locked_nodes)
    _, current_bounds = _measure(nodes, edges, points, fee_ids, adjacent, annotations, frame_groups)
    moved_components, skipped_components, fee_components = _pack_components(
        nodes, edges, points, fee_ids, current_bounds, budget, notify, frame_groups, centered_nodes)
    after, _ = _measure(nodes, edges, points, fee_ids, adjacent, annotations, frame_groups)
    main = [node for key, node in nodes.items() if key not in fee_ids]
    result["layout"]["main_top"] = min((node["y"] - node["height"] / 2 for node in main), default=160)
    result["layout"]["main_bottom"] = max((node["y"] + node["height"] / 2 for node in main), default=320)
    if "elk_metrics" not in result["layout"] and "metrics" in result["layout"]:
        result["layout"]["elk_metrics"] = copy.deepcopy(result["layout"]["metrics"])
    result["layout"]["metrics"] = {"before": before_metrics, "after": layout_metrics(result),
                                   "estimated": True, "miro_routes_exact": False,
                                   "method": "compaction_comparison", "acceptance_uses_metrics": False}
    result["layout"]["metrics"].update(public_search_counts(result["layout"].get("search")))
    if result.get("graph_options", {}).get("center_name"):
        result["layout"]["named_group"] = center_metrics(result)
    from .transaction_neighborhoods import neighborhood_metrics
    result["layout"].setdefault("branch_organization", {})["neighborhoods"] = neighborhood_metrics(result)
    result["layout"]["compaction"] = {
        "algorithm": ALGORITHM_COMPACTION, "version": 1, "before": before, "after": after,
        "moved_addresses": moved_addresses, "moved_components": moved_components,
        "accepted_moves": moved_addresses + moved_components,
        "skipped_moves": skipped_addresses + skipped_components,
        "truncated": budget.truncated, "unchanged": not (moved_addresses or moved_components),
        "clearances": {"linked_horizontal": LINKED_HORIZONTAL, "node_node": NODE_SPACING,
                       "components": COMPONENT_SPACING, "edge_node": EDGE_NODE_SPACING, "edge_edge": EDGE_SPACING},
        "fee_components_preserved": fee_components,
        "labels_estimated": True, "miro_routes_exact": False,
        "address_distance_basis": "Each address to its nearest creating transaction, or spending transaction for external inputs",
        "notice": "Checked moves preserve spacing; uncertain moves stay in place. Caption bounds are estimates and Miro routes may differ.",
    }
    notify(len(nodes), len(nodes), "Local compaction ready", True)
    return result
