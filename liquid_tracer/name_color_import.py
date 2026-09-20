"""Reviewed, offline name-group color imports with one atomic settings update."""

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
from .name_colors import _names, color_value, name_key
from .services import load_services

MAX_BYTES = 512 * 1024
MAX_ROWS = 5000
MAX_ERRORS = 50
FORMATS = {"auto", "csv", "json"}
FIELDS = {"name", "color"}
NOTICE = ("Import attribution names first. Matching ignores capitalization; one color applies to all "
          "addresses with that name. Blank or null colors request the default; choose replace to clear "
          "an existing assignment. Selected seeds retain their configured seed color. "
          "This saves local name colors only, preserving attribution evidence and graph role colors. "
          "Regenerate previews or sync Miro to update the graph. No trace or network request is started.")
TEMPLATE = ("Name,Color\n"
            "Perp,#f0abfc\n"
            "Example Exchange,#93c5fd\n"
            "Client wallet,#bbf7d0\n")


def read_import(path):
    """Read a deliberately selected regular file without blocking on streams."""
    path = Path(path).expanduser()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise TraceError("Choose a regular CSV or JSON file for name colors")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise TraceError("Name color import exceeds 512 KiB")
    try:
        # Preserve the exact source for the approval hash, including any BOM.
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise TraceError("Name color import must be UTF-8 text (CSV or JSON)") from None


def _header(value):
    if not isinstance(value, str):
        raise TraceError("Import field names must be text")
    return value.strip().casefold()


def _row(value):
    if not isinstance(value, dict):
        raise TraceError("Each entry must be an object with Name and Color fields")
    fields = {}
    for key, item in value.items():
        field = _header(key)
        if field in fields:
            raise TraceError("Duplicate field after normalizing column names")
        if field not in FIELDS:
            raise TraceError("Unsupported field; name color imports accept only Name and Color")
        fields[field] = item
    if fields.keys() != FIELDS:
        raise TraceError("Each entry requires both Name and Color")
    key = name_key(fields["name"])
    color = fields["color"]
    color = None if color is None or isinstance(color, str) and not color.strip() else color_value(color)
    return {"name": fields["name"].strip(), "key": key, "color": color}


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TraceError("Duplicate JSON field")
        result[key] = value
    return result


def parse_import(text, format="auto"):
    """Validate a complete CSV/JSON batch and coalesce identical name assignments."""
    if not isinstance(text, str):
        raise TraceError("Name color import must contain at most 512 KiB of UTF-8 text")
    try:
        source = text.encode("utf-8")
    except UnicodeEncodeError:
        raise TraceError("Name color import must be valid UTF-8 text") from None
    if len(source) > MAX_BYTES:
        raise TraceError("Name color import exceeds 512 KiB")
    if not isinstance(format, str) or format not in FORMATS:
        raise TraceError("Choose auto, csv, or json import format")
    requested_format = format
    text = text.lstrip("\ufeff")
    if not text.strip():
        raise TraceError("Choose a file or paste Name,Color rows before previewing the import")
    if format == "auto":
        format = "json" if text.lstrip().startswith(("[", "{")) else "csv"
    try:
        if format == "json":
            raw = json.loads(text, object_pairs_hook=_json_pairs)
            if not isinstance(raw, list):
                raise TraceError("JSON imports must be an array of Name and Color objects")
            rows = enumerate(raw, 1)
        else:
            rows = csv_rows(text, fields=FIELDS, required=FIELDS, normalize=_header,
                            missing_message="CSV requires Name and Color columns")
        accepted, errors, duplicates, total = {}, [], 0, 0
        for number, raw in rows:
            total += 1
            if total > MAX_ROWS:
                raise TraceError("Name color import exceeds 5,000 rows; split the file into smaller batches")
            try:
                if format == "csv" and (None in raw or any(value is None for value in raw.values())):
                    raise TraceError("CSV row has a different number of fields than its header")
                row = _row(raw)
                previous = accepted.get(row["key"])
                if previous is not None:
                    if previous["color"] != row["color"]:
                        raise TraceError("Conflicting colors for the same case-insensitive name "
                                         "(first at row " + str(previous["row"]) + ")")
                    duplicates += 1
                else:
                    accepted[row["key"]] = {"row": number, **row}
            except TraceError as error:
                if len(errors) < MAX_ERRORS:
                    errors.append({"row": number, "message": str(error)})
    except (csv.Error, json.JSONDecodeError, RecursionError):
        raise TraceError("Malformed CSV or JSON; fix the file before importing") from None
    if not total:
        raise TraceError("No name color rows were found")
    return {"format": format, "requested_format": requested_format, "rows": list(accepted.values()),
            "errors": errors, "duplicates": duplicates, "input_rows": total,
            "source_sha256": digest(source)}


