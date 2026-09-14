"""Case-local, case-insensitive attribution-name colors, independent of tracing."""

import copy
import fcntl
import re
from pathlib import Path

from .common import TraceError, now, save_json
from .services import _text, load_services

COLOR_PRESETS = (
    ("Magenta", "#f0abfc"), ("Violet", "#c4b5fd"), ("Blue", "#93c5fd"),
    ("Cyan", "#a5f3fc"), ("Teal", "#99f6e4"), ("Green", "#bbf7d0"),
    ("Gold", "#fde68a"), ("Orange", "#fdba74"), ("Pink", "#f9a8d4"),
    ("Gray", "#d1d5db"),
)
NOTICE = ("Colors are assigned to names, not confidence. Matching ignores capitalization; "
          "display spelling and address identity are unchanged. Selected seeds retain their configured seed color. "
          "Without an assigned color, the normal unspent/candidate/context color applies. "
          "Saving is local; regenerate previews or sync Miro to update the graph. No trace is started.")


def name_key(value):
    # A saved casefold key may be longer than its original 120-character name.
    return _text(value, "Attribution name", 360, required=True).casefold()


def color_value(value):
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value.strip()):
        raise TraceError("Choose a color in #RRGGBB format, or clear the assignment to use the default")
    return value.strip().lower()


def validate_name_colors(colors):
    """Validate stored mappings too; a color must never inject CSS, SVG or HTML."""
    if not isinstance(colors, dict):
        raise TraceError("Invalid saved name colors; restore services.json")
    for key, value in colors.items():
        # Unicode casefold can expand a 120-character display name.
        if (not isinstance(key, str) or not key or len(key) > 360
                or key != key.strip().casefold()
                or any(ord(char) < 32 or ord(char) == 127 for char in key)
                or color_value(value) != value):
            raise TraceError("Invalid saved name color; restore services.json")
    return colors


def _names(settings):
    groups = {}
    for rule in settings["rules"].values():
        name = rule["name"]
        if not name:
            continue
        key = name_key(name)
        row = groups.setdefault(key, {"key": key, "variants": set(), "addresses": 0, "enabled_addresses": 0})
        row["variants"].add(name)
        row["addresses"] += 1
        row["enabled_addresses"] += int(rule["enabled"])
    for key in settings.get("name_colors", {}):
        groups.setdefault(key, {"key": key, "variants": {key}, "addresses": 0, "enabled_addresses": 0})
    return groups


def name_color_catalog(case, *, query="", offset=0, limit=100):
    if (not isinstance(query, str) or len(query) > 256 or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 100):
        raise TraceError("Choose a short name search and a page of 1 to 100 names")
    settings = load_services(case)
    colors = settings.get("name_colors", {})
    query = query.strip().casefold()
    groups = _names(settings)
    ordered = sorted(key for key in groups if query in key)
    rows = []
    for key in ordered[offset:offset + limit]:
        row = groups[key]
        variants = sorted(row["variants"])
        rows.append({**row, "variants": variants, "name": variants[0], "color": colors.get(key)})
    from .role_colors import role_color_rows, NOTICE as ROLE_NOTICE
    return {"roles": role_color_rows(settings), "role_notice": ROLE_NOTICE,
            "revision": settings["revision"], "rows": rows, "total": len(ordered),
            "offset": offset, "limit": limit, "presets": list(COLOR_PRESETS), "notice": NOTICE}


def set_name_colors(case, updates, *, expected_revision):
    """Save a reviewed page/batch atomically without editing address assessments.

    The existing case and trace locks keep run and compact-preview snapshots
    consistent. Revision checking rejects an editor left open during an import.
    """
    if isinstance(updates, list) and any(isinstance(item, dict) and "role" in item for item in updates):
        from .role_colors import set_role_colors
        return set_role_colors(case, updates, expected_revision=expected_revision)
    if type(expected_revision) is not int or expected_revision < 0:
        raise TraceError("Refresh the name color menu before saving")
    if not isinstance(updates, list) or not 1 <= len(updates) <= 100:
        raise TraceError("Save between 1 and 100 name color assignments at a time")
    normalized = {}
    for update in updates:
        if not isinstance(update, dict) or set(update) != {"name", "color"}:
            raise TraceError("Each color assignment requires a name and color only")
        key = name_key(update["name"])
        value = update["color"]
        value = None if value is None or isinstance(value, str) and not value.strip() else color_value(value)
        if key in normalized and normalized[key] != value:
            raise TraceError("Conflicting colors for the same case-insensitive name")
        normalized[key] = value
    case = Path(case)
    with (case / "trace.lock").open("a") as trace_lock:
        try:
            fcntl.flock(trace_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("A trace, lookup, or reviewed layout is active; assign colors after it finishes") from None
        with (case / "case.lock").open("a") as case_lock:
            fcntl.flock(case_lock, fcntl.LOCK_EX)
            settings = load_services(case)
            if settings["revision"] != expected_revision:
                raise TraceError("Assessments or colors changed; refresh the name color menu before saving")
            if set(normalized) - _names(settings).keys():
                raise TraceError("Import or save the attribution name before assigning its color")
            before = settings.get("name_colors", {})
            after = dict(before)
            for key, value in normalized.items():
                if value is None:
                    after.pop(key, None)
                else:
                    after[key] = value
            changed = sum(before.get(key) != after.get(key) for key in normalized)
            if changed:
                settings["name_colors"] = after
                settings["revision"] += 1
                settings["history"].append({"revision": settings["revision"], "changed_at": now(),
                    "type": "name_colors", "previous": copy.deepcopy(before), "name_colors": dict(after)})
                save_json(case / "services.json", settings)
            return {"changed": changed, "revision": settings["revision"], "notice": NOTICE}


def apply_name_colors(nodes, colors, *, role_colors=None):
    """Resolve display colors once per shared node, never by confidence.

    Conflicting colors on independent assessments do not silently choose one
    attribution. The ordinary trace-role color is retained and the conflict is
    exposed in the register. Multiple names with the same color are compatible.
    """
    validate_name_colors(colors)
    from .role_colors import apply_role_colors
    nodes = list(nodes)
    apply_role_colors(nodes, {} if role_colors is None else role_colors)
    for node in nodes:
        if node["kind"] != "address" or node["details"].get("network") != "liquid":
            continue
        matches = {}
        for assessment in node["details"].get("address_attributions", []):
            name = assessment.get("entity") or assessment.get("name")
            if isinstance(name, str) and name.strip():
                # Legacy label imports may use longer names; matching is still
                # exact casefold, without applying newer input length limits.
                key = name.strip().casefold()
                if key in colors:
                    matches[key] = colors[key]
        node.setdefault("color_source", node.get("role", "address"))
        if not matches:
            continue
        node["details"]["name_colors"] = dict(sorted(matches.items()))
        conflict = len(set(matches.values())) > 1
        node["details"]["name_color_conflict"] = conflict
        if node.get("role") != "seed" and not conflict:
            node["color"] = next(iter(matches.values()))
            node["color_source"] = "name"


def color_text(color):
    """A readable foreground for arbitrary investigator-chosen backgrounds."""
    color = color_value(color)
    channels = [int(color[i:i+2], 16) / 255 for i in (1, 3, 5)]
    linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in channels]
    luminance = sum(v * w for v, w in zip(linear, (.2126, .7152, .0722)))
    return "#000000" if luminance > .179 else "#ffffff"
