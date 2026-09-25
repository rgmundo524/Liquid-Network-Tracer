"""Keep external input context out of the displayed connector corridors.

ELK bends are not sent to Miro. Check both saved paths and attachment-based
estimates before moving a context shape, then check every changed connector.
This repair may use more space; shrinking a graph must not trap a circle on a
line. Work limits constrain the search only, never the exported evidence.
"""

import math
from collections import defaultdict
from itertools import combinations, product
from statistics import median

from .compaction import (_Budget, _Index, _box, _expand, _hits_box, _segment_box,
                         _shared_port_only, _touch, EDGE_NODE_SPACING,
                         LINKED_HORIZONTAL, NODE_SPACING)
from .edge_labels import caption_box, caption_text
from .elk_layout import segment_hits_node
from .routing_estimates import estimated_miro_route, route_variants


def context_candidates(graph):
    """Only input-only context, including summaries and shared external inputs."""
    from .named_group_layout import group_structure

    nodes = {node["id"]: node for node in graph["nodes"]}
    adjacent = defaultdict(list)
    for edge in graph["edges"]:
        adjacent[edge["source"]].append(edge)
        adjacent[edge["target"]].append(edge)
    locked = set(graph.get("layout", {}).get("change_outputs", {}).get("locked_nodes", []))
    locked.update(group_structure(graph)["core"])
    locked.update(graph.get("fee_items", {}))
    eligible = {}
    for key, node in nodes.items():
        incident = adjacent[key]
        if (node["kind"] not in ("address", "context_group") or key in locked
                or node.get("layout_hub") or node.get("change_output") or not incident):
            continue
        if all(edge["source"] == key and edge.get("role") == "context_input"
               and nodes[edge["target"]]["kind"] == "transaction"
               and not edge.get("change_output")
               and not edge.get("details", {}).get("validated_trace_link")
               and not edge.get("details", {}).get("vin", {}).get("is_pegin")
               and not edge.get("details", {}).get("vin", {}).get("is_coinbase")
               for edge in incident):
            eligible[key] = incident
    return eligible


