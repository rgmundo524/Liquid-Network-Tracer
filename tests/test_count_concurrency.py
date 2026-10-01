"""Adaptive lookup policy scales network overlap, never provider quotas."""

import os
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.count_concurrency import CountConcurrency


MIB = 1024 * 1024


class CountConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        for name, options in (
                ("time.monotonic", {"side_effect": lambda: self.now}),
                ("_available_cpu_count", {"return_value": 16}),
                ("_available_bytes", {"return_value": 32 * 1024 * MIB})):
            patcher = patch("liquid_tracer.count_concurrency." + name, **options)
            mock = patcher.start()
            self.addCleanup(patcher.stop)
            if name == "_available_cpu_count":
                self.cpus = mock
            elif name == "_available_bytes":
                self.memory = mock
        setting = patch.dict(os.environ, {"LIQUID_COUNT_WORKERS": "auto"})
        setting.start()
        self.addCleanup(setting.stop)

    def metrics(self, completed=0, latency=1.0, pressure=0):
        return {"completed_requests": completed, "latency_seconds": latency,
                "pressure_events": pressure}

    def test_auto_grows_beyond_eight_gradually_with_a_hard_sixty_four_limit(self):
        policy = CountConcurrency(8, 7956, 49)
        self.assertEqual((policy.mode, policy.ceiling, policy.peak), ("auto", 64, 8))
        self.assertEqual(policy.target(self.metrics()), 8)
        self.assertEqual(policy.target(self.metrics(7)), 8)
        self.assertEqual(policy.target(self.metrics(8)), 16)
        self.assertEqual(policy.target(self.metrics(16)), 32)
        self.assertEqual(policy.target(self.metrics(24, latency=2)), 64)
        self.assertEqual(policy.target(self.metrics(32, latency=20)), 64)
        self.assertEqual(policy.peak, 64)

    def test_repeated_or_single_completion_callbacks_do_not_jump_exponentially(self):
        policy = CountConcurrency(8, 1000, 49)
        self.assertEqual(policy.target(self.metrics(8)), 16)
        for completed in (8, 8, 9, 10, 11, 12, 13, 14, 15):
            self.assertEqual(policy.target(self.metrics(completed)), 16)
        self.assertEqual(policy.target(self.metrics(1000)), 32)
        self.assertEqual(policy.target(self.metrics(1000)), 32)

    def test_latency_target_tracks_existing_rate_instead_of_increasing_it(self):
        policy = CountConcurrency(8, 7956, 49)
        self.assertEqual(policy.target(self.metrics(8, latency=.1)), 7)
        self.assertEqual(policy.target(self.metrics(16, latency=.001)), 1)
        public = CountConcurrency(8, 7956, 4)
        self.assertEqual(public.target(self.metrics(8, latency=.25)), 2)
        self.assertEqual(public.target(self.metrics(16, latency=.25)), 2)

    def test_service_time_overlaps_evidence_work_beyond_fast_network_responses(self):
        policy = CountConcurrency(8, 972456, 49)
        # Network-only sizing previously selected three workers. A worker is
        # actually occupied for 400 ms including durable evidence collection.
        metrics = self.metrics(8, latency=.048)
        metrics.update(completed_endpoints=7, service_latency_seconds=.4)
        self.assertEqual(policy.target(metrics), 8)
        metrics.update(completed_requests=16, completed_endpoints=8)
        self.assertEqual(policy.target(metrics), 16)
        self.assertEqual(policy.target(metrics), 16)
        metrics.update(completed_requests=24, completed_endpoints=16)
        self.assertEqual(policy.target(metrics), 25)
        self.assertEqual(policy.ceiling, 64)
        self.assertEqual(policy._rate, 49)

    def test_network_completions_do_not_advance_service_measurement_window(self):
        policy = CountConcurrency(8, 972456, 49)
        metrics = self.metrics(800, latency=2)
        metrics.update(completed_endpoints=0, service_latency_seconds=None)
        self.assertEqual(policy.target(metrics), 8)
        metrics.update(completed_endpoints=8, service_latency_seconds=2)
        self.assertEqual(policy.target(metrics), 16)
        metrics["completed_requests"] = 900
        self.assertEqual(policy.target(metrics), 16)

    def test_fast_service_samples_preserve_initial_overlap_without_quota_feedback(self):
        policy = CountConcurrency(8, 972456, 49)
        metrics = self.metrics(8, latency=.001)
        metrics.update(completed_endpoints=8, service_latency_seconds=.048,
                       pacing_wait_seconds_total=10000,
                       retry_wait_seconds_total=50000)
        self.assertEqual(policy.target(metrics), 8)
        for completed in (16, 24, 32):
            metrics["completed_endpoints"] = completed
            metrics["pacing_wait_seconds_total"] *= 2
            self.assertEqual(policy.target(metrics), 8)
        self.assertEqual(policy.peak, 8)

    def test_service_target_still_halves_on_pressure_and_recovers_after_clean_window(self):
        policy = CountConcurrency(8, 972456, 49)
        metrics = self.metrics(8, latency=.001)
        metrics.update(completed_endpoints=8, service_latency_seconds=.04)
        self.assertEqual(policy.target(metrics), 8)
        metrics.update(completed_endpoints=9, pressure_events=1)
        self.assertEqual(policy.target(metrics), 4)
        metrics["completed_endpoints"] = 17
        self.assertEqual(policy.target(metrics), 4)
        metrics["completed_endpoints"] = 25
        self.assertEqual(policy.target(metrics), 8)
        metrics.update(completed_endpoints=26, pressure_events=2)
        self.assertEqual(policy.target(metrics), 4)

    def test_service_target_obeys_saved_fixed_backlog_and_resource_limits(self):
        metrics = self.metrics(1000, latency=.001)
        metrics.update(completed_endpoints=1000, service_latency_seconds=30)
        self.assertEqual(CountConcurrency(3, 972456, 49).target(metrics), 3)
        self.assertEqual(CountConcurrency(8, 3, 49).target(metrics), 3)
        with patch.dict(os.environ, {"LIQUID_COUNT_WORKERS": "32"}):
            self.assertEqual(CountConcurrency(8, 972456, 49).target(metrics), 32)
        self.memory.return_value = 256 * MIB
        self.assertEqual(CountConcurrency(8, 972456, 49).target(metrics), 2)
        self.memory.return_value = 32 * 1024 * MIB
        self.cpus.return_value = 1
        self.assertEqual(CountConcurrency(8, 972456, 49).target(metrics), 8)

    def test_invalid_service_sample_falls_back_to_legacy_network_sample(self):
        for service in (None, 0, -1, "1", True, float("inf"), float("nan")):
            with self.subTest(service=service):
                policy = CountConcurrency(8, 972456, 49)
                metrics = self.metrics(8, latency=.1)
                metrics.update(completed_endpoints=8, service_latency_seconds=service)
                self.assertEqual(policy.target(metrics), 7)

    def test_saved_low_worker_settings_remain_caps_in_auto_mode(self):
        for initial in (1, 2, 3, 7):
            with self.subTest(initial=initial):
                policy = CountConcurrency(initial, 7956, 49)
                self.assertEqual(policy.ceiling, initial)
                self.assertEqual(policy.target(self.metrics(100, latency=30)), initial)
        self.assertEqual(CountConcurrency(None, 7956, 49).ceiling, 64)

    def test_backlog_caps_workers_but_retries_do_not_consume_backlog(self):
        policy = CountConcurrency(8, 3, 49)
        self.assertEqual(policy.ceiling, 3)
        self.assertEqual(policy.target(self.metrics(64)), 3)

    def test_unknown_memory_is_conservative_and_low_memory_stays_serial(self):
        for available, expected in ((None, 8), (0, 1), (127 * MIB, 1),
                                    (256 * MIB, 2), (1024 * MIB, 8)):
            with self.subTest(available=available):
                self.memory.return_value = available
                policy = CountConcurrency(8, 7956, 49)
                self.assertEqual(policy.ceiling, expected)
                self.assertEqual(policy.target(self.metrics(100, latency=30)), expected)

    def test_cpu_allowance_caps_io_workers(self):
        for cpus, expected in ((1, 8), (2, 16), (4, 32), (8, 64), (128, 64)):
            with self.subTest(cpus=cpus):
                self.cpus.return_value = cpus
                self.assertEqual(CountConcurrency(8, 7956, 49).ceiling, expected)

    def test_resources_are_resampled_once_per_second_and_reduce_target_immediately(self):
        policy = CountConcurrency(8, 7956, 49)
        self.assertEqual(policy.target(self.metrics(8)), 16)
        self.memory.return_value = 256 * MIB
        self.now += .99
        self.assertEqual(policy.target(self.metrics(9)), 16)
        self.assertEqual(self.memory.call_count, 1)
        self.now += .01
        self.assertEqual(policy.target(self.metrics(10)), 2)
        self.assertEqual(policy.ceiling, 2)
        self.assertEqual(self.memory.call_count, 2)
        self.assertEqual(policy.peak, 16)
        self.memory.return_value = 32 * 1024 * MIB
        self.now += 1
        self.assertEqual(policy.target(self.metrics(11)), 2)
        self.assertEqual(policy.target(self.metrics(16)), 4)

    def test_pressure_halves_target_then_requires_a_clean_window_before_growth(self):
        policy = CountConcurrency(8, 7956, 49)
        self.assertEqual(policy.target(self.metrics(8)), 16)
        self.assertEqual(policy.target(self.metrics(9, pressure=1)), 8)
        self.assertEqual(policy.target(self.metrics(9, pressure=1)), 8)
        self.assertEqual(policy.target(self.metrics(16, pressure=1)), 8)
        self.assertEqual(policy.target(self.metrics(17, pressure=1)), 8)
        self.assertEqual(policy.target(self.metrics(25, pressure=1)), 16)
        self.assertEqual(policy.target(self.metrics(26, pressure=2)), 8)
        self.assertEqual(policy.target(self.metrics(27, pressure=3)), 4)
        self.assertEqual(policy.target(self.metrics(28, pressure=4)), 2)
        self.assertEqual(policy.target(self.metrics(29, pressure=5)), 1)
        self.assertEqual(policy.target(self.metrics(30, pressure=6)), 1)

    def test_fixed_environment_overrides_saved_setting_but_respects_resources(self):
        with patch.dict(os.environ, {"LIQUID_COUNT_WORKERS": " 32 "}):
            policy = CountConcurrency(1, 7956, 4)
            self.assertEqual((policy.mode, policy.ceiling, policy.peak), ("fixed", 32, 32))
            self.assertEqual(policy.target(self.metrics(8, latency=.001)), 32)
            self.assertEqual(policy.target(self.metrics(9, pressure=1)), 16)
            self.assertEqual(policy.target(self.metrics(17, pressure=1)), 16)
            self.assertEqual(policy.target(self.metrics(25, pressure=1)), 32)
            self.cpus.return_value = 1
            self.now += 1
            self.assertEqual(policy.target(self.metrics(26, pressure=1)), 8)
            self.assertEqual(policy.ceiling, 8)
        with patch.dict(os.environ, {"LIQUID_COUNT_WORKERS": "1"}):
            self.assertEqual(CountConcurrency(8, 7956, 49).target(self.metrics(100)), 1)

    def test_fixture_rate_zero_keeps_initial_worker_count(self):
        policy = CountConcurrency(8, 7956, 0)
        self.assertEqual(policy.target(self.metrics(8, latency=100)), 8)
        self.assertEqual(policy.target(self.metrics(16, latency=.0001)), 8)

    def test_unknown_latency_does_not_increase_workers(self):
        for latency in (None, 0, -1, "1", True, float("inf"), float("nan")):
            with self.subTest(latency=latency):
                policy = CountConcurrency(8, 7956, 49)
                self.assertEqual(policy.target(self.metrics(8, latency=latency)), 8)
        self.assertEqual(CountConcurrency(8, 7956, 49).target({}), 8)

    def test_invalid_configuration_fails_before_starting_workers(self):
        for value in ("", "0", "65", "1.5", "-1", "+2", "none", "128"):
            with self.subTest(setting=value), patch.dict(os.environ, {"LIQUID_COUNT_WORKERS": value}):
                with self.assertRaisesRegex(TraceError, "LIQUID_COUNT_WORKERS"):
                    CountConcurrency(8, 7956, 49)
        for initial in (0, 9, True, 1.5, "8"):
            with self.subTest(initial=initial), self.assertRaisesRegex(TraceError, "workers"):
                CountConcurrency(initial, 7956, 49)
        for total in (0, -1, True, "10"):
            with self.subTest(total=total), self.assertRaisesRegex(TraceError, "backlog"):
                CountConcurrency(8, total, 49)
        for rate in (-1, None, True, "49", float("inf"), float("nan")):
            with self.subTest(rate=rate), self.assertRaisesRegex(TraceError, "rate"):
                CountConcurrency(8, 7956, rate)

    def test_adaptive_api_rate_raises_worker_demand_beyond_initial_forty_nine(self):
        policy = CountConcurrency(8, 972456, 49)
        feedback = {"api_rate_mode": "adaptive", "service_latency_seconds": .1}
        for completed, rate, expected in ((8, 49, 8), (16, 128, 16),
                                           (24, 256, 32), (32, 512, 64)):
            feedback.update(completed_endpoints=completed, api_target_rps=rate,
                            shared_api_effective_rps=rate)
            self.assertEqual(policy.target(feedback), expected)
        self.assertEqual(policy.peak, 64)

    def test_adaptive_rate_pressure_reduces_workers_then_holds_a_clean_window(self):
        policy = CountConcurrency(8, 972456, 49)
        feedback = {"api_rate_mode": "adaptive", "api_target_rps": 512,
                    "shared_api_effective_rps": 512, "service_latency_seconds": .1}
        for completed in (8, 16, 24):
            feedback["completed_endpoints"] = completed
            policy.target(feedback)
        self.assertEqual(policy.peak, 64)
        feedback.update(completed_endpoints=25, pressure_events=1,
                        api_target_rps=256, shared_api_effective_rps=256)
        self.assertEqual(policy.target(feedback), 32)
        feedback.update(completed_endpoints=33, api_target_rps=512, shared_api_effective_rps=512)
        self.assertEqual(policy.target(feedback), 32)
        feedback["completed_endpoints"] = 41
        self.assertEqual(policy.target(feedback), 64)

    def test_fixed_rate_and_lower_shared_allowance_do_not_inflate_worker_demand(self):
        feedback = {"api_rate_mode": "fixed", "api_target_rps": 512,
                    "shared_api_effective_rps": 512, "service_latency_seconds": .1}
        fixed = CountConcurrency(8, 972456, 49)
        shared = CountConcurrency(8, 972456, 49)
        for completed in (8, 16, 24):
            feedback["completed_endpoints"] = completed
            self.assertEqual(fixed.target(feedback), 8)
            self.assertEqual(shared.target({**feedback, "api_rate_mode": "adaptive",
                                           "shared_api_effective_rps": 49}), 8)

    def test_malformed_adaptive_target_uses_configured_rate(self):
        for invalid in (None, 0, -1, "512", True, [], {}, float("nan"), float("inf"), 10 ** 400):
            with self.subTest(invalid=invalid):
                policy = CountConcurrency(8, 972456, 49)
                feedback = {"completed_endpoints": 8, "service_latency_seconds": .1,
                            "api_rate_mode": "adaptive", "api_target_rps": invalid,
                            "shared_api_effective_rps": invalid}
                self.assertEqual(policy.target(feedback), 8)

    def test_trace_workers_follow_same_adaptive_api_target(self):
        from liquid_tracer.trace_fetch import TraceConcurrency
        with patch.dict(os.environ, {"LIQUID_TRACE_WORKERS": "auto"}):
            policy = TraceConcurrency(8, 0, 49)
            feedback = {"api_rate_mode": "adaptive", "api_target_rps": 512,
                        "shared_api_effective_rps": 512, "service_latency_seconds": .1}
            for completed, expected in ((8, 16), (16, 32), (24, 64)):
                self.assertEqual(policy.target({**feedback, "completed_endpoints": completed}), expected)


if __name__ == "__main__":
    unittest.main()
