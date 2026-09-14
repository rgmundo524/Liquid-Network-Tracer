"""Bounded starter-to-starter paths, derived only from verified saved UTXO spends.

The directed transaction DAG is the search graph. Address equality, co-inputs,
shared descendants and display-layout edges cannot manufacture a connection.
"""
from collections import defaultdict, deque
from copy import deepcopy
from pathlib import Path
import re
import html
import fcntl
import uuid

from .common import TraceError, canonical, digest, match_labels, output_kind, parse_outpoint, read_json, save_json

PREVIEW_ID = re.compile(r"[0-9a-f]{16}-connections-[0-9a-f]{8}\Z")
FILES = frozenset({"graph.html", "graph.svg", "graph.json", "layout-report.json", "graph.mmd",
                   "nodes.csv", "edges.csv", "connections.json", "miro-plan.json", "SHA256SUMS"})
SCOPE = ("Search scope: verified spends in the selected saved run, from the selected starting outputs. "
         "This view does not fetch additional transactions. Unsearched, paused, stopped or hop-limited "
         "branches may contain undiscovered connections; no result is not proof of no connection.")


def validate_hops(value):
    if type(value) is not int or not 0 <= value <= 2147483647:
        raise TraceError("Connection hops must be a whole number from 0 to 2147483647")
    return value


def connecting_outpoints(state, max_hops=10):
    """Union of every edge on any directed path between distinct starters <= H.

    Two bounded BFS passes per starter suffice. In a DAG, an edge u->v belongs
    iff distance(start,u) + 1 + distance(v,any OTHER starter) <= H. This retains
    alternate routes, not just shortest paths, without enumerating them. Each
    source's first edge must spend an explicitly selected seed output. Passing
    another starter never resets that source's distance.
    """
    limit = validate_hops(max_hops)
    seeds = set(state["seeds"])
    roots = sorted({parse_outpoint(key)[0] for key in seeds})
    if len(roots) < 2:
        raise TraceError("Choose outputs from at least two distinct starting transactions")
    transactions, outputs = state["transactions"], state["outputs"]
    forward, backward = defaultdict(list), defaultdict(list)
    verified = []
    indegree = {key: 0 for key in transactions}
    try:
        for key, link in sorted(state["links"].items()):
            parent, index = parse_outpoint(key)
            child, vin = link["spending_txid"], link["vin"]
            tracked = outputs[key]
            funding, spending = transactions[parent]["data"], transactions[child]["data"]
            if (funding.get("txid") != parent or spending.get("txid") != child
                    or type(tracked["vout"]) is not int or tracked["txid"] != parent
                    or tracked["vout"] != index or index >= len(funding["vout"])
                    or type(vin) is not int or not 0 <= vin < len(spending["vin"])):
                raise TraceError("Connection search requires exact saved output and input indices")
            actual = spending["vin"][vin]
            if (actual.get("is_pegin") or actual.get("is_coinbase")
                    or actual.get("txid") != parent or type(actual.get("vout")) is not int
                    or actual["vout"] != index or parent == child):
                raise TraceError("Connection spend link disagrees with its saved transaction input")
            output = funding["vout"][index]
            if output_kind(output) != "spendable":
                raise TraceError("Connection spend link references a non-spendable output")
            # Validate topology even for a link later excluded by a stop rule.
            verified.append((parent, child, key, vin))
            indegree[child] += 1
            forward[parent].append((child, key))
        ready = deque(key for key, count in indegree.items() if not count)
        visited = 0
        while ready:
            parent = ready.popleft(); visited += 1
            for child, _ in forward[parent]:
                indegree[child] -= 1
                if not indegree[child]: ready.append(child)
        if visited != len(transactions):
            raise TraceError("Saved UTXO spends contain a cycle; connection search cannot proceed")
        forward.clear()
        eligible = []
        for parent, child, key, vin in verified:
            output = transactions[parent]["data"]["vout"][parse_outpoint(key)[1]]
            if any(label.get("stop") is True for label in match_labels(state["labels"], key, output)):
                continue
            if not state.get("include_unconfirmed", False) and any(
                    transactions[txid]["data"].get("status", {}).get("confirmed") is not True
                    for txid in (parent, child)):
                continue
            eligible.append((parent, child, key, vin))
            forward[parent].append((child, key))
            backward[child].append((parent, key))
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise TraceError("Connection search needs complete, consistent saved spend evidence") from exc

    def distances(starts, adjacency, source=None):
        distance = {key: 0 for key in starts}
        queue = deque(sorted(distance))
        while queue:
            node = queue.popleft()
            if distance[node] >= limit: continue
            for other, outpoint in adjacency.get(node, ()):
                if node == source and outpoint not in seeds: continue
                if other not in distance:
                    distance[other] = distance[node] + 1
                    queue.append(other)
        return distance

    retained, pairs = set(), []
    for root in roots:
        downstream = distances([root], forward, root)
        targets = [target for target in roots if target != root and target in downstream]
        if not targets: continue
        upstream = distances(targets, backward)
        for parent, child, key, _ in eligible:
            if parent == root and key not in seeds: continue
            if (parent in downstream and child in upstream
                    and downstream[parent] + 1 + upstream[child] <= limit):
                retained.add(key)
        pairs.extend({"source": root, "target": target, "shortest_hops": downstream[target]} for target in targets)
    return {"schema_version": 1, "max_hops": limit, "starting_transactions": roots,
            "pairs": pairs, "outpoints": sorted(retained), "scope": SCOPE,
            "source_run_status": state.get("status"), "source_stop_reason": state.get("stop_reason"),
            "source_max_hops": state.get("limits", {}).get("max_hops"),
            "status": "connections_found" if retained else "no_connection_found"}


