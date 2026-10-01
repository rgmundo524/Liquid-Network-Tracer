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
}
API_COUNTS = {
    "completed_requests", "completed_endpoints", "cache_hits", "coalesced_hits",
    "rate_limit_responses", "retry_responses", "pressure_events",
    "quota_reserve_calls", "quota_admitted", "quota_denied",
    "local_deadline_timeouts",
}
API_STORAGE_ENUMS = {
    "quota_journal_mode": {"pending", "wal", "delete"},
    "quota_journal_mode_requested": {"wal", "delete"},
    "quota_synchronous": {"full"},
    "quota_connection_mode": {"persistent"},
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
    peak = value.get("peak_in_flight")
    if type(peak) is int and 0 <= peak <= 64:
        clean["peak_in_flight"] = peak
    for key, allowed in API_STORAGE_ENUMS.items():
        option = value.get(key)
        if isinstance(option, str) and option in allowed:
            clean[key] = option
    version = value.get("quota_sqlite_version")
    if isinstance(version, str) and re.fullmatch(r"[0-9]{1,6}(?:\.[0-9]{1,6}){2}", version):
        clean["quota_sqlite_version"] = version
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
