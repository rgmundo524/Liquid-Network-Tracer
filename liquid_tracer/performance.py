"""Bounded, nonsecret collection timing reports for saved runs and the UI."""

import math
import re


SECONDS = {
    "tracing_seconds", "fetch_wait_seconds", "checkpoint_seconds", "processing_seconds",
    "address_counts_seconds", "network_seconds_total", "pacing_wait_seconds_total",
    "retry_wait_seconds_total", "evidence_seconds_total",
}
COUNTS = {"checkpoint_count", "request_count", "cache_hits", "rate_limit_responses", "retry_responses"}
WORKERS = {"worker_peak", "worker_limit", "peak_in_flight"}
API_SECONDS = {
    "network_seconds_total", "pacing_wait_seconds_total", "retry_wait_seconds_total",
    "evidence_seconds_total", "latency_seconds", "service_latency_seconds", "quota_reserve_seconds",
    "evidence_write_lock_wait_seconds_total", "evidence_read_lock_wait_seconds_total",
    "evidence_write_seconds_total", "evidence_read_seconds_total", "evidence_commit_seconds_total",
    "evidence_queue_wait_seconds_total",
}
API_CUMULATIVE_SECONDS = API_SECONDS - {"latency_seconds", "service_latency_seconds"}
API_COUNTS = {
    "completed_requests", "completed_endpoints", "cache_hits", "coalesced_hits",
    "rate_limit_responses", "retry_responses", "pressure_events",
    "quota_reserve_calls", "quota_admitted", "quota_denied",
    "local_deadline_timeouts",
    "evidence_commits", "evidence_write_operations", "evidence_read_operations",
    "evidence_write_batches",
}
# Maxima describe the Store lifetime. Unlike cumulative operation counters,
# they must remain gauges in probe windows rather than be subtracted.
API_MAXIMA = {"evidence_batch_size_max", "evidence_queue_depth_peak"}
# Match the Store's bounded writer without importing its database/thread code.
_API_MAXIMUM_LIMITS = {"evidence_batch_size_max": 64, "evidence_queue_depth_peak": 128}
API_STORAGE_ENUMS = {
    "quota_journal_mode": {"pending", "wal", "delete"},
    "quota_journal_mode_requested": {"wal", "delete"},
    "quota_synchronous": {"full"},
    "quota_connection_mode": {"persistent"},
    "evidence_journal_mode": {"wal", "delete"},
    "evidence_journal_mode_requested": {"wal", "delete"},
    "evidence_synchronous": {"full"},
}
API_DIAGNOSTIC_FIELDS = API_SECONDS | API_COUNTS | API_MAXIMA | set(API_STORAGE_ENUMS) | {
    "api_rate_mode", "api_target_rps", "shared_api_effective_rps", "shared_api_active_clients",
    "shared_api_peak_active_clients",
    "peak_in_flight", "in_flight", "quota_sqlite_version", "evidence_sqlite_version",
}


def public_api_rate(value):
    """Keep the pacing target distinct from observed request throughput."""
    clean = {}
    if value.get("api_rate_mode") in ("adaptive", "fixed"):
        clean["api_rate_mode"] = value["api_rate_mode"]
    for key in ("api_target_rps", "shared_api_effective_rps"):
        number = value.get(key)
        if type(number) in (int, float) and 0 <= number <= 2 ** 53 - 1 and math.isfinite(number):
            clean[key] = number
    return clean


def public_api_diagnostics(value):
    """Allow only known nonsecret timings and counters from an API snapshot.

    Worker totals overlap each other and wall time. Service latency is an EWMA
    including network/evidence work but excluding admission and retry waits;
    it must not be added to those totals or presented as a separate phase.
    Quota reserve time is cumulative and is already included in pacing time.
    Evidence operations count logical writes; commits count successful durable
    transactions. Queue waits accumulate caller time, while write-lock and
    commit timers measure the physical batches processing those operations.
    """
    if not isinstance(value, dict):
        return {}
    clean = public_api_rate(value)
    for key in API_SECONDS:
        number = value.get(key)
        if type(number) in (int, float) and 0 <= number <= 2 ** 53 - 1 and math.isfinite(number):
            clean[key] = number
    for key in API_COUNTS:
        number = value.get(key)
        if type(number) is int and 0 <= number <= 2 ** 53 - 1:
            clean[key] = number
    for key, maximum in _API_MAXIMUM_LIMITS.items():
        number = value.get(key)
        if type(number) is int and 0 <= number <= maximum:
            clean[key] = number
    for key in ("peak_in_flight", "in_flight"):
        workers = value.get(key)
        if type(workers) is int and 0 <= workers <= 64:
            clean[key] = workers
    for key in ("shared_api_active_clients", "shared_api_peak_active_clients"):
        clients = value.get(key)
        if type(clients) is int and 1 <= clients <= 65535:
            clean[key] = clients
    for key, allowed in API_STORAGE_ENUMS.items():
        option = value.get(key)
        if isinstance(option, str) and option in allowed:
            clean[key] = option
    for key in ("quota_sqlite_version", "evidence_sqlite_version"):
        version = value.get(key)
        if isinstance(version, str) and re.fullmatch(r"[0-9]{1,6}(?:\.[0-9]{1,6}){2}", version):
            clean[key] = version
    return clean


def public_performance(value):
    """Drop unknown content and malformed measurements, including booleans.

    Tracing, fetch waiting, checkpointing, and processing use the main thread's
    wall clock. Fields ending in ``_total`` accumulate worker time and can exceed
    the run's elapsed time because independent requests overlap.
    """
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        return {}
    clean = {}
    for key in SECONDS:
        number = value.get(key)
        if type(number) in (int, float) and 0 <= number <= 2 ** 53 - 1 and math.isfinite(number):
            clean[key] = number
    for key in COUNTS:
        number = value.get(key)
        if type(number) is int and 0 <= number <= 2 ** 53 - 1:
            clean[key] = number
    for key in WORKERS:
        number = value.get(key)
        if type(number) is int and 0 <= number <= 64:
            clean[key] = number
    clean.update(public_api_diagnostics(value))
    return {"schema_version": 1, **clean} if clean else {}
