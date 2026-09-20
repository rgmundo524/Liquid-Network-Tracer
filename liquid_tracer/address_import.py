"""Offline, reviewed bulk attributions. One atomic settings write; no chain queries."""

import copy
import csv
import fcntl
import io
import json
import os
import re
import stat
import uuid
from pathlib import Path

from .address_activity import validate_address
from .common import TraceError, canonical, digest, now, save_json
from .csv_import import csv_rows
from .services import _text, load_services, rule_fields, validate_rule_fields, notes_for

MAX_BYTES = 512 * 1024
MAX_ROWS = 5000
MAX_ERRORS = 50
FIELDS = {"address", "name", "notes", "confidence", "source",
          "observed_at", "stop_tracing", "hop_limit", "enabled"}
ALIASES = {"value": "address", "entity": "name", "service_name": "name", "label": "name",
           "rationale": "notes", "stop": "stop_tracing"}
FORMATS = {"auto", "csv", "json", "text"}
NOTICE = ("Importing records your assessment, not independent verification of ownership. "
          "Addresses use the same text validation as Address review; network/checksums are not checked offline. "
          "Use the public address form shown by the trace, not a confidential-address alias. "
          "Active address stops apply to the first run and continuations, including seed outputs. "
          "No blockchain requests or Miro changes are made. Existing evidence is retained.")
TEMPLATE = ("Address,Name,confidence,stop_tracing,hop_limit,source,notes\n"
            "REPLACE_WITH_LIQUID_ADDRESS_1,Example Exchange,suspected,true,,Investigator research,Explain the evidence\n"
            "REPLACE_WITH_LIQUID_ADDRESS_2,Client wallet,confirmed,false,,Client records,Continue tracing\n"
            "REPLACE_WITH_LIQUID_ADDRESS_3,Service deposit,suspected,false,1,Investigator research,Follow one consolidation hop\n")


def read_import(path):
    """Read a deliberately selected regular local file, never an unbounded stream."""
    path = Path(path).expanduser()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise TraceError("Choose a regular CSV, JSON, or text file")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise TraceError("Address import exceeds 512 KiB")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise TraceError("Address import must be UTF-8 text (CSV, JSON, or plain text)") from None


def _boolean(value, name, default):
    if value is None or value == "":
        return default
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        value = value.strip().lower()
        if value in ("true", "yes", "1"):
            return True
        if value in ("false", "no", "0"):
            return False
    raise TraceError(name + " must be true/false, yes/no, or 1/0")


def _header(value):
    if not isinstance(value, str):
        raise TraceError("Import field names must be text")
    key = value.strip().lower().replace(" ", "_").replace("-", "_")
    return ALIASES.get(key, key)


def _value(fields, key, default=""):
    value = fields.get(key)
    return default if value is None or value == "" else value


def _row(value):
    if isinstance(value, str):
        value = {"address": value}
    if not isinstance(value, dict):
        raise TraceError("Each entry must be an address or an attribution object")
    fields = {}
    for key, item in value.items():
        name = _header(key)
        if name in fields:
            raise TraceError("Duplicate field after normalizing column names")
        if name == "kind" and item == "address":
            continue
        if name == "network" and isinstance(item, str) and item.strip().lower() == "liquid":
            continue
        if name not in FIELDS:
            raise TraceError("Unsupported field; use the supplied Liquid address-import template")
        fields[name] = item
    address = validate_address(fields.get("address"))
    metadata = {"confidence": _text(_value(fields, "confidence", "suspected"), "Confidence", 30).casefold(),
                "source": _text(_value(fields, "source", "Investigator designation"), "Source", 1000, required=True),
                "observed_at": _text(_value(fields, "observed_at"), "Observation date", 80),
                "stop_tracing": _boolean(fields.get("stop_tracing"), "Stop tracing", True),
                "hop_limit": fields.get("hop_limit")}
    validate_rule_fields(metadata)
    return {"address": address, "name": _text(_value(fields, "name"), "Name", 120),
            "notes": _text(_value(fields, "notes"), "Notes", 4000, multiline=True),
            "enabled": _boolean(fields.get("enabled"), "Enabled", True), **metadata}


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TraceError("Duplicate JSON field")
        result[key] = value
    return result


