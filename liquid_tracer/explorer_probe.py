"""Measure useful missing address-statistics work through the real count pipeline.

This is a bounded sample of local throughput, not a provider load test or a
claim about an account's maximum capacity. Completed observations remain in the
ordinary count cache; verified transaction archives are never rewritten.
"""

import math
import time
from collections import deque

from .address_counts import fetch_counts
from .common import TraceError
from .performance import (API_COUNTS, API_CUMULATIVE_SECONDS, API_DIAGNOSTIC_FIELDS,
                          public_api_diagnostics)


WINDOW_SECONDS = 5.
MAX_WINDOW_SAMPLES = 120
_CUMULATIVE_METRICS = API_COUNTS | API_CUMULATIVE_SECONDS


def _window_snapshot(event):
    value = public_api_diagnostics(event)
    workers = {}
    for key in ("worker_count", "worker_limit"):
        number = event.get(key)
        if type(number) is int and 1 <= number <= 64:
            workers[key] = number
    if workers.get("worker_count", 1) <= workers.get("worker_limit", 64):
        value.update(workers)
    return value


class _Windows:
    def __init__(self):
        self.started = None
        self.origin = None
        self.fetched = 0
        self.previous_fetched = 0
        self.count = 0
        self.peak = None
        self.latest = None
        self.latest_metrics = {}
        self.series = deque(maxlen=MAX_WINDOW_SAMPLES)
        self.baseline = {}
        self.previous_metrics = {}
        self.reset_metrics = set()
        self.peak_active_clients = 0
        self.window_peak_active_clients = 0

    def _begin(self, now, fetched, metrics):
        self.started, self.fetched = now, fetched
        self.baseline = metrics
        self.reset_metrics.clear()
        self.window_peak_active_clients = metrics.get("shared_api_active_clients", 0)

    def observe(self, event):
        diagnostics = public_api_diagnostics(event)
        self.latest_metrics.update(diagnostics)
        self.peak_active_clients = max(self.peak_active_clients,
                                      diagnostics.get("shared_api_active_clients", 0),
                                      diagnostics.get("shared_api_peak_active_clients", 0))
        fetched = event.get("fetched")
        if (event.get("phase") != "address_counts" or type(fetched) is not int
                or not 0 <= fetched <= 2 ** 53 - 1):
            return
        now = time.monotonic()
        snapshot = _window_snapshot(event)
        self.window_peak_active_clients = max(self.window_peak_active_clients,
                                               snapshot.get("shared_api_active_clients", 0))
        for key in _CUMULATIVE_METRICS & snapshot.keys() & self.previous_metrics.keys():
            if snapshot[key] < self.previous_metrics[key]:
                self.reset_metrics.add(key)
        # Missing fields are not zero and must not erase the last known
        # counter: a reset can happen while an optional metric is absent.
        self.previous_metrics.update({key: value for key, value in snapshot.items()
                                      if key in _CUMULATIVE_METRICS})
        reset = fetched < self.previous_fetched
        self.previous_fetched = fetched
        if self.started is None or reset:
            if self.origin is None:
                self.origin = now
            self._begin(now, fetched, snapshot)
            return
        elapsed = now - self.started
        if elapsed < WINDOW_SECONDS:
            return
        # Only successful newly fetched statistics are measured. The generic
        # progress 'completed' also includes cache hits and examined failures.
        completed = max(0, fetched - self.fetched)
        self.latest = {"seconds": elapsed, "fetched": completed,
                       "counts_per_second": completed / elapsed}
        self.count += 1
        if self.peak is None or self.latest["counts_per_second"] > self.peak["counts_per_second"]:
            self.peak = self.latest
        # Only cumulative counters/timers present at both boundaries can be
        # differenced. A reset seen anywhere in the window invalidates its
        # delta, even if the counter subsequently exceeds its starting value.
        deltas = {key: snapshot[key] - self.baseline[key]
                  for key in _CUMULATIVE_METRICS & snapshot.keys() & self.baseline.keys()
                  if key not in self.reset_metrics and snapshot[key] >= self.baseline[key]}
        gauges = {key: value for key, value in snapshot.items() if key not in _CUMULATIVE_METRICS}
        if self.window_peak_active_clients:
            gauges["shared_api_window_peak_active_clients"] = self.window_peak_active_clients
        self.series.append({**self.latest,
                            "start_seconds": self.started - self.origin,
                            "end_seconds": now - self.origin,
                            **gauges, "deltas": deltas})
        self._begin(now, fetched, snapshot)


