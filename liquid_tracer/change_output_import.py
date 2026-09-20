"""Reviewed, offline change-output imports with a single atomic settings update."""

import copy
import csv
import fcntl
import json
import os
import re
import stat
import uuid
from pathlib import Path

from .common import TraceError, canonical, digest, now, save_json
from .csv_import import csv_rows
from .change_outputs import (_saved_state, check_known_output, notes_value, validate_txid, vout_value)
from .services import load_services

MAX_BYTES = 512 * 1024
MAX_ROWS = 5000
MAX_ERRORS = 50
FORMATS = {"auto", "csv", "json"}
FIELDS = {"txid", "changevout", "notes"}
REQUIRED = {"txid", "changevout"}
NOTICE = ("One investigator-designated change output per transaction. Blank or null ChangeVout requests "
          "clearing; choose replace to clear an existing designation. Unknown transaction hashes may be "
          "saved before a trace reaches them. Saved transaction outputs are checked without network requests. "
          "This changes layout preferences only, preserving evidence and tracing. Regenerate a preview or "
          "use Sync and reorganize to apply them. Transactions without change keep the normal ELK layout.")
TEMPLATE = "Txid,ChangeVout,Notes\n" + "0" * 64 + ",0,Replace this example with a transaction hash\n"


def read_import(path):
    path = Path(path).expanduser()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise TraceError("Choose a regular CSV or JSON file for change outputs")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise TraceError("Change output import exceeds 512 KiB")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise TraceError("Change output import must be UTF-8 text (CSV or JSON)") from None


def _header(value):
    if not isinstance(value, str):
        raise TraceError("Import field names must be text")
    return value.strip().casefold()


def _row(value, format):
    if not isinstance(value, dict):
        raise TraceError("Each entry must be an object with Txid and ChangeVout fields")
    fields = {}
    for key, item in value.items():
        key = _header(key)
        if key in fields:
            raise TraceError("Duplicate field after normalizing column names")
        if key not in FIELDS:
            raise TraceError("Change imports accept Txid, ChangeVout, and optional Notes fields only")
        fields[key] = item
    if not REQUIRED <= fields.keys():
        raise TraceError("Each entry requires Txid and ChangeVout")
    vout = fields["changevout"]
    if isinstance(vout, str) and not vout.strip():
        vout = None
    elif format == "csv" and isinstance(vout, str):
        if not re.fullmatch(r"[0-9]{1,10}", vout.strip()):
            raise TraceError("ChangeVout must be a nonnegative output index, or blank to clear")
        vout = int(vout.strip())
    return {"txid": validate_txid(fields["txid"]), "vout": vout_value(vout),
            "notes": notes_value(fields.get("notes", ""))}


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TraceError("Duplicate JSON field")
        result[key] = value
    return result


def parse_import(text, format="auto"):
    if not isinstance(text, str):
        raise TraceError("Change output import must contain at most 512 KiB of UTF-8 text")
    try:
        source = text.encode("utf-8")
    except UnicodeEncodeError:
        raise TraceError("Change output import must be valid UTF-8 text") from None
    if len(source) > MAX_BYTES:
        raise TraceError("Change output import exceeds 512 KiB")
    if not isinstance(format, str) or format not in FORMATS:
        raise TraceError("Choose auto, csv, or json import format")
    requested_format = format
    text = text.lstrip("\ufeff")
    if not text.strip():
        raise TraceError("Choose a file or paste Txid,ChangeVout rows before previewing")
    if format == "auto":
        format = "json" if text.lstrip().startswith(("[", "{")) else "csv"
    try:
        if format == "json":
            raw = json.loads(text, object_pairs_hook=_json_pairs)
            if not isinstance(raw, list):
                raise TraceError("JSON imports must be an array of Txid and ChangeVout objects")
            rows = enumerate(raw, 1)
        else:
            rows = csv_rows(text, fields=FIELDS, required=REQUIRED, normalize=_header,
                            missing_message="CSV requires Txid and ChangeVout columns; Notes is optional")
        accepted, errors, duplicates, total = {}, [], 0, 0
        for number, raw in rows:
            total += 1
            if total > MAX_ROWS:
                raise TraceError("Change output import exceeds 5,000 rows; split it into smaller batches")
            try:
                if format == "csv" and (None in raw or any(value is None for value in raw.values())):
                    raise TraceError("CSV row has a different number of fields than its header")
                row = _row(raw, format)
                previous = accepted.get(row["txid"])
                if previous is not None:
                    if (previous["vout"], previous["notes"]) != (row["vout"], row["notes"]):
                        raise TraceError("Conflicting change designations for the same transaction (first at row "
                                         + str(previous["row"]) + ")")
                    duplicates += 1
                else:
                    accepted[row["txid"]] = {"row": number, **row}
            except TraceError as error:
                if len(errors) < MAX_ERRORS:
                    errors.append({"row": number, "message": str(error)})
    except (csv.Error, json.JSONDecodeError, RecursionError):
        raise TraceError("Malformed CSV or JSON; fix the file before importing") from None
    if not total:
        raise TraceError("No change output rows were found")
    return {"format": format, "requested_format": requested_format, "rows": list(accepted.values()),
            "errors": errors, "duplicates": duplicates, "input_rows": total, "source_sha256": digest(source)}


