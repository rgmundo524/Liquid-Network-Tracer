"""Explicit change-output presentation, independent of tracing and ownership.

ELK supplies horizontal placement and its normal unconstrained ordering. This
pass adds rows only where an investigator selected an exact output. Shared
address objects are never duplicated to satisfy incompatible row requests.
"""

import copy
import heapq
import math
from bisect import bisect_left
from collections import defaultdict

from .common import output_kind
from .connector_styles import routed_shape
from .edge_labels import translate_label
from .input_order import centered_input_positions


SPACING = 80.0
COMPONENT_SPACING = 120.0
NOTICE = ("Change is an investigator designation, not an on-chain ownership determination. "
          "Shared addresses or conflicting rows can prevent alignment; skipped selections have no enforced change row.")


def annotate_changes(graph, state):
    """Attach exact-outpoint semantics before layout, without changing evidence."""
    assignments = state.get("service_controls", {}).get("change_outputs", {})
    if not assignments:
        return
    edges = {edge["id"]: edge for edge in graph["edges"]}
    available, skipped = [], []
    for txid, assignment in sorted(assignments.items()):
        vout = assignment.get("vout") if isinstance(assignment, dict) else None
        entry = {"txid": txid, "vout": vout, "outpoint": f"{txid}:{vout}"}
        record = state.get("transactions", {}).get(txid)
        if not record:
            skipped.append({**entry, "reason": "Transaction is not present in this saved graph."})
            continue
        outputs = record["data"]["vout"]
        if type(vout) is not int or not 0 <= vout < len(outputs):
            skipped.append({**entry, "reason": "Selected vout is not present in this transaction."})
            continue
        if output_kind(outputs[vout]) != "spendable":
            skipped.append({**entry, "reason": "Selected vout is not a spendable output."})
            continue
        edge = edges.get("out:" + entry["outpoint"])
        if not edge:
            skipped.append({**entry, "reason": "Selected output is not visible in this graph."})
            continue
        edge["change_output"] = copy.deepcopy(assignment)
        edge["label"] += " · Change"
        available.append({**entry, "edge_id": edge["id"]})
    graph["change_outputs"] = {"designations": available, "skipped": skipped, "notice": NOTICE}
    graph["notice"] += " " + NOTICE


class _Groups:
    def __init__(self, keys):
        self.parent = {key: key for key in keys}

    def find(self, key):
        root = key
        while root != self.parent[root]:
            root = self.parent[root]
        while key != root:
            following = self.parent[key]
            self.parent[key] = root
            key = following
        return root

    def join(self, left, right):
        left, right = self.find(left), self.find(right)
        if left != right:
            self.parent[max(left, right)] = min(left, right)

    def members(self):
        result = defaultdict(list)
        for key in self.parent:
            result[self.find(key)].append(key)
        return result


class _HorizontalFloor:
    """Coordinate-compressed range maxima, including empty change-row spans.

    Range query/update costs O(log n), independent of the physical row width.
    The segment tree's recursion is logarithmic, never graph-depth traversal.
    """
    def __init__(self, spans):
        self.coordinates = sorted({value for span in spans for value in span})
        self.size = max(1, len(self.coordinates) - 1)
        self.maximum = [-math.inf] * (4 * self.size)
        self.lazy = [-math.inf] * (4 * self.size)

    def _visit(self, left, right, value, index, start, stop):
        if left <= start and stop <= right:
            if value is not None:
                self.maximum[index] = max(self.maximum[index], value)
                self.lazy[index] = max(self.lazy[index], value)
            return self.maximum[index]
        middle = (start + stop) // 2
        found = self.lazy[index]
        if left < middle:
            found = max(found, self._visit(left, right, value, index * 2 + 1, start, middle))
        if right > middle:
            found = max(found, self._visit(left, right, value, index * 2 + 2, middle, stop))
        if value is not None:
            self.maximum[index] = max(self.lazy[index], self.maximum[index * 2 + 1], self.maximum[index * 2 + 2])
        return found

    def range(self, span, value=None):
        left, right = (bisect_left(self.coordinates, point) for point in span)
        return self._visit(left, right, value, 0, 0, self.size)


def _row_span(main, keys):
    return (min(main[key]["x"] - main[key]["width"] / 2 for key in keys) - SPACING / 2,
            max(main[key]["x"] + main[key]["width"] / 2 for key in keys) + SPACING / 2)


