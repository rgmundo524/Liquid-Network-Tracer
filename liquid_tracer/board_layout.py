"""Read-only board snapshots and isolated ELK additions for maintained plots.

Snapshots retain geometry and hashes, never copies of unrelated board text.
The sync journal records our acknowledged changes so an interrupted update can
resume while still refusing unrelated changes to its reviewed board snapshot.
"""
import copy
import fcntl
import math
import os
import re
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import quote, urlencode

from .api import http
from .common import TraceError, canonical, digest

GAP = 600.


def _hash(value):
    return digest(canonical(value))


def _descriptor(body):
    # Ignore transport links/timestamps. Geometry is normalized because REST
    # may omit default origin/rotation or serialize coordinates as integers.
    value = {key: copy.deepcopy(body[key]) for key in
             ("type", "data", "style", "captions", "shape", "startItem", "endItem") if key in body}
    for name, field in list(value.get("style", {}).items()):
        if name in ("fontSize", "borderWidth", "borderOpacity", "fillOpacity", "strokeWidth"):
            try:
                value["style"][name] = float(field)
            except (TypeError, ValueError):
                pass
        elif name.endswith("Color") and isinstance(field, str):
            value["style"][name] = field.lower()
    content = value.get("data", {}).get("content")
    if isinstance(content, str):
        value["data"]["content"] = re.sub(r"<br\s*/?>", "<br>", content)
    if "captions" in value:
        value["captions"] = [{key: caption[key] for key in ("content", "position") if key in caption}
                             for caption in value["captions"]]
    for name in ("startItem", "endItem"):
        if name in value:
            value[name] = {key: value[name][key] for key in ("id", "position", "snapTo") if key in value[name]}
    if "position" in body:
        position = body["position"]
        value["position"] = {axis: float(position[axis]) for axis in ("x", "y")}
        value["position"].update(relativeTo=position.get("relativeTo", "canvas_center"),
                                 origin=position.get("origin", "center"))
    if "geometry" in body:
        value["geometry"] = {axis: float(body["geometry"].get(axis, 0)) for axis in ("width", "height", "rotation")}
    if (body.get("parent") or {}).get("id"):
        value["parent"] = {"id": body["parent"]["id"]}
    return {key: _hash(item) for key, item in value.items()}


def mapped_record(record):
    result = {field: copy.deepcopy(record[field]) for field in ("id", "endpoint", "source", "target") if field in record}
    if record.get("context_group_proof"):
        from .context_group_miro import _identity
        result["context_identity_sha256"] = _hash(_identity(record["context_group_proof"]))
    if record.get("context_parallel_proof"):
        from .context_parallel_miro import _valid
        result["context_parallel_sha256"] = _hash(_valid(record["context_parallel_proof"]))
    return result


def _items(requests, base, headers):
    from .miro import _response
    result, cursors, cursor = {}, set(), None
    while True:
        query = {"limit": 50, **({"cursor": cursor} if cursor else {})}
        status, _, raw = requests.request("GET", base + "/items?" + urlencode(query), headers)
        if status != 200:
            raise TraceError("Cannot index the Miro board items; no board writes made")
        page = _response(raw, "board layout inventory")
        data, cursor = page.get("data"), page.get("cursor")
        if not isinstance(data, list) or (cursor is not None and not isinstance(cursor, str)):
            raise TraceError("Malformed board item inventory; no board writes made")
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] or item["id"] in result:
                raise TraceError("Board items changed while indexing; regenerate the plot")
            result[item["id"]] = item
        if not cursor:
            if "total" in page and page["total"] != len(result):
                raise TraceError("Incomplete board item inventory; regenerate the plot")
            return result
        if cursor in cursors or not data:
            raise TraceError("Incomplete board item pagination; regenerate the plot")
        cursors.add(cursor)


def _complete_geometry(item):
    try:
        values = [item["position"][axis] for axis in ("x", "y")]
        values += [item["geometry"][axis] for axis in ("width", "height")]
        return all(type(value) in (int, float) and math.isfinite(value) for value in values) and min(values[2:]) > 0
    except (KeyError, TypeError):
        return False


