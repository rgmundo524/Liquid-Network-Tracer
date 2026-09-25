"""An investigation's named Miro boards and their durable plot bindings.

Board creation and publication are independent. Old mappings are discovered
without migration; new boards each keep an isolated, reusable sync mapping.
"""

import copy
import fcntl
import json
import os
import re
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from urllib.parse import quote

from .api import http
from .boards import BOARDS_URL, board_options
from .common import TraceError, canonical, digest, now, read_json, save_json
from .investigations import read_case
from .miro_state import load_state, journal_path

GOALS = {"full": "Full trace", "connections": "Starter connections", "pegouts": "Peg-outs"}
RECORD_ID = re.compile(r"board-[0-9a-f]{32}\Z")
UNCERTAIN = ("Miro board creation outcome is uncertain. Inspect your Miro boards and link the created "
             "board to this pending board entry. No additional board will be created automatically.")
PEGOUT_ADDRESS_NOTICE = (
    "This board uses the older per-output address layout and can still sync compatible saved layouts. "
    "For one node per address, generate a new Paths to peg-outs layout in Plot Layouts, then create "
    "or link a different Miro board for it in Miro Boards. The existing board stays unchanged.")


def _url(board):
    return "https://miro.com/app/board/" + quote(board, safe="") + "/" if board else None


def _goal(goal):
    if goal not in GOALS:
        raise TraceError("Choose full, connections, or pegouts as the board goal")
    return goal


def _paths(case):
    case = Path(case)
    metadata = read_case(case)
    return case, metadata, case / "miro" / "boards.json"


def _read(path, metadata):
    if not path.exists():
        return {"schema_version": 1, "case_id": metadata["case_id"], "boards": []}
    try:
        data = read_json(path)
        if (not isinstance(data, dict) or data.get("schema_version") != 1
                or data.get("case_id") != metadata["case_id"] or not isinstance(data.get("boards"), list)):
            raise ValueError
        ids, targets = set(), set()
        for record in data["boards"]:
            if (not isinstance(record, dict) or not isinstance(record.get("id"), str)
                    or not RECORD_ID.fullmatch(record["id"]) or record["id"] in ids
                    or record.get("goal") not in GOALS or not isinstance(record.get("name"), str)
                    or record.get("status") not in ("pending_creation", "creation_rejected", "linked", "synced", "syncing", "sync_error")
                    or record.get("state_file") != "miro/boards/" + record["id"] + ".json"):
                raise ValueError
            ids.add(record["id"])
            target = record.get("board_id")
            if target:
                from .cli import board_id
                if board_id(target) != target or target in targets:
                    raise ValueError
                targets.add(target)
        return data
    except (ValueError, TypeError, OSError, KeyError, TraceError):
        raise TraceError("Invalid investigation board registry; restore miro/boards.json before changing boards") from None


