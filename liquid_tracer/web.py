"""Loopback-only Astro UI server using the existing investigation/CLI engine.

The browser chooses validated actions. It never supplies executable arguments,
filesystem paths, API credentials, or a SecretSpec provider configuration.
"""

import argparse
import json
import math
import mimetypes
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from .common import TraceError, read_json
from .inspection import parse_transaction_hashes
from .investigations import (create_investigation, default_root, load_settings,
                             read_case, save_settings, update_case, validate_settings)
from .menu import _command, _environment, _lookup_reports, _project, _seed_values, _trace_arguments
from .progress import public_progress
from .layout_search import MAX_LAYOUT_ATTEMPTS, normalize_layout_attempts
from .layout_search_reporting import public_search_counts

MAX_BODY = 64 * 1024
CASE_ID = re.compile(r"[0-9a-f]{32}")
RUN_ID = re.compile(r"[a-zA-Z0-9]{16}")
FRAME_REVIEW_ID = re.compile(r"[0-9a-f]{64}")
MIRO_ITEM_ID = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_-]{0,199}")
ARTIFACT_DIR = re.compile(r"[a-zA-Z0-9]{16}-(?:csv|mermaid|elk|compact|connections|pegouts)-[0-9a-f]{8}")
COMPACTION_DIR = re.compile(r"[a-zA-Z0-9]{16}-compact-[0-9a-f]{8}")
LEGACY_EXPORT_NAMES = {"nodes.csv", "edges.csv", "inputs.csv", "outputs.csv", "spends.csv",
                       "events.csv", "frontier.csv", "export.json", "SHA256SUMS"}
EXPORT_NAMES = {"transactions.csv", "export.json", "SHA256SUMS"}
PREVIEW_NAMES = {"graph.html", "graph.svg", "graph.mmd", "graph.json",
                 "mermaid-node-map.json", "mermaid-config.json"}
LAYOUT_NAMES = {"graph.html", "graph.svg", "graph.json", "layout-report.json"}
LAYOUT_DETAIL_NAMES = {"details.html", "details.json"}
COMPACTION_NAMES = LAYOUT_NAMES | {"before.html", "before.svg", "before.json", "compaction.json", "SHA256SUMS"}
LAYOUT_ALGORITHMS = ("elk_layered_v1", "dependency_layers_v1")
FALLBACK_REASONS = ("size_limit", "timeout", "mermaid_size_limit", "mermaid_timeout")
from .connections import FILES as CONNECTION_NAMES, LEGACY_FILES as LEGACY_CONNECTION_NAMES, preview_files
CANCELLABLE_ACTIONS = {"layout", "mermaid", "compact", "connections", "pegouts", "pegouts-preview"}


def public_pegout_search(summary):
    """Expose the saved query and outcome, never archive paths or API errors."""
    from .pegouts import SEARCH_ID

    identity = summary.get("search_id", summary.get("id"))
    if not isinstance(identity, str) or not SEARCH_ID.fullmatch(identity):
        raise TraceError("Invalid peg-out search identifier")
    query = summary.get("query", summary)
    txid, lower, upper = query.get("txid"), query.get("min_hops"), query.get("max_hops")
    if (not isinstance(txid, str) or not re.fullmatch(r"[0-9a-f]{64}", txid)
            or type(lower) is not int or type(upper) is not int
            or not 0 <= lower <= upper <= 2147483647):
        raise TraceError("Invalid saved peg-out query")
    value = {"id": identity, "search_id": identity, "txid": txid,
             "min_hops": lower, "max_hops": upper}
    for key in ("status", "stop_reason"):
        field = summary.get(key)
        if field is None or isinstance(field, str) and re.fullmatch(r"[a-z_]{1,64}", field):
            value[key] = field
    for key in ("match_count", "transaction_count"):
        field = summary.get(key)
        if type(field) is int and 0 <= field <= 2 ** 53 - 1:
            value[key] = field
    for key in ("resumable", "recoverable", "complete", "partial"):
        if type(summary.get(key)) is bool:
            value[key] = summary[key]
    created = summary.get("created_at")
    if isinstance(created, str) and re.fullmatch(r"[0-9TtZz:+. -]{10,40}", created):
        value["created_at"] = created
    return value


def public_graph_options(options):
    if not isinstance(options, dict):
        return None
    try:
        settings = validate_settings({key: options[key] for key in ("group_context_inputs", "hub_addresses", "center_name", "color_attribution_arrows") if key in options})
    except TraceError:
        return None
    result = {key: settings[key] for key in ("group_context_inputs", "hub_addresses", "center_name", "color_attribution_arrows")}
    # Old artifacts did not record a search budget. Do not claim the current
    # default was used to calculate those saved coordinates.
    if "layout_attempts" in options:
        try:
            if options["layout_attempts"] is None:
                return None
            result["layout_attempts"] = normalize_layout_attempts(options["layout_attempts"])
        except TraceError:
            return None
    return result


def public_service(rule):
    """Expose only the investigator's designation, never settings/audit internals."""
    if not isinstance(rule, dict):
        return None
    from .services import rule_fields, notes_for
    return {key: rule[key] for key in ("address", "name", "enabled", "created_at", "updated_at") if key in rule} | rule_fields(rule) | {"notes": notes_for(rule)}



def public_address_activity(summary):
    """Whitelist successful observations; exclude raw API bodies, errors and paths."""
    from .address_activity import validate_address

    value = {"address": validate_address(summary.get("address")), "schema_version": 1}
    for key in ("confirmed_tx_count", "mempool_tx_count", "confirmed_unspent_output_count",
                "unspent_output_count", "history_pages", "max_pages", "history_transactions_seen"):
        count = summary.get(key)
        value[key] = count if type(count) is int and 0 <= count <= 2 ** 53 - 1 else None
    delta = summary.get("mempool_unspent_output_delta")
    value["mempool_unspent_output_delta"] = delta if type(delta) is int and abs(delta) <= 2 ** 53 - 1 else None
    for key in ("history_complete", "output_counts_consistent"):
        value[key] = summary.get(key) is True
    for key in ("observed_at", "completed_at"):
        stamp = summary.get(key)
        value[key] = stamp if isinstance(stamp, str) and re.fullmatch(r"[0-9TtZz:+. -]{10,40}", stamp) else None
    reasons = {"complete", "page_limit", "no_confirmed_transactions", "request_limit", "time_limit",
               "server_retry_later", "interrupted", "lookup_limit", "repeated_history", "snapshot_inconsistent"}
    value["history_stop_reason"] = summary.get("history_stop_reason") if summary.get("history_stop_reason") in reasons else "unavailable"
    for key in ("first_confirmed_activity", "oldest_observed_confirmed_activity", "latest_confirmed_activity"):
        activity = summary.get(key)
        value[key] = None
        if isinstance(activity, dict) and isinstance(activity.get("txid"), str) and re.fullmatch(r"[0-9a-f]{64}", activity["txid"]):
            stamp = activity.get("date_utc")
            value[key] = {"txid": activity["txid"], "date_utc": stamp if isinstance(stamp, str)
                          and re.fullmatch(r"[0-9TtZz:+. -]{10,40}", stamp) else None}
    value["observation_ids"] = [item for item in summary.get("observation_ids", [])
                                if type(item) is int and 0 < item <= 2 ** 53 - 1]
    warnings = {
        "Address statistics are inconsistent; negative derived output counts are unavailable.",
        "Confirmed history and statistics may have changed during lookup; the first confirmed activity is unverified.",
        "Some confirmed transactions have no block timestamp; their dates are unavailable.",
    }
    value["warnings"] = [item for item in summary.get("warnings", []) if isinstance(item, str) and item in warnings]
    return value


def public_rendering_metadata(result):
    """Only controlled renderer names and reasons may cross the browser boundary."""
    value = {}
    if result.get("layout_algorithm") in LAYOUT_ALGORITHMS:
        value["layout_algorithm"] = result["layout_algorithm"]
    if result.get("renderer") == "direct_svg":
        value["renderer"] = "direct_svg"
    if result.get("fallback_reason") in FALLBACK_REASONS:
        value["fallback_reason"] = result["fallback_reason"]
    return value


