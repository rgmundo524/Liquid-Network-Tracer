import copy
import fcntl
import html
import json
import math
import os
import re
import statistics
import time
import urllib.parse
from contextlib import ExitStack
from pathlib import Path

from .api import http
from .common import TraceError, canonical, digest, now
from .export import COLORS, edge_color, legend_lines
from .miro_http import MiroHTTP
from .miro_requests import MiroRequestNotSent, MiroRequests
from .miro_state import SyncState, load_state
from .miro_reads import check_empty_frames, preflight, validate_frame_children
from .miro_quota import SharedMiroQuota
from .miro_frames import frame_bodies, validate_activity_frames


def make_plan(graph):
    shapes, connectors = [], []
    incremental = "namespace" in graph
    title = ("SYNTHETIC DEMO · " if graph["simulated"] else "") + "Liquid UTXO trace"
    if not incremental:
        title += " · " + graph["run_id"]
    shapes.append({"key": "legend", "body": {"data": {"shape": "rectangle", "content":
        "<p><strong>" + html.escape(title) + "</strong></p><p>"
        + "<br>".join(html.escape(line) for line in legend_lines())
        + "</p><p>" + html.escape(graph["notice"]) + "</p>"},
        "position": {"x": 700, "y": -160, "origin": "center"}, "geometry": {"width": 1300, "height": 260},
        "style": {"fillColor": COLORS["address"], "fontSize": "16", "textAlign": "left"}}})
    if incremental:
        details = graph.get("run", {})
        lines = ["Run: " + graph["run_id"]]
        for key in ("started_at", "finished_at", "status", "parent_run", "stop_reason", "max_hops", "seeds", "limits", "stats"):
            if key in details:
                value = details[key]
                if key == "seeds" and isinstance(value, list):
                    transactions = {str(seed).rpartition(":")[0] for seed in value}
                    lines.append(f"Starting outputs: {len(value)} across {len(transactions)} transactions")
                    continue
                lines.append(key.replace("_", " ") + ": " + (json.dumps(value, ensure_ascii=False)
                             if isinstance(value, (dict, list)) else str(value)))
        shapes.append({"key": "run:" + graph["run_id"], "body": {
            "data": {"shape": "rectangle", "content": "<p>" + "<br>".join(html.escape(s) for s in lines) + "</p>"},
            "position": {"x": 700, "y": -480, "origin": "center"},
            "geometry": {"width": 1300, "height": 280},
            "style": {"fillColor": "#e0f2fe", "fontSize": "14", "textAlign": "left"}}})
    for node in graph["nodes"]:
        content = "<p>" + "<br>".join(html.escape(line) for line in node["label"].splitlines()) + "</p>"
        if node.get("url"):
            content += '<p><a href="' + html.escape(node["url"], quote=True) + '">Explorer</a></p>'
        shapes.append({"key": node["id"], "body": {
            "data": {"shape": {"address": "circle", "transaction": "rectangle", "event": "rhombus"}[node["kind"]], "content": content},
            "position": {"x": node["x"], "y": node["y"], "origin": "center"},
            "geometry": {"width": node["width"], "height": node["height"]},
            "style": {"fillColor": node["color"], "fillOpacity": "1", "borderColor": "#334155", "borderWidth": "2",
                      "fontSize": "12", "textAlign": "center", "textAlignVertical": "middle"}}})
    transaction_keys = {node["id"] for node in graph["nodes"] if node["kind"] == "transaction"}
    for edge in graph["edges"]:
        connector = {"key": edge["id"], "source": edge["source"], "target": edge["target"], "body": {
            "shape": edge.get("connector_shape", "curved"), "captions": [{"content": html.escape(edge["label"] + " · " + edge["quantity"]), "position": "50%"}],
            "style": {"startStrokeCap": "none", "endStrokeCap": "stealth", "strokeStyle": "normal",
                      "strokeColor": edge_color(edge["role"]),
                      "strokeWidth": "2", "fontSize": "11"}}}
        if graph.get("connector_attachment") == "transaction_sides_v1":
            connector["attachment"] = {}
            for field, logical, side in (("startItem", "source", "right"), ("endItem", "target", "left")):
                if edge[logical] in transaction_keys:
                    connector["attachment"][field] = {"snapTo": side}
        elif graph.get("connector_attachment") == "transaction_ports_v2":
            connector["attachment"] = copy.deepcopy(edge.get("attachment"))
        connectors.append(connector)
    if graph.get("layout"):
        # The graph supplies its actual top bound, including the optional fee row.
        top = min((node["y"] - node["height"] / 2 for node in graph["nodes"]), default=0)
        shapes[0]["body"]["position"]["y"] = top - 230
        if incremental:
            shapes[1]["body"]["position"]["y"] = top - 550
        annotations = graph["layout"].get("annotations", {})
        note_shapes = [(shapes[0], "legend")] + ([(shapes[1], "run")] if incremental else [])
        for shape, name in note_shapes:
            if name in annotations:
                shape["body"]["position"].update({field: annotations[name][field] for field in ("x", "y")})
    plan = {"schema_version": 2 if incremental else 1, "run_id": graph["run_id"], "shapes": shapes, "connectors": connectors}
    for key in ("layout", "fee_items", "include_fees", "connector_attachment", "graph_options"):
        if key in graph:
            plan[key] = copy.deepcopy(graph[key])
    if "fee_items" in graph:
        plan["include_fees"] = graph.get("graph_options", {}).get("include_fees", graph.get("include_fees", False))
    if "presentation_version" in graph:
        plan["presentation_version"] = graph["presentation_version"]
    if incremental:
        plan["namespace"] = copy.deepcopy(graph["namespace"])
        plan["run"] = copy.deepcopy(graph.get("run", {}))
    if "activity_frames" in graph:
        plan["activity_frames"] = copy.deepcopy(graph["activity_frames"])
        plan["frames"] = frame_bodies(plan["activity_frames"], {
            item["key"]: _bounds(item["body"], item["key"]) for item in shapes})
    plan["sha256"] = digest(canonical(plan))
    return plan


def validate_plan(plan):
    if not isinstance(plan, dict) or plan.get("schema_version") not in (1, 2):
        raise TraceError("Unsupported Miro plan schema; regenerate the export")
    copy = {k: v for k, v in plan.items() if k != "sha256"}
    if digest(canonical(copy)) != plan.get("sha256"):
        raise TraceError("Miro plan checksum mismatch; regenerate the export")
    try:
        if not isinstance(plan["run_id"], str) or not plan["run_id"]:
            raise ValueError
        if not all(isinstance(plan[k], list) for k in ("shapes", "connectors")) or not isinstance(plan.get("frames", []), list):
            raise ValueError
        keys = [item["key"] for item in plan["shapes"] + plan["connectors"] + plan.get("frames", [])]
        if any(not isinstance(k, str) or not k for k in keys):
            raise ValueError
        for item in plan["shapes"] + plan["connectors"] + plan.get("frames", []):
            if not isinstance(item["body"], dict):
                raise ValueError
        if plan["schema_version"] == 2:
            if not isinstance(plan.get("run", {}), dict):
                raise ValueError
            ancestors = plan.get("run", {}).get("ancestor_runs", [])
            if not isinstance(ancestors, list) or any(not isinstance(s, str) or not s for s in ancestors):
                raise ValueError
        for item in plan["shapes"]:
            body = item["body"]
            if not isinstance(body["data"]["content"], str) or not body["data"]["shape"]:
                raise ValueError
            for field in ("x", "y"):
                if not math.isfinite(float(body["position"][field])):
                    raise ValueError
            for field in ("width", "height"):
                if not math.isfinite(float(body["geometry"][field])) or float(body["geometry"][field]) <= 0:
                    raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        raise TraceError("Malformed Miro plan; regenerate the export") from None
    if len(keys) != len(set(keys)):
        raise TraceError("Miro plan has duplicate item keys")
    shape_keys = {item["key"] for item in plan["shapes"]}
    for item in plan["connectors"]:
        if item.get("source") not in shape_keys or item.get("target") not in shape_keys or item["source"] == item["target"]:
            raise TraceError("Invalid Miro connector endpoints")
    _validate_attachments(plan)
    _fee_catalog(plan)
    if "activity_frames" in plan:
        seeds = plan.get("run", {}).get("seeds")
        starts = ({"tx:" + seed.rpartition(":")[0] for seed in seeds
                   if isinstance(seed, str) and ":" in seed} & shape_keys) if isinstance(seeds, list) else None
        validate_activity_frames(plan["activity_frames"], shape_keys, plan["connectors"],
                                 starting_transaction_keys=starts)
        expected = frame_bodies(plan["activity_frames"], {
            item["key"]: _bounds(item["body"], item["key"]) for item in plan["shapes"]})
        if plan.get("frames") != expected:
            raise TraceError("Miro frame geometry disagrees with the graph; regenerate the export")
    elif "frames" in plan:
        raise TraceError("Miro frames need activity metadata; regenerate the export")


