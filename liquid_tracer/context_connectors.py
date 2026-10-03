"""Validated display connectors for isolated groups and repeated address inputs.

Canonical edges remain the evidence and CSV inventory. Only a temporary display
projection replaces each group's inputs with one connector; its membership is
rederived before use. Persisted flags cannot impersonate a runtime projection.
"""
from collections import defaultdict
from copy import deepcopy

from .common import TraceError, canonical, digest

VERSION = 2
PREFIX = "context-inputs:"
PARALLEL_PREFIX = "context-parallel:"
GEOMETRY_FIELDS = frozenset({"route", "attachment", "connector_shape", "routing_exception", "label_layout"})
_ERROR = "Invalid context connector summary; regenerate the plot from its saved evidence"


class _DisplayGraph(dict):
    def __init__(self, value, canonical):
        super().__init__(value)
        self._canonical = canonical

    def __deepcopy__(self, memo):
        # The canonical graph is a read-only evidence reference, not another
        # complete graph to copy for every geometry candidate.
        result = _DisplayGraph({}, self._canonical)
        memo[id(self)] = result
        for key, value in self.items():
            result[deepcopy(key, memo)] = deepcopy(value, memo)
        return result


def _inventory(graph):
    nodes = {node["id"]: node for node in graph["nodes"]}
    edges = {edge["id"]: edge for edge in graph["edges"]}
    if (len(nodes) != len(graph["nodes"]) or len(edges) != len(graph["edges"])
            or set(nodes).intersection(edges)):
        raise TraceError(_ERROR)
    return nodes, edges


def _derive_groups(graph):
    """Preserve the exact v1 isolated-group derivation for saved previews."""
    from .context_groups import CONTEXT_GROUP_VERSION, _input_edge, _protected

    try:
        nodes, edges = _inventory(graph)
        controls = graph.get("context_groups", {})
        if (type(controls.get("version")) is not int or controls["version"] != CONTEXT_GROUP_VERSION
                or controls.get("enabled") is not True
                or graph.get("graph_options", {}).get("group_context_inputs") is not True):
            raise ValueError
        incident = defaultdict(list)
        for edge in edges.values():
            if edge["source"] not in nodes or edge["target"] not in nodes:
                raise ValueError
            incident[edge["source"]].append(edge)
            incident[edge["target"]].append(edge)
        visible_addresses = {node.get("details", {}).get("address") for node in nodes.values()
                             if node["kind"] == "address" and node.get("details", {}).get("network") == "liquid"}
        designated = {f"{txid}:{item['vout']}"
                      for txid, item in graph.get("service_controls", {}).get("change_outputs", {}).items()
                      if isinstance(item, dict) and type(item.get("vout")) is int}
        hubs = set(graph.get("graph_options", {}).get("hub_addresses", []))
        seen_members, seen_addresses, seen_inputs = set(), set(), set()
        summaries = []
        for node in sorted(nodes.values(), key=lambda item: item["id"]):
            if node["kind"] != "context_group":
                continue
            details = node["details"]
            target = details["transaction_id"]
            if (not isinstance(target, str) or not target.startswith("tx:")
                    or nodes.get(target, {}).get("kind") != "transaction"
                    or node["id"] != "context-group:" + target[3:]
                    or not isinstance(details["members"], list)):
                raise ValueError
            members, addresses = set(), set()
            for member in details["members"]:
                info = member.get("details", {})
                address = info.get("address")
                key = member["id"]
                if (member["kind"] != "address" or info.get("network") != "liquid"
                        or not isinstance(address, str) or not address or _protected(member)
                        or not isinstance(key, str) or not key or key in members or key in nodes
                        or key in seen_members or address in visible_addresses or address in seen_addresses
                        or address in hubs):
                    raise ValueError
                members.add(key)
                addresses.add(address)
            inputs = incident[node["id"]]
            input_ids = sorted(edge["id"] for edge in inputs)
            if (len(addresses) < 2 or not inputs or len(input_ids) != len(set(input_ids))
                    or details["input_edge_ids"] != input_ids
                    or type(details["input_count"]) is not int or details["input_count"] != len(inputs)
                    or type(details["address_count"]) is not int or details["address_count"] != len(addresses)
                    or {edge.get("original_source") for edge in inputs} != members):
                raise ValueError
            for edge in inputs:
                if (not _input_edge(edge, node["id"], target)
                        or edge.get("details", {}).get("validated_trace_link") is not None
                        or not edge["id"].startswith("in:" + target[3:] + ":")
                        or not isinstance(edge.get("outpoint"), str) or ":" not in edge["outpoint"]
                        or edge["outpoint"] in designated or edge["id"] in seen_inputs):
                    raise ValueError
            identity = PREFIX + target[3:]
            if identity in nodes or identity in edges:
                raise ValueError
            summaries.append({"id": identity, "source": node["id"], "target": target,
                "role": "context_input", "outpoint": None,
                "label": f"{len(inputs)} context inputs · {len(addresses)} addresses", "quantity": "",
                "details": {"context_summary": {"version": 1, "input_count": len(inputs),
                    "address_count": len(addresses), "member_edge_ids": input_ids}}})
            seen_members.update(members)
            seen_addresses.update(addresses)
            seen_inputs.update(input_ids)
        counts = {"group_count": len(summaries), "grouped_node_count": len(seen_members),
                  "address_count": len(seen_addresses), "input_count": len(seen_inputs)}
        if any(type(controls.get(key)) is not int or controls[key] != value for key, value in counts.items()):
            raise ValueError
        return summaries
    except (KeyError, TypeError, ValueError, AttributeError):
        raise TraceError(_ERROR) from None


