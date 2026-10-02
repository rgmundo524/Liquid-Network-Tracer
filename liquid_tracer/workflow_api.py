"""Public HTTP representations for the collection, plot, and board workflow."""

from urllib.parse import quote
import heapq
import json
import re
import stat

from .common import TraceError
from .investigations import read_case

PLOT_FIELDS = {"id", "preview_id", "run_id", "goal", "min_hops", "max_hops", "created_at",
               "status", "node_count", "edge_count", "transaction_count", "match_count",
               "connection_count", "source_max_hops", "source_run_status", "source_stop_reason",
               "notice", "coverage_notice", "publication_notice", "reviewable", "review_error", "empty", "saved_data_only",
               "layout_mode", "board_record_id", "board_id", "board_name", "hop_reference_name"}
PLOT_FIELDS.update({"input_snapshot_version", "input_snapshot_at", "validation_pending"})
BOARD_FIELDS = {"id", "record_id", "name", "goal", "board_id", "status", "preview_id", "run_id",
                "legacy_snapshot", "can_sync", "notice", "pending_count", "created", "reused",
                "creation_preview_id", "created_board", "reused_board"}


def _fields(value, allowed):
    return {key: item for key, item in value.items() if key in allowed
            and (item is None or isinstance(item, (str, int, float, bool)))}


def public_board(value):
    from .cli import board_id

    result = _fields(value, BOARD_FIELDS)
    if value.get("board_id"):
        identity = board_id(value["board_id"])
        result["board_id"] = identity
        result["board_url"] = "https://miro.com/app/board/" + quote(identity, safe="") + "/"
    return result


