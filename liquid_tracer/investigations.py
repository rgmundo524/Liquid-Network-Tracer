"""Persistent investigation settings, independent of the terminal interface."""

import fcntl
import math
import os
import re
import uuid
from pathlib import Path

from .common import TraceError, now, parse_outpoint, read_json, save_json
from .layout_search import DEFAULT_LAYOUT_ATTEMPTS, normalize_layout_attempts


DEFAULTS = {
    "hops": 1,
    "max_transactions": 20,
    "max_outpoints": 100,
    "max_requests": 30,
    "max_seconds": 60,
    "max_new_items": 750,
    "layout_attempts": DEFAULT_LAYOUT_ATTEMPTS,
    "include_fees": False,
    "color_attribution_arrows": False,
    "group_context_inputs": False,
    "hub_addresses": [],
    "center_name": "",
    "connector_style": "straight",
}


PLOT_SETTING_KEYS = frozenset({"layout_attempts", "connector_style", "include_fees",
                              "color_attribution_arrows", "group_context_inputs", "center_name", "hub_addresses"})


def validate_blockchain(value):
    """Validate the chain identity independently of live or fixture data sources."""
    if not isinstance(value, str) or value != "liquid":
        raise TraceError("Unsupported blockchain. Only Liquid is currently available.")
    return value


def default_root():
    root = os.environ.get("LIQUID_INVESTIGATIONS_DIR")
    return Path(root).expanduser() if root else Path(os.environ.get("LIQUID_TRACER_ROOT") or Path.cwd()) / "cases"


def validate_settings(settings):
    if not isinstance(settings, dict) or set(settings) - set(DEFAULTS):
        raise TraceError("Run settings must contain only supported tracing limits and graph options")
    result = {**DEFAULTS, **settings}
    for key, value in result.items():
        if key == "layout_attempts":
            if value is None:
                raise TraceError("layout_attempts must be a whole number from 1 to 1000")
            result[key] = normalize_layout_attempts(value)
            continue
        if key == "hub_addresses":
            if (not isinstance(value, list)
                    or any(not isinstance(address, str)
                           or not re.fullmatch(r"[A-Za-z0-9]{14,200}", address.strip())
                           for address in value)):
                raise TraceError("hub_addresses must be a list of full Liquid addresses, using 14 to 200 letters or numbers each")
            result[key] = sorted({address.strip() for address in value})
            continue
        if key == "center_name":
            if not isinstance(value, str):
                raise TraceError("Center named group must be an attribution name, or blank to disable")
            value = value.strip()
            if len(value) > 120 or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise TraceError("Center named group must contain at most 120 characters and no control characters")
            result[key] = value
            continue
        if key == "connector_style":
            if not isinstance(value, str) or value not in ("straight", "curved", "elbowed"):
                raise TraceError("connector_style must be straight, curved, or elbowed")
            continue
        if key in ("include_fees", "color_attribution_arrows", "group_context_inputs"):
            if type(value) is not bool:
                raise TraceError(f"{key} must be true or false")
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise TraceError(f"{key} must be a finite number")
        if key != "max_seconds" and not isinstance(value, int):
            raise TraceError(f"{key} must be a whole number")
        minimum = 0 if key in ("hops", "max_new_items", "max_seconds") else 1
        if key == "max_seconds" and value <= 0:
            raise TraceError("max_seconds must be positive")
        if value < minimum:
            raise TraceError(f"{key} must be at least {minimum}")
    return result


def load_settings(root):
    path = Path(root) / "settings.json"
    return validate_settings(read_json(path)) if path.exists() else validate_settings({})


def save_settings(root, settings):
    values = validate_settings(settings)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "settings.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        save_json(root / "settings.json", values)
    return values


def _validate_case(metadata):
    if not isinstance(metadata, dict):
        raise TraceError("Invalid investigation metadata; restore case.json")
    identity = metadata.get("case_id")
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
        raise TraceError("Invalid case identity; restore the original case.json")
    return {**metadata, "blockchain": validate_blockchain(metadata.get("blockchain", "liquid"))}


