"""Two-level Trace geometry: translated local routes and boundary corridors.

Temporary boundary anchors give the bounded local solver real escape routes.
They are removed before publication; every original node, port, edge and label
appears exactly once. Only edges crossing section envelopes reach the outer
gutter router. Local fanout never changes another section's internal distances.
"""
from bisect import bisect_right
from collections import defaultdict
from copy import deepcopy

from .common import TraceError, canonical, digest

_PADDING = 24.0
_ERROR = "Invalid local section geometry; regenerate the layout"


def refresh_section_bounds(graph):
    """Record occupied section bounds in the final graph coordinate frame.

    Assembly envelopes include temporary boundary escapes; postpasses may move
    individual objects. Saved bounds therefore describe current node boxes and
    internal visible routes/captions, not a promise of disjoint rigid envelopes.
    Cross-section routes have no single owner and are excluded. Work is linear
    in the displayed geometry, with no evidence copies or pair comparisons.
    """
    from .edge_labels import caption_box, caption_text
    from .elk_layout import _default_attachments, attachment_point

    geometry = graph.get("layout", {}).get("section_geometry", {})
    sections = geometry.get("sections", [])
    if not sections:
        return
    owner = {key: index for index, section in enumerate(sections) for key in section["node_ids"]}
    nodes = {node["id"]: node for node in graph["nodes"]}
    bounds = {}

    def include(index, box):
        old = bounds.get(index, box)
        bounds[index] = (min(old[0], box[0]), min(old[1], box[1]),
                         max(old[2], box[2]), max(old[3], box[3]))

    for key, index in owner.items():
        if key in nodes:
            node = nodes[key]
            include(index, (node["x"] - node["width"] / 2, node["y"] - node["height"] / 2,
                            node["x"] + node["width"] / 2, node["y"] + node["height"] / 2))
    # Restored graphs retain hidden canonical input routes. Measure the same
    # display summary geometry as the preview without constructing a deep copy.
    summaries = graph.get("context_connectors", {}).get("summaries", [])
    hidden = {key for edge in summaries for key in edge["details"]["context_summary"]["member_edge_ids"]}
    summary_lookup = {edge["id"]: edge for edge in summaries}
    seen = set()

    def displayed_edges():
        for edge in graph["edges"]:
            if edge["id"] in summary_lookup:
                seen.add(edge["id"])
            if edge["id"] not in hidden:
                yield edge
        yield from (edge for key, edge in summary_lookup.items() if key not in seen)

    for edge in displayed_edges():
        index = owner.get(edge["source"])
        if (index is None or owner.get(edge["target"]) != index
                or edge["source"] not in nodes or edge["target"] not in nodes):
            continue
        source, target = nodes[edge["source"]], nodes[edge["target"]]
        attachment = edge.get("attachment") or _default_attachments(source, target)
        first = attachment_point(source, attachment["startItem"])
        last = attachment_point(target, attachment["endItem"])
        route = edge.get("route") or []
        interior = route[1:-1] if edge.get("connector_shape", "straight") != "straight" else []
        points = [(point["x"], point["y"]) for point in (first, *interior, last)]
        for x, y in points:
            include(index, (x, y, x, y))
        if caption_text(edge):
            include(index, caption_box(edge, points))
    for index, section in enumerate(sections):
        if index in bounds:
            left, top, right, bottom = bounds[index]
            section.update(x=left, y=top, width=right - left, height=bottom - top)
    geometry["bounds_source"] = "final_geometry"


def section_id(keys):
    return "trace-section:" + digest(canonical(sorted(keys)))[:24]