def public_plot(value):
    result = _fields(value, PLOT_FIELDS)
    source = value.get("collection_source")
    if (isinstance(source, dict) and source.get("kind") == "shared"
            and isinstance(source.get("dataset_id"), str) and re.fullmatch(r"[0-9a-f]{32}", source["dataset_id"])
            and isinstance(source.get("run_id"), str) and re.fullmatch(r"[0-9a-f]{16}", source["run_id"])
            and isinstance(source.get("archive_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", source["archive_sha256"])):
        result["collection_source"] = {key: source[key] for key in ("kind", "dataset_id", "run_id", "archive_sha256")}
    counts = value.get("update_counts")
    count_keys = {"new_nodes", "retained_nodes", "removed_nodes", "new_connectors", "removed_connectors"}
    if (value.get("layout_mode") == "update" and isinstance(counts, dict) and set(counts) == count_keys
            and all(type(count) is int and 0 <= count <= 2 ** 53 - 1 for count in counts.values())):
        result["update_counts"] = dict(counts)
    scope, maximum = value.get("connection_scope"), value.get("max_hops")
    if (value.get("goal") == "connections" and
            ((scope == "all_saved" and maximum is None) or
             (scope == "hop_limited" and type(maximum) is int and 0 <= maximum <= 2147483647))):
        result["connection_scope"] = scope
        query = value.get("query")
        allowed = {"connection_scope", "hop_reference_name", "transaction_io"}
        if scope == "hop_limited":
            allowed.add("max_hops")
        if (isinstance(query, dict) and set(query) <= allowed
                and query.get("connection_scope") == scope):
            from .group_hops import normalize_reference_name
            try:
                name = normalize_reference_name(query.get("hop_reference_name", ""))
                normalized = {"connection_scope": scope, **({"hop_reference_name": name} if name else {})}
                if scope == "hop_limited":
                    if type(query.get("max_hops")) is not int or query["max_hops"] != maximum:
                        raise TraceError("Saved connection hop limit disagrees with its query")
                    normalized["max_hops"] = maximum
                if query.get("transaction_io") == "complete":
                    normalized["transaction_io"] = "complete"
                if query == normalized:
                    result["query"] = normalized
            except TraceError:
                pass
        context_count = value.get("context_edge_count")
        if (result.get("query", {}).get("transaction_io") == "complete"
                and type(context_count) is int and context_count >= 0):
            result["context_edge_count"] = context_count
    if value.get("goal") == "pegouts":
        from .pegout_paths import validate_query
        from .pegout_csv import validate_pegout_lbtc_summary
        if "pegout_lbtc_summary" in value:
            try:
                result["pegout_lbtc_summary"] = validate_pegout_lbtc_summary(
                    value["pegout_lbtc_summary"], value.get("match_count"))
            except TraceError:
                pass
        query = value.get("query")
        if isinstance(query, dict):
            try:
                normalized = validate_query(**query)
                if normalized == query:
                    result["query"] = normalized
            except (TraceError, TypeError):
                pass
        context_count = value.get("context_edge_count")
        if ((result.get("query", {}).get("include_context")
                or result.get("query", {}).get("transaction_io") == "complete")
                and type(context_count) is int and context_count >= 0):
            result["context_edge_count"] = context_count
        counts = value.get("endpoint_counts")
        if (isinstance(counts, dict) and set(counts) == {"pegout", "unspent", "unspendable"}
                and all(type(count) is int and count >= 0 for count in counts.values())
                and type(value.get("endpoint_count")) is int
                and value["endpoint_count"] == sum(counts.values())):
            result["endpoint_counts"] = dict(counts)
            result["endpoint_count"] = value["endpoint_count"]
    if "layout_settings" in value:
        from .plots import validate_layout_settings
        try:
            result["layout_settings"] = validate_layout_settings(value["layout_settings"])
        except TraceError:
            pass
    if value.get("review_error"):
        # The detailed verification error can include local paths.
        result["review_error"] = result["reason"] = "Saved plot no longer matches the investigation. Generate it again."
    return result


def plot_artifact(case, preview_id, *, verified=False):
    from .plots import reviewed_plot, plot_files
    from .web import safe_path
    from .common import read_json
    from .plot_csv import csv_links

    if not verified:
        reviewed_plot(case, preview_id)
    directory = safe_path(case, ["previews", preview_id])
    identity = read_case(case)["case_id"]
    result = {"preview_id": preview_id, "downloads": []}
    for name in sorted(plot_files(directory)):
        if not safe_path(case, ["previews", preview_id, name]).is_file():
            continue
        url = "/files/" + identity + "/previews/" + quote(preview_id) + "/" + quote(name)
        result["downloads"].append({"name": name, "url": url})
        if name == "graph.html":
            result["preview_url"] = url
    if read_json(directory / "plot.json").get("goal") == "pegouts":
        result["downloads"] = [item for item in result["downloads"] if not item["name"].endswith(".csv")]
        result["downloads"].extend(csv_links(identity, preview_id))
    return result


# Listing a saved plot must not read its graph, Miro plan, or source archive.
# Reports are small metadata; bound both the number and size of those reads.
PLOT_SUMMARY_LIMIT = 100
PLOT_SUMMARY_BYTES = 1024 * 1024


def _plot_summary(case, directory, identity):
    from .plots import GOALS, _ordinary, FILES, PEGOUT_CSV_FILES, INPUT_SNAPSHOT_FILES
    from .plot_csv import csv_links

    path = _ordinary(directory / "plot.json")
    details = path.stat()
    if not stat.S_ISREG(details.st_mode) or details.st_size > PLOT_SUMMARY_BYTES:
        raise TraceError("Saved plot report is unavailable")
    with path.open("rb") as stream:
        raw = stream.read(PLOT_SUMMARY_BYTES + 1)
    if len(raw) > PLOT_SUMMARY_BYTES:
        raise TraceError("Saved plot report is too large")
    report = json.loads(raw)
    if (not isinstance(report, dict) or type(report.get("schema_version")) is not int
            or report["schema_version"] != 1 or report.get("case_id") != identity
            or report.get("run_id") != directory.name[:16]
            or not isinstance(report.get("goal"), str) or report["goal"] not in GOALS
            or not isinstance(report.get("query"), dict)
            or not isinstance(report.get("created_at"), str) or not report["created_at"]
            or any(type(report.get(key)) is not int or not 0 <= report[key] <= 2 ** 53 - 1
                   for key in ("node_count", "edge_count", "transaction_count"))):
        raise TraceError("Saved plot report does not match this investigation")
    files = FILES
    for key, names in (("input_snapshot_version", INPUT_SNAPSHOT_FILES), ("csv_export_version", PEGOUT_CSV_FILES)):
        if key in report:
            if (type(report[key]) is not int or report[key] != 1
                    or (key == "csv_export_version" and report["goal"] != "pegouts")):
                raise TraceError("Unsupported saved plot report")
            files |= names
    # A manifest is the completion marker, not evidence of integrity. Only the
    # selected-plot and download endpoints perform the expensive verification.
    manifest = _ordinary(directory / "SHA256SUMS")
    if not manifest.is_file():
        raise TraceError("Saved plot is not finished")
    item = public_plot(report)
    for key in ("id", "preview_id", "review_error", "reason", "reviewable", "validation_pending", "empty"):
        item.pop(key, None)
    for key in ("match_count", "connection_count", "source_max_hops", "min_hops", "max_hops"):
        if key in item and item[key] is not None and (type(item[key]) is not int or not 0 <= item[key] <= 2 ** 53 - 1):
            item.pop(key)
    for key in ("saved_data_only",):
        if key in item and type(item[key]) is not bool:
            item.pop(key)
    item.update(id=directory.name, preview_id=directory.name, reviewable=False,
                validation_pending=True, empty=report["node_count"] == 0)
    artifact = {"preview_id": directory.name, "downloads": []}
    for name in sorted(files):
        if not _ordinary(directory / name).is_file():
            continue
        url = "/files/" + identity + "/previews/" + quote(directory.name) + "/" + quote(name)
        artifact["downloads"].append({"name": name, "url": url})
        if name == "graph.html":
            artifact["preview_url"] = url
    if report["goal"] == "pegouts":
        artifact["downloads"] = [value for value in artifact["downloads"] if not value["name"].endswith(".csv")]
        artifact["downloads"].extend(csv_links(identity, directory.name))
    item["artifact"] = artifact
    return item


def plot_summaries(case):
    """Return bounded, unverified metadata for choosing a saved plot."""
    from .plots import PREVIEW_ID, _ordinary

    case = _ordinary(case)
    identity = read_case(case)["case_id"]
    previews = _ordinary(case / "previews")
    candidates = []
    for directory in previews.glob("*-plots-*"):
        try:
            details = directory.lstat()
            if PREVIEW_ID.fullmatch(directory.name) and stat.S_ISDIR(details.st_mode):
                candidates.append((details.st_mtime_ns, directory.name, directory))
        except OSError:
            continue
    recent = heapq.nlargest(PLOT_SUMMARY_LIMIT, candidates)
    result = []
    for _, _, directory in recent:
        try:
            result.append(_plot_summary(case, directory, identity))
        except (TraceError, OSError, ValueError, TypeError, KeyError):
            continue
    return sorted(result, key=lambda value: (value["created_at"], value["id"]), reverse=True)


def selected_plot(case, preview_id):
    """Verify only the selected saved plot before presenting it for review."""
    from .plots import reviewed_plot, _summary

    graph, _ = reviewed_plot(case, preview_id)
    item = public_plot(_summary(graph, preview_id))
    item["validation_pending"] = False
    item["artifact"] = plot_artifact(case, preview_id, verified=True)
    return item


def case_boards(case):
    """Read authoritative board mappings separately from plot summaries."""
    from .investigation_boards import list_boards

    result = {"boards": []}
    try:
        result["boards"] = [public_board(value) for value in list_boards(case)]
    except (TraceError, OSError, ValueError, TypeError, KeyError):
        result["boards_notice"] = "The saved board registry is unavailable. Restore it before changing investigation boards."
    return result


def case_workflow(case, *, lightweight=False):
    from .plots import list_plots

    result = {"plots": []}
    try:
        plots = plot_summaries(case) if lightweight else list_plots(case)
    except (TraceError, OSError, ValueError, TypeError, KeyError):
        plots = []
        result["plots_notice"] = "Saved plots are temporarily unavailable. Wait for collection or settings updates to finish."
    for value in plots:
        if lightweight:
            result["plots"].append(value)
            continue
        item = public_plot(value)
        if value.get("reviewable"):
            try:
                item["artifact"] = plot_artifact(case, value["preview_id"], verified=True)
            except (TraceError, OSError, ValueError):
                item.update(reviewable=False, reason="Saved plot unavailable. Generate it again.")
        if value.get("goal") == "pegouts" and "artifact" not in item:
            # list_plots verified the immutable snapshot. Changed live settings
            # can prevent syncing, while its original CSV tables remain useful.
            from .plot_csv import csv_links
            item["artifact"] = {"preview_id": value["preview_id"],
                                "downloads": csv_links(read_case(case)["case_id"], value["preview_id"])}
        result["plots"].append(item)
    if not lightweight:
        result.update(case_boards(case))
    return result


def workflow_action(server, case, metadata, body):
    from .boards import board_options
    from .cli import board_id, resolve_latest, run_path
    from .investigation_boards import GOALS, list_boards
    from .plots import reviewed_plot
    from .web import RequestError
    from .investigations import effective_run_settings

    action = body["action"]
    live = False
    if action in ("plot", "plot-sync"):
        required = {"action", "goal", "run_id"}
        if body.get("goal") != "connections":
            required.update({"min_hops", "max_hops"})
        endpoint_options = {"include_unspent", "include_unspendable"}
        allowed = {"include_context", "layout_mode", "board_record_id", "layout_settings", "min_hops", "max_hops",
                   "data_source", "dataset_id", "connection_scope"}
        if action == "plot-sync":
            allowed.add("name")
        if not required <= set(body) or set(body) - required - endpoint_options - allowed:
            raise RequestError("Choose a saved collection, plotting goal, and hop range.")
        if not isinstance(body.get("goal"), str) or body["goal"] not in GOALS:
            raise RequestError("Choose full trace, starter connections, or peg-out paths.")
        scope = body.get("connection_scope", "all_saved")
        if scope not in ("all_saved", "hop_limited") or ("connection_scope" in body and body["goal"] != "connections"):
            raise RequestError("Choose a connection search scope for Starter connections only.")
        if scope == "hop_limited" and "max_hops" not in body:
            raise RequestError("Choose the maximum number of transaction hops for Starter connections.")
        mode = body.get("layout_mode", "fresh")
        if mode not in ("fresh", "update"):
            raise RequestError("Choose a fresh layout or an update for an existing board.")
        if mode == "update":
            record = next((item for item in list_boards(case)
                           if item["id"] == body.get("board_record_id")), None)
            if not record or not record.get("can_sync") or record["goal"] != body["goal"]:
                raise RequestError("Choose a managed Miro board matching this plotting goal.")
            live = True  # Both update paths first read the live arrangement.
        elif "board_record_id" in body:
            raise RequestError("A fresh layout does not use an existing board.")
        if action == "plot-sync":
            if mode == "fresh":
                name = board_options(body.get("name"), None, "private")["name"]
            elif "name" in body:
                raise RequestError("An update uses the selected board's existing name.")
            live = True
        if any(type(body.get(key, False)) is not bool for key in endpoint_options):
            raise RequestError("Additional endpoint options must be true or false.")
        if body["goal"] != "pegouts" and any(body.get(key, False) for key in endpoint_options):
            raise RequestError("Additional endpoint options apply only to peg-out paths plots.")
        if type(body.get("include_context", False)) is not bool:
            raise RequestError("Include context addresses must be true or false.")
        if body["goal"] != "pegouts" and body.get("include_context", False):
            raise RequestError("Include context addresses applies only to peg-out paths plots.")
        lower, upper = body.get("min_hops", 0), body.get("max_hops", 0)
        if type(lower) is not int or type(upper) is not int or not 0 <= lower <= upper <= 2147483647:
            raise RequestError("Enter whole-number hops from 0 to 2147483647, with minimum no greater than maximum.")
        if not isinstance(body.get("run_id"), str):
            raise RequestError("Choose a saved collection to plot.")
        data_source = body.get("data_source", "investigation")
        if data_source not in ("investigation", "shared"):
            raise RequestError("Choose this investigation's collection or the shared collection.")
        if data_source == "shared":
            from .shared_collection import pin_shared_run
            dataset_id = body.get("dataset_id")
            if not isinstance(dataset_id, str) or not re.fullmatch(r"[0-9a-f]{32}", dataset_id):
                raise RequestError("Choose a compatible shared collection dataset.")
            selected, _ = pin_shared_run(case, body["run_id"], dataset_id=dataset_id)
        else:
            if "dataset_id" in body:
                raise RequestError("A shared dataset ID applies only to shared collection data.")
            selected = resolve_latest(case, body["run_id"])
            if not re.fullmatch(r"[0-9a-f]{16}", selected) or not run_path(case, selected).is_dir():
                raise RequestError("Choose a saved collection to plot.")
            # The worker verifies the pinned archive before using any evidence.
            # Hashing here would delay registration and hold the jobs API lock.
        arguments = [action, "--case", str(case), "--goal", body["goal"], "--run", selected,
                     "--min-hops", str(lower), "--max-hops", str(upper)]
        if "connection_scope" in body:
            arguments.extend(["--connection-scope", scope])
        if data_source == "shared":
            arguments.extend(["--data-source", "shared", "--dataset-id", dataset_id])
        if "layout_settings" in body:
            from .plots import LAYOUT_SETTINGS, validate_layout_settings
            from .export import PRESENTATION_VERSION
            supplied = body["layout_settings"]
            if not isinstance(supplied, dict) or set(supplied) != LAYOUT_SETTINGS:
                raise RequestError("Provide the complete layout settings for this plot.")
            snapshot = validate_layout_settings({**supplied, "presentation_version": PRESENTATION_VERSION})
            arguments.extend(["--layout-settings-json", json.dumps(snapshot, separators=(",", ":"))])
        for key in ("include_unspent", "include_unspendable", "include_context"):
            if body.get(key):
                arguments.append("--" + key.replace("_", "-"))
        if mode == "update":
            arguments.extend(["--layout-mode", "update", "--board-record-id", record["id"]])
        if action == "plot-sync":
            if mode == "fresh":
                arguments.extend(["--name", name])
            settings = effective_run_settings(metadata.get("run_defaults", {}))
            arguments.extend(["--max-items", str(settings["max_new_items"])])
    elif action == "board-create-sync":
        if set(body) != {"action", "name", "preview_id"}:
            raise RequestError("Choose a fresh saved plot and a name for the new board.")
        name = board_options(body.get("name"), None, "private")["name"]
        graph, _ = reviewed_plot(case, body.get("preview_id"))
        if graph.get("plot", {}).get("layout_mode", "fresh") != "fresh":
            raise RequestError("Create and sync requires a fresh layout.")
        if not graph.get("nodes"):
            raise RequestError("This plot has no matching activity to send to Miro.")
        settings = effective_run_settings(metadata.get("run_defaults", {}))
        arguments = ["investigation-board-create-sync", "--case", str(case),
                     "--preview", body["preview_id"], "--name", name,
                     "--max-items", str(settings["max_new_items"])]
        live = True
    elif action in ("board-create", "board-link"):
        expected = {"action", "goal", "name"} | ({"board"} if action == "board-link" else set())
        if set(body) != expected and not (action == "board-link" and set(body) == expected | {"record_id"}):
            raise RequestError("Choose a board name and plotting goal, and a board URL when linking.")
        if not isinstance(body.get("goal"), str) or body["goal"] not in GOALS:
            raise RequestError("Choose a plotting goal for this board.")
        name = board_options(body.get("name"), None, "private")["name"]
        arguments = ["investigation-" + action, "--case", str(case), "--goal", body["goal"], "--name", name]
        live = action == "board-create"
        if action == "board-link":
            arguments.extend(["--board", board_id(body.get("board"))])
            if "record_id" in body:
                records = list_boards(case)
                record = next((item for item in records if item["id"] == body["record_id"]), None)
                if not record or record["goal"] != body["goal"]:
                    raise RequestError("Choose the pending board entry to recover.")
                arguments.extend(["--record", record["id"]])
    elif action == "board-sync":
        if set(body) != {"action", "record_id", "preview_id", "reorganize"} or type(body.get("reorganize")) is not bool:
            raise RequestError("Choose an investigation board, a saved plot, and whether to reorganize it.")
        records = list_boards(case)
        record = next((item for item in records if item["id"] == body["record_id"]), None)
        if not record or not record.get("can_sync"):
            raise RequestError("Choose a managed board available for syncing.")
        graph, _ = reviewed_plot(case, body.get("preview_id"))
        if graph["plot"]["goal"] != record["goal"]:
            raise RequestError("The selected plot must match the board's plotting goal.")
        mode = graph["plot"].get("layout_mode")
        if mode == "update" and (graph["plot"].get("board_record_id") != record["id"]
                                 or graph["plot"].get("board_id") != record["board_id"]):
            raise RequestError("This update layout belongs to a different Miro board.")
        if mode == "update" and body["reorganize"]:
            raise RequestError("An update layout preserves the investigator's existing arrangement.")
        if mode == "fresh" and record.get("creation_preview_id") != body["preview_id"]:
            raise RequestError("Generate an update layout for this existing board, or create a new board from the fresh layout.")
        if not graph.get("nodes") and mode != "update":
            raise RequestError("This plot has no matching activity to send to Miro.")
        settings = effective_run_settings(metadata.get("run_defaults", {}))
        arguments = ["investigation-board-sync", "--case", str(case), "--record", record["id"],
                     "--preview", body["preview_id"], "--max-items", str(settings["max_new_items"])]
        if body["reorganize"]:
            arguments.append("--reorganize")
        live = True
    else:
        raise RequestError("Unknown workflow action.")
    return server.start_job(arguments, action=action, live=live, case=case)


def workflow_result(case, value, action):
    if action == "plot":
        result = public_plot(value)
        result["artifact"] = plot_artifact(case, value["preview_id"])
        return result
    result = public_board(value)
    if action == "plot-sync":
        result = {**public_plot(value), **result}
        result["artifact"] = plot_artifact(case, value["preview_id"])
        if type(value.get("published")) is bool:
            result["published"] = value["published"]
    for key in ("new_shapes", "new_connectors", "new_items", "updated", "deleted", "moved", "reorganize"):
        if type(value.get(key)) in (int, bool):
            result[key] = value[key]
    return result