class _Clearance:
    def __init__(self, nodes, edges, budget):
        self.nodes, self.edges, self.budget = nodes, edges, budget
        self.node_index, self.segment_index, self.caption_index = (_Index(budget) for _ in range(3))
        self.routes, self.segment_keys, self.caption_keys = {}, {}, {}
        self.segments = {}
        for key, node in nodes.items():
            self.node_index.add(key, _box(node))
        for key in edges:
            self.update_edge(key)

    def update_edge(self, key):
        for part in self.segment_keys.get(key, []):
            self.segment_index.remove(part)
            del self.segments[part]
        for part in self.caption_keys.get(key, []):
            self.caption_index.remove(part)
        self.segment_keys[key], self.caption_keys[key] = [], []
        self.routes[key] = route_variants(self.edges[key], self.nodes)
        for variant, route in enumerate(self.routes[key]):
            for index, (a, b) in enumerate(zip(route, route[1:])):
                part = key, variant, index
                self.segment_keys[key].append(part)
                self.segments[part] = a, b
                self.segment_index.add(part, _segment_box(a, b))
            if caption_text(self.edges[key]):
                part = key, variant
                self.caption_keys[key].append(part)
                self.caption_index.add(part, caption_box(self.edges[key], route))

    def shape_clear(self, key, changed):
        box = _box(self.nodes[key])
        neighbors = self.node_index.query(_expand(box, NODE_SPACING))
        if neighbors is None or any(other != key and _touch(_expand(box, NODE_SPACING), self.node_index.boxes[other])
                                    for other in neighbors):
            return False
        padded = _expand(box, EDGE_NODE_SPACING)
        segments = self.segment_index.query(padded)
        if segments is None or any(part[0] not in changed and _hits_box(*self.segments[part], padded)
                                   for part in segments):
            return False
        captions = self.caption_index.query(box)
        return captions is not None and not any(part[0] not in changed and _touch(box, self.caption_index.boxes[part])
                                                for part in captions)

    def routes_clear(self, key, proposed):
        """Every changed route must miss unrelated nodes and its own shapes."""
        for edge_id, variants in proposed.items():
            edge = self.edges[edge_id]
            endpoints = {edge["source"], edge["target"]}
            for route in variants:
                for a, b in zip(route, route[1:]):
                    if not self.budget.spend():
                        return False
                    neighbors = self.node_index.query(_expand(_segment_box(a, b), EDGE_NODE_SPACING))
                    if neighbors is None:
                        return False
                    if any(other not in endpoints and _hits_box(a, b, _expand(self.node_index.boxes[other], EDGE_NODE_SPACING))
                           for other in neighbors):
                        return False
                    if any(segment_hits_node({"x": a[0], "y": a[1]}, {"x": b[0], "y": b[1]}, self.nodes[endpoint])
                           for endpoint in endpoints):
                        return False
                if caption_text(edge):
                    # New paths invalidate reserved ELK caption positions.
                    box = caption_box(edge, route, use_layout=False)
                    neighbors = self.node_index.query(box)
                    if neighbors is None or _touch(box, _box(self.nodes[key])) or any(
                            other != key and _touch(box, self.node_index.boxes[other]) for other in neighbors):
                        return False
        return True

    def conflicts(self, proposed):
        """Count external crossings, treating a summary's same-target fan as one.

        Moving one summary moves all its vin ports together. The parallel
        connectors to that same transaction are its presentation fan, not
        separate branches to keep apart. Preserve every edge and attachment,
        but do not score this fan's internal overlaps quadratically. Connectors
        reaching another transaction still receive the ordinary pair checks.
        """
        families = defaultdict(list)
        for key in sorted(proposed):
            edge = self.edges[key]
            if (edge.get("role") == "context_input"
                    and self.nodes[edge["source"]]["kind"] == "context_group"
                    and self.nodes[edge["target"]]["kind"] == "transaction"):
                family = "summary", edge["source"], edge["target"]
            else:
                family = "edge", key
            families[family].append(key)
        external = self.segment_index
        if any(len(keys) > 1 for keys in families.values()):
            # The usual spatial query would repeatedly scan the summary's own
            # hundreds of segments only to discard them below. Build a bounded
            # index of exactly the external segments the same check uses.
            external = _Index(self.budget)
            for part, box in self.segment_index.boxes.items():
                if not self.budget.spend():
                    return None
                if part[0] not in proposed:
                    external.add(part, box)
        pairs = set()
        for edge_id, variants in proposed.items():
            edge = self.edges[edge_id]
            for route in variants:
                for a, b in zip(route, route[1:]):
                    candidates = external.query(_segment_box(a, b))
                    if candidates is None:
                        return None
                    for part in candidates:
                        other_id = part[0]
                        if other_id == edge_id or other_id in proposed:
                            continue
                        other = self.edges[other_id]
                        other_route = self.routes[other_id][part[1]]
                        shared = {edge["source"], edge["target"]} & {other["source"], other["target"]}
                        ports = []
                        for endpoint in shared:
                            own = route[0] if edge["source"] == endpoint else route[-1]
                            theirs = other_route[0] if other["source"] == endpoint else other_route[-1]
                            if math.dist(own, theirs) < 1e-6:
                                ports.append(own)
                        if not _shared_port_only(a, b, *self.segments[part], ports):
                            pairs.add(tuple(sorted((edge_id, other_id))))
        for first_family, second_family in combinations(families.values(), 2):
            for one, two in product(first_family, second_family):
                for first in proposed[one]:
                    for second in proposed[two]:
                        shared = [point for point in (first[0], first[-1]) if point in (second[0], second[-1])]
                        for a, b in zip(first, first[1:]):
                            for c, d in zip(second, second[1:]):
                                if not self.budget.spend():
                                    return None
                                if not _shared_port_only(a, b, c, d, shared):
                                    pairs.add(tuple(sorted((one, two))))
        return pairs

    def candidates(self, key, incident):
        node = self.nodes[key]
        targets = {edge["target"]: self.nodes[edge["target"]] for edge in incident}
        desired_x = min(_box(target)[0] for target in targets.values()) - LINKED_HORIZONTAL - node["width"] / 2
        desired_y = median(target["y"] for target in targets.values())
        step_x, step_y = node["width"] + NODE_SPACING, node["height"] + NODE_SPACING
        xs = {desired_x - step_x * multiple for multiple in (0, 1, 2, 4)}
        xs.add(min(desired_x, node["x"]))
        ys = {node["y"], desired_y}
        ys.update(desired_y + step_y * multiple for multiple in (-8, -6, -4, -3, -2, -1, 1, 2, 3, 4, 6, 8))
        anchor_xs, anchor_ys = set(xs), set(ys)
        # Obstacle boundaries supply close-fitting slots between the fixed
        # transaction rows; a uniform grid alone misses narrow safe corridors.
        window = (desired_x - 4 * step_x - node["width"], desired_y - 8 * step_y,
                  desired_x + node["width"], desired_y + 8 * step_y)
        changed = {edge["id"] for edge in incident}
        for index, padding, skip in ((self.node_index, NODE_SPACING, lambda part: part == key),
                                     (self.segment_index, EDGE_NODE_SPACING, lambda part: part[0] in changed),
                                     (self.caption_index, 8, lambda part: part[0] in changed)):
            found = index.query(window)
            if found is None:
                return []
            for part in found:
                if skip(part):
                    continue
                left, top, right, bottom = index.boxes[part]
                ys.update((top - node["height"] / 2 - padding - 1,
                           bottom + node["height"] / 2 + padding + 1))
                xs.update(value for value in (left - node["width"] / 2 - padding - 1,
                                               right + node["width"] / 2 + padding + 1)
                          if desired_x - 4 * step_x <= value <= desired_x)
        # Keep the escape positions even when many nearby connector ports
        # produce almost identical obstacle boundaries.
        xs = anchor_xs | set(sorted(xs - anchor_xs, key=lambda x: (abs(x - desired_x), x))[:12])
        ys = anchor_ys | set(sorted(ys - anchor_ys, key=lambda y: (abs(y - desired_y), y))[:40])
        return sorted(((x, y) for x in xs for y in ys),
                      key=lambda point: (math.hypot(point[0] - desired_x, point[1] - desired_y),
                                         math.dist(point, (node["x"], node["y"])), point))


