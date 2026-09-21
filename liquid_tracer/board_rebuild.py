"""Publish a refreshed graph to a new private board, retaining the old board."""

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from .api import http
from .boards import BOARDS_URL, board_options, default_board_name
from .common import TraceError, canonical, digest, now, read_json, save_json
from .investigations import read_case
from .miro import _namespace, sync, validate_plan
from .miro_state import journal_path, load_state


_STATUSES = {"prepared", "pending", "rejected", "created", "syncing", "complete"}
_UNCERTAIN = ("The new Miro board creation outcome is uncertain. Inspect your Miro boards and, if it exists, "
              "link that board in Investigation settings and use Sync to Miro. "
              "This rebuild will not create another board automatically. The old board is unchanged.")


def _url(board):
    return "https://miro.com/app/board/" + quote(board, safe="") + "/"


def _directory(case, source):
    return Path(case) / "miro" / "rebuilds" / digest(source.encode())[:24]


def _receipt(path, identity, source=None):
    from .cli import board_id

    if not path.exists():
        return None
    try:
        value = read_json(path)
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or value.get("case_id") != identity or value.get("status") not in _STATUSES
                or board_id(value.get("source_board_id")) != value["source_board_id"]
                or (source is not None and value["source_board_id"] != source)
                or not isinstance(value.get("run_id"), str) or not re.fullmatch(r"[0-9a-f]{16}", value["run_id"])
                or any(not isinstance(value.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", value[key])
                       for key in ("plan_sha256", "archived_plan_sha256"))
                or not isinstance(value.get("request"), dict)):
            raise ValueError
        if board_options(value["request"].get("name")) != value["request"]:
            raise ValueError
        namespace = _namespace({"schema_version": 2, "namespace": value.get("namespace")})
        if namespace["case_id"] != identity or namespace["address_mode"] != "merged":
            raise ValueError
        has_target = value["status"] in {"created", "syncing", "complete"}
        if (("board_id" in value) != has_target
                or ("mapping_checked" in value and type(value["mapping_checked"]) is not bool)
                or (value["status"] in {"syncing", "complete"} and value.get("mapping_checked") is not True)):
            raise ValueError
        if has_target:
            target = board_id(value.get("board_id"))
            if target != value.get("board_id") or target == value["source_board_id"]:
                raise ValueError
        if value["status"] == "complete":
            result = value.get("result")
            if (not isinstance(result, dict) or result.get("board_id") != value["board_id"]
                    or result.get("previous_board_id") != value["source_board_id"]
                    or result.get("run_id") != value["run_id"] or result.get("plan_sha256") != value["plan_sha256"]
                    or result.get("rebuild_status") != "complete"):
                raise ValueError
        return value
    except (OSError, ValueError, TypeError, KeyError, TraceError):
        raise TraceError("Cannot read the board rebuild receipt; restore it before rebuilding another board") from None


def rebuild_status(case):
    """Return a small read-only status so refreshed UIs offer resume, not recreate."""
    from .cli import board_id

    case = Path(case)
    metadata = read_case(case)
    if not metadata.get("miro_board"):
        return None
    current = board_id(metadata["miro_board"])
    directory = case / "miro" / "rebuilds"
    if not directory.is_dir():
        return None
    matches = []
    for path in directory.glob("*/receipt.json"):
        try:
            receipt = _receipt(path, metadata["case_id"])
        except TraceError as error:
            return {"status": "unavailable", "notice": str(error), "resume_command": None}
        if current in (receipt["source_board_id"], receipt.get("board_id")):
            # Normal Sync can finish a partially published replacement too.
            # Reflect that without mutating receipts during a read-only UI load.
            if receipt["status"] == "syncing" and receipt.get("board_id") == current:
                try:
                    state = load_state(case / "miro" / (digest(current.encode())[:24] + ".json"), {})
                    published = state.get("runs", {}).get(receipt["run_id"], {}).get("plan_sha256s", [])
                    if (state.get("board_id") == current and state.get("namespace") == receipt["namespace"]
                            and not state.get("active_run_id")
                            and not state.get("pending") and not state.get("pending_creations")
                            and isinstance(published, list) and bool(published)):
                        receipt = {**receipt, "status": "complete"}
                except (TraceError, OSError, ValueError, TypeError, AttributeError):
                    pass  # The ordinary recovery status describes journal faults.
            matches.append(receipt)
    if not matches:
        return None
    # An unfinished action from the current board takes precedence over the
    # completed action that originally created that same board.
    receipt = max(matches, key=lambda r: (r["status"] != "complete", r.get("prepared_at", "")))
    target = receipt.get("board_id")
    status = receipt["status"]
    return {"status": status, "source_board_id": receipt["source_board_id"],
            "previous_board_id": receipt["source_board_id"],
            "previous_board_url": _url(receipt["source_board_id"]),
            "board_id": target, "board_url": _url(target) if target else None,
            "run_id": receipt["run_id"], "name": receipt["request"].get("name"),
            "resume_command": "miro-rebuild-board" if status != "pending" else None,
            "notice": _UNCERTAIN if status == "pending" else (
                "The old board is unchanged. Resume this rebuild, or use Sync to Miro on the linked new board."
                if target and status != "complete" else "The old board is unchanged.")}


