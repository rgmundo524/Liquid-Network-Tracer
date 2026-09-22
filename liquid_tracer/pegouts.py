"""Bounded, resumable peg-out searches and separate reviewed plot snapshots."""

from contextlib import contextmanager
import fcntl
import heapq
import html
import json
import os
from pathlib import Path
import re
import sys
import uuid

from .api import Esplora, Limits
from .common import StopRun, TraceError, canonical, digest, now, read_json, save_json
from .investigations import read_case, validate_settings
from .pegout_paths import pegout_graph, validate_query
from .services import apply_service_labels, load_services
from .store import Store
from .trace import new_state, trace, validate_transaction

SEARCH_ID = re.compile(r"[0-9a-f]{16}\Z")
PREVIEW_ID = re.compile(r"[0-9a-f]{16}-pegouts-[0-9a-f]{8}\Z")
FILES = frozenset({"graph.html", "graph.svg", "graph.json", "layout-report.json", "graph.mmd",
                   "transactions.csv", "pegouts.json", "miro-plan.json", "SHA256SUMS"})
OPTIONAL_FILES = frozenset({"details.html", "details.json"})
ARCHIVE_FILES = frozenset({"trace.json", "query.json", "evidence-index.json"})


def preview_files(directory):
    directory = Path(directory)
    return FILES | {name for name in OPTIONAL_FILES
                    if (directory / name).exists() or (directory / name).is_symlink()}