def parallel_identity(source, target):
    """One stable display connector per visible address/transaction pair."""
    return PARALLEL_PREFIX + digest(canonical([source, target]))[:24]


def _derive_parallel(graph):
    """Bundle plain context edges, never the visible address or its other UTXOs.

    Exact displayed output/input topology protects a continuation even when its
    trace role is context. Address reuse cannot transfer that protection to an
    unrelated input. Named, shared and hub addresses remain visible unchanged.
    """
    from .context_groups import _input_edge

    try:
        nodes, edges = _inventory(graph)
        seeds = set(graph.get("run", {}).get("seeds") or [])
        designated = {f"{txid}:{item['vout']}"
                      for txid, item in graph.get("service_controls", {}).get("change_outputs", {}).items()
                      if isinstance(item, dict) and type(item.get("vout")) is int}
        protected_outpoints = seeds | designated
        # Protect exact output continuations regardless of layout coordinates:
        # changing rows/columns must never change which evidence is summarized.
        outputs = {(edge["outpoint"], edge["target"])
                   for edge in edges.values()
                   if nodes[edge["source"]]["kind"] == "transaction"
                   and nodes[edge["target"]]["kind"] == "address"
                   and isinstance(edge.get("outpoint"), str) and edge["outpoint"]}
        candidates = defaultdict(list)
        for edge in edges.values():
            source, target = nodes[edge["source"]], nodes[edge["target"]]
            info = source.get("details", {})
            address = info.get("address")
            if (source["kind"] != "address" or target["kind"] != "transaction"
                    or info.get("network") != "liquid" or not isinstance(address, str) or not address
                    or not _input_edge(edge, source["id"], target["id"])
                    or edge.get("details", {}).get("validated_trace_link") is not None
                    or edge.get("original_source") is not None
                    or edge.get("outpoint") in protected_outpoints
                    or (edge.get("outpoint"), source["id"]) in outputs):
                continue
            vin = edge["details"].get("vin", {})
            txid, index = vin.get("txid"), vin.get("vout")
            # A display-only summary must still carry exact canonical vin IDs
            # and outpoints. Unknown/malformed inputs stay individual.
            prefix = "in:" + target["id"].removeprefix("tx:") + ":"
            suffix = edge["id"].removeprefix(prefix)
            if (not isinstance(txid, str) or not txid or type(index) is not int or index < 0
                    or edge.get("outpoint") != f"{txid}:{index}"
                    or not edge["id"].startswith(prefix) or not suffix.isascii()
                    or not suffix.isdecimal() or str(int(suffix)) != suffix):
                continue
            candidates[source["id"], target["id"]].append(edge["id"])
        summaries = []
        for (source, target), inputs in sorted(candidates.items()):
            if len(inputs) < 2:
                continue
            identity = parallel_identity(source, target)
            if identity in nodes or identity in edges:
                raise ValueError
            summaries.append({"id": identity, "source": source, "target": target,
                "role": "context_input", "outpoint": None,
                "label": f"{len(inputs)} context inputs · 1 address", "quantity": "",
                "details": {"context_summary": {"version": VERSION, "kind": "parallel",
                    "input_count": len(inputs), "address_count": 1,
                    "member_edge_ids": sorted(inputs)}}})
        return summaries
    except (KeyError, TypeError, ValueError, AttributeError):
        raise TraceError(_ERROR) from None


def _derive(graph, version=VERSION):
    summaries = _derive_groups(graph)
    return summaries if version == 1 else summaries + _derive_parallel(graph)


