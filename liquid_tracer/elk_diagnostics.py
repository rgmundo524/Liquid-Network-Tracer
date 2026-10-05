"""Automatic local ELK failure reports, separate from graph and browser output."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

from .render_runtime import _elk_failure_diagnostic


TRACE_PREFIX = "LIQUID_ELK_TRACE "
MAX_TRACE_CHARS = 262144
MAX_STDERR_CHARS = 32768


def _text(value, limit):
    return value[:limit] if isinstance(value, str) else ""


def _worker_trace(stderr):
    """Bound every field independently; never serialize arbitrary worker data."""
    for line in reversed(stderr.splitlines()):
        if not line.startswith(TRACE_PREFIX) or len(line) > MAX_TRACE_CHARS:
            continue
        try:
            payload = json.loads(line[len(TRACE_PREFIX):])
        except (ValueError, RecursionError):
            continue
        if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
            continue
        result = {"version": 1, "truncated": payload.get("truncated") is True}
        for key in ("code", "stage", "branch_profile", "input_order_policy", "engine_version", "engine_build"):
            if isinstance(payload.get(key), str):
                result[key] = payload[key][:128]
        seed = payload.get("seed")
        if type(seed) is int and 1 <= seed <= 2147483647:
            result["seed"] = seed
        runtime = payload.get("runtime")
        if isinstance(runtime, dict):
            result["runtime"] = {key: _text(runtime.get(key), 128) for key in ("node", "v8", "platform", "arch")}
        errors = payload.get("errors")
        result["errors"] = []
        if isinstance(errors, list):
            result["truncated"] |= len(errors) > 4
            for error in errors[:4]:
                if not isinstance(error, dict):
                    continue
                record = {}
                for key, limit in (("name", 128), ("message", 1024), ("stack", 8192)):
                    value = _text(error.get(key), limit)
                    result["truncated"] |= isinstance(error.get(key), str) and len(error[key]) > limit
                    record[key] = value
                result["errors"].append(record)
        return result
    return {}


def _terminal(message):
    # A closed terminal must never replace the actual layout failure.
    try:
        print(message, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def report_location(paths):
    """Summarize paths without copying private worker text into an exception."""
    if not paths:
        return ""
    if len(paths) == 1:
        return " Diagnostic report: " + json.dumps(str(paths[0])) + "."
    directory = str(Path(paths[0]).parent)
    return f" {len(paths)} diagnostic reports saved in " + json.dumps(directory) + "."


def save_failure_report(stderr, *, graph, seeds, failure_code, returncode, heap_mb,
                        elapsed_seconds, engine_version):
    """Save a private report on failure, without a graph copy or environment dump.

    Exception messages and stack paths can contain graph identifiers. They stay
    in this owner-only file and never enter public progress or saved layouts.
    Diagnostic failure must not affect worker failure classification or retries.
    """
    path = None
    try:
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        if not isinstance(stderr, str):
            stderr = ""
        # Retain protocol records at either end and the newest native crash text.
        excerpt = stderr if len(stderr) <= 2 * MAX_TRACE_CHARS else stderr[:MAX_TRACE_CHARS] + "\n" + stderr[-MAX_TRACE_CHARS:]
        worker = _worker_trace(excerpt)
        native = "\n".join(line for line in excerpt.splitlines()
                           if not line.startswith((TRACE_PREFIX, "LIQUID_ELK_FAILURE ", "LIQUID_ELK_USAGE ")))
        children = graph.get("children", [])
        edges = graph.get("edges", [])
        orders = graph.get("inputPortOrders", {})
        now = datetime.now(timezone.utc)
        report = {
            "version": 1, "created_at": now.isoformat(),
            "engine": {"name": "ELK", "version": engine_version},
            "failure_code": failure_code, "returncode": returncode, "heap_mb": heap_mb,
            "elapsed_seconds": round(max(0, elapsed_seconds), 3), "seeds": list(seeds[:3]),
            "graph_counts": {"nodes": len(children), "edges": len(edges),
                             "ports": sum(len(node.get("ports", [])) for node in children)},
            "request": {"branch_profile": graph.get("branchProfile") if graph.get("branchProfile") in ("balanced", "flow_weighted") else None,
                        "boundary_ordering": graph.get("boundaryOrdering") is True,
                        "named_group_centered": bool(graph.get("centerNodeOrder")),
                        "input_order_constrained_nodes": len(orders)},
            "worker_context": _elk_failure_diagnostic(excerpt), "worker": worker,
            "stderr_tail": native[-MAX_STDERR_CHARS:],
            "stderr_truncated": len(stderr) > len(excerpt) or len(native) > MAX_STDERR_CHARS,
        }
        encoded = json.dumps(report, ensure_ascii=True, indent=2) + "\n"
        while len(encoded) > 262144:
            # JSON escaping can expand control characters and non-ASCII text.
            tail = report["stderr_tail"]
            report["stderr_tail"] = tail[len(tail) // 2:]
            report["stderr_truncated"] = True
            worker["truncated"] = True
            for error in worker.get("errors", []):
                error["stack"] = error["stack"][:len(error["stack"]) // 2]
            encoded = json.dumps(report, ensure_ascii=True, indent=2) + "\n"
        state = Path(os.environ.get("XDG_STATE_HOME", ""))
        if not state.is_absolute():
            state = Path.home() / ".local" / "state"
        directory = state / "liquid-tracer" / "diagnostics"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        prefix = "elk-failure-" + now.strftime("%Y%m%dT%H%M%S%fZ") + "-"
        descriptor, filename = tempfile.mkstemp(prefix=prefix, suffix=".json", dir=directory)
        path = Path(filename)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(encoded)
    except Exception:
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass
        _terminal("ELK diagnostics: could not save the local failure report; the original layout error follows.")
        return None
    _terminal("ELK diagnostic report saved: " + json.dumps(str(path)))
    return path
