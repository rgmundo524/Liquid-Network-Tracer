"""An investigation's named Miro boards and their durable plot bindings.

Fresh plot creation and publication share a durable, resumable board entry.
Old mappings are discovered without migration; each board keeps its own sync map.
"""

import copy
import fcntl
import json
import math
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
    "For one node per address, select Paths to peg-outs and New board in Plot & Miro, then choose "
    "Generate & create board. The existing board stays unchanged.")


class _BoardBusy(TraceError):
    """An active board operation, distinct from an abandoned pending record."""


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
    """Keep investigation inputs stable without serializing different boards."""
    with ExitStack() as stack:
        names = [("trace.lock", fcntl.LOCK_SH)] if trace else []
        names += [("case.lock", fcntl.LOCK_SH)]
        for name, mode in names:
            handle = stack.enter_context((Path(case) / name).open("a"))
            try:
                fcntl.flock(handle, mode | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TraceError("Investigation data or settings are busy; try after that operation finishes") from None
        yield


@contextmanager
def _registry_lock(case):
    """Serialize short local registry edits only, never remote requests."""
    with (Path(case) / "boards.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


@contextmanager
def _board_lock(case, record_id):
    """One mutable operation per board, including uncertain-creation recovery.

    Initial acquisition happens under the registry lock and never waits. This
    lets the owner reacquire the registry lock to merge its completion without
    deadlocking a second caller trying to acquire the same board.
    """
    directory = Path(case) / "miro" / "operations"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / (digest(str(record_id).encode()) + ".lock")).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise _BoardBusy("This Miro board is busy in another operation or Liquid Tracer instance; "
                             "wait for it to finish, or select a different board") from None
        yield


def _update_record(case, path, metadata, record_id, **fields):
    """Merge just this board into the latest registry after remote work."""
    with _registry_lock(case):
        registry = _read(path, metadata)
        record = next((item for item in registry["boards"] if item["id"] == record_id), None)
        if record is None:
            raise TraceError("The selected board entry is missing; restore the investigation board registry")
        record.update(fields)
        save_json(path, registry)
        return copy.deepcopy(record)


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
    with _lock(case), _registry_lock(case), ExitStack() as operation:
        registry = _read(path, read_case(case))
        _assert_unused(case, target, record_id)
        if record_id is not None:
            record = next((item for item in registry["boards"] if item["id"] == record_id), None)
            if record is None or record["goal"] != goal:
                raise TraceError("Choose the matching pending board entry")
            operation.enter_context(_board_lock(case, record_id))
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


def create_board(case, goal, name=None, *, team_id=None, transport=http, creation_preview_id=None,
                 creation_run_id=None, token=None):
    """Create a private named board once, journaling intent before its POST."""
    goal = _goal(goal)
    case, metadata, path = _paths(case)
    name = _name(metadata, goal, name)
    body = board_options(name, team_id, "private")
    with ExitStack() as operation:
        with _lock(case), _registry_lock(case):
            registry = _read(path, read_case(case))
            record = next((item for item in registry["boards"] if item["goal"] == goal and (
                item.get("creation_preview_id") == creation_preview_id if creation_preview_id is not None
                else item["name"] == name)), None)
            if record is not None:
                operation.enter_context(_board_lock(case, record["id"]))
            if record and record.get("board_id"):
                return {**_display(case, record), "created": False, "reused": True}
            if record and record["status"] == "pending_creation":
                raise TraceError(UNCERTAIN)
            token = token or os.getenv("MIRO_ACCESS_TOKEN")
            if not isinstance(token, str) or not token or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token):
                raise TraceError("MIRO_ACCESS_TOKEN is missing or malformed; load its raw value through SecretSpec")
            if record is None:
                record = _new(goal, name)
                operation.enter_context(_board_lock(case, record["id"]))
                if creation_preview_id is not None:
                    record["creation_preview_id"] = creation_preview_id
                registry["boards"].append(record)
            if creation_preview_id is not None:
                # Recovery must be visible even when the board POST or its first
                # item sync fails before sync_board can persist its own binding.
                record["preview_id"] = creation_preview_id
                record["run_id"] = creation_run_id
            record.update(status="pending_creation", attempted_at=now(), creation_request=body)
            save_json(path, registry)
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"}
        try:
            status, _, raw = transport("POST", BOARDS_URL, headers, canonical(body), 30)
        except (TraceError, OSError, ValueError):
            raise TraceError(UNCERTAIN) from None
        if status != 201:
            if isinstance(status, int) and 400 <= status < 500 and status != 408:
                _update_record(case, path, metadata, record["id"], status="creation_rejected", http_status=status)
                raise TraceError("Miro board creation failed (HTTP " + str(status) + "); check token, team permissions, or board limits before retrying")
            raise TraceError(UNCERTAIN)
        from .cli import board_id
        try:
            response = json.loads(raw)
            target = board_id(response["id"])
        except (TraceError, ValueError, KeyError, TypeError):
            raise TraceError(UNCERTAIN) from None
        try:
            with _registry_lock(case):
                registry = _read(path, metadata)
                _assert_unused(case, target, record["id"])
                saved = next(item for item in registry["boards"] if item["id"] == record["id"])
                saved.update(board_id=target, status="linked", created_at=now())
                save_json(path, registry)
                record = copy.deepcopy(saved)
        except OSError:
            raise TraceError("Miro created board " + target + "; link that ID to pending entry " + record["id"] +
                             ". Its acknowledgement could not be saved; do not create another board.") from None
        except (TraceError, StopIteration):
            raise TraceError(UNCERTAIN) from None
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