def _plan(case, settings, parsed, policy):
    if not isinstance(policy, str) or policy not in ("keep", "replace"):
        raise TraceError("Conflict policy must be keep or replace")
    state = _saved_state(case)
    mapping = settings.get("change_outputs", {})
    changes, errors = [], list(parsed["errors"])
    counts = {"add": 0, "replace": 0, "clear": 0, "unchanged": 0, "keep": 0}
    for entry in parsed["rows"]:
        txid, vout, notes = entry["txid"], entry["vout"], entry["notes"]
        try:
            check_known_output(state, txid, vout)
        except TraceError as error:
            if len(errors) < MAX_ERRORS:
                errors.append({"row": entry["row"], "message": str(error)})
            continue
        previous = mapping.get(txid)
        if (previous is None and vout is None or previous is not None
                and previous["vout"] == vout and previous["notes"] == notes):
            action = "unchanged"
        elif previous is None:
            action = "add"
        elif policy == "keep":
            action = "keep"
        else:
            action = "clear" if vout is None else "replace"
        counts[action] += 1
        changes.append({**entry, "previous": previous["vout"] if previous else None,
                        "previous_notes": previous["notes"] if previous else "", "action": action})
    valid = not errors
    approval = digest(canonical({"kind": "change_output_import", "case_id": settings["case_id"],
        "settings_sha256": digest(canonical(settings)), "source_sha256": parsed["source_sha256"],
        "evidence_sha256": digest(canonical(state)) if state is not None else None,
        "requested_format": parsed["requested_format"], "format": parsed["format"], "policy": policy})) if valid else None
    return {"schema_version": 1, "case_id": settings["case_id"], "base_revision": settings["revision"],
            "valid": valid, "approval_sha256": approval, "format": parsed["format"], "policy": policy,
            "counts": counts, "input_rows": parsed["input_rows"], "unique_transactions": len(parsed["rows"]),
            "duplicate_rows": parsed["duplicates"], "errors": errors, "changes": changes, "notice": NOTICE}


def preview_import(case, text, *, format="auto", policy="keep"):
    return _plan(case, load_services(case), parse_import(text, format), policy)


def apply_import(case, text, *, approval_sha256, format="auto", policy="keep"):
    if not isinstance(approval_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", approval_sha256):
        raise TraceError("Preview the change output import and approve its exact approval_sha256 before saving")
    parsed = parse_import(text, format)
    case = Path(case)
    with (case / "trace.lock").open("a") as trace_lock:
        try:
            fcntl.flock(trace_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("A trace, lookup, or reviewed layout is active; import change outputs after it finishes") from None
        with (case / "case.lock").open("a") as case_lock:
            fcntl.flock(case_lock, fcntl.LOCK_EX)
            settings = load_services(case)
            plan = _plan(case, settings, parsed, policy)
            if not plan["valid"] or plan["approval_sha256"] != approval_sha256:
                raise TraceError("The file, options, settings, or saved evidence changed; preview and approve change outputs again")
            before = settings.get("change_outputs", {})
            after, changed, stamp = copy.deepcopy(before), 0, now()
            for entry in plan["changes"]:
                if entry["action"] not in ("add", "replace", "clear"):
                    continue
                if entry["vout"] is None:
                    after.pop(entry["txid"], None)
                else:
                    after[entry["txid"]] = {"vout": entry["vout"], "notes": entry["notes"], "updated_at": stamp}
                changed += 1
            batch_id = uuid.uuid4().hex if changed else None
            if changed:
                settings["change_outputs"] = after
                settings["revision"] += 1
                settings["history"].append({"revision": settings["revision"], "changed_at": stamp,
                    "type": "change_outputs", "previous": copy.deepcopy(before), "change_outputs": copy.deepcopy(after),
                    "import_id": batch_id, "import_sha256": parsed["source_sha256"],
                    "import_format": parsed["format"], "import_policy": policy})
                save_json(case / "services.json", settings)
            return {"import_id": batch_id, "changed": changed, "revision": settings["revision"],
                    "counts": plan["counts"], "duplicate_rows": plan["duplicate_rows"], "notice": NOTICE}
