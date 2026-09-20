"""Small, credential-free progress events for terminal and local UI clients."""

import json
import math
import os
import sys
import time
from pathlib import Path

from .layout_search_reporting import layout_search_warning, public_search_counts


MESSAGES = {
    "address_counts": "Fetching address transaction counts",
    "address_counts_ready": "Address transaction counts are ready",
    "address_counts_incomplete": "Some address counts are unavailable; see the lookup summary",
    "optimizing": "Optimizing the saved graph with ELK",
    "compacting": "Compacting address positions and activity components",
    "preflight": "Checking existing Miro items before making changes",
    "layout": "Preparing the graph layout",
    "checking_layout": "Checking completed ELK previews for this full graph",
    "reusing_layout": "Reusing the completed ELK layout; no recalculation",
    "building_plan": "Building and validating the Miro publication plan",
    "updating": "Updating mapped Miro items",
    "removing": "Removing obsolete generated items",
    "framing": "Updating graph export frames",
    "recovery": "Checking the confirmed-empty Miro board",
    "frame_recovery": "Checking frames for the interrupted Miro sync",
    "creating": "Adding new Miro items",
    "waiting": "Waiting before retrying a Miro request",
    "complete": "Miro synchronization completed",
}


ELK_STAGES = {
    "preparing": "Preparing the graph for ELK",
    "measuring_input": "Measuring the input layout before ELK",
    "calculating": "Calculating the graph layout with ELK",
    "applying": "Validating ELK coordinates and connector routes",
    "measuring_output": "Measuring the completed ELK layout",
    "memory_measured": "ELK worker memory measurement completed",
    "ready": "ELK layout completed",
    "attempt_failed": "ELK layout attempt failed; continuing the layout search",
    "retrying_memory": "Retrying the ELK layout attempt alone with the full shared heap budget",
    "ready_with_failures": "ELK layout completed with failed attempts",
}


_ELK_MEMORY_FAILURES = {
    "heap_exhausted": "JavaScript heap exhausted; close other applications to free memory",
    "memory_exhausted": "renderer reported memory exhaustion; close other applications to free memory",
    "worker_killed": "worker was killed; memory exhaustion is possible but unconfirmed",
}


def _public_elk_workers(event, attempt_total):
    """Keep concurrency counts consistent and memory caps within their shared pool."""
    workers, active = event.get("worker_count"), event.get("active_workers")
    if (type(workers) is not int or type(active) is not int
            or not 1 <= workers <= (attempt_total or 1000)
            or not 0 <= active <= workers):
        return {}
    value = {"worker_count": workers, "active_workers": active}
    total_heap, heap = event.get("total_heap_mb"), event.get("heap_mb")
    if (type(total_heap) is int and type(heap) is int
            and 1 <= heap <= total_heap <= 2 ** 53 - 1):
        value["total_heap_mb"] = total_heap
    return value