def board_for_plot(case, record_id, goal):
    """Resolve a maintained target without changing its registry or mapping."""
    record = next((item for item in list_boards(case) if item["id"] == record_id), None)
    if record is None or not record["can_sync"]:
        raise TraceError("Select a linked managed board; historical snapshots remain read-only")
    if record["goal"] != goal:
        raise TraceError("The selected board has a different plotting goal")
    if record.get("pending_count") or record.get("status") in ("interrupted", "syncing"):
        raise TraceError("Finish or reconcile this board's interrupted sync before preparing another layout")
    return record


def _check_legacy_removals(plan, record, state_path):
    """Older full-board maps lack the per-object authority for scope pruning."""
    if not record.get("legacy"):
        return
    state = load_state(state_path, {})
    desired = {item["key"] for endpoint in ("shapes", "connectors") for item in plan[endpoint]}
    absent = {key for key, item in state.get("items", {}).items()
              if item.get("endpoint") in ("shapes", "connectors") and key not in desired}
    if not absent:
        return
    from .miro import _fee_removals
    # Fee, annotation and context replacement paths carry their own older
    # proofs. A live snapshot alone must never grant new deletion authority.
    supported = _fee_removals(plan, state)
    if absent - supported.keys():
        raise TraceError("This older full-trace board can receive additions and attribution updates, "
                         "but its saved mapping cannot safely remove out-of-scope graph objects. "
                         "Generate a fresh layout and use Create and sync for a new board. "
                         "The existing board remains unchanged.")


def create_and_sync(case, preview_id, name=None, *, team_id=None, max_items=0, transport=http,
                    token=None, interval=.02, progress=None, workers=4):
    """Create one private board per reviewed fresh plot, then resume its publication."""
    from .miro import sync

    case, _, _ = _paths(case)
    with _publication_review(case, preview_id) as (graph, plan):
        if graph["plot"].get("layout_mode", "fresh") != "fresh" or "board_layout" in plan:
            raise TraceError("This layout updates an existing board; use Update board for its selected target")
        if not graph.get("nodes"):
            raise TraceError("This plot has no matching paths; no Miro board was created")
        # Complete local validation and the full new-board budget before POSTing.
        # A unique nonexistent state path keeps this dry run independent of any board.
        sync(plan, "new-board-preflight", case / "miro" / ("preflight-" + uuid.uuid4().hex + ".json"),
             max_items=max_items, dry_run=True, reorganize=True, interval=interval, workers=workers, progress=progress)
        record = create_board(case, graph["plot"]["goal"], name, team_id=team_id, transport=transport,
                              creation_preview_id=preview_id, creation_run_id=graph["run_id"], token=token)
        result = sync_board(case, record["id"], preview_id, max_items=max_items, transport=transport,
                            token=token, interval=interval, workers=workers, progress=progress)
    return {**result, "board_id": record["board_id"], "board_url": record["board_url"],
            "created_board": record["created"], "reused_board": record["reused"]}


@contextmanager
def _publication_review(case, preview_id):
    """Frozen plots publish independently; legacy plans retain input locks."""
    from .plots import reviewed_plot
    graph, plan = reviewed_plot(case, preview_id)
    if graph.get("plot", {}).get("input_snapshot_version") == 1:
        yield graph, plan
    else:
        # Recheck after acquisition so legacy reviews cannot race an import.
        with _lock(case, trace=True):
            yield reviewed_plot(case, preview_id)


