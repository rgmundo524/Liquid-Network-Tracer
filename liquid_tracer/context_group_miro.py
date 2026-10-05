"""Evidence-checked, reversible Miro publication of optional context summaries.

A toggle replaces only unchanged generated objects. Connector logical keys and
UTXO evidence survive; remote IDs may change. The ordinary sync deletion and
creation journals make interrupted replacements resumable without blind POSTs.
"""

from .networks import blockchain, is_primary
import copy
import math

from .common import TraceError

PREFIX = "context-group:"
VERSION = 1
DISPLAY_VERSION = 2
DISPLAY_PREFIX = "context-inputs:"


def evidence(edge):
    return {"source": edge.get("original_source", edge["source"]),
            "target": edge["target"], "outpoint": edge.get("outpoint"),
            "role": edge.get("role")}


def catalog(graph):
    """Keep a small membership proof, not copies of every investigative record."""
    from .context_connectors import canonical_graph, display_graph

    displayed = display_graph(graph)
    graph = canonical_graph(graph)
    summaries = {edge["source"]: edge for edge in displayed["edges"]
                 if edge["id"].startswith(DISPLAY_PREFIX)}
    edges = {edge["id"]: edge for edge in graph["edges"]}
    result = {}
    for node in graph["nodes"]:
        if node["kind"] != "context_group":
            continue
        details = node["details"]
        members = {}
        for member in details["members"]:
            info = member.get("details", {})
            if member["kind"] != "address" or not is_primary(member, graph) or not info.get("address"):
                raise TraceError("Context summary contains an unproven address; regenerate the export")
            members[member["id"]] = {"address": info["address"],
                                      "width": member["width"], "height": member["height"]}
        result[node["id"]] = {
            "version": VERSION, "key": node["id"], "target": details["transaction_id"],
            "members": members,
            "inputs": {key: evidence(edges[key]) for key in details["input_edge_ids"]},
            "geometry": {axis: node[axis] for axis in ("width", "height")},
        }
        if node["id"] in summaries:
            summary = summaries[node["id"]]
            result[node["id"]].update(version=DISPLAY_VERSION, display_inputs={
                summary["id"]: list(summary["details"]["context_summary"]["member_edge_ids"])})
    return result