def _constraints(main, proposals):
    rows, blocks = _Groups(main), _Groups(main)
    for proposal in proposals:
        source = proposal["source"]
        for key in proposal["row"]:
            rows.join(source, key)
            blocks.join(source, key)
        for key in proposal["below"]:
            blocks.join(source, key)
    members = rows.members()
    following, previous = defaultdict(set), defaultdict(set)
    bad = set()
    for keys in members.values():
        ordered = sorted(keys, key=lambda key: (main[key]["x"], key))
        right = -math.inf
        for key in ordered:
            node = main[key]
            if node["x"] - node["width"] / 2 < right + SPACING - 1e-6:
                bad.add(blocks.find(key))
            right = max(right, node["x"] + node["width"] / 2)
    for proposal in proposals:
        source = rows.find(proposal["source"])
        for key in proposal["below"]:
            target = rows.find(key)
            following[source].add(target)
            previous[target].add(source)
    indegree = {key: len(previous[key]) for key in members}
    ready = [key for key, count in indegree.items() if not count]
    while ready:
        source = ready.pop()
        for target in following[source]:
            indegree[target] -= 1
            if not indegree[target]:
                ready.append(target)
    # Removing the entire connected constraint block resolves all its cycles
    # together. There is no recursive or per-selection quadratic retry loop.
    for root, count in indegree.items():
        if count:
            bad.add(blocks.find(root))
    rejected = [proposal for proposal in proposals if blocks.find(proposal["source"]) in bad]
    return rows, members, following, previous, rejected


def _pack_rows(main, proposals, components):
    rows, members, following, previous, _ = _constraints(main, proposals)
    affected = {components.find(proposal["source"]) for proposal in proposals}
    relevant = {root: keys for root, keys in members.items() if components.find(keys[0]) in affected}
    component_rows = defaultdict(set)
    for root, keys in relevant.items():
        component_rows[components.find(keys[0])].add(root)
    for roots in component_rows.values():
        desired, heights, spans = {}, {}, {}
        for root in roots:
            keys = relevant[root]
            anchor = min(keys, key=lambda key: (main[key]["kind"] != "transaction", main[key]["column"], key))
            desired[root] = main[anchor]["y"]
            heights[root] = max(main[key]["height"] for key in keys)
            spans[root] = _row_span(main, keys)
        indegree = {root: len(previous[root]) for root in roots}
        ready = [(desired[root], root) for root, count in indegree.items() if not count]
        heapq.heapify(ready)
        placed, floor = {}, _HorizontalFloor(spans.values())
        while ready:
            _, root = heapq.heappop(ready)
            keys = relevant[root]
            y = max([desired[root]] + [placed[parent] + (heights[parent] + heights[root]) / 2 + SPACING
                                      for parent in previous[root]])
            # Reserve the entire horizontal spine, including gaps between its
            # objects, so unrelated inputs cannot sit on a straight change link.
            y = max(y, floor.range(spans[root]) + heights[root] / 2 + SPACING)
            placed[root] = y
            for key in keys:
                node = main[key]
                node["y"] = round(y, 4)
            floor.range(spans[root], y + heights[root] / 2)
            for target in following[root]:
                indegree[target] -= 1
                if not indegree[target]:
                    heapq.heappush(ready, (desired[target], target))
    return members


def _separate_components(main, components, original, rows):
    groups = list(components.members().values())
    groups.sort(key=lambda keys: (min(original[key][1] - main[key]["height"] / 2 for key in keys), min(keys)))
    spans = {root: _row_span(main, keys) for root, keys in rows.items()}
    component_rows = defaultdict(list)
    for root, keys in rows.items():
        component_rows[components.find(keys[0])].append(root)
    floor = _HorizontalFloor(spans.values())
    for keys in groups:
        roots = component_rows[components.find(keys[0])]
        bounds = {root: (min(main[key]["y"] - main[key]["height"] / 2 for key in rows[root]),
                         max(main[key]["y"] + main[key]["height"] / 2 for key in rows[root])) for root in roots}
        shift = max([0.0] + [floor.range(spans[root]) + COMPONENT_SPACING - bounds[root][0] for root in roots])
        for key in keys:
            node = main[key]
            node["y"] = round(node["y"] + shift, 4)
        for root in roots:
            floor.range(spans[root], bounds[root][1] + shift)