def _validated_summaries(graph):
    if "context_connectors" not in graph:
        return []
    metadata = graph["context_connectors"]
    if (not isinstance(metadata, dict) or set(metadata) != {"version", "summaries"}
            or type(metadata["version"]) is not int or metadata["version"] not in (1, VERSION)
            or not isinstance(metadata["summaries"], list)):
        raise TraceError(_ERROR)
    expected = _derive(graph, metadata["version"])
    if len(metadata["summaries"]) != len(expected):
        raise TraceError(_ERROR)
    for actual, wanted in zip(metadata["summaries"], expected):
        if (not isinstance(actual, dict)
                or {key: value for key, value in actual.items() if key not in GEOMETRY_FIELDS} != wanted):
            raise TraceError(_ERROR)
        proof = actual["details"]["context_summary"]
        if any(type(proof[key]) is not int for key in ("version", "input_count", "address_count")):
            raise TraceError(_ERROR)
    return metadata["summaries"]


def prepare(graph):
    """Opt a newly grouped graph in; never silently upgrade legacy snapshots."""
    if "context_connectors" in graph:
        _validated_summaries(graph)
        return graph
    result = dict(graph)
    result["context_connectors"] = {"version": VERSION, "summaries": _derive(graph)}
    return result


def summaries(graph):
    """Return validated summary metadata (also usable by Miro membership code)."""
    if isinstance(graph, _DisplayGraph):
        identities = {value["id"] for value in _validated_summaries(graph._canonical)}
        return [edge for edge in graph["edges"] if edge["id"] in identities]
    return _validated_summaries(graph)


def display_graph(graph):
    """Return a private, idempotent projection for layout and visual exports."""
    if isinstance(graph, _DisplayGraph):
        return graph
    summaries = _validated_summaries(graph)
    if not summaries:
        return graph
    hidden = {key for summary in summaries for key in summary["details"]["context_summary"]["member_edge_ids"]}
    result = _DisplayGraph(deepcopy(graph), graph)
    result["edges"] = [edge for edge in result["edges"] if edge["id"] not in hidden]
    result["edges"].extend(result["context_connectors"]["summaries"])
    if "activity_frames" in graph:
        from .miro_frames import activity_frames
        result["activity_frames"] = activity_frames(result,
            indexed=graph["activity_frames"].get("schema_version") != 1)
    return result


def evidence_graph(graph):
    """Read canonical inputs with the current display's objects for catalogs.

    Internal board-addition projections may contain a node subset. Preserve
    only evidence whose endpoints are both in that subset; this helper is not
    a request to turn the subset into a newly validated complete preview.
    """
    if not isinstance(graph, _DisplayGraph):
        return graph
    result = dict(graph)
    nodes = {node["id"] for node in graph["nodes"]}
    ordinary = {edge["id"]: edge for edge in graph["edges"]}
    result["edges"] = [ordinary.get(edge["id"], edge) for edge in graph._canonical["edges"]
                       if edge["source"] in nodes and edge["target"] in nodes]
    return result


canonical_graph = evidence_graph


def restore_graph(display_result, canonical_original):
    """Keep laid-out display geometry and restore every original input record."""
    originals = _validated_summaries(canonical_original)
    if not originals:
        return display_result
    try:
        # Compare against a lightweight expected inventory. Re-projecting here
        # would deep-copy all original evidence just to inspect edge identities.
        hidden = {key for summary in originals for key in summary["details"]["context_summary"]["member_edge_ids"]}
        expected = {**canonical_original, "edges": [edge for edge in canonical_original["edges"]
                    if edge["id"] not in hidden] + originals}
        original_nodes, original_edges = _inventory(expected)
        nodes, edges = _inventory(display_result)
        if set(nodes) != set(original_nodes) or set(edges) != set(original_edges):
            raise ValueError
        for key, edge in edges.items():
            if any(edge.get(field) != original_edges[key].get(field)
                   for field in ("source", "target", "role", "outpoint", "label", "quantity", "details", "original_source")):
                raise ValueError
        result = dict(display_result)
        result["edges"] = [deepcopy(edges.get(edge["id"], edge)) for edge in canonical_original["edges"]]
        result["context_connectors"] = {"version": canonical_original["context_connectors"]["version"], "summaries": [
            deepcopy(edges[summary["id"]]) for summary in originals]}
        if "activity_frames" in canonical_original:
            result["activity_frames"] = deepcopy(canonical_original["activity_frames"])
        _validated_summaries(result)
        return result
    except (KeyError, TypeError, ValueError, AttributeError):
        raise TraceError(_ERROR) from None