def _valid(proof):
    try:
        if (not isinstance(proof, dict) or type(proof.get("version")) is not int
                or proof["version"] not in (VERSION, DISPLAY_VERSION)
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
        if proof["version"] == DISPLAY_VERSION:
            expected = {DISPLAY_PREFIX + proof["target"][3:]: sorted(proof["inputs"])}
            if proof.get("display_inputs") != expected:
                raise ValueError
        elif "display_inputs" in proof:
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        raise TraceError("Invalid context-summary membership proof; regenerate the export or restore the mapping") from None
    return proof


def _display_inputs(proof):
    """Map actual board connectors to their complete canonical input evidence."""
    return (proof["display_inputs"] if proof["version"] == DISPLAY_VERSION
            else {key: [key] for key in proof["inputs"]})


def display_evidence(edge):
    summary = edge["details"]["context_summary"]
    return {"version": VERSION, "group": edge["source"], "target": edge["target"],
            "member_edge_ids": list(summary["member_edge_ids"])}


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
        if proof["version"] == DISPLAY_VERSION and connectors.keys() & proof["inputs"].keys():
            raise TraceError("Context-summary inputs cannot also have individual display connectors")
        members.update(proof["members"])
        incident = {edge["key"] for edge in connectors.values()
                    if key in (edge["source"], edge["target"])}
        if incident != set(_display_inputs(proof)):
            raise TraceError("Context summary has unexpected connections; regenerate the export")
        for edge_key, input_keys in _display_inputs(proof).items():
            item = connectors[edge_key]
            if item["source"] != key or item["target"] != proof["target"]:
                raise TraceError("Context-summary input evidence changed; regenerate the export")
            if proof["version"] == DISPLAY_VERSION:
                expected = {"version": VERSION, "group": key, "target": proof["target"],
                            "member_edge_ids": input_keys}
                valid = item.get("context_display_evidence") == expected and "context_evidence" not in item
            else:
                valid = (item.get("context_evidence") == proof["inputs"][edge_key]
                         and "context_display_evidence" not in item)
            if not valid:
                raise TraceError("Context-summary input evidence changed; regenerate the export")
    aggregates = {key for key in connectors if key.startswith(DISPLAY_PREFIX)}
    proved = {key for proof in groups.values() if proof["version"] == DISPLAY_VERSION
              for key in _display_inputs(proof)}
    if aggregates != proved:
        raise TraceError("Context-summary display connectors require membership proofs; regenerate the export")
    if any("context_display_evidence" in edge and key not in proved for key, edge in connectors.items()):
        raise TraceError("Unexpected context-summary display evidence; regenerate the export")
    return groups


def _identity(proof):
    identity = {key: proof[key] for key in ("key", "target", "members", "inputs")}
    if proof["version"] == DISPLAY_VERSION:
        identity["display_inputs"] = proof["display_inputs"]
    return identity


def resize_updates(plan, state, remote, removed):
    """Shrink unchanged legacy summaries during an explicit reorganization.

    The membership proof supplies the generated size, since normal graph
    geometry intentionally remains analyst-owned. Never resize other shapes or
    a summary whose content, font, dimensions or rotation was edited.
    """
    from .miro import _same
    from .miro_legend_updates import geometry_snapshot

    updates = {}
    shapes = {item["key"]: item["body"] for item in plan["shapes"]}
    for key, desired in plan.get("context_group_items", {}).items():
        record, actual = state["items"].get(key), remote.get(key)
        if key in removed or not record or not actual or record.get("endpoint") != "shapes":
            continue
        previous = _valid(record.get("context_group_proof"))
        old = geometry_snapshot({"geometry": previous["geometry"]})
        new = geometry_snapshot({"geometry": desired["geometry"]})
        live = geometry_snapshot(actual)
        legacy = {"width": 240., "height": float(max(160, (len(previous["inputs"]) + 1) * 18))}
        if (_identity(previous) != _identity(desired) or old != legacy or new != {"width": 240., "height": 160.}
                or old["height"] <= new["height"] or live is None
                or any(not math.isclose(live[axis], old[axis], rel_tol=0, abs_tol=.01) for axis in old)
                or actual.get("data", {}).get("shape") != "rectangle"):
            continue
        try:
            rotation = float(actual.get("geometry", {}).get("rotation", actual.get("rotation", 0)))
            if not math.isfinite(rotation) or rotation % 360:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        managed = record.get("managed", {})
        content = actual.get("data", {}).get("content")
        if (not isinstance(content, str) or not any(
                _same(content, body.get("data", {}).get("content"), ("data", "content"))
                for body in (managed, shapes[key]))
                or not _same(actual.get("style", {}).get("fontSize"),
                             managed.get("style", {}).get("fontSize"), ("style", "fontSize"))):
            continue
        updates[key] = {"geometry": copy.deepcopy(desired["geometry"])}
    return updates


def acknowledge_resize(record, body, expected):
    """Refresh only acknowledged dimensions, also after an uncertain PATCH."""
    from .miro_legend_updates import geometry_snapshot

    current = geometry_snapshot(body)
    desired = geometry_snapshot({"geometry": expected})
    if current is None or desired is None or any(
            not math.isclose(current[axis], desired[axis], rel_tol=0, abs_tol=.01) for axis in current):
        return False
    proof = copy.deepcopy(_valid(record.get("context_group_proof")))
    proof["geometry"] = current
    record["context_group_proof"] = proof
    return True


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
    from .context_parallel_miro import _same_input, validate as validate_parallel
    parallel_inputs = {key: (evidence, proof["source"]) for proof in validate_parallel(plan).values()
                       for key, evidence in proof["inputs"].items()}

    def scoped_retirement(key):
        # A revised board projection can omit old inputs altogether. Ordinary
        # group toggles must still prove every original input is restored.
        # Only the matching captured board and generated projection authorize
        # this relaxation; remote checks continue to protect investigator work.
        from . import board_layout, plot_miro

        snapshot = board_layout.validate(plan)
        projection = plot_miro.validate(plan)
        record = state["items"].get(key)
        proof = record.get("projection_proof", {}) if record else {}
        captured = (snapshot or {}).get("mapped", {}).get(key, {})
        return bool(snapshot and projection and record
                    and snapshot["board_id"] == state.get("board_id")
                    and snapshot["namespace"] == state.get("namespace") == plan.get("namespace")
                    and proof.get("projection") == projection
                    and proof.get("endpoint") == record["endpoint"]
                    and captured.get("id") == record["id"]
                    and captured.get("endpoint") == record["endpoint"])

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
        scoped = changed and scoped_retirement(key)
        omitted = set()
        # Validate canonical UTXO evidence independently of whether the desired
        # board represents it by one line per input or by one summary line.
        for edge_key, expected in old["inputs"].items():
            item = connectors.get(edge_key)
            desired = new["inputs"].get(edge_key) if new else None
            if desired is None and item:
                desired = item.get("context_evidence", {})
            if desired is None and edge_key in parallel_inputs:
                desired = parallel_inputs[edge_key][0]
            if desired is None and scoped:
                omitted.add(edge_key)
                continue
            if desired is None or any(desired.get(field) != expected[field]
                                      for field in ("source", "target", "outpoint")):
                raise TraceError("Cannot restore context summary: original input evidence is missing or changed")
            if new is None or edge_key not in new["inputs"]:
                source = parallel_inputs[edge_key][1] if edge_key in parallel_inputs else item["source"]
                if source != expected["source"] or source not in shapes:
                    raise TraceError("Context-summary restoration would change an original address")
                if shapes[source]["body"]["data"]["shape"] != "circle":
                    raise TraceError("Context-summary restoration requires original address circles")
        for edge_key, input_keys in _display_inputs(old).items():
            record = state["items"].get(edge_key)
            if record and (record.get("source") != key or record.get("target") != old["target"]):
                raise TraceError("Saved context-summary input disagrees with its membership proof")
            if record and omitted.intersection(input_keys) and not scoped_retirement(edge_key):
                raise TraceError("An omitted context input has no matching board projection proof")
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
            if (record.get("source") != expected["source"] or record.get("target") != expected["target"]
                    or ("context_evidence" in record and not _same_input(record["context_evidence"], expected))):
                raise TraceError("Grouping would change an unproven Miro input connection")
            retire(edge_key, new)
    # Prove that no old run's still-managed connection loses its endpoint.
    for key, record in state["items"].items():
        if record["endpoint"] == "connectors" and key not in result:
            if record.get("source") in result or record.get("target") in result:
                raise TraceError("Another managed connection uses a context address; leave that address ungrouped")
    return result


def check_remote(state, remote, removals, inventory, *, projection_removals=None, allow_organization=False):
    """Refuse destructive presentation replacement when analyst work is visible."""
    from .miro_conflicts import MiroEditConflict, editable_report
    shape_ids = {state["items"][key]["id"] for key, proof in removals.items()
                 if proof["endpoint"] == "shapes"}
    connector_ids = {state["items"][key]["id"] for key, proof in removals.items()
                     if proof["endpoint"] == "connectors"}
    # The projection checker validates omitted edges before this checker runs.
    # A retiring connector from that same update is not an analyst attachment.
    connector_ids.update(state["items"][key]["id"] for key, proof in (projection_removals or {}).items()
                         if proof.get("kind") == "board_projection" and proof.get("endpoint") == "connectors"
                         and key in state["items"] and state["items"][key]["endpoint"] == "connectors")
    for body in inventory.values():
        ends = [(body.get(field) or {}).get("id") for field in ("startItem", "endItem")]
        if shape_ids.intersection(ends) and body["id"] not in connector_ids:
            raise TraceError("A board connector attaches to a retiring context object; preserve that attachment before syncing. No board writes made.")
    report = editable_report(state, remote, {**(projection_removals or {}), **removals})
    if report:
        raise MiroEditConflict('Context object has manual edits; open the affected objects below to review the changed fields. No board writes made.', report)
    for key, proof in removals.items():
        record = state["items"][key]
        if key not in remote:  # Verified attempted deletion, checked by preflight.
            continue
        body = remote[key]
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
        if allow_organization:
            # A board-aware update reviewed these current organizational
            # changes before authorizing retirement of generated objects.
            continue
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