def read_case(case):
    # Writers replace case.json atomically. A read observes one complete version
    # and creates no lock files, so offline previews remain strictly read-only.
    return _validate_case(read_json(Path(case) / "case.json"))


def update_case(case, updates):
    if not isinstance(updates, dict) or set(updates) - {"name", "miro_board", "run_defaults"}:
        raise TraceError("Only investigation name, board, and run defaults can be edited")
    changes = dict(updates)
    if "name" in changes:
        changes["name"] = _name(changes["name"])
    if "miro_board" in changes and changes["miro_board"] is not None:
        from .cli import board_id
        changes["miro_board"] = board_id(changes["miro_board"])
    if "run_defaults" in changes:
        changes["run_defaults"] = validate_settings(changes["run_defaults"])
    case = Path(case)
    with (case / "case.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        metadata = {**read_case(case), **changes}
        save_json(case / "case.json", metadata)
    return metadata


def save_plot_settings(case, settings):
    """Merge display settings into the latest investigation metadata atomically."""
    if not isinstance(settings, dict) or set(settings) - PLOT_SETTING_KEYS:
        raise TraceError("Plot settings accept layout attempts, connectors, fees, attribution arrows, context grouping, center name, and branch hubs only")
    validated = validate_settings(settings)
    changes = {key: validated[key] for key in settings}
    case = Path(case)
    with (case / "trace.lock").open("a") as trace_lock, (case / "case.lock").open("a") as case_lock:
        try:
            # The same lock order as plotting/publication avoids waiting on a
            # shared preview lock while holding another operation's lock.
            fcntl.flock(trace_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(case_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("An investigation operation is active; save plot settings after it finishes") from None
        metadata = read_case(case)
        defaults = validate_settings(metadata.get("run_defaults", {}))
        defaults = validate_settings({**defaults, **changes})
        if changes and defaults != metadata.get("run_defaults"):
            metadata = {**metadata, "run_defaults": defaults}
            save_json(case / "case.json", metadata)
        return metadata


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 120:
        raise TraceError("Investigation name must contain 1 to 120 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise TraceError("Investigation name cannot contain control characters")
    return value.strip()


def create_investigation(root, name, *, board=None, fixture=None, seeds=None, run_defaults=None, blockchain="liquid"):
    blockchain = validate_blockchain(blockchain)
    name = _name(name)
    defaults = validate_settings(run_defaults or {})
    if board:
        from .cli import board_id
        board = board_id(board)
    if fixture is not None:
        fixture = Path(fixture).expanduser().resolve()
        if not fixture.is_file():
            raise TraceError("The investigation's fixture file does not exist")
    normalized = sorted(set(f"{txid}:{index}" for txid, index in map(parse_outpoint, seeds or [])))
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "investigation"
    identity = uuid.uuid4().hex
    case = root / f"{slug}-{identity[:8]}"
    case.mkdir()  # Never reuse an existing investigation, even if names match.
    save_json(case / "case.json", {
        "schema_version": 1, "case_id": identity, "name": name, "blockchain": blockchain,
        "created_at": now(), "miro_board": board or None,
        "fixture": str(fixture) if fixture else None, "seeds": normalized,
        "run_defaults": defaults,
    })
    return case


def list_investigations(root):
    root = Path(root)
    if not root.exists():
        return []
    entries = []
    for case in root.iterdir():
        if not case.is_dir() or not (case / "case.json").exists():
            continue
        try:
            metadata = read_case(case)
        except (TraceError, OSError, ValueError) as error:
            metadata = {"name": case.name, "error": str(error)}
        entries.append((case, metadata))
    return sorted(entries, key=lambda entry: (str(entry[1].get("name") or entry[0].name).casefold(), entry[0].name))