def repair_context_clearance(graph):
    """Repair blocked input-only context, preserving all evidence and ports."""
    from .branch_layout import BRANCH_LAYOUT_VERSION

    nodes = {node["id"]: node for node in graph["nodes"]}
    edges = {edge["id"]: edge for edge in graph["edges"]}
    eligible = context_candidates(graph)
    budget = _Budget(len(nodes) + len(edges))
    geometry = _Clearance(nodes, edges, budget) if eligible else None
    moved, unresolved, added_crossings = 0, [], 0
    for key in sorted(eligible, key=lambda key: (nodes[key]["x"], nodes[key]["y"], key)):
        incident = eligible[key]
        changed = {edge["id"] for edge in incident}
        old_routes = {edge_id: geometry.routes[edge_id] for edge_id in changed}
        already_clear = geometry.shape_clear(key, changed) and geometry.routes_clear(key, old_routes)
        if already_clear and nodes[key]["kind"] != "context_group":
            continue
        node = nodes[key]
        original = node["x"], node["y"]
        targets = {edge["target"] for edge in incident}
        def distance(point):
            return sum(math.dist(point, (nodes[target]["x"], nodes[target]["y"])) for target in targets)
        original_distance = distance(original)
        if already_clear and len(targets) == 1:
            target = nodes[next(iter(targets))]
            nearest = (_box(target)[0] - LINKED_HORIZONTAL - node["width"] / 2, target["y"])
            if math.dist(original, nearest) < 1e-6:
                continue
        baseline = geometry.conflicts(old_routes)
        best = None
        if baseline is not None:
            for point in geometry.candidates(key, incident):
                if not budget.spend():
                    break
                if already_clear and distance(point) >= original_distance - 1e-6:
                    continue
                node["x"], node["y"] = point
                if not geometry.shape_clear(key, changed):
                    continue
                proposed = {edge["id"]: [estimated_miro_route(edge, nodes)] for edge in incident}
                if not geometry.routes_clear(key, proposed):
                    continue
                conflicts = geometry.conflicts(proposed)
                if conflicts is None:
                    break
                cost = len(conflicts - baseline)
                # A summary already clear of lines may shorten its fan, but
                # proximity alone never justifies introducing a new crossing.
                if already_clear and cost:
                    continue
                if best is None or cost < best[0]:
                    best = cost, point, proposed
                if cost == 0:
                    break
        node["x"], node["y"] = original
        if best is None:
            if not already_clear:
                unresolved.append(key)
            continue
        cost, point, proposed = best
        node["x"], node["y"] = point
        geometry.node_index.add(key, _box(node))
        for edge in incident:
            edge["route"] = [{"x": x, "y": y} for x, y in proposed[edge["id"]][0]]
            edge.pop("label_layout", None)
            geometry.update_edge(edge["id"])
        moved += int(point != original)
        added_crossings += cost
    layout = graph.setdefault("layout", {})
    fees = set(graph.get("fee_items", {}))
    main = [node for key, node in nodes.items() if key not in fees]
    if main:
        layout["main_top"] = min(_box(node)[1] for node in main)
        layout["main_bottom"] = max(_box(node)[3] for node in main)
    organization = layout.setdefault("branch_organization", {})
    organization["version"] = BRANCH_LAYOUT_VERSION
    organization["context_clearance"] = {
        "candidates": len(eligible), "moved": moved, "unresolved": len(unresolved),
        "unresolved_nodes": unresolved,
        "checks_truncated": budget.truncated, "added_crossing_pairs": added_crossings,
        "edge_node_clearance": EDGE_NODE_SPACING, "miro_routes_exact": False,
    }
    return graph
