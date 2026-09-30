"""Reviewed plotting goals over shared, immutable investigation evidence.

Collecting evidence is a separate operation. This module reads verified
main-run archives and saved display/trace controls. Board updates additionally
read the selected Miro board; neither mode fetches transactions or statistics.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import heapq
import html
from pathlib import Path
import re
import uuid

from .api import http
from .common import TraceError, canonical, digest, now, read_json, save_json
from .group_hops import reference_name

GOALS = frozenset({"full", "connections", "pegouts"})
LAYOUT_SETTINGS = frozenset({"include_fees", "group_context_inputs", "hub_addresses",
                             "color_attribution_arrows", "center_name", "connector_style", "layout_attempts"})
PREVIEW_ID = re.compile(r"[0-9a-f]{16}-plots-[0-9a-f]{8}\Z")
FILES = frozenset({"graph.html", "graph.svg", "graph.json", "layout-report.json", "graph.mmd",
                   "transactions.csv", "plot.json", "miro-plan.json", "details.html", "details.json",
                   "SHA256SUMS"})
PEGOUT_CSV_FILES = frozenset({"path-transactions.csv", "trace-endpoints.csv"})
SCOPE = ("Saved-data-only plot. No additional transactions or address statistics were fetched. "
         "Paused, stopped, unconfirmed, unsearched or hop-limited branches may contain further activity. "
         "Use Collect data to extend the evidence, then regenerate this plot.")


def plot_files(directory=None):
    if directory is None:
        return FILES | PEGOUT_CSV_FILES
    report = read_json(_ordinary(Path(directory) / "plot.json"))
    if not isinstance(report, dict):
        raise TraceError("Malformed saved investigation plot")
    if "csv_export_version" not in report:
        return FILES  # Earlier immutable previews keep their original manifest.
    if type(report["csv_export_version"]) is not int or report["csv_export_version"] != 1 or report.get("goal") != "pegouts":
        raise TraceError("Unsupported saved plot CSV exports; regenerate the plot")
    return FILES | PEGOUT_CSV_FILES


def _ordinary(path):
    path = Path(path)
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise TraceError("Plot files cannot contain symbolic links")
    return path


@contextmanager
def _locked(case):
    with (case / "trace.lock").open("a") as trace_lock, (case / "case.lock").open("a") as case_lock:
        try:
            fcntl.flock(trace_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(case_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("Data collection or a settings change is active; retry the plot afterward") from None
        yield


def _settings(metadata):
    from .cli import (attribution_arrow_coloring, branch_hubs, centered_name_group,
                      connector_appearance, context_input_grouping, include_fee_flows, layout_search_attempts)
    from .export import PRESENTATION_VERSION
    return {"include_fees": include_fee_flows(metadata),
            "group_context_inputs": context_input_grouping(metadata),
            "hub_addresses": branch_hubs(metadata),
            "color_attribution_arrows": attribution_arrow_coloring(metadata),
            "center_name": centered_name_group(metadata),
            "connector_style": connector_appearance(metadata),
            "layout_attempts": layout_search_attempts(metadata),
            "presentation_version": PRESENTATION_VERSION}


def validate_layout_settings(value):
    """Return a canonical settings snapshot; never accept extra or coerced fields."""
    from .investigations import validate_settings

    if (not isinstance(value, dict) or set(value) != LAYOUT_SETTINGS | {"presentation_version"}
            or type(value.get("presentation_version")) is not int or value["presentation_version"] < 1):
        raise TraceError("Invalid saved layout settings; regenerate the plot")
    normalized = validate_settings({key: value[key] for key in LAYOUT_SETTINGS})
    result = {key: normalized[key] for key in LAYOUT_SETTINGS}
    result["presentation_version"] = value["presentation_version"]
    if canonical(result) != canonical(value):
        raise TraceError("Saved layout settings are not canonical; regenerate the plot")
    return deepcopy(result)


def _effective_settings(settings, goal, query=None):
    result = validate_layout_settings(settings)
    if goal != "full":
        # Filtered plots never add fee flows or hub branches. Context grouping
        # applies only when a peg-out query explicitly includes local context.
        result.update(include_fees=False, hub_addresses=[])
        if goal != "pegouts" or not (query or {}).get("include_context"):
            result["group_context_inputs"] = False
    return result


def _snapshot_settings(graph):
    report = graph["plot"]
    if "layout_settings" not in report:
        return None  # Older snapshots retain their original strict review.
    settings = validate_layout_settings(report["layout_settings"])
    if (report.get("settings_sha256") != digest(canonical(settings))
            or _effective_settings(settings, report["goal"], report.get("query")) != settings
            or graph.get("presentation_version") != settings["presentation_version"]):
        raise TraceError("Saved layout settings disagree with their snapshot; regenerate the plot")
    options = graph.get("graph_options", {})
    if (not isinstance(options, dict) or any(key not in options or canonical(options[key]) != canonical(settings[key])
                                            for key in LAYOUT_SETTINGS)):
        raise TraceError("Saved layout settings disagree with the graph; regenerate the plot")
    layout = graph.get("layout", {})
    if not isinstance(layout, dict):
        raise TraceError("Saved layout metadata is malformed; regenerate the plot")
    search = layout.get("search", {})
    if (not isinstance(search, dict) or ("attempt_count" in search
            and canonical(search["attempt_count"]) != canonical(settings["layout_attempts"]))):
        raise TraceError("Saved layout settings disagree with the layout search; regenerate the plot")
    return settings


def _snapshot_board(graph, plan):
    """Keep board-aware reviews bound to the selected target and capture."""
    report = graph["plot"]
    mode = report.get("layout_mode")
    if mode is None:
        if "board_layout" in graph or "board_layout" in plan:
            raise TraceError("Saved board layout is missing its mode; regenerate the plot")
        return  # Older immutable plot snapshots keep their original behavior.
    if mode not in ("fresh", "update"):
        raise TraceError("Invalid saved layout mode; regenerate the plot")
    if mode == "fresh":
        if (any(key in report for key in ("board_record_id", "board_id", "board_name"))
                or "board_layout" in graph or "board_layout" in plan):
            raise TraceError("A fresh layout cannot be bound to an existing board")
        return
    if (not isinstance(report.get("board_record_id"), str)
            or not isinstance(report.get("board_id"), str)
            or not isinstance(report.get("board_name"), str)
            or not isinstance(graph.get("board_layout"), dict)
            or plan.get("board_layout") != graph["board_layout"]
            or graph["board_layout"].get("board_id") != report["board_id"]
            or report.get("update_counts") != graph["board_layout"].get("counts", {})):
        raise TraceError("Saved board layout does not match its target; regenerate the plot")


def _source(case, run_id):
    from .address_counts import apply_saved_counts
    from .cli import resolve_latest, run_path, verify_export
    from .investigations import read_case
    from .services import apply_service_labels, load_services
    metadata = read_case(case)
    run_id = resolve_latest(case, run_id)
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{16}", run_id):
        raise TraceError("Choose a saved collection run")
    archive = _ordinary(run_path(case, run_id))
    _ordinary(archive / "SHA256SUMS")
    if (archive / "SHA256SUMS").is_file():
        for line in (archive / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            parts = line.split("  ", 1)
            if len(parts) == 2:
                _ordinary(archive / parts[1])
    verify_export(archive)
    state = read_json(archive / "trace.json")
    if not isinstance(state, dict) or state.get("run_id") != run_id or state.get("case_id") != metadata["case_id"]:
        raise TraceError("The selected collection run does not belong to this investigation")
    controls = {key: value for key, value in load_services(case).items() if key != "history"}
    state["labels"] = apply_service_labels(state["labels"], controls)
    state["service_controls"] = controls
    counts = apply_saved_counts(case, state)
    settings = _settings(metadata)
    fingerprints = {"archive_sha256": digest((archive / "SHA256SUMS").read_bytes()),
                    "service_sha256": digest(canonical(controls)),
                    "settings_sha256": digest(canonical(settings)),
                    "address_counts_sha256": digest(canonical(counts))}
    return state, settings, fingerprints


def _query(goal, state, min_hops, max_hops, *, include_unspent=False, include_unspendable=False,
           include_context=False):
    from .connections import validate_hops
    from .pegout_paths import validate_query
    if not isinstance(goal, str) or goal not in GOALS:
        raise TraceError("Choose the full investigation, starter connections, or peg-out paths plot")
    if type(include_unspent) is not bool or type(include_unspendable) is not bool:
        raise TraceError("Additional endpoint options must be true or false")
    if goal != "pegouts" and (include_unspent or include_unspendable):
        raise TraceError("Additional endpoint options apply only to peg-out paths plots")
    if type(include_context) is not bool:
        raise TraceError("Include context addresses must be true or false")
    if goal != "pegouts" and include_context:
        raise TraceError("Include context addresses applies only to peg-out paths plots")
    if goal == "pegouts":
        return validate_query(seeds=state["seeds"], min_hops=min_hops, max_hops=max_hops,
                              include_unspent=include_unspent, include_unspendable=include_unspendable,
                              include_context=include_context, hop_reference_name=reference_name(state))
    reference = {"hop_reference_name": reference_name(state)} if reference_name(state) else {}
    if goal == "connections":
        return {"max_hops": validate_hops(max_hops), **reference}
    return reference


def _observations(case, run_id):
    """Read retrieval times from the verified archive, without opening a live store."""
    directory = _ordinary(case / "runs" / run_id)
    names = {line.split("  ", 1)[1] for line in (directory / "SHA256SUMS").read_text(encoding="utf-8").splitlines()}
    if "evidence-index.json" not in names:
        return {}  # Some earlier or imported run archives contain only the graph evidence.
    rows = read_json(_ordinary(directory / "evidence-index.json"))
    if not isinstance(rows, list) or any(not isinstance(row, dict) or type(row.get("id")) is not int for row in rows):
        raise TraceError("Malformed saved observation index")
    result = {row["id"]: row for row in rows}
    if len(result) != len(rows):
        raise TraceError("Duplicate observation IDs in the saved index")
    return result


def _graph(state, goal, query, settings):
    from .connections import connection_graph
    from .export import build_graph
    from .pegout_paths import pegout_graph
    from .plot_scope import project_full_scope
    options = {key: settings[key] for key in ("color_attribution_arrows", "center_name")}
    if goal == "connections":
        return connection_graph(state, query["max_hops"], **options)
    if goal == "pegouts":
        return pegout_graph(state, query, group_context_inputs=settings["group_context_inputs"], **options)
    return build_graph(project_full_scope(state), merge_addresses=True, **options,
                       **{key: settings[key] for key in ("include_fees", "group_context_inputs", "hub_addresses")})


def plot_plan(graph):
    """Keep the namespace for board workspaces; empty results create no objects."""
    from .miro import make_plan
    if graph["nodes"]:
        return make_plan(graph)
    plan = {"schema_version": 2, "run_id": graph["run_id"], "shapes": [], "connectors": [],
            "namespace": deepcopy(graph["namespace"]), "run": deepcopy(graph.get("run", {})),
            "graph_options": deepcopy(graph.get("graph_options", {}))}
    if "board_layout" in graph:
        plan["board_layout"] = deepcopy(graph["board_layout"])
    plan["sha256"] = digest(canonical(plan))
    return plan


def _coverage(state):
    maximum = state.get("limits", {}).get("max_hops")
    status = state.get("status")
    reason = state.get("stop_reason")
    text = f"Source collection {state['run_id']}: status {status or 'unknown'}; hop limit {maximum if maximum is not None else 'unknown'}. "
    name = reference_name(state)
    if name:
        text += f"Hops count away from attribution group {name}, resetting at each reached group output. "
    if reason:
        text += f"Stop reason: {reason}. "
    return {"saved_data_only": True, "source_max_hops": maximum, "source_run_status": status,
            **({"hop_reference_name": name} if name else {}),
            "source_stop_reason": reason, "coverage_notice": text + SCOPE}


def _empty_export(graph, destination):
    destination.mkdir(parents=True, exist_ok=False)
    document = ('<!doctype html><html lang="en"><meta charset="utf-8"><title>Investigation plot</title>'
                '<h1>No matching activity in the saved data</h1><p>' + html.escape(graph["notice"]) + '</p></html>')
    paths = {"html": "graph.html", "svg": "graph.svg", "graph": "graph.json",
             "report": "layout-report.json", "details": "details.html", "details_index": "details.json"}
    save_json(destination / "graph.json", graph)
    save_json(destination / "layout-report.json", {"run_id": graph["run_id"], "node_count": 0, "edge_count": 0,
                                                    "notice": graph["notice"]})
    (destination / "graph.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="800" height="100"/>', encoding="utf-8")
    for name in ("graph.html", "details.html"):
        (destination / name).write_text(document, encoding="utf-8")
    save_json(destination / "details.json", {"schema_version": 1, "run_id": graph["run_id"], "activities": [], "pages": []})
    return {key: str(destination / name) for key, name in paths.items()}


def _summary(graph, preview_id, *, reviewable=True, reason=None):
    report = graph["plot"]
    result = {**report, "id": preview_id, "preview_id": preview_id, "notice": graph["notice"],
              "reviewable": reviewable, "empty": not graph["nodes"]}
    if reason:
        result["review_error"] = reason
    return result


def preview_plot(case, goal, run_id="latest", min_hops=0, max_hops=10, *, include_unspent=False,
                 include_unspendable=False, include_context=False, open_browser=False, progress=None,
                 layout_mode="fresh", board_record_id=None, token=None, transport=http,
                 interval=.02, workers=4, _preflight=None):
    """Plot saved evidence afresh, or review additions against a selected live board."""
    from .cli import open_preview
    from .elk_layout import optimize_graph
    from .layout_preview import export_layout
    from .mermaid import mermaid_source
    from .miro import validate_plan
    from .transaction_csv import write_transaction_csv
    if layout_mode not in ("fresh", "update"):
        raise TraceError("Choose a fresh layout or an update to an existing Miro board")
    if (layout_mode == "fresh" and board_record_id is not None
            or layout_mode == "update" and not isinstance(board_record_id, str)):
        raise TraceError("Choose a target Miro board only when preparing an update layout")
    case = _ordinary(case)
    with _locked(case):
        state, settings, fingerprints = _source(case, run_id)
        query = _query(goal, state, min_hops, max_hops, include_unspent=include_unspent,
                       include_unspendable=include_unspendable, include_context=include_context)
        settings = _effective_settings(settings, goal, query)
        graph = _graph(state, goal, query, settings)
        graph["graph_options"].update({key: deepcopy(settings[key]) for key in LAYOUT_SETTINGS})
        board_fields = {"layout_mode": layout_mode}
        if layout_mode == "update":
            from .board_layout import capture, prepare_graph
            from .investigation_boards import board_for_plot, _board_plan, _check_legacy_removals
            record = board_for_plot(case, board_record_id, goal)
            board_plan = _board_plan(plot_plan(graph), record)
            state_path = case / record["state_file"]
            _check_legacy_removals(board_plan, record, state_path)
            if _preflight is not None:
                _preflight(graph, record)
            snapshot = capture(record["board_id"], state_path, board_plan["namespace"],
                               token=token, transport=transport, interval=interval,
                               workers=workers, progress=progress)
            graph = prepare_graph(graph, snapshot, connector_style=settings["connector_style"],
                                  layout_attempts=settings["layout_attempts"], progress=progress)
            board_fields.update(board_record_id=record["id"], board_id=record["board_id"],
                                board_name=record["name"],
                                update_counts=deepcopy(graph["board_layout"].get("counts", {})))
        else:
            if _preflight is not None:
                _preflight(graph, None)
            if graph["nodes"]:
                graph = optimize_graph(graph, connector_style=settings["connector_style"],
                                       layout_attempts=settings["layout_attempts"], progress=progress)
        coverage = _coverage(state)
        graph["notice"] = coverage["coverage_notice"] + " " + graph["notice"]
        report = {"schema_version": 1, "case_id": state["case_id"], "run_id": state["run_id"],
                  "goal": goal, "query": query, "created_at": now(), **coverage, **fingerprints, **board_fields,
                  "layout_settings": deepcopy(settings), "settings_sha256": digest(canonical(settings)),
                  "min_hops": query.get("min_hops", 0), "max_hops": query.get("max_hops"),
                  "node_count": len(graph["nodes"]), "edge_count": len(graph["edges"]),
                  "transaction_count": sum(node["kind"] == "transaction" for node in graph["nodes"]),
                  "status": "plotted" if graph["nodes"] else "empty"}
        if goal == "connections":
            report.update(connection_count=graph["connections"]["connection_count"], status=graph["connections"]["status"])
        elif goal == "pegouts":
            report.update(match_count=graph["pegouts"]["match_count"], status=graph["pegouts"]["status"])
            report["csv_export_version"] = 1
            for key in ("endpoint_count", "endpoint_counts", "context_edge_count"):
                if key in graph["pegouts"]:
                    report[key] = deepcopy(graph["pegouts"][key])
        graph["plot"] = report
        destination = _ordinary(case / "previews" / (state["run_id"] + "-plots-" + uuid.uuid4().hex[:8]))
        if progress:
            progress({"phase": "exporting_plot", "completed": 0, "total": 1})
        result = export_layout(graph, destination) if graph["nodes"] else _empty_export(graph, destination)
        try:
            plan = plot_plan(graph)
            validate_plan(plan)
            save_json(destination / "miro-plan.json", plan)
            save_json(destination / "plot.json", report)
            (destination / "graph.mmd").write_text(mermaid_source(graph) if graph["nodes"] else
                "flowchart LR\n  %% No matching activity in saved collection data.\n", encoding="utf-8")
            write_transaction_csv(destination / "transactions.csv", graph, state)
            if goal == "pegouts":
                from .pegout_csv import write_pegout_csvs
                write_pegout_csvs(destination, graph, state, observations=_observations(case, state["run_id"]))
            manifest = "".join(digest((destination / name).read_bytes()) + "  " + name + "\n"
                               for name in sorted(plot_files(destination) - {"SHA256SUMS"}))
            (destination / "SHA256SUMS.tmp").write_text(manifest, encoding="utf-8")
            (destination / "SHA256SUMS.tmp").replace(destination / "SHA256SUMS")
        except BaseException:
            (destination / "SHA256SUMS").unlink(missing_ok=True)
            raise
        if progress:
            progress({"phase": "exporting_plot", "completed": 1, "total": 1})
        return {**_summary(graph, destination.name), **result, "directory": str(destination.resolve()),
                "browser_opened": open_preview(result["html"]) if open_browser else False}


def _snapshot(case, preview_id):
    if not isinstance(preview_id, str) or not PREVIEW_ID.fullmatch(preview_id):
        raise TraceError("Choose a saved investigation plot")
    directory = _ordinary(case / "previews" / preview_id)
    files = plot_files(directory)
    for name in files:
        if not _ordinary(directory / name).is_file():
            raise TraceError("The saved plot is incomplete; regenerate it")
    seen = set()
    for line in (directory / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if (len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0])
                or parts[1] not in files - {"SHA256SUMS"} or parts[1] in seen):
            raise TraceError("Invalid plot manifest")
        checksum, name = parts
        if digest((directory / name).read_bytes()) != checksum:
            raise TraceError("Saved plot changed; regenerate and review it")
        seen.add(name)
    if seen != files - {"SHA256SUMS"}:
        raise TraceError("The saved plot manifest is incomplete")
    from .investigations import read_case
    from .miro import validate_plan
    graph, plan = read_json(directory / "graph.json"), read_json(directory / "miro-plan.json")
    if (not isinstance(graph, dict) or not isinstance(plan, dict)
            or not isinstance(graph.get("plot"), dict) or not isinstance(graph.get("namespace"), dict)
            or not isinstance(graph.get("nodes"), list) or not isinstance(graph.get("edges"), list)):
        raise TraceError("Malformed saved investigation plot")
    report = graph["plot"]
    if (report.get("schema_version") != 1 or not isinstance(report.get("goal"), str)
            or report["goal"] not in GOALS or not isinstance(report.get("query"), dict)
            or report.get("case_id") != read_case(case)["case_id"]
            or graph.get("namespace", {}).get("case_id") != report.get("case_id")
            or report.get("run_id") != preview_id[:16] or graph.get("run_id") != report.get("run_id")
            or plan.get("run_id") != graph["run_id"] or read_json(directory / "plot.json") != report
            or report.get("node_count") != len(graph["nodes"]) or report.get("edge_count") != len(graph["edges"])):
        raise TraceError("Saved plot does not match this investigation")
    name = reference_name(report)
    if name != reference_name(report["query"]) or name != reference_name(graph):
        raise TraceError("Saved plot hop reference disagrees with its graph; regenerate the plot")
    validate_plan(plan)
    if plot_plan(graph) != plan:
        raise TraceError("Saved plot and its Miro plan disagree")
    if report["goal"] == "pegouts":
        query = report["query"]
        pegouts = graph.get("pegouts", {})
        if (canonical(graph.get("graph_options", {}).get("pegout_query")) != canonical(query)
                or canonical(pegouts.get("query")) != canonical(query)
                or any(canonical(report.get(key)) != canonical(pegouts.get(key))
                       for key in ("match_count", "endpoint_count", "endpoint_counts", "context_edge_count", "status"))):
            raise TraceError("Saved endpoint options disagree with the graph; regenerate the plot")
    _snapshot_settings(graph)
    _snapshot_board(graph, plan)
    return graph, plan


def _review_source(case, graph, source_cache=None):
    report = graph["plot"]
    run_id = report["run_id"]
    if source_cache is not None and run_id in source_cache:
        state, fingerprints = source_cache[run_id]
    else:
        state, _, fingerprints = _source(case, run_id)
        if source_cache is not None:
            # Several goals commonly share one large archive. Verify its bytes
            # once per listing, retaining only the seed/source identity here.
            state = {key: state[key] for key in ("seeds", "source", "hop_reference_name") if key in state}
            source_cache[run_id] = state, fingerprints
    query = report.get("query", {})
    expected = _query(report["goal"], state, query.get("min_hops", 0), query.get("max_hops", 10),
                      include_unspent=query.get("include_unspent", False),
                      include_unspendable=query.get("include_unspendable", False),
                      include_context=query.get("include_context", False))
    settings = _snapshot_settings(graph)
    if settings is not None:
        from .export import PRESENTATION_VERSION
        if settings["presentation_version"] != PRESENTATION_VERSION:
            raise TraceError("Saved layout uses a different presentation version; regenerate the plot")
    if (any(report.get(key) != value for key, value in fingerprints.items()
            if settings is None or key != "settings_sha256")
            or query != expected or graph["namespace"].get("source") != state["source"]):
        raise TraceError("Evidence, address counts, trace controls or plot settings changed; regenerate the plot")


def reviewed_plot(case, preview_id):
    """Verify archived bytes, current evidence controls and frozen layout settings."""
    case = _ordinary(case)
    with _locked(case):
        graph, plan = _snapshot(case, preview_id)
        _review_source(case, graph)
        return graph, plan


def list_plots(case):
    """List recent intact layouts, keeping stale evidence visible for refresh."""
    case = _ordinary(case)
    recent = heapq.nlargest(100, (path for path in (case / "previews").glob("*-plots-*")
        if PREVIEW_ID.fullmatch(path.name) and not path.is_symlink()), key=lambda path: path.stat().st_mtime_ns)
    result, source_cache = [], {}
    with _locked(case):
        for directory in recent:
            try:
                graph, _ = _snapshot(case, directory.name)
            except (TraceError, OSError, ValueError, TypeError, KeyError):
                continue
            reason = None
            try:
                _review_source(case, graph, source_cache)
            except (TraceError, OSError, ValueError, TypeError, KeyError) as error:
                reason = str(error)
            result.append(_summary(graph, directory.name, reviewable=reason is None, reason=reason))
    return sorted(result, key=lambda value: (value["created_at"], value["id"]), reverse=True)
