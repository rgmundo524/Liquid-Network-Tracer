"""Loopback-only Astro UI server using the existing investigation/CLI engine.

The browser chooses validated actions. It never supplies executable arguments,
filesystem paths, API credentials, or a SecretSpec provider configuration.
"""

import argparse
import json
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

MAX_BODY = 64 * 1024
CASE_ID = re.compile(r"[0-9a-f]{32}")
RUN_ID = re.compile(r"[a-zA-Z0-9]{16}")
ARTIFACT_DIR = re.compile(r"[a-zA-Z0-9]{16}-(?:csv|mermaid|elk)-[0-9a-f]{8}")
EXPORT_NAMES = {"nodes.csv", "edges.csv", "inputs.csv", "outputs.csv", "spends.csv",
                "events.csv", "frontier.csv", "export.json", "SHA256SUMS"}
PREVIEW_NAMES = {"graph.html", "graph.svg", "graph.mmd", "graph.json",
                 "mermaid-node-map.json", "mermaid-config.json"}
LAYOUT_NAMES = {"graph.html", "graph.svg", "graph.json", "layout-report.json"}
LAYOUT_ALGORITHMS = ("elk_layered_v1", "dependency_layers_v1")
FALLBACK_REASONS = ("size_limit", "timeout", "mermaid_size_limit", "mermaid_timeout")
CANCELLABLE_ACTIONS = {"layout", "mermaid"}


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