def _centered_output_positions(graph, nodes, aligned, fee_ids):
    """Keep sibling output ports below the explicitly centered change spine.

    Row packing can move a former upper output below the transaction. Its old
    ELK port then crosses the new change row regardless of the random seed.
    Reorder physical ports by their final opposite attachment, retaining every
    edge's original vout and evidence. Fee routes use their separate lane.
    """
    from .elk_layout import attachment_point, _default_attachments

    outgoing = defaultdict(list)
    for edge in graph["edges"]:
        if nodes[edge["source"]]["kind"] == "transaction" and edge["target"] not in fee_ids:
            outgoing[edge["source"]].append(edge)
    result = {}
    for edges in outgoing.values():
        if not any(edge["id"] in aligned for edge in edges):
            continue

        def destination(edge):
            source, target = nodes[edge["source"]], nodes[edge["target"]]
            attachment = edge.get("attachment") or _default_attachments(source, target)
            point = attachment_point(target, attachment["endItem"])
            suffix = edge["id"].rsplit(":", 1)[-1]
            index = int(suffix) if suffix.isascii() and suffix.isdecimal() else float("inf")
            return point["y"], index, edge["id"]

        siblings = sorted((edge for edge in edges if edge["id"] not in aligned), key=destination)
        for index, edge in enumerate(siblings, start=1):
            result[edge["id"]] = 50 + 50 * index / (len(siblings) + 1)
    return result


def _routes(graph, nodes, original, aligned):
    from .elk_layout import attachment_point, _default_attachments
    input_positions = centered_input_positions(graph, aligned)
    fee_ids = {key for key, value in graph.get("fee_items", {}).items() if value["endpoint"] == "shapes"}
    output_positions = _centered_output_positions(graph, nodes, aligned, fee_ids)
    for edge in graph["edges"]:
        source, target = nodes[edge["source"]], nodes[edge["target"]]
        deltas = [(node["x"] - original[node["id"]][0], node["y"] - original[node["id"]][1])
                  for node in (source, target)]
        centered = edge["id"] in aligned
        if (not centered and edge["id"] not in input_positions and edge["id"] not in output_positions
                and deltas[0] == deltas[1]):
            if deltas[0] != (0, 0):
                edge["route"] = [{"x": point["x"] + deltas[0][0], "y": point["y"] + deltas[0][1]}
                                 for point in edge.get("route", [])]
                translate_label(edge, *deltas[0])
            continue
        attachment = copy.deepcopy(edge.get("attachment") or _default_attachments(source, target))
        if centered:
            attachment = {"startItem": {"position": {"x": "100%", "y": "50%"}},
                          "endItem": {"position": {"x": "0%", "y": "50%"}}}
        if edge["id"] in input_positions:
            attachment["endItem"] = {"position": {"x": "0%", "y": f'{input_positions[edge["id"]]:.6f}%'}}
        if edge["id"] in output_positions:
            attachment["startItem"] = {"position": {"x": "100%", "y": f'{output_positions[edge["id"]]:.6f}%'}}
        a, b = attachment_point(source, attachment["startItem"]), attachment_point(target, attachment["endItem"])
        if centered and input_positions.get(edge["id"], 50) == 50:
            route, reason = [a, b], None
        elif edge["target"] in fee_ids:
            lane = target["y"] + target["height"] / 2 + 70
            route = [a, {"x": a["x"] + 60, "y": a["y"]}, {"x": a["x"] + 60, "y": lane},
                     {"x": b["x"], "y": lane}, b]
            reason = "fee"
        elif b["x"] <= a["x"]:
            departure = a["x"] + (60 if a["x"] >= source["x"] else -60)
            arrival = b["x"] + (60 if b["x"] >= target["x"] else -60)
            lane = min(source["y"] - source["height"] / 2, target["y"] - target["height"] / 2) - 60
            route = [a, {"x": departure, "y": a["y"]}, {"x": departure, "y": lane},
                     {"x": arrival, "y": lane}, {"x": arrival, "y": b["y"]}, b]
            reason = "return"
        else:
            middle = (a["x"] + b["x"]) / 2
            route = [a, {"x": middle, "y": a["y"]}, {"x": middle, "y": b["y"]}, b]
            # The replacement route has not undergone ELK's obstacle routing.
            # Keep the established exception contract used by Miro/compaction.
            reason = "unchecked"
        edge.update(attachment=attachment, route=route, routing_exception=reason,
                    connector_shape=routed_shape(graph.get("graph_options", {}).get("connector_style", "straight"), reason))
        # ELK's horizontal label clearance remains, but this new route needs a
        # fresh midpoint caption position instead of its old ELK coordinates.
        edge.pop("label_layout", None)