def probe_explorer(case, run_id="latest", *, seconds=60, max_requests=10000,
                   progress=None, transport=None):
    """Fetch missing counts for a bounded time/request sample and retain them.

    The ordinary ``fetch_counts`` entry point provides archive verification,
    case identity checks, process locks, request budgets, evidence recording and
    durable count checkpoints. There is deliberately no refresh option: cached
    addresses must not be requested again merely to generate test traffic.

    ``seconds`` bounds request admission, not offline archive verification or
    final cache compaction. An admitted response may need time to finish saving.
    """
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        raise TraceError("Explorer probe seconds must be a finite positive number")
    if type(max_requests) is not int or max_requests <= 0:
        raise TraceError("Explorer probe max_requests must be a positive whole number")
    windows = _Windows()

    def observe(event):
        windows.observe(event)
        if progress is not None:
            try:
                progress(event)
            except Exception:
                pass  # Reporting must not prevent a useful count being saved.

    started = time.monotonic()
    report = fetch_counts(case, run_id, max_requests=max_requests, max_seconds=seconds,
                          refresh=False, progress=observe, transport=transport)
    wall_seconds = max(0., time.monotonic() - started)
    # No lookup timer exists when every requested count was already cached.
    # In that case all command time belongs to preparation/verification.
    elapsed = report.get("elapsed_seconds", 0.)
    metrics = windows.latest_metrics
    metrics.update(public_api_diagnostics(report))
    peak_active_clients = max(windows.peak_active_clients,
                              metrics.get("shared_api_active_clients", 0),
                              metrics.get("shared_api_peak_active_clients", 0))
    if peak_active_clients:
        metrics["shared_api_peak_active_clients"] = peak_active_clients
    rate_limits = metrics.get("rate_limit_responses")
    feedback = ("unavailable" if rate_limits is None else
                "observed" if rate_limits else "not_observed")
    if report["fetched"] == 0 and report["remaining"] == 0:
        outcome = "no_missing_counts"
        explanation = "All address counts were already saved; no API capacity was measured."
    elif report["remaining"] == 0:
        outcome = "backlog_exhausted"
        explanation = "All missing counts were fetched before the probe needed to stop."
    elif feedback == "observed":
        outcome = "provider_throttled"
        explanation = "The API returned rate-limit feedback during this sample."
    elif windows.count == 0:
        outcome = "short_sample"
        explanation = "The sample did not cover a complete five-second measurement window."
    else:
        outcome = "bounded_sample"
        explanation = "The probe measured useful count throughput within its request and time budget."
    # Invalid diagnostic values cannot survive through the raw count report
    # when a sanitizer omits them. Keep the latest valid sample as fallback.
    count_report = {key: value for key, value in report.items() if key not in API_DIAGNOSTIC_FIELDS}
    return {**count_report, **metrics, "schema_version": 1, "kind": "explorer_probe",
            "probe_seconds": seconds, "probe_max_requests": max_requests,
            "probe_wall_seconds": wall_seconds,
            "probe_outside_lookup_seconds": max(0., wall_seconds - elapsed),
            "counts_per_second": report["fetched"] / max(.001, elapsed),
            "sample_windows": windows.count, "peak_window": windows.peak,
            "latest_window": windows.latest, "rate_limit_feedback": feedback,
            "window_series": list(windows.series),
            "window_series_dropped": windows.count - len(windows.series),
            "outcome": outcome,
            "notice": explanation + " Successfully fetched counts are saved for subsequent work. "
                "Observed throughput and the learned pacing target do not establish a provider or account maximum. "
                "Other processes sharing this API, local storage and available workers can affect this result. "
                "Worker timing totals overlap and must not be added together. Outside-lookup time includes "
                "archive verification, state/cache preparation and cleanup, rather than API request time. "
                "Window start/end times are relative to the first lookup sample; worker_count is the worker "
                "target and in_flight is actual HTTP occupancy at the sample boundary. Window deltas are "
                "counter and accumulated worker-time differences, not separate wall-clock phases. "
                "At most the latest 120 complete measurement windows are retained."}
