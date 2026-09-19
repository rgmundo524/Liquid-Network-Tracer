"""Reviewed recovery of one uncertain frame creation on an existing board.

Recovery only reads Miro and changes the local publication journal. A matching
frame is a candidate, not proof of ownership. Absence additionally requires the
investigator's explicit inspection because a delayed POST can still complete.
"""

import copy
import fcntl
import json
import math
import os
import re
from contextlib import ExitStack, contextmanager
from pathlib import Path
from urllib.parse import quote, urlencode

from .api import http
from .common import TraceError, canonical, digest, now
from .miro import _frame_proof, _load_sync_state, _namespace, _record_pending, _SyncProgress
from .miro_errors import read_error
from .miro_http import MiroHTTP
from .miro_quota import SharedMiroQuota
from .miro_requests import MiroRequests
from .miro_state import SyncState, load_state


_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,199}\Z")
_REVIEW = re.compile(r"[0-9a-f]{64}\Z")
MAX_FRAME_PAGES = 200
MAX_UNMAPPED_FRAMES = 1000
MAX_CANDIDATES = 50
_UNCHANGED = "; pending frame remains unchanged."


def _descriptor(body, *, remote=False):
    if not isinstance(body, dict) or (remote and body.get("type") != "frame"):
        raise TraceError("Malformed Miro frame" + _UNCHANGED)
    data, position, geometry = (body.get(field) for field in ("data", "position", "geometry"))
    parent = body.get("parent")
    if (not isinstance(data, dict) or not isinstance(data.get("title"), str)
            or (not remote and not data["title"].strip()) or len(data["title"]) > 10000
            or not isinstance(position, dict) or not isinstance(geometry, dict)
            or position.get("relativeTo") not in (None, "canvas_center")
            or position.get("origin") not in (None, "center")
            or (parent is not None and (not isinstance(parent, dict) or parent.get("id")))
            or any(value is not None and not isinstance(value, str)
                   for value in (data.get("type"), data.get("format")))
            or (not remote and (data.get("type") != "freeform" or data.get("format") != "custom"))
            or not isinstance(body.get("style", {}), dict)):
        raise TraceError("Cannot verify the frame's title and canvas coordinates" + _UNCHANGED)
    values = [position.get("x"), position.get("y"), geometry.get("width"), geometry.get("height")]
    rotation = geometry.get("rotation", 0)
    try:
        valid = (all(type(value) in (int, float) and math.isfinite(value) for value in [*values, rotation])
                 and min(values[2:]) > 0 and rotation % 360 == 0)
    except (ValueError, OverflowError, TypeError):
        valid = False
    if not valid:
        raise TraceError("Cannot verify the frame's geometry" + _UNCHANGED)
    return dict(zip(("title", "x", "y", "width", "height"), [data["title"], *values]))


def pending_frame(state):
    """Require one intact frame intent from the interrupted active run."""
    if (not isinstance(state, dict) or state.get("schema_version") != 2
            or not isinstance(state.get("items"), dict) or not isinstance(state.get("runs"), dict)):
        raise TraceError("Frame recovery requires intact schema 2 sync state.")
    _namespace(state)
    entries = state.get("pending_creations")
    if not isinstance(entries, dict) or len(entries) != 1 or state.get("pending") is not None:
        raise TraceError("Frame recovery requires exactly one uncertain frame creation.")
    for field in ("pending_updates", "pending_deletions", "pending_frame_deletions", "pending_creation_detaches"):
        if not isinstance(state.get(field, {}), dict) or state.get(field):
            raise TraceError("Other Miro operations need recovery first; preserve the sync state.")
    if state.get("address_migration") is not None and state.get("address_migration") != {}:
        raise TraceError("Other Miro operations need recovery first; preserve the sync state.")
    run_id = state.get("active_run_id")
    key, entry = next(iter(entries.items()))
    if (not isinstance(run_id, str) or not run_id.strip() or not isinstance(entry, dict)
            or not isinstance(key, str) or entry.get("key") != key or key in state["items"]
            or entry.get("endpoint") != "frames" or entry.get("run_id") != run_id
            or not isinstance(entry.get("operation_id"), str)
            or re.fullmatch(r"[0-9a-f]{24}", entry["operation_id"]) is None
            or entry.get("frame_proof") != _frame_proof(key)):
        raise TraceError("Malformed pending Miro frame; preserve the sync state.")
    _descriptor(entry.get("body"))
    history = state.get("recovery_history", [])
    if not isinstance(history, list) or any(not isinstance(value, dict) for value in history):
        raise TraceError("Malformed Miro recovery history; preserve the sync state.")
    return key, entry