def apply_change_layout(graph):
    """Apply row constraints to an owned ELK candidate in place; return it."""
    designations = graph.get("change_outputs")
    if not designations:
        return graph
    nodes = {node["id"]: node for node in graph["nodes"]}
    edges = {edge["id"]: edge for edge in graph["edges"]}
    fee_ids = {key for key, value in graph.get("fee_items", {}).items() if value["endpoint"] == "shapes"}
    main = {key: node for key, node in nodes.items() if key not in fee_ids}
    outpoints, spenders_by_output, outputs, components = defaultdict(set), defaultdict(list), defaultdict(list), _Groups(main)
    for edge in edges.values():
        outpoints[edge["source"]].add(edge.get("outpoint"))
        outpoints[edge["target"]].add(edge.get("outpoint"))
        if nodes[edge["target"]]["kind"] == "transaction":
            spenders_by_output[edge["source"], edge.get("outpoint")].append(edge)
        if edge["id"].startswith("out:"):
            outputs[edge["source"]].append(edge)
        if edge["source"] in main and edge["target"] in main:
            components.join(edge["source"], edge["target"])
    skipped = copy.deepcopy(designations.get("skipped", []))
    proposals = []
    for entry in designations.get("designations", []):
        edge = edges.get(entry["edge_id"])
        reason = None
        if not edge or edge["source"] not in main or edge["target"] not in main:
            reason = "Selected output is not visible in this graph."
        elif outpoints[edge["target"]] != {entry["outpoint"]}:
            reason = "The change address is shared by other outpoints; one address object cannot represent separate change rows."
        elif nodes[edge["target"]]["x"] - nodes[edge["target"]]["width"] / 2 < nodes[edge["source"]]["x"] + nodes[edge["source"]]["width"] / 2 + SPACING - 1e-6:
            reason = "The shared graph does not leave forward space for this change output."
        if reason:
            skipped.append({**entry, "reason": reason})
            continue
        row, aligned = [edge["source"], edge["target"]], [edge["id"]]
        spenders = spenders_by_output[edge["target"], entry["outpoint"]]
        if len(spenders) == 1:
            spender = nodes[spenders[0]["target"]]
            if spender["x"] - spender["width"] / 2 >= nodes[edge["target"]]["x"] + nodes[edge["target"]]["width"] / 2 + SPACING - 1e-6:
                row.append(spender["id"])
                aligned.append(spenders[0]["id"])
        below = sorted({item["target"] for item in outputs[edge["source"]]
                        if item["id"] != edge["id"] and item["target"] in main})
        proposals.append({"entry": entry, "source": edge["source"], "row": row, "below": below, "aligned": aligned})
    _, _, _, _, rejected = _constraints(main, proposals)
    rejected_ids = {proposal["entry"]["outpoint"] for proposal in rejected}
    skipped.extend({**proposal["entry"], "reason": "Change selections require conflicting rows in this connected group; automatic row alignment was skipped."}
                   for proposal in rejected)
    proposals = [proposal for proposal in proposals if proposal["entry"]["outpoint"] not in rejected_ids]
    if proposals:
        original = {key: (node["x"], node["y"]) for key, node in nodes.items()}
        rows = _pack_rows(main, proposals, components)
        _separate_components(main, components, original, rows)
        _routes(graph, nodes, original, {key for proposal in proposals for key in proposal["aligned"]})
        graph["layout"]["main_top"] = min(node["y"] - node["height"] / 2 for node in main.values())
        graph["layout"]["main_bottom"] = max(node["y"] + node["height"] / 2 for node in main.values())
        style = graph.get("graph_options", {}).get("connector_style", "straight")
        graph["layout"]["routing_exceptions"] = sum(edge.get("connector_shape", style) != style for edge in edges.values())
    graph["layout"]["change_outputs"] = {
        "algorithm": "explicit_change_rows_v1", "applied": [proposal["entry"] for proposal in proposals],
        "skipped": skipped, "locked_nodes": sorted({key for proposal in proposals for key in proposal["row"] + proposal["below"]}),
        "notice": NOTICE,
    }
    return graph