def _ordinary(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise TraceError("Peg-out search files cannot contain symbolic links")
    return path


def _search_path(case, search_id):
    if not isinstance(search_id, str) or not SEARCH_ID.fullmatch(search_id):
        raise TraceError("Choose a saved peg-out search")
    return _ordinary(Path(case) / "pegouts" / search_id)


@contextmanager
def _locked(case, *, exclusive=False):
    with (case / "trace.lock").open("a") as trace_lock, (case / "case.lock").open("a") as case_lock:
        try:
            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(trace_lock, mode | fcntl.LOCK_NB)
            fcntl.flock(case_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("A trace or settings change is active; retry the peg-out search afterward") from None
        yield


def _write_manifest(directory, names):
    names = sorted(set(names) - {"SHA256SUMS"})
    temporary = directory / "SHA256SUMS.tmp"
    with temporary.open("w", encoding="utf-8") as manifest:
        manifest.write("".join(digest((directory / name).read_bytes()) + "  " + name + "\n" for name in names))
        manifest.flush()
        os.fsync(manifest.fileno())
    temporary.replace(directory / "SHA256SUMS")


def _verify_manifest(directory, allowed, required):
    _ordinary(directory / "SHA256SUMS")
    seen = set()
    for line in (directory / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        fields = line.split("  ", 1)
        if (len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{64}", fields[0])
                or fields[1] in seen or not allowed(fields[1])):
            raise TraceError("Invalid peg-out snapshot manifest")
        checksum, name = fields
        path = _ordinary(directory / name)
        if not path.is_file() or digest(path.read_bytes()) != checksum:
            raise TraceError("Peg-out snapshot changed or is incomplete; restore the saved evidence")
        seen.add(name)
    if not set(required).issubset(seen):
        raise TraceError("Peg-out snapshot manifest is incomplete")
    return seen


def _read_search(case, search_id, *, checkpoint=False):
    directory = _search_path(case, search_id)
    for name in ("trace.json", "query.json"):
        _ordinary(directory / name)
    sealed = (directory / "SHA256SUMS").is_file()
    if sealed:
        files = _verify_manifest(directory,
            lambda name: name in ARCHIVE_FILES or bool(re.fullmatch(r"evidence/[0-9]+\.response", name)),
            ARCHIVE_FILES)
        index = read_json(directory / "evidence-index.json")
        if (not isinstance(index, list) or any(not isinstance(row, dict) for row in index)
                or {row.get("file") for row in index} != files - ARCHIVE_FILES):
            raise TraceError("Peg-out evidence index disagrees with its manifest")
    elif not checkpoint:
        raise TraceError("Peg-out search was interrupted before archiving; resume it first")
    state, query = read_json(directory / "trace.json"), read_json(directory / "query.json")
    if (not isinstance(query, dict) or set(query) != {"txid", "min_hops", "max_hops"}
            or validate_query(**query) != query or not isinstance(state, dict)
            or state.get("case_id") != read_case(case)["case_id"] or state.get("run_id") != search_id
            or state.get("pegout_query") != query
            or not isinstance(state.get("transactions"), dict) or not isinstance(state.get("outputs"), dict)
            or not isinstance(state.get("links"), dict) or not isinstance(state.get("seeds"), list)
            or state.get("limits", {}).get("max_hops") != query["max_hops"]):
        raise TraceError("Peg-out search does not match this investigation or query")
    return state, query, sealed


def _verify_checkpoint(store, state):
    """Recover unsealed checkpoints only from responses actually in evidence."""
    observations = {row["id"]: row for row in store.observations(state.get("observations", []))}
    for txid, record in state["transactions"].items():
        validate_transaction(record["data"], txid)
        row = observations.get(record.get("observation_id"))
        if (row is None or row["source"] != state["source"] or row["endpoint"] != "/tx/" + txid
                or json.loads(row["body"]) != record["data"]):
            raise TraceError("Interrupted peg-out transaction is not backed by saved evidence")
    for key, link in state["links"].items():
        txid, _, index = key.rpartition(":")
        row = observations.get(link.get("observation_id"))
        if row is None or row["source"] != state["source"] or row["endpoint"] != "/tx/" + txid + "/outspends":
            raise TraceError("Interrupted peg-out spend is not backed by saved evidence")
        data = json.loads(row["body"])
        if (not index.isdecimal() or not isinstance(data, list) or int(index) >= len(data)
                or data[int(index)].get("spent") is not True
                or data[int(index)].get("txid") != link.get("spending_txid")
                or data[int(index)].get("vin") != link.get("vin")):
            raise TraceError("Interrupted peg-out spend disagrees with saved evidence")


def _archive(store, state, query, directory):
    save_json(directory / "trace.json", state)
    save_json(directory / "query.json", query)
    evidence = directory / "evidence"
    evidence.mkdir(exist_ok=True)
    index = []
    for observation in store.observations(state["observations"]):
        body = observation.pop("body")
        name = f"evidence/{observation['id']:08d}.response"
        (directory / name).write_bytes(body)
        index.append({**observation, "file": name})
    save_json(directory / "evidence-index.json", index)
    _write_manifest(directory, ARCHIVE_FILES | {row["file"] for row in index})


def _limits(metadata, max_hops, overrides):
    settings = validate_settings({**metadata.get("run_defaults", {}),
                                  **{key: value for key, value in overrides.items() if value is not None}})
    return Limits(max_hops, settings["max_transactions"], settings["max_outpoints"],
                  settings["max_requests"], settings["max_seconds"])


def _progress(progress, phase):
    if progress is not None:
        try:
            progress({"phase": phase, "completed": 0, "total": 0})
        except Exception:
            pass  # Progress is advisory; keep saved evidence recoverable.


def search_pegouts(case, txid=None, min_hops=0, max_hops=10, *, resume=None,
                   max_transactions=None, max_outpoints=None, max_requests=None, max_seconds=None,
                   open_browser=False, progress=None):
    """Fetch a bounded search, archive it, then plot the paths found so far."""
    from .change_outputs import _lookup_options, _saved_state
    case = _ordinary(Path(case))
    with _locked(case, exclusive=True):
        metadata = read_case(case)
        parent, sealed = None, True
        if resume is not None:
            if txid is not None or min_hops != 0 or max_hops != 10:
                raise TraceError("Resume uses the saved transaction and hop range; omit new query fields")
            parent, query, sealed = _read_search(case, resume, checkpoint=True)
        else:
            query = validate_query(txid, min_hops, max_hops)
        latest = _saved_state(case) if parent is None else None
        baseline = parent if parent is not None else latest
        options = _lookup_options(case, baseline)
        limits = _limits(metadata, query["max_hops"], {
            "max_transactions": max_transactions, "max_outpoints": max_outpoints,
            "max_requests": max_requests, "max_seconds": max_seconds})
        limits.validate()
        controls = load_services(case)
        labels = apply_service_labels(baseline.get("labels", []) if baseline else [], controls)
        store, api = Store(case), None
        try:
            if parent is not None and not sealed:
                _verify_checkpoint(store, parent)
            api = Esplora(store, "pending", limits, base=options["base_url"],
                           auth=options["auth"], fixture=options["fixture"])
            state = new_state([], api.base, limits, labels, parent, case_id=metadata["case_id"])
            state["pegout_query"] = query
            state["service_controls"] = {k: v for k, v in controls.items() if k != "history"}
            state["include_unconfirmed"] = bool(baseline and baseline.get("include_unconfirmed", False))
            state["investigation"] = {"case_id": metadata["case_id"], "name": metadata.get("name"), "miro_board": None}
            state["address_mode"] = "outpoint_occurrences"
            api.run_id = state["run_id"]
            directory = _search_path(case, state["run_id"])
            directory.mkdir(parents=True, exist_ok=False)
            save_json(directory / "query.json", query)
            save_json(directory / "trace.json", state)
            _progress(progress, "pegout_search")
            try:
                if not state["seeds"]:
                    transaction, _ = api.get("/tx/" + query["txid"])
                    validate_transaction(transaction, query["txid"])
                    if not transaction["vout"]:
                        raise TraceError("The origin transaction has no outputs")
                    state["seeds"] = [f"{query['txid']}:{index}" for index in range(len(transaction["vout"]))]
                    if parent is not None:
                        # A bootstrapping interruption has no existing frontier.
                        state["outputs"] = {key: {"outpoint": key, "txid": query["txid"], "vout": index,
                            "depth": 0, "origin": "analyst_seed", "status": "pending"}
                            for index, key in enumerate(state["seeds"])}
                trace(api, state, limits, directory / "trace.json", state["include_unconfirmed"])
            except (StopRun, KeyboardInterrupt) as error:
                state.update(status="paused", stop_reason="interrupted" if isinstance(error, KeyboardInterrupt) else str(error))
            except TraceError as error:
                state.update(status="error", stop_reason="error")
                state["errors"].append(str(error))
            state["finished_at"] = now()
            state["observations"] = sorted(set(state["observations"]) | api.used)
            state.setdefault("stats", {})["requests_this_run"] = api.budget.requests
            _archive(store, state, query, directory)
            for error in state["errors"]:
                print("Peg-out search: " + str(error), file=sys.stderr)
        finally:
            try:
                if api is not None:
                    api.close()
            finally:
                store.close()
    # Archive completion precedes rendering; a renderer failure needs no refetch.
    try:
        return preview_pegouts(case, state["run_id"], open_browser=open_browser, progress=progress)
    except TraceError as error:
        raise TraceError(f"Peg-out search {state['run_id']} was saved. Retry its preview without fetching again. "
                         + str(error)) from error


def _plan(graph):
    from .connections import connection_plan
    return connection_plan(graph)


def _summary(state, query):
    return {"id": state["run_id"], "search_id": state["run_id"], "run_id": state["run_id"],
            "query": query, **query, "created_at": state["started_at"], "status": state["status"],
            "stop_reason": state.get("stop_reason"), "resumable": True}


def preview_pegouts(case, search_id, *, open_browser=False, progress=None):
    from .address_counts import apply_saved_counts
    from .cli import attribution_arrow_coloring, centered_name_group, connector_appearance, layout_search_attempts, open_preview
    from .elk_layout import optimize_graph
    from .layout_preview import export_layout
    from .mermaid import mermaid_source
    from .miro import validate_plan
    from .transaction_csv import write_transaction_csv
    case = _ordinary(Path(case))
    with _locked(case):
        state, query, _ = _read_search(case, search_id)
        metadata, controls = read_case(case), load_services(case)
        state["labels"] = apply_service_labels(state["labels"], controls)
        state["service_controls"] = {k: v for k, v in controls.items() if k != "history"}
        apply_saved_counts(case, state)
        _progress(progress, "pegout_paths")
        graph = pegout_graph(state, query, color_attribution_arrows=attribution_arrow_coloring(metadata),
                              center_name=centered_name_group(metadata))
        if graph["nodes"]:
            graph = optimize_graph(graph, connector_style=connector_appearance(metadata), progress=progress,
                                   layout_attempts=layout_search_attempts(metadata))
        report = graph["pegouts"]
        report["archive_sha256"] = digest((_search_path(case, search_id) / "SHA256SUMS").read_bytes())
        report["service_sha256"] = digest(canonical(state["service_controls"]))
        destination = _ordinary(case / "previews" / (search_id + "-pegouts-" + uuid.uuid4().hex[:8]))
        if graph["nodes"]:
            result = export_layout(graph, destination)
        else:
            destination.mkdir(parents=True, exist_ok=False)
            save_json(destination / "graph.json", graph)
            save_json(destination / "layout-report.json", {"run_id": search_id, "node_count": 0, "edge_count": 0})
            (destination / "graph.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="800" height="100"/>', encoding="utf-8")
            (destination / "graph.html").write_text('<!doctype html><html lang="en"><meta charset="utf-8">'
                '<title>Peg-out search</title><h1>No peg-out requests found</h1><p>'
                + html.escape(graph["notice"]) + '</p></html>', encoding="utf-8")
            result = {"html": str(destination / "graph.html"), "svg": str(destination / "graph.svg")}
        try:
            plan = _plan(graph)
            validate_plan(plan)
            save_json(destination / "miro-plan.json", plan)
            save_json(destination / "pegouts.json", report)
            (destination / "graph.mmd").write_text(mermaid_source(graph) if graph["nodes"] else
                "flowchart LR\n  %% No peg-out requests found in searched data.\n", encoding="utf-8")
            write_transaction_csv(destination / "transactions.csv", graph, state)
            _write_manifest(destination, preview_files(destination))
        except BaseException:
            (destination / "SHA256SUMS").unlink(missing_ok=True)
            raise
        return {**_summary(state, query), **result, "match_count": report["match_count"],
                "transaction_count": report["transaction_count"], "notice": graph["notice"],
                "preview_id": destination.name, "directory": str(destination.resolve()),
                "center_name": graph["graph_options"]["center_name"],
                "browser_opened": open_preview(result["html"]) if open_browser else False}


def reviewed_pegouts(case, preview_id):
    from .cli import attribution_arrow_coloring, centered_name_group
    from .miro import validate_plan
    case = _ordinary(Path(case))
    if not isinstance(preview_id, str) or not PREVIEW_ID.fullmatch(preview_id):
        raise TraceError("Choose a saved peg-out preview")
    directory = _ordinary(case / "previews" / preview_id)
    files = preview_files(directory)
    _verify_manifest(directory, lambda name: name in files - {"SHA256SUMS"}, files - {"SHA256SUMS"})
    graph, plan = read_json(directory / "graph.json"), read_json(directory / "miro-plan.json")
    state, query, _ = _read_search(case, preview_id[:16])
    metadata, controls = read_case(case), load_services(case)
    report = graph.get("pegouts", {})
    if (graph.get("namespace", {}).get("case_id") != metadata["case_id"]
            or graph.get("run_id") != state["run_id"] or plan.get("run_id") != state["run_id"]
            or graph.get("graph_options", {}).get("view") != "pegout_paths"
            or report.get("query") != query or plan.get("schema_version") != 1):
        raise TraceError("Peg-out preview does not match this investigation or query")
    if (report.get("archive_sha256") != digest((_search_path(case, state["run_id"]) / "SHA256SUMS").read_bytes())
            or report.get("service_sha256") != digest(canonical({k: v for k, v in controls.items() if k != "history"}))
            or graph["graph_options"].get("color_attribution_arrows", False) is not attribution_arrow_coloring(metadata)
            or graph["graph_options"].get("center_name", "") != centered_name_group(metadata)):
        raise TraceError("Evidence, colors, layout settings or trace controls changed; regenerate the peg-out preview")
    validate_plan(plan)
    if _plan(graph) != plan or read_json(directory / "pegouts.json") != report:
        raise TraceError("Peg-out preview and publication plan disagree")
    return graph, plan


def list_pegout_searches(case):
    """Return at most 100 verified recent search summaries, including recovery."""
    case = _ordinary(Path(case))
    candidates = [p for p in (case / "pegouts").glob("*") if SEARCH_ID.fullmatch(p.name) and not p.is_symlink()]
    recent = heapq.nlargest(100, candidates, key=lambda p: p.stat().st_mtime_ns)
    preview_paths = heapq.nlargest(300,
        (p for p in (case / "previews").glob("*-pegouts-*") if PREVIEW_ID.fullmatch(p.name) and not p.is_symlink()),
        key=lambda p: p.stat().st_mtime_ns)
    result = []
    for directory in recent:
        try:
            state, query, sealed = _read_search(case, directory.name, checkpoint=True)
            summary = _summary(state, query)
            summary["recoverable"] = not sealed
            if not sealed:
                summary.update(status="paused", stop_reason="interrupted")
            for preview in preview_paths:
                if not preview.name.startswith(directory.name + "-"):
                    continue
                try:
                    graph, _ = reviewed_pegouts(case, preview.name)
                    summary.update(preview_id=preview.name, match_count=graph["pegouts"]["match_count"])
                    break
                except (TraceError, OSError, ValueError, TypeError, KeyError):
                    continue
            result.append(summary)
        except (TraceError, OSError, ValueError, TypeError, KeyError):
            continue
    return sorted(result, key=lambda value: (value["created_at"], value["id"]), reverse=True)


def publish_pegouts(case, preview_id, board, *, max_items=750, **kwargs):
    from .cli import board_id
    from .miro import publish
    if type(max_items) is not int or max_items < 0:
        raise TraceError("The Miro item budget must be a nonnegative whole number")
    case = _ordinary(Path(case))
    with _locked(case):
        graph, plan = reviewed_pegouts(case, preview_id)
        if not graph["nodes"]:
            return {"status": "no_pegout_found", "items": 0, "search_id": graph["run_id"]}
        target, metadata = board_id(board), read_case(case)
        key = digest(target.encode())[:24]
        if ((metadata.get("miro_board") and target == board_id(metadata["miro_board"]))
                or (case / "miro" / (key + ".json")).exists()
                or (case / "miro" / ("connections-" + key + ".json")).exists()):
            raise TraceError("Choose a separate Miro board for peg-out paths; an existing graph is protected")
        return publish(plan, target, case / "miro" / ("pegouts-" + key + ".json"), max_items=max_items, **kwargs)
