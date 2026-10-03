"""Bounded, actionable diagnostics for protected edits on retiring Miro items.

These reports describe the existing preflight comparison. They never authorize
an overwrite, include unrelated board content, or change comparison semantics.
"""

import json
import re
from urllib.parse import quote, urlencode

from .common import TraceError

MAX_ITEMS = 20
MAX_CHANGES = 20
MAX_TOTAL_CHANGES = 80
MAX_VALUE_CHARS = 1000
MAX_TOTAL_VALUE_CHARS = 12000
MAX_REPORT_BYTES = 96 * 1024
_ID = re.compile(r"[A-Za-z0-9_=-]{1,200}\Z")
_FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\[[0-9]{1,6}\])?(?:\.[A-Za-z][A-Za-z0-9_]*(?:\[[0-9]{1,6}\])?)*\Z")
_KINDS = {"shapes": "shape", "connectors": "connector", "frames": "frame"}
_TYPES = {"string", "number", "boolean", "null", "json"}


def object_url(board_id, item_id):
    if not isinstance(board_id, str) or not _ID.fullmatch(board_id):
        return None
    if not isinstance(item_id, str) or not _ID.fullmatch(item_id):
        return None
    return "https://miro.com/app/board/" + quote(board_id, safe="") + "/?" + urlencode({"moveToWidget": item_id})


def _value(value, missing):
    if value is missing:
        return {"present": False}
    if isinstance(value, str):
        text, kind = value, "string"
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        kind = "null" if value is None else "boolean" if isinstance(value, bool) else "number" if isinstance(value, (int, float)) else "json"
    return {"present": True, "value": text[:MAX_VALUE_CHARS], "type": kind,
            "truncated": len(text) > MAX_VALUE_CHARS}


def _pair(saved, current, missing):
    cells = [_value(saved, missing), _value(current, missing)]
    if isinstance(saved, str) and isinstance(current, str):
        prefix = 0
        for before, after in zip(saved, current):
            if before != after:
                break
            prefix += 1
        if prefix > MAX_VALUE_CHARS // 2:
            start = max(0, prefix - 150)
            label = "[Excerpt starting at character " + str(start + 1) + "]\n"
            for cell, original in zip(cells, (saved, current)):
                cell.update(value=label + original[start:start + MAX_VALUE_CHARS - len(label)], truncated=True)
    return cells


def _differences(saved, current, path, missing):
    """Expand a changed managed field without ignoring any changed values."""
    if saved == current:
        return
    if isinstance(saved, dict) and isinstance(current, dict):
        for key in sorted(set(saved) | set(current)):
            yield from _differences(saved.get(key, missing), current.get(key, missing), path + "." + str(key), missing)
    elif isinstance(saved, list) and isinstance(current, list):
        for index in range(max(len(saved), len(current))):
            yield from _differences(saved[index] if index < len(saved) else missing,
                                    current[index] if index < len(current) else missing,
                                    path + "[" + str(index) + "]", missing)
    else:
        before, after = _pair(saved, current, missing)
        yield {"field": path, "saved": before, "current": after}


def editable_report(state, remote, removals):
    """Identify all changed editable fields using the existing sync predicate."""
    from .miro import _MISSING, _comparison_editable, _fields, _get, _same

    items, truncated = [], False
    for key in removals:
        record, body = state["items"][key], remote.get(key)
        if body is None:
            continue
        actual = _comparison_editable(body, record["endpoint"], record["managed"])
        changes = []
        item_truncated = False
        changed = False
        for path, saved in _fields(record["managed"]):
            current = _get(actual, path)
            if _same(current, saved, path):
                continue
            changed = True
            for change in _differences(saved, current, ".".join(path), _MISSING):
                if len(changes) == MAX_CHANGES:
                    item_truncated = True
                    break
                changes.append(change)
        if not changed:
            continue
        if not changes:
            raise TraceError("A retiring Miro object has manual edits; diagnostic details are unavailable. No board writes made.")
        if len(items) == MAX_ITEMS:
            truncated = True
            break
        items.append({"key": key, "item_id": record["id"], "kind": _KINDS[record["endpoint"]],
                      "changes": changes, "truncated": item_truncated})
    if not items:
        return None
    report = public_report({"kind": "miro_edit_conflicts", "board_id": state["board_id"],
                            "items": items, "truncated": truncated})
    if report is None:
        raise TraceError("A retiring Miro object has manual edits; diagnostic details are unavailable. No board writes made.")
    return report


