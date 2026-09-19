"""Evidence-checked, reversible Miro publication of optional context summaries.

A toggle replaces only unchanged generated objects. Connector logical keys and
UTXO evidence survive; remote IDs may change. The ordinary sync deletion and
creation journals make interrupted replacements resumable without blind POSTs.
"""

import copy
import math

from .common import TraceError

PREFIX = "context-group:"
VERSION = 1


def evidence(edge):
    return {"source": edge.get("original_source", edge["source"]),
            "target": edge["target"], "outpoint": edge.get("outpoint"),
            "role": edge.get("role")}


def catalog(graph):
    """Keep a small membership proof, not copies of every investigative record."""
    edges = {edge["id"]: edge for edge in graph["edges"]}
    result = {}
    for node in graph["nodes"]:
        if node["kind"] != "context_group":
            continue
        details = node["details"]
        members = {}
        for member in details["members"]:
            info = member.get("details", {})
            if member["kind"] != "address" or info.get("network") != "liquid" or not info.get("address"):
                raise TraceError("Context summary contains an unproven address; regenerate the export")
            members[member["id"]] = {"address": info["address"],
                                      "width": member["width"], "height": member["height"]}
        result[node["id"]] = {
            "version": VERSION, "key": node["id"], "target": details["transaction_id"],
            "members": members,
            "inputs": {key: evidence(edges[key]) for key in details["input_edge_ids"]},
            "geometry": {axis: node[axis] for axis in ("width", "height")},
        }
    return result


def _valid(proof):
    try:
        if (not isinstance(proof, dict) or proof.get("version") != VERSION
                or not isinstance(proof["target"], str) or not proof["target"].startswith("tx:")
                or proof["key"] != PREFIX + proof["target"][3:]
                or not isinstance(proof["members"], dict) or not proof["members"]
                or not isinstance(proof["inputs"], dict) or not proof["inputs"]):
            raise ValueError
        if len({item["address"] for item in proof["members"].values()}) < 2:
            raise ValueError
        for key, member in proof["members"].items():
            if not isinstance(key, str) or not key or key.startswith((PREFIX, "tx:")):
                raise ValueError
            if not isinstance(member["address"], str) or not member["address"]:
                raise ValueError
        for dimensions in [proof["geometry"], *proof["members"].values()]:
            if any(isinstance(dimensions[axis], bool) or not math.isfinite(float(dimensions[axis]))
                   or float(dimensions[axis]) <= 0 for axis in ("width", "height")):
                raise ValueError
        for key, item in proof["inputs"].items():
            if (not key.startswith("in:" + proof["target"][3:] + ":")
                    or item["source"] not in proof["members"]
                    or item["target"] != proof["target"] or item["role"] != "context_input"
                    or not isinstance(item["outpoint"], str) or ":" not in item["outpoint"]):
                raise ValueError
        if {item["source"] for item in proof["inputs"].values()} != set(proof["members"]):
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        raise TraceError("Invalid context-summary membership proof; regenerate the export or restore the mapping") from None
    return proof


def validate(plan):
    groups = plan.get("context_group_items", {})
    if not isinstance(groups, dict):
        raise TraceError("Invalid context-summary catalog; regenerate the export")
    shapes = {item["key"]: item for item in plan["shapes"]}
    connectors = {item["key"]: item for item in plan["connectors"]}
    if {key for key in shapes if key.startswith(PREFIX)} != set(groups):
        raise TraceError("Context summaries require membership proofs; regenerate the export")
    members = set()
    for key, proof in groups.items():
        _valid(proof)
        if (proof["key"] != key or proof["target"] not in shapes
                or shapes[key]["body"]["data"]["shape"] != "rectangle"
                or shapes[key]["body"]["geometry"] != proof["geometry"]
                or members.intersection(proof["members"]) or set(shapes).intersection(proof["members"])):
            raise TraceError("Context-summary shape disagrees with its proof; regenerate the export")
        members.update(proof["members"])
        incident = {edge["key"] for edge in connectors.values()
                    if key in (edge["source"], edge["target"])}
        if incident != set(proof["inputs"]):
            raise TraceError("Context summary has unexpected connections; regenerate the export")
        for edge_key, expected in proof["inputs"].items():
            item = connectors[edge_key]
            if (item["source"] != key or item["target"] != proof["target"]
                    or item.get("context_evidence") != expected):
                raise TraceError("Context-summary input evidence changed; regenerate the export")
    return groups


def _identity(proof):
    return {key: proof[key] for key in ("key", "target", "members", "inputs")}


