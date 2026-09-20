"""A failed seed cannot erase valid layouts or disguise invalid input."""

import copy
import json
import os
import unittest
from unittest.mock import Mock, patch

from liquid_tracer.common import TraceError
from liquid_tracer.elk_errors import ElkWorkerFailure
from liquid_tracer.elk_layout import _worker, optimize_graph
from liquid_tracer.layout_search import layout_seeds
from tests.test_elk_layout import ROOT, crossing_graph, synthetic_candidate
from tests.test_layout_search import disconnected_graph


class FailedSeedSearchTests(unittest.TestCase):
    def test_failed_third_seed_retains_earlier_best_and_attempts_later_seeds(self):
        graph = disconnected_graph(2)
        original = copy.deepcopy(graph)
        events, observed = [], []

        def worker(request, seeds, **kwargs):
            observed.extend(seeds)
            if seeds == [19]:
                raise ElkWorkerFailure("private worker detail", failure_code="elk_engine_error", returncode=1)
            candidates = synthetic_candidate(request, seeds)
            if seeds != [1]:
                for node in candidates[0]["nodes"]:
                    node.update(x=0, y=0)
            return candidates

        with patch("liquid_tracer.elk_layout._worker", side_effect=worker):
            result = optimize_graph(graph, layout_attempts=5, progress=events.append)
        self.assertEqual(observed, list(layout_seeds(5)))
        self.assertEqual(graph, original)
        self.assertEqual(result["nodes"], optimize_with_synthetic(graph, 1)["nodes"])
        search = result["layout"]["search"]
        self.assertEqual(search["selected_seed"], 1)
        self.assertEqual(search["candidate_count"], 4)
        self.assertEqual(search["seeds"], list(layout_seeds(5)))
        self.assertEqual(search["failed_attempts"], [
            {"attempt_index": 3, "seed": 19, "failure_code": "elk_engine_error"}])
        for counts in (search, result["layout"]["metrics"]):
            self.assertEqual(counts["attempt_count"], 5)
            self.assertEqual(counts["attempted_count"], 5)
            self.assertEqual(counts["successful_count"], 4)
            self.assertEqual(counts["failed_count"], 1)
        failure = next(event for event in events if event["stage"] == "attempt_failed")
        self.assertEqual((failure["attempt_index"], failure["attempted_count"],
                          failure["successful_count"], failure["failed_count"]), (3, 3, 2, 1))
        self.assertEqual(events[-1]["stage"], "ready_with_failures")
        self.assertEqual(events[-1]["completed"], 1)
        self.assertEqual(events[-1]["successful_count"], 4)
        self.assertNotIn("private worker detail", json.dumps(result) + json.dumps(events))

    def test_first_and_last_seed_failures_still_return_verified_middle_candidate(self):
        for failed_seeds in ({1}, {19}, {1, 19}):
            def worker(request, seeds, **kwargs):
                if seeds[0] in failed_seeds:
                    raise ElkWorkerFailure("worker failure", failure_code="stack_limit")
                return synthetic_candidate(request, seeds)

            with self.subTest(failed_seeds=failed_seeds), \
                    patch("liquid_tracer.elk_layout._worker", side_effect=worker) as mocked:
                result = optimize_graph(crossing_graph(), layout_attempts=3)
            self.assertEqual(mocked.call_count, 3)
            search = result["layout"]["search"]
            self.assertEqual(search["successful_count"], 3 - len(failed_seeds))
            self.assertEqual(search["failed_count"], len(failed_seeds))
            self.assertNotIn(search["selected_seed"], failed_seeds)
            self.assertEqual(len(result["nodes"]), 4)
            self.assertEqual(len(result["edges"]), 2)

    def test_all_failed_seeds_raise_without_a_result_or_mutating_input(self):
        graph = crossing_graph()
        original = copy.deepcopy(graph)
        events = []
        with patch("liquid_tracer.elk_layout._worker", side_effect=ElkWorkerFailure(
                "private worker detail", failure_code="heap_exhausted")) as worker:
            with self.assertRaisesRegex(TraceError, "All 4 ELK layout attempts failed") as caught:
                optimize_graph(graph, layout_attempts=4, progress=events.append)
        self.assertEqual(worker.call_count, 4)
        self.assertEqual(graph, original)
        self.assertIn("heap_exhausted", str(caught.exception))
        self.assertIn("Memory exhaustion was reported", str(caught.exception))
        self.assertIn("Close other applications", str(caught.exception))
        self.assertIn("No Miro changes", str(caught.exception))
        self.assertNotIn("private worker detail", str(caught.exception) + json.dumps(events))
        self.assertFalse(any(event["stage"] in ("ready", "ready_with_failures") for event in events))

    def test_setup_validation_and_cancellation_abort_even_with_a_completed_candidate(self):
        graph = crossing_graph()
        original = copy.deepcopy(graph)
        for error in (TraceError("malformed or invalid geometry"), KeyboardInterrupt(), SystemExit()):
            def worker(request, seeds, **kwargs):
                if seeds == [7]:
                    raise error
                return synthetic_candidate(request, seeds)

            with self.subTest(error=type(error).__name__), \
                    patch("liquid_tracer.elk_layout._worker", side_effect=worker) as mocked, \
                    self.assertRaises(type(error)):
                optimize_graph(graph, layout_attempts=5)
            self.assertEqual(mocked.call_count, 2)
            self.assertEqual(graph, original)

    def test_all_killed_workers_do_not_claim_confirmed_memory_exhaustion(self):
        with patch("liquid_tracer.elk_layout._worker", side_effect=ElkWorkerFailure(
                "private worker detail", failure_code="worker_killed", returncode=-9)):
            with self.assertRaises(TraceError) as caught:
                optimize_graph(crossing_graph(), layout_attempts=2)
        self.assertIn("worker was killed; memory exhaustion is possible but unconfirmed", str(caught.exception))
        self.assertNotIn("Memory exhaustion was reported", str(caught.exception))
        self.assertNotIn("private worker detail", str(caught.exception))

    def test_invalid_geometry_is_not_skipped_after_a_valid_candidate(self):
        graph = crossing_graph()

        def worker(request, seeds, **kwargs):
            candidates = synthetic_candidate(request, seeds)
            if seeds == [7]:
                candidates[0]["nodes"].pop()
            return candidates

        with patch("liquid_tracer.elk_layout._worker", side_effect=worker) as mocked:
            with self.assertRaisesRegex(TraceError, "objects"):
                optimize_graph(graph, layout_attempts=5)
        self.assertEqual(mocked.call_count, 2)

    def test_failure_codes_cannot_carry_unrecognized_worker_text(self):
        for code in ("PRIVATE_SECRET", {}, None):
            failure = ElkWorkerFailure("worker error", failure_code=code)
            self.assertEqual(failure.failure_code, "unknown_exit")

    def test_successful_search_reports_zero_failed_attempts(self):
        result = optimize_with_synthetic(crossing_graph(), 3)
        search = result["layout"]["search"]
        self.assertEqual(search["failed_attempts"], [])
        self.assertEqual(search["failed_count"], 0)
        self.assertEqual(search["attempted_count"], search["successful_count"])


