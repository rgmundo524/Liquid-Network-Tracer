"""Memory selection is bounded by the environment, without a graph ceiling."""

import json
import os
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.render_runtime import _available_bytes, renderer_failure, renderer_failure_code, renderer_heap_mb


GIB = 1024 ** 3


class MemoryBudgetTests(unittest.TestCase):
    def available(self, files):
        with patch("liquid_tracer.render_runtime._read", side_effect=lambda path: files.get(str(path), "")), \
                patch("liquid_tracer.render_runtime.os.sysconf", side_effect=ValueError):
            return _available_bytes()

    def test_64_gib_host_uses_90_percent_of_currently_available_memory(self):
        files = {"/proc/meminfo": "MemTotal:       67108864 kB\nMemAvailable:   50331648 kB\n"}
        available = self.available(files)
        self.assertEqual(available, 48 * GIB)
        with patch.dict(os.environ, {"LIQUID_RENDER_HEAP_MB": "auto"}), \
                patch("liquid_tracer.render_runtime._available_bytes", return_value=available):
            self.assertEqual(renderer_heap_mb(), 44236)

    def test_auto_recalculates_90_percent_for_each_render(self):
        with patch.dict(os.environ, {"LIQUID_RENDER_HEAP_MB": "auto"}), \
                patch("liquid_tracer.render_runtime._available_bytes", side_effect=[28 * GIB, 16 * GIB]):
            self.assertEqual(renderer_heap_mb(), 25804)
            self.assertEqual(renderer_heap_mb(), 14745)

    def test_auto_rounds_down_to_whole_mib(self):
        mib = 1024 ** 2
        for available, expected in ((2 * mib, 1), (3 * mib, 2), (10 * mib - 1, 8), (10 * mib, 9)):
            with self.subTest(available=available), \
                    patch.dict(os.environ, {"LIQUID_RENDER_HEAP_MB": "auto"}), \
                    patch("liquid_tracer.render_runtime._available_bytes", return_value=available):
                self.assertEqual(renderer_heap_mb(), expected)
                self.assertLessEqual(renderer_heap_mb() * mib * 10, available * 9)

    def test_auto_uses_90_percent_of_cgroup_headroom_not_host_memory(self):
        files = {"/proc/meminfo": "MemAvailable: 50331648 kB\n",
                 "/sys/fs/cgroup/memory.max": str(4 * GIB),
                 "/sys/fs/cgroup/memory.current": str(2 * GIB)}
        with patch.dict(os.environ, {"LIQUID_RENDER_HEAP_MB": "auto"}), \
                patch("liquid_tracer.render_runtime._read", side_effect=lambda path: files.get(str(path), "")):
            self.assertEqual(renderer_heap_mb(), 1843)

    def test_auto_preserves_integer_limit_and_rejects_sub_mib_budget(self):
        with patch.dict(os.environ, {"LIQUID_RENDER_HEAP_MB": "auto"}):
            with patch("liquid_tracer.render_runtime._available_bytes", return_value=10 ** 30):
                self.assertEqual(renderer_heap_mb(), 2147483647)
            with patch("liquid_tracer.render_runtime._available_bytes", return_value=1024 ** 2):
                with self.assertRaisesRegex(TraceError, "Too little available memory"):
                    renderer_heap_mb()

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
    def test_structured_engine_error_reports_only_safe_code_and_context(self):
        stderr = "PRIVATE-CASE-ID\nLIQUID_ELK_FAILURE " + json.dumps({
            "version": 1, "code": "elk_illegal_state", "stage": "traced_first_layout", "seed": 19,
            "branch_profile": "flow_weighted", "input_order_policy": "traced_first",
            "message": "PRIVATE-TOKEN /private/case/graph.json",
        })
        self.assertEqual(renderer_failure_code(stderr, 1), "elk_illegal_state")
        error = renderer_failure(stderr, 1, "ELK", 26902)
        self.assertIn("invalid internal layout state", error)
        self.assertIn("calculating the traced-first layout; seed 19; flow-weighted profile", error)
        self.assertNotIn("private", error.lower())
        self.assertNotIn("memory exhaustion", error)

    def test_invalid_structured_fields_cannot_be_reflected(self):
        for fields in ({"stage": ["private"]}, {"seed": True}, {"seed": 2147483648},
                       {"branch_profile": {"private": "value"}}, {"input_order_policy": "private"}):
            with self.subTest(fields=fields):
                stderr = "LIQUID_ELK_FAILURE " + json.dumps({"version": 1, "code": "elk_engine_error", **fields})
                error = renderer_failure(stderr, 1, "ELK", 1024)
                self.assertIn("unclassified engine error", error)
                self.assertNotIn("private", error)
                self.assertNotIn("Worker context", error)

    def test_untrusted_or_malformed_marker_does_not_establish_a_cause(self):
        for payload in ("PRIVATE", "null", "[]", '{"version":1,"code":"PRIVATE"}',
                        '{"version":true,"code":"elk_index_error"}',
                        '{"version":2,"code":"elk_index_error"}',
                        '{"version":1,"code":["PRIVATE"]}', "[" * 2000):
            with self.subTest(payload=payload[:70]):
                stderr = "LIQUID_ELK_FAILURE " + payload
                self.assertEqual(renderer_failure_code(stderr, 1), "unknown_exit")
                error = renderer_failure(stderr, 1, "ELK", 26902)
                self.assertNotIn("PRIVATE", error)
                self.assertIn("recognized cause", error)

    def test_mermaid_does_not_accept_elk_marker(self):
        stderr = 'LIQUID_ELK_FAILURE {"version":1,"code":"elk_index_error","seed":19}'
        self.assertEqual(renderer_failure_code(stderr, 1, "Mermaid"), "unknown_exit")

    def test_node_startup_failure_has_fixed_category_without_echoing_paths(self):
        for stderr in ("Error [ERR_MODULE_NOT_FOUND]: cannot find /PRIVATE/module.js",
                       "Error: MODULE_NOT_FOUND /PRIVATE/module.js",
                       "SyntaxError: unexpected PRIVATE at /PRIVATE/run.mjs"):
            with self.subTest(stderr=stderr):
                self.assertEqual(renderer_failure_code(stderr, 1), "elk_worker_setup")
                error = renderer_failure(stderr, 1, "ELK", 1024)
                self.assertIn("could not load or initialize", error)
                self.assertNotIn("PRIVATE", error)

    def test_fatal_runtime_signature_takes_precedence_over_structured_caught_error(self):
        stderr = ('LIQUID_ELK_FAILURE {"version":1,"code":"elk_engine_error"}\n'
                  'FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory')
        self.assertEqual(renderer_failure_code(stderr, -6), "heap_exhausted")

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