def _validate_attachments(plan):
    policy = plan.get("connector_attachment")
    if policy not in (None, "transaction_sides_v1", "transaction_ports_v2"):
        raise TraceError("Unsupported Miro connector attachment policy; regenerate the export")
    transactions = {item["key"] for item in plan["shapes"]
                    if item["key"].startswith("tx:") and item["body"]["data"]["shape"] == "rectangle"}
    shapes = {item["key"]: item["body"]["data"]["shape"] for item in plan["shapes"]}
    for item in plan["connectors"]:
        if policy == "transaction_ports_v2":
            _validate_ports(item, shapes, transactions)
            continue
        if policy is None:
            if "attachment" in item:
                raise TraceError("Miro connector attachment needs a declared policy; regenerate the export")
            continue
        expected = {field: {"snapTo": side}
                    for field, logical, side in (("startItem", "source", "right"), ("endItem", "target", "left"))
                    if item[logical] in transactions}
        if item.get("attachment") != expected:
            raise TraceError("Miro transaction connector sides disagree with their endpoints; regenerate the export")


def _percentage(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+)%", value):
        raise ValueError
    number = float(value[:-1])
    if not math.isfinite(number) or not 0 <= number <= 100:
        raise ValueError
    return number


def _validate_ports(item, shapes, transactions):
    """Only layout-relative perimeter coordinates belong in a portable plan."""
    try:
        if item["body"].get("shape") not in ("straight", "curved", "elbowed"):
            raise ValueError
        if any(field in item["body"] for field in ("startItem", "endItem")):
            raise ValueError
        if (item["source"] in transactions) == (item["target"] in transactions):
            raise ValueError
        attachments = item["attachment"]
        if not isinstance(attachments, dict) or set(attachments) != {"startItem", "endItem"}:
            raise ValueError
        for field, logical, side in (("startItem", "source", 100), ("endItem", "target", 0)):
            connection = attachments[field]
            if not isinstance(connection, dict) or set(connection) != {"position"}:
                raise ValueError
            position = connection["position"]
            if not isinstance(position, dict) or set(position) != {"x", "y"}:
                raise ValueError
            x, y = (_percentage(position[axis]) for axis in ("x", "y"))
            if item[logical] in transactions:
                if x != side:
                    raise ValueError
            elif shapes[item[logical]] == "circle":
                if abs(((x - 50) / 50) ** 2 + ((y - 50) / 50) ** 2 - 1) > .002:
                    raise ValueError
            elif shapes[item[logical]] == "rhombus":
                if abs(abs(x - 50) + abs(y - 50) - 50) > .05:
                    raise ValueError
            else:
                raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        raise TraceError("Invalid Miro layout ports or connector topology; regenerate the export") from None


def _connection_body(item, source_id, target_id):
    """Add remote IDs only at the API boundary; plans never choose remote targets.

    Miro REST accepts exactly one of snapTo or position on an attachment.
    Legacy snapTo selects a side midpoint. ELK plans supply explicit percentage
    positions for both endpoints, including ports along transaction sides.
    https://developers.miro.com/reference/create-connector-1
    https://github.com/miroapp/api-clients/blob/main/packages/miro-api/model/itemConnectionCreationData.ts
    """
    return {field: {"id": item_id, **item.get("attachment", {}).get(field, {"snapTo": "auto"})}
            for field, item_id in (("startItem", source_id), ("endItem", target_id))}


def _attachment_patch(item, remote):
    # GET commonly reports a percentage position without its snapTo setting.
    # Even after we applied a fixed side, a manual reset to auto can coincide
    # with that midpoint. Explicit organization reasserts ambiguous settings;
    # ordinary sync never calls this helper or changes attachment routing.
    def same_attachment(actual, desired):
        if "snapTo" in desired:
            return actual.get("snapTo") == desired["snapTo"]
        # Percentage coordinates are returned for both fixed and automatic
        # attachment modes. Even matching percentages cannot prove the port is
        # fixed. Explicit reorganization reasserts the requested mode; ordinary
        # sync never reaches this helper.
        return False

    return {field: {"id": remote[field]["id"], **desired}
            for field, desired in item.get("attachment", {}).items()
            if not same_attachment(remote[field], desired)}


def _namespace(plan):
    if plan.get("schema_version") != 2:
        raise TraceError("Incremental Miro sync requires a schema 2 export; export this run anew or use legacy miro-publish")
    namespace = plan.get("namespace")
    if (not isinstance(namespace, dict) or set(namespace) != {"case_id", "source", "address_mode"}
            or any(not isinstance(v, str) or not v.strip() for v in namespace.values())):
        raise TraceError("Miro plan needs a case_id, source, and address_mode namespace; export anew")
    if namespace["address_mode"] not in ("merged", "outpoint_occurrences"):
        raise TraceError("Invalid Miro address mode")
    return namespace


def _load_sync_state(path, board_id, namespace):
    state = load_state(path, {
        "schema_version": 2, "board_id": board_id, "namespace": copy.deepcopy(namespace),
        "items": {}, "runs": {}, "pending": None, "pending_creations": {}})
    if state.get("schema_version") != 2:
        raise TraceError("This is legacy Miro snapshot state; keep it intact and use a separate sync state file")
    if state.get("board_id") != board_id or state.get("namespace") != namespace:
        raise TraceError("Miro state belongs to a different board, case, API source, or address mode; use the matching export and state")
    if not isinstance(state.get("items"), dict) or not isinstance(state.get("runs"), dict):
        raise TraceError("Malformed Miro sync state; restore its last intact version")
    ids = []
    for key, record in state["items"].items():
        if (not isinstance(key, str) or not isinstance(record, dict)
                or not isinstance(record.get("id"), str) or not record["id"]
                or record.get("endpoint") not in ("shapes", "connectors", "frames")
                or not isinstance(record.get("managed"), dict) or not isinstance(record.get("intent"), dict)):
            raise TraceError("Malformed Miro item mapping; restore its last intact version")
        ids.append(record["id"])
        if record["endpoint"] == "frames" and record.get("frame_proof") != _frame_proof(key):
            raise TraceError("Malformed managed Miro frame mapping; restore its last intact version")
    if len(ids) != len(set(ids)):
        raise TraceError("Miro state maps different objects to the same remote ID; repair the mapping")
    pending_updates = state.get("pending_updates", {})
    if not isinstance(pending_updates, dict):
        raise TraceError("Malformed Miro update journal; restore its last intact version")
    for key, entry in pending_updates.items():
        record = state["items"].get(key)
        if (not isinstance(entry, dict) or record is None
                or entry.get("id") != record["id"] or entry.get("endpoint") != record["endpoint"]
                or not isinstance(entry.get("patch"), dict)):
            raise TraceError("Malformed Miro update journal; restore its last intact version")
    pending_creations = state.get("pending_creations", {})
    if not isinstance(pending_creations, dict):
        raise TraceError("Malformed Miro creation journal; restore its last intact version")
    for key, entry in pending_creations.items():
        if (not isinstance(key, str) or not isinstance(entry, dict) or entry.get("key") != key
                or entry.get("endpoint") not in ("shapes", "connectors", "frames")
                or not isinstance(entry.get("body"), dict) or key in state["items"]):
            raise TraceError("Malformed Miro creation journal; restore its last intact version")
        if entry["endpoint"] == "frames" and entry.get("frame_proof") != _frame_proof(key):
            raise TraceError("Malformed pending Miro frame; restore its last intact version")
    unresolved = list(pending_creations)
    if state.get("pending"):
        unresolved.append(str(state["pending"].get("key")))
    if unresolved:
        raise TraceError("Prior Miro POST outcome is uncertain for " + ", ".join(unresolved) +
                         "; inspect the board and use miro-resolve --key for each item before retrying")
    return state


def _recover_updates(state, remote):
    """Adopt observed successful fields, without replaying an uncertain PATCH.

    A previous value remains eligible for the normal merge. A third value is
    treated as a manual edit. Layout is always recalculated from live positions;
    only an explicit reorganization may replace those positions again.
    """
    for key, entry in state.get("pending_updates", {}).items():
        if key not in remote:
            continue
        record = state["items"][key]
        actual = _editable(remote[key], record["endpoint"])
        for path, desired in _fields(_editable(entry["patch"], record["endpoint"])):
            current = _get(actual, path)
            if _same(current, desired, path):
                _set(record["managed"], path, current)
                _set(record["intent"], path, desired)


def _editable(body, endpoint):
    """Fields ordinary sync may update. Explicit organization handles layout separately."""
    result = {}
    if endpoint == "shapes" and "content" in body.get("data", {}):
        result["data"] = {"content": copy.deepcopy(body["data"]["content"])}
    if endpoint == "frames" and "title" in body.get("data", {}):
        result["data"] = {"title": copy.deepcopy(body["data"]["title"])}
    if "style" in body:
        result["style"] = copy.deepcopy(body["style"])
    if endpoint == "connectors" and "captions" in body:
        result["captions"] = [{k: copy.deepcopy(c[k]) for k in ("content", "position") if k in c}
                              for c in body["captions"]]
    return result