def _publication_budget(case, graph, record, max_items):
    """Reject oversized publications before ELK or remote board inventory.

    Identity counts are independent of coordinates. Context regrouping also
    replaces its summary and reattached connectors. The final saved plan still
    receives the sync engine's complete dry-run validation before publication.
    """
    from .context_group_miro import _identity
    from .plots import plot_plan

    plan = plot_plan(graph)
    mapped = load_state(case / record["state_file"], {}).get("items", {}) if record else {}
    replacements = set()
    for key, proof in plan.get("context_group_items", {}).items():
        previous = mapped.get(key, {}).get("context_group_proof")
        if previous and _identity(previous) != _identity(proof):
            replacements.add(key)
            replacements.update(proof["inputs"])
    for item in plan["connectors"]:
        previous = mapped.get(item["key"])
        if previous and (previous.get("source") != item["source"] or previous.get("target") != item["target"]):
            replacements.add(item["key"])
    count = sum(item["key"] not in mapped or item["key"] in replacements
                for endpoint in ("shapes", "connectors") for item in plan[endpoint])
    if max_items and count > max_items:
        raise TraceError(f"Sync needs {count} new items, above max-items={max_items}; "
                         "reduce the trace or explicitly raise the limit")


def _check_unfinished_creation(case):
    """Block abandoned POSTs while allowing other instances' active creations."""
    with _lock(case), _registry_lock(case):
        for record in list_boards(case):
            preview = record.get("creation_preview_id")
            if preview and record.get("status") == "pending_creation":
                with ExitStack() as operation:
                    try:
                        operation.enter_context(_board_lock(case, record["id"]))
                    except _BoardBusy:
                        continue
                    raise TraceError("A Miro board creation outcome is uncertain. Link the created board to its pending "
                                     "entry and resume saved plot " + preview + " before generating another new board.")


def generate_and_sync(case, goal, run_id="latest", min_hops=0, max_hops=10, *, include_unspent=False,
                      include_unspendable=False, include_context=False, layout_mode="fresh",
                      board_record_id=None, name=None, team_id=None, max_items=0, token=None,
                      transport=http, interval=.02, progress=None, workers=4, layout_settings=None,
                      data_source="investigation", dataset_id=None, connection_scope="all_saved"):
    """Save one plot, then create its board or apply its bound board update.

    This is one live action over already collected evidence. On publication
    failure its saved preview is the recovery input for create_and_sync or
    sync_board; resuming must not generate a second layout or board.
    """
    from .plots import preview_plot

    goal = _goal(goal)
    case, metadata, _ = _paths(case)
    if layout_mode not in ("fresh", "update"):
        raise TraceError("Choose a fresh layout or an update to an existing Miro board")
    if type(max_items) is not int or max_items < 0:
        raise TraceError("max-items must be a nonnegative integer (0 means unlimited new items)")
    if (isinstance(interval, bool) or not isinstance(interval, (int, float))
            or not math.isfinite(interval) or interval < 0):
        raise TraceError("Miro interval must be finite and nonnegative")
    if type(workers) is not int or not 1 <= workers <= 4:
        raise TraceError("Miro workers must be an integer between 1 and 4")
    if progress is not None and not callable(progress):
        raise TraceError("Miro progress must be a callback")
    if layout_mode == "fresh":
        if board_record_id is not None:
            raise TraceError("Choose a target Miro board only when preparing an update layout")
        name = _name(metadata, goal, name)
        board_options(name, team_id, "private")
        _check_unfinished_creation(case)
    else:
        if name is not None or team_id is not None:
            raise TraceError("Board name and team apply only when creating a new Miro board")
        board_for_plot(case, board_record_id, goal)
    token = token or os.getenv("MIRO_ACCESS_TOKEN")
    if not isinstance(token, str) or not token or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token):
        raise TraceError("MIRO_ACCESS_TOKEN is missing or malformed; load its raw value through SecretSpec")
    with ExitStack() as operation:
        if layout_mode == "update":
            operation.enter_context(_board_lock(case, board_record_id))
        if progress:
            progress({"phase": "building_plan", "completed": 0, "total": 1})
        plot = preview_plot(case, goal, run_id, min_hops, max_hops, include_unspent=include_unspent,
                            include_unspendable=include_unspendable, include_context=include_context,
                            layout_mode=layout_mode, board_record_id=board_record_id, token=token,
                            transport=transport, interval=interval, progress=progress, workers=workers,
                            data_source=data_source, dataset_id=dataset_id,
                            connection_scope=connection_scope,
                            layout_settings=layout_settings, _board_lock_held=layout_mode == "update",
                            _preflight=lambda graph, record: _publication_budget(case, graph, record, max_items))
        if plot["empty"] and layout_mode == "fresh":
            return {**plot, "published": False, "created_board": False, "reused_board": False,
                    "status": "empty", "publication_notice": "No matching paths were found in the saved data; no Miro board was created."}
        if progress:
            progress({"phase": "building_plan", "completed": 1, "total": 1})
        options = {"max_items": max_items, "transport": transport, "token": token,
                   "interval": interval, "progress": progress, "workers": workers}
        try:
            if layout_mode == "fresh":
                _check_unfinished_creation(case)
                published = create_and_sync(case, plot["preview_id"], name, team_id=team_id, **options)
            else:
                published = sync_board(case, board_record_id, plot["preview_id"], _board_lock_held=True, **options)
        except (TraceError, OSError, ValueError) as error:
            raise TraceError(str(error) + " Saved plot " + plot["preview_id"] +
                             " is available for recovery; resume that saved plot instead of generating another.") from error
        return {**plot, **published, "published": True, "status": "synced"}


