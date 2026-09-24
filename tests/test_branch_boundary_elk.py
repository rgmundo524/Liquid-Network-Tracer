"""Branch alternatives preserve the seed budget and the ELK worker contract."""

import copy
import json
import os
import unittest
from unittest.mock import Mock, patch

from liquid_tracer.common import TraceError
from liquid_tracer.elk_layout import _apply_candidate, _request_graph, _worker, optimize_graph
from liquid_tracer.elk_parallel import _request
from liquid_tracer.export import build_graph
from liquid_tracer.horizontal_spacing import compact_candidate
from tests.test_elk_layout import HAS_ELK, ROOT, crossing_graph, synthetic_candidate
from tests.test_input_order import input_order_state


class BranchBoundaryAdapterTests(unittest.TestCase):
    def test_candidate_schedule_retains_baseline_and_stable_prefix(self):
        request, _, _ = _request_graph(crossing_graph())
        request["branchNodeOrder"] = [node["id"] for node in request["children"]]
        original = copy.deepcopy(request)
        choices = [_request(request, index) for index in range(1, 8)]
        self.assertEqual([choice["boundaryOrdering"] for choice in choices],
                         [True, False, True, False, True, False, True])
        self.assertEqual([choice["branchProfile"] for choice in choices],
                         ["flow_weighted", "balanced", *["flow_weighted"] * 5])
        self.assertEqual(choices[:3], [_request(request, index) for index in range(1, 4)])
        self.assertEqual(request, original)
        request.pop("branchNodeOrder")
        self.assertFalse(any(_request(request, index)["boundaryOrdering"] for index in range(1, 8)))

    def test_boundary_metadata_filters_fee_ids_without_changing_partitions(self):
        graph = crossing_graph()
        graph["fee_items"] = {"c": {"endpoint": "shapes"}}
        with patch("liquid_tracer.elk_layout.branch_order", return_value=["d", "b", "c", "a"]):
            request, _, _ = _request_graph(graph)
        self.assertEqual(request["branchNodeOrder"], ["b", "a", "d"])
        self.assertEqual([child["id"] for child in request["children"]], ["a", "b", "d"])
        self.assertEqual([child["layoutOptions"]["elk.partitioning.partition"]
                          for child in request["children"]], ["1", "1", "2"])

    def test_worker_rejects_wrong_boundary_metadata(self):
        request, _, _ = _request_graph(crossing_graph())
        request.update(branchNodeOrder=[node["id"] for node in request["children"]], boundaryOrdering=True)
        for value in (None, False, 1, "true"):
            candidate = synthetic_candidate(request, [1])[0]
            candidate["branchBoundary"] = value
            process = Mock(returncode=0)
            process.communicate.return_value = (json.dumps({"version": "0.12.0", "candidates": [candidate]}), "")
            process.poll.return_value = 0
            with self.subTest(value=value), \
                    patch.dict(os.environ, {"LIQUID_TRACER_ROOT": str(ROOT), "LIQUID_NODE_BIN": "/synthetic/node"}), \
                    patch("pathlib.Path.is_file", return_value=True), \
                    patch("liquid_tracer.elk_layout.subprocess.Popen", return_value=process), \
                    self.assertRaisesRegex(TraceError, "invalid layout"):
                _worker(request, [1])

    def test_geometry_compaction_retains_boundary_metadata(self):
        graph = crossing_graph()
        request, ports, fees = _request_graph(graph)
        candidate = synthetic_candidate(request, [1])[0]
        candidate["branchBoundary"] = True
        result = _apply_candidate(graph, compact_candidate(candidate), ports, fees, "straight")
        self.assertIs(result["layout"]["branch_organization"]["boundary_ordering"], True)
        for value in (None, 1, "true"):
            candidate["branchBoundary"] = value
            with self.subTest(value=value), self.assertRaisesRegex(TraceError, "boundary ordering"):
                _apply_candidate(graph, candidate, ports, fees, "straight")

    def test_final_context_compaction_rejects_worse_branch_quality(self):
        # Isolate the branch gate with nonintersecting objects. A future
        # compactor may move a tracked input even while collision counts tie.
        graph = crossing_graph()
        graph["edges"] = []
        before = []

        def compact(result):
            before.append(result["nodes"][0]["y"])
            result["nodes"][0]["y"] += 100
            result["layout"]["branch_organization"]["context_inputs_moved"] = 1

        def boundaries(result):
            return {"enabled": True, "interleavings": 0,
                    "boundary_depth": result["nodes"][0]["y"],
                    "interbranch_travel": 0.0, "truncated": False}

        with patch("liquid_tracer.elk_layout._worker", side_effect=synthetic_candidate), \
                patch("liquid_tracer.elk_layout.compact_context_inputs", side_effect=compact), \
                patch("liquid_tracer.elk_layout.boundary_metrics", side_effect=boundaries):
            result = optimize_graph(graph, layout_attempts=1)
        self.assertEqual(result["nodes"][0]["y"], before[0])
        self.assertEqual(result["layout"]["branch_organization"]["context_compaction_rejected"],
                         "branch_boundary_quality")


@unittest.skipUnless(HAS_ELK, "local ELK package is not installed")
class BranchBoundaryWorkerTests(unittest.TestCase):
    def test_real_worker_keeps_policy_on_every_input_order_alternative(self):
        graph = build_graph(input_order_state(4, continuing=(3,)))
        request, _, _ = _request_graph(graph)
        request["branchNodeOrder"] = [node["id"] for node in request["children"]]
        original = copy.deepcopy(request)
        for enabled in (False, True):
            candidates = _worker({**request, "boundaryOrdering": enabled}, [1])
            self.assertIn(len(candidates), (1, 2))
            self.assertTrue(all(candidate["branchBoundary"] is enabled for candidate in candidates))
            self.assertIn([candidate["inputOrderPolicy"] for candidate in candidates],
                          (["traced_first"], ["geometry", "traced_first"]))
        self.assertEqual(request, original)

    def test_real_worker_rejects_invalid_boundary_order_before_layout(self):
        request, _, _ = _request_graph(crossing_graph())
        request.pop("branchNodeOrder")
        order = [node["id"] for node in request["children"]]
        mutations = [{"boundaryOrdering": True},
                     {"branchNodeOrder": order, "boundaryOrdering": None},
                     {"branchNodeOrder": order, "boundaryOrdering": "true"},
                     {"branchNodeOrder": order[:-1], "boundaryOrdering": True},
                     {"branchNodeOrder": [*order[:-1], order[0]], "boundaryOrdering": True},
                     {"branchNodeOrder": [*order[:-1], "missing"], "boundaryOrdering": True}]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaisesRegex(TraceError, "rejected its generated layout request"):
                _worker({**request, **mutation}, [1])


if __name__ == "__main__":
    unittest.main()