def boundary_requests(request, groups, requests):
    """Add two bounded helper anchors at most, with no canonical mutations."""
    owner = {key: index for index, group in enumerate(groups) for key in group}
    local_pairs = defaultdict(list)
    for a, b in request.get("forwardNodePairs", ()):
        if owner[a] == owner[b]:
            local_pairs[owner[a]].append((a, b))
    ports = {port["id"]: (node["id"], port.get("layoutOptions", {}).get("elk.port.side", "EAST"))
             for node in request["children"] for port in node.get("ports", [])}
    external = defaultdict(list)
    for edge in request["edges"]:
        source, target = edge["sources"][0], edge["targets"][0]
        if owner[ports[source][0]] == owner[ports[target][0]]:
            continue
        for end, port in enumerate((source, target)):
            external[owner[ports[port][0]]].append((edge["id"], end, port, ports[port][1]))
    identities = {node["id"] for node in request["children"]} | set(ports)
    identities.update(edge["id"] for edge in request["edges"])
    result = []
    for index, (group, original) in enumerate(zip(groups, requests)):
        key = section_id(group)
        item = {**original, "children": list(original["children"]), "edges": list(original["edges"])}
        anchors, stubs = {}, {}
        columns = [float(node.get("layoutOptions", {}).get("elk.partitioning.partition", 0))
                   for node in original["children"]]
        for number, (edge, end, port, side) in enumerate(sorted(external[index])):
            # NORTH/SOUTH ports retain their original local side. Their escape
            # ends at a left/right anchor, so outer corridors need only two
            # sides; no graph identity or return direction is changed.
            boundary = "WEST" if side in ("WEST", "NORTH") else "EAST"
            anchor_id = key + ":boundary:" + boundary
            stub_id, port_id = key + ":stub:" + str(number), key + ":port:" + str(number)
            if any(value in identities for value in (anchor_id, stub_id, port_id)):
                raise TraceError(_ERROR)
            if boundary not in anchors:
                column = min(columns) - 1 if boundary == "WEST" else max(columns) + 1
                anchors[boundary] = {"id": anchor_id, "width": 1, "height": 1, "ports": [],
                    "layoutOptions": {"elk.partitioning.partition": str(column),
                        "elk.portConstraints": "FIXED_SIDE"}}
            anchors[boundary]["ports"].append({"id": port_id, "width": 0, "height": 0,
                "layoutOptions": {"elk.port.side": "EAST" if boundary == "WEST" else "WEST"}})
            item["edges"].append({"id": stub_id, "sources": [port if end == 0 else port_id],
                                  "targets": [port_id if end == 0 else port], "labels": []})
            stubs[stub_id] = {"edge_id": edge, "end": end, "port": port,
                              "anchor": anchor_id, "side": boundary}
        item["children"] = ([anchors["WEST"]] if "WEST" in anchors else []) + item["children"] + (
            [anchors["EAST"]] if "EAST" in anchors else [])
        item["centerNodeOrder"] = [node["id"] for node in item["children"]]
        # Section-local ranks and cycle-breaking groups must describe the same
        # order, including temporary boundary helpers.
        values = sorted({float(node.get("layoutOptions", {}).get("elk.partitioning.partition", 0))
                         for node in item["children"]})
        ranks = {value: rank for rank, value in enumerate(values)}
        item["children"] = [{**node, "layoutOptions": {**node.get("layoutOptions", {}),
            "elk.layered.considerModelOrder.groupModelOrder.cycleBreakingId": str(ranks[
                float(node.get("layoutOptions", {}).get("elk.partitioning.partition", 0))])}}
            for node in item["children"]]
        item["localSection"] = {"id": key, "node_ids": list(group),
            "internal_edge_ids": [edge["id"] for edge in original["edges"]],
            "boundary_edges": stubs, "anchor_ids": [node["id"] for node in anchors.values()]}
        item["forwardNodePairs"] = local_pairs[index]
        result.append(item)
    return result


def _points(edge):
    section, = edge["sections"]
    return [section["startPoint"], *section.get("bendPoints", []), section["endPoint"]]


def _overlap(a, b):
    return (max(a["x"], b["x"]) < min(a["x"] + a["width"], b["x"] + b["width"])
            and max(a["y"], b["y"]) < min(a["y"] + a["height"], b["y"] + b["height"]))


def _hits(a, b, node):
    x, y, w, h = (node[key] for key in ("x", "y", "width", "height"))
    if a["x"] == b["x"]:
        return x < a["x"] < x + w and min(a["y"], b["y"]) < y + h and max(a["y"], b["y"]) > y
    return y < a["y"] < y + h and min(a["x"], b["x"]) < x + w and max(a["x"], b["x"]) > x