def _fill_geometry(requests, base, headers, inventory, mapped_ids):
    """GenericItem pages can omit text/image dimensions; read those items fully."""
    from .miro import _response
    missing = [item_id for item_id, body in inventory.items()
               if item_id not in mapped_ids and body.get("type") != "connector" and not _complete_geometry(body)]

    def read(item_id):
        return requests.request("GET", base + "/items/" + quote(item_id, safe=""), headers)

    def accept(item_id, response):
        status, _, raw = response
        if not 200 <= status < 300:
            raise TraceError("Cannot read complete geometry for an existing board item; no board writes made")
        body = _response(raw, "board item geometry")
        previous = inventory[item_id]
        if (body.get("id") != item_id or (previous.get("type") and body.get("type") != previous["type"])
                or not _complete_geometry(body)):
            raise TraceError("An existing board item's geometry could not be verified; no board writes made")
        inventory[item_id] = {**previous, **body}

    requests.map(missing, read, accept)


def _canvas(items):
    """Flatten parent-relative coordinates, including unmanaged frame parents."""
    from .miro import _bounds
    result = {}
    for item_id, item in items.items():
        if item.get("type") == "connector":
            continue
        chain, current, seen = [], item_id, set()
        while current not in result:
            if current in seen or current not in items:
                raise TraceError("Cannot resolve board frame/group coordinates; no board writes made")
            seen.add(current)
            body = items[current]
            position, parent = body.get("position", {}), (body.get("parent") or {}).get("id")
            chain.append(current)
            if not parent or position.get("relativeTo") == "canvas_center":
                break
            if position.get("relativeTo") != "parent_top_left":
                raise TraceError("Unsupported board frame/group coordinates; move the item to the canvas before plotting")
            current = parent
        for key in reversed(chain):
            body = copy.deepcopy(items[key])
            position, parent = body.get("position", {}), (body.get("parent") or {}).get("id")
            if parent and position.get("relativeTo") != "canvas_center":
                parent_body = result[parent]
                if float(parent_body.get("geometry", {}).get("rotation", 0)) % 360:
                    raise TraceError("Cannot resolve children of a rotated board container")
                x, y, width, height = _bounds(parent_body, parent)
                try:
                    position.update(x=x - width / 2 + float(position["x"]),
                                    y=y - height / 2 + float(position["y"]), relativeTo="canvas_center")
                except (KeyError, TypeError, ValueError, OverflowError):
                    raise TraceError("Invalid board frame/group coordinates") from None
            _bounds(body, key)
            result[key] = body
    return result


def read_live(requests, base, headers, state, remote=None):
    from .address_migration import _inventory
    from .miro import _bounds
    from .miro_reads import preflight
    inventories = {}

    def read(kind):
        # Each endpoint's cursor chain is sequential. The two independent
        # chains share the existing worker bound and Miro credit pacing gate.
        return (_items if kind == "items" else _inventory)(requests, base, headers)

    requests.map(("items", "connectors"), read,
                 lambda kind, result: inventories.__setitem__(kind, result))
    inventory = inventories["items"]
    connectors = {item_id: {**body, "type": "connector"} for item_id, body in inventories["connectors"].items()}
    inventory.update(connectors)
    if remote is None:
        remote = preflight(requests, base, headers, state, {})
    # Full mapped bodies are authoritative for editable fields. /items only
    # exposes summaries and must never be used as a text/style baseline.
    for key, body in remote.items():
        record = state["items"][key]
        inventory[record["id"]] = {**body, "type": {"shapes": "shape", "connectors": "connector", "frames": "frame"}[record["endpoint"]]}
    _fill_geometry(requests, base, headers, inventory, {record["id"] for record in state["items"].values()})
    canvas = _canvas(inventory)
    inventory.update(canvas)
    for key, record in state["items"].items():
        if record["id"] in canvas and key in remote:
            remote[key] = canvas[record["id"]]
    # A creation response is normalized before a durable automatic-parent
    # detach. Its proven parent/location is safe to resume, not snapshot drift.
    for entry in state.get("pending_creation_detaches", {}).values():
        body = inventory.get(entry["id"])
        if (body and (body.get("parent") or {}).get("id") == entry["parent_id"]
                and all(math.isclose(float(body["position"][axis]), entry["position"][axis], abs_tol=.01) for axis in ("x", "y"))):
            body = copy.deepcopy(body)
            body.pop("parent", None)
            inventory[entry["id"]] = body
    geometry = {key: list(_bounds(body, key)) for key, body in canvas.items()}
    boxes = list(geometry.values())
    # Connector bounding geometry, where the API supplies it, contributes to
    # clearance too. Otherwise its attached objects already bound its ends.
    for item_id, body in connectors.items():
        if "position" in body and "geometry" in body:
            try:
                x, y = (float(body["position"][axis]) for axis in ("x", "y"))
                width, height = (float(body["geometry"][axis]) for axis in ("width", "height"))
                angle = math.radians(float(body["geometry"].get("rotation", 0)))
                if not all(math.isfinite(number) for number in (x, y, width, height, angle)) or min(width, height) < 0:
                    raise ValueError
                boxes.append((x, y, abs(width * math.cos(angle)) + abs(height * math.sin(angle)),
                              abs(width * math.sin(angle)) + abs(height * math.cos(angle))))
            except (KeyError, ValueError, TypeError, OverflowError):
                raise TraceError("Cannot verify the bounds of an existing board connector") from None
    bounds = ([min(x - w / 2 for x, y, w, h in boxes), min(y - h / 2 for x, y, w, h in boxes),
               max(x + w / 2 for x, y, w, h in boxes), max(y + h / 2 for x, y, w, h in boxes)] if boxes else None)
    mapped = {key: mapped_record(record) for key, record in state["items"].items()}
    return {"items": {key: _descriptor(body) for key, body in inventory.items()},
            "geometry": geometry, "bounds": bounds, "mapped": mapped}, inventory