def _token():
    token = os.environ.get("MIRO_ACCESS_TOKEN")
    if not token or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token):
        raise TraceError("MIRO_ACCESS_TOKEN is missing or malformed; load its raw value through SecretSpec")
    return token


@contextmanager
def _locks(case):
    # Attribution and trace writers acquire trace.lock before case.lock too.
    # A shared lock keeps the selected evidence/presentation stable throughout
    # preparation and publication without blocking other read-only previews.
    with (case / "trace.lock").open("a") as trace_lock, (case / "case.lock").open("a") as case_lock:
        try:
            fcntl.flock(trace_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(case_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("Investigation settings, tracing, or a board rebuild are busy; try again after that operation finishes") from None
        yield


def _create(receipt, path, transport):
    from .cli import board_id

    token = _token()
    receipt.update(status="pending", attempted_at=now())
    save_json(path, receipt)
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"}
    try:
        status, _, raw = transport("POST", BOARDS_URL, headers, canonical(receipt["request"]), 30)
    except (TraceError, OSError, ValueError):
        raise TraceError(_UNCERTAIN) from None
    if status != 201:
        if isinstance(status, int) and 400 <= status < 500 and status != 408:
            receipt.update(status="rejected", http_status=status)
            save_json(path, receipt)
            raise TraceError(f"Miro board creation failed (HTTP {status}). Check the token, team permissions, "
                             "private-board availability, and account limits before retrying. The old board is unchanged.")
        raise TraceError(_UNCERTAIN)
    try:
        response = json.loads(raw)
        target = board_id(response.get("id"))
        if target == receipt["source_board_id"]:
            raise ValueError
    except (AttributeError, TraceError, TypeError, ValueError):
        raise TraceError(_UNCERTAIN) from None
    receipt.update(status="created", created_at=now(), board_id=target)
    try:
        save_json(path, receipt)
    except OSError:
        raise TraceError("Miro created board " + target + " but its receipt could not be saved. "
                         "Link this board in Investigation settings and use Sync to Miro. Do not repeat board creation.") from None
    return target


def rebuild_board(case, run_id="latest", source_board=None, name=None, max_new_items=750, progress=None,
                  transport=http):
    """Create once per source board; retry the same frozen graph on its replacement.

    The explicit source pins the user's action across a changed case board link.
    A later intentional rebuild supplies the currently linked replacement as its
    source. No DELETE is sent and no source mapping is changed.
    """
    from .cli import (attribution_arrow_coloring, board_id, branch_hubs, connector_appearance, context_input_grouping, include_fee_flows,
                      layout_search_attempts, refresh_presentation, resolve_latest, run_path, verify_export)
    from .services import load_services

    case = Path(case)
    metadata = read_case(case)
    source = board_id(source_board)
    if type(max_new_items) is not int or max_new_items < 0:
        raise TraceError("--max-new-items must be a nonnegative integer")
    if name is not None:
        board_options(name)
    directory = _directory(case, source)
    receipt_path = directory / "receipt.json"
    plan_path = directory / "miro-plan.json"
    created = False
    # One lock protects settings, source identity, creation receipts and linking.
    # Use sync directly: sync_run would recursively acquire this same case lock.
    with _locks(case):
        metadata = read_case(case)
        active = rebuild_status(case)
        if active and active["status"] == "unavailable":
            raise TraceError(active["notice"])
        if (active and active["status"] != "complete" and active.get("source_board_id") != source):
            raise TraceError("This board has an unfinished rebuild. Resume it using --source-board "
                             + active["source_board_id"] + " before starting another rebuild")
        receipt = _receipt(receipt_path, metadata["case_id"], source)
        current = board_id(metadata.get("miro_board"))
        if current not in (source, (receipt or {}).get("board_id")):
            raise TraceError("The linked Miro board changed since this rebuild was requested. Reload the investigation before starting another rebuild")
        if receipt and receipt["status"] == "pending":
            raise TraceError(_UNCERTAIN)
        if receipt and (run_id not in ("latest", receipt["run_id"])
                        or (name is not None and name.strip() != receipt["request"].get("name"))):
            raise TraceError("This source board already has a rebuild for another run or name. Resume that rebuild, "
                             "then start a new rebuild from the linked replacement board")
        if receipt and receipt.get("board_id"):
            target = receipt["board_id"]
            try:
                plan = read_json(plan_path)
                validate_plan(plan)
                if (plan["sha256"] != receipt["plan_sha256"] or plan["run_id"] != receipt["run_id"]
                        or _namespace(plan) != receipt["namespace"]):
                    raise ValueError
            except (OSError, TraceError, ValueError, KeyError, TypeError):
                raise TraceError("The saved board rebuild plan is missing or invalid. Restore it before resuming; "
                                 "no additional board will be created") from None
            if receipt["status"] == "complete":
                if current != target:
                    save_json(case / "case.json", {**metadata, "miro_board": target})
                return {**receipt["result"], "created": False, "reused": True}
        else:
            _token()  # Avoid an expensive layout when board creation cannot run.
            resolved = receipt["run_id"] if receipt else resolve_latest(case, run_id)
            archive = run_path(case, resolved)
            verify_export(archive)
            archived = read_json(archive / "miro-plan.json")
            validate_plan(archived)
            if archived.get("run_id") != resolved or _namespace(archived)["case_id"] != metadata["case_id"]:
                raise TraceError("Saved Miro plan does not match this investigation and selected run")
            count_report = {}
            plan = refresh_presentation(
                archived, archive / "trace.json", include_fee_flows(metadata), connector_appearance(metadata),
                progress=progress, service_settings=load_services(case), preview_directory=case / "previews",
                fetch_address_counts=True, count_report=count_report,
                group_context_inputs=context_input_grouping(metadata), hub_addresses=branch_hubs(metadata),
                color_attribution_arrows=attribution_arrow_coloring(metadata),
                layout_attempts=layout_search_attempts(metadata))
            # A fresh board needs every shape and connector. Check the full item
            # budget and sync invariants before even recording a POST intent.
            with tempfile.TemporaryDirectory(prefix="liquid-rebuild-") as temporary:
                sync(plan, "rebuild_preflight", Path(temporary) / "state.json", max_items=max_new_items, dry_run=True)
            if read_case(case) != metadata:
                raise TraceError("Investigation settings changed while preparing the rebuild. Retry with the current settings")
            body = board_options(name if name is not None else default_board_name(metadata))
            receipt = {"schema_version": 1, "case_id": metadata["case_id"], "source_board_id": source,
                       "status": "prepared", "prepared_at": now(), "run_id": resolved, "request": body,
                       "namespace": _namespace(plan),
                       "plan_sha256": plan["sha256"], "archived_plan_sha256": archived["sha256"],
                       "address_counts": count_report}
            save_json(plan_path, plan)
            save_json(receipt_path, receipt)
            target = _create(receipt, receipt_path, transport)
            created = True
        state_path = case / "miro" / (digest(target.encode())[:24] + ".json")
        if not receipt.get("mapping_checked"):
            if state_path.exists() or journal_path(state_path).exists():
                raise TraceError("The created board already has a local mapping. Keep both board records and inspect them before syncing")
            receipt["mapping_checked"] = True
            save_json(receipt_path, receipt)
        # Validate resumability and budget before changing the current board link.
        sync(plan, target, state_path, max_items=max_new_items, dry_run=True)
        try:
            save_json(case / "case.json", {**metadata, "miro_board": target})
        except OSError:
            raise TraceError("The new board is saved but linking it failed. Retry this same rebuild to reuse the saved board") from None
        receipt.update(status="syncing")
        save_json(receipt_path, receipt)
        try:
            result = sync(plan, target, state_path, max_items=max_new_items, progress=progress, transport=transport)
        except (TraceError, OSError, ValueError) as error:
            raise TraceError("The new board " + target + " is linked, but its graph publication did not finish. "
                             "Resume this rebuild or use Sync to Miro; do not create another board. "
                             "The old board is unchanged. " + str(error)) from None
        report = {**result, "board_id": target, "board_url": _url(target),
                  "previous_board_id": source, "previous_board_url": _url(source),
                  "run_id": plan["run_id"], "name": receipt["request"]["name"],
                  "created": created, "reused": not created, "rebuild_status": "complete",
                  "receipt_file": str(receipt_path.resolve()), "state_file": str(state_path.resolve()),
                  "plan_sha256": plan["sha256"], "archived_plan_sha256": receipt["archived_plan_sha256"],
                  "presentation_refreshed": plan["sha256"] != receipt["archived_plan_sha256"],
                  "address_counts": receipt.get("address_counts", {}),
                  "notice": "The previous board is unchanged. Future syncs use the new board. Create frames separately when the graph is finished."}
        receipt.update(status="complete", completed_at=now(), result=report)
        save_json(receipt_path, receipt)
        return report
