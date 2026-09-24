"""Scoped reconciliation for a registered, maintained investigation plot.

Only items created under the same board projection carry retirement proofs.
The ordinary sync journal handles acknowledgements and interrupted deletions.
"""

import copy
import math
import re

from .common import TraceError


def validate(plan):
    value = plan.get("board_projection")
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"version", "record_id", "goal"}
            or value.get("version") != 1 or value.get("goal") not in ("full", "connections", "pegouts")
            or not isinstance(value.get("record_id"), str)
            or not re.fullmatch(r"board-[0-9a-f]{32}", value["record_id"])
            or not plan.get("namespace", {}).get("case_id", "").endswith(":" + value["record_id"])):
        raise TraceError("Invalid registered board projection; regenerate its plot")
    return value


def creation_proof(plan, endpoint, body):
    projection = validate(plan)
    if projection is None or endpoint not in ("shapes", "connectors"):
        return None
    proof = {"projection": copy.deepcopy(projection), "endpoint": endpoint}
    if endpoint == "shapes":
        proof.update(shape=body["data"]["shape"], geometry=copy.deepcopy(body["geometry"]),
                     position={axis: body["position"][axis] for axis in ("x", "y")})
    else:
        proof["routing"] = {field: copy.deepcopy(body.get(field)) for field in ("shape", "startItem", "endItem")}
    return proof


def acknowledge(record, body, patch=None):
    """Refresh only acknowledged generated routing or geometry, never live edits."""
    from .miro import _same
    proof = record.get("projection_proof")
    if not proof:
        return
    if record["endpoint"] == "shapes":
        if "position" in body and (patch is None or "position" in patch):
            axes = ("x", "y")
            if all(axis in body["position"] for axis in axes) and (patch is None or all(
                    _same(body["position"].get(axis), patch["position"].get(axis), ("position", axis)) for axis in axes)):
                proof["position"] = {axis: body["position"][axis] for axis in axes}
        if "geometry" in body and (patch is None or "geometry" in patch):
            axes = ("width", "height")
            if all(axis in body["geometry"] for axis in axes) and (patch is None or all(
                    _same(body["geometry"].get(axis), patch["geometry"].get(axis), ("geometry", axis)) for axis in axes)):
                proof["geometry"] = {axis: body["geometry"][axis] for axis in axes}
    else:
        for field in ("shape", "startItem", "endItem"):
            if field not in body or (patch is not None and field not in patch):
                continue
            actual = body[field]
            expected = None if patch is None else patch[field]
            if patch is None or _same(actual, expected, (field,)):
                proof.setdefault("routing", {})[field] = copy.deepcopy(actual)


def removals(plan, state):
    projection = validate(plan)
    if projection is None:
        return {}
    desired = {item["key"] for name in ("shapes", "connectors") for item in plan[name]}
    desired_edges = {item["key"]: item for item in plan["connectors"]}
    # Group/ungroup transitions reuse logical connector keys with different
    # proven context endpoints. Let the existing context replacement machinery
    # retire those shapes and connectors together, using its membership proof.
    replacements = set()
    for key, record in state["items"].items():
        edge = desired_edges.get(key)
        if record["endpoint"] != "connectors" or edge is None:
            continue
        for field in ("source", "target"):
            previous = record.get(field)
            if previous not in desired and previous != edge.get(field):
                replacements.add(previous)
    result = {}
    for key, record in state["items"].items():
        if record["endpoint"] == "frames" or key in desired or key in replacements:
            continue
        proof = record.get("projection_proof")
        if (not isinstance(proof, dict) or proof.get("projection") != projection
                or proof.get("endpoint") != record["endpoint"]):
            raise TraceError("An obsolete plot item has no matching creation proof; preserve its mapping before syncing")
        result[key] = {"kind": "board_projection", "version": 1, "key": key, **copy.deepcopy(proof)}
    for key, record in state["items"].items():
        if (record["endpoint"] == "connectors" and key not in result
                and any(record.get(field) in result for field in ("source", "target"))):
            raise TraceError("A retained connector still uses a retiring plot object; regenerate the plot")
    return result


def check_remote(state, remote, removals, inventory):
    from .miro import _editable, _fields, _get, _same

    retiring_shapes = {state["items"][key]["id"] for key in removals
                       if state["items"][key]["endpoint"] == "shapes"}
    retiring_connectors = {state["items"][key]["id"] for key in removals
                           if state["items"][key]["endpoint"] == "connectors"}
    for item in inventory.values():
        if (any((item.get(field) or {}).get("id") in retiring_shapes for field in ("startItem", "endItem"))
                and item["id"] not in retiring_connectors):
            raise TraceError("A board connector attaches to an obsolete plot object; preserve that attachment before syncing. No board writes made.")
    for key, proof in removals.items():
        record, body = state["items"][key], remote.get(key)
        if body is None:  # Only an attempted, journaled DELETE permits absence.
            continue
        actual = _editable(body, record["endpoint"])
        if any(not _same(_get(actual, path), value, path) for path, value in _fields(record["managed"])):
            raise TraceError("An obsolete plot object has manual text/style edits; preserve them before syncing. No board writes made.")
        if record["endpoint"] == "connectors":
            if record["id"] not in inventory:
                raise TraceError("Connector inventory is incomplete; no board writes made")
            for field, logical in (("startItem", "source"), ("endItem", "target")):
                if (body.get(field) or {}).get("id") != state["items"].get(record[logical], {}).get("id"):
                    raise TraceError("An obsolete plot connector was manually reattached; restore it before syncing")
            for field, expected in proof.get("routing", {}).items():
                if not _same(body.get(field), expected, (field,)):
                    raise TraceError("An obsolete plot connector has edited routing; preserve it before syncing")
            continue
        if body.get("data", {}).get("shape") != proof.get("shape") or (body.get("parent") or {}).get("id"):
            raise TraceError("An obsolete plot object has an edited shape or frame/group parent; preserve it before syncing")
        try:
            if any(not math.isclose(float(body["position"][axis]), float(proof["position"][axis]), abs_tol=.01)
                   for axis in ("x", "y")):
                raise TraceError("An obsolete plot object was manually moved; preserve its position before syncing")
            geometry = body["geometry"]
            values = [float(geometry[axis]) for axis in ("width", "height")]
            if (any(not math.isfinite(value) for value in values)
                    or any(not math.isclose(float(geometry[axis]), float(proof["geometry"][axis]), abs_tol=.01)
                           for axis in ("width", "height"))
                    or float(geometry.get("rotation", body.get("rotation", 0))) % 360):
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            raise TraceError("An obsolete plot object was resized or rotated; preserve its changes before syncing") from None
