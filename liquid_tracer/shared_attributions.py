"""Reviewed workspace attribution library, independent of investigation controls."""

import copy
import csv
import fcntl
import io
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import address_import
from .common import TraceError, canonical, digest, now
from .csv_import import csv_rows

FILENAME = ".shared-attributions.json"
LOCKNAME = ".shared-attributions.lock"
MAX_LIBRARY_BYTES = 64 * 1024 * 1024
MAX_LIBRARY_RULES = 50000
NOTICE = ("Shared attributions are investigator assessments, not independent ownership verification. "
          "Only investigations that opt in use this library; local address rules override shared entries, "
          "including disabled local rules. Shared names, confidence, source, notes, observation dates, "
          "and enabled state can be reused. Stop-tracing and hop-limit controls are ignored and never shared. "
          "Saved runs and previews are unchanged; no blockchain requests or Miro changes are made.")
_RULE_FIELDS = {"address", "name", "notes", "confidence", "source", "observed_at", "enabled",
                "stop_tracing", "hop_limit", "created_at", "updated_at"}
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _root(root):
    path = Path(root).expanduser().absolute()
    if ".." in path.parts or any(part.is_symlink() for part in (path, *path.parents)):
        raise TraceError("Shared attribution paths must not contain symbolic links or parent traversal")
    return path


@contextmanager
def _directory(root, *, create=False):
    descriptor = os.open(root.anchor, _DIRECTORY)
    try:
        for part in root.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, _DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _empty(root):
    return {"schema_version": 1, "library_id": digest(str(root).encode("utf-8")),
            "revision": 0, "rules": {}, "history": []}


def _regular(descriptor, label):
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise TraceError("Shared attribution " + label + " must be an ordinary file, not a link")
    return info


def _rule(rule):
    if (not isinstance(rule, dict) or set(rule) != _RULE_FIELDS
            or rule.get("stop_tracing") is not False or rule.get("hop_limit") is not None
            or type(rule.get("enabled")) is not bool):
        raise TraceError("Invalid shared attribution rule; tracing controls cannot be shared")
    normalized = address_import._row({key: value for key, value in rule.items()
                                      if key not in {"created_at", "updated_at"}})
    if normalized != {key: value for key, value in rule.items() if key in normalized}:
        raise TraceError("Shared attribution fields must use their validated values")
    from datetime import datetime
    for field in ("created_at", "updated_at"):
        value = rule[field]
        if not isinstance(value, str) or not 1 <= len(value) <= 80:
            raise TraceError("Invalid shared attribution timestamp")
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise TraceError("Invalid shared attribution timestamp") from None
    return rule


