"""Bounded, path-guided Trace layout requests; no changes to evidence.

The preferred spine stays a placement guide. Every other object has one owner,
including shared addresses. Internal connections and temporary boundary
anchors are sent to a section's ELK worker; helpers are removed afterwards.
No seed-to-endpoint paths are enumerated and no section failure falls back to a
whole-graph solve.
"""

from collections import defaultdict
import math

from .common import TraceError
from .elk_errors import ElkWorkerFailure
from .elk_sections_parallel import iter_sections
from .trace_layout import trace_structure
from .trace_section_geometry import assemble

SECTION_LAYOUT_VERSION = 7
MIN_SECTION_NODES = 600
MIN_SECTION_EDGES = 2400
MAX_SECTION_NODES = 128
MAX_SECTION_PORTS = 1024


def address_neighbors(graph, request, structure):
    """Nominate unpinned, exact two-edge address continuations for row repair.

    This is a layout preference derived from already loaded graph evidence.
    Reused addresses, inferred address-level paths and designated placements
    are excluded. No records are read or changed by this analysis.
    """
    from .named_group_layout import selected_members

    nodes = {node["id"]: node for node in graph["nodes"]}
    children = {node["id"]: node for node in request["children"]}
    protected = set(selected_members(graph))
    for field in ("core", "members", "spine", "excluded_hubs", "shared_hubs"):
        protected.update(structure.get(field, ()))
    layout = graph.get("layout", {})
    protected.update(layout.get("change_outputs", {}).get("locked_nodes", ()))
    protected.update(layout.get("output_alignment", {}).get("outputs", ()))
    hubs = set(graph.get("graph_options", {}).get("hub_addresses", ()))
    designated = {f"{txid}:{item['vout']}"
                  for txid, item in graph.get("service_controls", {}).get("change_outputs", {}).items()
                  if isinstance(item, dict) and type(item.get("vout")) is int}
    incoming, outgoing = defaultdict(list), defaultdict(list)
    for edge in graph["edges"]:
        incoming[edge["target"]].append(edge)
        outgoing[edge["source"]].append(edge)
    result = {}
    for key, node in sorted(nodes.items()):
        details = node.get("details", {})
        if (key not in children or node["kind"] != "address" or key in protected
                or node.get("role") == "seed"
                or details.get("network", "liquid") != "liquid"
                or details.get("address") in hubs
                or any(node.get(field) for field in ("layout_hub", "change_output", "name", "name_colors",
                                                    "attribution_reference", "is_starting"))
                or node.get("starting_transaction_index") is not None
                or details.get("address_attributions")
                or len(incoming[key]) != 1 or len(outgoing[key]) != 1):
            continue
        before, after = incoming[key][0], outgoing[key][0]
        parent, child = before["source"], after["target"]
        if (parent == child or parent not in children or child not in children
                or nodes.get(parent, {}).get("kind") != "transaction"
                or nodes.get(child, {}).get("kind") != "transaction"
                or before.get("change_output") or after.get("change_output")
                or before.get("role") == "seed_output"):
            continue
        outpoint = before.get("outpoint")
        vin = after.get("details", {}).get("vin", {})
        if (not isinstance(outpoint, str) or not outpoint or outpoint != after.get("outpoint")
                or outpoint in designated or before["id"] != "out:" + outpoint
                or not after["id"].startswith("in:" + child.removeprefix("tx:") + ":")
                or not isinstance(vin.get("txid"), str) or type(vin.get("vout")) is not int
                or vin["vout"] < 0 or vin.get("is_pegin") or vin.get("is_coinbase")
                or "tx:" + vin["txid"] != parent or f"{vin['txid']}:{vin['vout']}" != outpoint):
            continue
        try:
            ranks = [float(children[item].get("layoutOptions", {}).get("elk.partitioning.partition", 0))
                     for item in (parent, key, child)]
        except (ValueError, TypeError, OverflowError):
            continue
        if all(math.isfinite(value) for value in ranks) and ranks[0] < ranks[1] < ranks[2]:
            result[key] = [parent, child]
    return result


def enabled(graph, request):
    return (graph.get("graph_options", {}).get("layout_style") == "trace"
            and (len(request["children"]) >= MIN_SECTION_NODES
                 or len(request["edges"]) >= MIN_SECTION_EDGES))