def connection_graph(state, max_hops=10):
    """Generate only connecting transaction/UTXO nodes; keep the archive intact."""
    from .export import build_graph
    from .layout import arrange
    from .miro_frames import activity_frames

    report = connecting_outpoints(state, max_hops)
    outpoints = set(report["outpoints"])
    selected = {key.rpartition(":")[0] for key in outpoints}
    selected.update(state["links"][key]["spending_txid"] for key in outpoints)
    reduced = deepcopy(state)
    reduced["transactions"] = {k: v for k, v in reduced["transactions"].items() if k in selected}
    reduced["outputs"] = {k: v for k, v in reduced["outputs"].items() if k in outpoints}
    reduced["links"] = {k: v for k, v in reduced["links"].items() if k in outpoints}
    # A separate circle per outpoint avoids visual cross-spends between different
    # UTXOs at a reused address. The ordinary merged-address graph is unchanged.
    graph = build_graph(reduced, merge_addresses=False, include_fees=False)
    edge_ids = {"out:" + key for key in outpoints}
    edge_ids.update(f"in:{state['links'][key]['spending_txid']}:{state['links'][key]['vin']}" for key in outpoints)
    graph["edges"] = [e for e in graph["edges"] if e["id"] in edge_ids]
    keep_nodes = {e[field] for e in graph["edges"] for field in ("source", "target")}
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] in keep_nodes]
    graph["fee_items"] = {}
    graph["layout"] = arrange({n["id"]: n for n in graph["nodes"]}, graph["edges"], reduced["transactions"], {})
    graph["activity_frames"] = activity_frames(graph)
    report.update(transaction_count=len(selected), connection_count=len(report["pairs"]))
    graph["connections"] = report
    graph["graph_options"].update(view="starter_connections", connection_hops=max_hops)
    graph["notice"] = ((f"Starter-to-starter paths of at most {max_hops} transaction hops. "
                         f"{len(report['pairs'])} ordered starter pair(s) connected. " if outpoints else
                         f"No connection found within {max_hops} hops in the saved searched data. Nothing is plotted. ")
                        + SCOPE + " One circle per connecting UTXO, not address clustering. "
                        "Every displayed edge belongs to a qualifying path; their union may also form longer routes. "
                        "UTXO reachability does not prove ownership or allocate confidential values.")
    return graph