def public_layout_metrics(metrics):
    """Expose counts only, never copy arbitrary saved report fields to the UI."""
    if not isinstance(metrics, dict):
        return None
    result = {"estimated": True}
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
            summary["runs"] = sorted(runs, key=lambda run: (run.get("created_at") or "", run["id"]), reverse=True)
            summary["artifacts"] = self.saved_artifacts(case, metadata, {run["id"] for run in runs})
        return summary

    def saved_artifacts(self, case, metadata, runs):
        """Rediscover complete local products without relying on browser memory.

        Check identities and expected files, skipping partial exports and links.
        Never return absolute paths from an export's provenance metadata.
        """
        artifacts, newest = {}, {}
        for folder, kind, names, metadata_file in (
            ("previews", "mermaid", PREVIEW_NAMES, "graph.json"),
            ("previews", "elk", LAYOUT_NAMES, "graph.json"),
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
                    files = {name: self.artifact(case, [folder, directory.name, name]) for name in names}
                    if not all(path.is_file() for path in files.values()):
                        continue
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
                    finished = files["SHA256SUMS" if kind == "csv" else "graph.html"].stat().st_mtime_ns
                    order = (finished, directory.name)
                    if order <= newest.get((run_id, kind), (-1, "")):
                        continue
                    product = self.artifact_links(case, [folder, directory.name], names)
                    product["include_fees"] = fees
                    if kind == "elk":
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
                    elif kind == "mermaid":
                        preview = info.get("preview", {})
                        if isinstance(preview, dict):
                            product.update(public_rendering_metadata({
                                "renderer": preview.get("renderer"),
                                "fallback_reason": preview.get("reason")}))
                    artifacts.setdefault(run_id, {})[kind] = product
                    newest[(run_id, kind)] = order
                except (RequestError, OSError, ValueError, TypeError):
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
                               "cancellable": action in CANCELLABLE_ACTIONS and not live,
                               "message": ("Working. Check the launching terminal if Proton Pass needs to unlock."
                                           if live else "Working with saved local evidence…")}
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
        if job["action"] not in CANCELLABLE_ACTIONS or job["live"]:
            raise RequestError("Only local ELK and Mermaid calculations can be canceled here.", 409)
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
                                          message="Action completed.", result=value)
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
                  "reattached", "dry_run", "reorganize", "presentation_refreshed", "fee_items_to_remove",
                  "existing_items", "items", "runs", "max_items", "remote_preflight_required"}
        value = {key: item for key, item in result.items()
                 if key in fields and (item is None or isinstance(item, (str, int, float, bool)))}
        if isinstance(result.get("conflicts"), (list, dict)):
            value["conflicts_count"] = len(result["conflicts"])
        if isinstance(result.get("stats"), dict):
            value["stats"] = {key: item for key, item in result["stats"].items()
                              if isinstance(item, (int, float)) and not isinstance(item, bool)}
        if result.get("connector_style") in ("straight", "curved", "elbowed"):
            value["connector_style"] = result["connector_style"]
        value.update(public_rendering_metadata(result))
        metrics = public_layout_metrics(result.get("layout_metrics"))
        if metrics is not None:
            value["layout_metrics"] = metrics
        if action in ("mermaid", "csv", "layout"):
            directory = Path(result["directory"])
            relative = directory.relative_to(case)
            names = {"mermaid": PREVIEW_NAMES, "csv": EXPORT_NAMES, "layout": LAYOUT_NAMES}[action]
            value.update(self.artifact_links(case, relative.parts, names))
        return value

    @staticmethod
    def artifact(case, parts):
        if len(parts) != 3 or parts[0] not in ("previews", "exports") or not ARTIFACT_DIR.fullmatch(parts[1]):
            raise RequestError("File not found", 404)
        kind = parts[1].split("-")[1]
        if (parts[0] == "exports") != (kind == "csv"):
            raise RequestError("File not found", 404)
        expected = {"mermaid": PREVIEW_NAMES, "csv": EXPORT_NAMES, "elk": LAYOUT_NAMES}[kind]
        if parts[2] not in expected:
            raise RequestError("File not found", 404)
        return safe_path(case, parts)

    def action(self, case, metadata, body):
        from .boards import board_options, default_board_name
        from .cli import resolve_latest, run_path, verify_export

        action = body.get("action")
        settings = validate_settings(body.get("settings", metadata.get("run_defaults", {})))
        selected = body.get("run_id", "latest")
        live = False
        if action == "trace":
            if selected != "latest":
                raise RequestError("Continue from the latest saved run.")
            arguments, live = _trace_arguments(case, metadata, settings)
            update_case(case, {"run_defaults": settings})
        elif action == "miro-create":
            name = body.get("name") or default_board_name(metadata)
            board_options(name, visibility="private")
            if metadata.get("miro_board"):
                raise RequestError("A Miro board is already linked. Use Sync to Miro.")
            arguments = ["miro-create-board", "--case", str(case), "--name", name, "--visibility", "private"]
            live = True
        elif action in ("mermaid", "csv", "layout", "miro-preview", "miro-sync", "miro-organize"):
            selected = resolve_latest(case, selected)
            archive = run_path(case, selected)
            safe_path(case, ["runs", selected, "trace.json"])
            verify_export(archive)
            if action in ("mermaid", "csv", "layout"):
                arguments = [{"mermaid": "mermaid", "csv": "csv-export", "layout": "layout-preview"}[action],
                             "--case", str(case), "--run", selected]
            else:
                if not metadata.get("miro_board"):
                    raise RequestError("Create or link a Miro board in investigation settings first.")
                arguments = ["miro-sync", "--case", str(case), "--run", selected,
                             "--board", metadata["miro_board"], "--max-new-items", str(settings["max_new_items"])]
                if action == "miro-preview":
                    arguments.append("--dry-run")
                elif action == "miro-organize":
                    arguments.append("--reorganize")
                live = action != "miro-preview"
            arguments.append("--include-fees" if settings["include_fees"] else "--exclude-fees")
            if action not in ("mermaid", "csv"):
                arguments.extend(["--connector-style", settings["connector_style"]])
        else:
            raise RequestError("Choose a supported investigation action.")
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

    def body(self):
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
            raise RequestError("A bounded JSON request body is required.")
        try:
            length = int(self.headers["Content-Length"])
        except ValueError:
            raise RequestError("Invalid request length.") from None
        if not 0 < length <= MAX_BODY:
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
                body = self.body()
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
        elif parts == ["api", "demo"]:
            lines = (_project() / "examples" / "demo-seeds.txt").read_text().splitlines()
            seeds = _seed_values(" ".join(line.split("#", 1)[0] for line in lines))
            self.send(200, {"txids": list(dict.fromkeys(seed.split(":")[0] for seed in seeds))})
        elif len(parts) == 3 and parts[:2] == ["api", "cases"]:
            case, metadata = self.server.case(parts[2])
            self.send(200, self.server.case_summary(case, metadata, detail=True))
        elif len(parts) == 3 and parts[:2] == ["api", "jobs"]:
            with self.server.job_lock:
                job = self.server.jobs.get(parts[2])
                if job is None:
                    raise RequestError("Job unavailable. Refresh the investigation to review saved runs.", 404)
                self.send(200, dict(job))
        elif len(parts) == 5 and parts[0] == "files":
            case, _ = self.server.case(parts[1])
            path = self.server.artifact(case, parts[2:])
            if not path.is_file():
                raise RequestError("File not found", 404)
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send(200, path.read_bytes(), content_type, preview=path.suffix in (".html", ".svg"),
                      download=None if path.suffix == ".html" else path.name,
                      explorer_links=parts[3].split("-")[1] == "elk" and path.suffix in (".html", ".svg"))
        else:
            path = safe_path(self.server.assets, ["index.html"] if parts == [""] else parts)
            if not path.is_file():
                raise RequestError("Page not found", 404)
            self.send(200, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    def post(self, parts, body):
        if parts == ["api", "settings"]:
            return {"settings": save_settings(self.server.root, body.get("settings"))}, 200
        if parts == ["api", "lookup"]:
            txids = parse_transaction_hashes(body.get("txids"))
            source = body.get("source")
            if source not in ("live", "demo"):
                raise RequestError("Select a live or synthetic demo source.")
            arguments = ["inspect-txs", "--txids", ",".join(txids)]
            if source == "demo":
                arguments.extend(["--fixture", str(_project() / "examples" / "demo-api.json")])
            return self.server.start_job(arguments, action="lookup", live=source == "live", txids=txids), 202
        if parts == ["api", "cases"]:
            source = body.get("source")
            if source not in ("demo", "live"):
                raise RequestError("Select a live or synthetic demo source.")
            seeds = body.get("seeds")
            if not isinstance(seeds, list) or not seeds or any(not isinstance(seed, str) for seed in seeds):
                raise RequestError("Select at least one starting output.")
            normalized = _seed_values(" ".join(seeds))
            settings = validate_settings(body.get("settings", load_settings(self.server.root)))
            case = create_investigation(self.server.root, body.get("name"), seeds=normalized,
                board=body.get("board") or None, run_defaults=settings,
                fixture=_project() / "examples" / "demo-api.json" if source == "demo" else None)
            return self.server.case_summary(case, read_case(case), detail=True), 201
        if len(parts) == 4 and parts[:2] == ["api", "cases"]:
            case, metadata = self.server.case(parts[2])
            if parts[3] == "settings":
                updates = {"name": body.get("name", metadata.get("name")),
                           "miro_board": body.get("board", metadata.get("miro_board")) or None,
                           "run_defaults": body.get("settings", metadata.get("run_defaults", {}))}
                return self.server.case_summary(case, update_case(case, updates), detail=True), 200
            if parts[3] == "actions":
                return self.server.action(case, metadata, body), 202
        raise RequestError("Route not found", 404)


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
