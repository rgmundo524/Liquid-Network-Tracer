"""Bounded local ELK placement; presentation only, never tracing or attribution.

The worker receives opaque IDs, dimensions, dependency partitions and edges. ELK
chooses layer ordering, port ordering and orthogonal routes. Miro cannot accept
arbitrary ELK bendpoints, so its collision counts remain estimates; saved routes
are used by the local preview. No cloud layout service is involved.
"""

import copy
import json
import math
import os
import shutil
import signal
import subprocess
from pathlib import Path

from .common import TraceError


ALGORITHM = "elk_layered_v1"
ELK_VERSION = "0.12.0"
TIMEOUT_SECONDS = 30
MAX_NODES = 10000
MAX_EDGES = 30000
MAX_COMPARISONS = 250000
STYLES = {"straight", "elbowed", "curved"}
_EPS = 1e-7


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _point(value):
    if not isinstance(value, dict) or not _finite(value.get("x")) or not _finite(value.get("y")):
        raise TraceError("ELK returned an invalid coordinate; no Miro changes were made")
    if abs(value["x"]) > 1e8 or abs(value["y"]) > 1e8:
        raise TraceError("ELK layout exceeds the supported coordinate range")
    return {"x": round(value["x"], 4), "y": round(value["y"], 4)}


def _percent(value):
    return str(round(max(0, min(100, value)), 6)).rstrip("0").rstrip(".") + "%" if value % 1 else str(int(value)) + "%"


def _port(node, x, y):
    """Project an ELK rectangle port to the actual circle/diamond perimeter."""
    width, height = node["width"], node["height"]
    dx, dy = (x - width / 2) / (width / 2), (y - height / 2) / (height / 2)
    if node["kind"] == "address":
        scale = math.hypot(dx, dy) or 1
        dx, dy = dx / scale, dy / scale
    elif node["kind"] == "event":
        scale = abs(dx) + abs(dy) or 1
        dx, dy = dx / scale, dy / scale
    return {"position": {"x": _percent((dx + 1) * 50), "y": _percent((dy + 1) * 50)}}


def attachment_point(node, attachment):
    """Absolute position of a Miro percentage attachment on a centered node."""
    position = attachment["position"]
    return {"x": node["x"] + (float(position["x"].rstrip("%")) / 100 - .5) * node["width"],
            "y": node["y"] + (float(position["y"].rstrip("%")) / 100 - .5) * node["height"]}


def _default_attachments(source, target):
    def side(node, other, outgoing):
        right = outgoing if node["kind"] == "transaction" else other["x"] > node["x"]
        return {"position": {"x": "100%" if right else "0%", "y": "50%"}}
    return {"startItem": side(source, target, True), "endItem": side(target, source, False)}


def _cross(a, b, c):
    return (b["x"] - a["x"]) * (c["y"] - a["y"]) - (b["y"] - a["y"]) * (c["x"] - a["x"])


def segments_cross(a, b, c, d):
    """Proper interior crossing. Shared ports and collinear overlap are separate."""
    return _cross(a, b, c) * _cross(a, b, d) < -_EPS and _cross(c, d, a) * _cross(c, d, b) < -_EPS


def _intersection(a, b, c, d):
    """Unique segment intersection, including bends; exclude collinear overlap."""
    rx, ry = b["x"] - a["x"], b["y"] - a["y"]
    sx, sy = d["x"] - c["x"], d["y"] - c["y"]
    denominator = rx * sy - ry * sx
    if abs(denominator) < _EPS:
        return None
    qx, qy = c["x"] - a["x"], c["y"] - a["y"]
    t, u = (qx * sy - qy * sx) / denominator, (qx * ry - qy * rx) / denominator
    if -_EPS <= t <= 1 + _EPS and -_EPS <= u <= 1 + _EPS:
        return {"x": a["x"] + t * rx, "y": a["y"] + t * ry}
    return None