def _check_lineage(plan, state):
    active = state.get("active_run_id")
    latest = state.get("latest_run_id")
    if active and active != plan["run_id"]:
        raise TraceError("Finish or reconcile the interrupted Miro sync for run " + active + " before syncing a different run")
    if latest and latest != plan["run_id"]:
        details = plan.get("run", {})
        ancestors = details.get("ancestor_runs", [])
        if latest not in ancestors and details.get("parent_run") != latest:
            raise TraceError("Miro sync would replay an older or independent branch over run " + latest +
                             ". Continue the latest synced run (unpublished intermediate continuations are allowed), or use a new case/board state.")


def _fields(body):
    for group, value in body.items():
        if group in ("data", "style"):
            for key, leaf in value.items():
                yield (group, key), leaf
        else:
            yield (group,), value


_MISSING = object()


def _get(body, path):
    value = body
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return _MISSING
        value = value[key]
    return value


def _set(body, path, value):
    current = body
    for key in path[:-1]:
        current = current.setdefault(key, {})
    current[path[-1]] = copy.deepcopy(value)


def _same(a, b, path=()):
    # REST sometimes serializes numeric styles as numbers instead of strings.
    if path and path[-1] in ("fontSize", "borderWidth", "borderOpacity", "fillOpacity", "strokeWidth"):
        try:
            return float(a) == float(b)
        except (TypeError, ValueError):
            pass
    if path and path[-1].endswith("Color") and isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    return a == b


def _baseline(intent, remote, endpoint):
    """Remember the server representation of only the fields we supplied."""
    result = copy.deepcopy(intent)
    actual = _editable(remote, endpoint)
    for path, _ in _fields(intent):
        value = _get(actual, path)
        if value is not _MISSING:
            _set(result, path, value)
    return result


def _merge_fields(record, body, remote, key):
    desired = _editable(body, record["endpoint"])
    actual = _editable(remote, record["endpoint"])
    managed = copy.deepcopy(record["managed"])
    intent = copy.deepcopy(record["intent"])
    patch, conflicts = {}, []
    for path, value in _fields(desired):
        current, previous = _get(actual, path), _get(managed, path)
        old_intent = _get(intent, path)
        if _same(current, value, path):
            _set(managed, path, current)
            _set(intent, path, value)
        elif _same(current, previous, path) and previous is not _MISSING:
            if not _same(value, old_intent, path):
                _set(patch, path, value)
        else:
            conflicts.append({"key": key, "item_id": record["id"], "field": ".".join(path),
                              "reason": "Remote field differs from last managed value; manual edit preserved"})
    return patch, managed, intent, conflicts


class _SyncProgress:
    """Best-effort status only; reporting must never interrupt a saved mutation."""

    def __init__(self, callback):
        self.callback = callback
        self.current = {"phase": "preflight", "completed": 0, "total": 0,
                        "message": "Checking existing board items"}

    def emit(self, phase, completed, total, message):
        self.current = {"phase": phase, "completed": completed, "total": total, "message": message}
        self._send(self.current)

    def _send(self, event):
        if self.callback is not None:
            try:
                self.callback(dict(event))
            except Exception:
                # Progress is advisory, unlike the durable sync journal. A
                # closed terminal or failed progress file must not lose an
                # acknowledged remote ID or turn a POST into an uncertain one.
                pass

    def pause(self, delay, message, reason="rate_limit"):
        self._send({**self.current, "phase": "waiting", "message": message, "retry_after": delay,
                    "reason": reason})
        time.sleep(delay)
        self._send(self.current)


def _retry_delay(headers):
    values = {k.lower(): v for k, v in headers.items()}
    retry = values.get("retry-after")
    try:
        delay = float(retry) if retry is not None else float(values["x-ratelimit-reset"]) - time.time()
    except (KeyError, TypeError, ValueError):
        delay = 2.
    if not math.isfinite(delay) or delay > 30:
        raise TraceError("Miro rate limited for more than 30 seconds; wait for the limit to reset and rerun sync")
    return max(1., delay)


def _request(transport, method, url, headers, body=None, interval=0, progress=None):
    """Bounded safe retries. POST uncertainty is handled separately by sync."""
    for attempt in range(4):
        status, response_headers, raw = transport(method, url, headers, canonical(body) if body is not None else None, 30)
        if status == 429 and attempt < 3:
            delay = _retry_delay(response_headers)
            if progress is not None:
                progress.pause(delay, "Waiting for the Miro rate limit to reset")
            else:
                time.sleep(delay)
            continue
        if method == "GET" and status >= 500 and attempt < 3:
            delay = min(2 ** attempt, 4)
            if progress is not None:
                progress.pause(delay, "Waiting before retrying a Miro read", "server_retry")
            else:
                time.sleep(delay)
            continue
        time.sleep(max(0., interval))
        return status, response_headers, raw


def _response(raw, operation):
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (ValueError, TypeError):
        raise TraceError("Miro returned invalid JSON for " + operation) from None


def _remote_url(base, record):
    return base + "/" + record["endpoint"] + "/" + urllib.parse.quote(record["id"], safe="")


def _fee_catalog(plan):
    if "fee_items" not in plan:
        return {}
    catalog = plan["fee_items"]
    if not isinstance(catalog, dict) or type(plan.get("include_fees")) is not bool:
        raise TraceError("Malformed Miro fee metadata; regenerate the export")
    shapes = {item["key"]: item for item in plan["shapes"]}
    connectors = {item["key"]: item for item in plan["connectors"]}
    for key, proof in catalog.items():
        if not isinstance(key, str) or not isinstance(proof, dict):
            raise TraceError("Malformed Miro fee metadata; regenerate the export")
        if proof.get("endpoint") == "shapes":
            txid, vout = proof.get("txid"), proof.get("vout")
            if (not isinstance(txid, str) or not re.fullmatch(r"[0-9a-f]{64}", txid)
                    or type(vout) is not int or not 0 <= vout <= 0xffffffff
                    or key != f"event:{txid}:{vout}"):
                raise TraceError("Invalid Miro fee shape proof; regenerate the export")
            if plan["include_fees"] != (key in shapes):
                raise TraceError("Miro fee visibility disagrees with the planned shapes; regenerate the export")
            if key in shapes and shapes[key]["body"]["data"]["shape"] != "rhombus":
                raise TraceError("Miro fee shape must be an event; regenerate the export")
        elif proof.get("endpoint") == "connectors":
            shape = catalog.get(proof.get("target")) if isinstance(proof.get("target"), str) else None
            if not shape or shape.get("endpoint") != "shapes":
                raise TraceError("Invalid Miro fee connector proof; regenerate the export")
            txid, vout = shape.get("txid"), shape.get("vout")
            if key != f"out:{txid}:{vout}" or proof.get("source") not in ("tx:" + str(txid), "source:" + str(txid)):
                raise TraceError("Invalid Miro fee connector endpoints; regenerate the export")
            if plan["include_fees"] != (key in connectors):
                raise TraceError("Miro fee visibility disagrees with the planned connectors; regenerate the export")
            if key in connectors and any(connectors[key][field] != proof[field] for field in ("source", "target")):
                raise TraceError("Miro fee connector endpoints disagree; regenerate the export")
        else:
            raise TraceError("Invalid Miro fee item type; regenerate the export")
    return catalog


def _fee_removals(plan, state):
    catalog = _fee_catalog(plan)
    removals = {}
    if catalog and not plan["include_fees"]:
        for key, proof in catalog.items():
            record = state["items"].get(key)
            if record is None:
                continue
            if record["endpoint"] != proof["endpoint"] or (record["endpoint"] == "connectors" and any(
                    record.get(field) != proof.get(field) for field in ("source", "target"))):
                raise TraceError("Miro fee proof disagrees with the saved mapping; repair the mapping before syncing")
            if record.get("fee_proof") is not None and record["fee_proof"] != proof:
                raise TraceError("Miro fee proof disagrees with the created item; repair the mapping before syncing")
            if record["endpoint"] == "shapes" and not re.match(r"^<p>FEE(?:<br\s*/?>|</p>)",
                                                               record["intent"].get("data", {}).get("content", "")):
                raise TraceError("Mapped event was not generated as a fee; refusing to remove a non-fee item")
            removals[key] = proof
    pending = state.get("pending_deletions", {})
    if not isinstance(pending, dict):
        raise TraceError("Malformed pending Miro deletions; restore the sync state")
    for key, entry in pending.items():
        record = state["items"].get(key)
        if (key not in removals or not isinstance(entry, dict) or not record
                or entry.get("id") != record["id"] or entry.get("proof") != removals[key]
                or type(entry.get("attempted")) is not bool):
            raise TraceError("Finish the interrupted Miro fee removal with fees excluded before changing visibility or plans")
    return removals