@contextmanager
def _session(state_path, board_id, namespace, token, transport, interval, progress):
    if (not isinstance(board_id, str) or not board_id or len(board_id) > 200
            or any(character in board_id for character in "/?#")
            or any(character.isspace() for character in board_id)):
        raise TraceError("Provide the Miro board ID, not its full URL.")
    _namespace({"schema_version": 2, "namespace": namespace})
    if progress is not None and not callable(progress):
        raise TraceError("Miro progress must be a callback.")
    state_path = Path(state_path)
    if not state_path.is_file():
        raise TraceError("Miro sync state does not exist; there is no interrupted frame to recover.")
    token = token or os.getenv("MIRO_ACCESS_TOKEN")
    if not token:
        raise TraceError("Set MIRO_ACCESS_TOKEN locally (boards:read scope).")
    with state_path.with_suffix(".lock").open("a") as lock, ExitStack() as resources:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("Another publisher is using this state file; wait for it to finish.") from None
        state = _load_sync_state(state_path, board_id, namespace, allow_pending=True)
        pending_frame(state)
        base = "https://api.miro.com/v2/boards/" + quote(board_id, safe="")
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        quota = None
        if transport is http:
            quota = resources.enter_context(SharedMiroQuota(token))
            transport = resources.enter_context(MiroHTTP())
        status_progress = _SyncProgress(progress)
        requests = resources.enter_context(MiroRequests(transport, interval=interval, workers=1,
                                                         progress=status_progress, quota=quota))
        yield state_path, state, requests, base, headers, status_progress


def _get(requests, url, headers, endpoint):
    status, response_headers, raw = requests.request("GET", url, headers)
    if status != 200:
        raise TraceError(read_error(status, endpoint, response_headers, raw, headers) + _UNCHANGED)
    try:
        body = json.loads(raw)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise TraceError("Miro frame review returned invalid JSON" + _UNCHANGED) from None
    if not isinstance(body, dict):
        raise TraceError("Miro frame review returned an incomplete response" + _UNCHANGED)
    return body


def _inventory(requests, base, headers, progress):
    frames, cursors, cursor, total = {}, set(), None, None
    progress.emit("frame_recovery", 0, 1, "Checking the complete Miro frame inventory")
    for _ in range(MAX_FRAME_PAGES):
        query = {"type": "frame", "limit": 50, **({"cursor": cursor} if cursor else {})}
        page = _get(requests, base + "/items?" + urlencode(query), headers, "items")
        data, cursor, links = page.get("data"), page.get("cursor"), page.get("links", {})
        if (not isinstance(data, list) or len(data) > 50
                or (cursor is not None and not isinstance(cursor, str)) or not isinstance(links, dict)):
            raise TraceError("Malformed Miro frame inventory" + _UNCHANGED)
        cursor = cursor or None
        if "size" in page and (type(page["size"]) is not int or page["size"] != len(data)):
            raise TraceError("Miro frame inventory is incomplete" + _UNCHANGED)
        if "total" in page:
            reported = page["total"]
            if type(reported) is not int or reported < 0 or (total is not None and reported != total):
                raise TraceError("Miro frame inventory changed during review" + _UNCHANGED)
            total = reported
        for item in data:
            if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                    or _ID.fullmatch(item["id"]) is None or item["id"] in frames
                    or item.get("type") != "frame" or item.get("isSupported") is False):
                raise TraceError("Incomplete or changing Miro frame inventory" + _UNCHANGED)
            frames[item["id"]] = {field: copy.deepcopy(item[field]) for field in
                                  ("id", "type", "data", "position", "geometry", "style", "parent") if field in item}
        if cursor is None:
            if links.get("next") not in (None, "") or (total is not None and total != len(frames)):
                raise TraceError("Miro frame inventory did not reconcile" + _UNCHANGED)
            return frames
        if cursor in cursors or not data or (total is not None and len(frames) >= total):
            raise TraceError("Miro frame pagination did not complete" + _UNCHANGED)
        cursors.add(cursor)
    raise TraceError("Miro frame review exceeded its read budget; no partial inventory is accepted" + _UNCHANGED)


def _same_bounds(first, second):
    # An absolute tolerance accommodates normal decimal serialization without
    # becoming permissive when an investigation uses very large coordinates.
    return all(math.isclose(first[field], second[field], rel_tol=0, abs_tol=.01)
               for field in ("x", "y", "width", "height"))