def _inside(point, node, margin=0):
    dx = abs(point["x"] - node["x"]) / (node["width"] / 2 + margin)
    dy = abs(point["y"] - node["y"]) / (node["height"] / 2 + margin)
    if node["kind"] == "address":
        return dx * dx + dy * dy < 1 - _EPS
    if node["kind"] == "event":
        return dx + dy < 1 - _EPS
    return dx < 1 - _EPS and dy < 1 - _EPS


def segment_hits_node(a, b, node):
    """Whether any interior portion of a segment enters the displayed shape."""
    if _inside(a, node) or _inside(b, node):
        return True
    # Scale to a unit circle / diamond / rectangle and minimize distance to the
    # shape. The ellipse closest point is exact after this affine transform.
    ax, ay = (a["x"] - node["x"]) / (node["width"] / 2), (a["y"] - node["y"]) / (node["height"] / 2)
    bx, by = (b["x"] - node["x"]) / (node["width"] / 2), (b["y"] - node["y"]) / (node["height"] / 2)
    dx, dy = bx - ax, by - ay
    if node["kind"] == "address":
        t = max(0, min(1, -(ax * dx + ay * dy) / (dx * dx + dy * dy or 1)))
        return (ax + t * dx) ** 2 + (ay + t * dy) ** 2 < 1 - _EPS
    if node["kind"] == "event":
        candidates = [0, 1]
        if dx:
            candidates.append(max(0, min(1, -ax / dx)))
        if dy:
            candidates.append(max(0, min(1, -ay / dy)))
        return min(abs(ax + t * dx) + abs(ay + t * dy) for t in candidates) < 1 - _EPS
    lower, upper = 0.0, 1.0
    for start, delta in ((ax, dx), (ay, dy)):
        if not delta:
            if abs(start) >= 1 - _EPS:
                return False
        else:
            limits = sorted(((-1 + _EPS - start) / delta, (1 - _EPS - start) / delta))
            lower, upper = max(lower, limits[0]), min(upper, limits[1])
    return lower < upper - _EPS


def _box_segment(a, b):
    return (min(a["x"], b["x"]), min(a["y"], b["y"]), max(a["x"], b["x"]), max(a["y"], b["y"]))


def _box_node(node):
    return (node["x"] - node["width"] / 2, node["y"] - node["height"] / 2,
            node["x"] + node["width"] / 2, node["y"] + node["height"] / 2)


