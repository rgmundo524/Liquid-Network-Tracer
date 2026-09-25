"""Review several kinds of CSV input together and save them atomically."""

import copy
import csv
import fcntl
import io
import os
import re
import stat
import uuid
from pathlib import Path

from . import address_import, change_output_import, name_color_import
from .common import TraceError, canonical, digest, now, save_json
from .services import _text, load_services

MAX_FILES = 3
MAX_BYTES = 512 * 1024
KINDS = ("attributions", "name-colors", "change-outputs")
NOTICE = ("Review address attributions, name colors, and change outputs together. "
          "Attributions in this upload are checked before name colors, regardless of file order. "
          "Existing values are kept unless you choose replace for that file. "
          "All files must pass review before anything is saved. "
          "This saves local investigation settings without blockchain requests or Miro changes. "
          "Regenerate previews or sync Miro to update the graph.")

_IMPORTERS = {"attributions": address_import, "name-colors": name_color_import,
              "change-outputs": change_output_import}
_REQUIRED = {"attributions": {"address"}, "name-colors": {"name", "color"},
             "change-outputs": {"txid", "changevout"}}


def read_import(path):
    """Read a selected regular file, preserving the exact UTF-8 source and BOM."""
    path = Path(path).expanduser()
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise TraceError("Choose a regular CSV file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_BYTES:
        raise TraceError("Each CSV file must contain at most 512 KiB of UTF-8 text")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise TraceError("CSV files must contain valid UTF-8 text") from None


def _counts():
    return {"add": 0, "replace": 0, "clear": 0, "unchanged": 0, "keep": 0}


def _detect(text):
    try:
        headers = next(csv.reader(io.StringIO(text.lstrip("\ufeff"), newline=""), strict=True), [])
    except csv.Error:
        raise TraceError("Malformed CSV header; fix the file before importing") from None
    matches = [kind for kind in KINDS if _REQUIRED[kind] <=
               {_IMPORTERS[kind]._header(header) for header in headers}]
    if len(matches) > 1:
        raise TraceError("CSV matches more than one input type; choose its type before previewing")
    if not matches:
        raise TraceError("CSV type was not recognized. Use Address; Name and Color; "
                         "or Txid and ChangeVout columns, or choose the file type")
    return matches[0]


def _prepare(files):
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise TraceError("Choose one to three CSV files, with at most one file of each type")
    prepared = []
    for index, value in enumerate(files):
        entry = {"name": "File " + str(index + 1), "kind": "auto", "requested_kind": "auto",
                 "policy": "keep", "errors": [], "parsed": None, "source_sha256": None}
        prepared.append(entry)
        try:
            if not isinstance(value, dict):
                raise TraceError("Each upload must include a file name and CSV text")
            entry["name"] = _text(value.get("name"), "File name", 1000, required=True)
            if set(value) - {"name", "text", "kind", "policy"}:
                raise TraceError("Uploads accept only name, text, kind, and policy fields")
            kind, policy = value.get("kind", "auto"), value.get("policy", "keep")
            if not isinstance(kind, str) or kind not in ("auto", *KINDS):
                raise TraceError("Choose auto, attributions, name-colors, or change-outputs")
            entry["kind"] = entry["requested_kind"] = kind
            if not isinstance(policy, str) or policy not in ("keep", "replace"):
                raise TraceError("Conflict policy must be keep or replace")
            entry["policy"] = policy
            text = value.get("text")
            if not isinstance(text, str):
                raise TraceError("CSV uploads must contain UTF-8 text")
            try:
                source = text.encode("utf-8")
            except UnicodeEncodeError:
                raise TraceError("CSV uploads must contain valid UTF-8 text") from None
            if len(source) > MAX_BYTES:
                raise TraceError("Each CSV file must contain at most 512 KiB of UTF-8 text")
            entry["source_sha256"] = digest(source)
            if kind == "auto":
                kind = entry["kind"] = _detect(text)
            entry["parsed"] = _IMPORTERS[kind].parse_import(text, "csv")
        except TraceError as error:
            entry["errors"].append({"message": str(error)})
    for kind in KINDS:
        duplicates = [entry for entry in prepared if entry["kind"] == kind]
        if len(duplicates) > 1:
            for entry in duplicates:
                entry["errors"].append({"message": "Only one CSV per input type is allowed; "
                                         "combine the " + kind + " files before uploading"})
    return prepared


def _plan(case, settings, prepared):
    # Project only valid attribution changes. Color validation must see the names
    # that would actually be saved, including the file's keep/replace policy.
    projected = copy.deepcopy(settings)
    plans = [None] * len(prepared)
    order = sorted(range(len(prepared)), key=lambda index: prepared[index]["kind"] != "attributions")
    for index in order:
        entry = prepared[index]
        plan = {"valid": False, "approval_sha256": None, "format": "csv", "counts": _counts(),
                "input_rows": 0, "duplicate_rows": 0, "changes": [], "errors": list(entry["errors"])}
        if not plan["errors"]:
            try:
                importer = _IMPORTERS[entry["kind"]]
                args = ((case, projected) if entry["kind"] == "change-outputs" else (projected,))
                plan = importer._plan(*args, entry["parsed"], entry["policy"])
            except TraceError as error:
                plan["errors"].append({"message": str(error)})
        if entry["kind"] == "attributions" and plan["valid"]:
            for change in plan["changes"]:
                if change["action"] in ("add", "replace"):
                    rule = change["rule"]
                    projected["rules"][rule["address"]] = copy.deepcopy(rule)
        if entry["kind"] == "name-colors" and "notice" in plan:
            plan["notice"] = plan["notice"].replace("Import attribution names first. ",
                "Attribution names in this upload are available before colors are checked. ")
        plans[index] = {**plan, "name": entry["name"], "kind": entry["kind"],
                        "policy": entry["policy"], "file_index": index}
    errors = [{"file": plan["name"], "file_index": index, **error}
              for index, plan in enumerate(plans) for error in plan["errors"]]
    counts = _counts()
    for plan in plans:
        for action, count in plan["counts"].items():
            counts[action] += count
    valid = not errors
    approval = digest(canonical({"kind": "input_import", "settings_sha256": digest(canonical(settings)),
        "files": [{key: entry[key] for key in ("name", "requested_kind", "policy", "source_sha256")}
                  for entry in prepared],
        # Existing planners bind their evidence snapshots as well as their rows.
        "plans": [plan["approval_sha256"] for plan in plans]})) if valid else None
    return {"schema_version": 1, "case_id": settings["case_id"], "base_revision": settings["revision"],
            "valid": valid, "approval_sha256": approval, "files": plans, "errors": errors,
            "counts": counts, "notice": NOTICE}


def preview_import(case, files):
    """Plan all uploads against one settings snapshot without writing anything."""
    prepared = _prepare(files)
    return _plan(case, load_services(case), prepared)


def _save_attributions(settings, plan, parsed, batch_id, stamp):
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
    return changed


def _save_mapping(settings, plan, parsed, batch_id, stamp):
    colors = plan["kind"] == "name-colors"
    field = "name_colors" if colors else "change_outputs"
    before = settings.get(field, {})
    after, changed = copy.deepcopy(before), 0
    for entry in plan["changes"]:
        if entry["action"] not in ("add", "replace", "clear"):
            continue
        key, value = (entry["key"], entry["color"]) if colors else (entry["txid"], entry["vout"])
        if value is None:
            after.pop(key, None)
        else:
            after[key] = value if colors else {"vout": value, "notes": entry["notes"], "updated_at": stamp}
        changed += 1
    if changed:
        settings[field] = after
        settings["revision"] += 1
        settings["history"].append({"revision": settings["revision"], "changed_at": stamp,
            "type": field, "previous": copy.deepcopy(before), field: copy.deepcopy(after),
            "import_id": batch_id, "import_sha256": parsed["source_sha256"],
            "import_format": parsed["format"], "import_policy": plan["policy"]})
    return changed


def apply_import(case, files, *, approval_sha256):
    """Revalidate the whole review under both locks, then save all files or none."""
    if not isinstance(approval_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", approval_sha256):
        raise TraceError("Preview the CSV files and approve their exact approval_sha256 before saving")
    prepared = _prepare(files)
    case = Path(case)
    with (case / "trace.lock").open("a") as trace_lock:
        try:
            fcntl.flock(trace_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("A trace, lookup, or reviewed layout is active; import CSV files after it finishes") from None
        with (case / "case.lock").open("a") as case_lock:
            fcntl.flock(case_lock, fcntl.LOCK_EX)
            settings = load_services(case)
            plan = _plan(case, settings, prepared)
            if not plan["valid"] or plan["approval_sha256"] != approval_sha256:
                raise TraceError("The files, import options, settings, or saved evidence changed; "
                                 "preview and approve the CSV files again")
            batch_id, stamp = uuid.uuid4().hex, now()
            results = [None] * len(prepared)
            order = sorted(range(len(prepared)), key=lambda index: plan["files"][index]["kind"] != "attributions")
            for index in order:
                item = plan["files"][index]
                save = _save_attributions if item["kind"] == "attributions" else _save_mapping
                changed = save(settings, item, prepared[index]["parsed"], batch_id, stamp)
                results[index] = {"name": item["name"], "kind": item["kind"], "counts": item["counts"],
                                  "changed": changed, "duplicate_rows": item["duplicate_rows"]}
            changed = sum(result["changed"] for result in results)
            if changed:
                save_json(case / "services.json", settings)
            return {"changed": changed, "revision": settings["revision"], "files": results,
                    "counts": plan["counts"], "notice": NOTICE, "import_id": batch_id if changed else None}
