"""Case-local graph-role fills, separate from imported attribution-name colors."""

import copy
import fcntl
from pathlib import Path

from .common import TraceError, now, save_json

ROLE_LABELS = {
    "seed": "Seed addresses",
    "starting_transaction": "Seed / starting transactions",
    "transaction": "Child / downstream transactions",
    "address": "Context addresses",
    "candidate": "Child / reachable addresses",
    "unspent_endpoint": "Unspent",
    "event": "Events / fees / unspendable outputs",
}
NOTICE = (
    "Graph-role colors are display settings for this investigation. Selected seeds use "
    "the seed-address color even when named; assigned name colors still override other "
    "address fills. Borders and tracing rules are unchanged. Clear an assignment to "
    "restore that role's default. Regenerate previews or sync Miro; no retracing is needed."
)


def validate_role_colors(colors):
    """Reject unknown roles and unsafe stored colors, including manual file edits."""
    from .name_colors import color_value
    if not isinstance(colors, dict) or set(colors) - ROLE_LABELS.keys():
        raise TraceError("Invalid saved graph-role colors; restore services.json")
    for value in colors.values():
        if color_value(value) != value:
            raise TraceError("Invalid saved graph-role color; restore services.json")
    return colors


def role_color_rows(settings):
    # Import lazily: export's palette remains the authoritative default palette.
    from .export import COLORS
    colors = validate_role_colors(settings.get("role_colors", {}))
    return [{"role": role, "name": label, "color": colors.get(role),
             "default_color": COLORS[role]} for role, label in ROLE_LABELS.items()]


def set_role_colors(case, updates, *, expected_revision):
    """Atomically edit only the role palette with the existing locks and revision."""
    from .name_colors import color_value
    from .services import load_services
    if type(expected_revision) is not int or expected_revision < 0:
        raise TraceError("Refresh the color menu before saving")
    if not isinstance(updates, list) or not 1 <= len(updates) <= len(ROLE_LABELS):
        raise TraceError("Save between 1 and 7 graph-role assignments at a time")
    normalized = {}
    for update in updates:
        if not isinstance(update, dict) or set(update) != {"role", "color"}:
            raise TraceError("Each graph-role assignment requires a role and color only; save names separately")
        role, value = update["role"], update["color"]
        if not isinstance(role, str) or role not in ROLE_LABELS:
            raise TraceError("Choose a listed graph role")
        value = None if value is None or isinstance(value, str) and not value.strip() else color_value(value)
        if role in normalized and normalized[role] != value:
            raise TraceError("Conflicting colors for the same graph role")
        normalized[role] = value
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
                raise TraceError("Assessments or colors changed; refresh the color menu before saving")
            before = settings.get("role_colors", {})
            after = dict(before)
            for role, value in normalized.items():
                if value is None:
                    after.pop(role, None)
                else:
                    after[role] = value
            changed = sum(before.get(role) != after.get(role) for role in normalized)
            if changed:
                settings["role_colors"] = after
                settings["revision"] += 1
                settings["history"].append({"revision": settings["revision"], "changed_at": now(),
                    "type": "role_colors", "previous": copy.deepcopy(before), "role_colors": dict(after)})
                save_json(case / "services.json", settings)
            return {"changed": changed, "revision": settings["revision"], "notice": NOTICE}


def apply_role_colors(nodes, colors):
    """Apply fills to fresh graph nodes before name overrides; never alter roles."""
    validate_role_colors(colors)
    for node in nodes:
        role = node.get("role", node["kind"])
        if role in colors:
            node["color"] = colors[role]
            node["color_source"] = "role_palette"