def parse_import(text, format="auto"):
    """Validate the entire batch before proposing changes, including duplicate rows."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_BYTES:
        raise TraceError("Address import must contain at most 512 KiB of UTF-8 text")
    if not isinstance(format, str) or format not in FORMATS:
        raise TraceError("Choose auto, csv, json, or text import format")
    text = text.lstrip("\ufeff")
    if not text.strip():
        raise TraceError("Choose a file or paste addresses before previewing the import")
    try:
        if format == "auto":
            if text.lstrip().startswith(("[", "{")):
                format = "json"
            else:
                try:
                    first = next(csv.reader(io.StringIO(text.strip(), newline=""), strict=True), [])
                except csv.Error:
                    # A long whitespace-separated address list is one oversized
                    # CSV cell. Preserve text detection without relaxing CSV parsing.
                    first = [cell.strip('"') for cell in text.strip().splitlines()[0].split(",")]
                format = "csv" if any(_header(cell) == "address" for cell in first) else "text"
        if format == "json":
            raw = json.loads(text, object_pairs_hook=_json_pairs)
            if not isinstance(raw, list):
                raise TraceError("JSON imports must be an array of addresses or attribution objects")
            rows = enumerate(raw, 1)
        elif format == "csv":
            rows = csv_rows(text, fields=FIELDS | {"kind", "network"}, required={"address"},
                            normalize=_header, missing_message="CSV needs one address column")
        else:
            rows = ((number, part) for number, line in enumerate(text.splitlines(), 1)
                    if not line.lstrip().startswith("#") for part in re.split(r"[,;\s]+", line.strip()) if part)
        accepted, errors, duplicates, total = {}, [], 0, 0
        for number, raw in rows:
            total += 1
            if total > MAX_ROWS:
                raise TraceError("Address import exceeds 5,000 rows; split the file into smaller batches")
            try:
                if isinstance(raw, dict) and (None in raw or any(item is None for item in raw.values())):
                    raise TraceError("CSV row has a different number of fields than its header")
                row = _row(raw)
                existing = accepted.get(row["address"])
                if existing:
                    if not _same_assessment(existing["rule"], row):
                        raise TraceError("Conflicting entries for the same address (first at row " + str(existing["row"]) + ")")
                    duplicates += 1
                else:
                    accepted[row["address"]] = {"row": number, "rule": row}
            except (TraceError, TypeError) as error:
                errors.append({"row": number, "message": str(error) if isinstance(error, TraceError) else "Invalid field type"})
                if len(errors) >= MAX_ERRORS:
                    break
    except (csv.Error, json.JSONDecodeError, RecursionError):
        raise TraceError("Malformed CSV or JSON; fix the file before importing") from None
    if not total:
        raise TraceError("No address rows were found")
    return {"format": format, "rows": list(accepted.values()), "errors": errors,
            "duplicates": duplicates, "input_rows": total, "source_sha256": digest(text.encode("utf-8"))}


def _semantic(rule):
    return {key: rule[key] for key in ("address", "name", "enabled")} | {"notes": notes_for(rule)} | rule_fields(rule)


def _same_assessment(left, right):
    """Compare names without case, preserving original spelling and all other fields.

    Only this comparison copy is folded. Address identity, source references,
    notes, reviewed input hashes, and stored/displayed names stay unchanged.
    """
    left, right = _semantic(left), _semantic(right)
    left["name"], right["name"] = left["name"].casefold(), right["name"].casefold()
    return left == right


def _plan(settings, parsed, policy):
    if not isinstance(policy, str) or policy not in ("keep", "replace"):
        raise TraceError("Conflict policy must be keep or replace")
    changes = []
    counts = {"add": 0, "replace": 0, "unchanged": 0, "keep": 0}
    for entry in parsed["rows"]:
        new = entry["rule"]
        previous = settings["rules"].get(new["address"])
        action = "add" if previous is None else "unchanged" if _same_assessment(previous, new) else policy
        counts[action] += 1
        changes.append({**entry, "action": action, "previous": _semantic(previous) if previous else None})
    valid = not parsed["errors"]
    approval = digest(canonical({"case_id": settings["case_id"], "settings_sha256": digest(canonical(settings)),
        "source_sha256": parsed["source_sha256"], "rows": parsed["rows"], "policy": policy})) if valid else None
    stops = sum(entry["rule"]["enabled"] and entry["rule"]["stop_tracing"] for entry in changes
                if entry["action"] in ("add", "replace"))
    return {"schema_version": 1, "case_id": settings["case_id"], "base_revision": settings["revision"],
            "valid": valid, "approval_sha256": approval, "format": parsed["format"], "policy": policy,
            "counts": counts, "input_rows": parsed["input_rows"], "unique_addresses": len(parsed["rows"]),
            "duplicate_rows": parsed["duplicates"], "active_stops_to_save": stops,
            "errors": parsed["errors"], "changes": changes, "notice": NOTICE}


def preview_import(case, text, *, format="auto", policy="keep"):
    return _plan(load_services(case), parse_import(text, format), policy)


def apply_import(case, text, *, approval_sha256, format="auto", policy="keep"):
    """Revalidate the reviewed batch under case locks, then save all rows or none."""
    if not isinstance(approval_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", approval_sha256):
        raise TraceError("Preview the import and approve its exact approval_sha256 before saving")
    parsed = parse_import(text, format)
    case = Path(case)
    # Refuse writes while tracing or a compact layout uses the assessment snapshot.
    with (case / "trace.lock").open("a") as trace_lock:
        try:
            fcntl.flock(trace_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("A trace, lookup, or reviewed layout is active; import after it finishes") from None
        with (case / "case.lock").open("a") as case_lock:
            fcntl.flock(case_lock, fcntl.LOCK_EX)
            settings = load_services(case)
            plan = _plan(settings, parsed, policy)
            if not plan["valid"] or plan["approval_sha256"] != approval_sha256:
                raise TraceError("The file, import options, or assessments changed; preview and approve the import again")
            batch_id, stamp = uuid.uuid4().hex, now()
            changed = 0
            for entry in plan["changes"]:
                if entry["action"] not in ("add", "replace"):
                    continue
                new = entry["rule"]
                previous = settings["rules"].get(new["address"])
                rule = {**new, "created_at": previous["created_at"] if previous else stamp, "updated_at": stamp}
                settings["revision"] += 1
                settings["history"].append({"revision": settings["revision"], "changed_at": stamp,
                    "address": new["address"], "previous": copy.deepcopy(previous), "rule": copy.deepcopy(rule),
                    "import_id": batch_id, "import_sha256": parsed["source_sha256"], "import_row": entry["row"]})
                settings["schema_version"] = 2
                settings["rules"][new["address"]] = rule
                changed += 1
            if changed:
                save_json(case / "services.json", settings)
            return {"import_id": batch_id if changed else None, "changed": changed,
                    "revision": settings["revision"], "counts": plan["counts"],
                    "duplicate_rows": plan["duplicate_rows"], "notice": NOTICE}