def _validate(value):
    if (not isinstance(value, dict) or set(value) != {"schema_version", "library_id", "revision", "rules", "history"}
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or not isinstance(value.get("library_id"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["library_id"])
            or type(value.get("revision")) is not int or not 0 <= value["revision"] <= 2 ** 53 - 1
            or not isinstance(value.get("rules"), dict) or len(value["rules"]) > MAX_LIBRARY_RULES
            or not isinstance(value.get("history"), list) or len(value["history"]) != value["revision"]):
        raise TraceError("Invalid shared attribution library; restore its saved file")
    for address, rule in value["rules"].items():
        if _rule(rule)["address"] != address:
            raise TraceError("Invalid shared attribution address identity")
    previous = {}
    for revision, entry in enumerate(value["history"], 1):
        if (not isinstance(entry, dict) or set(entry) != {"revision", "changed_at", "address", "previous", "rule",
                                                       "import_id", "import_sha256", "import_row"}
                or type(entry.get("revision")) is not int or entry["revision"] != revision
                or not isinstance(entry.get("import_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", entry["import_id"])
                or not isinstance(entry.get("import_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", entry["import_sha256"])
                or type(entry.get("import_row")) is not int or entry["import_row"] < 1):
            raise TraceError("Invalid shared attribution history")
        rule = _rule(entry["rule"])
        address = rule["address"]
        if (entry["address"] != address or entry["changed_at"] != rule["updated_at"]
                or entry["previous"] != previous.get(address)):
            raise TraceError("Shared attribution history does not match its revisions")
        previous[address] = rule
    if previous != value["rules"]:
        raise TraceError("Shared attribution history does not match the current library")
    return value


def _read(descriptor, root):
    try:
        file_descriptor = os.open(FILENAME, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=descriptor)
    except FileNotFoundError:
        return _empty(root)
    with os.fdopen(file_descriptor, "rb") as stream:
        info = _regular(stream.fileno(), "library")
        if info.st_size > MAX_LIBRARY_BYTES:
            raise TraceError("Shared attribution library exceeds its 64 MiB safety limit")
        raw = stream.read(MAX_LIBRARY_BYTES + 1)
    if len(raw) > MAX_LIBRARY_BYTES:
        raise TraceError("Shared attribution library exceeds its 64 MiB safety limit")
    return _validate(json.loads(raw.decode("utf-8"), object_pairs_hook=address_import._json_pairs))


def load_library(root):
    """Read one atomic version without creating workspace files or directories."""
    root = _root(root)
    try:
        with _directory(root) as descriptor:
            return _read(descriptor, root)
    except FileNotFoundError:
        return _empty(root)
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        raise TraceError("Unable to read the shared attribution library safely") from error


def catalog(root, query="", offset=0, limit=100):
    if (not isinstance(query, str) or len(query) > 512 or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 500):
        raise TraceError("Choose a text search and a valid shared attribution page")
    library = load_library(root)
    needle = query.strip().casefold()
    rows = [rule for address, rule in sorted(library["rules"].items())
            if not needle or needle in " ".join(str(rule[key]) for key in
                ("address", "name", "notes", "source", "confidence")).casefold()]
    return {"library_id": library["library_id"], "revision": library["revision"], "total": len(rows),
            "offset": offset, "limit": limit, "rows": copy.deepcopy(rows[offset:offset + limit]), "notice": NOTICE}


def _parse(text, format):
    if not isinstance(text, str):
        raise TraceError("Shared attribution import must be UTF-8 text")
    try:
        source = text.encode("utf-8")
    except UnicodeEncodeError:
        raise TraceError("Shared attribution import must be valid UTF-8 text") from None
    if len(source) > address_import.MAX_BYTES:
        raise TraceError("Shared attribution import exceeds 512 KiB")
    if not isinstance(format, str) or format not in address_import.FORMATS:
        raise TraceError("Choose auto, csv, json, or text import format")
    requested = format
    text = text.lstrip("\ufeff")
    if not text.strip():
        raise TraceError("Choose a file or paste shared attributions before previewing")
    if format == "auto":
        if text.lstrip().startswith(("[", "{")):
            format = "json"
        else:
            try:
                first = next(csv.reader(io.StringIO(text.strip(), newline=""), strict=True), [])
            except csv.Error:
                first = [cell.strip('"') for cell in text.strip().splitlines()[0].split(",")]
            format = "csv" if any(address_import._header(cell) == "address" for cell in first) else "text"
    try:
        if format == "json":
            raw = json.loads(text, object_pairs_hook=address_import._json_pairs)
            if not isinstance(raw, list):
                raise TraceError("JSON imports must be an array of address attributions")
            rows = enumerate(raw, 1)
        elif format == "csv":
            rows = csv_rows(text, fields=address_import.FIELDS | {"kind", "network"}, required={"address"},
                            normalize=address_import._header, missing_message="CSV needs one address column")
        else:
            rows = ((number, part) for number, line in enumerate(text.splitlines(), 1)
                    if not line.lstrip().startswith("#") for part in re.split(r"[,;\s]+", line.strip()) if part)
        accepted, errors, duplicates, total = {}, [], 0, 0
        ignored = {"stop_tracing": 0, "hop_limit": 0}
        ignored_rows = 0
        for number, raw in rows:
            total += 1
            if total > address_import.MAX_ROWS:
                raise TraceError("Shared attribution import exceeds 5,000 rows; split the file into smaller batches")
            try:
                if isinstance(raw, dict) and (None in raw or any(item is None for item in raw.values())):
                    # JSON null is a valid empty field; ragged CSV is not.
                    if format == "csv":
                        raise TraceError("CSV row has a different number of fields than its header")
                fields = {"address": raw} if isinstance(raw, str) else raw
                if not isinstance(fields, dict):
                    raise TraceError("Each entry must be an address or an attribution object")
                normalized = {}
                for key, item in fields.items():
                    field = address_import._header(key)
                    if field in normalized:
                        raise TraceError("Duplicate field after normalizing column names")
                    normalized[field] = item
                controls = [field for field in ignored if field in normalized and normalized[field] not in (None, "")]
                for field in controls:
                    ignored[field] += 1
                ignored_rows += bool(controls)
                normalized.update(stop_tracing=False, hop_limit=None)
                rule = address_import._row(normalized)
                existing = accepted.get(rule["address"])
                if existing:
                    if not address_import._same_assessment(existing["rule"], rule):
                        raise TraceError("Conflicting entries for the same address (first at row " + str(existing["row"]) + ")")
                    duplicates += 1
                else:
                    accepted[rule["address"]] = {"row": number, "rule": rule}
            except (TraceError, TypeError) as error:
                errors.append({"row": number, "message": str(error) if isinstance(error, TraceError) else "Invalid field type"})
                if len(errors) >= address_import.MAX_ERRORS:
                    break
    except (csv.Error, json.JSONDecodeError, RecursionError):
        raise TraceError("Malformed CSV or JSON; fix the file before importing") from None
    if not total:
        raise TraceError("No address rows were found")
    return {"format": format, "requested_format": requested, "rows": list(accepted.values()), "errors": errors,
            "duplicates": duplicates, "input_rows": total, "source_sha256": digest(source),
            "ignored_controls": ignored, "ignored_control_rows": ignored_rows}


def _plan(root, library, parsed, policy):
    settings = {**library, "case_id": library["library_id"],
                "workspace_sha256": digest(str(root).encode("utf-8")),
                "import_format": parsed["requested_format"]}
    result = address_import._plan(settings, parsed, policy)
    result.pop("case_id")
    return {**result, "library_id": library["library_id"], "notice": NOTICE,
            "ignored_controls": parsed["ignored_controls"], "ignored_control_rows": parsed["ignored_control_rows"]}


def preview_import(root, text, *, format="auto", policy="keep"):
    root = _root(root)
    return _plan(root, load_library(root), _parse(text, format), policy)


def _write(descriptor, library):
    raw = json.dumps(library, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    if len(raw) > MAX_LIBRARY_BYTES:
        raise TraceError("Shared attribution library would exceed its 64 MiB safety limit")
    temporary = ".shared-attributions-" + uuid.uuid4().hex + ".tmp"
    try:
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
        with os.fdopen(handle, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, FILENAME, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except FileNotFoundError:
            pass


def apply_import(root, text, *, approval_sha256, format="auto", policy="keep"):
    if not isinstance(approval_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", approval_sha256):
        raise TraceError("Preview and approve the exact shared attribution import before saving")
    root, parsed = _root(root), _parse(text, format)
    try:
        with _directory(root, create=True) as descriptor:
            lock = os.open(LOCKNAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=descriptor)
            with os.fdopen(lock, "a") as stream:
                _regular(stream.fileno(), "lock")
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                library = _read(descriptor, root)
                plan = _plan(root, library, parsed, policy)
                if not plan["valid"] or plan["approval_sha256"] != approval_sha256:
                    raise TraceError("The shared library, file, or import options changed; preview and approve again")
                changed, stamp, batch = 0, now(), uuid.uuid4().hex
                for entry in plan["changes"]:
                    if entry["action"] not in ("add", "replace"):
                        continue
                    rule = entry["rule"]
                    previous = library["rules"].get(rule["address"])
                    updated = {**rule, "created_at": previous["created_at"] if previous else stamp, "updated_at": stamp}
                    library["revision"] += 1
                    library["rules"][rule["address"]] = updated
                    library["history"].append({"revision": library["revision"], "changed_at": stamp,
                        "address": rule["address"], "previous": copy.deepcopy(previous), "rule": copy.deepcopy(updated),
                        "import_id": batch, "import_sha256": parsed["source_sha256"], "import_row": entry["row"]})
                    changed += 1
                if len(library["rules"]) > MAX_LIBRARY_RULES:
                    raise TraceError("Shared attribution library exceeds 50,000 addresses")
                if changed:
                    _write(descriptor, library)
                return {"library_id": library["library_id"], "import_id": batch if changed else None,
                        "changed": changed, "revision": library["revision"], "counts": plan["counts"],
                        "duplicate_rows": plan["duplicate_rows"], "ignored_controls": parsed["ignored_controls"],
                        "ignored_control_rows": parsed["ignored_control_rows"], "notice": NOTICE}
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        raise TraceError("Unable to save the shared attribution library safely") from error


def export_library(root):
    """Export import-sized CSV parts; tracing controls are explicitly inactive."""
    from .input_export import _parts, _zip
    library = load_library(root)
    parts, count = _parts(library, "attributions")
    multiple = len(parts) > 1
    return {"filename": "shared-attributions.zip" if multiple else "shared-attributions.csv",
            "content_type": "application/zip" if multiple else "text/csv; charset=utf-8",
            "data": _zip(parts) if multiple else parts[0][1], "revision": library["revision"],
            "library_id": library["library_id"], "rows": count, "parts": len(parts)}