def plan(graph, request, *, structure=None):
    """Partition once by the preferred spine and existing exact-flow branches."""
    structure = trace_structure(graph) if structure is None else structure
    children = {node["id"]: node for node in request["children"]}
    order = request.get("centerNodeOrder", list(children))
    rank = {key: index for index, key in enumerate(order)}
    backbone = set(structure["spine"]) & children.keys()
    groups, assigned = [], set()

    def partition(keys):
        current, ports = [], 0
        for key in sorted((set(keys) & children.keys()) - assigned,
                          key=lambda key: (rank.get(key, len(rank)), key)):
            count = len(children[key].get("ports", []))
            # Reserve two boundary anchors and one matching helper port for
            # every external port; actual worker requests retain these caps.
            if current and (len(current) >= max(1, MAX_SECTION_NODES - 2)
                            or ports + count > MAX_SECTION_PORTS // 2):
                groups.append(current)
                current, ports = [], 0
            current.append(key)
            assigned.add(key)
            ports += count
        if current:
            groups.append(current)

    partition(backbone)
    for branch in structure["branches"]:
        partition(branch)
    # Shared hubs must remain individual junctions, never duplicated into the
    # branches they connect. Isolated/context objects also retain one owner.
    for key in order:
        if key not in assigned:
            partition([key])
    for key in sorted(children.keys() - assigned):
        partition([key])
    if request.get("outputAlignmentPositions"):
        # A board addition explicitly requires equal output ranks across
        # unrelated branches. Singleton envelopes retain that hard constraint;
        # their canonical connections still use the same boundary pipeline.
        groups = [[key] for key in order if key in children]
    owner = {key: index for index, group in enumerate(groups) for key in group}
    port_owner = {port["id"]: node["id"] for node in children.values() for port in node.get("ports", [])}
    internal = defaultdict(list)
    for edge in request["edges"]:
        source, target = (owner[port_owner[edge[field][0]]] for field in ("sources", "targets"))
        if source == target:
            internal[source].append(edge)
    requests = []
    input_orders = request.get("inputPortOrders", {})
    for index, group in enumerate(groups):
        item = {"id": "trace-section", "children": [children[key] for key in group],
                "edges": internal[index], "layoutOptions": dict(request["layoutOptions"]),
                "sectionLayout": True, "branchOrganization": request.get("branchOrganization", 0),
                "centerNodeOrder": group,
                "inputPortOrders": {key: input_orders[key] for key in group if key in input_orders}}
        requests.append(item)
    from .trace_section_local import boundary_requests
    return groups, boundary_requests(request, groups, requests), backbone


def _validate_local(request, candidate):
    """Do not let assembly hide missing objects, ports, routes, or labels."""
    if not isinstance(candidate, dict) or candidate.get("inputOrderPolicy", "geometry") not in ("geometry", "traced_first"):
        raise TraceError("ELK section returned an invalid layout")
    expected = {node["id"]: node for node in request["children"]}
    nodes, edges = candidate.get("nodes"), candidate.get("edges")
    if (not isinstance(nodes, list) or not all(isinstance(node, dict) and isinstance(node.get("id"), str) for node in nodes) or len(nodes) != len(expected)
            or {node.get("id") for node in nodes} != expected.keys()):
        raise TraceError("ELK section changed the graph's objects")
    def number(value):
        try:
            return type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            return False
    def point(value):
        return isinstance(value, dict) and number(value.get("x")) and number(value.get("y"))
    for node in nodes:
        original = expected[node["id"]]
        ports = node.get("ports", [])
        if (not isinstance(ports, list) or not all(isinstance(port, dict) and isinstance(port.get("id"), str) for port in ports)
                or not point(node) or any(node.get(key) != original[key] for key in ("width", "height"))
                or len(ports) != len(original.get("ports", []))
                or {port.get("id") for port in ports} != {port["id"] for port in original.get("ports", [])}
                or not all(point(port) for port in ports)):
            raise TraceError("ELK section returned invalid object geometry or attachments")
    expected_edges = {edge["id"]: edge for edge in request["edges"]}
    if (not isinstance(edges, list) or not all(isinstance(edge, dict) and isinstance(edge.get("id"), str) for edge in edges) or len(edges) != len(expected_edges)
            or {edge.get("id") for edge in edges} != expected_edges.keys()):
        raise TraceError("ELK section changed the graph's connections")
    for edge in edges:
        sections = edge.get("sections")
        if (not isinstance(sections, list) or len(sections) != 1 or not isinstance(sections[0], dict)
                or not isinstance(sections[0].get("bendPoints", []), list)
                or not all(point(p) for p in [sections[0].get("startPoint"),
                        *sections[0].get("bendPoints", []), sections[0].get("endPoint")])):
            raise TraceError("ELK section returned invalid connector geometry")
        labels = edge.get("labels", [])
        wanted = expected_edges[edge["id"]].get("labels", [])
        if (not isinstance(labels, list) or not all(isinstance(label, dict) for label in labels) or len(labels) != len(wanted)
                or any(not point(label) or any(label.get(key) != original[key]
                       for key in ("id", "width", "height")) for label, original in zip(labels, wanted))):
            raise TraceError("ELK section returned invalid connector labels")


def iter_candidates(graph, request, seeds, worker, progress_for_attempt, metadata, *, structure=None):
    # Board updates reserve dummy columns for the monolithic engine. The
    # assembler uses the real dependency columns directly, retaining their
    # alignment while removing those non-evidence spacer objects.
    spacers = set(request.get("outputAlignmentSpacers", []))
    if spacers:
        request = {**request, "children": [node for node in request["children"] if node["id"] not in spacers]}
        for field in ("centerNodeOrder", "branchNodeOrder"):
            if field in request:
                request[field] = [key for key in request[field] if key not in spacers]
    structure = trace_structure(graph) if structure is None else structure
    groups, requests, backbone = plan(graph, request, structure=structure)
    address_hints = address_neighbors(graph, request, structure)
    # Largest bounded job supplies conservative initial memory telemetry. A
    # high-degree singleton needs no ELK solve; all its ports are placed once
    # by the local fallback, without an oversized worker request.
    jobs = sorted((i for i, item in enumerate(requests) if len(groups[i]) > 1 and item["localSection"]["internal_edge_ids"]
                   and not set(groups[i]) <= backbone),
                  key=lambda i: (-sum(len(node.get("ports", [])) for node in requests[i]["children"]),
                                 -len(requests[i]["children"]), i))
    metadata.update(execution="sequential", worker_count=1, memory_retry_count=0,
                    section_layout_version=SECTION_LAYOUT_VERSION, section_count=len(groups),
                    section_worker_count=len(jobs),
                    max_section_nodes=max((len(item["children"]) for item in requests), default=0),
                    max_worker_ports=max((sum(len(n.get("ports", [])) for n in requests[i]["children"])
                                          for i in jobs), default=0))
    for attempt, seed in enumerate(seeds, 1):
        report = progress_for_attempt(attempt, seed)
        report({"phase": "optimizing", "stage": "section_preparing", "completed": 0, "total": 0,
                "message": f"Arranging {len(groups):,} Trace sections around the preferred backbone",
                "section_count": len(groups), "section_worker_count": len(jobs)})
        candidates = [None] * len(groups)
        execution = {}
        failure = None
        stream = iter_sections([requests[index] for index in jobs], seed, worker, report, execution)
        try:
            for index, alternatives in stream:
                if isinstance(alternatives, ElkWorkerFailure):
                    failure = alternatives
                    break
                if not isinstance(alternatives, list) or not alternatives:
                    raise TraceError("ELK section returned no layout")
                for candidate in alternatives:
                    _validate_local(requests[jobs[index - 1]], candidate)
                    if candidate.get("seed") != seed:
                        raise TraceError("ELK section returned an unexpected layout seed")
                candidates[jobs[index - 1]] = alternatives[0]
                report({"phase": "optimizing", "stage": "section_ready", "completed": 0, "total": 0,
                        "message": f"Arranged Trace section {index:,} of {len(jobs):,}",
                        "section_index": index, "section_total": len(jobs)})
        finally:
            stream.close()
        metadata["worker_count"] = max(metadata["worker_count"], execution.get("worker_count", 1))
        metadata["memory_retry_count"] += execution.get("memory_retry_count", 0)
        if execution.get("execution") == "parallel":
            metadata["execution"] = "parallel"
        if execution.get("peak_rss_mb"):
            metadata["peak_rss_mb"] = max(metadata.get("peak_rss_mb", 0), execution["peak_rss_mb"])
        if failure is not None:
            yield attempt, seed, failure
            continue
        report({"phase": "optimizing", "stage": "section_assembling", "completed": 0, "total": 0,
                "message": "Joining Trace sections and routing shared-address connections"})
        from .trace_section_local import local_candidates
        candidates = local_candidates({**request, "addressNeighbors": address_hints}, requests, candidates, backbone, seed,
                                      {node["id"]: node["kind"] for node in graph["nodes"]},
                                      structure["edges"])
        candidate = assemble({**request, "backboneEdges": structure["edges"],
                              "nodeShapes": {node["id"]: node["kind"] for node in graph["nodes"]},
                              "addressNeighbors": address_hints, "localSectionGeometry": True,
                              "sectionRequests": requests},
                             groups, candidates, backbone)
        candidate.update(seed=seed, inputOrderPolicy="geometry", branchProfile="flow_weighted", branchBoundary=False)
        yield attempt, seed, [candidate]
