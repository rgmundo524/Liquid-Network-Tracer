"""The next ELK failure automatically retains a private, bounded stack report."""

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from liquid_tracer.common import TraceError
from liquid_tracer.elk_diagnostics import save_failure_report
from liquid_tracer.elk_errors import ElkWorkerFailure
from liquid_tracer.elk_layout import _worker, optimize_graph
from liquid_tracer.progress import public_progress
from tests.test_elk_layout import crossing_graph, synthetic_candidate


PRIVATE_MESSAGE = "SYNTHETIC-private-error-message"
PRIVATE_GRAPH = "SYNTHETIC-private-graph"
STACK = "RangeError: Maximum call stack size exceeded\n    at visit (elk-worker.js:42:9)\n    at visit (elk-worker.js:42:9)"


def failure_stderr(*, trace=None, native=""):
    context = {"version": 1, "stage": "geometry_layout", "code": "stack_limit", "seed": 19,
               "branch_profile": "flow_weighted", "input_order_policy": "geometry"}
    trace = trace if trace is not None else {
        **context, "engine_version": "0.12.0", "engine_build": "non_minified",
        "runtime": {"node": "22.18.0", "v8": "12.4.254.21-node.27", "platform": "linux", "arch": "x64"},
        "errors": [{"name": "RangeError", "message": PRIVATE_MESSAGE, "stack": STACK},
                   {"name": "Error", "message": "nested failure", "stack": "Error: nested failure\n    at recurse (elk-worker.js:71:5)"}],
        "truncated": False,
    }
    return "LIQUID_ELK_FAILURE " + json.dumps(context) + "\nLIQUID_ELK_TRACE " + json.dumps(trace) + "\n" + native


def request_graph():
    return {"id": PRIVATE_GRAPH, "children": [{"id": PRIVATE_GRAPH, "ports": [{"id": PRIVATE_GRAPH}]}],
            "edges": [{"id": PRIVATE_GRAPH}], "labels": [PRIVATE_GRAPH],
            "branchProfile": "flow_weighted", "boundaryOrdering": True,
            "centerNodeOrder": [PRIVATE_GRAPH], "inputPortOrders": {PRIVATE_GRAPH: [PRIVATE_GRAPH]}}


class ElkDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root),
                                                 "MIRO_ACCESS_TOKEN": "SYNTHETIC-private-environment"}))
        self.terminal = self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def save(self, stderr=None, **changes):
        options = {"graph": request_graph(), "seeds": [19], "failure_code": "stack_limit", "returncode": 1,
                   "heap_mb": 4096, "elapsed_seconds": 2.5, "engine_version": "0.12.0", **changes}
        return save_failure_report(failure_stderr() if stderr is None else stderr, **options)

    def test_private_report_has_stack_runtime_context_and_counts_without_graph(self):
        path = self.save()
        self.assertIsInstance(path, Path)
        self.assertTrue(path.is_relative_to(self.root / "liquid-tracer" / "diagnostics"))
        text = path.read_text()
        report = json.loads(text)
        self.assertEqual(report["failure_code"], "stack_limit")
        self.assertEqual(report["graph_counts"], {"nodes": 1, "edges": 1, "ports": 1})
        self.assertEqual(report["seeds"], [19])
        self.assertEqual(report["heap_mb"], 4096)
        self.assertIn("visit (elk-worker.js:42:9)", text)
        self.assertIn("recurse (elk-worker.js:71:5)", text)
        self.assertIn("22.18.0", text)
        self.assertIn("geometry_layout", text)
        self.assertNotIn(PRIVATE_GRAPH, text)
        self.assertNotIn("SYNTHETIC-private-environment", text)
        self.assertIn(str(path), self.terminal.getvalue())
        self.assertNotIn(PRIVATE_MESSAGE, self.terminal.getvalue())
        self.assertNotIn("visit", self.terminal.getvalue())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_parallel_failures_get_distinct_complete_reports(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(pool.map(lambda _: self.save(), range(16)))
        self.assertEqual(len(set(paths)), 16)
        for path in paths:
            self.assertIsNotNone(path)
            self.assertEqual(json.loads(path.read_text())["failure_code"], "stack_limit")

    def test_native_abort_without_javascript_trace_still_gets_report(self):
        path = self.save("native-before\nFATAL ERROR: Reached heap limit\nnative-last\n",
                         failure_code="heap_exhausted", returncode=-6)
        report = json.loads(path.read_text())
        self.assertEqual(report["returncode"], -6)
        self.assertEqual(report["failure_code"], "heap_exhausted")
        self.assertIn("native-last", report["stderr_tail"])

    def test_oversized_native_output_is_bounded_and_keeps_last_lines(self):
        path = self.save("PRIVATE-NATIVE-BEGIN\n" + "x" * (1024 * 1024) + "\nNATIVE-END\n")
        report = json.loads(path.read_text())
        self.assertLess(path.stat().st_size, 300 * 1024)
        self.assertIn("NATIVE-END", report["stderr_tail"])
        self.assertNotIn("PRIVATE-NATIVE-BEGIN", report["stderr_tail"])

    def test_unicode_escaping_cannot_expand_report_beyond_encoded_limit(self):
        trace = {"version": 1, "code": "stack_limit", "errors": [
            {"name": "\U0001f680" * 128, "message": "\U0001f680" * 1024, "stack": "\U0001f680" * 4096}
            for _ in range(4)]}
        stderr = "LIQUID_ELK_TRACE " + json.dumps(trace, ensure_ascii=False) + "\n" + "\U0001f680" * 32768
        path = self.save(stderr)
        self.assertLessEqual(path.stat().st_size, 262144)
        report = json.loads(path.read_text())
        self.assertEqual(len(report["worker"]["errors"]), 4)
        self.assertTrue(report["worker"]["truncated"])

    def test_extra_trace_fields_and_graph_are_not_copied_into_report(self):
        trace = {"version": 1, "code": "stack_limit", "errors": [{"name": "RangeError", "stack": STACK,
                  "private_extra": PRIVATE_GRAPH}], "private_extra": PRIVATE_GRAPH,
                 "graph": request_graph(), "runtime": {"node": "22.18.0", "environment": PRIVATE_GRAPH}}
        path = self.save(failure_stderr(trace=trace))
        text = path.read_text()
        self.assertNotIn(PRIVATE_GRAPH, text)
        self.assertNotIn("LIQUID_ELK_TRACE", json.loads(text).get("stderr_tail", ""))

    def test_relative_xdg_state_uses_home_instead_of_working_directory(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": "relative-state"}), \
                patch("pathlib.Path.home", return_value=self.root):
            path = self.save()
        self.assertTrue(path.is_relative_to(self.root / ".local" / "state" / "liquid-tracer" / "diagnostics"))

    def test_write_failure_is_advisory_and_does_not_echo_exception(self):
        blocker = self.root / "not-a-directory"
        blocker.write_text("occupied")
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(blocker)}):
            self.assertIsNone(self.save())
        self.assertIn("could not save", self.terminal.getvalue().lower())
        self.assertNotIn(PRIVATE_MESSAGE, self.terminal.getvalue())

    @contextlib.contextmanager
    def worker_process(self, *, returncode=1, stderr=None):
        process = Mock(returncode=returncode, pid=12345)
        output = json.dumps({"version": "0.12.0", "candidates": [{}]}) if returncode == 0 else "PRIVATE-STDOUT"
        process.communicate.return_value = (output, failure_stderr() if stderr is None else stderr)
        process.poll.return_value = returncode
        with patch.dict(os.environ, {"LIQUID_NODE_BIN": "/synthetic/node", "LIQUID_RENDER_HEAP_MB": "4096"}), \
                patch("pathlib.Path.is_file", return_value=True), \
                patch("liquid_tracer.elk_layout.subprocess.Popen", return_value=process):
            yield process

    def test_worker_automatically_saves_next_failure_and_keeps_progress_clean(self):
        progress = []
        with self.worker_process():
            with self.assertRaises(ElkWorkerFailure) as raised:
                _worker(request_graph(), [19], progress=progress.append)
        path = raised.exception.diagnostic_path
        self.assertIsNotNone(path)
        report = Path(path).read_text()
        self.assertIn("visit (elk-worker.js:42:9)", report)
        self.assertNotIn("PRIVATE-STDOUT", report)
        self.assertNotIn(PRIVATE_MESSAGE, str(raised.exception))
        self.assertNotIn("visit", str(raised.exception))
        public = json.dumps([public_progress(event) for event in progress])
        self.assertNotIn(str(path), public)
        self.assertNotIn(PRIVATE_MESSAGE, public)

    def test_worker_original_stack_failure_survives_unwritable_report_directory(self):
        blocker = self.root / "not-a-directory"
        blocker.write_text("occupied")
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(blocker)}), self.worker_process():
            with self.assertRaises(ElkWorkerFailure) as raised:
                _worker(request_graph(), [19])
        self.assertEqual(raised.exception.failure_code, "stack_limit")
        self.assertIsNone(raised.exception.diagnostic_path)
        self.assertIn("could not save", self.terminal.getvalue().lower())

    def test_successful_worker_creates_no_failure_report(self):
        with self.worker_process(returncode=0, stderr=""):
            self.assertEqual(_worker({}, [19]), [{}])
        self.assertEqual(list(self.root.rglob("*.json")), [])
        self.assertEqual(self.terminal.getvalue(), "")

    def test_all_attempt_failure_summary_preserves_report_location_without_stacks(self):
        with self.worker_process():
            with self.assertRaises(TraceError) as raised:
                optimize_graph(crossing_graph(), layout_attempts=2)
        paths = list(self.root.rglob("*.json"))
        self.assertEqual(len(paths), 2)
        message = str(raised.exception)
        self.assertIn("stack_limit", message)
        self.assertNotIn(PRIVATE_MESSAGE, message)
        self.assertNotIn("visit", message)
        self.assertIn("2 diagnostic reports", message)
        self.assertIn(str(paths[0].parent), message)
        for path in paths:
            self.assertIn(str(path), self.terminal.getvalue())

    def test_single_attempt_failure_summary_names_the_exact_report(self):
        with self.worker_process():
            with self.assertRaises(TraceError) as raised:
                optimize_graph(crossing_graph(), layout_attempts=1)
        paths = list(self.root.rglob("*.json"))
        self.assertEqual(len(paths), 1)
        self.assertIn(str(paths[0]), str(raised.exception))

    def test_closed_terminal_does_not_prevent_report_or_change_worker_failure(self):
        terminal = Mock()
        terminal.write.side_effect = OSError("closed terminal")
        with patch("sys.stderr", terminal), self.worker_process():
            with self.assertRaises(ElkWorkerFailure) as raised:
                _worker(request_graph(), [19])
        self.assertEqual(raised.exception.failure_code, "stack_limit")
        self.assertTrue(Path(raised.exception.diagnostic_path).is_file())

    def test_partial_success_keeps_diagnostics_out_of_saved_layout(self):
        failure_path = self.save()

        def worker(request, seeds, **kwargs):
            if seeds == [1]:
                raise ElkWorkerFailure("safe error", failure_code="stack_limit", returncode=1,
                                       diagnostic_path=failure_path)
            return synthetic_candidate(request, seeds)

        with patch("liquid_tracer.elk_layout._worker", side_effect=worker):
            graph = optimize_graph(crossing_graph(), layout_attempts=2)
        self.assertEqual(graph["layout"]["search"]["failed_count"], 1)
        self.assertNotIn(str(failure_path), json.dumps(graph))
        self.assertNotIn(STACK, json.dumps(graph))


if __name__ == "__main__":
    unittest.main()
