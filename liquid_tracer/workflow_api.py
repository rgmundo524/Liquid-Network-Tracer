"""Public HTTP representations for the collection, plot, and board workflow."""

from urllib.parse import quote

from .common import TraceError
from .investigations import read_case

PLOT_FIELDS = {"id", "preview_id", "run_id", "goal", "min_hops", "max_hops", "created_at",
               "status", "node_count", "edge_count", "transaction_count", "match_count",
               "connection_count", "source_max_hops", "source_run_status", "source_stop_reason",
               "notice", "coverage_notice", "reviewable", "review_error", "empty", "saved_data_only"}
BOARD_FIELDS = {"id", "record_id", "name", "goal", "board_id", "status", "preview_id", "run_id",
                "legacy_snapshot", "can_sync", "notice", "pending_count", "created", "reused"}


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
    if value.get("review_error"):
        # The detailed verification error can include local paths.
        result["review_error"] = result["reason"] = "Saved plot no longer matches the investigation. Generate it again."
    return result


def plot_artifact(case, preview_id, *, verified=False):
    from .plots import reviewed_plot, plot_files
    from .web import safe_path

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
    return result


def case_workflow(case):
    from .plots import list_plots
    from .investigation_boards import list_boards

    result = {"plots": [], "boards": []}
    try:
        plots = list_plots(case)
    except (TraceError, OSError, ValueError, TypeError, KeyError):
        plots = []
        result["plots_notice"] = "Saved plots are temporarily unavailable. Wait for collection or settings updates to finish."
    for value in plots:
        item = public_plot(value)
        if value.get("reviewable"):
            try:
                item["artifact"] = plot_artifact(case, value["preview_id"], verified=True)
            except (TraceError, OSError, ValueError):
                item.update(reviewable=False, reason="Saved plot unavailable. Generate it again.")
        result["plots"].append(item)
    try:
        result["boards"] = [public_board(value) for value in list_boards(case)]
    except (TraceError, OSError, ValueError, TypeError, KeyError):
        result["boards_notice"] = "The saved board registry is unavailable. Restore it before changing investigation boards."
    return result


def workflow_action(server, case, metadata, body):
    from .boards import board_options
    from .cli import board_id, resolve_latest, run_path, verify_export
    from .investigation_boards import GOALS, list_boards
    from .plots import reviewed_plot
    from .web import RequestError, validate_settings

    action = body["action"]
    live = False
    if action == "plot":
        if set(body) != {"action", "goal", "run_id", "min_hops", "max_hops"}:
            raise RequestError("Choose a saved collection, plotting goal, and hop range.")
        if not isinstance(body.get("goal"), str) or body["goal"] not in GOALS:
            raise RequestError("Choose full trace, starter connections, or peg-out paths.")
        lower, upper = body.get("min_hops"), body.get("max_hops")
        if type(lower) is not int or type(upper) is not int or not 0 <= lower <= upper <= 2147483647:
            raise RequestError("Enter whole-number hops from 0 to 2147483647, with minimum no greater than maximum.")
        if not isinstance(body.get("run_id"), str):
            raise RequestError("Choose a saved collection to plot.")
        selected = resolve_latest(case, body["run_id"])
        verify_export(run_path(case, selected))
        arguments = ["plot", "--case", str(case), "--goal", body["goal"], "--run", selected,
                     "--min-hops", str(lower), "--max-hops", str(upper)]
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
    else:
        if set(body) != {"action", "record_id", "preview_id", "reorganize"} or type(body.get("reorganize")) is not bool:
            raise RequestError("Choose an investigation board, a saved plot, and whether to reorganize it.")
        records = list_boards(case)
        record = next((item for item in records if item["id"] == body["record_id"]), None)
        if not record or not record.get("can_sync"):
            raise RequestError("Choose a managed board available for syncing.")
        graph, _ = reviewed_plot(case, body.get("preview_id"))
        if graph["plot"]["goal"] != record["goal"]:
            raise RequestError("The selected plot must match the board's plotting goal.")
        if not graph.get("nodes"):
            raise RequestError("This plot has no matching activity to send to Miro.")
        settings = validate_settings(metadata.get("run_defaults", {}))
        arguments = ["investigation-board-sync", "--case", str(case), "--record", record["id"],
                     "--preview", body["preview_id"], "--max-items", str(settings["max_new_items"])]
        if body["reorganize"]:
            arguments.append("--reorganize")
        live = True
    return server.start_job(arguments, action=action, live=live, case=case)


def workflow_result(case, value, action):
    if action == "plot":
        result = public_plot(value)
        result["artifact"] = plot_artifact(case, value["preview_id"])
        return result
    result = public_board(value)
    for key in ("new_shapes", "new_connectors", "new_items", "updated", "deleted", "moved", "reorganize"):
        if type(value.get(key)) in (int, bool):
            result[key] = value[key]
    return result
