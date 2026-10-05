"""Delete an explicitly confirmed Miro board while retaining local evidence.

Receipts belong to the shared cases root because one remote board may be linked
by multiple investigations. A pending/uncertain deletion forbids later writes;
only Miro's documented 204 response proves that deletion completed.
"""

import fcntl
import os
from contextlib import ExitStack, contextmanager
from pathlib import Path
from urllib.parse import quote

from .api import http
from .common import StopRun, TraceError, digest, now, read_json, save_json
from .investigations import read_case

_STATUSES = {"pending", "uncertain", "rejected", "deleted"}
_UNCERTAIN = ("Miro did not confirm board deletion. The board remains listed and writes are blocked. "
              "Inspect the board, then explicitly retry Delete Board for this same board.")


def _safe_path(path):
    """Do not follow local symlinks for deletion authority, receipts or locks."""
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise TraceError("Miro board deletion paths must not contain symbolic links")
    return path


def _paths(case, target):
    case = _safe_path(case)
    directory = _safe_path(case.parent / ".miro-board-deletions")
    return case, directory / (digest(target.encode()) + ".json")


def deletion_receipt(case, target):
    if not target:
        return None
    _, path = _paths(case, target)
    _safe_path(path)
    if not path.exists():
        return None
    try:
        receipt = read_json(path)
        if (not isinstance(receipt, dict) or receipt.get("schema_version") != 1
                or receipt.get("board_id") != target or receipt.get("status") not in _STATUSES):
            raise ValueError
        return receipt
    except (OSError, ValueError, TypeError):
        raise TraceError("Invalid Miro board deletion receipt; restore it before changing this board") from None


def assert_board_writable(case, target):
    receipt = deletion_receipt(case, target)
    if receipt and receipt["status"] in ("pending", "uncertain", "deleted"):
        if receipt["status"] == "deleted":
            raise TraceError("This Miro board was deleted; select a different board or create a new board from a saved preview")
        raise TraceError(_UNCERTAIN)


@contextmanager
def _file_lock(path, message):
    path = _safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError(message) from None
        yield


@contextmanager
def board_write_lock(case, target):
    """Coordinate remote operations on a board across investigations/processes."""
    _, path = _paths(case, target)
    with _file_lock(path.with_suffix(".lock"), "This Miro board is busy in another operation"):
        assert_board_writable(case, target)
        yield


def _save(path, receipt):
    _safe_path(path)
    _safe_path(path.with_name(path.name + ".tmp"))
    save_json(path, receipt)
    # Durably retain the rename as well as the contents before remote deletion.
    fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _result(record):
    return {"id": record["id"], "record_id": record["id"], "board_id": record["board_id"],
            "name": record["name"], "goal": record["goal"], "status": "deleted", "deleted": True}


def _finish_local(case, target):
    # Mapping files and graph exports remain immutable evidence. The shared
    # receipt masks every legacy discovery and prevents saved creation reuse.
    metadata = read_case(case)
    if metadata.get("miro_board"):
        from .cli import board_id
        if board_id(metadata["miro_board"]) == target:
            _safe_path(case / "case.json")
            _safe_path(case / "case.json.tmp")
            save_json(case / "case.json", {**metadata, "miro_board": None})


def delete_board(case, record_id, expected_board_id, *, token=None, transport=http, progress=None):
    """Delete one listed board, requiring the exact locally bound ID twice."""
    from .cli import board_id
    from .investigation_boards import _board_lock, _registry_lock, list_boards
    from .boards import BOARDS_URL

    target = board_id(expected_board_id)
    if target != expected_board_id:
        raise TraceError("Confirm the exact Miro board ID shown for the selected board")
    case, receipt_path = _paths(case, target)
    for path in (case / "case.json", case / "miro", case / "miro" / "boards.json",
                 case / "boards.lock", case / "miro" / "operations"):
        _safe_path(path)
    read_case(case)
    with ExitStack() as operation:
        operation.enter_context(_file_lock(case / "case.lock", "Investigation settings or another operation are busy"))
        with _registry_lock(case):
            record = next((item for item in list_boards(case, include_deleted=True) if item["id"] == record_id), None)
            if record is None:
                prior = deletion_receipt(case, target)
                if (prior and prior["status"] == "deleted" and prior.get("record_id") == record_id
                        and prior.get("case_id") == read_case(case)["case_id"]):
                    record = {"id": record_id, "board_id": target, "name": prior["name"], "goal": prior["goal"]}
            if record is None or record.get("board_id") != target:
                raise TraceError("The selected board or confirmed board ID changed; reload the boards list")
            _safe_path(case / "miro" / "operations" / (digest(str(record_id).encode()) + ".lock"))
            operation.enter_context(_board_lock(case, record_id))
        if record.get("state_file"):
            mapping = _safe_path(case / record["state_file"])
            operation.enter_context(_file_lock(mapping.with_suffix(".lock"), "This Miro board mapping is busy in another operation"))
        operation.enter_context(_file_lock(receipt_path.with_suffix(".lock"), "This Miro board is busy in another operation"))
        receipt = deletion_receipt(case, target)
        if receipt and receipt["status"] == "deleted":
            _finish_local(case, target)
            return _result(record)
        token = token or os.getenv("MIRO_ACCESS_TOKEN")
        if not isinstance(token, str) or not token or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token):
            raise TraceError("MIRO_ACCESS_TOKEN is missing or malformed; load its raw value through SecretSpec")
        if progress:
            progress({"phase": "deleting_board", "completed": 0, "total": 1})
        # A rejected retry cannot resolve an earlier timeout: that original
        # DELETE may already have succeeded or may still be completing.
        previously_uncertain = bool(receipt and receipt["status"] in ("pending", "uncertain"))
        receipt = {"schema_version": 1, "board_id": target, "case_id": read_case(case)["case_id"],
                   "record_id": record_id, "name": record["name"], "goal": record["goal"],
                   "status": "pending", "attempted_at": now()}
        _save(receipt_path, receipt)
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        try:
            status, _, _ = transport("DELETE", BOARDS_URL + "/" + quote(target, safe=""), headers, None, 30)
        except (TraceError, OSError, ValueError):
            try:
                _save(receipt_path, {**receipt, "status": "uncertain", "notice": _UNCERTAIN})
            except OSError:
                pass  # The prior durable intent still blocks publication.
            raise TraceError(_UNCERTAIN) from None
        if status != 204:
            rejected = (not previously_uncertain and isinstance(status, int)
                        and status in (400, 401, 403, 405, 422, 429))
            notice = ("Miro rejected board deletion (HTTP " + str(status) + "). Check board ownership, "
                      "boards:write permission and the access token before retrying." if rejected else _UNCERTAIN)
            _save(receipt_path, {**receipt, "status": "rejected" if rejected else "uncertain",
                                 "http_status": status if isinstance(status, int) else None, "notice": notice})
            raise TraceError(notice)
        try:
            _save(receipt_path, {**receipt, "status": "deleted", "deleted_at": now(), "http_status": 204})
        except OSError:
            raise TraceError("Miro confirmed deletion of board " + target + ", but the acknowledgement could not be saved. "
                             "The pending receipt blocks writes; restore the receipt before further changes.") from None
        try:
            _finish_local(case, target)
        except OSError:
            raise TraceError("Miro board deletion is saved, but updating the local board link failed. Retry Delete Board "
                             "to finish locally without another remote request.") from None
        # Cancellation arriving after acknowledgement must not hide success.
        if progress:
            try:
                progress({"phase": "deleting_board", "completed": 1, "total": 1})
            except StopRun:
                pass
        return _result(record)
