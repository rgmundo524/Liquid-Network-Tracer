"""Memory selection is bounded by the environment, without a graph ceiling."""

import os
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.render_runtime import _available_bytes, renderer_failure, renderer_heap_mb


GIB = 1024 ** 3


class MemoryBudgetTests(unittest.TestCase):
    def available(self, files):
        with patch("liquid_tracer.render_runtime._read", side_effect=lambda path: files.get(str(path), "")), \
                patch("liquid_tracer.render_runtime.os.sysconf", side_effect=ValueError):
            return _available_bytes()

    def test_64_gib_host_keeps_half_currently_available_memory(self):
        files = {"/proc/meminfo": "MemTotal:       67108864 kB\nMemAvailable:   50331648 kB\n"}
        available = self.available(files)
        self.assertEqual(available, 48 * GIB)
        with patch.dict(os.environ, {"LIQUID_RENDER_HEAP_MB": "auto"}), \
                patch("liquid_tracer.render_runtime._available_bytes", return_value=available):
            self.assertEqual(renderer_heap_mb(), 24576)

    def test_v2_nested_and_ancestor_remaining_memory_constrain_host(self):
        files = {"/proc/meminfo": "MemTotal: 67108864 kB\nMemAvailable: 50331648 kB\n",
                 "/proc/self/cgroup": "0::/user.slice/app\n",
                 "/sys/fs/cgroup/memory.max": "max",
                 "/sys/fs/cgroup/user.slice/memory.max": str(8 * GIB),
                 "/sys/fs/cgroup/user.slice/memory.current": str(6 * GIB),
                 "/sys/fs/cgroup/user.slice/app/memory.max": str(4 * GIB),
                 "/sys/fs/cgroup/user.slice/app/memory.current": str(GIB)}
        self.assertEqual(self.available(files), 2 * GIB)

    def test_v1_limits_and_usage_are_respected(self):
        files = {"/proc/meminfo": "MemAvailable: 50331648 kB\n",
                 "/proc/self/cgroup": "5:cpu,memory:/tasks/renderer\n",
                 "/sys/fs/cgroup/memory/tasks/renderer/memory.limit_in_bytes": str(4 * GIB),
                 "/sys/fs/cgroup/memory/tasks/renderer/memory.usage_in_bytes": str(GIB)}
        self.assertEqual(self.available(files), 3 * GIB)

    def test_container_namespace_root_limit_is_checked_without_following_parent_traversal(self):
        files = {"/proc/meminfo": "MemAvailable: 50331648 kB\n",
                 "/proc/self/cgroup": "0::/../../host/container\n",
                 "/sys/fs/cgroup/memory.max": str(2 * GIB),
                 "/sys/fs/cgroup/memory.current": str(GIB)}
        self.assertEqual(self.available(files), GIB)

    def test_available_host_memory_can_be_lower_than_cgroup_headroom(self):
        files = {"/proc/meminfo": "MemAvailable: 1048576 kB\n",
                 "/sys/fs/cgroup/memory.max": str(8 * GIB),
                 "/sys/fs/cgroup/memory.current": str(GIB)}
        self.assertEqual(self.available(files), GIB)

    def test_exhausted_cgroup_stops_before_spawning_instead_of_using_host_ram(self):
        files = {"/proc/meminfo": "MemAvailable: 50331648 kB\n",
                 "/sys/fs/cgroup/memory.max": str(GIB),
                 "/sys/fs/cgroup/memory.current": str(2 * GIB)}
        self.assertEqual(self.available(files), 0)
        with patch.dict(os.environ, {"LIQUID_RENDER_HEAP_MB": "auto"}), \
                patch("liquid_tracer.render_runtime._available_bytes", return_value=0):
            with self.assertRaisesRegex(TraceError, "Too little available memory"):
                renderer_heap_mb()

    def test_unreadable_usage_still_constrains_by_known_cgroup_limit(self):
        files = {"/proc/meminfo": "MemAvailable: 50331648 kB\n",
                 "/sys/fs/cgroup/memory.max": str(2 * GIB)}
        self.assertEqual(self.available(files), 2 * GIB)

    def test_portable_fallback_when_memory_cannot_be_detected(self):
        self.assertIsNone(self.available({}))
        with patch.dict(os.environ, {"LIQUID_RENDER_HEAP_MB": "auto"}), \
                patch("liquid_tracer.render_runtime._available_bytes", return_value=None):
            self.assertEqual(renderer_heap_mb(), 1024)

    def test_explicit_budget_does_not_probe_or_silently_cap_requested_memory(self):
        with patch.dict(os.environ, {"LIQUID_RENDER_HEAP_MB": "32768"}), \
                patch("liquid_tracer.render_runtime._available_bytes", side_effect=AssertionError):
            self.assertEqual(renderer_heap_mb(), 32768)

    def test_invalid_configuration_does_not_become_a_node_option_or_echo_input(self):
        for value in ("", "0", "-1", "1.5", "NaN", "32768 --require private", "2147483648", "1" * 5000):
            with self.subTest(value=value[:20]), patch.dict(os.environ, {"LIQUID_RENDER_HEAP_MB": value}):
                with self.assertRaisesRegex(TraceError, "must be auto or a positive integer") as raised:
                    renderer_heap_mb()
                self.assertNotIn("private", str(raised.exception))


class RendererDiagnosticTests(unittest.TestCase):
    def test_heap_abort_is_recognized_without_echoing_stderr(self):
        error = renderer_failure("SYNTHETIC-PRIVATE\nFATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory", -6, "ELK", 32768)
        self.assertIn("JavaScript heap was exhausted", error)
        self.assertIn("32,768 MiB", error)
        self.assertIn("signal 6", error)
        self.assertNotIn("SYNTHETIC", error)

    def test_signal_alone_does_not_prove_memory_exhaustion(self):
        error = renderer_failure("SYNTHETIC-PRIVATE", -6, "ELK", 16384)
        self.assertIn("cause was not confirmed", error)
        self.assertNotIn("SYNTHETIC", error)

    def test_mermaid_heap_failure_does_not_promise_browser_can_use_requested_budget(self):
        error = renderer_failure("JavaScript heap out of memory", 1, "Mermaid", 32768)
        self.assertIn("lower heap limit than requested", error)
        self.assertIn("ELK preview", error)

    def test_stack_exhaustion_is_distinct_from_heap_budget(self):
        error = renderer_failure("RangeError: Maximum call stack size exceeded", 1, "Mermaid", 16384)
        self.assertIn("call-stack limit", error)
        self.assertIn("increasing heap memory alone may not resolve", error)

    def test_browser_protocol_timeout_is_distinct_from_installation(self):
        error = renderer_failure("ProtocolError: Runtime.callFunctionOn timed out", 1, "Mermaid", 8192)
        self.assertIn("browser protocol operation timed out", error)
        self.assertNotIn("could not start", error)

    def test_browser_launch_failure_has_specific_guidance(self):
        error = renderer_failure(b"Error: Could not find Chrome private-path", 1, "Mermaid", 8192)
        self.assertIn("Chromium could not start", error)
        self.assertNotIn("private-path", error)

    def test_unrecognized_mermaid_failure_does_not_blame_installation(self):
        error = renderer_failure("SYNTHETIC-PRIVATE", 1, "Mermaid", 8192)
        self.assertIn("recognized cause", error)
        self.assertNotIn("installation", error)
        self.assertNotIn("SYNTHETIC", error)