def _plan(settings, parsed, policy):
    if not isinstance(policy, str) or policy not in ("keep", "replace"):
        raise TraceError("Conflict policy must be keep or replace")
    groups, colors = _names(settings), settings.get("name_colors", {})
    changes, errors = [], list(parsed["errors"])
    counts = {"add": 0, "replace": 0, "clear": 0, "unchanged": 0, "keep": 0}
    for entry in parsed["rows"]:
        key, color = entry["key"], entry["color"]
        if key not in groups:
            if len(errors) < MAX_ERRORS:
                errors.append({"row": entry["row"], "message": "Unknown attribution name; import or save "
                               "the attribution name before assigning its color"})
            continue
        previous = colors.get(key)
        if previous == color:
            action = "unchanged"
        elif previous is None:
            action = "add"
        elif policy == "keep":
            action = "keep"
        else:
            action = "clear" if color is None else "replace"
        counts[action] += 1
        changes.append({**entry, "previous": previous, "action": action, "addresses": groups[key]["addresses"]})
    valid = not errors
    approval = digest(canonical({"kind": "name_color_import", "case_id": settings["case_id"],
        "settings_sha256": digest(canonical(settings)), "source_sha256": parsed["source_sha256"],
        "requested_format": parsed["requested_format"], "format": parsed["format"],
        "policy": policy})) if valid else None
    return {"schema_version": 1, "case_id": settings["case_id"], "base_revision": settings["revision"],
            "valid": valid, "approval_sha256": approval, "format": parsed["format"], "policy": policy,
            "counts": counts, "input_rows": parsed["input_rows"], "unique_names": len(parsed["rows"]),
            "duplicate_rows": parsed["duplicates"], "errors": errors, "changes": changes, "notice": NOTICE}


def preview_import(case, text, *, format="auto", policy="keep"):
    return _plan(load_services(case), parse_import(text, format), policy)


def apply_import(case, text, *, approval_sha256, format="auto", policy="keep"):
    """Revalidate the reviewed input under both locks, then write all changes once."""
    if not isinstance(approval_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", approval_sha256):
        raise TraceError("Preview the name color import and approve its exact approval_sha256 before saving")
    parsed = parse_import(text, format)
    case = Path(case)
    with (case / "trace.lock").open("a") as trace_lock:
        try:
            fcntl.flock(trace_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("A trace, lookup, or reviewed layout is active; import name colors after it finishes") from None
        with (case / "case.lock").open("a") as case_lock:
            fcntl.flock(case_lock, fcntl.LOCK_EX)
            settings = load_services(case)
            plan = _plan(settings, parsed, policy)
            if not plan["valid"] or plan["approval_sha256"] != approval_sha256:
                raise TraceError("The file, import options, assessments, or colors changed; "
                                 "preview and approve the name color import again")
            before = settings.get("name_colors", {})
            after = dict(before)
            changed = 0
            for entry in plan["changes"]:
                if entry["action"] not in ("add", "replace", "clear"):
                    continue
                if entry["color"] is None:
                    after.pop(entry["key"], None)
                else:
                    after[entry["key"]] = entry["color"]
                changed += 1
            batch_id = uuid.uuid4().hex if changed else None
            if changed:
                settings["name_colors"] = after
                settings["revision"] += 1
                settings["history"].append({"revision": settings["revision"], "changed_at": now(),
                    "type": "name_colors", "previous": copy.deepcopy(before), "name_colors": dict(after),
                    "import_id": batch_id, "import_sha256": parsed["source_sha256"],
                    "import_format": parsed["format"], "import_policy": policy})
                save_json(case / "services.json", settings)
            return {"import_id": batch_id, "changed": changed, "revision": settings["revision"],
                    "counts": plan["counts"], "duplicate_rows": plan["duplicate_rows"], "notice": NOTICE}
