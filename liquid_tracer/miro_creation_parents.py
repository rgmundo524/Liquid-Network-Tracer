"""Normalize automatically parented creations and durably detach only those shapes.

Creation omits optional parent fields. If Miro attaches a new shape to a frame,
we first establish its identity in canvas coordinates, save its acknowledged ID,
and then detach it without moving its visible position. A failed detach can be
resumed by reads, without repeating any creation request.
"""

import copy
import json
import math
import urllib.parse

from .common import TraceError
from .miro_reads import _frame_geometry


_TOLERANCE = .01


def _position(body):
    try:
        position = body["position"]
        if not isinstance(position, dict) or position.get("origin", "center") != "center":
            raise ValueError
        values = [position[axis] for axis in ("x", "y")]
        if any(isinstance(value, bool) for value in values):
            raise ValueError
        x, y = map(float, values)
        if not all(math.isfinite(value) for value in (x, y)):
            raise ValueError
        return x, y
    except (KeyError, TypeError, ValueError, OverflowError):
        raise TraceError("Miro created shape has invalid coordinates; keep the sync state and reconcile its creation") from None


def _parent(body):
    parent = body.get("parent")
    if parent is None:
        return None
    if not isinstance(parent, dict):
        raise TraceError("Miro created shape has an invalid parent; keep the sync state and reconcile its creation")
    item_id = parent.get("id")
    if item_id is not None and (not isinstance(item_id, str) or not item_id or len(item_id) > 200
                                or any(ord(char) < 33 or ord(char) > 126 or char in "/?#" for char in item_id)):
        raise TraceError("Miro created shape has an invalid parent; keep the sync state and reconcile its creation")
    return item_id


def _read(requests, base, headers, endpoint, item_id):
    status, _, raw = requests.request("GET", base + "/" + endpoint + "/" + urllib.parse.quote(item_id, safe=""), headers)
    if not 200 <= status < 300:
        raise TraceError(f"Miro creation-parent read returned HTTP {status}; acknowledged progress is saved; retry sync")
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or body.get("id") != item_id:
            raise ValueError
        return body
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise TraceError("Miro creation-parent read returned an invalid item; keep the sync state and retry sync") from None


def _canvas(body, requests, base, headers, frames):
    x, y = _position(body)
    parent = _parent(body)
    relative = body["position"].get("relativeTo")
    if relative == "canvas_center" or (relative is None and parent is None):
        return x, y
    if parent is None or relative != "parent_top_left":
        raise TraceError("Miro created shape has unsupported parent coordinates; keep the sync state and reconcile its creation")
    if parent not in frames:
        frame = _read(requests, base, headers, "frames", parent)
        try:
            frames[parent] = _frame_geometry(frame, parent)
        except TraceError:
            raise TraceError("Miro creation parent is not a supported canvas frame; keep the sync state and reconcile its creation") from None
    frame_x, frame_y, width, height = frames[parent]
    result = frame_x - width / 2 + x, frame_y - height / 2 + y
    if not all(math.isfinite(value) for value in result):
        raise TraceError("Miro created shape has invalid canvas coordinates; keep the sync state and reconcile its creation")
    return result


def normalize_created_shapes(returned, requests, base, headers):
    """Return matching bodies and detach descriptors keyed by remote ID.

    No board or local state changes happen here. The caller must verify every
    unique response assignment before adopting any of the returned IDs.
    """
    frames, normalized, detaches = {}, [], {}
    for body in returned:
        if not isinstance(body, dict):
            raise TraceError("Miro returned an invalid created shape; reconcile the pending items")
        parent = _parent(body)
        if parent is None:
            normalized.append(body)
            continue
        item_id = body.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise TraceError("Miro returned an invalid created shape ID; reconcile the pending items")
        try:
            x, y = _canvas(body, requests, base, headers, frames)
        except TraceError as error:
            raise TraceError(str(error) + "; reconcile the pending creations before retrying") from None
        value = copy.deepcopy(body)
        value["position"] = {"x": x, "y": y, "origin": "center", "relativeTo": "canvas_center"}
        value["parent"] = {"id": None}
        normalized.append(value)
        detaches[item_id] = {"id": item_id, "parent_id": parent, "position": {"x": x, "y": y}}
    return normalized, detaches


def validate_creation_detaches(state):
    pending = state.get("pending_creation_detaches", {})
    if not isinstance(pending, dict):
        raise TraceError("Malformed Miro creation-detach journal; restore its last intact version")
    for key, entry in pending.items():
        record = state["items"].get(key)
        try:
            if (not isinstance(key, str) or not isinstance(entry, dict)
                    or set(entry) != {"id", "parent_id", "position"}
                    or not record or record["endpoint"] != "shapes" or entry["id"] != record["id"]
                    or not isinstance(entry["parent_id"], str) or not entry["parent_id"]
                    or not isinstance(entry["position"], dict) or set(entry["position"]) != {"x", "y"}):
                raise ValueError
            if any(type(value) not in (int, float) or not math.isfinite(value)
                   for value in entry["position"].values()):
                raise ValueError
            _parent({"parent": {"id": entry["parent_id"]}})
            _position(entry)
        except (KeyError, ValueError, TypeError, OverflowError, TraceError):
            raise TraceError("Malformed Miro creation-detach journal; restore its last intact version") from None


def finish_creation_detaches(state, journal, requests, base, headers):
    """Read before retrying a detach, protecting edits and acknowledged IDs."""
    for key, entry in list(state.get("pending_creation_detaches", {}).items()):
        actual = _read(requests, base, headers, "shapes", entry["id"])
        if actual.get("type") != "shape":
            raise TraceError("Miro creation detach did not find the acknowledged shape; keep the sync state and retry sync")
        parent = _parent(actual)
        if parent not in (None, entry["parent_id"]):
            raise TraceError("A newly created Miro shape was moved to another parent. Restore its original position and parent "
                             "before retrying sync; acknowledged items remain saved")
        point = _canvas(actual, requests, base, headers, {})
        if any(abs(point[index] - entry["position"][axis]) > _TOLERANCE for index, axis in enumerate(("x", "y"))):
            raise TraceError("A newly created Miro shape or its frame was moved before detaching. Restore its original position "
                             "before retrying sync; acknowledged items remain saved")
        if parent is not None:
            payload = {"parent": {"id": None}, "position": {**entry["position"], "origin": "center"}}
            status, _, _ = requests.request("PATCH", base + "/shapes/" + urllib.parse.quote(entry["id"], safe=""), headers, payload)
            if not 200 <= status < 300:
                raise TraceError(f"Miro creation detach returned HTTP {status}; acknowledged items remain saved; retry sync")
            # Read back the server's result instead of trusting a partial PATCH
            # response. A failed read keeps the descriptor for the next sync.
            result = _read(requests, base, headers, "shapes", entry["id"])
            point = _canvas(result, requests, base, headers, {})
            if result.get("type") != "shape" or _parent(result) is not None or any(
                    abs(point[index] - entry["position"][axis]) > _TOLERANCE for index, axis in enumerate(("x", "y"))):
                raise TraceError("Miro did not confirm the created shape on the canvas; acknowledged items remain saved; retry sync")
        journal.commit(deletes=[("pending_creation_detaches", key)])
