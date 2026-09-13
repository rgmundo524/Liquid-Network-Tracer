"""Portable activity grouping and canvas bounds for exportable Miro frames.

Activity means connectivity in the displayed graph, not common ownership.
Frame rectangles are visually nested; they do not reparent board items.
"""

import hashlib
import math

from .common import TraceError


OUTER_KEY = "frame:graph"
ACTIVITY_PREFIX = "frame:activity:"
FRAME_PADDING = 60.0
FRAME_TITLE_SPACE = 90.0


def _note_key(key):
    return key == "legend" or key.startswith("run:")


def _key(value):
    if not isinstance(value, str) or not value:
        raise TraceError("Malformed Miro activity frame metadata; regenerate the export")
    return value


def _starting_keys(graph, nodes):
    seeds = {"tx:" + seed.rpartition(":")[0]
             for seed in graph.get("run", {}).get("seeds", []) or []
             if isinstance(seed, str) and ":" in seed}
    return {key for key, node in nodes.items() if node.get("kind") == "transaction"
            and (node.get("role") == "starting_transaction" or key in seeds)}


def _confirmed_time(node):
    """Use recorded confirmation time only; undated starts sort after dated ones."""
    details = node.get("details") or {}
    transaction = details.get("transaction") or {}
    status = transaction.get("status") or {}
    stamp = status.get("block_time")
    # UTC seconds through year 9999. Do not accept booleans, missing dates,
    # unconfirmed timestamps, or dates the preview cannot display.
    return (stamp if status.get("confirmed") is True and type(stamp) is int
            and 0 <= stamp <= 253402300799 else None)


def _starting_catalog(nodes, starts):
    times = {key: _confirmed_time(nodes[key]) for key in starts}
    ordered = sorted(starts, key=lambda key: (times[key] is None, times[key] or 0, key))
    return [{"key": key, "index": index, "block_time": times[key]}
            for index, key in enumerate(ordered, 1)]


def activity_frames(graph, *, indexed=True):
    """Describe weakly connected components using node identity, never labels.

    The iterative topology walk is O(V + E), including cycles and isolated
    nodes. Sorting the resulting descriptors makes them independent of input
    order. Ordinary continuation retains each frame's starting-transaction
    anchor; merging components retains the smallest such anchor. Display
    numbers are global, oldest confirmation first, with full keys breaking
    timestamp ties. Frame identities never depend on those display numbers.
    ``indexed=False`` is only for validation of historical schema-1 exports.
    """
    nodes = {}
    for node in graph["nodes"]:
        key = _key(node["id"])
        if key in nodes or _note_key(key) or key.startswith("frame:"):
            raise TraceError("Miro graph node keys conflict with activity frames or notes")
        nodes[key] = node
    starts = _starting_keys(graph, nodes)
    catalog = _starting_catalog(nodes, starts) if indexed else []
    start_numbers = {entry["key"]: entry["index"] for entry in catalog}
    neighbors = {key: [] for key in nodes}
    edge_keys = set()
    for edge in graph["edges"]:
        key = _key(edge["id"])
        source, target = _key(edge["source"]), _key(edge["target"])
        if (key in edge_keys or key in nodes or _note_key(key) or key.startswith("frame:")
                or source not in nodes or target not in nodes):
            raise TraceError("Invalid Miro activity frame graph connections")
        edge_keys.add(key)
        neighbors[source].append(target)
        neighbors[target].append(source)

    components, component_for = [], {}
    for first in nodes:
        if first in component_for:
            continue
        index = len(components)
        members, component_starts, stack = [], [], [first]
        component_for[first] = index
        while stack:
            key = stack.pop()
            members.append(key)
            if key in starts:
                component_starts.append(key)
            for neighbor in neighbors[key]:
                if neighbor not in component_for:
                    component_for[neighbor] = index
                    stack.append(neighbor)
        anchor = min(component_starts or members)
        components.append({"anchor": anchor, "shape_keys": sorted(members),
                           "starting_transaction_keys": sorted(component_starts),
                           "connector_keys": []})
    for edge in graph["edges"]:
        components[component_for[edge["source"]]]["connector_keys"].append(edge["id"])

    activities = []
    for number, component in enumerate(sorted(components, key=lambda group: group["anchor"]), 1):
        count = len(component["starting_transaction_keys"])
        if indexed:
            numbers = sorted(start_numbers[key] for key in component["starting_transaction_keys"])
            suffix = (" · Starting transaction" + ("s" if count != 1 else "") + " "
                      + ", ".join(map(str, numbers))) if numbers else " · No starting transactions"
        else:
            # Schema 1 remains reproducible for immutable historical plans.
            suffix = f" · {count} starting transaction" + ("s" if count != 1 else "")
        activities.append({
            "key": ACTIVITY_PREFIX + hashlib.sha256(component["anchor"].encode("utf-8")).hexdigest(),
            "title": f"Activity {number}" + suffix,
            "shape_keys": component["shape_keys"],
            "connector_keys": sorted(component["connector_keys"]),
            "starting_transaction_keys": component["starting_transaction_keys"],
        })
    result = {"schema_version": 2 if indexed else 1,
              "outer": {"key": OUTER_KEY, "title": "Liquid UTXO trace · Complete graph"},
              "activities": activities}
    if indexed:
        result["starting_transactions"] = catalog
    return result