def _check_fee_removals(state, remote, removals):
    for key in removals:
        record = state["items"][key]
        if key not in remote:  # Only an attempted pending DELETE can reach here.
            continue
        actual = _editable(remote[key], record["endpoint"])
        for path, previous in _fields(record["managed"]):
            if not _same(_get(actual, path), previous, path):
                raise TraceError("Fee item " + key + " has manual edits. Keep fees included, or copy those notes elsewhere and restore the generated fee item before hiding fees. No board writes made.")
        if record["endpoint"] == "shapes" and remote[key].get("data", {}).get("shape") != "rhombus":
            raise TraceError("Fee shape type was changed; restore the generated fee shape before hiding fees. No board writes made.")
    for key, record in state["items"].items():
        if record["endpoint"] != "connectors":
            continue
        if key not in removals and (record.get("source") in removals or record.get("target") in removals):
            raise TraceError("A non-fee mapped connector is attached to a fee shape; keep fees included or repair that connection before hiding fees. No board writes made.")
        if key in remote:
            for field, logical in (("startItem", "source"), ("endItem", "target")):
                expected = state["items"].get(record[logical], {}).get("id")
                if remote[key].get(field, {}).get("id") != expected:
                    raise TraceError("Miro connector endpoints were changed for " + key + "; restore or repair this connection before syncing. No board writes made.")


def _frame_proof(key):
    """A dedicated marker separates generated frames from other mapped items."""
    if key == "frame:graph":
        kind = "graph"
    elif isinstance(key, str) and re.fullmatch(r"frame:activity:[0-9a-f]{64}", key):
        kind = "activity"
    else:
        raise TraceError("Invalid managed Miro frame key; restore the sync state")
    return {"schema_version": 1, "key": key, "kind": kind}


def _frame_removals(plan, state):
    desired = {item["key"] for item in plan.get("frames", [])}
    removals = {}
    if "activity_frames" in plan:
        for key, record in state["items"].items():
            if record["endpoint"] == "frames" and key not in desired:
                proof = _frame_proof(key)
                if record.get("frame_proof") != proof:
                    raise TraceError("Miro frame proof disagrees with its saved mapping; no board writes made")
                removals[key] = proof
    pending = state.get("pending_frame_deletions", {})
    if not isinstance(pending, dict):
        raise TraceError("Malformed pending Miro frame deletions; restore the sync state")
    for key, entry in pending.items():
        record = state["items"].get(key)
        if (key not in removals or not isinstance(entry, dict) or not record
                or entry.get("id") != record["id"] or entry.get("proof") != removals[key]
                or type(entry.get("attempted")) is not bool):
            raise TraceError("Finish the interrupted Miro frame update with the same graph before changing plans")
    return removals


def _live_frames(plan, state, remote, positions, removals):
    """Fit frames to the geometry that this sync will actually leave on canvas.

    Older run notes remain included in the full-graph export frame. Retained
    shapes contribute their live sizes and rotations, including manual edits.
    Frames are presentation containers, so their bounds are always refreshed.
    """
    if "activity_frames" not in plan:
        return []
    bounds = {key: _bounds(remote[key], key) for key, record in state["items"].items()
              if record["endpoint"] == "shapes" and key not in removals}
    for item in plan["shapes"]:
        key = item["key"]
        value = bounds.get(key) or _bounds(item["body"], key)
        bounds[key] = (*positions[key], value[2], value[3]) if key in positions else value
    return frame_bodies(plan["activity_frames"], bounds)


def _bounds(body, key):
    position = body.get("position", {})
    if (position.get("relativeTo") not in (None, "canvas_center")
            or position.get("origin") not in (None, "center")
            or ((body.get("parent") or {}).get("id") and position.get("relativeTo") != "canvas_center")):
        raise TraceError("Miro shape " + key + " has frame/group-relative coordinates. Move mapped shapes to the board canvas before syncing; no board writes made.")
    try:
        x, y = (float(position[field]) for field in ("x", "y"))
        width, height = (float(body["geometry"][field]) for field in ("width", "height"))
        rotation = float(body["geometry"].get("rotation", 0))
        if not all(math.isfinite(v) for v in (x, y, width, height, rotation)) or min(width, height) <= 0:
            raise ValueError
        angle = math.radians(rotation)
        return (x, y, abs(width * math.cos(angle)) + abs(height * math.sin(angle)),
                abs(height * math.cos(angle)) + abs(width * math.sin(angle)))
    except (KeyError, TypeError, ValueError, OverflowError):
        raise TraceError("Cannot read current Miro geometry for " + key + "; no board writes made") from None


def _overlap(a, b, gap=60):
    return abs(a[0] - b[0]) < (a[2] + b[2]) / 2 + gap and abs(a[1] - b[1]) < (a[3] + b[3]) / 2 + gap


def _complete_layout(plan):
    """Recognize complete layouts without changing legacy dependency plans.

    Earlier dependency-layer archives rely on the column placement below. The
    explicit marker distinguishes the full, scalable fallback presentation from
    those archives; both it and ELK already supply positions for every shape.
    """
    layout = plan.get("layout", {})
    return (layout.get("algorithm") == "elk_layered_v1"
            or (layout.get("algorithm") == "dependency_layers_v1"
                and layout.get("placement") == "complete_graph_v1"
                and layout.get("fallback_reason") in ("size_limit", "timeout", "mermaid_size_limit", "mermaid_timeout")))


def _placements(plan, state, remote, removed, reorganize):
    """Place new connected groups near their existing anchors, preserving old items.

    Only managed objects are known. Cyclic/backward links are allowed; forward
    links in the desired layout constrain new groups between existing anchors.
    """
    planned = {item["key"]: _bounds(item["body"], item["key"]) for item in plan["shapes"]}
    existing = {key: _bounds(remote[key], key) for key, record in state["items"].items()
                if record["endpoint"] == "shapes" and key not in removed}
    new = set(planned) - set(existing)
    result = {}
    complete_layout = _complete_layout(plan)
    if complete_layout and not existing:
        # A first sync has nothing to preserve or avoid. In particular, do not
        # repack thousands of disconnected components against each other.
        return {key: (value[0], value[1]) for key, value in planned.items()}, 0
    if not plan.get("layout") and not reorganize:
        shift = max(0., max((v[0] + v[2] / 2 for v in existing.values()), default=-math.inf) + 300
                    - min((planned[k][0] - planned[k][2] / 2 for k in new), default=math.inf))
        return {key: (planned[key][0] + shift, planned[key][1]) for key in new}, shift
    occupied = {key: value for key, value in existing.items() if not reorganize or key not in planned}
    targets = set(planned) if reorganize else new
    fee_keys = {key for key, proof in plan.get("fee_items", {}).items() if proof["endpoint"] == "shapes" and key in planned}
    adjacency = {key: set() for key in planned}
    for edge in plan["connectors"]:
        adjacency[edge["source"]].add(edge["target"])
        adjacency[edge["target"]].add(edge["source"])

    def effective(key, x, y):
        dimensions = existing.get(key, planned[key])
        return (x, y, dimensions[2], dimensions[3])

    def place_group(group, dx, dy, upward=False):
        # Shift the whole connected addition so its internal ordering stays intact.
        for _ in range(len(occupied) + 2):
            hits = [(effective(key, planned[key][0] + dx, planned[key][1] + dy), other)
                    for key in group for other in occupied.values()
                    if _overlap(effective(key, planned[key][0] + dx, planned[key][1] + dy), other)]
            if not hits:
                break
            if upward:
                dy -= max(a[1] + a[3] / 2 - (b[1] - b[3] / 2) + 61 for a, b in hits)
            else:
                dy += max(b[1] + b[3] / 2 - (a[1] - a[3] / 2) + 61 for a, b in hits)
        else:
            raise TraceError("Cannot place the new Miro items without overlap; reorganize the graph or move nearby managed items before syncing")
        for key in group:
            x, y = planned[key][0] + dx, planned[key][1] + dy
            result[key] = (x, y)
            occupied[key] = effective(key, x, y)

    def place_fees():
        keys = sorted(targets & fee_keys, key=lambda key: (planned[key][0], key))
        if not keys:
            return
        mapped_fees = sorted(fee_keys & set(existing), key=lambda key: (planned[key][0], key)) if not reorganize else []
        dy = statistics.median(existing[key][1] - planned[key][1] for key in mapped_fees) if mapped_fees else 0
        # Keep all newly placed fees on one chronological row. Existing fees
        # retain manual positions during ordinary sync; explicit reorganization
        # is needed when earlier historical fees are inserted into a tight row.
        previous_right = -math.inf
        for key in keys:
            x, y, width, height = effective(key, planned[key][0], planned[key][1])
            earlier = [other for other in mapped_fees if planned[other][0] < planned[key][0]]
            later = [other for other in mapped_fees if planned[other][0] > planned[key][0]]
            lower = max([previous_right + 100 + width / 2] + [existing[other][0] + existing[other][2] / 2 + 100 + width / 2 for other in earlier])
            upper = min([math.inf] + [existing[other][0] - existing[other][2] / 2 - 100 - width / 2 for other in later])
            if lower > upper:
                raise TraceError("The existing fee row needs space for earlier fees; choose Reorganize graph before syncing")
            x = max(lower, min(x, upper))
            planned[key] = (x, y, width, height)
            previous_right = x + width / 2
        place_group(keys, 0, dy, upward=True)

    if reorganize and complete_layout:
        # Layout layer members can have different center x coordinates because
        # their widths differ. Repacking every distinct center as a column
        # destroys its layering and ordering. Retain the complete
        # layout, expanding uniformly only for larger live geometry.
        scale = max([1.] + [max(existing[key][axis] / planned[key][axis] for axis in (2, 3))
                            for key in targets & set(existing)])
        for key in targets:
            x, y, width, height = planned[key]
            planned[key] = (x * scale, y * scale, width, height)
        # Old run summaries not present in this cumulative plan stay put.
        # Translate the whole group around them, keeping its internal order.
        place_group(sorted(targets), 0, 0)
        return result, 0

    if reorganize:
        # Preserve resized/rotated geometry while expanding columns just enough
        # to keep forward flow and remove overlap between larger shapes.
        graph_keys = [key for key in targets if key != "legend" and not key.startswith("run:") and key not in fee_keys]
        columns = {}
        for key in graph_keys:
            columns.setdefault(planned[key][0], []).append(key)
        previous_right = -math.inf
        for x, keys in sorted(columns.items()):
            half = max(effective(key, 0, 0)[2] / 2 for key in keys)
            actual_x = max(x, previous_right + 100 + half)
            for key in sorted(keys, key=lambda key: (planned[key][1], key)):
                place_group([key], actual_x - x, 0)
            previous_right = actual_x + half
        place_fees()
        for key in sorted(targets - set(graph_keys) - fee_keys):
            place_group([key], 0, 0, upward=True)
        return result, 0

    graph_targets = {key for key in targets if key != "legend" and not key.startswith("run:") and key not in fee_keys}
    remaining = set(graph_targets)
    while remaining:
        seed = min(remaining, key=lambda key: (planned[key][0], planned[key][1], key))
        group, stack = set(), [seed]
        while stack:
            key = stack.pop()
            if key in group:
                continue
            group.add(key)
            stack.extend(adjacency[key] & remaining - group)
        remaining -= group
        anchors = {neighbor for key in group for neighbor in adjacency[key] if neighbor in existing and neighbor not in fee_keys}
        dx = statistics.median(existing[key][0] - planned[key][0] for key in anchors) if anchors else 0
        dy = statistics.median(existing[key][1] - planned[key][1] for key in anchors) if anchors else 0
        lower, upper = -math.inf, math.inf
        for edge in plan["connectors"]:
            source, target = edge["source"], edge["target"]
            if source in fee_keys or target in fee_keys:
                continue
            if planned[source][0] >= planned[target][0]:
                continue  # Reused-address cycles are intentionally return links.
            if source in existing and target in group:
                lower = max(lower, existing[source][0] + existing[source][2] / 2 + 80
                            - (planned[target][0] - planned[target][2] / 2))
            if source in group and target in existing:
                upper = min(upper, existing[target][0] - existing[target][2] / 2 - 80
                            - (planned[source][0] + planned[source][2] / 2))
        if lower > upper:
            raise TraceError("Existing Miro positions leave no left-to-right space for this continuation; choose Reorganize graph or move its connected shapes before syncing. No board writes made.")
        dx = max(lower, min(dx, upper))
        place_group(sorted(group), dx, dy)
    place_fees()
    for key in sorted(targets - graph_targets - fee_keys):
        place_group([key], 0, 0, upward=True)
    return result, 0