def capture(board_id, state_path, namespace, *, token=None, transport=http, interval=.02, workers=4, progress=None):
    from .miro import _load_sync_state, _SyncProgress
    from .miro_http import MiroHTTP
    from .miro_requests import MiroRequests
    from .miro_quota import SharedMiroQuota
    path = Path(state_path)
    token = token or os.getenv("MIRO_ACCESS_TOKEN")
    if not token:
        raise TraceError("Set MIRO_ACCESS_TOKEN to prepare an update from the current board")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock, ExitStack() as resources:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("A Miro sync is active; finish it before preparing the board update") from None
        state = _load_sync_state(path, board_id, namespace)
        if state.get("active_run_id") or any(state.get(key) for key in ("pending_updates", "pending_deletions", "pending_creation_detaches")):
            raise TraceError("Finish the interrupted board sync before preparing another update")
        quota = None
        if transport is http:
            quota = resources.enter_context(SharedMiroQuota(token))
            transport = resources.enter_context(MiroHTTP())
        requests = resources.enter_context(MiroRequests(transport, interval=interval, workers=workers,
            progress=_SyncProgress(progress), quota=quota))
        base = "https://api.miro.com/v2/boards/" + quote(board_id, safe="")
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"}
        snapshot, _ = read_live(requests, base, headers, state)
        return {"version": 1, "mode": "update", "board_id": board_id,
                "namespace": copy.deepcopy(namespace), **snapshot}


