import copy
import fcntl
import html
import json
import math
import os
import time
import urllib.parse
from pathlib import Path

from .api import http
from .common import TraceError, canonical, digest, now, read_json, save_json


def make_plan(graph):
    shapes, connectors = [], []
    incremental = "namespace" in graph
    title = ("SYNTHETIC DEMO · " if graph["simulated"] else "") + "Liquid UTXO trace"
    if not incremental:
        title += " · " + graph["run_id"]
    shapes.append({"key": "legend", "body": {"data": {"shape": "rectangle", "content":
        "<p><strong>" + html.escape(title) + "</strong></p><p>Circles: addresses / outpoint occurrences. Squares: transactions. Diamonds: events.</p>"
        "<p>Red: seed. Yellow: candidate. Gray: context. Green: analyst attribution (read confidence).</p><p>" + html.escape(graph["notice"]) + "</p>"},
        "position": {"x": 700, "y": -160, "origin": "center"}, "geometry": {"width": 1300, "height": 260},
        "style": {"fillColor": "#f5f6f8", "fontSize": "18", "textAlign": "left"}}})
    if incremental:
        details = graph.get("run", {})
        lines = ["Run: " + graph["run_id"]]
        for key in ("started_at", "finished_at", "status", "parent_run", "stop_reason", "max_hops", "seeds", "limits", "stats"):
            if key in details:
                value = details[key]
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
    for edge in graph["edges"]:
        connectors.append({"key": edge["id"], "source": edge["source"], "target": edge["target"], "body": {
            "shape": "curved", "captions": [{"content": html.escape(edge["label"] + " · " + edge["quantity"]), "position": "50%"}],
            "style": {"startStrokeCap": "none", "endStrokeCap": "stealth", "strokeStyle": "normal",
                      "strokeColor": "#9ca3af" if edge["role"].startswith("context") else "#155e75",
                      "strokeWidth": "2", "fontSize": "11"}}})
    plan = {"schema_version": 2 if incremental else 1, "run_id": graph["run_id"], "shapes": shapes, "connectors": connectors}
    if incremental:
        plan["namespace"] = copy.deepcopy(graph["namespace"])
        plan["run"] = copy.deepcopy(graph.get("run", {}))
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
        if not all(isinstance(plan[k], list) for k in ("shapes", "connectors")):
            raise ValueError
        keys = [item["key"] for item in plan["shapes"] + plan["connectors"]]
        if any(not isinstance(k, str) or not k for k in keys):
            raise ValueError
        for item in plan["shapes"] + plan["connectors"]:
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
    state = read_json(path) if Path(path).exists() else {
        "schema_version": 2, "board_id": board_id, "namespace": copy.deepcopy(namespace),
        "items": {}, "runs": {}, "pending": None}
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
                or record.get("endpoint") not in ("shapes", "connectors")
                or not isinstance(record.get("managed"), dict) or not isinstance(record.get("intent"), dict)):
            raise TraceError("Malformed Miro item mapping; restore its last intact version")
        ids.append(record["id"])
    if len(ids) != len(set(ids)):
        raise TraceError("Miro state maps different objects to the same remote ID; repair the mapping")
    if state.get("pending"):
        raise TraceError("Prior Miro POST outcome is uncertain for " + str(state["pending"].get("key")) +
                         "; inspect the board and use miro-resolve before retrying")
    return state


def _editable(body, endpoint):
    """Only fields we may update. Geometry, parent, routing and endpoints stay untouched."""
    result = {}
    if endpoint == "shapes" and "content" in body.get("data", {}):
        result["data"] = {"content": copy.deepcopy(body["data"]["content"])}
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


def _retry_delay(headers):
    retry = next((v for k, v in headers.items() if k.lower() == "retry-after"), "2")
    try:
        delay = max(1., float(retry))
    except (TypeError, ValueError):
        delay = 2.
    if not math.isfinite(delay) or delay > 30:
        raise TraceError("Miro rate limited; rerun this command later")
    return delay