def public_progress(event):
    """Allow only known phases and bounded numbers across the browser boundary.

    Messages are generated here, never copied from API responses, exceptions,
    provider output, remote item content, or local filesystem paths.
    """
    if (not isinstance(event, dict) or not isinstance(event.get("phase"), str)
            or event["phase"] not in MESSAGES):
        return None
    done, total = event.get("completed"), event.get("total")
    if type(done) is not int or type(total) is not int or not 0 <= done <= total <= 2 ** 53 - 1:
        return None
    value = {"phase": event["phase"], "completed": done, "total": total,
             "message": MESSAGES[event["phase"]]}
    if value["phase"] == "optimizing":
        stage = event.get("stage")
        if isinstance(stage, str) and stage in ELK_STAGES:
            value.update(stage=stage, message=ELK_STAGES[stage])
        peak_rss = event.get("peak_rss_mb")
        if (stage == "memory_measured" and type(peak_rss) is int
                and 1 <= peak_rss <= 2 ** 31 - 1):
            value.update(peak_rss_mb=peak_rss,
                         message=f"Measured ELK worker peak RAM: {peak_rss:,} MiB")
        failure_code = event.get("failure_code")
        if (stage == "attempt_failed" and isinstance(failure_code, str)
                and failure_code in _ELK_MEMORY_FAILURES):
            value["failure_code"] = failure_code
            value["message"] = ("ELK layout attempt failed: " + _ELK_MEMORY_FAILURES[failure_code]
                                + "; continuing the layout search")
        elif (stage == "retrying_memory" and isinstance(failure_code, str)
                and failure_code in _ELK_MEMORY_FAILURES):
            value["failure_code"] = failure_code
            value["message"] += "; " + _ELK_MEMORY_FAILURES[failure_code]
        search_counts = public_search_counts(
            {**event, "attempt_count": event.get("attempt_count", event.get("attempt_total"))},
            allow_no_success=True)
        value.update(search_counts)
        if stage == "ready_with_failures":
            warning = layout_search_warning(search_counts)
            if warning:
                value["message"] = "ELK layout completed: " + warning
        attempt, attempts, seed = (event.get(key) for key in ("attempt_index", "attempt_total", "seed"))
        if (type(attempt) is int and type(attempts) is int
                and 1 <= attempt <= attempts <= 1000):
            value.update(attempt_index=attempt, attempt_total=attempts)
            value["message"] += f"; layout attempt {attempt} of {attempts}"
            if type(seed) is int and 1 <= seed <= 2 ** 31 - 1:
                value["seed"] = seed
        for field in ("node_count", "edge_count", "heap_mb"):
            number = event.get(field)
            if type(number) is int and 0 <= number <= 2 ** 53 - 1:
                value[field] = number
        if "node_count" in value and "edge_count" in value:
            value["message"] += f" ({value['node_count']:,} objects, {value['edge_count']:,} connections)"
        value.update(_public_elk_workers(event, value.get("attempt_total")))
        if value.get("worker_count", 1) > 1:
            value["message"] += (f"; up to {value['worker_count']} ELK workers"
                                 f"; {value['active_workers']} active")
            if "total_heap_mb" in value:
                value["message"] += f"; shared heap budget {value['total_heap_mb']:,} MiB"
            if "heap_mb" in value:
                value["message"] += f"; per-worker Node heap budget {value['heap_mb']:,} MiB"
        elif "heap_mb" in value:
            value["message"] += f"; Node heap budget {value['heap_mb']:,} MiB"
    if value["phase"] == "waiting":
        reason = event.get("reason")
        if reason == "rate_limit":
            value.update(reason=reason, message="Waiting for the Miro rate limit to reset")
        elif reason == "server_retry":
            value.update(reason=reason, message="Waiting before retrying a temporary Miro read error")
    delay = event.get("retry_after")
    if type(delay) in (int, float) and 0 <= delay <= 2 ** 53 - 1 and math.isfinite(delay):
        value["retry_after"] = delay
    elapsed = event.get("elapsed_seconds")
    if type(elapsed) in (int, float) and 0 <= elapsed <= 2 ** 53 - 1 and math.isfinite(elapsed):
        value["elapsed_seconds"] = elapsed
    return value


class ProgressReporter:
    """Emit to stderr and optionally a disposable, private IPC file.

    Evidence and Miro mapping checkpoints use durable fsync writes elsewhere.
    Progress is advisory and replaceable; failure to report must not interrupt
    an acknowledged board update or prevent its mapping from being saved.
    """

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None
        self.previous_phase = None
        self.previous_stage = None
        self.previous_attempt = None
        self.file_time = self.terminal_time = float("-inf")

    def __call__(self, event):
        value = public_progress(event)
        if value is None:
            return
        now = time.monotonic()
        urgent = (value["phase"] != self.previous_phase or value.get("stage") != self.previous_stage
                  or value.get("attempt_index") != self.previous_attempt
                  or value["phase"] == "waiting"
                  or (value["total"] > 0 and value["completed"] == value["total"]))
        self.previous_phase = value["phase"]
        self.previous_stage = value.get("stage")
        self.previous_attempt = value.get("attempt_index")
        if self.path is not None and (urgent or now - self.file_time >= .1):
            temporary = self.path.with_name(self.path.name + ".tmp")
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(value, stream)
                temporary.replace(self.path)
                self.file_time = now
            except OSError:
                pass
        if urgent or now - self.terminal_time >= 1:
            counts = f" ({value['completed']}/{value['total']})" if value["total"] else ""
            wait = f"; retry in {value['retry_after']:g}s" if "retry_after" in value else ""
            elapsed = f"; {value['elapsed_seconds']:g}s elapsed" if "elapsed_seconds" in value else ""
            try:
                prefix = ("Counts: " if value["phase"].startswith("address_counts") else
                          "ELK: " if value["phase"] in ("optimizing", "compacting") else "Miro: ")
                print(prefix + value["message"] + counts + wait + elapsed, file=sys.stderr, flush=True)
            except (OSError, ValueError):
                pass
            self.terminal_time = now