def _boxes_touch(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _pairs(boxes):
    """Sweep broad phase; each caller also bounds the exact comparisons."""
    active = []
    for index in sorted(range(len(boxes)), key=lambda i: (boxes[i][0], i)):
        box = boxes[index]
        active = [other for other in active if boxes[other][2] >= box[0]]
        for other in active:
            # Count broad-phase candidates too. A dense graph with disjoint
            # y-ranges must not spend unbounded time on rejected comparisons.
            yield other, index
        active.append(index)


def layout_metrics(graph, *, routed=True, max_comparisons=MAX_COMPARISONS):
    """Bounded geometric estimates. Label bounds and Miro auto-routing excluded.

    Truncated counts are explicitly lower bounds. Rectangular object-overlap
    bounds are deliberately conservative for circles and diamonds.
    """
    nodes = {node["id"]: node for node in graph["nodes"]}
    segments, edge_length, endpoints = [], 0.0, {}
    for edge in graph["edges"]:
        attachment = edge.get("attachment") or _default_attachments(nodes[edge["source"]], nodes[edge["target"]])
        points = [attachment_point(nodes[edge["source"]], attachment["startItem"]),
                  attachment_point(nodes[edge["target"]], attachment["endItem"])]
        endpoints[edge["id"]] = {edge["source"]: points[0], edge["target"]: points[-1]}
        if routed and edge.get("connector_shape") in ("elbowed", "curved") and edge.get("route"):
            points = [points[0], *edge["route"][1:-1], points[-1]]
        for a, b in zip(points, points[1:]):
            edge_length += math.hypot(a["x"] - b["x"], a["y"] - b["y"])
            segments.append((edge, a, b))
    boxes = [_box_segment(a, b) for _, a, b in segments]
    node_list = list(nodes.values())
    node_boxes = [_box_node(node) for node in node_list]
    all_boxes = boxes + node_boxes
    crossings, intersections, overlaps = set(), set(), 0
    comparisons, truncated = 0, False
    for i, j in _pairs(all_boxes):
        comparisons += 1
        if comparisons > max_comparisons:
            truncated = True
            break
        if not _boxes_touch(all_boxes[i], all_boxes[j]):
            continue
        if i >= len(segments) and j >= len(segments):
            a, b = node_boxes[i - len(segments)], node_boxes[j - len(segments)]
            overlaps += int(min(a[2], b[2]) - max(a[0], b[0]) > _EPS and min(a[3], b[3]) - max(a[1], b[1]) > _EPS)
        elif i < len(segments) and j < len(segments):
            first, a, b = segments[i]
            second, c, d = segments[j]
            if first["id"] != second["id"]:
                intersection = _intersection(a, b, c, d)
                shared = endpoints[first["id"]].keys() & endpoints[second["id"]].keys()
                meets_at_shared_port = intersection and any(
                    all(math.hypot(endpoints[edge["id"]][key]["x"] - intersection["x"],
                                   endpoints[edge["id"]][key]["y"] - intersection["y"]) < 1e-5
                        for edge in (first, second)) for key in shared)
                if intersection and not meets_at_shared_port:
                    crossings.add(tuple(sorted((first["id"], second["id"]))))
        else:
            si, ni = (i, j) if i < len(segments) else (j, i)
            edge, a, b = segments[si]
            node = node_list[ni - len(segments)]
            if node["id"] not in (edge["source"], edge["target"]) and segment_hits_node(a, b, node):
                intersections.add((edge["id"], node["id"]))
    return {"crossings": len(crossings), "node_overlaps": overlaps, "node_intersections": len(intersections),
            "edge_length": round(edge_length, 2), "comparisons": min(comparisons, max_comparisons),
            "truncated": truncated, "estimated": True,
            "method": "planned_segments" if routed else "straight_segments",
            "labels_measured": False, "curves_approximated": True}


def _worker(graph, seeds):
    project = Path(os.environ.get("LIQUID_TRACER_ROOT", Path(__file__).resolve().parents[1]))
    runner = project / "layout" / "run.mjs"
    node = os.environ.get("LIQUID_NODE_BIN") or shutil.which("node")
    if node and os.environ.get("LIQUID_NODE_BIN") and not Path(node).is_absolute():
        raise TraceError("LIQUID_NODE_BIN must be an absolute path to the pinned Node executable")
    if not node or not runner.is_file() or not (runner.parent / "node_modules" / "elkjs" / "package.json").is_file():
        raise TraceError("Local ELK dependencies are unavailable. Enter the project devenv shell and run liquid-layout-setup")
    # Explicit allowlist strips API credentials, NODE_OPTIONS, preload hooks,
    # proxy variables, and secrets-provider state from this pure calculation.
    environment = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT") if name in os.environ}
    process = None
    try:
        process = subprocess.Popen([node, str(runner)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env=environment, start_new_session=True)
        output, _ = process.communicate(json.dumps({"graph": graph, "seeds": seeds}), timeout=TIMEOUT_SECONDS)
        if process.returncode:
            raise TraceError("Local ELK layout failed; no Miro changes were made. Check the layout dependency installation")
        if len(output) > 64 * 1024 * 1024:
            raise TraceError("ELK returned an oversized layout")
        result = json.loads(output)
        if not isinstance(result, dict) or result.get("version") != ELK_VERSION or not isinstance(result.get("candidates"), list):
            raise ValueError("invalid worker response")
        if len(result["candidates"]) != len(seeds):
            raise ValueError("missing candidates")
        return result["candidates"]
    except subprocess.TimeoutExpired as exc:
        raise TraceError("Local ELK layout exceeded its 30-second limit; use a smaller bounded run") from exc
    except (OSError, ValueError, TypeError) as exc:
        raise TraceError("Local ELK returned an invalid layout; no Miro changes were made") from exc
    finally:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()


def _request_graph(graph):
    nodes = {node["id"]: node for node in graph["nodes"]}
    fee_ids = {key for key, item in graph.get("fee_items", {}).items() if item["endpoint"] == "shapes"}
    main = {key: node for key, node in nodes.items() if key not in fee_ids}
    children, port_map = {}, {}
    for key, node in sorted(main.items()):
        children[key] = {"id": key, "width": node["width"], "height": node["height"], "ports": [],
                         "layoutOptions": {"elk.partitioning.partition": str(node["column"]),
                                           "elk.portConstraints": "FIXED_SIDE"}}
    edge_values = []
    main_edges = sorted((edge for edge in graph["edges"] if edge["source"] in main and edge["target"] in main),
                        key=lambda edge: edge["id"])
    for index, edge in enumerate(main_edges):
        ports = []
        for outgoing, key, other in ((True, edge["source"], edge["target"]), (False, edge["target"], edge["source"])):
            node = main[key]
            if node["kind"] == "transaction":
                east = outgoing
            else:
                east = main[other]["column"] > node["column"]
                if main[other]["column"] == node["column"]:
                    east = not outgoing
            port_id = "p" + str(index) + ("s" if outgoing else "t")
            children[key]["ports"].append({"id": port_id, "width": 0, "height": 0,
                                           "layoutOptions": {"elk.port.side": "EAST" if east else "WEST"}})
            ports.append(port_id)
        port_map[edge["id"]] = ports
        edge_values.append({"id": edge["id"], "sources": [ports[0]], "targets": [ports[1]]})
    return {"id": "liquid-layout", "layoutOptions": {
        "elk.algorithm": "layered", "elk.direction": "RIGHT", "elk.edgeRouting": "ORTHOGONAL",
        "elk.partitioning.activate": "true", "elk.spacing.nodeNode": "80", "elk.spacing.componentComponent": "120",
        "elk.layered.spacing.nodeNodeBetweenLayers": "200", "elk.spacing.edgeNode": "35",
        "elk.layered.spacing.edgeNodeBetweenLayers": "60", "elk.spacing.edgeEdge": "22",
        "elk.layered.spacing.edgeEdgeBetweenLayers": "22", "elk.layered.thoroughness": "8",
        "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
        "elk.layered.crossingMinimization.greedySwitch.type": "TWO_SIDED",
        "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF",
        "elk.layered.nodePlacement.favorStraightEdges": "true",
        "elk.padding": "[top=0,left=0,bottom=0,right=0]"},
        "children": list(children.values()), "edges": edge_values}, port_map, fee_ids


def _apply_candidate(graph, candidate, port_map, fee_ids, connector_style):
    result = copy.deepcopy(graph)
    nodes = {node["id"]: node for node in result["nodes"]}
    raw_nodes = candidate.get("nodes")
    raw_edges = candidate.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise TraceError("ELK returned an invalid layout")
    main_ids = set(nodes) - fee_ids
    if len(raw_nodes) != len(main_ids) or {node.get("id") for node in raw_nodes} != main_ids:
        raise TraceError("ELK changed the graph's objects; no Miro changes were made")
    if len(raw_edges) != len(port_map) or {edge.get("id") for edge in raw_edges} != set(port_map):
        raise TraceError("ELK changed the graph's connections; no Miro changes were made")
    ports = {}
    offset_x = 130 - min((node["x"] + node["width"] / 2 for node in raw_nodes), default=130)
    offset_y = 160 - min((node["y"] for node in raw_nodes), default=160)
    for raw in raw_nodes:
        point = _point(raw)
        node = nodes[raw["id"]]
        if raw.get("width") != node["width"] or raw.get("height") != node["height"]:
            raise TraceError("ELK changed object dimensions; no Miro changes were made")
        node.update(x=round(point["x"] + node["width"] / 2 + offset_x, 4),
                    y=round(point["y"] + node["height"] / 2 + offset_y, 4))
        for port in raw.get("ports", []):
            ports[port["id"]] = _port(node, **_point(port))
    route_map = {}
    for raw in raw_edges:
        sections = raw.get("sections")
        if not isinstance(sections, list) or len(sections) != 1:
            raise TraceError("ELK returned an unsupported connector route")
        section = sections[0]
        points = [section.get("startPoint"), *section.get("bendPoints", []), section.get("endPoint")]
        if len(points) > 1000:
            raise TraceError("ELK returned an oversized connector route")
        route_map[raw["id"]] = [{"x": point["x"] + offset_x, "y": point["y"] + offset_y}
                                for point in map(_point, points)]
    # Existing fee row x positions already encode confirmed height/time order.
    fees = sorted((nodes[key] for key in fee_ids & nodes.keys()), key=lambda node: (node["x"], node["id"]))
    for index, node in enumerate(fees):
        node.update(x=130 + index * 230, y=-100)
    for edge in result["edges"]:
        source, target = nodes[edge["source"]], nodes[edge["target"]]
        if edge["id"] in port_map:
            try:
                attachment = {"startItem": ports[port_map[edge["id"]][0]], "endItem": ports[port_map[edge["id"]][1]]}
            except KeyError as exc:
                raise TraceError("ELK omitted a connector attachment") from exc
            route = route_map[edge["id"]]
        else:
            attachment = _default_attachments(source, target)
            attachment["endItem"] = {"position": {"x": "50%", "y": "100%"}}
            start, end = attachment_point(source, attachment["startItem"]), attachment_point(target, attachment["endItem"])
            route = [start, {"x": start["x"] + 100, "y": start["y"]},
                     {"x": start["x"] + 100, "y": 50}, {"x": end["x"], "y": 50}, end]
        a, b = attachment_point(source, attachment["startItem"]), attachment_point(target, attachment["endItem"])
        route[0], route[-1] = a, b
        returns = b["x"] <= a["x"] or segment_hits_node(a, b, source) or segment_hits_node(a, b, target)
        reason = "return" if returns else ("fee" if target["id"] in fee_ids else None)
        edge.update(attachment=attachment, route=route, connector_shape="elbowed" if reason else connector_style,
                    routing_exception=reason)
    # A bounded sweep finds straight-line obstructions. If it reaches its work
    # limit, retain ELK's routed paths for the unchecked edges conservatively.
    node_values = list(nodes.values())
    node_boxes = [_box_node(node) for node in node_values]
    edges = result["edges"]
    edge_boxes = [_box_segment(edge["route"][0], edge["route"][-1]) for edge in edges]
    all_boxes = edge_boxes + node_boxes
    routing_checks_truncated = False
    for count, (i, j) in enumerate(_pairs(all_boxes), 1):
        if count > MAX_COMPARISONS:
            routing_checks_truncated = True
            break
        if (i < len(edges)) == (j < len(edges)) or not _boxes_touch(all_boxes[i], all_boxes[j]):
            continue
        ei, ni = (i, j) if i < len(edges) else (j, i)
        edge, node = edges[ei], node_values[ni - len(edges)]
        if (not edge["routing_exception"] and node["id"] not in (edge["source"], edge["target"])
                and segment_hits_node(edge["route"][0], edge["route"][-1], node)):
            edge.update(connector_shape="elbowed", routing_exception="obstacle")
    if routing_checks_truncated:
        for edge in edges:
            if not edge["routing_exception"]:
                edge.update(connector_shape="elbowed", routing_exception="unchecked")
    exceptions = sum(bool(edge["routing_exception"]) and connector_style != "elbowed" for edge in edges)
    # Enforce the recorded transaction dependency order even for merged-address
    # display cycles. ELK partitions may route backwards but cannot reverse TXs.
    for node in nodes.values():
        if node["kind"] != "transaction":
            continue
        for vin in node.get("details", {}).get("transaction", {}).get("vin", []):
            parent = nodes.get("tx:" + str(vin.get("txid", "")))
            if (parent and not vin.get("is_pegin") and not vin.get("is_coinbase")
                    and parent["column"] < node["column"] and parent["x"] >= node["x"]):
                raise TraceError("ELK could not preserve transaction order; no Miro changes were made")
    main = [nodes[key] for key in main_ids]
    shift = -260 if fees else 0
    result["layout"] = {"algorithm": ALGORITHM, "version": ELK_VERSION, "direction": "left_to_right",
                        "main_top": min((node["y"] - node["height"] / 2 for node in main), default=160),
                        "main_bottom": max((node["y"] + node["height"] / 2 for node in main), default=320),
                        "fee_row_y": -100 if fees else None,
                        "cycle_groups": copy.deepcopy(graph.get("layout", {}).get("cycle_groups", [])),
                        "annotations": {"legend": {"x": 700, "y": -160 + shift}, "run": {"x": 700, "y": -480 + shift}},
                        "routing_exceptions": exceptions, "routing_checks_truncated": routing_checks_truncated}
    result.setdefault("graph_options", {})["connector_style"] = connector_style
    result["connector_attachment"] = "transaction_ports_v2"
    result["presentation_version"] = 6
    return result


def optimize_graph(graph, connector_style="straight", progress=None):
    """Deep-copy and optimize a display graph in at most three local ELK trials."""
    if not isinstance(connector_style, str) or connector_style not in STYLES:
        raise TraceError("Connector style must be straight, elbowed, or curved")
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) or not isinstance(graph.get("edges"), list):
        raise TraceError("Cannot optimize an invalid graph")
    if len(graph["nodes"]) > MAX_NODES or len(graph["edges"]) > MAX_EDGES:
        raise TraceError("Graph exceeds the local layout limit of 10,000 objects or 30,000 connections")
    nodes = {}
    for node in graph["nodes"]:
        if (not isinstance(node, dict) or not isinstance(node.get("id"), str) or node["id"] in nodes
                or node.get("kind") not in ("transaction", "address", "event")
                or not isinstance(node.get("column"), int)
                or any(not _finite(node.get(name)) for name in ("x", "y", "width", "height"))
                or min(node["width"], node["height"]) <= 0):
            raise TraceError("Cannot optimize invalid graph objects")
        nodes[node["id"]] = node
    edge_ids = set()
    for edge in graph["edges"]:
        if (not isinstance(edge, dict) or not isinstance(edge.get("id"), str) or edge["id"] in edge_ids
                or edge.get("source") not in nodes or edge.get("target") not in nodes):
            raise TraceError("Cannot optimize invalid graph connections")
        edge_ids.add(edge["id"])
    if progress:
        try:
            progress({"phase": "optimizing", "message": "Calculating local ELK layout", "completed": 0, "total": 1})
        except Exception:
            pass  # An advisory progress sink must not change layout behavior.
    request, ports, fee_ids = _request_graph(graph)
    seeds = [1, 7, 19] if len(request["children"]) <= 300 else [1]
    before = layout_metrics(graph)
    if request["children"]:
        candidates = _worker(request, seeds)
    else:
        candidates = [{"seed": 1, "nodes": [], "edges": []}]
    scored = []
    for candidate in candidates:
        try:
            result = _apply_candidate(graph, candidate, ports, fee_ids, connector_style)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise TraceError("ELK returned an invalid layout; no Miro changes were made") from exc
        metrics = layout_metrics(result)
        main = {"nodes": [node for node in result["nodes"] if node["id"] not in fee_ids],
                "edges": [edge for edge in result["edges"] if edge["source"] not in fee_ids and edge["target"] not in fee_ids]}
        score_metrics = layout_metrics(main) if fee_ids & nodes.keys() else metrics
        score = (score_metrics["node_overlaps"], score_metrics["node_intersections"], score_metrics["crossings"], score_metrics["edge_length"])
        scored.append((score, result, metrics, candidate["seed"]))
    _, result, after, seed = min(scored, key=lambda item: item[0])
    result["layout"]["metrics"] = {"before": before, "after": after, "estimated": True,
                                    "candidate_count": len(candidates), "selected_seed": seed,
                                    "routing_exceptions": result["layout"]["routing_exceptions"],
                                    "miro_routes_exact": False, "time_limit_seconds": TIMEOUT_SECONDS}
    if progress:
        try:
            progress({"phase": "optimizing", "message": "Local ELK layout ready", "completed": 1, "total": 1})
        except Exception:
            pass
    return result