def prepare_graph(graph, snapshot, *, connector_style="straight", progress=None, layout_attempts=None):
    from .elk_layout import optimize_graph, _default_attachments
    from .edge_labels import route_signature
    from .context_connectors import display_graph, restore_graph
    canonical_graph = graph
    graph = display_graph(graph)
    result = copy.deepcopy(graph)
    mapped = snapshot["mapped"]
    from .context_group_miro import catalog, _identity
    groups = catalog(graph)
    from .context_parallel_miro import catalog as parallel_catalog
    parallel = parallel_catalog(graph)
    replaced = {key for key, proof in groups.items() if key in mapped
                and mapped[key].get("context_identity_sha256") != _hash(_identity(proof))}
    new_ids = {node["id"] for node in graph["nodes"] if node["id"] not in mapped or node["id"] in replaced}
    new_edges = {}
    if new_ids:
        addition = copy.deepcopy(graph)
        addition["nodes"] = [node for node in addition["nodes"] if node["id"] in new_ids]
        addition["edges"] = [edge for edge in addition["edges"] if edge["source"] in new_ids and edge["target"] in new_ids]
        included = new_ids | {edge["id"] for edge in addition["edges"]}
        if "fee_items" in addition:
            addition["fee_items"] = {key: value for key, value in addition["fee_items"].items() if key in included}
        addition.pop("activity_frames", None)
        from .output_alignment import update_columns
        alignment = update_columns(graph, new_ids)
        if alignment:
            addition.setdefault("layout", {})["output_alignment"] = alignment
        addition = optimize_graph(addition, connector_style=connector_style, progress=progress,
                                  layout_attempts=layout_attempts, validation_graph=graph)
        new_nodes = {node["id"]: node for node in addition["nodes"]}
        left = min(node["x"] - node["width"] / 2 for node in new_nodes.values())
        top = min(node["y"] - node["height"] / 2 for node in new_nodes.values())
        board = snapshot["bounds"]
        dx, dy = ((board[2] + GAP - left, board[1] - top) if board else (-left, -top))
        for node in new_nodes.values():
            node["x"] += dx
            node["y"] += dy
        for edge in addition["edges"]:
            for point in edge.get("route", []):
                point["x"] += dx
                point["y"] += dy
            label = edge.get("label_layout")
            if label:
                label["x"] += dx
                label["y"] += dy
                label["route_signature"] = route_signature([(point["x"], point["y"]) for point in edge.get("route", [])])
            new_edges[edge["id"]] = edge
        from .trace_section_local import refresh_section_bounds
        refresh_section_bounds(addition)
        result["nodes"] = [new_nodes.get(node["id"], node) for node in result["nodes"]]
        result["layout"] = {"algorithm": "board_incremental_v1", "additions_layout": copy.deepcopy(addition.get("layout", {}))}
    else:
        result["layout"] = {"algorithm": "board_incremental_v1", "new_node_count": 0}
    for node in result["nodes"]:
        if node["id"] in mapped and node["id"] not in new_ids:
            record = mapped[node["id"]]
            if record["endpoint"] != "shapes":
                raise TraceError("Board mapping disagrees with graph object types")
            node["x"], node["y"], node["width"], node["height"] = snapshot["geometry"][record["id"]]
    # Existing manual routing cannot be reconstructed from ELK. New boundary
    # links use semantic transaction sides and Miro's own routing when synced.
    nodes = {node["id"]: node for node in result["nodes"]}
    result["edges"] = [new_edges.get(edge["id"], edge) for edge in result["edges"]]
    for edge in result["edges"]:
        if edge["id"] not in new_edges:
            for field in ("route", "attachment", "label_layout", "routing_exception"):
                edge.pop(field, None)
        if "attachment" not in edge:
            attachment = _default_attachments(nodes[edge["source"]], nodes[edge["target"]])
            for field, logical, side in (("startItem", "source", "100%"), ("endItem", "target", "0%")):
                if nodes[edge[logical]]["kind"] == "context_group":
                    attachment[field] = {"position": {"x": side, "y": "50%"}}
            edge["attachment"] = attachment
        edge.setdefault("connector_shape", connector_style)
    result["connector_attachment"] = "transaction_ports_v2"
    result["layout"]["existing_routing_notice"] = (
        "Existing Miro connector bends are not exposed by the board API. The preview approximates those links; "
        "sync preserves their live routing and attachments. New objects are placed outside indexed item bounds.")
    result["board_layout"] = copy.deepcopy(snapshot)
    result["board_layout"]["new_node_ids"] = sorted(new_ids)
    desired_nodes = {node["id"] for node in result["nodes"]}
    desired_edges = {edge["id"] for edge in result["edges"]}
    replaced_edges = {edge["id"] for edge in result["edges"] if edge["id"] in mapped and
                      (edge["source"] in replaced or edge["target"] in replaced or
                       (edge["id"] in parallel and mapped[edge["id"]].get("context_parallel_sha256") != _hash(parallel[edge["id"]])) or
                       any(mapped[edge["id"]].get(field) != edge[field] for field in ("source", "target")))}
    result["board_layout"]["counts"] = {
        "new_nodes": len(new_ids), "retained_nodes": len(desired_nodes) - len(new_ids),
        "removed_nodes": sum(record["endpoint"] == "shapes" and (key not in desired_nodes or key in replaced)
                             and not key.startswith(("legend", "annotation:", "run:")) for key, record in mapped.items()),
        "new_connectors": len(desired_edges - mapped.keys()) + len(replaced_edges),
        "removed_connectors": sum(record["endpoint"] == "connectors" and (key not in desired_edges or key in replaced_edges)
                                  for key, record in mapped.items())}
    return restore_graph(result, canonical_graph)


def finalize_plan(plan):
    """Place new legends/annotations beside additions; preserve every old item."""
    snapshot = plan.get("board_layout")
    if not snapshot:
        return
    mapped, geometry = snapshot["mapped"], snapshot["geometry"]
    new = []
    for item in plan["shapes"]:
        key = item["key"]
        if key in mapped and key not in snapshot.get("new_node_ids", []):
            x, y, _, _ = geometry[mapped[key]["id"]]
            item["body"]["position"].update(x=x, y=y)
        else:
            new.append(item)
    if not new:
        return
    # Legends are constructed from the whole graph and may otherwise overlap
    # preserved objects. Move non-graph new annotations above the new cluster.
    nodes = set(snapshot.get("new_node_ids", []))
    graph_items = [item for item in new if item["key"] in nodes]
    others = [item for item in new if item["key"] not in nodes]
    board = snapshot["bounds"]
    left = board[2] + GAP if board else 0
    top = min((item["body"]["position"]["y"] - item["body"]["geometry"]["height"] / 2 for item in graph_items), default=0)
    for item in sorted(others, key=lambda item: item["key"]):
        width, height = (item["body"]["geometry"][axis] for axis in ("width", "height"))
        top -= GAP + height
        item["body"]["position"].update(x=left + width / 2, y=top + height / 2)