# Miro's bulk response has no request-order guarantee or client key. Keep every
# request spatially unambiguous and match returned shapes by type and board
# position. A hundredth of a board unit tolerates insignificant serialization
# rounding, while any overlapping candidate leaves the whole operation pending.
_BULK_POSITION_TOLERANCE = .01


def _same_shape_position(first, second):
    try:
        if first["data"]["shape"] != second["data"]["shape"]:
            return False
        return all(math.isfinite(float(second["position"][axis]))
                   and abs(float(first["position"][axis]) - float(second["position"][axis]))
                   <= _BULK_POSITION_TOLERANCE for axis in ("x", "y"))
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _shape_batches(shapes):
    """Do not put indistinguishable shapes in the same bulk request."""
    batch = []
    for pending in shapes:
        if len(batch) == 20 or any(_same_shape_position(pending["body"], other["body"])
                                   for other in batch):
            yield batch
            batch = []
        batch.append(pending)
    if batch:
        yield batch


def _bulk_records(batch, raw, mapped_ids):
    response = _response(raw, "bulk POST; reconcile the pending items before retrying")
    returned = response.get("data")
    if not isinstance(returned, list) or len(returned) != len(batch):
        raise TraceError("Miro bulk POST returned an incomplete item list; reconcile the pending items")
    assignments, ids = {}, set()
    for item in returned:
        if (not isinstance(item, dict) or item.get("type") != "shape"
                or not isinstance(item.get("id"), str) or not item["id"]
                or item["id"] in mapped_ids or item["id"] in ids):
            raise TraceError("Miro bulk POST returned an invalid or already mapped ID; reconcile the pending items")
        parent, position = item.get("parent"), item.get("position")
        if ((parent is not None and (not isinstance(parent, dict) or parent.get("id")))
                or not isinstance(position, dict)
                or position.get("origin", "center") != "center"
                or position.get("relativeTo", "canvas_center") != "canvas_center"):
            raise TraceError("Miro bulk POST returned non-canvas positions; reconcile the pending items")
        candidates = [pending for pending in batch if _same_shape_position(pending["body"], item)]
        if len(candidates) != 1 or candidates[0]["key"] in assignments:
            raise TraceError("Miro bulk POST returned ambiguous shape positions; reconcile the pending items")
        pending = candidates[0]
        ids.add(item["id"])
        assignments[pending["key"]] = _record_pending(pending, item["id"], item)
    return assignments


def _create_items(plan, state, journal, requests, base, headers, positions, mapped_ids, report, progress):
    """Batch shapes, then overlap connectors whose endpoint IDs are durable.

    All checkpoint changes happen on the coordinator. On failure the request
    pool drains acknowledgements; only jobs proved never sent lose their intent.
    A POST with no trustworthy response is never retried automatically.
    """
    progress.emit("creating", 0, report["new_items"], "Adding new shapes and connections")

    def pending_item(item, endpoint):
        key = item["key"]
        body = copy.deepcopy(item["body"])
        if endpoint == "shapes":
            body["position"].update(dict(zip(("x", "y"), positions[key])))
            if "activity_frames" in plan:
                body["parent"] = {"id": None}
        elif endpoint == "connectors":
            body.update(_connection_body(item, state["items"][item["source"]]["id"],
                                         state["items"][item["target"]]["id"]))
        pending = {"key": key, "endpoint": endpoint, "body": body, "run_id": plan["run_id"]}
        if key in plan.get("fee_items", {}):
            pending["fee_proof"] = copy.deepcopy(plan["fee_items"][key])
        if endpoint == "connectors":
            pending.update({"source": item["source"], "target": item["target"]})
            if item.get("attachment"):
                pending["attachments"] = copy.deepcopy(item["attachment"])
        if endpoint == "frames":
            pending["frame_proof"] = _frame_proof(key)
        return pending

    def journal_job(batch, endpoint):
        operation = digest(canonical({"run_id": plan["run_id"], "endpoint": endpoint, "items": batch}))[:24]
        for pending in batch:
            pending["operation_id"] = operation
        journal.commit(sets=[(("pending_creations", pending["key"]), pending) for pending in batch])
        return {"items": batch, "endpoint": endpoint}

    def clear(job):
        journal.commit(deletes=[("pending_creations", pending["key"]) for pending in job["items"]])

    def worker(job):
        batch = job["items"]
        body = ([{"type": "shape", **pending["body"]} for pending in batch]
                if job["endpoint"] == "items/bulk" else batch[0]["body"])
        encoded = canonical(body)
        for attempt in range(4):
            result = requests.send("POST", base + "/" + job["endpoint"], headers, encoded, 30,
                                   credits=100 * len(batch))
            if result[0] == 429 and attempt < 3:
                try:
                    requests.pause_for_rate_limit(result[1])
                except TraceError as error:
                    # The rejection is known. Pass it back to the coordinator
                    # before raising the wait error so no false uncertainty stays.
                    return result, error
                continue
            return result, None

    def reject(job, error):
        if isinstance(error, MiroRequestNotSent):
            clear(job)

    def accept(job, outcome):
        (status, _, raw), known_error = outcome
        if not 200 <= status < 300:
            rejected = 400 <= status < 500 and status != 408
            if rejected:
                clear(job)
            if known_error is not None:
                raise known_error
            raise TraceError("Miro POST returned HTTP " + str(status) +
                             ("; fix the error and rerun sync" if rejected
                              else "; reconcile the pending items before retrying"))
        batch = job["items"]
        if job["endpoint"] == "items/bulk":
            records = _bulk_records(batch, raw, mapped_ids)
        else:
            response = _response(raw, "POST; reconcile the pending item before retrying")
            item_id = response.get("id")
            if not isinstance(item_id, str) or not item_id or item_id in mapped_ids:
                raise TraceError("Miro POST returned an invalid or already mapped ID; reconcile the pending item")
            pending = batch[0]
            records = {pending["key"]: _record_pending(pending, item_id, response)}
        # Atomically record the entire acknowledged batch before its connector
        # IDs can be used. Invalid response matching never saves partial guesses.
        journal.commit(sets=[(("items", key), record) for key, record in records.items()],
                       deletes=[("pending_creations", pending["key"]) for pending in batch])
        mapped_ids.update(record["id"] for record in records.values())
        for _ in batch:
            report["created"] += 1
            if job["endpoint"] == "frames":
                report["created_frames"] = report.get("created_frames", 0) + 1
                progress.emit("framing", report["created_frames"], report["new_frames"], "Updating graph export frames")
            else:
                progress.emit("creating", report["created"], report["new_items"], "Adding new shapes and connections")

    shape_items = (pending_item(item, "shapes") for item in plan["shapes"] if item["key"] not in state["items"])
    # Serial bulk requests already remove almost all shape round trips. Keeping
    # one outstanding bulk makes an uncertain response straightforward to inspect.
    for batch in _shape_batches(shape_items):
        def shape_job():
            yield journal_job(batch, "items/bulk" if len(batch) > 1 else "shapes")
        # The one-job worker batch also distinguishes cancellation while waiting
        # for quota from an uncertain POST and drains any response already sent.
        requests.map(shape_job(), worker, accept, reject=reject)

    def connector_jobs():
        for item in plan["connectors"]:
            if item["key"] not in state["items"]:
                # The generator runs on the coordinator before executor.submit.
                # At most workers intents are outstanding, never the whole graph.
                yield journal_job([pending_item(item, "connectors")], "connectors")

    requests.map(connector_jobs(), worker, accept, reject=reject)
    # Frames are created last and one at a time. They have no native parent or
    # children fields: the nested export regions are defined by canvas bounds.
    for item in plan.get("frames", []):
        if item["key"] not in state["items"]:
            progress.emit("framing", report.get("created_frames", 0), report["new_frames"], "Updating graph export frames")
            def frame_job():
                yield journal_job([pending_item(item, "frames")], "frames")
            requests.map(frame_job(), worker, accept, reject=reject)