def sync_board(case, record_id, preview_id, *, reorganize=False, max_items=0, _board_lock_held=False, **kwargs):
    """Apply a reviewed fresh publication or a board-bound incremental layout."""
    from .miro import sync

    case, metadata, path = _paths(case)
    with ExitStack() as operation:
        with _registry_lock(case):
            record = next((item for item in list_boards(case) if item["id"] == record_id), None)
            if record is None or not record["can_sync"]:
                raise TraceError("Select a linked managed board; historical snapshots remain read-only")
            if not _board_lock_held:
                operation.enter_context(_board_lock(case, record_id))
        graph, plan = operation.enter_context(_publication_review(case, preview_id))
        if graph.get("plot", {}).get("goal") != record["goal"]:
            raise TraceError("The selected plot has a different goal from this board")
        layout_mode = graph["plot"].get("layout_mode")
        if layout_mode == "update":
            if (graph["plot"].get("board_record_id") != record_id
                    or graph["plot"].get("board_id") != record["board_id"] or "board_layout" not in plan):
                raise TraceError("This update layout belongs to a different board; regenerate it for this target")
            if reorganize:
                raise TraceError("An update layout preserves the existing board; generate a fresh layout for a new board to reorganize")
        if not graph.get("nodes") and layout_mode != "update":
            raise TraceError("This plot has no matching paths; the existing Miro board will remain unchanged")
        plan = _board_plan(plan, record)
        state_path = case / record["state_file"]
        if layout_mode == "fresh":
            if record.get("creation_preview_id") != preview_id:
                raise TraceError("A fresh layout creates its own board; use Create and sync, or generate an update layout for this board")
            current = load_state(state_path, {})
            completed = (plan["sha256"] in current.get("runs", {}).get(graph["run_id"], {}).get("plan_sha256s", [])
                         and not current.get("active_run_id") and not _pending(current))
            if current.get("items") and record.get("preview_id") != preview_id:
                raise TraceError("This board already contains a different layout; generate an update layout")
            # First publication follows the reviewed coordinates. Repeating an
            # acknowledged publication must preserve subsequent manual movement.
            reorganize = not completed
        if layout_mode == "update":
            _check_legacy_removals(plan, record, state_path)
        _check_pegout_address_mode(plan, record, state_path)
        # Validate item budgets and lineage before changing the publication record.
        sync(plan, record["board_id"], state_path, max_items=max_items, dry_run=True, reorganize=reorganize)
        if not record.get("legacy"):
            if record.get("status") in ("syncing", "sync_error", "interrupted") and record.get("preview_id") != preview_id:
                state = load_state(state_path, {})
                if _pending(state) or state.get("active_run_id"):
                    raise TraceError("Finish or reconcile this board's interrupted sync with its saved plot before selecting another")
            _update_record(case, path, metadata, record_id, status="syncing", preview_id=preview_id,
                           run_id=graph["run_id"], sync_plan_sha256=plan["sha256"], attempted_sync_at=now())
        try:
            result = sync(plan, record["board_id"], state_path, max_items=max_items, reorganize=reorganize, **kwargs)
        except (TraceError, OSError, ValueError):
            if not record.get("legacy"):
                _update_record(case, path, metadata, record_id, status="sync_error")
            raise
        if not record.get("legacy"):
            _update_record(case, path, metadata, record_id, status="synced", published_at=now(),
                           published_plan_sha256=plan["sha256"])
        return {**result, "record_id": record_id, "preview_id": preview_id, "goal": record["goal"]}
