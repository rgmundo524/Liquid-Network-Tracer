"""Local ELK placement; presentation only, never tracing or attribution.

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
import time
from collections import defaultdict
from concurrent.futures import CancelledError
from pathlib import Path

from .common import TraceError
from .elk_errors import ELK_FATAL_FAILURE_CODES, ElkWorkerFailure
from .elk_parallel import POLL_SECONDS, iter_attempts
from .processes import defer_cancellation_during_spawn
from .render_runtime import renderer_failure, renderer_failure_code, renderer_heap_mb, renderer_peak_rss_mb
from .edge_labels import FONT_SIZE, LABEL_LAYOUT_VERSION, caption_size, caption_text, route_signature
from .input_order import input_orders, input_order_metadata
from .attachment_order import attachment_order_metrics
from .endpoint_alignment import align_near_horizontal_endpoints
from .horizontal_spacing import compact_candidate
from .layout import HORIZONTAL_NODE_GAP
from .layout_search import LAYOUT_SEARCH_VERSION, layout_seeds, normalize_layout_attempts
from .branch_layout import (BRANCH_LAYOUT_VERSION, edge_priorities, organization_metrics,
                            compact_context_inputs, hub_nodes)
from .branch_boundaries import branch_order, boundary_metrics
from .named_group_layout import (CORE_STRAIGHTNESS, center_order, center_metrics, group_structure)
from .transaction_neighborhoods import neighborhood_order, neighborhood_metrics
from .hub_layout import hub_plan, hub_layout_view


ALGORITHM = "elk_layered_v1"
ELK_VERSION = "0.12.0"
# This interval only reports activity. It is never a calculation deadline.
PROGRESS_INTERVAL_SECONDS = 5
MAX_COMPARISONS = 250000
STYLES = {"straight", "elbowed", "curved"}
_EPS = 1e-7


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _point(value):
    if not isinstance(value, dict) or not _finite(value.get("x")) or not _finite(value.get("y")):
        raise TraceError("ELK returned an invalid coordinate; no Miro changes were made")
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


def layout_metrics(graph, *, routed=True, max_comparisons=MAX_COMPARISONS, midpoint_elbows=False):
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
        if (midpoint_elbows and edge.get("connector_shape") in ("elbowed", "curved")
                and points[-1]["x"] > points[0]["x"]
                and edge.get("routing_exception") not in ("return", "fee")):
            # Miro does not accept ELK bends. A simple midpoint elbow exposes
            # the obstacle/crossing pattern seen on the board, but remains an
            # estimate, not a simulation of Miro's undocumented route chooser.
            a, b = points
            middle = (a["x"] + b["x"]) / 2
            points = [a, {"x": middle, "y": a["y"]}, {"x": middle, "y": b["y"]}, b]
        elif routed and edge.get("connector_shape") in ("elbowed", "curved") and edge.get("route"):
            points = [points[0], *edge["route"][1:-1], points[-1]]
        for a, b in zip(points, points[1:]):
            edge_length += math.hypot(a["x"] - b["x"], a["y"] - b["y"])
            segments.append((edge, a, b))
    boxes = [_box_segment(a, b) for _, a, b in segments]
    node_list = list(nodes.values())
    node_boxes = [_box_node(node) for node in node_list]
    all_boxes = boxes + node_boxes
    crossings, intersections, line_overlaps, overlaps = set(), set(), set(), 0
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
                # Count positive-length collinear overlaps separately from
                # crossings. Shared endpoints alone are not overlaps.
                if (abs(_cross(a, b, c)) < _EPS and abs(_cross(a, b, d)) < _EPS
                        and math.hypot(a["x"] - b["x"], a["y"] - b["y"]) > _EPS
                        and math.hypot(c["x"] - d["x"], c["y"] - d["y"]) > _EPS):
                    axis = "x" if abs(a["x"] - b["x"]) >= abs(a["y"] - b["y"]) else "y"
                    overlap = min(max(a[axis], b[axis]), max(c[axis], d[axis])) - max(min(a[axis], b[axis]), min(c[axis], d[axis]))
                    if overlap > _EPS:
                        line_overlaps.add(tuple(sorted((first["id"], second["id"]))))
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
    return {"crossings": len(crossings), "connector_overlaps": len(line_overlaps),
            "node_overlaps": overlaps, "node_intersections": len(intersections),
            "edge_length": round(edge_length, 2), "comparisons": min(comparisons, max_comparisons),
            "truncated": truncated, "estimated": True,
            "method": "midpoint_elbow_estimate" if midpoint_elbows else "planned_segments" if routed else "straight_segments",
            "labels_measured": False, "curves_approximated": True}


def _report_progress(progress, message, *, completed=0, elapsed_seconds=None, stage=None, **counts):
    if progress:
        event = {"phase": "optimizing", "message": message, "completed": completed, "total": 1 if completed else 0}
        if stage is not None:
            event["stage"] = stage
        event.update(counts)
        if elapsed_seconds is not None:
            event["elapsed_seconds"] = elapsed_seconds
        try:
            progress(event)
        except Exception:
            pass  # An advisory progress sink must not change layout behavior.


def _worker(graph, seeds, progress=None, *, heap_mb=None, cancel_event=None):
    project = Path(os.environ.get("LIQUID_TRACER_ROOT", Path(__file__).resolve().parents[1]))
    runner = project / "layout" / "run.mjs"
    node = os.environ.get("LIQUID_NODE_BIN") or shutil.which("node")
    if node and os.environ.get("LIQUID_NODE_BIN") and not Path(node).is_absolute():
        raise TraceError("LIQUID_NODE_BIN must be an absolute path to the pinned Node executable")
    if not node or not runner.is_file() or not (runner.parent / "node_modules" / "elkjs" / "package.json").is_file():
        raise TraceError("Local ELK dependencies are unavailable. Enter the project devenv shell and run liquid-layout-setup")
    heap_mb = renderer_heap_mb() if heap_mb is None else heap_mb
    if type(heap_mb) is not int or heap_mb < 1:
        raise TraceError("The local ELK worker requires a positive heap budget")
    node_count, edge_count = len(graph.get("children", [])), len(graph.get("edges", []))
    graph_size = f"{node_count:,} objects, {edge_count:,} connections"
    context = f"{graph_size}; Node heap budget {heap_mb:,} MiB"
    # Explicit allowlist strips API credentials, NODE_OPTIONS, preload hooks,
    # proxy variables, and secrets-provider state from this pure calculation.
    environment = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT") if name in os.environ}
    process = None
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError()
        _report_progress(progress, f"Calculating local ELK layout ({context}); cancel to stop",
                         stage="calculating", node_count=node_count, edge_count=edge_count, heap_mb=heap_mb)
        with defer_cancellation_during_spawn():
            process = subprocess.Popen([node, f"--max-old-space-size={heap_mb}", str(runner)],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, env=environment, start_new_session=True)
        # Signals are handled by the main thread. It can cancel while this
        # thread is inside Popen; ownership is established before we unwind.
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError()
        started = time.monotonic()
        last_report = 0
        payload = json.dumps({"graph": graph, "seeds": seeds})
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError()
            try:
                # communicate resumes pipe reads/writes after TimeoutExpired;
                # passing input again would duplicate the request. Its timeout
                # only lets us report that this local calculation is active.
                output, errors = process.communicate(
                    payload, timeout=POLL_SECONDS if cancel_event is not None else PROGRESS_INTERVAL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                payload = None
                elapsed = max(0, int(time.monotonic() - started))
                if elapsed < last_report + PROGRESS_INTERVAL_SECONDS:
                    continue
                last_report = elapsed
                _report_progress(progress, f"Calculating local ELK layout ({context}; {elapsed:,} seconds elapsed); cancel to stop",
                                 elapsed_seconds=elapsed, stage="calculating",
                                 node_count=node_count, edge_count=edge_count, heap_mb=heap_mb)
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError()
        if process.returncode:
            detail = renderer_failure(errors, process.returncode, "ELK", heap_mb)
            failure_code = renderer_failure_code(errors, process.returncode, "ELK")
            message = f"{detail} Graph: {graph_size}. No Miro changes were made"
            if failure_code in ELK_FATAL_FAILURE_CODES:
                raise TraceError(message)
            raise ElkWorkerFailure(message, failure_code=failure_code, returncode=process.returncode)
        result = json.loads(output)
        if not isinstance(result, dict) or result.get("version") != ELK_VERSION or not isinstance(result.get("candidates"), list):
            raise ValueError("invalid worker response")
        if not len(seeds) <= len(result["candidates"]) <= 2 * len(seeds):
            raise ValueError("missing candidates")
        expected_boundary = graph.get("boundaryOrdering", False)
        if any(not isinstance(candidate, dict)
               or type(candidate.get("branchBoundary", False)) is not bool
               or candidate.get("branchBoundary", False) != expected_boundary
               for candidate in result["candidates"]):
            raise ValueError("invalid branch boundary ordering result")
        if any(candidate.get("inputOrderPolicy") == "geometry"
               and candidate.get("inputOrderFallback") == "traced_first_order_not_preserved"
               for candidate in result["candidates"]):
            _report_progress(progress, "Preferred connector ordering unavailable; retaining ELK geometry for validation",
                             stage="input_order_fallback")
        peak_rss_mb = renderer_peak_rss_mb(errors)
        if peak_rss_mb is not None:
            _report_progress(progress, f"Measured ELK worker peak RAM: {peak_rss_mb:,} MiB",
                             stage="memory_measured", peak_rss_mb=peak_rss_mb)
        return result["candidates"]
    except OSError as exc:
        raise TraceError("The local ELK worker could not start or communicate; check the Node executable and local system resources. "
                         "No Miro changes were made") from exc
    except (ValueError, TypeError) as exc:
        raise TraceError("Local ELK returned an invalid layout; no Miro changes were made") from exc
    finally:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()


def _request_graph(graph):
    original = graph
    graph = hub_layout_view(graph)
    # Source-first layering keeps independent hub spenders in a vertical
    # column even when some output branches terminate earlier than others.
    hub_layering = ({"elk.layered.layering.strategy": "LONGEST_PATH_SOURCE"}
                    if hub_plan(graph)["hubs"] else {})
    nodes = {node["id"]: node for node in graph["nodes"]}
    fee_ids = {key for key, item in graph.get("fee_items", {}).items() if item["endpoint"] == "shapes"}
    main = {key: node for key, node in nodes.items() if key not in fee_ids}
    # Explicit hubs restart presentation depth. The temporary view retains
    # every object and connector; original dependency columns remain saved.
    columns = {key: node["column"] for key, node in main.items()}
    children, port_map = {}, {}
    for key, node in sorted(main.items()):
        children[key] = {"id": key, "width": node["width"], "height": node["height"], "ports": [],
                         "layoutOptions": {"elk.partitioning.partition": str(columns[key]),
                                           "elk.portConstraints": "FIXED_SIDE"}}
    edge_values = []
    priorities = edge_priorities(graph)
    centered = group_structure(graph)
    for key in centered["edges"]:
        priorities[key] = max(priorities[key], CORE_STRAIGHTNESS)
    main_edges = sorted((edge for edge in graph["edges"] if edge["source"] in main and edge["target"] in main),
                        key=lambda edge: edge["id"])
    for index, edge in enumerate(main_edges):
        ports = []
        for outgoing, key, other in ((True, edge["source"], edge["target"]), (False, edge["target"], edge["source"])):
            node = main[key]
            if node["kind"] == "transaction":
                east = outgoing
            else:
                east = columns[other] > columns[key]
                if columns[other] == columns[key]:
                    east = not outgoing
            port_id = "p" + str(index) + ("s" if outgoing else "t")
            children[key]["ports"].append({"id": port_id, "width": 0, "height": 0,
                                           "layoutOptions": {"elk.port.side": "EAST" if east else "WEST"}})
            ports.append(port_id)
        port_map[edge["id"]] = ports
        item = {"id": edge["id"], "sources": [ports[0]], "targets": [ports[1]],
                "layoutOptions": {"elk.layered.priority.straightness": str(priorities[edge["id"]])}}
        if caption_text(edge):
            # ELK needs dimensions, not confidential caption text, to reserve
            # room. Keep the full public-facing text in the Python graph only.
            # ELK ignores labels with no text, even if dimensions are supplied.
            # This constant activates placement without sharing caption text.
            item["labels"] = [{"id": "label:" + edge["id"], "text": "caption", **caption_size(edge),
                               "layoutOptions": {"elk.edgeLabels.placement": "CENTER"}}]
        edge_values.append(item)
    ordered_nodes = neighborhood_order(graph, branch_order(graph))
    centered_order = center_order(graph, ordered_nodes, centered)
    center_metadata = {"centerNodeOrder": centered_order} if centered_order is not None else {}
    boundary_order = ({"branchNodeOrder": [key for key in ordered_nodes if key in main]}
                      if ordered_nodes is not None else {})
    return {"id": "liquid-layout", "layoutOptions": {
        "elk.algorithm": "layered", "elk.direction": "RIGHT", "elk.edgeRouting": "ORTHOGONAL",
        "elk.partitioning.activate": "true", "elk.spacing.nodeNode": "80", "elk.spacing.componentComponent": "120",
        "elk.layered.spacing.nodeNodeBetweenLayers": str(HORIZONTAL_NODE_GAP), "elk.spacing.edgeNode": "35",
        "elk.layered.spacing.edgeNodeBetweenLayers": "60", "elk.spacing.edgeEdge": "22",
        "elk.layered.spacing.edgeEdgeBetweenLayers": "22", "elk.layered.thoroughness": "8",
        "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
        "elk.layered.crossingMinimization.greedySwitch.type": "TWO_SIDED",
        "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF",
        "elk.layered.nodePlacement.favorStraightEdges": "true",
        "elk.layered.edgeLabels.sideSelection": "ALWAYS_UP", "elk.spacing.edgeLabel": "7",
        "elk.padding": "[top=0,left=0,bottom=0,right=0]", **hub_layering},
        "children": list(children.values()), "edges": edge_values,
        "branchOrganization": BRANCH_LAYOUT_VERSION,
        **boundary_order,
        **center_metadata,
        "inputPortOrders": {key: [port_map[edge_id][1] for edge_id in order]
                            for key, order in input_orders(original).items()}}, port_map, fee_ids


def _apply_candidate(graph, candidate, port_map, fee_ids, connector_style):
    input_policy = candidate.get("inputOrderPolicy", "traced_first")
    if input_policy not in ("traced_first", "geometry"):
        raise TraceError("ELK returned an invalid input ordering policy")
    input_fallback = candidate.get("inputOrderFallback")
    if "inputOrderFallback" in candidate and (
            input_policy != "geometry" or input_fallback != "traced_first_order_not_preserved"):
        raise TraceError("ELK returned an invalid input ordering fallback")
    if "inputOrderRejected" in candidate and (
            "inputOrderFallback" in candidate or candidate.get("inputOrderPolicy") != "traced_first"
            or candidate["inputOrderRejected"] != "traced_first_order_not_preserved"):
        raise TraceError("ELK returned an invalid rejected input ordering alternative")
    boundary_ordering = candidate.get("branchBoundary", False)
    if type(boundary_ordering) is not bool:
        raise TraceError("ELK returned an invalid branch boundary ordering policy")
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
        node.update(_point({"x": point["x"] + node["width"] / 2 + offset_x,
                            "y": point["y"] + node["height"] / 2 + offset_y}))
        for port in raw.get("ports", []):
            ports[port["id"]] = _port(node, **_point(port))
    route_map, label_map = {}, {}
    for raw in raw_edges:
        sections = raw.get("sections")
        if not isinstance(sections, list) or len(sections) != 1:
            raise TraceError("ELK returned an unsupported connector route")
        section = sections[0]
        points = [section.get("startPoint"), *section.get("bendPoints", []), section.get("endPoint")]
        route_map[raw["id"]] = [_point({"x": point["x"] + offset_x, "y": point["y"] + offset_y})
                                for point in map(_point, points)]
        labels = raw.get("labels", [])
        if not isinstance(labels, list) or len(labels) > 1:
            raise TraceError("ELK returned invalid connector labels")
        if labels:
            label = labels[0]
            if (not isinstance(label, dict) or label.get("id") != "label:" + raw["id"]
                    or any(not _finite(label.get(key)) or label[key] <= 0 for key in ("width", "height"))):
                raise TraceError("ELK returned invalid connector label dimensions")
            position = _point(label)
            label_map[raw["id"]] = {**_point({"x": position["x"] + offset_x, "y": position["y"] + offset_y}),
                                    "width": label["width"], "height": label["height"]}
    # Existing fee row x positions already encode confirmed height/time order.
    fees = sorted((nodes[key] for key in fee_ids & nodes.keys()), key=lambda node: (node["x"], node["id"]))
    for index, node in enumerate(fees):
        node.update(x=130 + index * 230, y=-100)
    for edge in result["edges"]:
        edge.pop("label_layout", None)
        if edge["id"] in port_map and bool(caption_text(edge)) != (edge["id"] in label_map):
            raise TraceError("ELK omitted or changed connector labels")
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
        if edge["id"] in label_map:
            label = label_map[edge["id"]]
            size = caption_size(edge)
            if any(label[key] != size[key] for key in ("width", "height")):
                raise TraceError("ELK changed connector label dimensions")
            edge["label_layout"] = {**label, "route_signature": route_signature([(p["x"], p["y"]) for p in route])}
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
    # Preserve every dependency except a verified vin routed through a selected
    # hub. That exact relation returns to the hub before starting its new tree.
    hub_layout = hub_plan(graph)
    columns = hub_layout["columns"]
    for node in nodes.values():
        if node["kind"] != "transaction":
            continue
        for index, vin in enumerate(node.get("details", {}).get("transaction", {}).get("vin", [])):
            parent = nodes.get("tx:" + str(vin.get("txid", "")))
            if (parent and not vin.get("is_pegin") and not vin.get("is_coinbase")
                    and (node["id"], index) not in hub_layout["cut_inputs"]
                    and columns.get(parent["id"], parent["column"]) < columns.get(node["id"], node["column"])
                    and parent["x"] >= node["x"]):
                raise TraceError("ELK could not preserve transaction order; no Miro changes were made")
    for hub, children in hub_layout["roots"].items():
        if any(nodes[hub]["x"] >= nodes[child]["x"] for child in children):
            raise TraceError("ELK could not preserve separate hub tree order; no Miro changes were made")
    main = [nodes[key] for key in main_ids]
    shift = -260 if fees else 0
    result["layout"] = {"algorithm": ALGORITHM, "version": ELK_VERSION, "direction": "left_to_right",
                        "main_top": min((node["y"] - node["height"] / 2 for node in main), default=160),
                        "main_bottom": max((node["y"] + node["height"] / 2 for node in main), default=320),
                        "fee_row_y": -100 if fees else None,
                        "cycle_groups": copy.deepcopy(graph.get("layout", {}).get("cycle_groups", [])),
                        "annotations": {"legend": {"x": 700, "y": -160 + shift}},
                        "routing_exceptions": exceptions, "routing_checks_truncated": routing_checks_truncated,
                        "input_order": input_order_metadata(graph, input_policy),
                        "branch_organization": {"version": BRANCH_LAYOUT_VERSION,
                                                 "profile": candidate.get("branchProfile", "balanced"),
                                                 "boundary_ordering": boundary_ordering,
                                                 "hubs": sorted(hub_nodes(graph)),
                                                 "hub_rule": "restart_tree_depth_with_return_connections",
                                                 "hub_roots": hub_layout["roots"],
                                                 "hub_columns": hub_layout["columns"]},
                        "horizontal_spacing": copy.deepcopy(candidate.get("horizontal_spacing", {})),
                        "edge_labels": {"version": LABEL_LAYOUT_VERSION, "estimated": True,
                                        "font_size": FONT_SIZE, "placement": "center_above",
                                        "reserved_count": len(label_map), "miro_positions_exact": False}}
    if input_fallback is not None:
        result["layout"]["input_order"]["fallback_reason"] = input_fallback
    result.setdefault("graph_options", {})["connector_style"] = connector_style
    result["connector_attachment"] = "transaction_ports_v2"
    result["presentation_version"] = max(6, graph.get("presentation_version", 0))
    return result


def _validate_graph(graph, connector_style):
    if not isinstance(connector_style, str) or connector_style not in STYLES:
        raise TraceError("Connector style must be straight, elbowed, or curved")
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) or not isinstance(graph.get("edges"), list):
        raise TraceError("Cannot optimize an invalid graph")
    if "_hub_layout_view" in graph or "_hub_layout_plan" in graph:
        raise TraceError("Cannot optimize an internal layout view")
    nodes = {}
    for node in graph["nodes"]:
        if (not isinstance(node, dict) or not isinstance(node.get("id"), str) or node["id"] in nodes
                or node.get("kind") not in ("transaction", "address", "event", "context_group")
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
    return nodes


def fallback_graph(graph, connector_style="straight", reason="size_limit"):
    """Reuse the full dependency layout without sending a large graph to ELK.

    Production callers supply build_graph's deterministic, nonoverlapping
    dependency positions. This pass is presentation only: it never removes,
    merges, or repositions objects and never changes recorded relationships.
    Ports are assigned in sorted neighbor order with linear edge traversal.
    Routing is deliberately simple; geometric quality is measured with the
    same finite comparison budget rather than claimed to be optimized.
    """
    _validate_graph(graph, connector_style)
    descriptions = {
        "size_limit": "The graph exceeds the ELK optimization size limit",
        "timeout": "ELK reached its 30-second time limit",
        "mermaid_size_limit": "The graph exceeds the Mermaid rendering size limit",
        "mermaid_timeout": "Mermaid reached its rendering time limit",
    }
    if reason not in descriptions:
        raise TraceError("Invalid layout fallback reason")
    result = copy.deepcopy(graph)
    nodes = {node["id"]: node for node in result["nodes"]}
    fee_ids = {key for key, item in result.get("fee_items", {}).items()
               if item["endpoint"] == "shapes" and key in nodes}
    ports = defaultdict(list)
    ordered_inputs = {key: {edge_id: index for index, edge_id in enumerate(order)}
                      for key, order in input_orders(result).items()}
    for edge in result["edges"]:
        edge["attachment"] = {}
        for field, key, other, outgoing in (
            ("startItem", edge["source"], edge["target"], True),
            ("endItem", edge["target"], edge["source"], False),
        ):
            node, neighbor = nodes[key], nodes[other]
            east = outgoing if node["kind"] == "transaction" else neighbor["x"] > node["x"]
            side = "bottom" if key in fee_ids else "east" if east else "west"
            ports[key, side].append((neighbor["y"], other, edge["id"], field, edge))
    for (key, side), values in ports.items():
        node = nodes[key]
        ranks = ordered_inputs.get(key) if side == "west" else None
        ordered = sorted(values, key=lambda item: (item[0], ranks[item[2]] if ranks is not None else 0, *item[1:4]))
        for index, (_, _, _, field, edge) in enumerate(ordered):
            fraction = (index + 1) / (len(values) + 1)
            x = fraction * node["width"] if side == "bottom" else node["width"] if side == "east" else 0
            y = node["height"] if side == "bottom" else fraction * node["height"]
            edge["attachment"][field] = _port(node, x, y)
    exceptions = 0
    for edge in result["edges"]:
        source, target = nodes[edge["source"]], nodes[edge["target"]]
        a = attachment_point(source, edge["attachment"]["startItem"])
        b = attachment_point(target, edge["attachment"]["endItem"])
        returning = b["x"] <= a["x"] or segment_hits_node(a, b, source) or segment_hits_node(a, b, target)
        exception = "fee" if edge["target"] in fee_ids else "return" if returning else None
        middle = (a["x"] + b["x"]) / 2
        route = [a, {"x": middle, "y": a["y"]}, {"x": middle, "y": b["y"]}, b]
        if exception:
            departure = a["x"] + (100 if a["x"] >= source["x"] else -100)
            if exception == "fee":
                lane = target["y"] + target["height"] / 2 + 70
                route = [a, {"x": departure, "y": a["y"]}, {"x": departure, "y": lane},
                         {"x": b["x"], "y": lane}, b]
            else:
                arrival = b["x"] + (100 if b["x"] >= target["x"] else -100)
                lane = min(source["y"] - source["height"] / 2, target["y"] - target["height"] / 2) - 80
                route = [a, {"x": departure, "y": a["y"]}, {"x": departure, "y": lane},
                         {"x": arrival, "y": lane}, {"x": arrival, "y": b["y"]}, b]
        edge.update(route=route, connector_shape="elbowed" if exception else connector_style,
                    routing_exception=exception)
        exceptions += bool(exception) and connector_style != "elbowed"
    main = [node for key, node in nodes.items() if key not in fee_ids]
    shift = -260 if fee_ids else 0
    notice = (f"{descriptions[reason]}. Using the full dependency layout for "
              f"{len(nodes):,} objects and {len(result['edges']):,} connections; no objects or connections were omitted. "
              "Crossing optimization was skipped; connectors may cross objects or other connectors.")
    result["layout"] = {"algorithm": "dependency_layers_v1", "direction": "left_to_right",
                        "main_top": min((node["y"] - node["height"] / 2 for node in main), default=160),
                        "main_bottom": max((node["y"] + node["height"] / 2 for node in main), default=320),
                        "fee_row_y": graph.get("layout", {}).get("fee_row_y", -100 if fee_ids else None),
                        "cycle_groups": copy.deepcopy(graph.get("layout", {}).get("cycle_groups", [])),
                        "annotations": copy.deepcopy(graph.get("layout", {}).get("annotations", {
                            "legend": {"x": 700, "y": -160 + shift}})),
                        "fallback_reason": reason, "fallback_notice": notice,
                        "placement": "complete_graph_v1",
                        "routing_exceptions": exceptions, "routing_checks_truncated": False,
                        "crossing_optimization": False, "input_order": input_order_metadata(result, "geometry")}
    result["layout"]["metrics"] = {"before": layout_metrics(graph), "after": layout_metrics(result),
                                    "estimated": True, "candidate_count": 0,
                                    "routing_exceptions": exceptions, "miro_routes_exact": False}
    result.setdefault("graph_options", {})["connector_style"] = connector_style
    result["connector_attachment"] = "transaction_ports_v2"
    result["presentation_version"] = max(6, graph.get("presentation_version", 0))
    return result


def _validate_rejected_pairs(candidates):
    # Keep temporary pairing references scoped here so raw worker geometry can
    # be released as the caller consumes each candidate.
    for candidate in candidates:
        if "inputOrderRejected" not in candidate:
            continue
        same_seed = [other for other in candidates if other.get("seed") == candidate.get("seed")]
        partners = [other for other in same_seed if "inputOrderRejected" not in other
                    and other.get("inputOrderPolicy") == "geometry"
                    and other.get("inputOrderFallback") == "traced_first_order_not_preserved"]
        if len(same_seed) != 2 or len(partners) != 1:
            raise TraceError("ELK returned an unpaired rejected input ordering alternative")


def optimize_graph(graph, connector_style="straight", progress=None, *, layout_attempts=None):
    """Compare bounded parallel ELK attempts without graph size or time ceilings.

    A bounded candidate batch and the best result are retained. Possible memory
    failures retry alone with the full heap budget, then remain sequential.
    Other worker-process failures skip a seed, retaining earlier valid results.
    Invalid data and cancellation abort the search. The input is never modified.
    Quality measurement has its own work budget and never removes graph elements.
    """
    nodes = _validate_graph(graph, connector_style)
    attempts = normalize_layout_attempts(
        graph.get("graph_options", {}).get("layout_attempts") if layout_attempts is None else layout_attempts)
    seeds = layout_seeds(attempts)

    def attempt_progress(index, seed):
        def report(event):
            if progress:
                progress({**event, "attempt_index": index, "attempt_total": attempts, "seed": seed})
        return report

    report = attempt_progress(1, seeds[0])
    _report_progress(report, "Preparing local ELK search; cancel to stop", stage="preparing",
                     node_count=len(graph["nodes"]), edge_count=len(graph["edges"]))
    request, ports, fee_ids = _request_graph(graph)
    _report_progress(report, "Measuring input layout", stage="measuring_input")
    before = layout_metrics(graph)
    best = None
    candidate_count = 0
    successful_count = 0
    failed_attempts = []
    execution = {}
    for index, seed, candidates in iter_attempts(request, seeds, _worker, attempt_progress, execution):
        report = attempt_progress(index, seed)
        if isinstance(candidates, ElkWorkerFailure):
            failed_attempts.append({"attempt_index": index, "seed": seed, "failure_code": candidates.failure_code})
            _report_progress(report, "ELK layout attempt failed; retaining completed layouts and continuing the search",
                             stage="attempt_failed", attempted_count=index, successful_count=successful_count,
                             failed_count=len(failed_attempts), failure_code=candidates.failure_code)
            continue
        # A rejected optional rerun must travel with its same-seed geometry
        # fallback. Validate both layouts below before retaining either result.
        _validate_rejected_pairs(candidates)
        while candidates:
            candidate = candidates.pop(0)
            _report_progress(report, "Validating ELK coordinates and routes", stage="applying")
            try:
                compact_candidate(candidate)
                result = _apply_candidate(graph, candidate, ports, fee_ids, connector_style)
                from .change_layout import apply_change_layout
                apply_change_layout(result)
                align_near_horizontal_endpoints(result)
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise TraceError("ELK returned an invalid layout; no Miro changes were made") from exc
            if "inputOrderRejected" in candidate:
                del candidate, result
                continue
            candidate_count += 1
            _report_progress(report, "Measuring completed ELK layout", stage="measuring_output")
            metrics = layout_metrics(result)
            main = {"nodes": [node for node in result["nodes"] if node["id"] not in fee_ids],
                    "edges": [edge for edge in result["edges"] if edge["source"] not in fee_ids and edge["target"] not in fee_ids]}
            score_metrics = layout_metrics(main) if fee_ids & nodes.keys() else metrics
            endpoint_metrics = attachment_order_metrics(main)
            miro_estimate = layout_metrics(main, midpoint_elbows=True)
            organization = organization_metrics(main)
            result["layout"]["branch_organization"]["travel"] = organization
            neighborhoods = neighborhood_metrics(result)
            result["layout"]["branch_organization"]["neighborhoods"] = neighborhoods
            boundaries = boundary_metrics(result)
            result["layout"]["branch_organization"]["boundaries"] = boundaries
            centered = center_metrics(result)
            if graph.get("graph_options", {}).get("center_name"):
                result["layout"]["named_group"] = centered
            # Compare native ELK routes with a simple board-routing estimate. A
            # forced semantic slot order must not win merely because ELK can draw
            # bends that Miro cannot receive. Branch separation follows every
            # collision gate. Local forks and transaction proximity matter even
            # within one seed lineage; historical date/port order is secondary.
            score = (score_metrics["node_overlaps"], score_metrics["node_intersections"],
                     miro_estimate["node_intersections"], score_metrics["crossings"], miro_estimate["crossings"],
                     endpoint_metrics["endpoint_order_inversions"], endpoint_metrics["coincident_ports"],
                     score_metrics["connector_overlaps"], miro_estimate["connector_overlaps"],
                     centered["alignment_deviation"], centered["center_offset"],
                     boundaries["interleavings"], boundaries["boundary_depth"], boundaries["interbranch_travel"],
                     neighborhoods["flow_order_inversions"], neighborhoods["sibling_interleavings"],
                     neighborhoods["transaction_distance"], neighborhoods["transaction_center_drift"],
                     organization["weighted_vertical_travel"] + score_metrics["edge_length"], score_metrics["edge_length"],
                     result["layout"]["input_order"]["policy"] != "traced_first")
            if best is None or score < best[0]:
                best = (score, result, metrics, candidate["seed"])
            # Drop the current candidate and its graph views before requesting
            # another seed. The best tuple alone owns the retained winner.
            del candidate, result, main
        del candidates
        successful_count += 1
    if best is None:
        failure_codes = {attempt["failure_code"] for attempt in failed_attempts}
        codes = ", ".join(sorted(failure_codes))
        guidance = (" Memory exhaustion was reported. Close other applications and retry this saved run; "
                    "automatic heap budgets are recalculated before each batch or standalone attempt."
                    if failure_codes & {"heap_exhausted", "memory_exhausted"} else "")
        if not guidance and "worker_killed" in failure_codes:
            guidance = " A worker was killed; memory exhaustion is possible but unconfirmed."
        raise TraceError(f"All {attempts} ELK layout attempts failed; no valid layout was produced. "
                         f"Failure categories: {codes}.{guidance} No Miro changes were made")
    _, result, after, seed = best
    _report_progress(report, "Packing nearby transaction context", stage="applying")
    # Moving a circle closer can put Miro's midpoint elbow through a different
    # object even when ELK's saved route remains safe. Check that final pass
    # against the same endpoint/board estimates before accepting its positions.
    compacted = copy.deepcopy(result)
    compact_context_inputs(compacted)
    if compacted["layout"]["branch_organization"].get("context_inputs_moved", 0):
        alignment = compacted["layout"].get("endpoint_alignment", {})
        align_near_horizontal_endpoints(compacted)
        compacted["layout"]["endpoint_alignment"] = {
            **alignment, "after_context_compaction": compacted["layout"]["endpoint_alignment"]}
        original_estimate = layout_metrics(result, midpoint_elbows=True)
        compacted_estimate = layout_metrics(compacted, midpoint_elbows=True)
        original_ports, compacted_ports = attachment_order_metrics(result), attachment_order_metrics(compacted)
        original_boundaries, compacted_boundaries = boundary_metrics(result), boundary_metrics(compacted)
        original_neighbors, compacted_neighbors = neighborhood_metrics(result), neighborhood_metrics(compacted)
        original_center, compacted_center = center_metrics(result), center_metrics(compacted)
        def routing_quality(value):
            return (value["node_intersections"], value["crossings"], value["connector_overlaps"])
        safe_boundaries = all(compacted_boundaries[key] <= original_boundaries[key]
                              for key in ("interleavings", "boundary_depth", "interbranch_travel"))
        safe_neighbors = all(compacted_neighbors[key] <= original_neighbors[key]
                             for key in ("flow_order_inversions", "sibling_interleavings", "transaction_distance"))
        safe = (not original_estimate["truncated"] and not compacted_estimate["truncated"]
                and routing_quality(compacted_estimate) <= routing_quality(original_estimate)
                and compacted_ports["endpoint_order_inversions"] <= original_ports["endpoint_order_inversions"]
                and compacted_ports["coincident_ports"] <= original_ports["coincident_ports"]
                and all(compacted_center[key] <= original_center[key]
                        for key in ("alignment_deviation", "center_offset"))
                and safe_boundaries and safe_neighbors)
        if safe:
            result = compacted
        else:
            result["layout"]["branch_organization"]["context_compaction_rejected"] = (
                "branch_boundary_quality" if not safe_boundaries else
                "transaction_neighborhood_quality" if not safe_neighbors else "attachment_routing_estimate")
    else:
        result = compacted
    # Clearance repair is allowed to use extra space. A distance-reduction or
    # footprint gate here would keep context shapes trapped on existing lines.
    from .context_clearance import repair_context_clearance
    _report_progress(report, "Clearing connector space around context inputs", stage="applying")
    repair_context_clearance(result)
    result["layout"]["branch_organization"]["boundaries"] = boundary_metrics(result)
    result["layout"]["branch_organization"]["neighborhoods"] = neighborhood_metrics(result)
    if graph.get("graph_options", {}).get("center_name"):
        result["layout"]["named_group"] = center_metrics(result)
    after = layout_metrics(result)
    result["layout"]["metrics"] = {"before": before, "after": after, "estimated": True,
                                    "attempt_count": attempts, "candidate_count": candidate_count, "selected_seed": seed,
                                    "attempted_count": attempts, "successful_count": successful_count,
                                    "failed_count": len(failed_attempts),
                                    "attachments": attachment_order_metrics(result),
                                    "miro_routing_estimate": layout_metrics(result, midpoint_elbows=True),
                                    "routing_exceptions": result["layout"]["routing_exceptions"],
                                    "miro_routes_exact": False, "time_limit_seconds": None}
    result["layout"]["search"] = {"version": LAYOUT_SEARCH_VERSION, "attempt_count": attempts,
                                  "seeds": list(seeds), "candidate_count": candidate_count,
                                  "attempted_count": attempts, "successful_count": successful_count,
                                  "failed_count": len(failed_attempts), "failed_attempts": failed_attempts,
                                  "selected_seed": seed, **execution}
    result.setdefault("graph_options", {})["layout_attempts"] = attempts
    _report_progress(report, "Local ELK layout ready" if not failed_attempts
                     else "Best completed ELK layout ready; some layout attempts failed",
                     completed=1, stage="ready_with_failures" if failed_attempts else "ready",
                     attempted_count=attempts, successful_count=successful_count, failed_count=len(failed_attempts))
    return result