def sync(plan, board_id, state_path, max_items=750, token=None, transport=http, interval=.02, dry_run=False,
         reorganize=False, progress=None, workers=4):
    """Add bounded runs to one board; preserve manually edited fields and geometry.

    Official REST references (boards:read and boards:write):
    https://developers.miro.com/reference/get-shape-item-1
    https://developers.miro.com/reference/update-shape-item-1
    https://developers.miro.com/reference/get-connector-1
    https://developers.miro.com/reference/update-connector-1

    A dry run is local-only, so it cannot predict remote conflicts or positions.
    The same state file must be reused for every run of this case and board.
    Progress receives generic phase/count dictionaries, without board content.
    Reads, updates, and connector creation use at most four workers. Shapes
    are created in batches of up to twenty, matched by their unique positions
    rather than response order. Every POST has a durable per-item intent before
    dispatch; uncertain outcomes require explicit reconciliation. A local quota
    coordinator shares credit reservations across processes using this token.
    https://developers.miro.com/reference/rate-limiting
    """
    validate_plan(plan)
    namespace = _namespace(plan)
    if (not isinstance(board_id, str) or not board_id or len(board_id) > 200
            or any(c in board_id for c in "/?#") or any(c.isspace() for c in board_id)):
        raise TraceError("Provide the Miro board ID, not its full URL")
    if not isinstance(max_items, int) or max_items < 0:
        raise TraceError("max-items must be a nonnegative integer (it limits new items)")
    if (isinstance(interval, bool) or not isinstance(interval, (int, float))
            or not math.isfinite(interval) or interval < 0):
        raise TraceError("Miro interval must be finite and nonnegative")
    if type(workers) is not int or not 1 <= workers <= 4:
        raise TraceError("Miro workers must be an integer between 1 and 4")
    if type(reorganize) is not bool:
        raise TraceError("reorganize must be a boolean")
    if progress is not None and not callable(progress):
        raise TraceError("Miro progress must be a callback")
    status_progress = _SyncProgress(progress)
    state_path = Path(state_path)
    state = _load_sync_state(state_path, board_id, namespace)
    collections = (("shapes", plan["shapes"]), ("connectors", plan["connectors"]),
                   ("frames", plan.get("frames", [])))

    def preview(current):
        _check_lineage(plan, current)
        removals = _fee_removals(plan, current)
        frame_removals = _frame_removals(plan, current)
        report = {"dry_run": dry_run, "board_url": "https://miro.com/app/board/" + urllib.parse.quote(board_id, safe="") + "/",
                  "run_id": plan["run_id"], "namespace": namespace, "state_path": str(state_path), "max_items": max_items,
                  "reorganize": reorganize, "fee_items_to_remove": len(removals),
                  "frames_to_remove": len(frame_removals)}
        layout = plan.get("layout", {})
        if layout.get("algorithm"):
            report["layout_algorithm"] = layout["algorithm"]
            report["connector_style"] = plan.get("graph_options", {}).get("connector_style", "straight")
            if "fallback_reason" in layout:
                report["fallback_reason"] = layout["fallback_reason"]
            if "metrics" in layout:
                report["layout_metrics"] = copy.deepcopy(layout["metrics"])
                report["layout_metrics_notice"] = ("Estimated for the local proposed layout. Miro routes connectors itself; "
                                                   "preserved positions and live size adjustments can change crossings.")
        for endpoint, collection in collections:
            for item in collection:
                record = current["items"].get(item["key"])
                if record and record["endpoint"] != endpoint:
                    raise TraceError("Miro logical key changed item type: " + item["key"])
                if record and endpoint == "connectors" and (record.get("source") != item["source"] or record.get("target") != item["target"]):
                    raise TraceError("Miro connector logical key changed endpoints: " + item["key"])
            report["new_" + endpoint] = sum(item["key"] not in current["items"] for item in collection)
            report["mapped_" + endpoint] = len(collection) - report["new_" + endpoint]
        report["new_items"] = report["new_shapes"] + report["new_connectors"] + report["new_frames"]
        report["existing_items"] = len(current["items"])
        if report["new_items"] > max_items:
            raise TraceError(f"Sync needs {report['new_items']} new items, above max-items={max_items}; reduce the trace or explicitly raise the limit")
        return report

    report = preview(state)
    if dry_run:
        report["remote_preflight_required"] = True
        report["notice"] = "Local preview only; live sync checks mapped objects, manual edits, and current layout before writing."
        return report
    token = token or os.getenv("MIRO_ACCESS_TOKEN")
    if not token:
        raise TraceError("Set MIRO_ACCESS_TOKEN locally (boards:read and boards:write scopes)")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix(".lock").open("w") as lock, ExitStack() as resources:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("Another publisher is using this state file") from None
        state = _load_sync_state(state_path, board_id, namespace)
        report = preview(state)
        removals = _fee_removals(plan, state)
        frame_removals = _frame_removals(plan, state)
        base = "https://api.miro.com/v2/boards/" + urllib.parse.quote(board_id, safe="")
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"}
        quota = None
        if transport is http:
            quota = resources.enter_context(SharedMiroQuota(token))
            transport = resources.enter_context(MiroHTTP())
        requests = resources.enter_context(MiroRequests(transport, interval=interval, workers=workers,
                                                         progress=status_progress, quota=quota))
        # The complete live preflight must succeed before any state or board writes.
        remote = preflight(requests, base, headers, state, {**removals, **frame_removals}, progress=status_progress)
        live_frame_records = {key: record for key, record in state["items"].items()
                              if record["endpoint"] == "frames" and key in remote}
        live_frame_ids = {record["id"] for record in live_frame_records.values()}

        status_progress.emit("layout", 0, 1, "Checking connections and preparing the layout")
        _check_fee_removals(state, remote, removals)
        original_recovered = {key: copy.deepcopy(state["items"][key])
                              for key in state.get("pending_updates", {})}
        _recover_updates(state, remote)
        positions, shift_x = _placements(plan, state, remote, removals, reorganize)
        frames = _live_frames(plan, state, remote, positions, removals)
        live_collections = (("shapes", plan["shapes"]), ("connectors", plan["connectors"]), ("frames", frames))
        updates, conflicts, attachment_intents = [], [], {}
        for endpoint, collection in live_collections:
            for item in collection:
                key = item["key"]
                if key not in state["items"]:
                    continue
                record = state["items"][key]
                if endpoint == "connectors":
                    for field, logical in (("startItem", "source"), ("endItem", "target")):
                        expected = state["items"].get(record[logical], {}).get("id")
                        actual = remote[key].get(field, {}).get("id")
                        if actual != expected:
                            raise TraceError("Miro connector endpoints were changed for " + key + "; restore or repair this connection before syncing. No board writes made.")
                patch, managed, intent, item_conflicts = _merge_fields(record, item["body"], remote[key], key)
                if reorganize and endpoint == "shapes":
                    x, y = positions[key]
                    if any(float(remote[key]["position"][field]) != value for field, value in (("x", x), ("y", y))):
                        patch["position"] = {"x": x, "y": y, "origin": "center"}
                if reorganize and endpoint == "connectors" and item.get("attachment"):
                    patch.update(_attachment_patch(item, remote[key]))
                    attachment_intents[key] = copy.deepcopy(item["attachment"])
                    if (plan.get("connector_attachment") == "transaction_ports_v2"
                            and remote[key].get("shape") != item["body"]["shape"]):
                        patch["shape"] = item["body"]["shape"]
                if endpoint == "frames":
                    _bounds(remote[key], key)
                    for group, fields in (("position", ("x", "y")), ("geometry", ("width", "height"))):
                        if any(not math.isclose(float(remote[key][group][field]), float(item["body"][group][field]),
                                                rel_tol=0, abs_tol=.01) for field in fields):
                            patch[group] = copy.deepcopy(item["body"][group])
                conflicts.extend(item_conflicts)
                updates.append((key, patch, managed, intent))
        affected_frame_keys = {key for key, patch, _, _ in updates
                               if state["items"][key]["endpoint"] == "frames"
                               and ("position" in patch or "geometry" in patch)} | set(frame_removals)
        affected_frames = {key: record for key, record in live_frame_records.items() if key in affected_frame_keys}
        validate_frame_children(requests, base, headers, state, remote, affected_frames)
        affected_frame_ids = {record["id"] for record in affected_frames.values()}
        updates_by_key = {job[0]: job for job in updates}
        for key, record in state["items"].items():
            if record["endpoint"] != "shapes" or key in removals:
                continue
            parent_id = (remote[key].get("parent") or {}).get("id")
            job = updates_by_key.get(key)
            if (parent_id not in live_frame_ids
                    or (parent_id not in affected_frame_ids and not (job and "position" in job[1]))):
                continue
            # Detach only when this shape or its frame must move. An unchanged
            # export region can retain attached analyst notes without blocking
            # an otherwise empty sync. Include older run notes absent the plan.
            x, y = positions.get(key, _bounds(remote[key], key)[:2])
            if job is None:
                job = (key, {}, copy.deepcopy(record["managed"]), copy.deepcopy(record["intent"]))
                updates.append(job)
            job[1].update({"parent": {"id": None}, "position": {"x": x, "y": y}})
        status_progress.emit("layout", 1, 1, "Checking connections and preparing the layout")
        # State changes begin only after local and remote preflight succeeds.
        report.update({"created": 0, "updated": 0, "deleted": 0, "moved": 0, "reattached": 0,
                       "conflicts": conflicts, "new_batch_offset_x": shift_x})
        changes = [{"key": key, "item_id": state["items"][key]["id"],
                    "before": copy.deepcopy(remote[key]["position"]), "after": copy.deepcopy(patch["position"])}
                   for key, patch, _, _ in updates if "position" in patch]
        attachment_changes = [{"key": key, "item_id": state["items"][key]["id"],
                               "before": {field: {name: copy.deepcopy(value) for name, value in remote[key][field].items()
                                                  if name in ("id", "snapTo", "position")}
                                          for field in ("startItem", "endItem") if field in patch},
                               "after": {field: copy.deepcopy(patch[field]) for field in ("startItem", "endItem") if field in patch}}
                              for key, patch, _, _ in updates if "startItem" in patch or "endItem" in patch]
        connector_shapes = [{"key": key, "item_id": state["items"][key]["id"],
                             "before": remote[key].get("shape"), "after": patch["shape"]}
                            for key, patch, _, _ in updates if "shape" in patch]
        recovered_records = {key: state["items"][key] for key in original_recovered}
        state["items"].update(original_recovered)
        journal = resources.enter_context(SyncState(state_path, state))
        state["items"].update(recovered_records)
        if changes or attachment_changes or connector_shapes:
            snapshot = {"recorded_at": now(), "run_id": plan["run_id"], "plan_sha256": plan["sha256"],
                        "positions": changes, "attachments": attachment_changes}
            if connector_shapes:
                snapshot["connector_shapes"] = connector_shapes
            state.setdefault("layout_history", []).append(snapshot)
            report["layout_snapshot"] = copy.deepcopy(snapshot)
        state["active_run_id"] = plan["run_id"]
        # Journal every planned edit before dispatch. On retry, the full live
        # preflight reconciles these fields instead of blindly replaying them.
        state["pending_updates"] = {
            key: {"id": state["items"][key]["id"], "endpoint": state["items"][key]["endpoint"],
                  "patch": copy.deepcopy(patch)}
            for key, patch, _, _ in updates if patch}
        pending_deletions = state.setdefault("pending_deletions", {})
        for key, proof in removals.items():
            pending_deletions.setdefault(key, {"id": state["items"][key]["id"], "proof": copy.deepcopy(proof), "attempted": False})
        pending_frame_deletions = state.setdefault("pending_frame_deletions", {})
        for key, proof in frame_removals.items():
            pending_frame_deletions.setdefault(key, {
                "id": state["items"][key]["id"], "proof": copy.deepcopy(proof), "attempted": False})
        initial_sets = [(("active_run_id",), state["active_run_id"]),
                        (("pending_updates",), state["pending_updates"]),
                        (("pending_deletions",), pending_deletions),
                        (("pending_frame_deletions",), pending_frame_deletions)]
        if changes or attachment_changes or connector_shapes:
            initial_sets.append((("layout_history",), state["layout_history"]))
        # _recover_updates refreshed these records from live evidence. Save those
        # changes alongside the new edit journal before dispatching mutations.
        initial_sets.extend((("items", key), record) for key, record in recovered_records.items())
        journal.commit(sets=initial_sets)
        mapped_ids = {record["id"] for record in state["items"].values()}
        status_progress.emit("removing", 0, len(removals), "Removing excluded fee items")
        for key in sorted(removals, key=lambda key: (0 if state["items"][key]["endpoint"] == "connectors" else 1, key)):
            record = state["items"][key]
            # Save intent before DELETE so a lost response can be reconciled by
            # the next preflight GET, without tolerating unrelated missing items.
            pending_deletions[key]["attempted"] = True
            journal.commit(sets=[(("pending_deletions", key), pending_deletions[key])])
            status, _, _ = requests.request("DELETE", _remote_url(base, record), headers)
            if not (200 <= status < 300 or status == 404):
                raise TraceError("Miro fee DELETE returned HTTP " + str(status) + "; acknowledged progress is saved; rerun with fees excluded")
            del state["items"][key]
            mapped_ids.remove(record["id"])
            del pending_deletions[key]
            journal.commit(deletes=[("items", key), ("pending_deletions", key)])
            report["deleted"] += 1
            status_progress.emit("removing", report["deleted"], len(removals), "Removing excluded fee items")
        status_progress.emit("updating", 0, len(updates), "Applying changes while preserving manual edits")
        checked = 0

        def update_item(job):
            key, patch, _, _ = job
            if not patch:
                return None
            record = state["items"][key]
            result = requests.request("PATCH", _remote_url(base, record), headers, patch)
            status, _, raw = result
            if not 200 <= status < 300:
                raise TraceError("Miro PATCH returned HTTP " + str(status) + "; acknowledged progress is saved; rerun sync")
            response = _response(raw, "PATCH")
            if response.get("id") != record["id"]:
                raise TraceError("Miro PATCH returned the wrong item ID; rerun sync to reconcile the saved update journal")
            return response

        def accept_update(job, response):
            nonlocal checked
            key, patch, managed, intent = job
            record = state["items"][key]
            if patch:
                actual = _baseline(patch, response, record["endpoint"])
                for path, value in _fields(_editable(patch, record["endpoint"])):
                    _set(intent, path, value)
                    _set(managed, path, _get(actual, path))
                report["updated"] += 1
                if "position" in patch:
                    report["moved"] += 1
                if "startItem" in patch or "endItem" in patch:
                    report["reattached"] += 1
            changed = record["managed"] != managed or record["intent"] != intent
            record["managed"], record["intent"] = managed, intent
            if key in attachment_intents:
                changed |= any(record.get("attachments", {}).get(field) != value
                               for field, value in attachment_intents[key].items())
                record.setdefault("attachments", {}).update(attachment_intents[key])
            # Unchanged mappings need no per-item rewrite/fsync. Acknowledged
            # PATCHes and refreshed baselines still persist before continuing.
            if patch or changed:
                state["pending_updates"].pop(key, None)
                journal.commit(sets=[(("items", key), record)], deletes=[("pending_updates", key)])
            checked += 1
            status_progress.emit("updating", checked, len(updates), "Applying changes while preserving manual edits")
        # Native frame children must be detached and acknowledged before a
        # frame moves. Parallelizing these two groups together can move a child
        # twice even though every individual PATCH reports success.
        requests.map((job for job in updates if state["items"][job[0]]["endpoint"] != "frames"),
                     update_item, accept_update)
        changing_frames = {job[0]: state["items"][job[0]] for job in updates
                           if state["items"][job[0]]["endpoint"] == "frames"
                           and ("position" in job[1] or "geometry" in job[1])}
        check_empty_frames(requests, base, headers, changing_frames)
        if changing_frames:
            status_progress.emit("framing", 0, len(changing_frames), "Updating graph export frames")
        requests.map((job for job in updates if state["items"][job[0]]["endpoint"] == "frames"),
                     update_item, accept_update)
        _create_items({**plan, "frames": frames}, state, journal, requests, base, headers, positions, mapped_ids,
                      report, status_progress)
        # Obsolete component frames are removed only after the new graph and
        # replacement export regions exist. These IDs are ours, never frames
        # discovered by searching the board or user-created containers.
        if frame_removals:
            status_progress.emit("framing", 0, len(frame_removals), "Updating graph export frames")
        for index, key in enumerate(sorted(frame_removals), 1):
            record = state["items"][key]
            if key in remote:
                check_empty_frames(requests, base, headers, {key: record})
            pending_frame_deletions[key]["attempted"] = True
            journal.commit(sets=[(("pending_frame_deletions", key), pending_frame_deletions[key])])
            status, _, _ = requests.request("DELETE", _remote_url(base, record), headers)
            if not (200 <= status < 300 or status == 404):
                raise TraceError("Miro frame DELETE returned HTTP " + str(status) + "; acknowledged progress is saved; rerun sync")
            journal.commit(deletes=[("items", key), ("pending_frame_deletions", key)])
            mapped_ids.remove(record["id"])
            report["deleted"] += 1
            status_progress.emit("framing", index, len(frame_removals), "Updating graph export frames")
        summary = state["runs"].setdefault(plan["run_id"], {"first_synced_at": now(), "plan_sha256s": []})
        if plan["sha256"] not in summary["plan_sha256s"]:
            summary["plan_sha256s"].append(plan["sha256"])
        summary.update({"last_synced_at": now(), "shapes": len(plan["shapes"]), "connectors": len(plan["connectors"]),
                        "frames": len(frames),
                        "conflicts": conflicts, "run": copy.deepcopy(plan.get("run", {}))})
        state["latest_run_id"], state["active_run_id"] = plan["run_id"], None
        journal.commit(sets=[(("runs", plan["run_id"]), summary),
                             (("latest_run_id",), state["latest_run_id"]), (("active_run_id",), None)])
        report["items"] = len(state["items"])
        report["runs"] = len(state["runs"])
        status_progress.emit("complete", 1, 1, "Miro sync complete")
        return report


