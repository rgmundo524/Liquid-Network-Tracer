"""Create and persist one Miro board for an investigation, without publishing runs."""

import fcntl
import json
import os
from pathlib import Path
from urllib.parse import quote

from .api import http
from .common import TraceError, canonical, now, read_json, save_json
from .investigations import read_case


BOARDS_URL = "https://api.miro.com/v2/boards"
UNCERTAIN = ("Miro board creation outcome is uncertain. Inspect your Miro boards, then link the created "
             "board in Investigation settings. No additional board will be created automatically.")


def default_board_name(metadata):
    name = metadata.get("name") or "Liquid investigation"
    prefix = "SYNTHETIC DEMO · " if metadata.get("fixture") else ""
    return (prefix + str(name))[:60]


def board_options(name, team_id=None, visibility="private"):
    """Validate creation options locally and build the documented Miro v2 body.

    https://developers.miro.com/reference/create-board-1
    https://developers.miro.com/docs/rest-api-comparison-guide
    """
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
        raise TraceError("Miro board name must contain 1 to 60 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise TraceError("Miro board name cannot contain control characters")
    if visibility not in ("private", "team"):
        raise TraceError("Miro board visibility must be private or team")
    body = {"name": name.strip(), "policy": {"sharingPolicy": {
        "access": "private", "organizationAccess": "private",
        "teamAccess": "edit" if visibility == "team" else "private",
    }}}
    if team_id is not None and team_id != "":
        if (not isinstance(team_id, str) or not team_id.strip() or len(team_id.strip()) > 200
                or any(not (char.isascii() and (char.isalnum() or char in "_=-")) for char in team_id.strip())):
            raise TraceError("Invalid Miro team ID; use the team ID or leave it empty")
        body["teamId"] = team_id.strip()
    return body


def _result(target, receipt=None, *, created=False, receipt_path=None):
    return {"board_id": target, "board_url": "https://miro.com/app/board/" + quote(target, safe="") + "/",
            "name": (receipt or {}).get("name"), "team_id": (receipt or {}).get("team_id"),
            "visibility": (receipt or {}).get("visibility"), "created": created, "reused": not created,
            "receipt_file": str(receipt_path.resolve()) if receipt_path else None}


def _read_receipt(path, identity):
    if not path.exists():
        return None
    try:
        receipt = read_json(path)
    except (ValueError, OSError):
        raise TraceError("Cannot read the Miro board creation receipt; restore it before creating another board") from None
    if (not isinstance(receipt, dict) or receipt.get("schema_version") != 1
            or receipt.get("case_id") != identity or receipt.get("status") not in ("pending", "created", "rejected")):
        raise TraceError("Invalid Miro board creation receipt; restore it before creating another board")
    return receipt


def create_board(case, name=None, team_id=None, visibility="private", transport=http):
    """Create once; retain an intent before POST and a receipt before linking.

    The existing case lock also serializes settings edits. It is held directly
    here, rather than recursively acquiring it through update_case(). A failed
    response is never automatically retried or used to widen sharing access.
    """
    from .cli import board_id

    case = Path(case)
    metadata = read_case(case)  # Validate the case before creating any paths.
    body = board_options(default_board_name(metadata) if name is None else name, team_id, visibility)
    receipt_path = case / "miro" / "board-creation.json"
    with (case / "case.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("Investigation settings or Miro board creation are busy; try again after that operation finishes") from None
        metadata = read_case(case)
        # Explicitly linking a board in settings also recovers an uncertain POST.
        if metadata.get("miro_board"):
            target = board_id(metadata["miro_board"])
            return _result(target)
        receipt = _read_receipt(receipt_path, metadata["case_id"])
        if receipt and receipt["status"] == "created":
            target = board_id(receipt.get("board_id"))
            save_json(case / "case.json", {**metadata, "miro_board": target})
            return _result(target, receipt, receipt_path=receipt_path)
        if receipt and receipt["status"] == "pending":
            raise TraceError(UNCERTAIN)
        token = os.environ.get("MIRO_ACCESS_TOKEN")
        if not token or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token):
            raise TraceError("MIRO_ACCESS_TOKEN is missing or malformed; load its raw value through SecretSpec")
        # Re-read the current name under the lock if the default is requested.
        if name is None:
            body = board_options(default_board_name(metadata), team_id, visibility)
        receipt = {"schema_version": 1, "case_id": metadata["case_id"], "status": "pending",
                   "attempted_at": now(), "request": body, "visibility": visibility}
        save_json(receipt_path, receipt)
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"}
        try:
            status, _, raw = transport("POST", BOARDS_URL, headers, canonical(body), 30)
        except (TraceError, OSError, ValueError):
            raise TraceError(UNCERTAIN) from None
        if status != 201:
            definite = isinstance(status, int) and 400 <= status < 500 and status != 408
            if definite:
                receipt.update(status="rejected", http_status=status)
                save_json(receipt_path, receipt)
                detail = {
                    401: "Check the Miro access token in Proton Pass.",
                    403: "Check boards:write permission, team access, and whether your plan allows the selected visibility.",
                    400: "Check the board options and whether your plan allows the selected visibility.",
                    404: "Check the selected team and the token's access to it.",
                    409: "Check your team's board limits and permissions.",
                    429: "Miro rate limited this request; try again later.",
                }.get(status, "Correct the request or account permissions before retrying.")
                raise TraceError("Miro board creation failed (HTTP " + str(status) + "). " + detail)
            raise TraceError("Miro did not confirm board creation. " + UNCERTAIN)
        try:
            response = json.loads(raw)
            target = board_id(response.get("id")) if isinstance(response, dict) else None
            if not target:
                raise ValueError
        except (TraceError, TypeError, ValueError):
            raise TraceError(UNCERTAIN) from None
        team = response.get("team")
        returned_team = team.get("id") if isinstance(team, dict) else None
        receipt.update(status="created", created_at=now(), board_id=target,
                       board_url="https://miro.com/app/board/" + quote(target, safe="") + "/",
                       name=body["name"], team_id=returned_team if isinstance(returned_team, str) else body.get("teamId"))
        # If linking fails, this acknowledged ID survives and is reused next time.
        try:
            save_json(receipt_path, receipt)
        except OSError:
            raise TraceError("Miro created board " + target + " but its receipt could not be saved. "
                             "Link this board in Investigation settings; do not repeat board creation.") from None
        try:
            save_json(case / "case.json", {**metadata, "miro_board": target})
        except OSError:
            raise TraceError("Miro board creation is saved, but linking it to this investigation failed. "
                             "Retry Create Miro board to reuse the saved board without creating another.") from None
        return _result(target, receipt, created=True, receipt_path=receipt_path)
