import unittest

from liquid_tracer.performance import (API_COUNTS, API_SECONDS, public_api_diagnostics,
                                       public_performance)


class CollectionPerformanceTests(unittest.TestCase):
    def test_only_known_numeric_metrics_leave_the_saved_report(self):
        value = {"schema_version": 1, "tracing_seconds": 12.5, "checkpoint_seconds": .2,
                 "network_seconds_total": 70, "request_count": 20, "worker_peak": 8,
                 "message": "PRIVATE", "token": "PRIVATE", "url": "PRIVATE"}
        clean = public_performance(value)
        self.assertEqual(clean, {key: value[key] for key in (
            "schema_version", "tracing_seconds", "checkpoint_seconds", "network_seconds_total",
            "request_count", "worker_peak")})
        # Parallel worker time is allowed to exceed elapsed time, not truncated
        # or presented as another additive wall-clock phase.
        self.assertGreater(clean["network_seconds_total"], clean["tracing_seconds"])

    def test_old_unknown_and_malformed_reports_are_ignored(self):
        for value in (None, [], "PRIVATE", {}, {"schema_version": True},
                      {"schema_version": 2, "request_count": 5}):
            with self.subTest(value=value):
                self.assertEqual(public_performance(value), {})
        for number in (True, False, -1, "2", None, float("nan"), float("inf"), 10 ** 400):
            with self.subTest(number=number):
                self.assertEqual(public_performance({"schema_version": 1, "tracing_seconds": number,
                    "request_count": number, "worker_peak": number}), {})
        self.assertEqual(public_performance({"schema_version": 1, "request_count": 1.5, "worker_limit": 65}), {})

    def test_saved_reports_include_validated_rate_mode_and_targets(self):
        for mode in ("adaptive", "fixed"):
            with self.subTest(mode=mode):
                value = {"schema_version": 1, "api_rate_mode": mode, "api_target_rps": 256,
                         "shared_api_effective_rps": 128, "request_count": 300}
                self.assertEqual(public_performance(value), value)
        for invalid in (True, None, "PRIVATE", [], {}, -1, float("nan"), float("inf"), 10 ** 400):
            with self.subTest(invalid=invalid):
                self.assertEqual(public_performance({"schema_version": 1, "api_rate_mode": invalid,
                    "api_target_rps": invalid, "shared_api_effective_rps": invalid}), {})

    def test_api_diagnostics_preserve_overlapping_timings_without_adding_them(self):
        value = {"network_seconds_total": 100, "evidence_seconds_total": 150,
                 "pacing_wait_seconds_total": 250, "retry_wait_seconds_total": 0,
                 "latency_seconds": .08, "service_latency_seconds": .15,
                 "completed_requests": 1200, "completed_endpoints": 1100,
                 "peak_in_flight": 12, "cache_hits": 2, "coalesced_hits": 3,
                 "pressure_events": 0, "rate_limit_responses": 0, "retry_responses": 0,
                 "quota_reserve_seconds": .5, "quota_reserve_calls": 1400,
                 "quota_admitted": 1201, "quota_denied": 199}
        self.assertEqual(public_api_diagnostics({**value, "headers": "PRIVATE",
                                               "endpoint": "PRIVATE"}), value)
        expected = {"schema_version": 1, "tracing_seconds": 60, **value}
        self.assertEqual(public_performance(expected), expected)
        self.assertGreater(expected["network_seconds_total"], expected["tracing_seconds"])

    def test_api_diagnostics_drop_malformed_measurements_and_unknown_fields(self):
        for invalid in (True, None, "PRIVATE", [], {}, -1, float("nan"), float("inf"), 10 ** 400):
            with self.subTest(invalid=invalid):
                value = {key: invalid for key in API_SECONDS | API_COUNTS | {"peak_in_flight"}}
                self.assertEqual(public_api_diagnostics(value), {})
        for invalid in (None, [], "PRIVATE"):
            self.assertEqual(public_api_diagnostics(invalid), {})
        self.assertEqual(public_api_diagnostics({"peak_in_flight": 65, "completed_requests": 3.5}), {})


if __name__ == "__main__":
    unittest.main()