def _record_pending(pending, item_id, response=None):
    intent = _editable(pending["body"], pending["endpoint"])
    record = {"id": item_id, "endpoint": pending["endpoint"], "intent": intent,
              "managed": _baseline(intent, response or {}, pending["endpoint"])}
    if pending["endpoint"] == "connectors":
        record.update({"source": pending["source"], "target": pending["target"]})
        if pending.get("attachments"):
            record["attachments"] = copy.deepcopy(pending["attachments"])
    if "fee_proof" in pending:
        record["fee_proof"] = copy.deepcopy(pending["fee_proof"])
    if "frame_proof" in pending:
        record["frame_proof"] = copy.deepcopy(pending["frame_proof"])
    return record


def publish(plan, board_id, state_path, max_items=750, token=None, transport=http, interval=.4):
    """Append this run as a snapshot. Acknowledged items are never posted twice.

    Ambiguous POST outcomes are deliberately not automatically retried: the
    remote item may exist even if its response was lost. Reconcile via CLI.
    """
    validate_plan(plan)
    count = len(plan["shapes"]) + len(plan["connectors"]) + len(plan.get("frames", []))
    if count > max_items:
        raise TraceError(f"Plan has {count} items, above max-items={max_items}; select a smaller trace or explicitly raise the limit")
    token = token or os.getenv("MIRO_ACCESS_TOKEN")
    if not token:
        raise TraceError("Set MIRO_ACCESS_TOKEN locally (boards:write scope)")
    if not board_id or len(board_id) > 200:
        raise TraceError("Provide the Miro board ID, not its full URL")
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix(".lock").open("w") as lock, ExitStack() as resources:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("Another publisher is using this state file") from None
        state = load_state(state_path, {
            "board_id": board_id, "plan_sha256": plan["sha256"], "items": {}, "pending": None})
        if state["board_id"] != board_id or state["plan_sha256"] != plan["sha256"]:
            raise TraceError("Miro state belongs to a different board or plan")
        if state.get("pending"):
            raise TraceError("Prior Miro POST outcome is uncertain for " + state["pending"]["key"] +
                             "; inspect the board and use miro-resolve before retrying")
        journal = resources.enter_context(SyncState(state_path, state))
        base = "https://api.miro.com/v2/boards/" + urllib.parse.quote(board_id, safe="")
        for endpoint, collection in (("shapes", plan["shapes"]), ("connectors", plan["connectors"]),
                                     ("frames", plan.get("frames", []))):
            for item in collection:
                key = item["key"]
                if key in state["items"]:
                    continue
                body = dict(item["body"])
                if endpoint == "shapes" and "activity_frames" in plan:
                    body["parent"] = {"id": None}
                if endpoint == "connectors":
                    body.update(_connection_body(item, state["items"][item["source"]], state["items"][item["target"]]))
                for attempt in range(4):
                    state["pending"] = {"key": key, "endpoint": endpoint}
                    journal.commit(sets=[(("pending",), state["pending"])])
                    # Network failures leave pending intact for explicit reconciliation.
                    status, headers, raw = transport("POST", base + "/" + endpoint,
                        {"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"}, canonical(body), 30)
                    if 200 <= status < 300:
                        try:
                            item_id = __import__("json").loads(raw)["id"]
                            if not isinstance(item_id, str) or not item_id:
                                raise ValueError("missing id")
                        except (ValueError, KeyError, TypeError):
                            raise TraceError("Miro accepted POST but returned no valid ID; reconcile the pending item") from None
                        state["items"][key] = item_id
                        state["pending"] = None
                        journal.commit(sets=[(("items", key), item_id), (("pending",), None)])
                        break
                    if 400 <= status < 500 and status != 408:
                        state["pending"] = None
                        journal.commit(sets=[(("pending",), None)])
                    if status == 429 and attempt < 3:
                        retry = next((v for k, v in headers.items() if k.lower() == "retry-after"), "5")
                        try:
                            delay = max(1., float(retry))
                        except ValueError:
                            delay = 5.
                        if delay > 30:
                            raise TraceError("Miro rate limited; rerun this command later")
                        time.sleep(delay)
                        continue
                    raise TraceError("Miro POST returned HTTP " + str(status) + ("; reconcile pending item" if status >= 500 else ""))
                time.sleep(max(0., interval))
        return {"board_url": "https://miro.com/app/board/" + urllib.parse.quote(board_id, safe="") + "/",
                "items": len(state["items"]), "state_path": str(state_path)}