@contextmanager
def _lock(case, *, trace=False):
    with ExitStack() as stack:
        names = [("trace.lock", fcntl.LOCK_SH)] if trace else []
        names += [("case.lock", fcntl.LOCK_SH), ("boards.lock", fcntl.LOCK_EX)]
        for name, mode in names:
            handle = stack.enter_context((Path(case) / name).open("a"))
            try:
                fcntl.flock(handle, mode | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TraceError("Investigation data, settings, or boards are busy; try after that operation finishes") from None
        yield


def _pending(state):
    return sum(len(state.get(key, {})) for key in (
        "pending_creations", "pending_updates", "pending_deletions", "pending_frame_deletions",
        "pending_creation_detaches")) + int(bool(state.get("pending")))


def _display(case, record):
    result = {**copy.deepcopy(record), "board_url": _url(record.get("board_id")),
              "legacy_snapshot": bool(record.get("legacy_snapshot")),
              "can_sync": bool(record.get("board_id")) and not record.get("legacy_snapshot", False),
              "preview_id": record.get("preview_id"), "run_id": record.get("run_id"), "pending_count": 0}
    path = case / record["state_file"]
    if path.exists() or journal_path(path).exists():
        try:
            state = load_state(path)
            if state.get("board_id") != record.get("board_id"):
                raise TraceError("Board ID disagrees with mapping")
            result["run_id"] = state.get("latest_run_id", record.get("run_id"))
            result["pending_count"] = _pending(state)
            if result["pending_count"] or state.get("active_run_id"):
                result["status"] = "interrupted"
                result["notice"] = "Resume the saved plot, or reconcile uncertain Miro item creation before retrying."
            elif state.get("latest_run_id") or state.get("items"):
                attempted_hash = record.get("sync_plan_sha256")
                completed = attempted_hash in state.get("runs", {}).get(record.get("run_id"), {}).get("plan_sha256s", [])
                if record.get("status") not in ("syncing", "sync_error") or completed:
                    result["status"] = "archived_snapshot" if result["legacy_snapshot"] else "synced"
            namespace = state.get("namespace")
            if (record["goal"] == "pegouts" and not record.get("legacy") and not result.get("notice")
                    and isinstance(namespace, dict)
                    and str(namespace.get("case_id", "")).endswith(":" + record["id"])
                    and namespace.get("address_mode") == "outpoint_occurrences"):
                result["notice"] = PEGOUT_ADDRESS_NOTICE
        except (TraceError, OSError, ValueError, TypeError):
            result.update(status="mapping_error", can_sync=False, notice="Restore the saved Miro mapping before syncing this board.")
    if result["legacy_snapshot"]:
        result["notice"] = "Historical fixed snapshot. Create a managed board to refresh this plotting goal."
    return result


def list_boards(case):
    """Include registered, old full-trace, and historical snapshot boards."""
    from .cli import board_id

    case, metadata, path = _paths(case)
    records = [copy.deepcopy(record) for record in _read(path, metadata)["boards"]]
    targets = {record.get("board_id") for record in records if record.get("board_id")}
    linked = board_id(metadata["miro_board"]) if metadata.get("miro_board") else None
    candidates = []
    for mapping in sorted((case / "miro").glob("*.json")):
        match = re.fullmatch(r"(?:(connections|pegouts)-)?([0-9a-f]{24})\.json", mapping.name)
        if not match:
            continue
        try:
            state = load_state(mapping)
            target = board_id(state["board_id"])
        except (TraceError, OSError, ValueError, KeyError, TypeError):
            continue
        if digest(target.encode())[:24] != match[2]:
            continue
        goal = match[1] or "full"
        if not match[1] and state.get("namespace", {}).get("case_id") != metadata["case_id"]:
            continue
        candidates.append((target, goal, mapping, bool(match[1]), state.get("latest_run_id")))
    if linked and linked not in {item[0] for item in candidates}:
        candidates.append((linked, "full", case / "miro" / (digest(linked.encode())[:24] + ".json"), False, None))
    for target, goal, mapping, snapshot, run_id in candidates:
        if target in targets:
            continue
        targets.add(target)
        records.append({"id": "legacy-" + goal + "-" + digest(target.encode())[:24],
                        "name": GOALS[goal] + (" snapshot" if snapshot else ""), "goal": goal,
                        "board_id": target, "status": "archived_snapshot" if snapshot else "linked",
                        "state_file": str(mapping.relative_to(case)), "legacy_snapshot": snapshot,
                        "legacy": True, "run_id": run_id})
    return [_display(case, record) for record in records]


def _name(metadata, goal, name):
    default = (str(metadata.get("name") or "Liquid investigation") + " · " + GOALS[goal])[:60]
    return board_options(default if name is None else name)["name"]


def _new(goal, name):
    identity = "board-" + uuid.uuid4().hex
    return {"id": identity, "name": name, "goal": goal, "board_id": None,
            "status": "linked", "state_file": "miro/boards/" + identity + ".json",
            "created_at": now(), "preview_id": None, "run_id": None}


def _assert_unused(case, target, record_id=None):
    for record in list_boards(case):
        if record.get("board_id") == target and record["id"] != record_id:
            raise TraceError("This Miro board already belongs to " + record["name"] + "; select that board or link a different one")


def link_board(case, goal, name, board, *, record_id=None):
    """Link a new target, or recover an uncertain board POST explicitly."""
    from .cli import board_id

    goal, target = _goal(goal), board_id(board)
    case, metadata, path = _paths(case)
    name = _name(metadata, goal, name)
    with _lock(case):
        registry = _read(path, read_case(case))
        _assert_unused(case, target, record_id)
        if record_id is not None:
            record = next((item for item in registry["boards"] if item["id"] == record_id), None)
            if record is None or record["goal"] != goal:
                raise TraceError("Choose the matching pending board entry")
            if record.get("board_id"):
                if record["board_id"] != target:
                    raise TraceError("An existing board binding cannot be replaced; add a separate board entry")
                return _display(case, record)
            if record["status"] not in ("pending_creation", "creation_rejected"):
                raise TraceError("Only a pending board creation can be recovered by linking")
        else:
            record = _new(goal, name)
            registry["boards"].append(record)
        record.update(board_id=target, status="linked", name=name, linked_at=now())
        save_json(path, registry)
        return _display(case, record)


def create_board(case, goal, name=None, *, team_id=None, transport=http):
    """Create a private named board once, journaling intent before its POST."""
    goal = _goal(goal)
    case, metadata, path = _paths(case)
    name = _name(metadata, goal, name)
    body = board_options(name, team_id, "private")
    with _lock(case):
        registry = _read(path, read_case(case))
        record = next((item for item in registry["boards"] if item["goal"] == goal and item["name"] == name), None)
        if record and record.get("board_id"):
            return {**_display(case, record), "created": False, "reused": True}
        if record and record["status"] == "pending_creation":
            raise TraceError(UNCERTAIN)
        token = os.getenv("MIRO_ACCESS_TOKEN")
        if not token or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token):
            raise TraceError("MIRO_ACCESS_TOKEN is missing or malformed; load its raw value through SecretSpec")
        if record is None:
            record = _new(goal, name)
            registry["boards"].append(record)
        record.update(status="pending_creation", attempted_at=now(), creation_request=body)
        save_json(path, registry)
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"}
        try:
            status, _, raw = transport("POST", BOARDS_URL, headers, canonical(body), 30)
        except (TraceError, OSError, ValueError):
            raise TraceError(UNCERTAIN) from None
        if status != 201:
            if isinstance(status, int) and 400 <= status < 500 and status != 408:
                record.update(status="creation_rejected", http_status=status)
                save_json(path, registry)
                raise TraceError("Miro board creation failed (HTTP " + str(status) + "); check token, team permissions, or board limits before retrying")
            raise TraceError(UNCERTAIN)
        from .cli import board_id
        try:
            response = json.loads(raw)
            target = board_id(response["id"])
            _assert_unused(case, target, record["id"])
        except (TraceError, ValueError, KeyError, TypeError):
            raise TraceError(UNCERTAIN) from None
        record.update(board_id=target, status="linked", created_at=now())
        try:
            save_json(path, registry)
        except OSError:
            raise TraceError("Miro created board " + target + "; link that ID to pending entry " + record["id"] +
                             ". Its acknowledgement could not be saved; do not create another board.") from None
        return {**_display(case, record), "created": True, "reused": False}