def optimize_with_synthetic(graph, attempts):
    with patch("liquid_tracer.elk_layout._worker", side_effect=synthetic_candidate):
        return optimize_graph(graph, layout_attempts=attempts)


class FailedWorkerClassificationTests(unittest.TestCase):
    def worker_with_response(self, returncode, stdout, stderr):
        process = Mock(returncode=returncode)
        process.poll.return_value = returncode
        process.communicate.return_value = (stdout, stderr)
        with patch.dict(os.environ, {"LIQUID_TRACER_ROOT": str(ROOT), "LIQUID_NODE_BIN": "/synthetic/node",
                                     "LIQUID_RENDER_HEAP_MB": "4096"}), \
                patch("pathlib.Path.is_file", return_value=True), \
                patch("liquid_tracer.elk_layout.subprocess.Popen", return_value=process):
            return _worker({"children": [{}, {}], "edges": [{}]}, [1])

    def test_nonzero_process_exit_is_typed_and_does_not_echo_stderr(self):
        for code, stderr, failure_code in (
            (1, "private graph content", "unknown_exit"),
            (-6, "JavaScript heap out of memory; private graph", "heap_exhausted"),
            (1, "Maximum call stack size exceeded; private graph", "stack_limit"),
            (-9, "private graph", "worker_killed"),
        ):
            with self.subTest(code=code, failure_code=failure_code), self.assertRaises(ElkWorkerFailure) as caught:
                self.worker_with_response(code, "", stderr)
            self.assertEqual(caught.exception.failure_code, failure_code)
            self.assertEqual(caught.exception.returncode, code)
            self.assertNotIn("private graph", str(caught.exception))

    def test_invalid_or_malformed_success_response_is_fatal(self):
        for response in ("not json", json.dumps({"version": "0.12.0", "candidates": []}),
                         json.dumps({"version": "wrong", "candidates": [{}]})):
            with self.subTest(response=response), self.assertRaises(TraceError) as caught:
                self.worker_with_response(0, response, "")
            self.assertNotIsInstance(caught.exception, ElkWorkerFailure)

    def test_startup_failure_is_not_a_recoverable_seed_failure(self):
        with patch.dict(os.environ, {"LIQUID_TRACER_ROOT": str(ROOT), "LIQUID_NODE_BIN": "/synthetic/node"}), \
                patch("pathlib.Path.is_file", return_value=True), \
                patch("liquid_tracer.elk_layout.subprocess.Popen", side_effect=OSError("private path")):
            with self.assertRaises(TraceError) as caught:
                _worker({}, [1])
        self.assertNotIsInstance(caught.exception, ElkWorkerFailure)
        self.assertNotIn("private path", str(caught.exception))

    def test_structured_setup_and_invalid_output_diagnostics_are_fatal(self):
        for code, stage in (("elk_worker_setup", "load_engine"), ("elk_invalid_output", "serialize")):
            stderr = "LIQUID_ELK_FAILURE " + json.dumps({"version": 1, "code": code, "stage": stage})
            with self.subTest(code=code), self.assertRaises(TraceError) as caught:
                self.worker_with_response(1, "", stderr)
            self.assertNotIsInstance(caught.exception, ElkWorkerFailure)

    def test_reported_input_or_setup_error_stays_fatal(self):
        for code in ("elk_worker_setup", "elk_invalid_request", "elk_invalid_output", "elk_input_order", "elk_unsupported_graph",
                     "elk_unsupported_configuration", "graph_parse"):
            with self.subTest(code=code), \
                    patch("liquid_tracer.elk_layout.renderer_failure_code", return_value=code), \
                    self.assertRaises(TraceError) as caught:
                self.worker_with_response(1, "", "private details")
            self.assertNotIsInstance(caught.exception, ElkWorkerFailure)


if __name__ == "__main__":
    unittest.main()
