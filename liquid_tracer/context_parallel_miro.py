"""Connector-only Miro migrations for parallel UTXOs at one address.

The address is never a replacement target. Full canonical membership travels
with the generated connector so expansion and promotion can prove each input.
"""
import copy

from .common import TraceError
from .context_connectors import PARALLEL_PREFIX as PREFIX, parallel_identity

VERSION = 1
UTXO_VERSION = 2
INPUT_ROLES = frozenset({"context_input", "traced_input"})
OUTPUT_ROLES = frozenset({"context_output", "candidate_output", "seed_output"})
KIND = "context_parallel_replacement"


def catalog(graph):
    from .context_connectors import canonical_graph, display_graph
    from .context_group_miro import evidence

    displayed = display_graph(graph)
    edges = {edge["id"]: edge for edge in canonical_graph(graph)["edges"]}
    result = {}
    for edge in displayed["edges"]:
        if not edge["id"].startswith(PREFIX):
            continue
        summary = edge["details"]["context_summary"]
        members = summary["member_edge_ids"]
        result[edge["id"]] = {"version": VERSION, "key": edge["id"],
            "source": edge["source"], "target": edge["target"],
            "inputs": {key: evidence(edges[key]) for key in members}}
        if summary["version"] >= 3:
            result[edge["id"]].update(version=UTXO_VERSION, direction=summary["direction"])
    return result


def _index(value):
    return (isinstance(value, str) and value.isascii() and value.isdecimal()
            and str(int(value)) == value)


def _valid(proof):
    try:
        if not isinstance(proof, dict) or type(proof.get("version")) is not int:
            raise ValueError
        modern = proof["version"] == UTXO_VERSION
        fields = {"version", "key", "source", "target", "inputs"}
        if modern:
            fields.add("direction")
        direction = proof.get("direction", "input")
        transaction = proof["target"] if direction == "input" else proof["source"]
        if (set(proof) != fields or proof["version"] not in (VERSION, UTXO_VERSION)
                or direction not in ("input", "output")
                or any(not isinstance(proof[field], str) or not proof[field] for field in ("source", "target"))
                or not transaction.startswith("tx:") or not transaction[3:]
                or proof["key"] != parallel_identity(proof["source"], proof["target"])
                or not isinstance(proof["inputs"], dict) or len(proof["inputs"]) < 2):
            raise ValueError
        prefix = ("in:" if direction == "input" else "out:") + transaction[3:] + ":"
        roles = (INPUT_ROLES if direction == "input" else OUTPUT_ROLES) if modern else {"context_input"}
        for key, evidence in proof["inputs"].items():
            outpoint = evidence.get("outpoint")
            if (not isinstance(key, str) or not key.startswith(prefix) or not _index(key[len(prefix):])
                    or set(evidence) != {"source", "target", "outpoint", "role"}
                    or evidence["source"] != proof["source"] or evidence["target"] != proof["target"]
                    or evidence["role"] not in roles or not isinstance(outpoint, str)
                    or not outpoint.rpartition(":")[0] or not _index(outpoint.rpartition(":")[2])
                    or (direction == "output" and outpoint != key[4:])):
                raise ValueError
        if modern and len({item["outpoint"] for item in proof["inputs"].values()}) != len(proof["inputs"]):
            raise ValueError
        return proof
    except (KeyError, TypeError, ValueError, AttributeError):
        raise TraceError("Invalid repeated-context connector proof; regenerate the plot") from None


def display_evidence(edge):
    summary = edge["details"]["context_summary"]
    result = {"version": VERSION, "key": edge["id"], "source": edge["source"], "target": edge["target"],
              "member_edge_ids": list(summary["member_edge_ids"])}
    if summary["version"] >= 3:
        result.update(version=UTXO_VERSION, direction=summary["direction"])
    return result


def validate(plan):
    proofs = plan.get("context_parallel_items", {})
    if not isinstance(proofs, dict):
        raise TraceError("Invalid repeated-context connector catalog; regenerate the plot")
    shapes = {item["key"]: item for item in plan["shapes"]}
    connectors = {item["key"]: item for item in plan["connectors"]}
    if {key for key in connectors if key.startswith(PREFIX)} != set(proofs):
        raise TraceError("Repeated-context display connectors require membership proofs")
    used = set()
    grouped_inputs = {key for group in plan.get("context_group_items", {}).values() for key in group["inputs"]}
    for key, proof in proofs.items():
        _valid(proof)
        edge = connectors[key]
        expected = {"version": proof["version"], "key": key, "source": proof["source"], "target": proof["target"],
                    "member_edge_ids": sorted(proof["inputs"])}
        direction = proof.get("direction", "input")
        if proof["version"] == UTXO_VERSION:
            expected["direction"] = direction
        source_shape, target_shape = (("circle", "rectangle") if direction == "input" else ("rectangle", "circle"))
        if (proof["key"] != key or edge.get("context_parallel_evidence") != expected
                or "context_evidence" in edge or "context_display_evidence" in edge
                or edge["source"] != proof["source"] or edge["target"] != proof["target"]
                or shapes.get(proof["source"], {}).get("body", {}).get("data", {}).get("shape") != source_shape
                or shapes.get(proof["target"], {}).get("body", {}).get("data", {}).get("shape") != target_shape
                or (used | grouped_inputs | connectors.keys()).intersection(proof["inputs"])):
            raise TraceError("Repeated-context connector disagrees with its original input evidence")
        used.update(proof["inputs"])
    for edge in connectors.values():
        if "context_parallel_evidence" in edge and edge["key"] not in proofs:
            raise TraceError("Unexpected repeated-context connector evidence")
    return proofs