def connection_plan(graph):
    from .miro import make_plan
    if graph["nodes"]:
        snapshot = deepcopy(graph)
        snapshot.pop("namespace", None)  # A filtered snapshot cannot enter cumulative sync.
        return make_plan(snapshot)
    plan = {"schema_version": 1, "run_id": graph["run_id"], "shapes": [], "connectors": []}
    plan["sha256"] = digest(canonical(plan))
    return plan


def preview_connections(case, run_id="latest", max_hops=10, *, open_browser=False, progress=None):
    from .cli import resolve_latest, run_path, verify_export, connector_appearance, open_preview
    from .investigations import read_case
    from .services import apply_service_labels, load_services
    from .elk_layout import optimize_graph
    from .layout_preview import export_layout
    from .mermaid import mermaid_source
    from .miro import make_plan, validate_plan
    from .export import write_csv, node_csv_rows, NODE_CSV_FIELDS

    validate_hops(max_hops)
    case = Path(case)
    metadata = read_case(case)
    run_id = resolve_latest(case, run_id)
    archive = run_path(case, run_id)
    verify_export(archive)
    state = read_json(archive / "trace.json")
    if state.get("case_id") != metadata["case_id"] or state.get("run_id") != run_id:
        raise TraceError("The selected run does not belong to this investigation")
    settings = load_services(case)
    state["labels"] = apply_service_labels(state["labels"], settings)
    state["service_controls"] = {k: v for k, v in settings.items() if k != "history"}
    graph = connection_graph(state, max_hops)
    if graph["nodes"]:
        graph = optimize_graph(graph, connector_style=connector_appearance(metadata), progress=progress)
    report = graph["connections"]
    report["service_sha256"] = digest(canonical({k: v for k, v in settings.items() if k != "history"}))
    report["archive_sha256"] = digest((archive / "SHA256SUMS").read_bytes())
    destination = case / "previews" / (run_id + "-connections-" + uuid.uuid4().hex[:8])
    if graph["nodes"]:
        result = export_layout(graph, destination)
    else:
        # No placeholder vertices, legend shapes or layout call for no matches.
        destination.mkdir(parents=True, exist_ok=False)
        save_json(destination / "graph.json", graph)
        save_json(destination / "layout-report.json", {"run_id": run_id, "node_count": 0, "edge_count": 0})
        (destination / "graph.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="800" height="100"/>', encoding="utf-8")
        (destination / "graph.html").write_text('<!doctype html><html lang="en"><meta charset="utf-8"><title>Starter connections</title>'
            '<h1>No connection found</h1><p>' + html.escape(graph["notice"]) + '</p></html>', encoding="utf-8")
        result = {"html": str(destination / "graph.html"), "svg": str(destination / "graph.svg")}
    try:
        plan = connection_plan(graph)
        validate_plan(plan)
        save_json(destination / "miro-plan.json", plan)
        save_json(destination / "connections.json", report)
        (destination / "graph.mmd").write_text(mermaid_source(graph) if graph["nodes"] else "flowchart LR\n  %% No connection found in saved searched data.\n", encoding="utf-8")
        write_csv(destination / "nodes.csv", node_csv_rows(graph), NODE_CSV_FIELDS)
        write_csv(destination / "edges.csv", graph["edges"], ["id", "source", "target", "role", "outpoint", "label", "quantity", "details"])
        manifest = "".join(digest((destination / name).read_bytes()) + "  " + name + "\n" for name in sorted(FILES - {"SHA256SUMS"}))
        (destination / "SHA256SUMS").write_text(manifest, encoding="utf-8")
    except BaseException:
        (destination / "SHA256SUMS").unlink(missing_ok=True)
        raise
    return {**result, "directory": str(destination.resolve()), "preview_id": destination.name,
            "run_id": run_id, "include_fees": False, "max_hops": max_hops,
            "connection_count": report["connection_count"], "transaction_count": report["transaction_count"],
            "status": report["status"], "notice": graph["notice"],
            "browser_opened": open_preview(result["html"]) if open_browser else False}


def reviewed_connections(case, preview_id):
    from .investigations import read_case
    from .miro import validate_plan
    case = Path(case)
    if not isinstance(preview_id, str) or not PREVIEW_ID.fullmatch(preview_id):
        raise TraceError("Choose a saved starter-connections preview")
    directory = case / "previews" / preview_id
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise TraceError("Connection previews cannot contain symbolic links")
    for name in FILES:
        if (directory / name).is_symlink() or not (directory / name).is_file():
            raise TraceError("The connection preview is incomplete; regenerate it")
    seen = set()
    for line in (directory / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        fields = line.split("  ", 1)
        if len(fields) != 2 or fields[1] not in FILES - {"SHA256SUMS"} or fields[1] in seen:
            raise TraceError("Invalid connection-preview manifest")
        checksum, name = fields
        if digest((directory / name).read_bytes()) != checksum:
            raise TraceError("Connection preview changed; regenerate and review it")
        seen.add(name)
    if seen != FILES - {"SHA256SUMS"}:
        raise TraceError("Connection-preview manifest is incomplete")
    graph, plan = read_json(directory / "graph.json"), read_json(directory / "miro-plan.json")
    if (graph.get("namespace", {}).get("case_id") != read_case(case)["case_id"]
            or graph.get("run_id") != preview_id[:16] or plan.get("run_id") != graph["run_id"]
            or graph.get("graph_options", {}).get("view") != "starter_connections"
            or plan.get("schema_version") != 1):
        raise TraceError("Connection preview does not match this investigation")
    from .cli import run_path, verify_export
    from .services import load_services
    from .miro import make_plan
    archive = run_path(case, graph["run_id"])
    verify_export(archive)
    settings = load_services(case)
    report = graph.get("connections", {})
    if (report.get("archive_sha256") != digest((archive / "SHA256SUMS").read_bytes())
            or report.get("service_sha256") != digest(canonical({k: v for k, v in settings.items() if k != "history"}))):
        raise TraceError("Source evidence, colors or stop rules changed; regenerate the connection preview")
    validate_plan(plan)
    if connection_plan(graph) != plan or read_json(directory / "connections.json") != report:
        raise TraceError("Connection preview and its publication plan disagree")
    return graph, plan


def publish_connections(case, preview_id, board, *, max_items=750, **kwargs):
    """Keep reviewed stops/colors and board designation stable during publication."""
    if type(max_items) is not int or max_items < 0:
        raise TraceError("The Miro item budget must be a nonnegative whole number")
    case = Path(case)
    with (case / "trace.lock").open("a") as trace_lock, (case / "case.lock").open("a") as case_lock:
        try:
            fcntl.flock(trace_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(case_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("A trace or settings change is active; publish after it finishes") from None
        return _publish_connections(case, preview_id, board, max_items=max_items, **kwargs)


def _publish_connections(case, preview_id, board, *, max_items=750, **kwargs):
    """Publish a reviewed immutable snapshot, never replace the full trace board."""
    from .cli import board_id
    from .investigations import read_case
    from .miro import publish
    graph, plan = reviewed_connections(case, preview_id)
    if not graph["nodes"]:
        return {"status": "no_connection_found", "items": 0, "run_id": graph["run_id"]}
    target = board_id(board)
    case = Path(case)
    metadata = read_case(case)
    if metadata.get("miro_board") and target == board_id(metadata["miro_board"]):
        raise TraceError("Choose a separate Miro board for the connection-only snapshot; the full trace board is protected")
    key = digest(target.encode())[:24]
    if (case / "miro" / (key + ".json")).exists():
        raise TraceError("This Miro board already has a full-trace mapping; choose a separate board")
    return publish(plan, target, case / "miro" / ("connections-" + key + ".json"), max_items=max_items, **kwargs)
