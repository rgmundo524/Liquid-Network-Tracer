"""Bounded, nonsecret collection timing reports for saved runs and the UI."""

import math


SECONDS = {
    "tracing_seconds", "fetch_wait_seconds", "checkpoint_seconds", "processing_seconds",
    "address_counts_seconds", "network_seconds_total", "pacing_wait_seconds_total",
    "retry_wait_seconds_total", "evidence_seconds_total",
}
COUNTS = {"checkpoint_count", "request_count", "cache_hits", "rate_limit_responses", "retry_responses"}
WORKERS = {"worker_peak", "worker_limit", "peak_in_flight"}


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
    clean.update(public_api_rate(value))
    return {"schema_version": 1, **clean} if clean else {}