def validate(plan):
    value = plan.get("board_layout")
    if value is None:
        return None
    if (not isinstance(value, dict) or value.get("version") != 1 or value.get("mode") != "update"
            or not isinstance(value.get("board_id"), str) or not value["board_id"]
            or any(not isinstance(value.get(key), dict) for key in ("namespace", "items", "mapped", "geometry"))):
        raise TraceError("Invalid board layout snapshot; regenerate the plot")
    try:
        bounds = value.get("bounds")
        if bounds is not None and (not isinstance(bounds, list) or len(bounds) != 4
                or any(type(number) not in (int, float) or not math.isfinite(number) for number in bounds)
                or bounds[0] > bounds[2] or bounds[1] > bounds[3]):
            raise ValueError
        for item_id, descriptor in value["items"].items():
            if (not isinstance(item_id, str) or not item_id or not isinstance(descriptor, dict)
                    or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in descriptor.values())):
                raise ValueError
        for item_id, geometry in value["geometry"].items():
            if (item_id not in value["items"] or not isinstance(geometry, list) or len(geometry) != 4
                    or any(type(number) not in (int, float) or not math.isfinite(number) for number in geometry)
                    or min(geometry[2:]) <= 0):
                raise ValueError
        ids = []
        for key, record in value["mapped"].items():
            if not isinstance(key, str) or not isinstance(record, dict) or record.get("id") not in value["items"]:
                raise ValueError
            ids.append(record["id"])
            if record.get("endpoint") not in ("shapes", "connectors", "frames"):
                raise ValueError
            if record["endpoint"] in ("shapes", "frames") and record["id"] not in value["geometry"]:
                raise ValueError
        if len(ids) != len(set(ids)):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise TraceError("Invalid board geometry snapshot; regenerate the update layout") from None
    return value


def check_live(plan, state, live):
    value = validate(plan)
    if value is None:
        return
    if state["board_id"] != value["board_id"] or state["namespace"] != value["namespace"]:
        raise TraceError("This update layout belongs to a different Miro board")
    journal = state.get("board_layout_sync", {})
    same_sync = journal.get("plan_sha256") == plan["sha256"]
    mapped = {key: mapped_record(record) for key, record in state["items"].items()}
    expected_mapping = journal.get("mapped") if same_sync else value["mapped"]
    if mapped != expected_mapping:
        raise TraceError("The board item mapping changed after this layout was prepared; regenerate the update layout. No board writes made.")
    expected = journal.get("items") if journal.get("plan_sha256") == plan["sha256"] else value["items"]
    allowed = journal.get("pending_updates", {}) if journal.get("plan_sha256") == plan["sha256"] else {}
    missing = set(expected) - live["items"].keys()
    attempted = {entry["id"] for entry in state.get("pending_deletions", {}).values() if entry.get("attempted")}
    if (missing - attempted or set(live["items"]) - expected.keys()
            or any(actual != expected.get(key) and actual != allowed.get(key) for key, actual in live["items"].items())):
        raise TraceError("The Miro board changed after this update layout was prepared. Regenerate the update layout before syncing. No board writes made.")


def checkpoint(plan, live, remote, state, updates):
    """Freeze expected post-PATCH signatures before sending any mutations."""
    if not plan.get("board_layout"):
        return None
    pending = {}
    for key, patch, _, _ in updates:
        if patch:
            body = copy.deepcopy(remote[key])
            for field, value in patch.items():
                if isinstance(value, dict):
                    body.setdefault(field, {}).update(copy.deepcopy(value))
                else:
                    body[field] = copy.deepcopy(value)
            pending[state["items"][key]["id"]] = _descriptor(body)
    return {"plan_sha256": plan["sha256"], "items": copy.deepcopy(live["items"]),
            "mapped": copy.deepcopy(live["mapped"]), "pending_updates": pending}
