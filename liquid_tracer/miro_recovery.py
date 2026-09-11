"""Explicit recovery of an interrupted initial publication to an empty board.

An empty read cannot prove that a previous uncertain POST will never complete.
The investigator must first inspect the board and explicitly confirm absence.
This command then checks the same board, changes only the local creation
journal, and selects individual shape requests for the subsequent retry.
"""

import fcntl
import json
import math
import os
import re
import urllib.parse
from contextlib import ExitStack
from pathlib import Path

from .api import http
from .common import TraceError, now
from .miro import _load_sync_state, _namespace, _SyncProgress
from .miro_http import MiroHTTP
from .miro_quota import SharedMiroQuota
from .miro_requests import MiroRequests
from .miro_state import SyncState


def initial_pending_batch(state):
    """Limit bulk absence confirmation to one intact initial shape operation."""
    if (not isinstance(state, dict) or state.get("schema_version") != 2
            or not isinstance(state.get("items"), dict) or not isinstance(state.get("runs"), dict)):
        raise TraceError("Empty-board recovery requires intact schema 2 sync state.")
    if state["items"] or state["runs"] or state.get("latest_run_id") is not None:
        raise TraceError("Empty-board recovery requires an initial publication with no mapped items or completed runs; use miro-resolve for individual items.")
    if state.get("pending") is not None:
        raise TraceError("Empty-board recovery cannot clear a legacy pending item; use miro-resolve.")
    for field in ("pending_updates", "pending_deletions", "pending_frame_deletions"):
        if state.get(field, {}) != {}:
            raise TraceError("Empty-board recovery cannot clear other pending Miro operations; preserve the sync state.")
    entries = state.get("pending_creations", {})
    if not isinstance(entries, dict) or not 1 <= len(entries) <= 20:
        raise TraceError("Empty-board recovery requires one pending initial shape batch of at most 20 items.")
    run_id = state.get("active_run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise TraceError("The pending Miro batch has no valid active run; preserve the sync state.")
    operations = set()
    for key, entry in entries.items():
        if (not isinstance(key, str) or not key or not isinstance(entry, dict)
                or entry.get("key") != key or not isinstance(entry.get("body"), dict)):
            raise TraceError("Malformed pending Miro shape; preserve the sync state.")
        operation = entry.get("operation_id")
        if (entry.get("endpoint") != "shapes" or entry.get("run_id") != run_id
                or not isinstance(operation, str) or not re.fullmatch(r"[0-9a-f]{24}", operation)):
            raise TraceError("Empty-board recovery requires one initial shape operation for the active run; use miro-resolve for individual items.")
        operations.add(operation)
        body = entry["body"]
        data, position, geometry = (body.get(name) for name in ("data", "position", "geometry"))
        if (not isinstance(data, dict) or not isinstance(data.get("shape"), str) or not data["shape"]
                or not isinstance(data.get("content"), str) or not isinstance(position, dict)
                or not isinstance(geometry, dict) or not isinstance(body.get("style", {}), dict)):
            raise TraceError("Malformed pending Miro shape; preserve the sync state.")
        values = [position.get("x"), position.get("y"), geometry.get("width"), geometry.get("height")]
        try:
            valid = (all(type(value) in (int, float) and math.isfinite(value) for value in values)
                     and geometry["width"] > 0 and geometry["height"] > 0)
        except (ValueError, OverflowError):
            valid = False
        if not valid:
            raise TraceError("Malformed pending Miro shape geometry; preserve the sync state.")
    if len(operations) != 1:
        raise TraceError("Empty-board recovery cannot clear multiple uncertain Miro operations; use miro-resolve for individual items.")
    history = state.get("recovery_history", [])
    if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
        raise TraceError("Malformed Miro recovery history; preserve the sync state.")
    return entries


def _verify_empty(result, collection):
    status, _, raw = result
    if not 200 <= status < 300:
        raise TraceError("Miro empty-board check returned HTTP " + str(status) + "; pending items remain unchanged.")
    try:
        body = json.loads(raw)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise TraceError("Miro empty-board check returned invalid JSON; pending items remain unchanged.") from None
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise TraceError("Miro empty-board check is incomplete; pending items remain unchanged.")
    if body["data"]:
        raise TraceError("Miro board contains " + collection + "; empty-board recovery stopped. Inspect the board and use miro-resolve for individual items.")
    cursor, links = body.get("cursor"), body.get("links", {})
    if ((cursor is not None and (not isinstance(cursor, str) or cursor))
            or not isinstance(links, dict) or links.get("next") not in (None, "")
            or any(field in body and (type(body[field]) is not int or body[field] != 0)
                   for field in ("total", "size"))):
        raise TraceError("Miro empty-board check could not verify a complete empty inventory; pending items remain unchanged.")


def recover_empty_board(state_path, board_id, namespace, *, confirmed_empty=False,
                        token=None, transport=http, interval=.02, progress=None):
    """Confirm an empty initial board and atomically release its pending batch.

    Only GET requests are issued. The same state lock excludes local publishers
    throughout validation, remote checks, and the single durable mutation.
    Another person's board edits and delayed server commits are not lockable;
    ``confirmed_empty`` is therefore a required, explicit investigator claim.
    """
    if confirmed_empty is not True:
        raise TraceError("Inspect the Miro board after the failed request, then use --confirm-empty only if it is empty.")
    if (not isinstance(board_id, str) or not board_id or len(board_id) > 200
            or any(character in board_id for character in "/?#")
            or any(character.isspace() for character in board_id)):
        raise TraceError("Provide the Miro board ID, not its full URL.")
    _namespace({"schema_version": 2, "namespace": namespace})
    if progress is not None and not callable(progress):
        raise TraceError("Miro progress must be a callback.")
    state_path = Path(state_path)
    if not state_path.is_file():
        raise TraceError("Miro sync state does not exist; there is no initial publication to recover.")
    status_progress = _SyncProgress(progress)
    with state_path.with_suffix(".lock").open("w") as lock, ExitStack() as resources:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("Another publisher is using this state file; wait for it to finish.") from None
        state = _load_sync_state(state_path, board_id, namespace, allow_pending=True)
        entries = initial_pending_batch(state)
        run_id = state["active_run_id"]
        operation_id = next(iter(entries.values()))["operation_id"]
        history = state.get("recovery_history", [])
        token = token or os.getenv("MIRO_ACCESS_TOKEN")
        if not token:
            raise TraceError("Set MIRO_ACCESS_TOKEN locally (boards:read scope).")
        base = "https://api.miro.com/v2/boards/" + urllib.parse.quote(board_id, safe="")
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"}
        quota = None
        if transport is http:
            quota = resources.enter_context(SharedMiroQuota(token))
            transport = resources.enter_context(MiroHTTP())
        requests = resources.enter_context(MiroRequests(transport, interval=interval, workers=1,
                                                         progress=status_progress, quota=quota))
        status_progress.emit("recovery", 0, 2, "Checking the confirmed-empty Miro board")
        # The official connector client documents a page limit of 10:
        # https://github.com/miroapp/api-clients/blob/main/packages/miro-api/api/apis.ts
        for index, (collection, limit) in enumerate((("items", 1), ("connectors", 10)), start=1):
            _verify_empty(requests.request("GET", base + "/" + collection + "?limit=" + str(limit), headers), collection)
            status_progress.emit("recovery", index, 2, "Checking the confirmed-empty Miro board")
        keys = sorted(entries)
        audit = {"recovery": "confirmed_empty_board", "checked_at": now(), "board_id": board_id,
                 "run_id": run_id, "operation_id": operation_id, "recovered_keys": keys,
                 "confirmed_empty": True, "shape_batch_size": 1}
        with SyncState(state_path, state) as journal:
            journal.commit(sets=[(("shape_batch_size",), 1), (("recovery_history",), [*history, audit])],
                           deletes=[("pending_creations", key) for key in keys])
        return {"recovered_items": len(keys), "shape_batch_size": 1, "board_id": board_id, "run_id": run_id,
                "recovery": "confirmed_empty_board"}
