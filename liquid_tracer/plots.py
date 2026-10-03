"""Reviewed plotting goals over shared, immutable investigation evidence.

Collecting evidence is a separate operation. This module reads verified
main-run archives and saved display/trace controls. Board updates additionally
read the selected Miro board; neither mode fetches transactions or statistics.
"""
from contextlib import ExitStack, contextmanager
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
LEGACY_LAYOUT_SETTINGS = frozenset({"include_fees", "group_context_inputs", "hub_addresses",
                                    "color_attribution_arrows", "center_name", "connector_style", "layout_attempts"})
LAYOUT_SETTINGS = LEGACY_LAYOUT_SETTINGS | {"layout_style"}
PREVIEW_ID = re.compile(r"[0-9a-f]{16}-plots-[0-9a-f]{8}\Z")
FILES = frozenset({"graph.html", "graph.svg", "graph.json", "layout-report.json", "graph.mmd",
                   "transactions.csv", "plot.json", "miro-plan.json", "details.html", "details.json",
                   "SHA256SUMS"})
PEGOUT_CSV_FILES = frozenset({"path-transactions.csv", "trace-endpoints.csv"})
INPUT_SNAPSHOT_FILES = frozenset({"inputs.json"})
SCOPE = ("Saved-data-only plot. No additional transactions or address statistics were fetched. "
         "Paused, stopped, unconfirmed, unsearched or hop-limited branches may contain further activity. "
         "Use Collect data to extend the evidence, then regenerate this plot.")


def plot_files(directory=None):
    if directory is None:
        return FILES | PEGOUT_CSV_FILES | INPUT_SNAPSHOT_FILES
    report = read_json(_ordinary(Path(directory) / "plot.json"))
    if not isinstance(report, dict):
        raise TraceError("Malformed saved investigation plot")
    files = FILES
    if "input_snapshot_version" in report:
        if type(report["input_snapshot_version"]) is not int or report["input_snapshot_version"] != 1:
            raise TraceError("Unsupported saved plot input snapshot; regenerate the plot")
        files |= INPUT_SNAPSHOT_FILES
    if "csv_export_version" in report:
        if type(report["csv_export_version"]) is not int or report["csv_export_version"] != 1 or report.get("goal") != "pegouts":
            raise TraceError("Unsupported saved plot CSV exports; regenerate the plot")
        files |= PEGOUT_CSV_FILES
    from .layout_overview import navigation_files
    return files | navigation_files(directory)  # Earlier immutable previews retain their manifest.


def _ordinary(path):
    path = Path(path)
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise TraceError("Plot files cannot contain symbolic links")
    return path