def _usable_local(request, candidate):
    """Reject unsafe worker geometry before preserving it, within worker caps.

    Checking a bounded section is independent of the complete graph size.
    Existing global layout checks still evaluate the assembled candidate.
    """
    if candidate is None:
        return False
    try:
        nodes = candidate["nodes"]
        if any(_overlap(a, b) for i, a in enumerate(nodes) for b in nodes[i + 1:]):
            return False
        real = [node for node in nodes if node["id"] in request["localSection"]["node_ids"]]
        lookup = {node["id"]: node for node in nodes}
        endpoints = {port["id"]: {"x": node["x"] + port["x"], "y": node["y"] + port["y"]}
                     for node in nodes for port in node.get("ports", [])}
        originals = {edge["id"]: edge for edge in request["edges"]}
        port_owner = {port["id"]: node["id"] for node in request["children"] for port in node.get("ports", [])}
        columns = {node["id"]: float(node.get("layoutOptions", {}).get("elk.partitioning.partition", 0))
                   for node in request["children"]}
        for edge in request["edges"]:
            a, b = port_owner[edge["sources"][0]], port_owner[edge["targets"][0]]
            delta = columns[b] - columns[a]
            placed = (lookup[b]["x"] + lookup[b]["width"] / 2
                      - lookup[a]["x"] - lookup[a]["width"] / 2)
            if delta and delta * placed <= 0:
                return False
        for a, b in request.get("forwardNodePairs", ()):
            if lookup[a]["x"] + lookup[a]["width"] / 2 >= lookup[b]["x"] + lookup[b]["width"] / 2:
                return False
        for proof in request["localSection"]["boundary_edges"].values():
            anchor = lookup[proof["anchor"]]
            if ((proof["side"] == "WEST" and anchor["x"] + anchor["width"] > min(n["x"] for n in real))
                    or (proof["side"] == "EAST" and anchor["x"] < max(n["x"] + n["width"] for n in real))):
                return False
        for edge in candidate["edges"]:
            points = _points(edge)
            original = originals[edge["id"]]
            for point, field in ((points[0], "sources"), (points[-1], "targets")):
                port = endpoints[original[field][0]]
                if any(abs(point[axis] - port[axis]) > 1e-7 for axis in ("x", "y")):
                    return False
            for a, b in zip(points, points[1:]):
                if a["x"] != b["x"] and a["y"] != b["y"]:
                    return False
                if any(_hits(a, b, node) for node in real):
                    return False
            if any(_overlap(label, node) for label in edge.get("labels", []) for node in real):
                return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def local_candidates(request, requests, candidates, backbone, seed, shapes, backbone_edges):
    """Preserve usable worker geometry, with bounded intentional local fallbacks."""
    from .trace_section_geometry import assemble_gutters
    result = []
    for item, candidate in zip(requests, candidates):
        keys = set(item["localSection"]["node_ids"])
        neighbors = {key: pair for key, pair in request.get("addressNeighbors", {}).items()
                     if key in keys and set(pair) <= keys}
        fallback = not _usable_local(item, candidate)
        if not fallback and neighbors:
            centers = {node["id"]: node["y"] + node["height"] / 2 for node in candidate["nodes"]}
            # Keep the existing exact-continuation proximity repair before
            # preserving routes, and only within its own bounded section.
            fallback = any(not min(centers[other] for other in pair) <= centers[key]
                           <= max(centers[other] for other in pair) for key, pair in neighbors.items())
        if fallback:
            core = set(backbone) & set(item["localSection"]["node_ids"])
            direct = set(backbone_edges) & set(item["localSection"]["internal_edge_ids"])
            for key, proof in item["localSection"]["boundary_edges"].items():
                if proof["edge_id"] in backbone_edges:
                    core.add(proof["anchor"])
                    direct.add(key)
            groups = [[node["id"]] for node in item["children"]]
            hints = [{"seed": seed, "nodes": [{"id": node["id"], "y": 0, "height": node["height"]}]}
                     for node in item["children"]]
            if candidate is not None:
                groups, hints = [[node["id"] for node in item["children"]]], [candidate]
            candidate = assemble_gutters({**item, "backboneEdges": direct, "nodeShapes": shapes,
                                         "addressNeighbors": neighbors},
                                         groups, hints, core)
        result.append({**candidate, "localFallback": fallback})
    return result


def _translated_point(point, x, y):
    return {**point, "x": point["x"] + x, "y": point["y"] + y}


def _translated_edge(edge, x, y):
    points = [_translated_point(point, x, y) for point in _points(edge)]
    return {**edge, "sections": [{"startPoint": points[0], "bendPoints": points[1:-1], "endPoint": points[-1]}],
            "labels": [_translated_point(label, x, y) for label in edge.get("labels", [])]}