def _board_plan(plan, record):
    from .miro import validate_plan

    result = copy.deepcopy(plan)
    if not record.get("legacy"):
        result["namespace"]["case_id"] += ":" + record["id"]
        result["board_projection"] = {"version": 1, "record_id": record["id"], "goal": record["goal"]}
    result.pop("sha256", None)
    result["sha256"] = digest(canonical(result))
    validate_plan(result)
    return result


def _check_pegout_address_mode(plan, record, state_path):
    """Keep incompatible saved address identities out of an existing board.

    Old peg-out layouts are immutable snapshots. Their per-output nodes and
    connector endpoints cannot be replaced by the full-trace address migration,
    which proves a different graph. Keep both board modes usable independently.
    """
    if record["goal"] != "pegouts" or record.get("legacy"):
        return
    if not state_path.exists() and not journal_path(state_path).exists():
        return
    state = load_state(state_path)
    desired = plan["namespace"]
    previous = state.get("namespace")
    if (state.get("board_id") != record["board_id"] or not isinstance(previous, dict)
            or {**previous, "address_mode": desired["address_mode"]} != desired):
        return  # Ordinary sync retains its stricter board/case/source validation.
    if previous.get("address_mode") == "outpoint_occurrences" and desired["address_mode"] == "merged":
        raise TraceError(PEGOUT_ADDRESS_NOTICE)
    if previous.get("address_mode") == "merged" and desired["address_mode"] == "outpoint_occurrences":
        raise TraceError("This board uses one node per address, but the selected saved peg-out layout uses "
                         "older per-output nodes. Select a regenerated Paths to peg-outs layout for this "
                         "board, or sync the old layout to its original compatible board or a different "
                         "Miro board. The existing board stays unchanged.")


def sync_board(case, record_id, preview_id, *, reorganize=False, max_items=750, **kwargs):
    """Refresh the selected board from one verified offline plot."""
    from .plots import reviewed_plot
    from .miro import sync

    case, metadata, path = _paths(case)
    with _lock(case, trace=True):
        record = next((item for item in list_boards(case) if item["id"] == record_id), None)
        if record is None or not record["can_sync"]:
            raise TraceError("Select a linked managed board; historical snapshots remain read-only")
        graph, plan = reviewed_plot(case, preview_id)
        if graph.get("plot", {}).get("goal") != record["goal"]:
            raise TraceError("The selected plot has a different goal from this board")
        if not graph.get("nodes"):
            raise TraceError("This plot has no matching paths; the existing Miro board will remain unchanged")
        plan = _board_plan(plan, record)
        state_path = case / record["state_file"]
        _check_pegout_address_mode(plan, record, state_path)
        # Validate item budgets and lineage before changing the publication record.
        sync(plan, record["board_id"], state_path, max_items=max_items, dry_run=True, reorganize=reorganize)
        registry = _read(path, metadata)
        saved = next((item for item in registry["boards"] if item["id"] == record_id), None)
        if saved:
            if saved.get("status") in ("syncing", "sync_error") and saved.get("preview_id") != preview_id:
                state = load_state(state_path, {})
                if _pending(state) or state.get("active_run_id"):
                    raise TraceError("Finish or reconcile this board's interrupted sync with its saved plot before selecting another")
            saved.update(status="syncing", preview_id=preview_id, run_id=graph["run_id"],
                         sync_plan_sha256=plan["sha256"], attempted_sync_at=now())
            save_json(path, registry)
        try:
            result = sync(plan, record["board_id"], state_path, max_items=max_items, reorganize=reorganize, **kwargs)
        except (TraceError, OSError, ValueError):
            if saved:
                saved["status"] = "sync_error"
                save_json(path, registry)
            raise
        if saved:
            saved.update(status="synced", published_at=now(), published_plan_sha256=plan["sha256"])
            save_json(path, registry)
        return {**result, "record_id": record_id, "preview_id": preview_id, "goal": record["goal"]}
