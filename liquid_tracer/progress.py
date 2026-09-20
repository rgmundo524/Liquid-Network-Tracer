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
    "ready": "ELK layout completed",
    "attempt_failed": "ELK layout attempt failed; continuing the layout search",
    "ready_with_failures": "ELK layout completed with failed attempts",
}


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
        if "heap_mb" in value:
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
