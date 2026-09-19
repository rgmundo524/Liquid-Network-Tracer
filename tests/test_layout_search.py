"""Sequential candidate search keeps large graphs intact and memory bounded."""

import copy
import json
import os
import subprocess
import unittest
import weakref
from unittest.mock import Mock, patch

from liquid_tracer.common import TraceError
from liquid_tracer.elk_layout import _request_graph, _worker, optimize_graph
from liquid_tracer.layout_search import (DEFAULT_LAYOUT_ATTEMPTS, LAYOUT_SEARCH_VERSION,
                                         layout_seeds, normalize_layout_attempts)
from tests.test_elk_layout import HAS_ELK, ROOT, crossing_graph, synthetic_candidate


def disconnected_graph(count):
    return {"nodes": [{"id": f"node:{index}", "kind": "address", "column": 0,
                       "x": index * 500, "y": 0, "width": 80, "height": 80}
                      for index in range(count)], "edges": []}


class LayoutSearchTests(unittest.TestCase):
    def test_seed_prefix_is_stable_distinct_and_elk_integer_safe(self):
        seeds = layout_seeds(1000)
        self.assertEqual(seeds[:3], (1, 7, 19))
        self.assertEqual(len(seeds), len(set(seeds)))
        self.assertTrue(all(0 < seed <= 2147483647 for seed in seeds))
        for count in (1, 2, 3, 25, 100):
            self.assertEqual(layout_seeds(count), seeds[:count])
        self.assertEqual(len(layout_seeds()), DEFAULT_LAYOUT_ATTEMPTS)

    def test_attempt_count_requires_an_integer_in_range(self):
        for value in (True, False, 0, -1, 1.5, 25.0, "25", [], {}, 1001):
            with self.subTest(value=value), self.assertRaises(TraceError):
                normalize_layout_attempts(value)
        self.assertEqual(normalize_layout_attempts(), 25)
        self.assertEqual(normalize_layout_attempts(1000), 1000)

    def test_large_graph_uses_all_default_seeds_one_worker_call_at_a_time(self):
        graph = disconnected_graph(301)
        before = copy.deepcopy(graph)
        observed = []

        def worker(request, seeds, **kwargs):
            observed.append((list(seeds), request["branchProfile"]))
            self.assertEqual(len(seeds), 1)
            return synthetic_candidate(request, seeds)

        with patch("liquid_tracer.elk_layout._worker", side_effect=worker):
            result = optimize_graph(graph)
        self.assertEqual([value[0] for value in observed], [[seed] for seed in layout_seeds()])
        self.assertTrue(all(profile == "flow_weighted" for _, profile in observed))
        self.assertEqual(result["layout"]["search"]["seeds"], list(layout_seeds()))
        self.assertEqual(result["layout"]["search"]["version"], LAYOUT_SEARCH_VERSION)
        self.assertEqual(result["layout"]["metrics"]["attempt_count"], 25)
        self.assertEqual(result["layout"]["metrics"]["candidate_count"], 25)
        self.assertEqual(result["graph_options"]["layout_attempts"], 25)
        self.assertEqual(len(result["nodes"]), 301)
        self.assertEqual(graph, before)

    def test_small_graph_profile_prefix_does_not_depend_on_requested_count(self):
        graph = disconnected_graph(2)
        observed = []

        def worker(request, seeds, **kwargs):
            observed.append((seeds[0], request["branchProfile"]))
            return synthetic_candidate(request, seeds)

        with patch("liquid_tracer.elk_layout._worker", side_effect=worker):
            optimize_graph(graph, layout_attempts=1)
            optimize_graph(graph, layout_attempts=4)
        self.assertEqual(observed, [(1, "balanced"), (1, "balanced"), (7, "flow_weighted"),
                                    (19, "flow_weighted"), (layout_seeds(4)[3], "flow_weighted")])

    def test_thousand_object_graph_keeps_all_objects_across_requested_attempts(self):
        graph = disconnected_graph(1001)
        with patch("liquid_tracer.elk_layout._worker", side_effect=synthetic_candidate) as worker:
            result = optimize_graph(graph, layout_attempts=4)
        self.assertEqual(worker.call_count, 4)
        self.assertTrue(all(call.args[0]["branchProfile"] == "flow_weighted" for call in worker.call_args_list))
        self.assertEqual([node["id"] for node in result["nodes"]], [node["id"] for node in graph["nodes"]])
        self.assertEqual(result["layout"]["search"]["seeds"], list(layout_seeds(4)))

    def test_candidate_geometry_is_released_before_next_seed(self):
        class Candidate(dict):
            pass

        previous = []

        def worker(request, seeds, **kwargs):
            self.assertTrue(all(reference() is None for reference in previous))
            candidate = Candidate(synthetic_candidate(request, seeds)[0])
            previous.append(weakref.ref(candidate))
            return [candidate]

        with patch("liquid_tracer.elk_layout._worker", side_effect=worker):
            optimize_graph(disconnected_graph(2), layout_attempts=5)
        self.assertTrue(all(reference() is None for reference in previous))

    def test_graph_setting_and_explicit_override_are_respected(self):
        graph = disconnected_graph(2)
        graph["graph_options"] = {"layout_attempts": 4}
        with patch("liquid_tracer.elk_layout._worker", side_effect=synthetic_candidate) as worker:
            result = optimize_graph(graph)
            self.assertEqual(worker.call_count, 4)
            worker.reset_mock()
            override = optimize_graph(graph, layout_attempts=2)
            self.assertEqual(worker.call_count, 2)
        self.assertEqual(result["layout"]["search"]["attempt_count"], 4)
        self.assertEqual(override["graph_options"]["layout_attempts"], 2)
        self.assertEqual(graph["graph_options"]["layout_attempts"], 4)

    def test_best_earlier_candidate_survives_worse_later_candidates(self):
        def worker(request, seeds, **kwargs):
            candidates = synthetic_candidate(request, seeds)
            if seeds != [1]:
                for node in candidates[0]["nodes"]:
                    node.update(x=0, y=0)
            return candidates

        with patch("liquid_tracer.elk_layout._worker", side_effect=worker):
            result = optimize_graph(disconnected_graph(2), layout_attempts=5)
        self.assertEqual(result["layout"]["metrics"]["selected_seed"], 1)
        self.assertEqual(result["layout"]["metrics"]["after"]["node_overlaps"], 0)

    def test_both_input_order_candidates_are_scored_and_counted(self):
        def worker(request, seeds, **kwargs):
            candidates = synthetic_candidate(request, seeds)
            geometry = copy.deepcopy(candidates[0])
            geometry["inputOrderPolicy"] = "geometry"
            return [geometry, candidates[0]]

        with patch("liquid_tracer.elk_layout._worker", side_effect=worker):
            result = optimize_graph(disconnected_graph(2), layout_attempts=4)
        self.assertEqual(result["layout"]["metrics"]["candidate_count"], 8)
        self.assertEqual(result["layout"]["input_order"]["policy"], "traced_first")

    def test_failed_or_cancelled_later_attempt_keeps_input_graph_unchanged(self):
        graph = crossing_graph()
        before = copy.deepcopy(graph)
        for error in (TraceError("ELK failed"), KeyboardInterrupt()):
            def worker(request, seeds, **kwargs):
                if seeds == [7]:
                    raise error
                return synthetic_candidate(request, seeds)

            with self.subTest(error=type(error).__name__), \
                    patch("liquid_tracer.elk_layout._worker", side_effect=worker), \
                    self.assertRaises(type(error)):
                optimize_graph(graph, layout_attempts=3)
            self.assertEqual(graph, before)

    def test_progress_metadata_includes_worker_heartbeat_and_every_stage(self):
        graph = crossing_graph()
        request, _, _ = _request_graph(graph)
        candidates = {seed: synthetic_candidate(request, [seed]) for seed in layout_seeds(2)}
        processes = []
        for seed in layout_seeds(2):
            process = Mock(returncode=0)
            process.communicate.side_effect = [subprocess.TimeoutExpired("node", 5),
                (json.dumps({"version": "0.12.0", "candidates": candidates[seed]}), "")]
            process.poll.return_value = 0
            processes.append(process)
        events = []
        with patch.dict(os.environ, {"LIQUID_TRACER_ROOT": str(ROOT), "LIQUID_NODE_BIN": "/synthetic/node"}), \
                patch("pathlib.Path.is_file", return_value=True), \
                patch("liquid_tracer.elk_layout.subprocess.Popen", side_effect=processes):
            optimize_graph(graph, progress=events.append, layout_attempts=2)
        self.assertTrue(all(event["attempt_total"] == 2 for event in events))
        self.assertTrue(all(event["seed"] == layout_seeds(2)[event["attempt_index"] - 1] for event in events))
        heartbeat = [event for event in events if "elapsed_seconds" in event]
        self.assertEqual([event["attempt_index"] for event in heartbeat], [1, 2])
        self.assertEqual(events[-1]["stage"], "ready")


@unittest.skipUnless(HAS_ELK, "local ELK package is not installed")
class LayoutSearchWorkerTests(unittest.TestCase):
    def test_explicit_profile_overrides_single_seed_compatibility_default(self):
        request, _, _ = _request_graph(crossing_graph())
        original = copy.deepcopy(request)
        for profile in ("balanced", "flow_weighted"):
            candidates = _worker({**request, "branchProfile": profile}, [1])
            self.assertTrue(all(candidate["branchProfile"] == profile for candidate in candidates))
        self.assertEqual(request, original)

    def test_real_search_is_reproducible(self):
        graph = crossing_graph()
        first = optimize_graph(graph, layout_attempts=4)
        second = optimize_graph(graph, layout_attempts=4)
        self.assertEqual(first, second)
        self.assertIn(first["layout"]["metrics"]["candidate_count"], range(4, 9))
