import unittest

from liquid_tracer.performance import public_performance


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


if __name__ == "__main__":
    unittest.main()