def removals(plan, state):
    """Prove every replacement using both saved group and desired edge evidence."""
    desired_groups = validate(plan)
    old_groups = {}
    for key, record in state["items"].items():
        if key.startswith(PREFIX):
            proof = _valid(record.get("context_group_proof"))
            if record["endpoint"] != "shapes" or proof["key"] != key:
                raise TraceError("Context-summary mapping disagrees with its membership proof")
            old_groups[key] = proof
    if not desired_groups and not old_groups:
        return {}
    shapes = {item["key"]: item for item in plan["shapes"]}
    connectors = {item["key"]: item for item in plan["connectors"]}
    result = {}

    def retire(key, group, *, geometry=None, shape=None):
        record = state["items"].get(key)
        if record is None:
            return
        proof = {"kind": "context_group_replacement", "version": VERSION,
                 "endpoint": record["endpoint"], "group": copy.deepcopy(group)}
        if geometry is not None:
            if record["endpoint"] != "shapes":
                raise TraceError("Context-summary member mapping has the wrong type")
            proof.update(geometry={axis: geometry[axis] for axis in ("width", "height")}, shape=shape)
        else:
            if record["endpoint"] != "connectors":
                raise TraceError("Context-summary input mapping has the wrong type")
            proof.update(source=record.get("source"), target=record.get("target"))
        if key in result and result[key] != proof:
            # A connector can be retired by both its old and new group proof.
            return
        result[key] = proof

    for key, old in old_groups.items():
        new = desired_groups.get(key)
        changed = new is None or _identity(old) != _identity(new)
        for edge_key, expected in old["inputs"].items():
            item = connectors.get(edge_key)
            if not item or any(item.get("context_evidence", {}).get(field) != expected[field]
                               for field in ("source", "target", "outpoint")):
                raise TraceError("Cannot restore context summary: original input evidence is missing or changed")
            if item["source"] != key:
                if item["source"] != expected["source"] or item["source"] not in shapes:
                    raise TraceError("Context-summary restoration would change an original address")
                if shapes[item["source"]]["body"]["data"]["shape"] != "circle":
                    raise TraceError("Context-summary restoration requires original address circles")
            record = state["items"].get(edge_key)
            if record and (record.get("source") != key or record.get("target") != old["target"]):
                raise TraceError("Saved context-summary input disagrees with its membership proof")
            if changed:
                retire(edge_key, old)
        if changed:
            retire(key, old, geometry=old["geometry"], shape="rectangle")

    for key, new in desired_groups.items():
        for member_key, member in new["members"].items():
            retire(member_key, new, geometry=member, shape="circle")
        for edge_key, expected in new["inputs"].items():
            record = state["items"].get(edge_key)
            if record is None or (record.get("source") == key and key in old_groups):
                continue
            if record.get("source") != expected["source"] or record.get("target") != expected["target"]:
                raise TraceError("Grouping would change an unproven Miro input connection")
            retire(edge_key, new)
    # Prove that no old run's still-managed connection loses its endpoint.
    for key, record in state["items"].items():
        if record["endpoint"] == "connectors" and key not in result:
            if record.get("source") in result or record.get("target") in result:
                raise TraceError("Another managed connection uses a context address; leave that address ungrouped")
    return result


def check_remote(state, remote, removals, inventory):
    """Refuse destructive presentation replacement when analyst work is visible."""
    from .miro import _editable, _fields, _get, _same
    shape_ids = {state["items"][key]["id"] for key, proof in removals.items()
                 if proof["endpoint"] == "shapes"}
    connector_ids = {state["items"][key]["id"] for key, proof in removals.items()
                     if proof["endpoint"] == "connectors"}
    for body in inventory.values():
        ends = [(body.get(field) or {}).get("id") for field in ("startItem", "endItem")]
        if shape_ids.intersection(ends) and body["id"] not in connector_ids:
            raise TraceError("A board connector attaches to a retiring context object; preserve that attachment before syncing. No board writes made.")
    for key, proof in removals.items():
        record = state["items"][key]
        if key not in remote:  # Verified attempted deletion, checked by preflight.
            continue
        body = remote[key]
        actual = _editable(body, record["endpoint"])
        if any(not _same(_get(actual, path), value, path) for path, value in _fields(record["managed"])):
            raise TraceError("Context object has manual edits; preserve its notes/styles before changing grouping. No board writes made.")
        if record["endpoint"] == "connectors":
            if record["id"] not in inventory:
                raise TraceError("Complete connector inventory is missing a mapped context input; no board writes made")
            for field, logical in (("startItem", "source"), ("endItem", "target")):
                expected = state["items"].get(record[logical], {}).get("id")
                if body.get(field, {}).get("id") != expected:
                    raise TraceError("A context connector was manually reattached; restore it before changing grouping")
            continue
        if body.get("data", {}).get("shape") != proof["shape"]:
            raise TraceError("Context object shape was manually changed; no board writes made")
        if (body.get("parent") or {}).get("id"):
            raise TraceError("Move retiring context objects out of frames/groups before changing grouping")
        try:
            geometry = body["geometry"]
            if (any(not math.isclose(float(geometry[axis]), float(proof["geometry"][axis]), abs_tol=.01)
                    for axis in ("width", "height"))
                    or float(geometry.get("rotation", body.get("rotation", 0))) % 360):
                raise ValueError
        except (KeyError, ValueError, TypeError, OverflowError):
            raise TraceError("Context object was resized or rotated; preserve its changes before grouping") from None