def _review(state, requests, base, headers, progress):
    _, entry = pending_frame(state)
    expected = _descriptor(entry["body"])
    inventory = _inventory(requests, base, headers, progress)
    mapped = {record["id"] for record in state["items"].values()}
    unmapped = sorted(set(inventory) - mapped)
    if len(unmapped) > MAX_UNMAPPED_FRAMES:
        raise TraceError("Too many unmapped frames for a complete recovery review" + _UNCHANGED)
    candidates, potential, records = [], 0, {}
    for index, item_id in enumerate(unmapped, 1):
        progress.emit("frame_recovery", index - 1, len(unmapped), "Reading unmapped Miro frames")
        body = _get(requests, base + "/frames/" + quote(item_id, safe=""), headers, "frames")
        if body.get("id") != item_id:
            raise TraceError("Miro frame review returned the wrong item ID" + _UNCHANGED)
        descriptor = _descriptor(body, remote=True)
        # These fields capture manual edits relevant to adopting an object.
        # Response links and other transport metadata are deliberately excluded.
        records[item_id] = {field: copy.deepcopy(body[field]) for field in
                            ("id", "type", "data", "position", "geometry", "style", "parent") if field in body}
        title_matches = descriptor["title"] == expected["title"]
        bounds_match = _same_bounds(descriptor, expected)
        if title_matches and bounds_match:
            candidates.append({"id": item_id, **descriptor})
            if len(candidates) > MAX_CANDIDATES:
                raise TraceError("Too many matching frames for an unambiguous recovery review" + _UNCHANGED)
        elif title_matches or bounds_match:
            potential += 1
    progress.emit("frame_recovery", 1, 1, "Frame recovery review is ready")
    review_id = digest(canonical({"state": state, "inventory": inventory, "unmapped_frames": records}))
    return {"schema_version": 1, "recovery": "pending_frame_review", "review_id": review_id,
            "run_id": entry["run_id"], "pending_frame": expected, "candidates": candidates,
            "potential_match_count": potential, "can_confirm_absent": not candidates and potential == 0}, records


def review_pending_frame(state_path, board_id, namespace, *, token=None, transport=http, interval=.02, progress=None):
    """Read a complete review without changing the board or publication state."""
    with _session(state_path, board_id, namespace, token, transport, interval, progress) as session:
        _, state, requests, base, headers, status = session
        report, _ = _review(state, requests, base, headers, status)
        return report


def recover_pending_frame(state_path, board_id, namespace, *, review_id, item_id=None, confirmed_absent=False,
                          token=None, transport=http, interval=.02, progress=None):
    """Recheck an explicit selection, then atomically resolve local intent only."""
    if not isinstance(review_id, str) or _REVIEW.fullmatch(review_id) is None:
        raise TraceError("Review the pending Miro frame before applying recovery.")
    if (type(confirmed_absent) is not bool or bool(item_id is not None) == confirmed_absent
            or (item_id is not None and (not isinstance(item_id, str) or _ID.fullmatch(item_id) is None))):
        raise TraceError("Choose one reviewed frame or explicitly confirm the pending frame is absent.")
    with _session(state_path, board_id, namespace, token, transport, interval, progress) as session:
        path, state, requests, base, headers, status = session
        review, records = _review(state, requests, base, headers, status)
        if review["review_id"] != review_id:
            raise TraceError("The Miro frame review changed; review the current board again" + _UNCHANGED)
        if item_id is not None and item_id not in {candidate["id"] for candidate in review["candidates"]}:
            raise TraceError("Select an unmapped frame from the current reviewed candidates" + _UNCHANGED)
        if confirmed_absent and not review["can_confirm_absent"]:
            raise TraceError("A possible frame exists; inspect it before resolving the pending creation" + _UNCHANGED)
        # The sync lock cannot protect against someone replacing a state file
        # outside this program. Do not commit against a changed snapshot.
        if load_state(path) != state:
            raise TraceError("The Miro sync state changed during review" + _UNCHANGED)
        key, entry = pending_frame(state)
        recovery = "adopted_frame" if item_id else "confirmed_absent_frame"
        audit = {"recovery": recovery, "checked_at": now(), "board_id": board_id,
                 "run_id": entry["run_id"], "operation_id": entry["operation_id"],
                 "recovered_keys": [key], "review_id": review_id, "confirmed_absent": confirmed_absent}
        sets = []
        if item_id:
            audit["item_id"] = item_id
            sets.append((("items", key), _record_pending(entry, item_id, records[item_id])))
        sets.append((("recovery_history",), [*state.get("recovery_history", []), audit]))
        with SyncState(path, state) as journal:
            journal.commit(sets=sets, deletes=[("pending_creations", key)])
        return {"recovery": recovery, "run_id": entry["run_id"], "resolved_count": 1, "remaining_pending": 0}