def validate_activity_frames(metadata, shape_keys, connector_items, *, starting_transaction_keys=None):
    """Require an exact graph partition and generated frame identifiers.

    Plan annotations are deliberately outside activity groups. When available,
    the caller supplies starting transaction keys derived from the saved run.
    Older callers can use the explicit, member-constrained keys in metadata.
    """
    try:
        if (not isinstance(metadata, dict) or type(metadata.get("schema_version")) is not int
                or metadata["schema_version"] not in (1, 2)):
            raise ValueError
        keys = list(shape_keys)
        if any(not isinstance(key, str) or not key for key in keys) or len(keys) != len(set(keys)):
            raise ValueError
        if any(key.startswith("frame:") for key in keys):
            raise ValueError
        members = {key for key in keys if not _note_key(key)}
        if starting_transaction_keys is None:
            starts = {key for group in metadata["activities"] for key in group["starting_transaction_keys"]}
        else:
            if isinstance(starting_transaction_keys, str):
                raise ValueError
            starts = set(starting_transaction_keys)
        if any(not isinstance(key, str) or not key.startswith("tx:") or key not in members for key in starts):
            raise ValueError
        graph = {
            "nodes": [{"id": key, "kind": "transaction" if key.startswith("tx:") else "address",
                       "role": "starting_transaction" if key in starts else "address"} for key in members],
            "edges": [{"id": item["key"], "source": item["source"], "target": item["target"]}
                      for item in connector_items],
        }
        if metadata["schema_version"] == 2:
            # Plans retain the timestamp-to-index mapping because shape bodies
            # do not contain raw transaction evidence. Validate a complete,
            # canonical catalog, not arbitrary labels or per-frame numbering.
            catalog = metadata["starting_transactions"]
            if not isinstance(catalog, list) or len(catalog) != len(starts):
                raise ValueError
            lookup = {node["id"]: node for node in graph["nodes"]}
            seen = set()
            for index, entry in enumerate(catalog, 1):
                if (not isinstance(entry, dict) or set(entry) != {"key", "index", "block_time"}
                        or not isinstance(entry["key"], str) or entry["key"] not in starts
                        or entry["key"] in seen or type(entry["index"]) is not int
                        or entry["index"] != index):
                    raise ValueError
                stamp = entry["block_time"]
                if stamp is not None and (type(stamp) is not int or not 0 <= stamp <= 253402300799):
                    raise ValueError
                seen.add(entry["key"])
                lookup[entry["key"]]["details"] = {"transaction": {
                    "status": {"confirmed": stamp is not None, "block_time": stamp}}}
        if metadata != activity_frames(graph, indexed=metadata["schema_version"] == 2):
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        raise TraceError("Invalid Miro activity frame partition; regenerate the export") from None


def _rectangle(bounds):
    try:
        if len(bounds) != 4 or any(isinstance(value, bool) for value in bounds):
            raise ValueError
        x, y, width, height = map(float, bounds)
        if not all(math.isfinite(value) for value in (x, y, width, height)) or width <= 0 or height <= 0:
            raise ValueError
        result = (x - width / 2, y - height / 2, x + width / 2, y + height / 2)
        if not all(math.isfinite(value) for value in result):
            raise ValueError
        return result
    except (TypeError, ValueError, OverflowError):
        raise TraceError("Invalid shape bounds for Miro activity frames") from None


def _union(rectangles):
    bounds = None
    for left, top, right, bottom in rectangles:
        if bounds is None:
            bounds = (left, top, right, bottom)
        else:
            bounds = (min(bounds[0], left), min(bounds[1], top),
                      max(bounds[2], right), max(bounds[3], bottom))
    return bounds


def _padded(bounds):
    left, top, right, bottom = bounds
    return (left - FRAME_PADDING, top - FRAME_PADDING - FRAME_TITLE_SPACE,
            right + FRAME_PADDING, bottom + FRAME_PADDING)


def _frame(descriptor, rectangle):
    left, top, right, bottom = rectangle
    width, height = right - left, bottom - top
    # Halving before adding avoids intermediate overflow for large coordinates.
    x, y = left / 2 + right / 2, top / 2 + bottom / 2
    _rectangle((x, y, width, height))
    return {"key": descriptor["key"], "body": {
        "data": {"title": descriptor["title"], "type": "freeform", "format": "custom"},
        "position": {"x": x, "y": y},
        "geometry": {"width": width, "height": height},
        "style": {"fillColor": "#ffffffff"},
    }}


def frame_bodies(metadata, shape_bounds):
    """Fit frames to current canvas centers and axis-aligned shape dimensions.

    ``shape_bounds`` maps logical shape keys to ``(x, y, width, height)``.
    The caller resolves rotation and frame-relative positions first. Include
    saved run notes in this mapping so the complete frame encloses them too.
    This function emits canvas-positioned rectangles, never parent IDs.
    """
    rectangles = {key: _rectangle(bounds) for key, bounds in shape_bounds.items()}
    activities, group_bounds = [], []
    try:
        for descriptor in metadata["activities"]:
            bounds = _union(rectangles[key] for key in descriptor["shape_keys"])
            if bounds is None:
                raise ValueError
            bounds = _padded(bounds)
            group_bounds.append(bounds)
            activities.append(_frame(descriptor, bounds))
        bounds = _union(rectangles.values())
        for group in group_bounds:
            bounds = _union((bounds, group)) if bounds is not None else group
        if bounds is None:
            # An empty graph still has a valid export frame; normal plans also
            # provide a legend, so this only serves direct empty callers.
            bounds = (-200.0, -150.0, 200.0, 150.0)
        return [_frame(metadata["outer"], _padded(bounds)), *activities]
    except (KeyError, TypeError, ValueError, OverflowError):
        raise TraceError("Incomplete Miro activity frame bounds; regenerate the export") from None
