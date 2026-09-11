"""Read-only Miro preflight with complete connector pages and safe fallbacks.

Miro's /items collection returns GenericItem summaries without style, so shape
reads deliberately use /shapes/{id}. The connector collection returns the same
ConnectorWithLinks model as the individual endpoint. A partial collection body
must never be mistaken for a user deleting a managed field.
"""

import copy
import json
import math
import urllib.parse

from .common import TraceError


PAGE_SIZE = 50


def _frame_geometry(body, key):
    """Frames are canvas items; unsupported nested/rotated frames stay blocked."""
    try:
        position, geometry = body["position"], body["geometry"]
        parent = body.get("parent")
        if (body.get("type") != "frame" or not isinstance(position, dict)
                or not isinstance(geometry, dict)
                or (parent is not None and not isinstance(parent, dict))
                or (parent or {}).get("id")
                or position.get("relativeTo") not in (None, "canvas_center")
                or position.get("origin") not in (None, "center")):
            raise ValueError
        values = [position["x"], position["y"], geometry["width"], geometry["height"],
                  geometry.get("rotation", 0)]
        if any(isinstance(value, bool) for value in values):
            raise ValueError
        x, y, width, height, rotation = map(float, values)
        if not all(math.isfinite(value) for value in (x, y, width, height, rotation)):
            raise ValueError
        if min(width, height) <= 0 or rotation % 360 != 0:
            raise ValueError
        return x, y, width, height
    except (KeyError, TypeError, ValueError, OverflowError):
        raise TraceError("Cannot read supported canvas geometry for Miro frame " + key +
                         "; restore its position and geometry before syncing. No board writes made.") from None


def _canvas_positions(items, remote):
    """Resolve only verified managed frame parents, retaining their live positions.

    Miro reports a child's center relative to its parent's top-left corner.
    Unknown parents remain untouched so the existing layout validation refuses
    to guess their canvas location. Frames themselves cannot be REST children.
    """
    frames = {}
    for key, record in items.items():
        if record["endpoint"] == "frames" and key in remote:
            frames[record["id"]] = _frame_geometry(remote[key], key)
    managed_frames = {record["id"] for record in items.values() if record["endpoint"] == "frames"}
    for key, record in items.items():
        if record["endpoint"] != "shapes" or key not in remote:
            continue
        body = remote[key]
        parent, position = body.get("parent"), body.get("position", {})
        if parent is not None and not isinstance(parent, dict):
            raise TraceError("Miro shape " + key + " has malformed frame/group coordinates; no board writes made.")
        if not isinstance(position, dict):
            raise TraceError("Miro shape " + key + " has malformed coordinates; no board writes made.")
        parent_id = (parent or {}).get("id")
        if parent_id is not None and not isinstance(parent_id, str):
            raise TraceError("Miro shape " + key + " has malformed frame/group coordinates; no board writes made.")
        # An explicit canvas location needs no parent inference, including for
        # manually maintained external frames. The core validator checks size.
        if position.get("relativeTo") == "canvas_center":
            if parent_id in managed_frames:
                converted = copy.deepcopy(body)
                converted["_frame_source_position"] = copy.deepcopy(position)
                remote[key] = converted
            continue
        if parent_id not in managed_frames:
            continue
        if (parent_id not in frames or position.get("relativeTo") != "parent_top_left"
                or position.get("origin") not in (None, "center")):
            raise TraceError("Miro shape " + key + " has incomplete managed-frame coordinates; no board writes made.")
        try:
            if any(isinstance(position[axis], bool) for axis in ("x", "y")):
                raise ValueError
            x, y = float(position["x"]), float(position["y"])
            frame_x, frame_y, width, height = frames[parent_id]
            x, y = frame_x - width / 2 + x, frame_y - height / 2 + y
            if not all(math.isfinite(value) for value in (x, y)):
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            raise TraceError("Miro shape " + key + " has invalid managed-frame coordinates; no board writes made.") from None
        converted = copy.deepcopy(body)
        converted["_frame_source_position"] = copy.deepcopy(position)
        converted["position"] = {**position, "x": x, "y": y, "origin": "center", "relativeTo": "canvas_center"}
        remote[key] = converted