def public_frame_recovery(result, *, review):
    """A reviewed frame exposes geometry and candidate IDs, never journal data."""
    error = "Miro frame recovery returned an invalid report. Review the interrupted frame again."
    run_id = result.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise TraceError(error)
    resume = {}
    if "resume_action" in result:
        if result["resume_action"] not in ("miro-frames", "miro-sync"):
            raise TraceError(error)
        resume["resume_action"] = result["resume_action"]
    if not review:
        if (result.get("recovery") not in ("adopted_frame", "confirmed_absent_frame")
                or type(result.get("resolved_count")) is not int or result["resolved_count"] != 1
                or type(result.get("remaining_pending")) is not int or result["remaining_pending"] != 0):
            raise TraceError(error)
        return {"recovery": result["recovery"], "run_id": run_id,
                "resolved_count": 1, "remaining_pending": 0, **resume}

    review_id = result.get("review_id")
    potential = result.get("potential_match_count")
    if (type(result.get("schema_version")) is not int or result["schema_version"] != 1
            or result.get("recovery") != "pending_frame_review"
            or not isinstance(review_id, str) or not FRAME_REVIEW_ID.fullmatch(review_id)
            or type(potential) is not int or not 0 <= potential <= 2 ** 53 - 1
            or type(result.get("can_confirm_absent")) is not bool
            or not isinstance(result.get("candidates"), list)):
        raise TraceError(error)

    def frame(value, *, candidate=False):
        if (not isinstance(value, dict) or not isinstance(value.get("title"), str)
                or not 1 <= len(value["title"]) <= 6000):
            raise TraceError(error)
        clean = {"title": value["title"]}
        for key in ("x", "y", "width", "height"):
            number = value.get(key)
            if (type(number) not in (int, float)
                    or not -sys.float_info.max <= number <= sys.float_info.max
                    or (key in ("width", "height") and number <= 0)):
                raise TraceError(error)
            clean[key] = number
        if candidate:
            identity = value.get("id")
            if not isinstance(identity, str) or not MIRO_ITEM_ID.fullmatch(identity):
                raise TraceError(error)
            clean["id"] = identity
        return clean

    pending = frame(result.get("pending_frame"))
    candidates = [frame(item, candidate=True) for item in result["candidates"]]
    if (len({item["id"] for item in candidates}) != len(candidates)
            or (result["can_confirm_absent"] and (candidates or potential))):
        raise TraceError(error)
    return {"schema_version": 1, "recovery": "pending_frame_review", "review_id": review_id, **resume,
            "run_id": run_id, "pending_frame": pending, "candidates": candidates,
            "potential_match_count": potential, "can_confirm_absent": result["can_confirm_absent"]}


def public_layout_metrics(metrics):
    """Expose counts only, never copy arbitrary saved report fields to the UI."""
    if not isinstance(metrics, dict):
        return None
    result = {"estimated": True}
    attempts = metrics.get("attempt_count")
    if type(attempts) is int and 1 <= attempts <= MAX_LAYOUT_ATTEMPTS:
        result["attempt_count"] = attempts
    result.update(public_search_counts(metrics))
    for phase in ("before", "after"):
        values = metrics.get(phase)
        if not isinstance(values, dict):
            return None
        counts = {}
        for key in ("crossings", "node_overlaps", "node_intersections"):
            value = values.get(key)
            if type(value) is not int or not 0 <= value <= 2 ** 53 - 1:
                return None
            counts[key] = value
        counts["truncated"] = values.get("truncated") is True
        result[phase] = counts
    return result


def public_compaction_report(report):
    """Expose measured sizes and counts, not arbitrary preview metadata."""
    if not isinstance(report, dict):
        return None
    result = {}
    for phase in ("before", "after"):
        sizes = report.get(phase)
        if not isinstance(sizes, dict):
            return None
        result[phase] = {}
        for scope in ("main", "board"):
            measurements = sizes.get(scope)
            if not isinstance(measurements, dict):
                return None
            result[phase][scope] = {}
            for name in ("width", "height", "area", "edge_length", "address_distance"):
                value = measurements.get(name)
                if type(value) in (int, float) and 0 <= value <= 2 ** 53 - 1 and math.isfinite(value):
                    result[phase][scope][name] = value
                elif name in ("width", "height", "area"):
                    return None
    for name in ("moved_addresses", "moved_components", "accepted_moves", "skipped_moves"):
        value = report.get(name)
        if type(value) is int and 0 <= value <= 2 ** 53 - 1:
            result[name] = value
    for name in ("truncated", "unchanged"):
        result[name] = report.get(name) is True
    return result


class RequestError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


class JobCancelled(Exception):
    """A requested cancellation, separate from a CLI or credential error."""


def stop_worker(process):
    """Let the CLI unwind its separate renderer groups before killing it."""
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # The worker may exit between the timeout and escalation.
        process.wait()


def safe_path(root, parts):
    """Resolve only ordinary descendants; never follow a symlink at any level."""
    path = root
    for part in parts:
        if not part or part in (".", "..") or "/" in part or "\\" in part:
            raise RequestError("File not found", 404)
        path = path / part
        if path.is_symlink():
            raise RequestError("File not found", 404)
    if not path.resolve().is_relative_to(root.resolve()):
        raise RequestError("File not found", 404)
    return path


def worker_command(request, result, live=False):
    command = _command([], live=live)
    command[-1] = "liquid_tracer.web_worker"
    return [*command, str(request), str(result)]


def handoff_terminal(process, live):
    """Give a provider the existing controlling terminal, without a new session."""
    if not live or not sys.stdin.isatty():
        return None
    fd = sys.stdin.fileno()
    previous = os.tcgetpgrp(fd)
    os.tcsetpgrp(fd, process.pid)
    # Always preserve restoration state once the handoff succeeds, including a
    # provider that exits between tcsetpgrp and SIGCONT.
    try:
        os.killpg(process.pid, signal.SIGCONT)
    except ProcessLookupError:
        pass
    return fd, previous


