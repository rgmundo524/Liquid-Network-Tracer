"""Small, credential-free progress events for terminal and local UI clients."""

import json
import math
import os
import sys
import time
from pathlib import Path

from .layout_search_reporting import layout_search_warning, public_search_counts
from .performance import public_api_diagnostics


MESSAGES = {
    "deleting_investigation": "Deleting the confirmed investigation's local files",
    "deleting_board": "Deleting the confirmed Miro board and updating its local record",
    "scope_analysis": "Comparing saved tracing depths and identifying open branches",
    "plot_shared_projection": "Preparing the shared-data view",
    "plot_load_source": "Loading and verifying the plot source",
    "plot_build_graph": "Building graph objects and connections",
    "plot_read_board": "Reading the current Miro arrangement",
    "plot_layout": "Arranging the graph (including ELK resource waits)",
    "plot_export_preview": "Saving the graph preview",
    "plot_build_plan": "Building the saved Miro plan",
    "plot_write_exports": "Writing and verifying plot downloads",
    "plot_review_saved_plot": "Verifying the saved plot for publication",
    "plot_recheck_saved_plot": "Checking that reviewed plot files are unchanged",
    "plot_new_board_preflight": "Checking the new-board publication budget",
    "plot_board_preflight": "Checking the destination board plan",
    "plot_create_board": "Creating or recovering the Miro board",
    "plot_write_miro": "Writing the saved plot to Miro",
    "preparing_plot": "Checking saved plot inputs and destination",
    "verifying_files": "Verifying saved file checksums",
    "loading_collection": "Loading verified transaction data",
    "indexing_collection": "Building the reusable transaction index",
    "waiting_collection_index": "Waiting for the transaction index being built by another task",
    "reusing_collection_index": "Reusing the verified transaction index",
    "projecting_collection": "Preparing this investigation's shared collection snapshot",
    "collecting": "Collecting transaction data",
    "collection_complete": "Transaction data collection finished",
    "collection_paused": "Transaction data collection paused",
    "collection_error": "Transaction data collection stopped",
    "collection_empty": "No eligible outputs remain to collect",
    "exporting_collection": "Saving collected transaction data and downloads",
    "pegout_search": "Tracing toward peg-out requests within the selected hop range",
    "pegout_paths": "Preparing the matching peg-out paths",
    "address_counts": "Fetching address transaction counts",
    "address_counts_ready": "Address transaction counts are ready",
    "address_counts_incomplete": "Some address counts are unavailable; see the lookup summary",
    "optimizing": "Optimizing the saved graph with ELK",
    "exporting_plot": "Saving plot previews and transaction CSV",
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


COLLECTION_PHASES = {
    "collecting": "Processing hop {completed} of {total}",
    "collection_complete": "Collection finished; last processing hop {completed} of {total}",
    "collection_paused": "Collection paused while processing hop {completed} of {total}",
    "collection_error": "Collection stopped while processing hop {completed} of {total}",
    "collection_empty": "No eligible outputs remain to collect",
}


ELK_STAGES = {
    "preparing": "Preparing the graph for ELK",
    "measuring_input": "Measuring the input layout before ELK",
    "section_preparing": "Preparing Trace sections around the preferred backbone",
    "section_ready": "Trace section layout completed",
    "section_assembling": "Assembling Trace sections and their connections",
    "calculating": "Calculating the graph layout with ELK",
    "applying": "Validating ELK coordinates and connector routes",
    "input_order_fallback": "Preferred connector ordering unavailable; retaining ELK geometry for validation",
    "measuring_output": "Measuring the completed ELK layout",
    "memory_measured": "ELK worker memory measurement completed",
    "resource_wait": "Waiting for CPU and memory shared with other Liquid Tracer layouts",
    "resource_allocated": "CPU and memory allocated for this ELK layout",
    "ready": "ELK layout completed",
    "attempt_failed": "ELK layout attempt failed; continuing the layout search",
    "retrying_memory": "Retrying the ELK layout attempt alone with the full shared heap budget",
    "ready_with_failures": "ELK layout completed with failed attempts",
}

RESOURCE_WAIT_MESSAGES = {
    "memory": "Waiting for unreserved ELK memory allowance",
    "cpu": "Waiting for an available ELK CPU slot",
    "fifo": "Waiting for an earlier ELK resource request",
    "memory_retry": "Waiting to retry this ELK attempt with a larger memory allowance",
}


_ELK_MEMORY_FAILURES = {
    "heap_exhausted": "JavaScript heap exhausted; close other applications to free memory",
    "memory_exhausted": "renderer reported memory exhaustion; close other applications to free memory",
    "worker_killed": "worker was killed; memory exhaustion is possible but unconfirmed",
}


def _public_trace_sections(event):
    """Section counts are independent of the much smaller attempt budget."""
    value = {}
    count, worker_count = event.get("section_count"), event.get("section_worker_count")
    if type(count) is int and 0 <= count <= 2 ** 53 - 1:
        value["section_count"] = count
        if type(worker_count) is int and 0 <= worker_count <= count:
            value["section_worker_count"] = worker_count
    index, total = event.get("section_index"), event.get("section_total")
    if (type(index) is int and type(total) is int and 1 <= index <= total <= 2 ** 53 - 1
            and ("section_count" not in value or total <= value["section_count"])):
        value.update(section_index=index, section_total=total)
    return value


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


def _public_api_workers(event):
    """Expose API scheduling telemetry without provider-controlled text."""
    value = {}
    for field in ("worker_count", "worker_limit"):
        number = event.get(field)
        if type(number) is int and 1 <= number <= 64:
            value[field] = number
    if ("worker_count" in value and "worker_limit" in value
            and value["worker_count"] > value["worker_limit"]):
        value.clear()
    rate = event.get("observed_rps")
    if type(rate) in (int, float) and 0 <= rate <= 2 ** 53 - 1 and math.isfinite(rate):
        value["observed_rps"] = rate
    return value


def _public_shared_api(event):
    """Only local numeric pacing state and fixed reasons cross the UI boundary."""
    value = {}
    clients = event.get("shared_api_active_clients")
    if type(clients) is int and 1 <= clients <= 65535:
        value["shared_api_active_clients"] = clients
    for field in ("shared_api_effective_rps", "shared_api_wait_seconds"):
        number = event.get(field)
        if type(number) in (int, float) and 0 <= number <= 2 ** 53 - 1 and math.isfinite(number):
            value[field] = number
    reason = event.get("shared_api_wait_reason")
    if isinstance(reason, str) and reason in ("", "shared_rate_limit", "server_cooldown"):
        value["shared_api_wait_reason"] = reason
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
    if value["phase"] == "verifying_files" and total:
        value["message"] += f"; {done / 1048576:,.1f} of {total / 1048576:,.1f} MiB checked"
    if value["phase"].startswith("address_counts"):
        value.update(_public_api_workers(event))
        fetched = event.get("fetched")
        if type(fetched) is int and 0 <= fetched <= done:
            value["fetched"] = fetched
        if "worker_count" in value:
            noun = "request" if value["worker_count"] == 1 else "requests"
            value["message"] += f"; up to {value['worker_count']} concurrent {noun}"
        if "observed_rps" in value:
            value["message"] += f"; {value['observed_rps']:.1f} counts/s"
    if value["phase"] in COLLECTION_PHASES:
        value["message"] = COLLECTION_PHASES[value["phase"]].format(**value)
        from .group_hops import normalize_reference_name
        from .common import TraceError
        try:
            name = normalize_reference_name(event.get("hop_reference_name", ""))
        except TraceError:
            name = ""
        if name:
            value["hop_reference_name"] = name
            value["message"] += f" from {name}; named-group outputs reset to hop 0"
        value.update(_public_api_workers(event))
        if "worker_count" in value:
            noun = "request" if value["worker_count"] == 1 else "requests"
            value["message"] += f"; up to {value['worker_count']} concurrent {noun}"
        if "observed_rps" in value:
            value["message"] += f"; {value['observed_rps']:.1f} requests/s"
    if value["phase"].startswith("address_counts") or value["phase"] in COLLECTION_PHASES:
        value.update(public_api_diagnostics(event))
        if "api_rate_mode" in value and "api_target_rps" in value:
            value["message"] += (f"; {value['api_rate_mode']} API target "
                                 f"{value['api_target_rps']:.1f} requests/s total")
        value.update(_public_shared_api(event))
        if value.get("shared_api_active_clients", 1) > 1:
            value["message"] += f"; API budget shared by {value['shared_api_active_clients']} clients"
            if "shared_api_effective_rps" in value:
                value["message"] += f" ({value['shared_api_effective_rps']:.1f} requests/s total)"
        if value.get("shared_api_wait_reason") == "server_cooldown" and value.get("shared_api_wait_seconds", 0) > 0:
            value["message"] += "; waiting for the shared API cooldown"
    if value["phase"] == "optimizing":
        stage = event.get("stage")
        if isinstance(stage, str) and stage in ELK_STAGES:
            value.update(stage=stage, message=ELK_STAGES[stage])
        if stage in ("resource_wait", "resource_allocated"):
            for field in ("running_layouts", "waiting_layouts", "reserved_heap_mb", "available_heap_mb",
                          "cpu_slots", "reserved_workers"):
                number = event.get(field)
                if type(number) is int and 0 <= number <= 2 ** 53 - 1:
                    value[field] = number
            reason = event.get("wait_reason")
            if stage == "resource_wait" and isinstance(reason, str) and reason in RESOURCE_WAIT_MESSAGES:
                value.update(wait_reason=reason, message=RESOURCE_WAIT_MESSAGES[reason])
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
        value.update(_public_trace_sections(event))
        if "section_index" in value:
            value["message"] += f"; Trace section {value['section_index']:,} of {value['section_total']:,}"
        elif "section_count" in value:
            value["message"] += f"; {value['section_count']:,} Trace sections"
            if "section_worker_count" in value:
                value["message"] += f"; {value['section_worker_count']:,} sections require ELK"
        for field in ("node_count", "edge_count", "heap_mb"):
            number = event.get(field)
            if type(number) is int and 0 <= number <= 2 ** 53 - 1:
                value[field] = number
        if "node_count" in value and "edge_count" in value:
            value["message"] += f" ({value['node_count']:,} objects, {value['edge_count']:,} connections)"
        worker_limit = (min(64, value["section_total"]) if "section_total" in value
                        else value.get("attempt_total"))
        value.update(_public_elk_workers(event, worker_limit))
        if value.get("worker_count", 1) > 1:
            value["message"] += (f"; up to {value['worker_count']} ELK workers"
                                 f"; {value['active_workers']} active")
            if "total_heap_mb" in value:
                value["message"] += f"; shared heap budget {value['total_heap_mb']:,} MiB"
            if "heap_mb" in value:
                value["message"] += f"; per-worker Node heap budget {value['heap_mb']:,} MiB"
        elif "heap_mb" in value:
            value["message"] += f"; Node heap budget {value['heap_mb']:,} MiB"
        layouts = event.get("active_layouts")
        if type(layouts) is int and 1 <= layouts <= 65535:
            value["active_layouts"] = layouts
            if layouts > 1:
                value["message"] += f"; sharing resources across {layouts} layouts"
        machine_heap = event.get("machine_heap_mb")
        if type(machine_heap) is int and 1 <= machine_heap <= 2 ** 53 - 1:
            value["machine_heap_mb"] = machine_heap
    if value["phase"] == "waiting":
        reason = event.get("reason")
        if reason == "rate_limit":
            value.update(reason=reason, message="Waiting for the Miro rate limit to reset")
        elif reason == "server_retry":
            value.update(reason=reason, message="Waiting before retrying a temporary Miro read error")
        elif reason == "update_retry":
            value.update(reason=reason, message="Checking a temporary Miro update failure before retrying")
    delay = event.get("retry_after")
    if type(delay) in (int, float) and 0 <= delay <= 2 ** 53 - 1 and math.isfinite(delay):
        value["retry_after"] = delay
    elapsed = event.get("elapsed_seconds")
    if type(elapsed) in (int, float) and 0 <= elapsed <= 2 ** 53 - 1 and math.isfinite(elapsed):
        value["elapsed_seconds"] = elapsed
    return value


def report_progress(progress, phase, completed=0, total=0, *, hop_reference_name="", **telemetry):
    """Advisory observers cannot change collection or its saved evidence."""
    if progress is None:
        return
    value = public_progress({**telemetry, "phase": phase, "completed": completed, "total": total,
                             "hop_reference_name": hop_reference_name})
    if value is not None:
        try:
            progress(value)
        except Exception:
            pass


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
        self.previous_section = None
        self.previous_completed = None
        self.file_time = self.terminal_time = float("-inf")

    def __call__(self, event):
        value = public_progress(event)
        if value is None:
            return
        now = time.monotonic()
        section = (value.get("section_index"), value.get("section_total"))
        urgent = (value["phase"] != self.previous_phase or value.get("stage") != self.previous_stage
                  or value.get("attempt_index") != self.previous_attempt
                  or section != self.previous_section
                  or (value["phase"] == "collecting" and value["completed"] != self.previous_completed)
                  or value["phase"] == "waiting"
                  or (value["phase"] not in COLLECTION_PHASES and value["total"] > 0
                      and value["completed"] == value["total"]))
        self.previous_phase = value["phase"]
        self.previous_stage = value.get("stage")
        self.previous_attempt = value.get("attempt_index")
        self.previous_section = section
        self.previous_completed = value["completed"]
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
            counts = (f" ({value['completed']}/{value['total']})"
                      if value["total"] and value["phase"] not in COLLECTION_PHASES else "")
            wait = f"; retry in {value['retry_after']:g}s" if "retry_after" in value else ""
            elapsed = f"; {value['elapsed_seconds']:g}s elapsed" if "elapsed_seconds" in value else ""
            try:
                prefix = ("Collection: " if value["phase"] in COLLECTION_PHASES or value["phase"] == "exporting_collection" else
                          "Investigation: " if value["phase"] == "deleting_investigation" else
                          "Counts: " if value["phase"].startswith("address_counts") else
                          "Plot: " if value["phase"] == "exporting_plot" or value["phase"].startswith("plot_") else
                          "Peg-outs: " if value["phase"].startswith("pegout_") else
                          "ELK: " if value["phase"] in ("optimizing", "compacting") else "Miro: ")
                print(prefix + value["message"] + counts + wait + elapsed, file=sys.stderr, flush=True)
            except (OSError, ValueError):
                pass
            self.terminal_time = now