@contextmanager
def _locked(case):
    """Capture atomic preferences while collection continues in its own archive."""
    with (case / "case.lock").open("a") as case_lock:
        try:
            fcntl.flock(case_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("Investigation settings are being saved; retry the plot") from None
        yield


def _settings(metadata):
    from .cli import (attribution_arrow_coloring, branch_hubs, centered_name_group,
                      connector_appearance, context_input_grouping, include_fee_flows, layout_search_attempts)
    from .export import PRESENTATION_VERSION
    result = {"include_fees": include_fee_flows(metadata),
            "group_context_inputs": context_input_grouping(metadata),
            "hub_addresses": branch_hubs(metadata),
            "color_attribution_arrows": attribution_arrow_coloring(metadata),
            "center_name": centered_name_group(metadata),
            "connector_style": connector_appearance(metadata),
            "layout_attempts": layout_search_attempts(metadata),
            "presentation_version": PRESENTATION_VERSION}
    # A missing style is the historical standard layout. Do not add a field to
    # legacy fingerprints: immutable plots keep the exact settings they captured.
    defaults = metadata.get("run_defaults", {})
    if "layout_style" in defaults:
        from .investigations import validate_settings
        result["layout_style"] = validate_settings({"layout_style": defaults["layout_style"]})["layout_style"]
    return result


def validate_layout_settings(value):
    """Return a canonical settings snapshot; never accept extra or coerced fields."""
    from .investigations import validate_settings

    if (not isinstance(value, dict) or set(value) not in (LEGACY_LAYOUT_SETTINGS | {"presentation_version"},
                                                        LAYOUT_SETTINGS | {"presentation_version"})
            or type(value.get("presentation_version")) is not int or value["presentation_version"] < 1):
        raise TraceError("Invalid saved layout settings; regenerate the plot")
    captured_keys = LAYOUT_SETTINGS.intersection(value)
    normalized = validate_settings({key: value[key] for key in captured_keys})
    result = {key: normalized[key] for key in captured_keys}
    result["presentation_version"] = value["presentation_version"]
    if canonical(result) != canonical(value):
        raise TraceError("Saved layout settings are not canonical; regenerate the plot")
    return deepcopy(result)


def _effective_settings(settings, goal, query=None):
    result = validate_layout_settings(settings)
    if goal != "full":
        # New focused plots select transactions, then display their complete
        # local I/O. Legacy snapshots retain their original optional context.
        complete = goal in ("pegouts", "connections") and (query or {}).get("transaction_io") == "complete"
        # Hubs only influence placement after peg-out path selection. Saved
        # snapshots with an empty list keep their original layout settings.
        if goal == "connections":
            result["hub_addresses"] = []
        # Complete peg-out context honors the captured fee display setting.
        # Starter connections and legacy path-only snapshots keep their policy.
        if goal == "connections" or not complete:
            result["include_fees"] = complete
        if not complete and not (goal == "pegouts" and (query or {}).get("include_context")):
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
                                            for key in LAYOUT_SETTINGS.intersection(settings))
            or options.get("layout_style", "standard") != settings.get("layout_style", "standard")):
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