def scoped_retirement(plan, state, key):
    """Only a matching captured board projection can omit old evidence."""
    from . import board_layout, plot_miro
    snapshot, projection = board_layout.validate(plan), plot_miro.validate(plan)
    record = state["items"].get(key)
    proof = record.get("projection_proof", {}) if record else {}
    captured = (snapshot or {}).get("mapped", {}).get(key, {})
    return bool(snapshot and projection and record
                and snapshot["board_id"] == state.get("board_id")
                and snapshot["namespace"] == state.get("namespace") == plan.get("namespace")
                and proof.get("projection") == projection and proof.get("endpoint") == record["endpoint"]
                and captured == board_layout.mapped_record(record))


def input_evidence(plan):
    """Canonical evidence across all physical representations, after validation."""
    result = {item["key"]: item["context_evidence"] for item in plan["connectors"] if "context_evidence" in item}
    for group in plan.get("context_group_items", {}).values():
        result.update(group["inputs"])
    for proof in validate(plan).values():
        result.update(proof["inputs"])
    return result


def _same_input(actual, expected):
    # Saved creation evidence can retain the old presentation role after an
    # ordinary sync promotes or demotes the same input. Its UTXO identity and
    # endpoints must still match exactly; no other role is eligible.
    return (isinstance(actual, dict) and set(actual) == {"source", "target", "outpoint", "role"}
            and actual["role"] in ("context_input", "traced_input")
            and all(actual[field] == expected[field] for field in ("source", "target", "outpoint")))


def _same_edge(actual, expected):
    """Styling can change within a direction; physical UTXO identity cannot."""
    if not isinstance(expected, dict):
        return False
    roles = INPUT_ROLES if expected.get("role") in INPUT_ROLES else OUTPUT_ROLES
    return (isinstance(actual, dict) and set(actual) == {"source", "target", "outpoint", "role"}
            and expected.get("role") in roles and actual.get("role") in roles
            and all(actual[field] == expected[field] for field in ("source", "target", "outpoint")))


def removals(plan, state):
    """Retire only proved physical connectors; never an address shape."""
    desired = validate(plan)
    old = {}
    for key, record in state["items"].items():
        if key.startswith(PREFIX) or "context_parallel_proof" in record:
            proof = _valid(record.get("context_parallel_proof"))
            if (record["endpoint"] != "connectors" or proof["key"] != key
                    or any(record.get(field) != proof[field] for field in ("source", "target"))):
                raise TraceError("Repeated-context mapping disagrees with its membership proof")
            old[key] = proof
    if not old and not desired:
        return {}
    evidence = input_evidence(plan)
    connectors = {item["key"]: item for item in plan["connectors"]}
    shapes = {item["key"]: item for item in plan["shapes"]}
    scope_cache = {}
    result = {}

    def retire(key, proof):
        record = state["items"].get(key)
        if record is None:
            return
        if record["endpoint"] != "connectors":
            raise TraceError("Repeated-context replacement must be a connector")
        result[key] = {"kind": KIND, "version": VERSION, "endpoint": "connectors",
                       "source": record["source"], "target": record["target"], "bundle": copy.deepcopy(proof)}

    for key, proof in old.items():
        for member, expected in proof["inputs"].items():
            actual = evidence.get(member)
            if actual is None:
                if key not in scope_cache:
                    scope_cache[key] = scoped_retirement(plan, state, key)
                if scope_cache[key]:
                    continue
            if not _same_edge(actual, expected):
                raise TraceError("Cannot replace repeated-context connector: original input evidence is missing or changed")
            item = connectors.get(member)
            if item and not item["source"].startswith("context-group:"):
                address_field = "source" if proof.get("direction", "input") == "input" else "target"
                if (any(item.get(field) != expected[field] for field in ("source", "target"))
                        or shapes[item[address_field]]["body"]["data"]["shape"] != "circle"):
                    raise TraceError("Repeated-context restoration would change an original address")
        if desired.get(key) != proof:
            retire(key, proof)
    for proof in desired.values():
        for key, expected in proof["inputs"].items():
            record = state["items"].get(key)
            if record is None:
                continue
            if record.get("source", "").startswith("context-group:"):
                # The isolated-group migration owns its old rectangle and
                # inputs. It validates this desired canonical evidence too.
                from .context_group_miro import _valid as valid_group
                group = valid_group(state["items"].get(record["source"], {}).get("context_group_proof"))
                if group["inputs"].get(key) != expected:
                    raise TraceError("Repeated-context migration changed isolated-group input evidence")
                continue
            if (record.get("source") != expected["source"] or record.get("target") != expected["target"]
                    or ("context_evidence" in record and not _same_edge(record["context_evidence"], expected))):
                raise TraceError("Bundling would change original input evidence")
            retire(key, proof)
    return result