def validate_frame_children(requests, base, headers, state, remote, frame_records):
    """Prove every attached child can be safely detached by the sync engine.

    /items summaries provide identity, not complete editable shape fields. The
    full shape reads performed earlier must confirm the same parent. Both sides
    of that inventory are checked before the caller performs any board writes.
    """
    mapped = {record["id"]: (key, record) for key, record in state["items"].items()}
    expected = {record["id"]: set() for record in frame_records.values()}
    for key, record in state["items"].items():
        if record["endpoint"] == "shapes" and key in remote:
            parent_id = (remote[key].get("parent") or {}).get("id")
            if parent_id in expected:
                expected[parent_id].add(record["id"])

    def read(job):
        key, record = job
        frame_id = record["id"]
        cursor, cursors, seen = None, set(), {}
        while True:
            query = {"parent_item_id": frame_id, "limit": PAGE_SIZE}
            if cursor is not None:
                query["cursor"] = cursor
            status, _, raw = requests.request("GET", base + "/items?" + urllib.parse.urlencode(query), headers)
            if not 200 <= status < 300:
                raise TraceError("Miro frame child preflight returned HTTP " + str(status) + "; no board writes made.")
            body = _response(raw, "frame child preflight")
            data, cursor = body.get("data"), body.get("cursor")
            if not isinstance(data, list) or (cursor is not None and not isinstance(cursor, str)):
                raise TraceError("Miro frame child preflight is malformed; no board writes made.")
            cursor = cursor or None
            if cursor:
                if cursor in cursors or not data:
                    raise TraceError("Miro frame child pagination could not verify a complete inventory; no board writes made.")
                cursors.add(cursor)
            for child in data:
                if not isinstance(child, dict) or not isinstance(child.get("id"), str) or not child["id"]:
                    raise TraceError("Miro frame child preflight contains an invalid item; no board writes made.")
                child_id = child["id"]
                if child_id in seen:
                    if child != seen[child_id]:
                        raise TraceError("Miro frame children changed between pages; no board writes made. Retry sync.")
                    continue
                seen[child_id] = child
                child_key, child_record = mapped.get(child_id, (None, {}))
                if child_record.get("endpoint") != "shapes" or child.get("type") != "shape":
                    raise TraceError("Miro frame " + key + " contains an item outside this trace. Move that item to the "
                                     "board canvas before syncing so its frame can be updated safely. No board writes made.")
                actual = remote.get(child_key)
                if (not actual or (actual.get("parent") or {}).get("id") != frame_id
                        or actual.get("position", {}).get("relativeTo") != "canvas_center"):
                    raise TraceError("Miro frame child positions changed or could not be verified; no board writes made. Retry sync.")
            if cursor is None:
                break
        if set(seen) != expected[frame_id]:
            raise TraceError("Miro frame child inventory disagrees with the current shape positions; no board writes made. Retry sync.")

    requests.map(frame_records.items(), read, lambda _job, _result: None)


def check_empty_frames(requests, base, headers, frame_records):
    """Verify managed frames can be changed without moving or deleting children.

    Geometric export frames are deliberately not assigned parent/child links.
    Recheck after the sync engine detaches any verified managed children, before
    changing frames. A concurrent manual attachment must never be deleted or
    moved as a side effect of changing a generated export frame.
    """
    def read(job):
        _, record = job
        query = urllib.parse.urlencode({"parent_item_id": record["id"], "limit": 1})
        return requests.request("GET", base + "/items?" + query, headers)

    def accept(job, result):
        key, _ = job
        status, _, raw = result
        if not 200 <= status < 300:
            raise TraceError("Miro frame child check returned HTTP " + str(status) +
                             ". Acknowledged sync progress is saved; retry sync.")
        body = _response(raw, "frame child check", "acknowledged sync progress is saved; retry sync")
        data, cursor = body.get("data"), body.get("cursor")
        if not isinstance(data, list) or (cursor is not None and not isinstance(cursor, str)):
            raise TraceError("Miro frame child check is malformed. Acknowledged sync progress is saved; retry sync.")
        if data:
            raise TraceError("Miro frame " + key + " contains attached items. Move its contents to the board canvas "
                             "before syncing so the frame can be updated without moving or deleting those items. "
                             "Acknowledged sync progress is saved; retry sync after moving the remaining children to the canvas.")
        if cursor or body.get("total", 0) != 0:
            raise TraceError("Miro frame child check could not verify an empty frame. "
                             "Acknowledged sync progress is saved; retry sync.")

    requests.map(frame_records.items(), read, accept)


def _response(raw, operation, failure="no board writes made"):
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError
        return body
    except (TypeError, ValueError):
        raise TraceError("Miro returned invalid JSON for " + operation + "; " + failure) from None


def _complete_connector(body, record, pending_update):
    """Only use list bodies that contain every field needed for safe merging."""
    if body.get("type") not in (None, "connector") or not isinstance(body.get("shape"), str):
        return False
    if body.get("isSupported") is False or not isinstance(body.get("style"), dict):
        return False
    for field in ("startItem", "endItem"):
        connection = body.get(field)
        if not isinstance(connection, dict) or not isinstance(connection.get("id"), str):
            return False
    captions = body.get("captions")
    if not isinstance(captions, list):
        return False
    if any(not isinstance(caption, dict) or not isinstance(caption.get("content"), str)
           or "position" not in caption for caption in captions):
        return False
    expected_style = set()
    for original in (record.get("managed", {}), record.get("intent", {}), pending_update.get("patch", {})):
        expected_style.update(original.get("style", {}))
    return expected_style <= body["style"].keys()