def _archive_source(case, run_id, metadata=None, *, progress=None):
    """Verify only immutable collection bytes, without reading mutable inputs."""
    from .cli import run_path, verify_export
    from .investigations import read_case
    metadata = metadata or read_case(case)
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{16}", run_id):
        raise TraceError("Choose a saved collection run")
    archive = _ordinary(run_path(case, run_id))
    _ordinary(archive / "SHA256SUMS")
    if (archive / "SHA256SUMS").is_file():
        for line in (archive / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            parts = line.split("  ", 1)
            if len(parts) == 2:
                _ordinary(archive / parts[1])
    from .progress import report_progress
    verify_export(archive, progress=progress)
    report_progress(progress, "loading_collection", 0, 1)
    state = read_json(archive / "trace.json")
    report_progress(progress, "loading_collection", 1, 1)
    if not isinstance(state, dict) or state.get("run_id") != run_id or state.get("case_id") != metadata["case_id"]:
        raise TraceError("The selected collection run does not belong to this investigation")
    return state, digest((archive / "SHA256SUMS").read_bytes())


def _source(case, run_id, *, progress=None, prepared=None):
    from .address_counts import apply_saved_counts
    from .cli import resolve_latest
    from .investigations import read_case
    from .services import apply_service_labels, load_services
    # Collection checkpoints and latest-run publication do not mutate older
    # run archives. Hold only the metadata lock while choosing one saved run
    # and one complete CSV/settings revision, then release it before I/O/ELK.
    with _locked(case):
        metadata = read_case(case)
        run_id = resolve_latest(case, run_id)
        controls = {key: value for key, value in load_services(case).items() if key != "history"}
        settings = _settings(metadata)
    state, archive_sha256 = (prepared if prepared is not None else
                             _archive_source(case, run_id, metadata, progress=progress))
    state["labels"] = apply_service_labels(state["labels"], controls)
    state["service_controls"] = controls
    counts = apply_saved_counts(case, state)
    fingerprints = {"archive_sha256": archive_sha256,
                    "service_sha256": digest(canonical(controls)),
                    "settings_sha256": digest(canonical(settings)),
                    "address_counts_sha256": digest(canonical(counts))}
    return state, settings, fingerprints


def _query(goal, state, min_hops, max_hops, *, include_unspent=False, include_unspendable=False,
           include_context=False, transaction_io="complete", attribution_hop_limits="ignore",
           connection_scope="all_saved", pegout_lbtc_limit=None):
    from .connections import validate_hops, validate_connection_scope
    from .pegout_paths import validate_query
    if not isinstance(goal, str) or goal not in GOALS:
        raise TraceError("Choose the full investigation, starter connections, or peg-out paths plot")
    if goal != "connections" and connection_scope not in (None, "all_saved"):
        raise TraceError("Connection scope applies only to Starter connections")
    if type(include_unspent) is not bool or type(include_unspendable) is not bool:
        raise TraceError("Additional endpoint options must be true or false")
    if goal != "pegouts" and (include_unspent or include_unspendable):
        raise TraceError("Additional endpoint options apply only to peg-out paths plots")
    if type(include_context) is not bool:
        raise TraceError("Include context addresses must be true or false")
    if goal != "pegouts" and include_context:
        raise TraceError("Include context addresses applies only to peg-out paths plots")
    if goal != "pegouts" and pegout_lbtc_limit is not None:
        raise TraceError("A cumulative L-BTC limit applies only to peg-out paths plots")
    if goal == "pegouts":
        return validate_query(seeds=state["seeds"], min_hops=min_hops, max_hops=max_hops,
                              include_unspent=include_unspent, include_unspendable=include_unspendable,
                              include_context=include_context, hop_reference_name=reference_name(state),
                              transaction_io=transaction_io, attribution_hop_limits=attribution_hop_limits,
                              pegout_lbtc_limit=pegout_lbtc_limit)
    reference = {"hop_reference_name": reference_name(state)} if reference_name(state) else {}
    if goal == "connections":
        if transaction_io not in (None, "complete"):
            raise TraceError("Starter connection transaction inputs and outputs must be complete")
        io = {"transaction_io": transaction_io} if transaction_io is not None else {}
        scope = validate_connection_scope(connection_scope)
        if scope == "all_saved":
            if max_hops is not None:
                validate_hops(max_hops)  # Old clients may supply an unused, valid hop value.
            return {"connection_scope": "all_saved", **io, **reference}
        if scope == "hop_limited":
            return {"connection_scope": scope, "max_hops": validate_hops(max_hops), **io, **reference}
        return {"max_hops": validate_hops(max_hops), **io, **reference}
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


def _graph(state, goal, query, settings, *, initial_layout=True):
    from .connections import connection_graph
    from .export import build_graph
    from .pegout_paths import pegout_graph
    from .plot_scope import project_full_scope
    options = {key: settings[key] for key in ("color_attribution_arrows", "center_name")}
    options["initial_layout"] = initial_layout
    if goal == "connections":
        return connection_graph(state, query.get("max_hops"), connection_scope=query.get("connection_scope"),
                                transaction_io=query.get("transaction_io"),
                                group_context_inputs=settings["group_context_inputs"], **options)
    if goal == "pegouts":
        return pegout_graph(state, query, group_context_inputs=settings["group_context_inputs"],
                            include_fees=settings["include_fees"], hub_addresses=settings["hub_addresses"], **options)
    return build_graph(project_full_scope(state), merge_addresses=True, **options,
                       resolve_saved_inputs=state.get("collection_source", {}).get("kind") == "shared",
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
    selection = state.get("collection_source", {}).get("selection", {})
    if selection.get("connection_scope") is not None:
        text += "This saved projection contains starter-connection paths only. "
    if selection.get("max_hops") is not None:
        text += f"This saved projection was selected within {selection['max_hops']} transaction hops. "
    if selection.get("connection_scope") is not None or selection.get("max_hops") is not None:
        text += "Choose Shared collection again to search a different or wider scope. "
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
                 interval=.02, workers=4, layout_settings=None, data_source="investigation", dataset_id=None,
                 connection_scope="all_saved", pegout_lbtc_limit=None, _preflight=None, _board_lock_held=False):
    """Plot saved evidence afresh, or review additions against a selected live board."""
    from .cli import open_preview
    from .elk_layout import optimize_graph
    from .layout_preview import export_layout
    from .mermaid import mermaid_source
    from .miro import validate_plan
    from .transaction_csv import write_transaction_csv
    from .progress import report_progress
    from .plot_performance import PlotTimings
    timings = PlotTimings(progress)
    report_progress(progress, "preparing_plot", 0, 1)
    if layout_mode not in ("fresh", "update"):
        raise TraceError("Choose a fresh layout or an update to an existing Miro board")
    if (layout_mode == "fresh" and board_record_id is not None
            or layout_mode == "update" and not isinstance(board_record_id, str)):
        raise TraceError("Choose a target Miro board only when preparing an update layout")
    case = _ordinary(case)
    if data_source not in ("investigation", "shared") or (data_source == "investigation" and dataset_id is not None):
        raise TraceError("Choose investigation data or a saved shared collection")
    prepared = None
    if data_source == "shared":
        from .investigations import read_case
        from .shared_projection import materialize_shared_source
        with _locked(case):
            metadata = read_case(case)
        preliminary = {"seeds": metadata.get("seeds", []),
                       "hop_reference_name": metadata.get("run_defaults", {}).get("hop_reference_name", "")}
        query = _query(goal, preliminary, min_hops, max_hops, include_unspent=include_unspent,
                       include_unspendable=include_unspendable, include_context=include_context,
                       connection_scope=connection_scope, pegout_lbtc_limit=pegout_lbtc_limit)
        bound = query.get("max_hops") if goal in ("pegouts", "connections") else None
        scope = query.get("connection_scope") if goal == "connections" else None
        with timings.measure("shared_projection"):
            run_id, state, checksum = materialize_shared_source(
                case, run_id, dataset_id=dataset_id, progress=progress, max_hops=bound, connection_scope=scope)
        prepared = (state, checksum)
    with ExitStack() as operation:
        if layout_mode == "update" and not _board_lock_held:
            from .investigation_boards import _board_lock
            operation.enter_context(_board_lock(case, board_record_id))
        with timings.measure("load_source"):
            state, settings, fingerprints = _source(case, run_id, progress=progress, prepared=prepared)
        report_progress(progress, "layout", 0, 1)
        if layout_settings is not None:
            settings = validate_layout_settings(layout_settings)
        inputs = {"schema_version": 1, "case_id": state["case_id"], "run_id": state["run_id"],
                  "captured_at": now(), "service_controls": deepcopy(state["service_controls"]),
                  "address_tx_counts": deepcopy(state["address_tx_counts"])}
        query = _query(goal, state, min_hops, max_hops, include_unspent=include_unspent,
                       include_unspendable=include_unspendable, include_context=include_context,
                       connection_scope=connection_scope, pegout_lbtc_limit=pegout_lbtc_limit)
        settings = _effective_settings(settings, goal, query)
        with timings.measure("build_graph"):
            graph = _graph(state, goal, query, settings, initial_layout=False)
        graph["graph_options"].update({key: deepcopy(settings[key]) for key in LAYOUT_SETTINGS.intersection(settings)})
        board_fields = {"layout_mode": layout_mode}
        if layout_mode == "update":
            from .board_layout import capture, prepare_graph
            from .investigation_boards import (board_for_plot, _board_plan, _check_legacy_removals,
                                               _check_plot_address_mode)
            record = board_for_plot(case, board_record_id, goal)
            board_plan = _board_plan(plot_plan(graph), record)
            state_path = case / record["state_file"]
            _check_plot_address_mode(board_plan, record, state_path)
            _check_legacy_removals(board_plan, record, state_path)
            if _preflight is not None:
                _preflight(graph, record)
            with timings.measure("read_board"):
                snapshot = capture(record["board_id"], state_path, board_plan["namespace"],
                                   token=token, transport=transport, interval=interval,
                                   workers=workers, progress=progress)
            with timings.measure("layout"):
                graph = prepare_graph(graph, snapshot, connector_style=settings["connector_style"],
                                      layout_attempts=settings["layout_attempts"], progress=progress)
            board_fields.update(board_record_id=record["id"], board_id=record["board_id"],
                                board_name=record["name"],
                                update_counts=deepcopy(graph["board_layout"].get("counts", {})))
        else:
            if _preflight is not None:
                _preflight(graph, None)
            if graph["nodes"]:
                with timings.measure("layout"):
                    graph = optimize_graph(graph, connector_style=settings["connector_style"],
                                           layout_attempts=settings["layout_attempts"], progress=progress)
        coverage = _coverage(state)
        graph["notice"] = coverage["coverage_notice"] + " " + graph["notice"]
        from .context_connectors import summaries
        display_edge_count = len(graph["edges"]) - sum(
            len(edge["details"]["context_summary"]["member_edge_ids"]) - 1
            for edge in summaries(graph))
        report = {"schema_version": 1, "case_id": state["case_id"], "run_id": state["run_id"],
                  "goal": goal, "query": query, "created_at": now(), **coverage, **fingerprints, **board_fields,
                  "input_snapshot_version": 1, "input_snapshot_at": inputs["captured_at"],
                  "inputs_sha256": digest(canonical(inputs)),
                  "layout_settings": deepcopy(settings), "settings_sha256": digest(canonical(settings)),
                  "min_hops": query.get("min_hops", 0), "max_hops": query.get("max_hops"),
                  "node_count": len(graph["nodes"]), "edge_count": len(graph["edges"]),
                  "display_edge_count": display_edge_count,
                  "transaction_count": sum(node["kind"] == "transaction" for node in graph["nodes"]),
                  "status": "plotted" if graph["nodes"] else "empty"}
        if state.get("collection_source", {}).get("kind") == "shared":
            report["collection_source"] = deepcopy(state["collection_source"])
        if goal == "connections":
            report.update(connection_count=graph["connections"]["connection_count"], status=graph["connections"]["status"])
            if "connection_scope" in query:
                report["connection_scope"] = query["connection_scope"]
            if query.get("transaction_io") == "complete":
                report["transaction_io"] = "complete"
                report["context_edge_count"] = graph["connections"]["context_edge_count"]
        elif goal == "pegouts":
            from .pegout_csv import pegout_lbtc_summary
            report.update(match_count=graph["pegouts"]["match_count"], status=graph["pegouts"]["status"])
            report["pegout_lbtc_summary"] = pegout_lbtc_summary(graph, state)
            report["csv_export_version"] = 1
            for key in ("endpoint_count", "endpoint_counts", "context_edge_count", "pegout_limit_summary"):
                if key in graph["pegouts"]:
                    report[key] = deepcopy(graph["pegouts"][key])
        graph["plot"] = report
        destination = _ordinary(case / "previews" / (state["run_id"] + "-plots-" + uuid.uuid4().hex[:8]))
        if progress:
            progress({"phase": "exporting_plot", "completed": 0, "total": 1})
        with timings.measure("export_preview"):
            result = export_layout(graph, destination) if graph["nodes"] else _empty_export(graph, destination)
        try:
            with timings.measure("build_plan"):
                plan = plot_plan(graph)
                validate_plan(plan)
                save_json(destination / "miro-plan.json", plan)
            with timings.measure("write_exports"):
                save_json(destination / "plot.json", report)
                save_json(destination / "inputs.json", inputs)
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
        from .preview_numbers import numbered_previews
        result = {**_summary(graph, destination.name), **result, "directory": str(destination.resolve()),
                  "timings": timings.snapshot(),
                  "browser_opened": open_preview(result["html"]) if open_browser else False}
        return numbered_previews(case, [result], identity=state["case_id"])[0]


def _snapshot(case, preview_id, *, with_inputs=False):
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
    if ("display_edge_count" in report
            and (type(report["display_edge_count"]) is not int
                 or report["display_edge_count"] != len(plan["connectors"]))):
        raise TraceError("Saved plot displayed connection count disagrees with its Miro plan")
    if plot_plan(graph) != plan:
        raise TraceError("Saved plot and its Miro plan disagree")
    if report["goal"] == "pegouts":
        query = report["query"]
        pegouts = graph.get("pegouts", {})
        if (canonical(graph.get("graph_options", {}).get("pegout_query")) != canonical(query)
                or canonical(pegouts.get("query")) != canonical(query)
                or any(canonical(report.get(key)) != canonical(pegouts.get(key))
                       for key in ("match_count", "endpoint_count", "endpoint_counts", "context_edge_count", "status",
                                   "pegout_limit_summary"))):
            raise TraceError("Saved endpoint options disagree with the graph; regenerate the plot")
        if query.get("pegout_lbtc_limit") is not None or "pegout_limit_summary" in report:
            from .pegout_limit import validate_pegout_limit_summary
            validate_pegout_limit_summary(report.get("pegout_limit_summary"), query)
        if "pegout_lbtc_summary" in report:
            from .pegout_csv import validate_pegout_lbtc_summary
            validate_pegout_lbtc_summary(report["pegout_lbtc_summary"], report.get("match_count"))
    elif report["goal"] == "connections":
        query, connections, options = report["query"], graph.get("connections", {}), graph.get("graph_options", {})
        if (any(canonical(value.get("connection_scope")) != canonical(query.get("connection_scope"))
                for value in (report, connections, options))
                or any(canonical(value.get("transaction_io")) != canonical(query.get("transaction_io"))
                       for value in (report, connections, options))
                or canonical(report.get("context_edge_count")) != canonical(connections.get("context_edge_count"))
                or any(canonical(value) != canonical(query.get("max_hops")) for value in
                       (report.get("max_hops"), connections.get("max_hops"), options.get("connection_hops")))
                or any(canonical(report.get(key)) != canonical(connections.get(key))
                       for key in ("connection_count", "status"))):
            raise TraceError("Saved connection scope disagrees with its graph; regenerate the plot")
    _snapshot_settings(graph)
    _snapshot_board(graph, plan)
    inputs = _snapshot_inputs(directory, graph)
    return (graph, plan, inputs) if with_inputs else (graph, plan)


def _snapshot_inputs(directory, graph):
    report = graph["plot"]
    if "input_snapshot_version" not in report:
        return None
    inputs = read_json(_ordinary(directory / "inputs.json"))
    keys = {"schema_version", "case_id", "run_id", "captured_at", "service_controls", "address_tx_counts"}
    if ("layout_settings" not in report or not isinstance(inputs, dict) or set(inputs) != keys
            or type(inputs.get("schema_version")) is not int or inputs["schema_version"] != 1
            or not isinstance(inputs.get("captured_at"), str) or not inputs["captured_at"]
            or inputs.get("case_id") != report["case_id"]
            or inputs.get("run_id") != report["run_id"] or inputs.get("captured_at") != report.get("input_snapshot_at")
            or not isinstance(inputs.get("service_controls"), dict) or not isinstance(inputs.get("address_tx_counts"), dict)
            or digest(canonical(inputs)) != report.get("inputs_sha256")
            or canonical(graph.get("service_controls")) != canonical(inputs["service_controls"])
            or digest(canonical(inputs["service_controls"])) != report.get("service_sha256")
            or digest(canonical(inputs["address_tx_counts"])) != report.get("address_counts_sha256")):
        raise TraceError("Saved plot input snapshot changed or disagrees with its fingerprints; regenerate the plot")
    return inputs


def _review_source(case, graph, source_cache=None, inputs=None):
    report = graph["plot"]
    run_id = report["run_id"]
    frozen = inputs is not None
    cache_key = (run_id, frozen)
    if source_cache is not None and cache_key in source_cache:
        state, fingerprints = source_cache[cache_key]
    else:
        if frozen:
            state, archive_sha256 = _archive_source(case, run_id)
            fingerprints = {"archive_sha256": archive_sha256}
        else:
            state, _, fingerprints = _source(case, run_id)
        if source_cache is not None:
            # Several goals commonly share one large archive. Verify its bytes
            # once per listing, retaining only the seed/source identity here.
            state = {key: state[key] for key in ("seeds", "source", "hop_reference_name", "collection_source") if key in state}
            source_cache[cache_key] = state, fingerprints
    query = report.get("query", {})
    expected = _query(report["goal"], state, query.get("min_hops", 0), query.get("max_hops", 10),
                      include_unspent=query.get("include_unspent", False),
                      include_unspendable=query.get("include_unspendable", False),
                      include_context=query.get("include_context", False),
                      transaction_io=query.get("transaction_io"),
                      attribution_hop_limits=query.get("attribution_hop_limits"),
                      connection_scope=query.get("connection_scope"),
                      pegout_lbtc_limit=query.get("pegout_lbtc_limit"))
    settings = _snapshot_settings(graph)
    if settings is not None:
        from .export import PRESENTATION_VERSION
        if settings["presentation_version"] != PRESENTATION_VERSION:
            raise TraceError("Saved layout uses a different presentation version; regenerate the plot")
    if (any(report.get(key) != value for key, value in fingerprints.items()
            if settings is None or key != "settings_sha256")
            or query != expected or graph["namespace"].get("source") != state["source"]
            or canonical(report.get("collection_source")) != canonical(state.get("collection_source"))):
        raise TraceError("Evidence, address counts, trace controls or plot settings changed; regenerate the plot")


def _review_inventory(case, preview_id):
    """Bind one in-process review to the exact files that were verified.

    This inventory never authorizes a cold read. Reuse is restricted to the
    lifetime of a publication operation that has already hashed these bytes.
    ctime and inode detect replacement or edits with restored size/mtime.
    """
    from .cli import run_path
    from .snapshot_index import _inventory, _stat
    if not isinstance(preview_id, str) or not PREVIEW_ID.fullmatch(preview_id):
        raise TraceError("Choose a saved investigation plot")
    directory = _ordinary(case / "previews" / preview_id)
    preview = {name: _stat(_ordinary(directory / name)) for name in sorted(plot_files(directory))}
    archive = _inventory(run_path(case, preview_id[:16]), require_snapshot=False)
    return preview, archive


class _VerifiedPlot(tuple):
    """Private, operation-scoped capability; ordinary callers still get a pair."""
    def __new__(cls, graph, plan, case, preview_id, inventory):
        value = super().__new__(cls, (graph, plan))
        value.case = case.absolute()
        value.preview_id = preview_id
        value.inventory = inventory
        value.active = False
        return value

    def check(self, case, preview_id):
        if not self.active or self.case != Path(case).absolute() or self.preview_id != preview_id:
            raise TraceError("Saved plot review is not active for this publication")
        try:
            current = _review_inventory(self.case, self.preview_id)
        except (OSError, ValueError) as error:
            raise TraceError("Saved plot or source changed during publication; review it again") from error
        if current != self.inventory:
            raise TraceError("Saved plot or source changed during publication; review it again")
        from .investigations import read_case
        if read_case(self.case)["case_id"] != self[0]["plot"]["case_id"]:
            raise TraceError("Saved plot no longer belongs to this investigation")
        return self


def reviewed_plot(case, preview_id, *, _capture_reuse=False):
    """Verify archived bytes and frozen inputs; older plots keep strict review."""
    case = _ordinary(case)
    before = _review_inventory(case, preview_id) if _capture_reuse else None
    graph, plan, inputs = _snapshot(case, preview_id, with_inputs=True)
    _review_source(case, graph, inputs=inputs)
    if not _capture_reuse:
        return graph, plan
    if _review_inventory(case, preview_id) != before:
        raise TraceError("Saved plot or source changed while being reviewed; review it again")
    return _VerifiedPlot(graph, plan, case, preview_id, before)


def list_plots(case):
    """List recent intact layouts, keeping stale evidence visible for refresh."""
    case = _ordinary(case)
    recent = heapq.nlargest(100, (path for path in (case / "previews").glob("*-plots-*")
        if PREVIEW_ID.fullmatch(path.name) and not path.is_symlink()), key=lambda path: path.stat().st_mtime_ns)
    result, source_cache = [], {}
    for directory in recent:
        try:
            graph, _, inputs = _snapshot(case, directory.name, with_inputs=True)
        except (TraceError, OSError, ValueError, TypeError, KeyError):
            continue
        reason = None
        try:
            _review_source(case, graph, source_cache, inputs)
        except (TraceError, OSError, ValueError, TypeError, KeyError) as error:
            reason = str(error)
        result.append(_summary(graph, directory.name, reviewable=reason is None, reason=reason))
    from .preview_numbers import numbered_previews
    return numbered_previews(case, sorted(result, key=lambda value: (value["created_at"], value["id"]), reverse=True))
