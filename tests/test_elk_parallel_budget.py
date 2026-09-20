"""ELK workers share one memory allowance and respect available CPU capacity."""

import json
import os
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.render_runtime import _available_cpu_count, elk_worker_budget, renderer_peak_rss_mb


GIB = 1024 ** 3


class ElkWorkerBudgetTests(unittest.TestCase):
    def budget(self, attempts=25, workers="auto", heap="8192", cpus=16, peak=512):
        with patch.dict(os.environ, {"LIQUID_ELK_WORKERS": workers, "LIQUID_RENDER_HEAP_MB": heap}), \
                patch("liquid_tracer.render_runtime._available_cpu_count", return_value=cpus):
            return elk_worker_budget(attempts, peak_rss_mb=peak)

    def test_unmeasured_pilot_gets_entire_pool(self):
        self.assertEqual(self.budget(peak=None), (1, 8192, 8192))
        self.assertEqual(self.budget(peak=None, workers="64"), (1, 8192, 8192))

    def test_measured_auto_can_use_more_than_four_workers(self):
        self.assertEqual(self.budget(), (8, 8192, 1024))
        self.assertEqual(self.budget(attempts=100, heap="1000000", cpus=128), (64, 1000000, 15625))

    def test_measured_peak_receives_double_headroom_per_worker(self):
        self.assertEqual(self.budget(peak=1024), (4, 8192, 2048))
        self.assertEqual(self.budget(peak=1025), (3, 8192, 2730))
        self.assertEqual(self.budget(peak=3000), (1, 8192, 8192))

    def test_explicit_worker_count_is_a_ceiling(self):
        self.assertEqual(self.budget(workers="8"), (8, 8192, 1024))
        self.assertEqual(self.budget(workers="8", cpus=3), (3, 8192, 2730))
        self.assertEqual(self.budget(attempts=2, workers="8"), (2, 8192, 4096))
        self.assertEqual(self.budget(workers="1"), (1, 8192, 8192))

    def test_auto_is_bounded_by_cpu_count_and_remaining_attempts(self):
        self.assertEqual(self.budget(cpus=2), (2, 8192, 4096))
        self.assertEqual(self.budget(attempts=1), (1, 8192, 8192))

    def test_less_memory_reduces_workers_before_splitting(self):
        for heap, expected in ((4095, (3, 4095, 1365)), (2047, (1, 2047, 2047)),
                               (1024, (1, 1024, 1024)), (511, (1, 511, 511)), (1, (1, 1, 1))):
            with self.subTest(heap=heap):
                self.assertEqual(self.budget(heap=str(heap)), expected)

    def test_rounding_never_multiplies_or_oversubscribes_total_allowance(self):
        for heap in (1025, 3073, 8195, 65535):
            with self.subTest(heap=heap):
                workers, total, each = self.budget(heap=str(heap), workers="64", cpus=64)
                self.assertEqual(total, heap)
                self.assertLessEqual(workers * each, total)
                self.assertLess(total - workers * each, workers)

    def test_auto_total_is_90_percent_of_available_memory_not_90_percent_per_worker(self):
        with patch("liquid_tracer.render_runtime._available_bytes", return_value=48 * GIB):
            self.assertEqual(self.budget(heap="auto", peak=4096), (5, 44236, 8847))

    def test_each_new_batch_rechecks_available_memory(self):
        with patch("liquid_tracer.render_runtime._available_bytes", side_effect=[48 * GIB, 2 * GIB]) as available:
            self.assertEqual(self.budget(heap="auto", peak=4096), (5, 44236, 8847))
            self.assertEqual(self.budget(heap="auto"), (1, 1843, 1843))
            self.assertEqual(available.call_count, 2)

    def test_unreadable_memory_fallback_is_one_worker(self):
        with patch("liquid_tracer.render_runtime._available_bytes", return_value=None):
            self.assertEqual(self.budget(heap="auto", workers="64"), (1, 1024, 1024))

    def test_explicit_heap_is_shared_without_changing_existing_override_semantics(self):
        with patch("liquid_tracer.render_runtime._available_bytes", side_effect=AssertionError):
            self.assertEqual(self.budget(heap="32768"), (16, 32768, 2048))

    def test_setting_is_case_insensitive_and_trimmed(self):
        self.assertEqual(self.budget(workers=" AUTO "), (8, 8192, 1024))
        self.assertEqual(self.budget(workers=" 2 "), (2, 8192, 4096))

    def test_invalid_worker_setting_is_actionable_and_not_reflected(self):
        for value in ("", "0", "-1", "1.5", "65", "NaN", "4 --require PRIVATE", "1" * 5000):
            with self.subTest(value=value[:20]):
                with self.assertRaisesRegex(TraceError, "LIQUID_ELK_WORKERS must be auto or an integer from 1 to 64") as error:
                    self.budget(workers=value)
                self.assertIn("devenv.nix", str(error.exception))
                self.assertNotIn("PRIVATE", str(error.exception))

    def test_invalid_attempt_count_is_rejected(self):
        for value in (0, -1, True, "4", 1.5):
            with self.subTest(value=value), self.assertRaisesRegex(TraceError, "attempts must be a positive integer"):
                self.budget(attempts=value)

    def test_invalid_peak_measurement_is_rejected(self):
        for value in (0, -1, True, "4", 1.5, 2147483648):
            with self.subTest(value=value), self.assertRaisesRegex(TraceError, "peak memory must be a positive integer"):
                self.budget(peak=value)