def _request(transport, method, url, headers, body=None, interval=0):
    """Bounded safe retries. POST uncertainty is handled separately by sync."""
    for attempt in range(4):
        status, response_headers, raw = transport(method, url, headers, canonical(body) if body is not None else None, 30)
        if status == 429 and attempt < 3:
            time.sleep(_retry_delay(response_headers))
            continue
        if method == "GET" and status >= 500 and attempt < 3:
            time.sleep(min(2 ** attempt, 4))
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


def sync(plan, board_id, state_path, max_items=750, token=None, transport=http, interval=.4, dry_run=False):
    """Add bounded runs to one board; preserve manually edited fields and geometry.

    Official REST references (boards:read and boards:write):
    https://developers.miro.com/reference/get-shape-item-1
    https://developers.miro.com/reference/update-shape-item-1
    https://developers.miro.com/reference/get-connector-1
    https://developers.miro.com/reference/update-connector-1

    A dry run is local-only, so it cannot predict remote conflicts or positions.
    The same state file must be reused for every run of this case and board.
    """
    validate_plan(plan)
    namespace = _namespace(plan)
    if (not isinstance(board_id, str) or not board_id or len(board_id) > 200
            or any(c in board_id for c in "/?#") or any(c.isspace() for c in board_id)):
        raise TraceError("Provide the Miro board ID, not its full URL")
    if not isinstance(max_items, int) or max_items < 0:
        raise TraceError("max-items must be a nonnegative integer (it limits new items)")
    if not math.isfinite(interval) or interval < 0:
        raise TraceError("Miro interval must be finite and nonnegative")
    state_path = Path(state_path)
    state = _load_sync_state(state_path, board_id, namespace)
    collections = (("shapes", plan["shapes"]), ("connectors", plan["connectors"]))

    def preview(current):
        _check_lineage(plan, current)
        report = {"dry_run": dry_run, "board_url": "https://miro.com/app/board/" + urllib.parse.quote(board_id, safe="") + "/",
                  "run_id": plan["run_id"], "namespace": namespace, "state_path": str(state_path), "max_items": max_items}
        for endpoint, collection in collections:
            for item in collection:
                record = current["items"].get(item["key"])
                if record and record["endpoint"] != endpoint:
                    raise TraceError("Miro logical key changed item type: " + item["key"])
                if record and endpoint == "connectors" and (record.get("source") != item["source"] or record.get("target") != item["target"]):
                    raise TraceError("Miro connector logical key changed endpoints: " + item["key"])
            report["new_" + endpoint] = sum(item["key"] not in current["items"] for item in collection)
            report["mapped_" + endpoint] = len(collection) - report["new_" + endpoint]
        report["new_items"] = report["new_shapes"] + report["new_connectors"]
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
    with state_path.with_suffix(".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("Another publisher is using this state file") from None
        state = _load_sync_state(state_path, board_id, namespace)
        report = preview(state)
        base = "https://api.miro.com/v2/boards/" + urllib.parse.quote(board_id, safe="")
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"}
        remote, missing = {}, []
        # Complete all mapped-item GETs before the first POST or PATCH.
        for key, record in state["items"].items():
            status, _, raw = _request(transport, "GET", _remote_url(base, record), headers, interval=interval)
            if status == 404:
                missing.append(key + " (" + record["id"] + ")")
                continue
            if not 200 <= status < 300:
                raise TraceError("Miro preflight GET returned HTTP " + str(status) + "; no board writes made")
            item = _response(raw, "preflight GET")
            if item.get("id") != record["id"]:
                raise TraceError("Miro preflight returned the wrong item ID; no board writes made")
            remote[key] = item
        if missing:
            raise TraceError("Miro preflight found missing or inaccessible mapped items: " + ", ".join(missing) +
                             ". No board writes made. Restore the items/access or repair the mapping; they will not be recreated automatically.")

        right_edges = []
        for key, record in state["items"].items():
            if record["endpoint"] != "shapes":
                continue
            item = remote[key]
            position = item.get("position", {})
            if (position.get("relativeTo") not in (None, "canvas_center")
                    or position.get("origin") not in (None, "center")
                    or ((item.get("parent") or {}).get("id") and position.get("relativeTo") != "canvas_center")):
                raise TraceError("Miro shape " + key + " has frame/group-relative coordinates. Move mapped shapes to the board canvas before syncing; no board writes made.")
            try:
                x = float(item["position"]["x"])
                width = float(item["geometry"]["width"])
                height = float(item["geometry"]["height"])
                rotation = float(item["geometry"].get("rotation", 0))
                if not all(math.isfinite(v) for v in (x, width, height, rotation)) or min(width, height) <= 0:
                    raise ValueError
                angle = math.radians(rotation)
                right_edges.append(x + (abs(width * math.cos(angle)) + abs(height * math.sin(angle))) / 2)
            except (KeyError, TypeError, ValueError, OverflowError):
                raise TraceError("Cannot read current Miro geometry for " + key + "; no board writes made") from None
        new_shapes = [item for item in plan["shapes"] if item["key"] not in state["items"]]
        shift_x = 0
        if new_shapes and right_edges:
            left = min(float(item["body"]["position"]["x"]) - float(item["body"]["geometry"]["width"]) / 2 for item in new_shapes)
            shift_x = max(0., max(right_edges) + 300 - left)
        updates, conflicts = [], []
        for endpoint, collection in collections:
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
                conflicts.extend(item_conflicts)
                updates.append((key, patch, managed, intent))
        # State changes begin only after local and remote preflight succeeds.
        report.update({"created": 0, "updated": 0, "conflicts": conflicts, "new_batch_offset_x": shift_x})
        state["active_run_id"] = plan["run_id"]
        save_json(state_path, state)
        for key, patch, managed, intent in updates:
            record = state["items"][key]
            if patch:
                status, _, raw = _request(transport, "PATCH", _remote_url(base, record), headers, patch, interval)
                if not 200 <= status < 300:
                    raise TraceError("Miro PATCH returned HTTP " + str(status) + "; acknowledged progress is saved; rerun sync")
                response = _response(raw, "PATCH")
                actual = _baseline(patch, response, record["endpoint"])
                for path, value in _fields(patch):
                    _set(intent, path, value)
                    _set(managed, path, _get(actual, path))
                report["updated"] += 1
            record["managed"], record["intent"] = managed, intent
            save_json(state_path, state)
        for endpoint, collection in collections:
            for item in collection:
                key = item["key"]
                if key in state["items"]:
                    continue
                body = copy.deepcopy(item["body"])
                if endpoint == "shapes":
                    body["position"]["x"] = float(body["position"]["x"]) + shift_x
                else:
                    body.update({"startItem": {"id": state["items"][item["source"]]["id"], "snapTo": "auto"},
                                 "endItem": {"id": state["items"][item["target"]]["id"], "snapTo": "auto"}})
                pending = {"key": key, "endpoint": endpoint, "body": body, "run_id": plan["run_id"]}
                if endpoint == "connectors":
                    pending.update({"source": item["source"], "target": item["target"]})
                for attempt in range(4):
                    state["pending"] = pending
                    save_json(state_path, state)
                    status, response_headers, raw = transport("POST", base + "/" + endpoint, headers, canonical(body), 30)
                    if 200 <= status < 300:
                        response = _response(raw, "POST; reconcile the pending item before retrying")
                        item_id = response.get("id")
                        if not isinstance(item_id, str) or not item_id:
                            raise TraceError("Miro accepted POST without a valid ID; reconcile the pending item")
                        if item_id in {r["id"] for r in state["items"].values()}:
                            raise TraceError("Miro POST returned an already mapped ID; reconcile the pending item")
                        record = _record_pending(pending, item_id, response)
                        state["items"][key], state["pending"] = record, None
                        save_json(state_path, state)
                        report["created"] += 1
                        break
                    # 408 and 5xx can occur after a server-side commit; never retry them.
                    if 400 <= status < 500 and status != 408:
                        state["pending"] = None
                        save_json(state_path, state)
                    if status == 429 and attempt < 3:
                        time.sleep(_retry_delay(response_headers))
                        continue
                    raise TraceError("Miro POST returned HTTP " + str(status) +
                                     ("; reconcile the pending item" if state["pending"] else "; fix the error and rerun sync"))
                time.sleep(interval)
        summary = state["runs"].setdefault(plan["run_id"], {"first_synced_at": now(), "plan_sha256s": []})
        if plan["sha256"] not in summary["plan_sha256s"]:
            summary["plan_sha256s"].append(plan["sha256"])
        summary.update({"last_synced_at": now(), "shapes": len(plan["shapes"]), "connectors": len(plan["connectors"]),
                        "conflicts": conflicts, "run": copy.deepcopy(plan.get("run", {}))})
        state["latest_run_id"], state["active_run_id"] = plan["run_id"], None
        save_json(state_path, state)
        report["items"] = len(state["items"])
        report["runs"] = len(state["runs"])
        return report


def _record_pending(pending, item_id, response=None):
    intent = _editable(pending["body"], pending["endpoint"])
    record = {"id": item_id, "endpoint": pending["endpoint"], "intent": intent,
              "managed": _baseline(intent, response or {}, pending["endpoint"])}
    if pending["endpoint"] == "connectors":
        record.update({"source": pending["source"], "target": pending["target"]})
    return record


def publish(plan, board_id, state_path, max_items=750, token=None, transport=http, interval=.4):
    """Append this run as a snapshot. Acknowledged items are never posted twice.

    Ambiguous POST outcomes are deliberately not automatically retried: the
    remote item may exist even if its response was lost. Reconcile via CLI.
    """
    validate_plan(plan)
    count = len(plan["shapes"]) + len(plan["connectors"])
    if count > max_items:
        raise TraceError(f"Plan has {count} items, above max-items={max_items}; select a smaller trace or explicitly raise the limit")
    token = token or os.getenv("MIRO_ACCESS_TOKEN")
    if not token:
        raise TraceError("Set MIRO_ACCESS_TOKEN locally (boards:write scope)")
    if not board_id or len(board_id) > 200:
        raise TraceError("Provide the Miro board ID, not its full URL")
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix(".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("Another publisher is using this state file") from None
        state = read_json(state_path) if state_path.exists() else {
            "board_id": board_id, "plan_sha256": plan["sha256"], "items": {}, "pending": None}
        if state["board_id"] != board_id or state["plan_sha256"] != plan["sha256"]:
            raise TraceError("Miro state belongs to a different board or plan")
        if state.get("pending"):
            raise TraceError("Prior Miro POST outcome is uncertain for " + state["pending"]["key"] +
                             "; inspect the board and use miro-resolve before retrying")
        base = "https://api.miro.com/v2/boards/" + urllib.parse.quote(board_id, safe="")
        for endpoint, collection in (("shapes", plan["shapes"]), ("connectors", plan["connectors"])):
            for item in collection:
                key = item["key"]
                if key in state["items"]:
                    continue
                body = dict(item["body"])
                if endpoint == "connectors":
                    body.update({"startItem": {"id": state["items"][item["source"]], "snapTo": "auto"},
                                 "endItem": {"id": state["items"][item["target"]], "snapTo": "auto"}})
                for attempt in range(4):
                    state["pending"] = {"key": key, "endpoint": endpoint}
                    save_json(state_path, state)
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
                        save_json(state_path, state)
                        break
                    if 400 <= status < 500 and status != 408:
                        state["pending"] = None
                        save_json(state_path, state)
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


def resolve(state_path, item_id=None, absent=False):
    if bool(item_id) == bool(absent):
        raise TraceError("Choose an existing remote item ID or --absent")
    state_path = Path(state_path)
    with state_path.with_suffix(".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("Publisher is running") from None
        state = read_json(state_path)
        if not state.get("pending"):
            raise TraceError("No pending item to reconcile")
        if item_id:
            if not isinstance(item_id, str) or not item_id.strip() or "/" in item_id:
                raise TraceError("Provide the remote item ID, not a URL")
            if state.get("schema_version") == 2:
                if item_id in {record["id"] for record in state["items"].values()}:
                    raise TraceError("That remote item ID is already mapped; inspect the pending item again")
                state["items"][state["pending"]["key"]] = _record_pending(state["pending"], item_id)
            else:
                state["items"][state["pending"]["key"]] = item_id
        state["pending"] = None
        save_json(state_path, state)