def preflight(requests, base, headers, state, removals, progress=None):
    """Return mapped, live bodies before the caller performs any board writes.

    Paginate when more than one page of connectors is mapped. Missing or partial
    list results are checked individually, including permission/deletion cases.
    Page links are never followed: only an encoded cursor is sent to the same
    authenticated Miro endpoint. Shapes and the first connector page overlap.
    All acceptance and progress callbacks run on the request coordinator.
    """
    items = state["items"]
    total = len(items)
    checked = 0
    remote, missing = {}, []

    def emit():
        if progress is not None:
            progress.emit("preflight", checked, total, "Checking existing board items")

    emit()
    connectors = {record["id"]: (key, record) for key, record in items.items()
                  if record["endpoint"] == "connectors"}
    paginate = len(connectors) > PAGE_SIZE
    unseen = set(connectors) if paginate else set()
    seen_bodies = {}
    seen_cursors = set()
    next_cursor = None

    def request_page(cursor):
        query = {"limit": PAGE_SIZE}
        if cursor is not None:
            query["cursor"] = cursor
        url = base + "/connectors?" + urllib.parse.urlencode(query)
        return requests.request("GET", url, headers)

    def accept_page(result):
        nonlocal checked, next_cursor
        status, _, raw = result
        if not 200 <= status < 300:
            raise TraceError("Miro preflight connector list returned HTTP " + str(status) + "; no board writes made")
        body = _response(raw, "preflight connector list")
        data = body.get("data")
        if not isinstance(data, list):
            raise TraceError("Miro preflight connector list is malformed; no board writes made")
        cursor = body.get("cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise TraceError("Miro preflight connector cursor is malformed; no board writes made")
        cursor = cursor or None
        if cursor is not None:
            if cursor in seen_cursors:
                raise TraceError("Miro preflight connector pagination repeated a cursor; no board writes made")
            seen_cursors.add(cursor)
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                raise TraceError("Miro preflight connector list contains an invalid item; no board writes made")
            item_id = item["id"]
            if item_id in seen_bodies:
                if item != seen_bodies[item_id]:
                    raise TraceError("Miro preflight connector changed between pages; no board writes made. Retry sync.")
                continue
            # Only mapped items affect the preflight. Retain their bodies to
            # detect inconsistent repeated IDs without retaining unrelated work.
            if item_id not in connectors:
                continue
            if item.get("isSupported") is False:
                raise TraceError("Miro preflight found an unsupported mapped connector; no board writes made")
            seen_bodies[item_id] = item
            unseen.discard(item_id)
            key, record = connectors[item_id]
            if _complete_connector(item, record, state.get("pending_updates", {}).get(key, {})):
                remote[key] = item
                checked += 1
        # An empty page cannot advance a useful snapshot. Verify outstanding
        # IDs directly instead of trusting an unbounded sequence of empty pages.
        next_cursor = cursor if unseen and data else None
        emit()

    def read_item(job):
        key, record = job
        if key is None:
            return request_page(None)
        url = base + "/" + record["endpoint"] + "/" + urllib.parse.quote(record["id"], safe="")
        return requests.request("GET", url, headers)

    def accept_read(job, result):
        nonlocal checked
        key, record = job
        if key is None:
            accept_page(result)
            return
        status, _, raw = result
        checked += 1
        if status == 404:
            if record["endpoint"] == "frames":
                pending = state.get("pending_frame_deletions", {}).get(key, {})
                attempted = (pending.get("attempted") is True and pending.get("id") == record["id"]
                             and pending.get("proof") == removals.get(key))
            else:
                attempted = state.get("pending_deletions", {}).get(key, {}).get("attempted")
            if not (key in removals and attempted):
                missing.append(key + " (" + record["id"] + ")")
        elif not 200 <= status < 300:
            raise TraceError("Miro preflight GET returned HTTP " + str(status) + "; no board writes made")
        else:
            item = _response(raw, "preflight GET")
            if item.get("id") != record["id"]:
                raise TraceError("Miro preflight returned the wrong item ID; no board writes made")
            if record["endpoint"] == "connectors" and item.get("isSupported") is False:
                raise TraceError("Miro preflight found an unsupported mapped connector; no board writes made")
            remote[key] = item
        emit()

    def initial_jobs():
        if paginate:
            yield (None, None)
        yield from ((key, record) for key, record in items.items()
                    if not paginate or record["endpoint"] != "connectors")

    requests.map(initial_jobs(), read_item, accept_read)
    while next_cursor is not None:
        accept_page(request_page(next_cursor))
    if paginate:
        requests.map(((key, record) for key, record in connectors.values() if key not in remote),
                     read_item, accept_read)
    if missing:
        raise TraceError("Miro preflight found missing or inaccessible mapped items: " + ", ".join(missing) +
                         ". No board writes made. Restore the items/access or repair the mapping; they will not be recreated automatically.")
    _canvas_positions(items, remote)
    return remote