class PeakMemoryTelemetryTests(unittest.TestCase):
    def marker(self, **payload):
        return "LIQUID_ELK_USAGE " + json.dumps({"version": 1, "peak_rss_mb": 2345, **payload})

    def test_reads_only_numeric_memory_measurement(self):
        stderr = "PRIVATE-CASE /private/path\n" + self.marker(token="PRIVATE-TOKEN") + "\n"
        self.assertEqual(renderer_peak_rss_mb(stderr), 2345)
        self.assertEqual(renderer_peak_rss_mb(stderr.encode()), 2345)

    def test_unknown_missing_or_malformed_payload_stays_unmeasured(self):
        for value in (None, 2345, b"", "PRIVATE-TOKEN", "LIQUID_ELK_USAGE null", "LIQUID_ELK_USAGE []",
                      "LIQUID_ELK_USAGE {}", "LIQUID_ELK_USAGE " + "[" * 2000):
            with self.subTest(value=str(value)[:50]):
                self.assertIsNone(renderer_peak_rss_mb(value))

    def test_rejects_boolean_overflow_zero_and_unknown_version(self):
        for fields in ({"version": True}, {"version": 2}, {"peak_rss_mb": True}, {"peak_rss_mb": 0},
                       {"peak_rss_mb": -1}, {"peak_rss_mb": 2147483648}, {"peak_rss_mb": "2345"},
                       {"peak_rss_mb": 2345.0}, {"peak_rss_mb": [2345]}):
            with self.subTest(fields=fields):
                self.assertIsNone(renderer_peak_rss_mb(self.marker(**fields)))

    def test_oversized_telemetry_line_is_not_used(self):
        self.assertIsNone(renderer_peak_rss_mb(self.marker(extra="x" * 4096)))

    def test_telemetry_near_end_survives_bounded_stderr_excerpt(self):
        self.assertEqual(renderer_peak_rss_mb("x" * 200000 + "\n" + self.marker()), 2345)
        self.assertIsNone(renderer_peak_rss_mb("x" * 100000 + "\n" + self.marker() + "\n" + "x" * 100000))


class CpuCapacityTests(unittest.TestCase):
    def cpus(self, files=None, host=32, affinity=range(16)):
        with patch("liquid_tracer.render_runtime._read", side_effect=lambda path: (files or {}).get(str(path), "")), \
                patch("liquid_tracer.render_runtime.os.cpu_count", return_value=host), \
                patch("liquid_tracer.render_runtime.os.sched_getaffinity", return_value=set(affinity), create=True):
            return _available_cpu_count()

    def test_affinity_bounds_host_cpu_count(self):
        self.assertEqual(self.cpus(affinity=range(3)), 3)
        self.assertEqual(self.cpus(host=2), 2)

    def test_v2_nested_and_ancestor_quotas_bound_affinity(self):
        files = {"/proc/self/cgroup": "0::/user.slice/app\n",
                 "/sys/fs/cgroup/cpu.max": "max 100000",
                 "/sys/fs/cgroup/user.slice/cpu.max": "250000 100000",
                 "/sys/fs/cgroup/user.slice/app/cpu.max": "400000 100000"}
        self.assertEqual(self.cpus(files), 2)

    def test_fractional_quota_still_allows_one_worker(self):
        self.assertEqual(self.cpus({"/sys/fs/cgroup/cpu.max": "50000 100000"}), 1)

    def test_v1_cpu_and_combined_mount_quotas_are_supported(self):
        for mount in ("cpu", "cpu,cpuacct", "cpuacct,cpu"):
            with self.subTest(mount=mount):
                files = {"/proc/self/cgroup": "5:cpu,cpuacct:/tasks/renderer\n",
                         f"/sys/fs/cgroup/{mount}/tasks/cpu.cfs_quota_us": "300000",
                         f"/sys/fs/cgroup/{mount}/tasks/cpu.cfs_period_us": "100000",
                         f"/sys/fs/cgroup/{mount}/tasks/renderer/cpu.cfs_quota_us": "600000",
                         f"/sys/fs/cgroup/{mount}/tasks/renderer/cpu.cfs_period_us": "100000"}
                self.assertEqual(self.cpus(files), 3)

    def test_namespace_root_is_checked_without_following_parent_traversal(self):
        files = {"/proc/self/cgroup": "0::/../../host/container\n",
                 "/sys/fs/cgroup/cpu.max": "200000 100000",
                 "/sys/host/container/cpu.max": "100000 100000"}
        self.assertEqual(self.cpus(files), 2)

    def test_unlimited_missing_or_invalid_quotas_do_not_override_affinity(self):
        for value in ("max 100000", "", "100000 0", "-1 100000", "NaN 100000", "100000", "1" * 5000):
            with self.subTest(value=value[:20]):
                self.assertEqual(self.cpus({"/sys/fs/cgroup/cpu.max": value}, affinity=range(3)), 3)
        self.assertEqual(self.cpus({"/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "-1",
                                    "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000"}, affinity=range(3)), 3)

    def test_affinity_unavailable_uses_remaining_limits(self):
        with patch("liquid_tracer.render_runtime._read", return_value=""), \
                patch("liquid_tracer.render_runtime.os.cpu_count", return_value=6), \
                patch("liquid_tracer.render_runtime.os.sched_getaffinity", side_effect=OSError, create=True):
            self.assertEqual(_available_cpu_count(), 6)

    def test_unknown_cpu_capacity_falls_back_to_one(self):
        self.assertEqual(self.cpus(host=None, affinity=()), 1)


if __name__ == "__main__":
    unittest.main()