def restore_terminal(terminal):
    if terminal is not None:
        try:
            os.tcsetpgrp(*terminal)
        except OSError:
            pass


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, root, assets, port=4321):
        self.root = Path(root).expanduser().resolve()
        self.assets = Path(assets).expanduser().resolve()
        self.csrf = secrets.token_urlsafe(32)
        self.jobs = {}
        self.active_job = None
        self.job_lock = threading.RLock()
        self.process = None
        self.closing = False
        self.job_thread = None
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = "http://127.0.0.1:" + str(self.server_port)
        self.host = "127.0.0.1:" + str(self.server_port)

    def case(self, identity):
        if not CASE_ID.fullmatch(identity):
            raise RequestError("Investigation not found", 404)
        for case in self.case_paths():
            try:
                metadata = read_case(case)
                if metadata["case_id"] == identity:
                    return case, metadata
            except (TraceError, OSError, ValueError):
                continue
        raise RequestError("Investigation not found", 404)

    def case_paths(self):
        if not self.root.is_dir():
            return []
        return sorted(path for path in self.root.iterdir()
                      if path.is_dir() and not path.is_symlink()
                      and (path / "case.json").is_file() and not (path / "case.json").is_symlink())

    def case_summary(self, case, metadata, detail=False):
        summary = {"id": metadata["case_id"], "name": metadata.get("name") or case.name,
                   "created_at": metadata.get("created_at"), "latest_run": metadata.get("latest_run"),
                   "fixture": bool(metadata.get("fixture")), "miro_board": metadata.get("miro_board"),
                   "run_defaults": validate_settings(metadata.get("run_defaults", {})),
                   "seed_count": len(metadata["seeds"]) if isinstance(metadata.get("seeds"), list) else 0,
                   "status": "Not started"}
        runs = []
        directory = safe_path(case, ["runs"])
        if directory.is_dir():
            for path in directory.iterdir():
                if not RUN_ID.fullmatch(path.name) or path.is_symlink() or not path.is_dir():
                    continue
                try:
                    manifest = safe_path(case, ["runs", path.name, "SHA256SUMS"])
                    if not manifest.is_file():
                        continue
                    state = read_json(safe_path(case, ["runs", path.name, "trace.json"]))
                    if not isinstance(state, dict):
                        continue
                    if state.get("run_id") != path.name or state.get("case_id") != metadata["case_id"]:
                        continue
                    stats = state.get("stats", {})
                    if not isinstance(stats, dict):
                        continue
                    run = {"id": path.name, "status": state.get("status"),
                           "stop_reason": state.get("stop_reason"), "created_at": state.get("started_at"),
                           "transaction_count": stats.get("transactions_cumulative", len(state.get("transactions", {}))),
                           "frontier_count": stats.get("frontier_count", 0)}
                    runs.append(run)
                    if path.name == summary["latest_run"]:
                        summary["status"] = run["status"]
                        summary["latest"] = run
                except (TraceError, OSError, ValueError, TypeError):
                    continue
        if summary["latest_run"] and "latest" not in summary:
            summary["status"] = "Saved run unavailable"
        if detail:
            from .cli import miro_recovery_status
            from .board_rebuild import rebuild_status

            summary["runs"] = sorted(runs, key=lambda run: (run.get("created_at") or "", run["id"]), reverse=True)
            summary["artifacts"] = self.saved_artifacts(case, metadata, {run["id"] for run in runs})
            summary["pegout_searches"] = self.pegout_searches(case)
            summary["miro_recovery"] = miro_recovery_status(case)
            try:
                rebuild = rebuild_status(case)
            except (TraceError, OSError, ValueError, TypeError, KeyError):
                rebuild = {"status": "unavailable", "notice": "The saved board rebuild receipt is unavailable. "
                           "Restore it before rebuilding again. Your saved investigation remains available."}
            if rebuild:
                summary["miro_rebuild"] = {key: value for key, value in rebuild.items()
                    if key in {"status", "previous_board_id", "board_id", "run_id", "name", "notice"}
                    and isinstance(value, str)}
        return summary

    def pegout_artifact(self, case, preview_id):
        from .pegouts import reviewed_pegouts, preview_files as pegout_files

        graph, _ = reviewed_pegouts(case, preview_id)
        directory = safe_path(case, ["previews", preview_id])
        identity = read_case(case)["case_id"]
        product = {"preview_id": preview_id, "downloads": []}
        for name in sorted(pegout_files(directory)):
            path = safe_path(case, ["previews", preview_id, name])
            if not path.is_file():
                continue
            url = "/files/" + identity + "/previews/" + quote(preview_id) + "/" + quote(name)
            product["downloads"].append({"name": name, "url": url})
            if name == "graph.html":
                product["preview_url"] = url
        product.update(public_graph_options(graph.get("graph_options", {})) or {})
        layout = graph.get("layout", {})
        metrics = public_layout_metrics(layout.get("metrics")) if isinstance(layout, dict) else None
        if metrics is not None:
            product["layout_metrics"] = metrics
        return product

    def pegout_searches(self, case):
        from .pegouts import list_pegout_searches

        searches = []
        for summary in list_pegout_searches(case):
            try:
                item = public_pegout_search(summary)
            except (TraceError, ValueError, TypeError, AttributeError):
                continue
            if summary.get("preview_id"):
                try:
                    item["artifact"] = self.pegout_artifact(case, summary["preview_id"])
                except (TraceError, RequestError, OSError, ValueError, TypeError, KeyError):
                    pass  # Search evidence remains resumable when a preview is stale.
            searches.append(item)
        return searches

    def saved_artifacts(self, case, metadata, runs):
        """Rediscover complete local products without relying on browser memory.

        Check identities and expected files, skipping partial exports and links.
        Never return absolute paths from an export's provenance metadata.
        """
        artifacts, newest = {}, {}
        for folder, kind, names, metadata_file in (
            ("previews", "mermaid", PREVIEW_NAMES, "graph.json"),
            ("previews", "elk", LAYOUT_NAMES, "graph.json"),
            ("previews", "compact", COMPACTION_NAMES, "graph.json"),
            ("previews", "connections", CONNECTION_NAMES, "graph.json"),
            ("exports", "csv", EXPORT_NAMES, "export.json"),
        ):
            try:
                parent = safe_path(case, [folder])
                directories = list(parent.iterdir()) if parent.is_dir() else []
            except (RequestError, OSError):
                continue
            for directory in directories:
                if (not ARTIFACT_DIR.fullmatch(directory.name) or directory.is_symlink()
                        or not directory.is_dir() or f"-{kind}-" not in directory.name):
                    continue
                run_id = directory.name[:16]
                if run_id not in runs:
                    continue
                try:
                    selected_names = preview_files(directory) if kind == "connections" else names
                    files = {name: self.artifact(case, [folder, directory.name, name]) for name in selected_names}
                    if not all(path.is_file() for path in files.values()):
                        continue
                    if kind == "connections":
                        from .connections import reviewed_connections
                        reviewed_connections(case, directory.name)
                    compact_meta = None
                    if kind == "compact":
                        from .cli import compaction_preview_metadata

                        compact_meta = compaction_preview_metadata(case, run_id, directory.name)
                        # Verification caches only small metadata. Reopening a
                        # large comparison need not parse either full graph.
                        layout_report = read_json(files["layout-report.json"])
                        if not isinstance(layout_report, dict):
                            continue
                        info = {**compact_meta, "graph_options": {
                            **compact_meta.get("graph_options", {}), "connector_style": compact_meta["connector_style"]},
                            "layout": layout_report.get("layout")}
                    else:
                        info = read_json(files[metadata_file])
                    if not isinstance(info, dict) or info.get("run_id") != run_id:
                        continue
                    namespace = info if kind == "csv" else info.get("namespace", {})
                    if not isinstance(namespace, dict) or namespace.get("case_id") != metadata["case_id"]:
                        continue
                    options = info.get("graph_options", {})
                    if not isinstance(options, dict):
                        continue
                    fees = info.get("include_fees", options.get("include_fees", True))
                    if type(fees) is not bool:
                        continue
                    # Completion file is written last. A partial later attempt
                    # cannot hide a previous complete, downloadable product.
                    finished = files["SHA256SUMS" if kind in ("csv", "compact") else "graph.html"].stat().st_mtime_ns
                    order = (finished, directory.name)
                    if order <= newest.get((run_id, kind), (-1, "")):
                        continue
                    exposed_names = selected_names | LAYOUT_DETAIL_NAMES if kind in ("elk", "compact") else selected_names
                    product = self.artifact_links(case, [folder, directory.name], exposed_names)
                    product["include_fees"] = fees
                    if kind != "csv":
                        display_options = public_graph_options(options)
                        if display_options is None:
                            continue
                        product.update(display_options)
                    if kind == "connections":
                        report = info["connections"]
                        product.update(preview_id=directory.name, max_hops=report["max_hops"],
                                       connection_count=report["connection_count"], connection_status=report["status"])
                        layout = info.get("layout")
                        metrics = public_layout_metrics(layout.get("metrics")) if isinstance(layout, dict) else None
                        if metrics is not None:
                            product["layout_metrics"] = metrics
                    if kind in ("elk", "compact"):
                        style = options.get("connector_style")
                        layout = info.get("layout")
                        if (not isinstance(style, str) or style not in ("straight", "curved", "elbowed")
                                or not isinstance(layout, dict) or layout.get("algorithm") not in LAYOUT_ALGORITHMS):
                            continue
                        product["connector_style"] = style
                        product.update(public_rendering_metadata({
                            "layout_algorithm": layout.get("algorithm"),
                            "fallback_reason": layout.get("fallback_reason")}))
                        metrics = public_layout_metrics(layout.get("metrics"))
                        if metrics is not None:
                            product["layout_metrics"] = metrics
                        if compact_meta is not None:
                            report = public_compaction_report(compact_meta.get("compaction"))
                            if report is None:
                                continue
                            product["compaction"] = report
                            product["preview_id"] = directory.name
                    elif kind == "mermaid":
                        preview = info.get("preview", {})
                        if isinstance(preview, dict):
                            product.update(public_rendering_metadata({
                                "renderer": preview.get("renderer"),
                                "fallback_reason": preview.get("reason")}))
                    artifacts.setdefault(run_id, {})[kind] = product
                    newest[(run_id, kind)] = order
                except (RequestError, TraceError, OSError, ValueError, TypeError):
                    continue
        return artifacts

    def artifact_links(self, case, relative, names):
        product = {"downloads": []}
        case_id = read_case(case)["case_id"]
        for name in sorted(names):
            parts = [*relative, name]
            if self.artifact(case, parts).is_file():
                url = "/files/" + case_id + "/" + "/".join(map(quote, parts))
                product["downloads"].append({"name": name, "url": url})
                if name == "graph.html":
                    product["preview_url"] = url
        return product

    def session(self):
        cases = []
        for case in self.case_paths():
            try:
                cases.append(self.case_summary(case, read_case(case)))
            except (TraceError, OSError, ValueError, RequestError):
                continue
        return {"csrf": self.csrf, "settings": load_settings(self.root),
                "cases": cases, "active_job": self.active_job}

    def ensure_idle(self):
        if self.active_job:
            raise RequestError("An action is already running. Wait for it to finish.", 409)
        if self.closing:
            raise RequestError("The local server is shutting down.", 503)

    def start_job(self, arguments, *, action, live=False, case=None, txids=None):
        self.ensure_idle()
        case_id = read_case(case)["case_id"] if case is not None else None
        identity = secrets.token_hex(16)
        self.jobs[identity] = {"id": identity, "status": "running", "action": action,
                               "case_id": case_id, "live": bool(live),
                               "started_at": time.time(),
                               "cancellable": action in CANCELLABLE_ACTIONS,
                               "message": ("Working. Check the launching terminal if Proton Pass needs to unlock."
                                           if live else "Preparing the graph and checking missing address counts…"
                                           if action in CANCELLABLE_ACTIONS else "Working with saved local evidence…")}
        self.active_job = identity
        # Keep a bounded history for tabs that remain open. Evidence persists in
        # the case, independently of this transient browser job history.
        while len(self.jobs) > 128:
            self.jobs.pop(next(iter(self.jobs)))
        self.job_thread = threading.Thread(target=self.run_job,
            args=(identity, arguments, action, live, case, txids), daemon=True)
        self.job_thread.start()
        return dict(self.jobs[identity])

    def cancel_job(self, identity):
        """Called under job_lock, so only the identified active job can stop."""
        if not CASE_ID.fullmatch(identity) or identity not in self.jobs:
            raise RequestError("Job unavailable. Refresh the page to review active work.", 404)
        job = self.jobs[identity]
        if self.active_job != identity or job["status"] not in ("running", "cancelling"):
            raise RequestError("This action has already finished. Refresh the investigation.", 409)
        if job["action"] not in CANCELLABLE_ACTIONS:
            raise RequestError("Only chart preparation and layout calculations can be canceled here.", 409)
        if job["status"] == "cancelling":
            return dict(job)
        # If completion already won, leave its result available to the browser.
        if self.process is not None and self.process.poll() is not None:
            job["cancellable"] = False
            return dict(job)
        job.update(status="cancelling", cancellable=False,
                   message="Canceling the calculation and stopping its renderer…")
        return dict(job)

    def run_job(self, identity, arguments, action, live, case, txids):
        terminal = None
        process = None
        try:
            with tempfile.TemporaryDirectory(prefix="liquid-web-job-") as directory:
                request, result = Path(directory) / "request.json", Path(directory) / "result.json"
                request.write_text(json.dumps({"arguments": arguments}), encoding="utf-8")
                request.chmod(0o600)
                options = {"process_group": 0} if live else {"start_new_session": True}
                # Inherit the terminal. Provider prompts and diagnostics are not
                # captured into browser-readable job output.
                with self.job_lock:
                    if self.closing:
                        raise RuntimeError("Server is shutting down")
                    if self.jobs[identity]["status"] == "cancelling":
                        raise JobCancelled
                    process = subprocess.Popen(worker_command(request, result, live), cwd=_project(),
                                               env=_environment(), **options)
                    self.process = process
                terminal = handoff_terminal(process, live)
                progress_path = Path(directory) / "progress.json"
                while True:
                    with self.job_lock:
                        cancelling = self.jobs[identity]["status"] == "cancelling"
                        closing = self.closing
                    if cancelling or closing:
                        stop_worker(process)
                        if cancelling:
                            raise JobCancelled
                        raise RuntimeError("Server is shutting down")
                    try:
                        status = process.wait(timeout=.25)
                    except subprocess.TimeoutExpired:
                        self.read_progress(identity, progress_path)
                    else:
                        self.read_progress(identity, progress_path)
                        break
                restore_terminal(terminal)
                terminal = None
                with self.job_lock:
                    if self.jobs[identity]["status"] == "cancelling":
                        raise JobCancelled
                if status != 0:
                    raise RuntimeError("CLI action failed")
                report = read_json(result)
                if report.get("ok") is not True or not isinstance(report.get("result"), dict):
                    raise RuntimeError("Invalid action result")
                value = self.public_result(report["result"], action, case, txids)
            with self.job_lock:
                self.jobs[identity].update(status="succeeded", cancellable=False,
                    message=(("Frame recovery complete. Choose Create / update Miro frames to resume."
                              if value.get("resume_action") == "miro-frames" else
                              "Frame recovery complete. Finish Sync to Miro, then create or update frames.") if action == "miro-frame-recover"
                             else "Frame review ready. Inspect the linked board before choosing a recovery." if action == "miro-frame-review"
                             else "Recovery complete. Choose Sync to Miro to resume." if action == "miro-recover"
                             else "Action completed."), result=value)
        except JobCancelled:
            with self.job_lock:
                self.jobs[identity].update(status="canceled", cancellable=False,
                    message="Calculation canceled. Saved investigation runs are unchanged.")
        except Exception as error:
            print("Local UI action failed: " + str(error), file=sys.stderr)
            with self.job_lock:
                self.jobs[identity].update(status="failed", cancellable=False, message=(
                    "Action failed. Check the launching terminal for credential, API, or saved-file errors. "
                    "Review the investigation before retrying a live action."))
        finally:
            try:
                stop_worker(process)
            finally:
                restore_terminal(terminal)
                with self.job_lock:
                    if self.active_job == identity:
                        self.process = None
                        self.active_job = None

    def read_progress(self, identity, path):
        try:
            # The worker writes atomically inside its private temporary folder.
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 4096:
                return
            value = public_progress(read_json(path))
            if value is not None:
                with self.job_lock:
                    self.jobs[identity]["progress"] = value
        except (OSError, ValueError, TypeError):
            pass

    def public_result(self, result, action, case, txids):
        if action in ("pegouts", "pegouts-preview"):
            value = public_pegout_search(result)
            if result.get("preview_id"):
                value["artifact"] = self.pegout_artifact(case, result["preview_id"])
            return value
        if action in ("miro-frame-review", "miro-frame-recover"):
            from .cli import board_id

            value = public_frame_recovery(result, review=action == "miro-frame-review")
            # The linked board belongs to this case; worker output cannot add a
            # browser navigation target or expose a private API URL.
            board = read_case(case).get("miro_board")
            if board:
                value["board_url"] = "https://miro.com/app/board/" + quote(board_id(board), safe="") + "/"
            return value
        if action == "change-output-lookup":
            from .change_outputs import MAX_VOUT, NOTICE

            transaction = _lookup_reports(result, txids)[0]
            revision, current = result.get("revision"), result.get("current_vout")
            if (type(revision) is not int or not 0 <= revision <= 2 ** 53 - 1
                    or (current is not None and (type(current) is not int
                        or not 0 <= current <= MAX_VOUT))
                    or not isinstance(result.get("current_notes"), str)):
                raise TraceError("Change-output lookup returned an invalid selection report.")
            fields = ("vout", "outpoint", "address", "value", "asset", "script_type", "selectable", "reason")
            return {"txid": transaction["txid"], "outputs": [
                {**{key: output.get(key) for key in fields},
                 "value_text": str(output["value"]) if type(output.get("value")) is int else None}
                for output in transaction["outputs"]], "revision": revision,
                "current_vout": current, "current_notes": result["current_notes"], "notice": NOTICE}
        if action == "address-counts":
            return {key:result[key] for key in ("run_id", "fetched", "known", "total", "remaining", "requests_this_lookup", "stop_reason") if key in result}
        if action == "address-inspect":
            return public_address_activity(result)
        if action == "address-merge":
            fields = ("run_id", "board_id", "converted", "sync_required", "address_objects_before",
                      "address_objects_after", "duplicates_to_remove", "connectors_to_redirect")
            return {key: result[key] for key in fields if key in result
                    and isinstance(result[key], (str, int, bool))}
        if action == "lookup":
            # inspect-txs always uses a transactions wrapper, including one hash.
            report = result.get("transactions", [])
            validated = _lookup_reports(report[0] if len(txids) == 1 and report else result, txids)
            # JavaScript numbers cannot exactly represent every explicit Liquid
            # amount. Keep the numeric field for compatibility and provide its
            # exact decimal representation for display in the browser.
            return {"transactions": [{**transaction, "outputs": [
                {**output, "value_text": str(output["value"]) if type(output.get("value")) is int else None}
                for output in transaction["outputs"]]} for transaction in validated]}
        # Never return absolute local paths or raw trace errors to a page. Only
        # known successful report fields and controlled artifact URLs cross here.
        fields = {"run_id", "status", "stop_reason", "include_fees", "board_id", "board_url",
                  "created", "reused", "name", "visibility", "new_shapes", "new_connectors", "new_frames",
                  "mapped_frames", "frames_to_remove",
                  "mapped_shapes", "mapped_connectors", "new_items", "updated", "deleted", "moved",
                  "reattached", "dry_run", "reorganize", "presentation_refreshed", "fee_items_to_remove", "run_notes_to_remove",
                  "existing_items", "items", "runs", "max_items", "remote_preflight_required"}
        value = {key: item for key, item in result.items()
                 if key in fields and (item is None or isinstance(item, (str, int, float, bool)))}
        if action == "miro-rebuild":
            from .cli import board_id

            # Construct links from validated IDs, never navigate to a worker URL.
            for key, url_key in (("board_id", "board_url"), ("previous_board_id", "previous_board_url")):
                identity = board_id(result.get(key))
                value[key] = identity
                value[url_key] = "https://miro.com/app/board/" + quote(identity, safe="") + "/"
            if result.get("rebuild_status") in ("complete", "created", "syncing"):
                value["rebuild_status"] = result["rebuild_status"]
        if type(result.get("frames_only")) is bool:
            value["frames_only"] = result["frames_only"]
        for key in ("created_frames", "updated_frames"):
            if type(result.get(key)) is int and 0 <= result[key] <= 2 ** 53 - 1:
                value[key] = result[key]
        if action == "miro-recover":
            value.pop("run_id", None)
        if action == "miro-recover" and result.get("recovery") == "confirmed_empty_board":
            recovered = result.get("recovered_items")
            if (type(recovered) is int and 1 <= recovered <= 20
                    and type(result.get("shape_batch_size")) is int and result["shape_batch_size"] == 1):
                value.update(recovery="confirmed_empty_board", recovered_items=recovered, shape_batch_size=1)
                if isinstance(result.get("run_id"), str) and RUN_ID.fullmatch(result["run_id"]):
                    value["run_id"] = result["run_id"]
        if isinstance(result.get("conflicts"), (list, dict)):
            value["conflicts_count"] = len(result["conflicts"])
        if isinstance(result.get("stats"), dict):
            value["stats"] = {key: item for key, item in result["stats"].items()
                              if isinstance(item, (int, float)) and not isinstance(item, bool)}
        if result.get("connector_style") in ("straight", "curved", "elbowed"):
            value["connector_style"] = result["connector_style"]
        options = result.get("graph_options", {})
        if isinstance(options, dict):
            options = {**options, **{key: result[key] for key in ("group_context_inputs", "hub_addresses", "center_name", "layout_attempts", "color_attribution_arrows") if key in result}}
            display_options = public_graph_options(options)
            if display_options is not None:
                value.update({key: item for key, item in display_options.items() if key in options})
        from .address_counts import public_count_report
        counts = public_count_report(result.get("address_counts"))
        if counts is not None:
            value["address_counts"] = counts
        value.update(public_rendering_metadata(result))
        metrics = public_layout_metrics(result.get("layout_metrics"))
        if metrics is not None:
            value["layout_metrics"] = metrics
        if action in ("mermaid", "csv", "layout", "compact", "connections"):
            directory = Path(result["directory"])
            relative = directory.relative_to(case)
            names = {"mermaid": PREVIEW_NAMES, "csv": EXPORT_NAMES, "layout": LAYOUT_NAMES,
                     "compact": COMPACTION_NAMES, "connections": CONNECTION_NAMES}[action]
            if action in ("layout", "compact"):
                names = names | LAYOUT_DETAIL_NAMES
            if action == "connections":
                names = preview_files(directory)
                from .connections import reviewed_connections
                graph, _ = reviewed_connections(case, directory.name)
                report = graph["connections"]
                value.update(preview_id=directory.name, max_hops=report["max_hops"],
                             connection_count=report["connection_count"], connection_status=report["status"])
            if action == "compact":
                from .cli import compaction_preview_metadata

                if not COMPACTION_DIR.fullmatch(directory.name):
                    raise RequestError("Invalid compact preview identifier.")
                meta = compaction_preview_metadata(case, result["run_id"], directory.name)
                value["preview_id"] = directory.name
                value.update(public_graph_options(meta.get("graph_options", {})) or {})
                report = public_compaction_report(meta.get("compaction"))
                if report is not None:
                    value["compaction"] = report
            value.update(self.artifact_links(case, relative.parts, names))
        return value

    @staticmethod
    def artifact(case, parts):
        if len(parts) != 3 or parts[0] not in ("previews", "exports") or not ARTIFACT_DIR.fullmatch(parts[1]):
            raise RequestError("File not found", 404)
        kind = parts[1].split("-")[1]
        if (parts[0] == "exports") != (kind == "csv"):
            raise RequestError("File not found", 404)
        if kind == "pegouts":
            from .pegouts import reviewed_pegouts, preview_files as pegout_files

            directory = safe_path(case, parts[:2])
            if parts[2] not in pegout_files(directory):
                raise RequestError("File not found", 404)
            reviewed_pegouts(case, parts[1])
            return safe_path(case, parts)
        expected = {"mermaid": PREVIEW_NAMES, "csv": EXPORT_NAMES | LEGACY_EXPORT_NAMES, "elk": LAYOUT_NAMES,
                    "compact": COMPACTION_NAMES, "connections": CONNECTION_NAMES | LEGACY_CONNECTION_NAMES}[kind]
        if kind in ("elk", "compact", "connections"):
            expected = expected | LAYOUT_DETAIL_NAMES
        if parts[2] not in expected:
            raise RequestError("File not found", 404)
        return safe_path(case, parts)

    def address_merge_preview(self, case):
        from .address_migration import preview_merge
        report = preview_merge(case)
        # No physical item mappings, full evidence, or local paths in the page.
        fields = ("approval_sha256", "board_id", "run_id", "address_objects_before",
                  "address_objects_after", "duplicates_to_remove", "connectors_to_redirect",
                  "resume", "notice", "remote_preflight_required")
        return {key: report[key] for key in fields}

    def action(self, case, metadata, body):
        from .boards import board_options, default_board_name
        from .cli import miro_recovery_status, resolve_latest, run_path, verify_export

        action = body.get("action")
        if action in ("pegouts", "pegouts-preview", "miro-pegouts"):
            from .pegouts import SEARCH_ID, reviewed_pegouts
            from .cli import board_id

            live = action == "pegouts" and not bool(metadata.get("fixture"))
            if action == "pegouts":
                arguments = ["pegouts", "--case", str(case)]
                if "resume" in body:
                    if set(body) != {"action", "resume"}:
                        raise RequestError("Resume uses the saved peg-out origin and hop range.")
                    identity = body.get("resume")
                    if not isinstance(identity, str) or not SEARCH_ID.fullmatch(identity):
                        raise RequestError("Choose a saved peg-out search to resume.")
                    if not safe_path(case, ["pegouts", identity]).is_dir():
                        raise RequestError("Peg-out search not found.")
                    arguments.extend(["--resume", identity])
                else:
                    if set(body) != {"action", "txid", "min_hops", "max_hops"}:
                        raise RequestError("Peg-out searches accept one transaction and an inclusive hop range.")
                    txid = body.get("txid")
                    if not isinstance(txid, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", txid.strip()):
                        raise RequestError("Enter one transaction hash containing 64 hexadecimal characters.")
                    lower, upper = body.get("min_hops"), body.get("max_hops")
                    if (type(lower) is not int or type(upper) is not int
                            or not 0 <= lower <= upper <= 2147483647):
                        raise RequestError("Enter whole-number hops from 0 to 2147483647, with minimum no greater than maximum.")
                    arguments.extend(["--txid", txid.strip().lower(), "--min-hops", str(lower), "--max-hops", str(upper)])
            elif action == "pegouts-preview":
                identity = body.get("search_id")
                if set(body) != {"action", "search_id"} or not isinstance(identity, str) or not SEARCH_ID.fullmatch(identity):
                    raise RequestError("Choose a saved peg-out search to preview.")
                if not safe_path(case, ["pegouts", identity]).is_dir():
                    raise RequestError("Peg-out search not found.")
                arguments = ["pegouts-preview", "--case", str(case), "--search", identity]
            else:
                if set(body) != {"action", "preview_id", "board", "confirm_pegouts"} or body.get("confirm_pegouts") is not True:
                    raise RequestError("Review the peg-out snapshot and confirm publication to a separate Miro board.")
                graph, _ = reviewed_pegouts(case, body.get("preview_id"))
                if not graph.get("nodes"):
                    raise RequestError("This peg-out snapshot has no matching paths to publish.")
                target = board_id(body.get("board"))
                if metadata.get("miro_board") and target == board_id(metadata["miro_board"]):
                    raise RequestError("Choose a separate Miro board; the full-trace board is protected.")
                settings = validate_settings(metadata.get("run_defaults", {}))
                arguments = ["pegouts-publish", "--case", str(case), "--preview", body["preview_id"],
                             "--board", target, "--max-items", str(settings["max_new_items"])]
                live = True
            return self.start_job(arguments, action=action, live=live, case=case)
        if action in ("miro-frame-review", "miro-frame-recover"):
            if action == "miro-frame-review":
                if set(body) != {"action"}:
                    raise RequestError("Frame review uses the linked board and interrupted run only.")
                arguments = ["miro-frame-review", "--case", str(case)]
            else:
                review_id = body.get("review_id")
                if not isinstance(review_id, str) or not FRAME_REVIEW_ID.fullmatch(review_id):
                    raise RequestError("Review the interrupted frame before choosing a recovery.")
                arguments = ["miro-frame-recover", "--case", str(case), "--review-id", review_id]
                if set(body) == {"action", "review_id", "item_id"}:
                    identity = body["item_id"]
                    if not isinstance(identity, str) or not MIRO_ITEM_ID.fullmatch(identity):
                        raise RequestError("Choose an existing frame from the current review.")
                    arguments.extend(["--item-id", identity])
                elif set(body) == {"action", "review_id", "confirm_absent"} and body["confirm_absent"] is True:
                    arguments.append("--confirm-absent")
                else:
                    raise RequestError("Choose one reviewed frame or explicitly confirm that it is absent.")
            if not metadata.get("miro_board"):
                raise RequestError("Create or link a Miro board in investigation settings first.")
            if not miro_recovery_status(case).get("can_recover_frame"):
                raise RequestError("Frame recovery is unavailable. Review the pending Miro items before retrying.")
            return self.start_job(arguments, action=action, live=True, case=case)
        if action == "change-output-lookup":
            from .change_outputs import lookup_requires_network

            if set(body) != {"action", "txid"}:
                raise RequestError("Change-output lookup accepts one transaction ID only; not file paths or custom arguments.")
            txid = body.get("txid")
            if not isinstance(txid, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", txid):
                raise RequestError("Enter one transaction hash containing 64 hexadecimal characters, without :vout.")
            txid = txid.lower()
            try:
                live = lookup_requires_network(case, txid)
            except TraceError as error:
                raise RequestError(str(error)) from None
            arguments = ["change-output-lookup", "--case", str(case), "--txid", txid]
            return self.start_job(arguments, action=action, live=live, case=case, txids=[txid])
        if action == "miro-frames" and not set(body) <= {"action", "run_id"}:
            raise RequestError("Frame creation uses the selected saved run and linked board only.")
        if action == "miro-rebuild" and set(body) != {"action", "run_id", "source_board", "name", "max_new_items"}:
            raise RequestError("Rebuilding uses a saved run, the reviewed source board, a name and a new-item budget only.")
        settings = validate_settings(body.get("settings", metadata.get("run_defaults", {})))
        selected = body.get("run_id", "latest")
        live = False
        if action == "trace":
            if selected != "latest":
                raise RequestError("Continue from the latest saved run.")
            if "hops" in body:
                if "settings" in body:
                    raise RequestError("Choose a run hop allowance or legacy settings, not both.")
                settings = validate_settings({**settings, "hops": body["hops"]})
            arguments, live = _trace_arguments(case, metadata, settings)
            # Current UI actions use saved defaults and a one-run hop allowance.
            # Keep explicit legacy API settings compatible without rewriting
            # defaults every time an ordinary run is started.
            if "settings" in body:
                update_case(case, {"run_defaults": settings})
        elif action == "connections":
            from .connections import validate_hops
            hops = validate_hops(body.get("connection_hops", 10))
            selected = resolve_latest(case, selected)
            safe_path(case, ["runs", selected, "trace.json"])
            verify_export(run_path(case, selected))
            arguments = ["connections", "--case", str(case), "--run", selected, "--hops", str(hops)]
        elif action == "miro-connections":
            from .connections import reviewed_connections
            from .cli import board_id
            if body.get("confirm_connections") is not True:
                raise RequestError("Review the connection snapshot and confirm publication to a separate Miro board.")
            preview_id = body.get("preview_id")
            graph, _ = reviewed_connections(case, preview_id)
            selected = resolve_latest(case, selected)
            if graph["run_id"] != selected:
                raise RequestError("Select a connection preview of the chosen saved run.")
            target = board_id(body.get("board"))
            if metadata.get("miro_board") and target == board_id(metadata["miro_board"]):
                raise RequestError("Choose a separate Miro board; the full-trace board is protected.")
            arguments = ["connections-publish", "--case", str(case), "--preview", preview_id,
                         "--board", target, "--max-items", str(settings["max_new_items"])]
            live = bool(graph["nodes"])
        elif action == "address-counts":
            selected = resolve_latest(case, selected)
            safe_path(case, ["runs", selected, "trace.json"])
            verify_export(run_path(case, selected))
            arguments = ["address-counts", "--case", str(case), "--run", selected,
                         "--max-requests", str(settings["max_requests"]), "--max-seconds", str(settings["max_seconds"])]
            live = not bool(metadata.get("fixture"))
        elif action == "address-inspect":
            from .address_activity import validate_address

            address = validate_address(body.get("address"))
            if not isinstance(selected, str) or (selected != "latest" and not RUN_ID.fullmatch(selected)):
                raise RequestError("Choose a saved run for this address review.")
            if selected != "latest" or metadata.get("latest_run"):
                selected = resolve_latest(case, selected)
                safe_path(case, ["runs", selected, "trace.json"])
                verify_export(run_path(case, selected))
            arguments = ["address-inspect", "--case", str(case), "--address", address, "--run", selected,
                         "--max-pages", "5", "--max-requests", "10", "--max-seconds", "60"]
            live = not bool(metadata.get("fixture"))
        elif action == "miro-rebuild":
            from .cli import board_id

            if not metadata.get("miro_board"):
                raise RequestError("Create or link a Miro board first.")
            source = board_id(body.get("source_board"))
            if not isinstance(selected, str) or not RUN_ID.fullmatch(selected):
                raise RequestError("Choose a saved run for the rebuilt graph.")
            safe_path(case, ["runs", selected, "trace.json"])
            verify_export(run_path(case, selected))
            name = body.get("name")
            board_options(name, visibility="private")
            budget = body.get("max_new_items")
            if type(budget) is not int or not 0 <= budget <= 2 ** 53 - 1:
                raise RequestError("Enter a nonnegative whole-number budget for all shapes and connections.")
            arguments = ["miro-rebuild-board", "--case", str(case), "--run", selected,
                         "--source-board", source, "--name", name, "--max-new-items", str(budget)]
            live = True
        elif action == "miro-create":
            name = body.get("name") or default_board_name(metadata)
            board_options(name, visibility="private")
            if metadata.get("miro_board"):
                raise RequestError("A Miro board is already linked. Use Sync to Miro.")
            arguments = ["miro-create-board", "--case", str(case), "--name", name, "--visibility", "private"]
            live = True
        elif action == "address-merge":
            if body.get("confirm_merge") is not True:
                raise RequestError("Review the address conversion and confirm before changing Miro.")
            approval = body.get("approval_sha256")
            if not isinstance(approval, str) or not re.fullmatch(r"[0-9a-f]{64}", approval):
                raise RequestError("Choose a reviewed address conversion plan.")
            reviewed = self.address_merge_preview(case)
            if reviewed["approval_sha256"] != approval:
                raise RequestError("The address conversion changed; review a fresh preview.")
            arguments = ["miro-merge-addresses", "--case", str(case), "--board", reviewed["board_id"],
                         "--approve-plan", approval]
            live = True
        elif action == "miro-recover":
            if body.get("confirm_empty") is not True:
                raise RequestError("Inspect the linked Miro board and confirm that it is empty first.")
            if not metadata.get("miro_board"):
                raise RequestError("Create or link a Miro board in investigation settings first.")
            if not miro_recovery_status(case)["can_confirm_empty"]:
                raise RequestError("Empty-board recovery is unavailable. Review the pending Miro items before retrying.")
            arguments = ["miro-recover", "--case", str(case), "--confirm-empty"]
            live = True
        elif action in ("mermaid", "csv", "layout", "compact", "miro-preview", "miro-sync", "miro-organize", "miro-compact", "miro-frames"):
            if not isinstance(selected, str) or (selected != "latest" and not RUN_ID.fullmatch(selected)):
                raise RequestError("Choose a saved run for this graph.")
            selected = resolve_latest(case, selected)
            archive = run_path(case, selected)
            safe_path(case, ["runs", selected, "trace.json"])
            verify_export(archive)
            if action in ("mermaid", "csv", "layout", "compact"):
                arguments = [{"mermaid": "mermaid", "csv": "csv-export", "layout": "layout-preview",
                              "compact": "compact-preview"}[action],
                             "--case", str(case), "--run", selected]
            else:
                if not metadata.get("miro_board"):
                    raise RequestError("Create or link a Miro board in investigation settings first.")
                command = "miro-frames" if action == "miro-frames" else "miro-sync"
                arguments = [command, "--case", str(case), "--run", selected,
                             "--board", metadata["miro_board"], "--max-new-items", str(settings["max_new_items"])]
                if action == "miro-frames":
                    if miro_recovery_status(case)["pending_count"]:
                        raise RequestError("Recover the pending Miro items before creating or updating frames.")
                elif action == "miro-preview":
                    arguments.append("--dry-run")
                elif action == "miro-organize":
                    arguments.append("--reorganize")
                elif action == "miro-compact":
                    from .cli import verified_compaction_preview

                    identity = body.get("preview_id")
                    if (not isinstance(identity, str) or not COMPACTION_DIR.fullmatch(identity)
                            or identity[:16] != selected):
                        raise RequestError("Choose a compact preview of this saved run first.")
                    if miro_recovery_status(case)["pending_count"]:
                        raise RequestError("Recover the pending Miro items before applying a compact layout.")
                    verified_compaction_preview(case, selected, identity)
                    arguments.extend(["--compact-preview", identity, "--reorganize"])
                live = action != "miro-preview"
            if action not in ("miro-compact", "miro-frames"):
                arguments.append("--include-fees" if settings["include_fees"] else "--exclude-fees")
            if action not in ("mermaid", "csv", "miro-compact", "miro-frames"):
                arguments.extend(["--connector-style", settings["connector_style"]])
                arguments.extend(["--layout-attempts", str(settings["layout_attempts"])])
                arguments.append("--group-context-inputs" if settings["group_context_inputs"] else "--ungroup-context-inputs")
        else:
            raise RequestError("Choose a supported investigation action.")
        if action in CANCELLABLE_ACTIONS:
            from .address_counts import count_credentials_required
            live = count_credentials_required(case, selected)
        return self.start_job(arguments, action=action, live=live, case=case)

    def server_close(self):
        with self.job_lock:
            self.closing = True
        # The job thread owns process termination. A second SIGTERM from this
        # thread could interrupt cleanup and orphan a renderer in its own group.
        if self.job_thread is not None:
            self.job_thread.join()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = "LiquidLocal"

    def log_message(self, format, *args):
        # Do not log case identifiers, transaction hashes, or request bodies.
        pass

    def security(self, mutation=False):
        if self.headers.get_all("Host", []) != [self.server.host]:
            raise RequestError("Use the loopback address printed by liquid-web.", 403)
        origins = self.headers.get_all("Origin", [])
        if origins and origins != [self.server.origin]:
            raise RequestError("Cross-origin requests are not allowed.", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise RequestError("Cross-site requests are not allowed.", 403)
        if mutation:
            if origins != [self.server.origin]:
                raise RequestError("A same-origin browser request is required.", 403)
            token = self.headers.get("X-Liquid-CSRF", "")
            if not secrets.compare_digest(token, self.server.csrf):
                raise RequestError("Refresh the page before making changes.", 403)
            if self.headers.get_content_type() != "application/json":
                raise RequestError("Send an application/json request.", 415)

    def send(self, status, data, content_type="application/json; charset=utf-8", *, preview=False, download=None,
             explorer_links=False):
        raw = json.dumps(data).encode("utf-8") if content_type.startswith("application/json") and not isinstance(data, bytes) else data
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        preview_sandbox = "sandbox allow-popups allow-popups-to-escape-sandbox" if explorer_links else "sandbox"
        self.send_header("Content-Security-Policy", (
            preview_sandbox + "; default-src 'none'; img-src data:; style-src 'unsafe-inline'; frame-ancestors 'self'"
            if preview else "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'"))
        if download:
            self.send_header("Content-Disposition", 'attachment; filename="' + download + '"')
        self.end_headers()
        self.wfile.write(raw)

    def body(self, maximum=MAX_BODY):
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
            raise RequestError("A bounded JSON request body is required.")
        try:
            length = int(self.headers["Content-Length"])
        except ValueError:
            raise RequestError("Invalid request length.") from None
        if not 0 < length <= maximum:
            raise RequestError("Request is too large or empty.", 413)
        try:
            value = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            raise RequestError("Invalid JSON request.") from None
        if not isinstance(value, dict):
            raise RequestError("A JSON object is required.")
        return value

    def do_GET(self):
        self.dispatch(False)

    def do_POST(self):
        self.dispatch(True)

    def dispatch(self, mutation):
        try:
            self.connection.settimeout(10)
            self.security(mutation)
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
                raise RequestError("Route not found", 404)
            parts = unquote(parsed.path).strip("/").split("/")
            if mutation:
                # Only reviewed import routes accept larger, bounded text bodies.
                is_import = (len(parts) == 4 and parts[:2] == ["api", "cases"]
                             and parts[3] in ("address-import", "name-color-import", "change-output-import", "input-import"))
                # Three CSVs may each contain 512 KiB; JSON escaping can expand
                # their representation. Individual source limits still apply.
                import_limit = 12 * 1024 * 1024 if is_import and parts[3] == "input-import" else 4 * 1024 * 1024
                body = self.body(import_limit if is_import else MAX_BODY)
                with self.server.job_lock:
                    if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel":
                        result, status = self.server.cancel_job(parts[2]), 202
                    else:
                        self.server.ensure_idle()
                        result, status = self.post(parts, body)
                self.send(status, result)
            else:
                self.get(parts)
        except RequestError as error:
            self.send(error.status, {"error": error.message})
        except (TraceError, OSError, ValueError, KeyError, TypeError) as error:
            print("Local UI request failed: " + str(error), file=sys.stderr)
            self.send(400, {"error": "Request could not be completed. Check the entered values and saved investigation; details are in the launching terminal."})

    def get(self, parts):
        if parts == ["api", "session"]:
            self.send(200, self.server.session())
        elif len(parts) == 3 and parts[:2] == ["api", "cases"]:
            case, metadata = self.server.case(parts[2])
            self.send(200, self.server.case_summary(case, metadata, detail=True))
        elif len(parts) == 3 and parts[:2] == ["api", "jobs"]:
            with self.server.job_lock:
                job = self.server.jobs.get(parts[2])
                if job is None:
                    raise RequestError("Job unavailable. Refresh the investigation to review saved runs.", 404)
                self.send(200, dict(job))
        elif len(parts) == 5 and parts[:2] == ["api", "cases"] and parts[3] == "input-exports":
            if parts[4] not in {"attributions", "name-colors", "change-outputs", "all"}:
                raise RequestError("Input export not found", 404)
            from .input_export import build_input_export
            case, _ = self.server.case(parts[2])
            product = build_input_export(case, parts[4])
            self.send(200, product["data"], product["content_type"], download=product["filename"])
        elif len(parts) == 5 and parts[0] == "files":
            case, _ = self.server.case(parts[1])
            path = self.server.artifact(case, parts[2:])
            if not path.is_file():
                raise RequestError("File not found", 404)
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send(200, path.read_bytes(), content_type, preview=path.suffix in (".html", ".svg"),
                      download=None if path.suffix == ".html" else path.name,
                      explorer_links=parts[3].split("-")[1] in ("elk", "compact", "connections", "pegouts") and path.suffix in (".html", ".svg"))
        else:
            path = safe_path(self.server.assets, ["index.html"] if parts == [""] else parts)
            if not path.is_file():
                raise RequestError("Page not found", 404)
            self.send(200, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    def post(self, parts, body):
        if parts == ["api", "settings"]:
            return {"settings": save_settings(self.server.root, body.get("settings"))}, 200
        if parts == ["api", "lookup"]:
            if set(body) - {"txids", "source"}:
                raise RequestError("Transaction lookup accepts transaction IDs only; not fixture files or custom arguments.")
            if body.get("source", "live") != "live":
                raise RequestError("New transaction lookups use live Liquid data.")
            txids = parse_transaction_hashes(body.get("txids"))
            arguments = ["inspect-txs", "--txids", ",".join(txids)]
            return self.server.start_job(arguments, action="lookup", live=True, txids=txids), 202
        if parts == ["api", "cases"]:
            if set(body) - {"name", "seeds", "board", "settings", "source"}:
                raise RequestError("New investigations accept a name, starting outputs, board and settings only; not fixture files.")
            if body.get("source", "live") != "live":
                raise RequestError("New investigations use live Liquid data.")
            seeds = body.get("seeds")
            if not isinstance(seeds, list) or not seeds or any(not isinstance(seed, str) for seed in seeds):
                raise RequestError("Select at least one starting output.")
            normalized = _seed_values(" ".join(seeds))
            settings = validate_settings(body.get("settings", load_settings(self.server.root)))
            case = create_investigation(self.server.root, body.get("name"), seeds=normalized,
                board=body.get("board") or None, run_defaults=settings)
            return self.server.case_summary(case, read_case(case), detail=True), 201
        if len(parts) == 4 and parts[:2] == ["api", "cases"]:
            case, metadata = self.server.case(parts[2])
            if parts[3] == "change-outputs":
                from .change_outputs import catalog, set_change_output

                if set(body) <= {"query", "offset", "limit"}:
                    try:
                        return catalog(case, query=body.get("query", ""),
                            offset=body.get("offset", 0), limit=body.get("limit", 100)), 200
                    except TraceError as error:
                        raise RequestError(str(error)) from None
                if (set(body) - {"txid", "vout", "notes", "expected_revision"}
                        or not {"txid", "vout", "expected_revision"} <= set(body)):
                    raise RequestError("Change outputs accept a transaction search or one output selection with the current revision.")
                revision = body["expected_revision"]
                if type(revision) is not int or not 0 <= revision <= 2 ** 53 - 1:
                    raise RequestError("Saving a change output requires the current revision.")
                try:
                    return set_change_output(case, body["txid"], body["vout"],
                        notes=body.get("notes", ""), expected_revision=revision), 200
                except TraceError as error:
                    raise RequestError(str(error)) from None
            if parts[3] == "input-import":
                from .input_import import apply_import, preview_import

                if set(body) - {"files", "approve_plan"}:
                    raise RequestError("CSV import accepts uploaded files and approval only; not file paths.")
                try:
                    result = (apply_import(case, body.get("files"), approval_sha256=body["approve_plan"])
                              if "approve_plan" in body else preview_import(case, body.get("files")))
                except TraceError as error:
                    raise RequestError(str(error)) from None
                return result, 200
            if parts[3] == "change-output-import":
                from .change_output_import import apply_import, preview_import

                if set(body) - {"text", "format", "policy", "approve_plan"}:
                    raise RequestError("Change-output import accepts uploaded text, format, policy and approval only; not file paths.")
                options = {"format": body.get("format", "auto"), "policy": body.get("policy", "keep")}
                try:
                    result = (apply_import(case, body.get("text"), approval_sha256=body["approve_plan"], **options)
                              if "approve_plan" in body else preview_import(case, body.get("text"), **options))
                except TraceError as error:
                    raise RequestError(str(error)) from None
                return result, 200
            if parts[3] == "name-colors":
                from .name_colors import name_color_catalog, set_name_colors
                if set(body) - {"query", "offset", "limit", "updates", "expected_revision"}:
                    raise RequestError("Name colors accept a name search or reviewed color assignments only.")
                try:
                    if "updates" in body:
                        if set(body) != {"updates", "expected_revision"}:
                            raise RequestError("Saving colors requires updates and the current revision only.")
                        return set_name_colors(case, body["updates"], expected_revision=body["expected_revision"]), 200
                    if "expected_revision" in body:
                        raise RequestError("A color update is missing.")
                    return name_color_catalog(case, query=body.get("query", ""),
                        offset=body.get("offset", 0), limit=body.get("limit", 100)), 200
                except TraceError as error:
                    raise RequestError(str(error)) from None
            if parts[3] == "name-color-import":
                from .name_color_import import apply_import, preview_import
                if set(body) - {"text", "format", "policy", "approve_plan"}:
                    raise RequestError("Color import accepts uploaded text, format, policy and approval only; not file paths.")
                options = {"format": body.get("format", "auto"), "policy": body.get("policy", "keep")}
                try:
                    result = (apply_import(case, body.get("text"), approval_sha256=body["approve_plan"], **options)
                              if "approve_plan" in body else preview_import(case, body.get("text"), **options))
                except TraceError as error:
                    raise RequestError(str(error)) from None
                return result, 200
            if parts[3] == "address-import":
                from .address_import import apply_import, preview_import
                if set(body) - {"text", "format", "policy", "approve_plan"}:
                    raise RequestError("Import accepts uploaded text, format, policy and approval only; not file paths.")
                options = {"format": body.get("format", "auto"), "policy": body.get("policy", "keep")}
                try:
                    result = (apply_import(case, body.get("text"), approval_sha256=body["approve_plan"], **options)
                              if "approve_plan" in body else preview_import(case, body.get("text"), **options))
                except TraceError as error:
                    raise RequestError(str(error)) from None
                return result, 200
            if parts[3] == "address-merge-preview":
                return self.server.address_merge_preview(case), 200
            if parts[3] in ("addresses", "address", "services"):
                return self.address_request(parts[3], case, body), 200
            if parts[3] == "settings":
                updates = {"name": body.get("name", metadata.get("name")),
                           "miro_board": body.get("board", metadata.get("miro_board")) or None,
                           "run_defaults": body.get("settings", metadata.get("run_defaults", {}))}
                return self.server.case_summary(case, update_case(case, updates), detail=True), 200
            if parts[3] == "actions":
                return self.server.action(case, metadata, body), 202
        raise RequestError("Route not found", 404)

    def address_request(self, route, case, body):
        from .address_activity import validate_address
        from .address_review import list_addresses, saved_activity
        from .services import load_services, set_service

        if route == "addresses":
            selected = body.get("run_id", "latest")
            query = body.get("query", "")
            offset, limit = body.get("offset", 0), body.get("limit", 25)
            suspected = body.get("suspected_only", False)
            if (not isinstance(selected, str) or (selected != "latest" and not RUN_ID.fullmatch(selected))
                    or not isinstance(query, str) or len(query) > 256
                    or type(offset) is not int or not 0 <= offset <= 2 ** 53 - 1
                    or type(limit) is not int or not 1 <= limit <= 100
                    or type(suspected) is not bool):
                raise RequestError("Choose a saved run, a short search, and a page of 1 to 100 addresses.")
            result = list_addresses(case, run_id=selected, query=query, offset=offset,
                                    limit=limit, suspected_only=suspected)
            return {**{key: result[key] for key in ("run_id", "total", "offset", "limit")},
                    "rows": [{"address": row["address"], "run_output_count": row["run_output_count"],
                              "service": public_service(row.get("service")),
                              "activity": public_address_activity(row["activity"]) if row.get("activity") else None}
                             for row in result["rows"]]}
        address = validate_address(body.get("address"))
        if route == "address":
            selected = body.get("run_id", "latest")
            if not isinstance(selected, str) or (selected != "latest" and not RUN_ID.fullmatch(selected)):
                raise RequestError("Choose a saved run for this address review.")
            activity = saved_activity(case, address, run_id=selected)
            service = load_services(case)["rules"].get(address)
            return {"address": address, "service": public_service(service),
                    "activity": public_address_activity(activity) if activity else None}
        if type(body.get("enabled")) is not bool:
            raise RequestError("Choose whether this address assessment is enabled.")
        if "classification" in body:
            raise RequestError("Classification has been removed. Use confidence and stop_tracing independently.")
        settings = set_service(case, address, name=body.get("name"),
                               notes=body.get("notes", body.get("rationale")), enabled=body["enabled"],
                               **{key: body[key] for key in ("confidence", "source", "observed_at", "stop_tracing", "hop_limit") if key in body})
        return {"service": public_service(settings["rules"][address]), "revision": settings["revision"]}


def _interrupt(*_):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(description="Open the local Astro investigation interface")
    parser.add_argument("--root", type=Path, default=default_root(), help="Existing investigation directory")
    parser.add_argument("--assets", type=Path, default=_project() / "web" / "dist", help="Built Astro assets")
    parser.add_argument("--port", type=int, default=4321, help="Loopback port (default: 4321)")
    parser.add_argument("--no-open", action="store_true", help="Print the URL without opening a browser")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not (args.assets / "index.html").is_file():
        parser.error("Astro assets are missing. Start with liquid-web in the devenv shell to build them.")
    # The server remains alive while an interactive provider temporarily owns
    # the terminal. Ignoring SIGTTOU lets it restore the foreground after exit.
    previous = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    previous_term = signal.signal(signal.SIGTERM, _interrupt)
    server = None
    try:
        server = LocalServer(args.root, args.assets, args.port)
        print("Liquid Network Tracer: " + server.origin, flush=True)
        print("Keep this terminal open. Proton Pass prompts for live actions appear here.", flush=True)
        if not args.no_open:
            threading.Thread(target=webbrowser.open_new_tab, args=(server.origin,), daemon=True).start()
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    except OSError as error:
        print("Could not start local UI: " + str(error), file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.server_close()
        signal.signal(signal.SIGTTOU, previous)
        signal.signal(signal.SIGTERM, previous_term)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
