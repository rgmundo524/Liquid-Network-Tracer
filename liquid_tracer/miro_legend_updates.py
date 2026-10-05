"""Grow generated legends without replacing an analyst's board edits.

The caller applies these bounds to its placement view before placing new items,
then journals the same geometry and position alongside the content PATCH.
Geometry baselines are recorded only after a creation or resize is acknowledged.
"""

import math


_LEGACY_GEOMETRY = {"width": 1300., "height": 260.}
_GAP = 60.


def geometry_snapshot(body):
    """Return intrinsic dimensions, or None for an unreadable geometry."""
    try:
        geometry = body["geometry"]
        if any(isinstance(geometry[axis], bool) for axis in ("width", "height")):
            return None
        result = {axis: float(geometry[axis]) for axis in ("width", "height")}
        if any(not math.isfinite(value) or value <= 0 for value in result.values()):
            return None
        return result
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def remember_geometry(record, body):
    """Remember acknowledged generated dimensions, never a preserved manual edit."""
    geometry = geometry_snapshot(body)
    if geometry is not None:
        record["legend_geometry"] = geometry


def _eligible(record, actual, planned):
    # Lazy import keeps this helper independent of the sync module's imports.
    from .miro import _same

    content = actual.get("data", {}).get("content")
    managed = record.get("managed", {})
    if not isinstance(content, str) or not (
            _same(content, managed.get("data", {}).get("content"), ("data", "content"))
            or _same(content, planned.get("data", {}).get("content"), ("data", "content"))):
        return False
    font = actual.get("style", {}).get("fontSize")
    if not _same(font, managed.get("style", {}).get("fontSize"), ("style", "fontSize")):
        return False
    try:
        if float(actual.get("geometry", {}).get("rotation", 0)) != 0:
            return False
    except (TypeError, ValueError, OverflowError):
        return False
    current = geometry_snapshot(actual)
    baseline = (geometry_snapshot({"geometry": record["legend_geometry"]})
                if "legend_geometry" in record else _LEGACY_GEOMETRY)
    return current is not None and baseline is not None and all(
        math.isclose(current[axis], baseline[axis], rel_tol=0, abs_tol=.01)
        for axis in current)


def resize_updates(plan, state, remote, removed):
    """Plan upward growth of intact, existing generated legend rectangles.

    Keep each rectangle's left and bottom edges unless an upward movement is
    needed for clearance. Process lower pages first so upper pages can grow
    around them, while preserving all unchanged and manually edited objects.
    No input dictionaries are modified.
    """
    from .miro import _bounds, _overlap

    catalog = plan.get("presentation_items", {})
    candidates = {}
    for item in plan.get("shapes", []):
        key, body = item["key"], item["body"]
        if key != "legend" and catalog.get(key, {}).get("kind") != "legend":
            continue
        record = state.get("items", {}).get(key)
        if (key in removed or record is None or record.get("endpoint") != "shapes"
                or key not in remote or not _eligible(record, remote[key], body)):
            continue
        current, desired = geometry_snapshot(remote[key]), geometry_snapshot(body)
        if desired is None:
            continue
        grown = {axis: max(current[axis], desired[axis]) for axis in current}
        if grown == current:
            continue
        x, y, _, _ = _bounds(remote[key], key)
        candidates[key] = (
            x + (grown["width"] - current["width"]) / 2,
            y - (grown["height"] - current["height"]) / 2,
            grown["width"], grown["height"])
    if not candidates:
        return {}

    occupied = {
        key: _bounds(remote[key], key)
        for key, record in state.get("items", {}).items()
        if record.get("endpoint") == "shapes" and key in remote
        and key not in removed and key not in candidates
    }
    updates = {}
    for key in sorted(candidates, key=lambda name: (
            -(candidates[name][1] + candidates[name][3] / 2), name)):
        x, y, width, height = candidates[key]
        # Once above an obstacle's top edge, further upward movement cannot
        # hit it again. Descending top-edge order therefore needs one pass.
        for obstacle in sorted(occupied.values(), key=lambda box: box[1] - box[3] / 2,
                               reverse=True):
            if _overlap((x, y, width, height), obstacle, gap=_GAP):
                y = min(y, obstacle[1] - obstacle[3] / 2 - _GAP - height / 2)
        occupied[key] = (x, y, width, height)
        updates[key] = {"geometry": {"width": width, "height": height},
                        "position": {"x": x, "y": y, "origin": "center"}}
    return updates