def resolve(state_path, item_id=None, absent=False, key=None):
    """Reconcile one explicitly inspected creation, including a bulk member."""
    if bool(item_id) == bool(absent):
        raise TraceError("Choose an existing remote item ID or --absent")
    if key is not None and (not isinstance(key, str) or not key):
        raise TraceError("Provide the logical pending item key")
    state_path = Path(state_path)
    with state_path.with_suffix(".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("Publisher is running") from None
        state = load_state(state_path)
        entries = state.get("pending_creations", {})
        if not isinstance(entries, dict):
            raise TraceError("Malformed Miro creation journal; restore its last intact version")
        pending = dict(entries)
        legacy = state.get("pending")
        if legacy:
            if not isinstance(legacy, dict) or not isinstance(legacy.get("key"), str):
                raise TraceError("Malformed pending Miro item")
            if legacy["key"] in pending:
                raise TraceError("Duplicate pending Miro item; restore its last intact version")
            pending[legacy["key"]] = legacy
        if not pending:
            raise TraceError("No pending item to reconcile")
        if key is None:
            if len(pending) != 1:
                raise TraceError("Multiple Miro items need reconciliation; use --key for one of: " + ", ".join(pending))
            key = next(iter(pending))
        if key not in pending:
            raise TraceError("That key is not a pending Miro item; choose one of: " + ", ".join(pending))
        entry = pending[key]
        sets = []
        if item_id:
            if not isinstance(item_id, str) or not item_id.strip() or "/" in item_id:
                raise TraceError("Provide the remote item ID, not a URL")
            if state.get("schema_version") == 2:
                if item_id in {record["id"] for record in state["items"].values()}:
                    raise TraceError("That remote item ID is already mapped; inspect the pending item again")
                record = _record_pending(entry, item_id)
            else:
                if item_id in state["items"].values():
                    raise TraceError("That remote item ID is already mapped; inspect the pending item again")
                record = item_id
            sets.append((("items", key), record))
        deletes = []
        if key in entries:
            deletes.append(("pending_creations", key))
        else:
            sets.append((("pending",), None))
        with SyncState(state_path, state) as journal:
            journal.commit(sets=sets, deletes=deletes)