def public_report(value):
    """Strict allowlist for worker/UI boundaries; never trust supplied links."""
    if not isinstance(value, dict) or value.get("kind") != "miro_edit_conflicts":
        return None
    board = value.get("board_id")
    if not isinstance(board, str) or not _ID.fullmatch(board) or not isinstance(value.get("items"), list):
        return None
    result = {"kind": "miro_edit_conflicts", "board_id": board, "items": [],
              "truncated": value.get("truncated") is True}
    budget, count = MAX_TOTAL_VALUE_CHARS, 0
    for item in value["items"]:
        if len(result["items"]) >= MAX_ITEMS or count >= MAX_TOTAL_CHANGES:
            result["truncated"] = True
            break
        if not isinstance(item, dict):
            return None
        key, item_id, kind = item.get("key"), item.get("item_id"), item.get("kind")
        if (not isinstance(key, str) or not key or len(key) > 256
                or any(ord(char) < 32 for char in key)
                or kind not in _KINDS.values() or object_url(board, item_id) is None
                or not isinstance(item.get("changes"), list)):
            return None
        clean = {"key": key, "item_id": item_id, "kind": kind,
                 "object_url": object_url(board, item_id), "changes": [],
                 "truncated": item.get("truncated") is True}
        for change in item["changes"]:
            if len(clean["changes"]) >= MAX_CHANGES or count >= MAX_TOTAL_CHANGES:
                clean["truncated"] = result["truncated"] = True
                break
            if not isinstance(change, dict):
                return None
            field = change.get("field")
            if not isinstance(field, str) or len(field) > 128 or not _FIELD.fullmatch(field):
                return None
            entry = {"field": field}
            for side in ("saved", "current"):
                cell = change.get(side)
                if not isinstance(cell, dict) or type(cell.get("present")) is not bool:
                    return None
                if not cell["present"]:
                    entry[side] = {"present": False}
                    continue
                text, value_type = cell.get("value"), cell.get("type")
                if not isinstance(text, str) or value_type not in _TYPES:
                    return None
                limit = min(MAX_VALUE_CHARS, budget)
                clipped = text[:limit]
                limited = cell.get("truncated") is True or len(clipped) < len(text)
                entry[side] = {"present": True, "value": clipped, "type": value_type, "truncated": limited}
                budget -= len(clipped)
                if limited:
                    result["truncated"] = True
            clean["changes"].append(entry)
            count += 1
        if clean["changes"]:
            result["items"].append(clean)
            result["truncated"] |= clean["truncated"]
    if not result["items"]:
        return None
    # Bound JSON even for Unicode-heavy identifiers or repeated metadata.
    while len(json.dumps(result, ensure_ascii=True).encode("utf-8")) > MAX_REPORT_BYTES:
        result["truncated"] = True
        last = result["items"][-1]
        last["changes"].pop()
        last["truncated"] = True
        if not last["changes"]:
            result["items"].pop()
    return result


class MiroEditConflict(TraceError):
    def __init__(self, message, report):
        super().__init__(message)
        self.report = public_report(report)


def from_error(error):
    """Only known typed diagnostics may cross the CLI boundary."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, MiroEditConflict):
            return public_report(error.report)
        error = error.__cause__
    return None


def print_report(report, *, file):
    """Use JSON quoting to keep remote text inert in a terminal."""
    report = public_report(report)
    if report is None:
        return
    print("Miro objects with manual edits:", file=file)
    for item in report["items"]:
        print("  " + item["kind"] + " " + json.dumps(item["key"], ensure_ascii=True)
              + " (Miro ID " + item["item_id"] + ")", file=file)
        print("  Open object: " + item["object_url"], file=file)
        for change in item["changes"]:
            print("    " + change["field"], file=file)
            for side, label in (("saved", "Last synced"), ("current", "Current Miro")):
                cell = change[side]
                if not cell["present"]:
                    text = "[missing]"
                else:
                    text = json.dumps(cell["value"], ensure_ascii=True)
                    if cell["type"] != "string":
                        text += " (" + cell["type"] + ")"
                if cell.get("truncated"):
                    text += " [truncated]"
                print("      " + label + ": " + text, file=file)
    if report["truncated"]:
        print("  More differences exist; this report is limited to keep it readable.", file=file)