def _envelope(item, candidate, backbone):
    meta = item["localSection"]
    node_ids, internal = set(meta["node_ids"]), set(meta["internal_edge_ids"])
    nodes = [deepcopy(node) for node in candidate["nodes"] if node["id"] in node_ids]
    edges = {edge["id"]: edge for edge in candidate["edges"]}
    if len(nodes) != len(node_ids) or set(edges) != internal | meta["boundary_edges"].keys():
        raise TraceError(_ERROR)
    boxes = nodes + [label for key in internal for label in edges[key].get("labels", [])]
    points = [point for edge in edges.values() for point in _points(edge)]
    left = min([box["x"] for box in boxes] + [p["x"] for p in points]) - _PADDING
    top = min([box["y"] for box in boxes] + [p["y"] for p in points]) - _PADDING
    right = max([box["x"] + box["width"] for box in boxes] + [p["x"] for p in points]) + _PADDING
    bottom = max([box["y"] + box["height"] for box in boxes] + [p["y"] for p in points]) + _PADDING
    core = [node["y"] + node["height"] / 2 for node in nodes if node["id"] in backbone]
    if core:
        middle = sorted(core)[len(core) // 2]
        extent = max(middle - top, bottom - middle)
        top, bottom = middle - extent, middle + extent
    escapes = {}
    for key, proof in meta["boundary_edges"].items():
        path = _points(edges[key])
        if proof["end"]:
            path = list(reversed(path))
        end = {"x": left if proof["side"] == "WEST" else right, "y": path[-1]["y"]}
        escapes[proof["port"]] = {"side": proof["side"],
            "points": [_translated_point(point, -left, -top) for point in [*path, end]]}
    return {"id": meta["id"], "node_ids": list(meta["node_ids"]), "width": right - left, "height": bottom - top,
        "nodes": [{**node, "x": node["x"] - left, "y": node["y"] - top} for node in nodes],
        "edges": [_translated_edge(edges[key], -left, -top) for key in meta["internal_edge_ids"]],
        "escapes": escapes, "fallback": candidate.get("localFallback", False), "backbone": bool(core),
        "address_placement": candidate.get("sectionGeometry", {}).get("address_placement", {})}


def assemble_local(request, groups, candidates, backbone_ids):
    """Pack section envelopes and join canonical edges through outer corridors."""
    from .trace_section_geometry import assemble_gutters, _simplify
    from .trace_sections import SECTION_LAYOUT_VERSION
    children = {node["id"]: node for node in request["children"]}
    keys = [key for group in groups for key in group]
    requests = request.get("sectionRequests", [])
    if (len(keys) != len(set(keys)) or set(keys) != set(children)
            or len(groups) != len(candidates) or len(groups) != len(requests)):
        raise TraceError(_ERROR)
    if not children:
        return {"seed": 1, "nodes": [], "edges": [], "inputOrderPolicy": "geometry"}
    backbone = set(backbone_ids)
    envelopes = [_envelope(item, candidate, backbone) for item, candidate in zip(requests, candidates)]
    owner = {key: envelope["id"] for envelope in envelopes for key in envelope["node_ids"]}
    values = {envelope["id"]: envelope for envelope in envelopes}
    ports = {port["id"]: node["id"] for node in children.values() for port in node.get("ports", [])}
    columns = {key: float(node.get("layoutOptions", {}).get("elk.partitioning.partition", 0))
               for key, node in children.items()}
    starts = {value["id"]: min(columns[key] for key in value["node_ids"]) for value in envelopes}
    spine_starts = sorted({starts[value["id"]] for value in envelopes if value["backbone"]})
    # Sections overlapping the same backbone interval share one macro column;
    # summing their entire widths as independent dependency columns would
    # recreate the original spatial explosion.
    anchors = (sorted(set(starts.values())) if request.get("outputAlignmentPositions")
               else spine_starts or sorted(set(starts.values())))
    buckets = {key: max(0, bisect_right(anchors, start) - 1) for key, start in starts.items()}
    by_bucket = defaultdict(list)
    for value in envelopes:
        by_bucket[buckets[value["id"]]].append(value)
    offsets, spans = {}, {}
    for bucket, members in by_bucket.items():
        reference = min(members, key=lambda value: (not value["backbone"],
                        -len(value["node_ids"]), value["id"]))
        rank_x = defaultdict(list)
        for node in reference["nodes"]:
            rank_x[columns[node["id"]]].append(node["x"] + node["width"] / 2)
        rank_x = {rank: sum(xs) / len(xs) for rank, xs in rank_x.items()}
        ranks = sorted(rank_x)
        step = max(300.0, (rank_x[ranks[-1]] - rank_x[ranks[0]]) / max(1, ranks[-1] - ranks[0]))
        def location(rank):
            split = bisect_right(ranks, rank)
            if not split:
                return rank_x[ranks[0]] + (rank - ranks[0]) * step
            if split == len(ranks):
                return rank_x[ranks[-1]] + (rank - ranks[-1]) * step
            low, high = ranks[split - 1], ranks[split]
            return rank_x[low] + (rank - low) * (rank_x[high] - rank_x[low]) / (high - low)
        for value in members:
            first = starts[value["id"]]
            centers = [node["x"] + node["width"] / 2 for node in value["nodes"] if columns[node["id"]] == first]
            offsets[value["id"]] = location(first) - sum(centers) / len(centers)
        left = min(offsets[value["id"]] for value in members)
        spans[bucket] = max(offsets[value["id"]] + value["width"] for value in members) - left
        for value in members:
            offsets[value["id"]] -= left
    macro_nodes = [{"id": value["id"], "width": spans[buckets[value["id"]]], "height": value["height"],
        "ports": [{"id": key, "width": 0, "height": 0, "layoutOptions": {"elk.port.side": escape["side"]}}
                  for key, escape in sorted(value["escapes"].items())],
        "layoutOptions": {"elk.partitioning.partition": str(buckets[value["id"]])}}
        for value in envelopes]
    cross = [edge for edge in request["edges"] if owner[ports[edge["sources"][0]]] != owner[ports[edge["targets"][0]]]]
    macro_request = {"children": macro_nodes, "edges": cross,
        "centerNodeOrder": [value["id"] for value in envelopes],
        "backboneEdges": set(request.get("backboneEdges", ())) & {edge["id"] for edge in cross}}
    hints = [{"seed": candidates[0].get("seed", 1),
              "nodes": [{"id": node["id"], "y": 0, "height": node["height"]}]} for node in macro_nodes]
    macro = assemble_gutters(macro_request, [[node["id"]] for node in macro_nodes], hints,
                             {value["id"] for value in envelopes if value["backbone"]})
    positions = {node["id"]: node for node in macro["nodes"]}
    nodes, routed, escapes, sections = [], {}, {}, []
    for value in envelopes:
        box = positions[value["id"]]
        x, y = box["x"] + offsets[value["id"]], box["y"]
        nodes.extend({**node, "x": node["x"] + x, "y": node["y"] + y} for node in value["nodes"])
        routed.update((edge["id"], _translated_edge(edge, x, y)) for edge in value["edges"])
        for key, escape in value["escapes"].items():
            path = [_translated_point(point, x, y) for point in escape["points"]]
            path.append({"x": box["x"] if escape["side"] == "WEST" else box["x"] + box["width"],
                         "y": path[-1]["y"]})
            escapes[key] = _simplify(path)
        sections.append({"id": value["id"], "node_ids": value["node_ids"],
            "x": x, "y": y, "width": value["width"], "height": value["height"],
            "internal_edge_count": len(value["edges"]), "boundary_port_count": len(value["escapes"]),
            "fallback": value["fallback"]})
    cross_lookup = {edge["id"]: edge for edge in cross}
    for edge in macro["edges"]:
        original = cross_lookup[edge["id"]]
        before, after = escapes[original["sources"][0]], escapes[original["targets"][0]]
        middle = deepcopy(_points(edge))
        # The macro router owns outer corridors, but local escape endpoints
        # remain fixed. Move only the outermost horizontal stubs; all internal
        # nodes, ports, routes and captions remain translations of local data.
        if len(middle) == 2:
            a, b = before[-1], after[-1]
            mid_x = (a["x"] + b["x"]) / 2
            middle = [a, {"x": mid_x, "y": a["y"]}, {"x": mid_x, "y": b["y"]}, b]
        else:
            middle[0], middle[-1] = before[-1], after[-1]
            middle[1]["y"], middle[-2]["y"] = before[-1]["y"], after[-1]["y"]
        path = _simplify([*before, *middle[1:-1], *reversed(after)])
        routed[edge["id"]] = {**edge, "sections": [{"startPoint": path[0], "bendPoints": path[1:-1],
                                                    "endPoint": path[-1]}]}
    if set(routed) != {edge["id"] for edge in request["edges"]}:
        raise TraceError(_ERROR)
    # Translation-only local shapes sometimes have incompatible spans around
    # a rejoin or return. Refine those sections rather than stretch preserved
    # routes or reverse either direction. At most three assemblies are used;
    # a final singleton partition is a conservative, worker-free fallback.
    positioned = {node["id"]: node for node in nodes}
    conflicts = set()
    for edge in request["edges"]:
        a, b = (ports[edge[field][0]] for field in ("sources", "targets"))
        delta = columns[b] - columns[a]
        placed = (positioned[b]["x"] + positioned[b]["width"] / 2
                  - positioned[a]["x"] - positioned[a]["width"] / 2)
        if delta and delta * placed <= 0:
            conflicts.update((owner[a], owner[b]))
    for a, b in request.get("forwardNodePairs", ()):
        if (positioned[a]["x"] + positioned[a]["width"] / 2
                >= positioned[b]["x"] + positioned[b]["width"] / 2):
            conflicts.update((owner[a], owner[b]))
    if conflicts:
        round_number = request.get("localRefinementRound", 0)
        if round_number >= 2:
            raise TraceError("Local section refinement could not preserve connection directions")
        refined = []
        for value in envelopes:
            refined.extend([[key] for key in value["node_ids"]] if round_number == 1 or value["id"] in conflicts
                           else [value["node_ids"]])
        lookup = {node["id"]: node for node in request["children"]}
        owners = {key: index for index, group in enumerate(refined) for key in group}
        internal = defaultdict(list)
        for edge in request["edges"]:
            a, b = (owners[ports[edge[field][0]]] for field in ("sources", "targets"))
            if a == b:
                internal[a].append(edge)
        base = [{"children": [lookup[key] for key in group], "edges": internal[index],
                 "layoutOptions": dict(request.get("layoutOptions", {})), "sectionLayout": True}
                for index, group in enumerate(refined)]
        bounded = boundary_requests(request, refined, base)
        existing = {item["localSection"]["id"]: candidate for item, candidate in zip(requests, candidates)}
        fresh = local_candidates(request, bounded, [existing.get(item["localSection"]["id"]) for item in bounded],
                                 backbone, candidates[0].get("seed", 1), request.get("nodeShapes", {}),
                                 set(request.get("backboneEdges", ())))
        return assemble_local({**request, "sectionRequests": bounded, "localRefinementRound": round_number + 1},
                              refined, fresh, backbone)
    # Translation arithmetic can differ by a final floating-point bit between
    # node+port and route+offset. Bind endpoints to the canonical final ports;
    # retain the same orthogonal turn directions beside them.
    endpoints = {port["id"]: {"x": node["x"] + port["x"], "y": node["y"] + port["y"]}
                 for node in nodes for port in node.get("ports", [])}
    for original in request["edges"]:
        edge = routed[original["id"]]
        path = deepcopy(_points(edge))
        start, end = endpoints[original["sources"][0]], endpoints[original["targets"][0]]
        if len(path) > 2:
            first_axis = "x" if path[0]["x"] == path[1]["x"] else "y"
            last_axis = "x" if path[-1]["x"] == path[-2]["x"] else "y"
            path[1][first_axis], path[-2][last_axis] = start[first_axis], end[last_axis]
        path[0], path[-1] = start, end
        if len(path) == 2 and start["x"] != end["x"] and start["y"] != end["y"]:
            path.insert(1, {"x": end["x"], "y": start["y"]})
        edge["sections"] = [{"startPoint": path[0], "bendPoints": path[1:-1], "endPoint": path[-1]}]
    address_placement = dict(macro["sectionGeometry"]["address_placement"])
    for value in envelopes:
        for key, count in value["address_placement"].items():
            if key in {"version", "probe_limit", "max_address_probes"}:
                address_placement[key] = max(address_placement.get(key, 0), count)
            else:
                address_placement[key] = address_placement.get(key, 0) + count
    return {"seed": candidates[0].get("seed", 1), "branchProfile": "flow_weighted", "branchBoundary": False,
        "inputOrderPolicy": "geometry", "nodes": nodes,
        "edges": [routed[edge["id"]] for edge in request["edges"]],
        "width": macro["width"], "height": macro["height"],
        "sectionGeometry": {**macro["sectionGeometry"], "version": SECTION_LAYOUT_VERSION,
            "packing": "local_envelopes_neighbor_anchored", "routing": "local_routes_with_boundary_corridors",
            "sections": sections, "preserved_internal_edges": len(routed) - len(cross),
            "address_placement": address_placement,
            "cross_section_edges": len(cross), "local_fallback_sections": sum(value["fallback"] for value in envelopes),
            "refinement_rounds": request.get("localRefinementRound", 0), "refinement_round_limit": 2}}
