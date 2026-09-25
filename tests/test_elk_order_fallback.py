"""An unavailable optional port order retains validated ELK geometry visibly."""

import copy
import json
import os
import unittest
from unittest.mock import Mock, patch

from liquid_tracer.common import TraceError
from liquid_tracer.elk_layout import _apply_candidate, _request_graph, _worker, optimize_graph
from liquid_tracer.export import build_graph
from liquid_tracer.layout_preview import LAYOUT_NOTICE, layout_notice
from liquid_tracer.miro import make_plan, validate_plan
from liquid_tracer.progress import public_progress
from tests.test_elk_layout import ROOT, synthetic_candidate
from tests.test_input_order import input_order_state


REASON = "traced_first_order_not_preserved"
NOTICE = "Preferred connector ordering was unavailable; valid ELK geometry was retained."
PROGRESS = "Preferred connector ordering unavailable; retaining ELK geometry for validation"


def geometry_candidates(request, seeds, **kwargs):
    # Give the synthetic renderer dependency-ordered columns, just as ELK must.
    ordered = copy.deepcopy(request)
    ordered["children"].sort(key=lambda node: (
        int(node["layoutOptions"]["elk.partitioning.partition"]), node["id"]))
    candidates = synthetic_candidate(ordered, seeds)
    for candidate in candidates:
        candidate.update(inputOrderPolicy="geometry", inputOrderFallback=REASON,
                         branchBoundary=request.get("boundaryOrdering", False))
    return candidates


def fallback_candidates(request, seeds, **kwargs):
    candidates = []
    for fallback in geometry_candidates(request, seeds):
        rejected = copy.deepcopy(fallback)
        rejected.pop("inputOrderFallback")
        rejected.update(inputOrderPolicy="traced_first", inputOrderRejected=REASON)
        candidates.extend((fallback, rejected))
    return candidates


class ElkOrderFallbackTests(unittest.TestCase):
    def test_single_geometry_fallback_reaches_valid_miro_plan_with_notice(self):
        state = input_order_state()
        state["ancestor_runs"] = []
        graph = build_graph(state)
        original = copy.deepcopy(graph)
        with patch("liquid_tracer.elk_layout._worker", side_effect=fallback_candidates):
            result = optimize_graph(graph, layout_attempts=1)
        self.assertEqual(graph, original)
        self.assertEqual(result["layout"]["input_order"]["policy"], "geometry")
        self.assertEqual(result["layout"]["input_order"]["fallback_reason"], REASON)
        self.assertEqual(result["layout"]["search"]["successful_count"], 1)
        self.assertEqual(result["layout"]["search"]["failed_count"], 0)
        self.assertEqual(result["layout"]["search"]["candidate_count"], 1)
        self.assertIn(NOTICE, layout_notice(result))
        plan = make_plan(result)
        validate_plan(plan)
        self.assertEqual(plan["layout"]["input_order"], result["layout"]["input_order"])
        self.assertEqual({connector["key"] for connector in plan["connectors"]},
                         {edge["id"] for edge in graph["edges"]})

    def test_fallback_metadata_must_match_geometry_policy_and_exact_reason(self):
        graph = build_graph(input_order_state())
        request, ports, fees = _request_graph(graph)
        valid = geometry_candidates(request, [1])[0]
        mutations = [{"inputOrderFallback": reason}
                     for reason in (None, "", "PRIVATE", True, 1, [], {})]
        mutations += [{"inputOrderPolicy": policy} for policy in ("traced_first", None, [], {})]
        for mutation in mutations:
            candidate = {**valid, **mutation}
            with self.subTest(mutation=mutation), self.assertRaisesRegex(TraceError, "input ordering"):
                _apply_candidate(graph, candidate, ports, fees, "straight")
        candidate = copy.deepcopy(valid)
        candidate.pop("inputOrderPolicy")
        with self.assertRaisesRegex(TraceError, "input ordering fallback"):
            _apply_candidate(graph, candidate, ports, fees, "straight")

    def test_invalid_fallback_still_aborts_after_an_earlier_valid_candidate(self):
        graph = build_graph(input_order_state())

        def worker(request, seeds, **kwargs):
            candidate = geometry_candidates(request, seeds)[0]
            malformed = {**candidate, "inputOrderFallback": "PRIVATE"}
            return [candidate, malformed]

        with patch("liquid_tracer.elk_layout._worker", side_effect=worker), \
                self.assertRaisesRegex(TraceError, "input ordering fallback"):
            optimize_graph(graph, layout_attempts=1)

    def test_rejected_metadata_requires_explicit_policy_and_excludes_fallback(self):
        graph = build_graph(input_order_state())
        request, ports, fees = _request_graph(graph)
        rejected = fallback_candidates(request, [1])[1]
        mutations = [{"inputOrderRejected": reason}
                     for reason in (None, "", "PRIVATE", True, 1, [], {})]
        mutations += [{"inputOrderPolicy": policy} for policy in ("geometry", None, [], {})]
        mutations += [{"inputOrderFallback": reason} for reason in (REASON, None)]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaisesRegex(TraceError, "input ordering"):
                _apply_candidate(graph, {**rejected, **mutation}, ports, fees, "straight")
        rejected.pop("inputOrderPolicy")
        with self.assertRaisesRegex(TraceError, "rejected input ordering"):
            _apply_candidate(graph, rejected, ports, fees, "straight")

    def test_rejected_alternative_requires_one_same_seed_fallback(self):
        graph = build_graph(input_order_state())
        for case in ("standalone", "different_seed", "missing_marker", "duplicate_rejected"):
            def worker(request, seeds, **kwargs):
                fallback, rejected = fallback_candidates(request, seeds)
                if case == "standalone":
                    return [rejected]
                if case == "different_seed":
                    fallback["seed"] += 1
                elif case == "missing_marker":
                    fallback.pop("inputOrderFallback")
                elif case == "duplicate_rejected":
                    return [fallback, rejected, copy.deepcopy(rejected)]
                return [fallback, rejected]

            with self.subTest(case=case), patch("liquid_tracer.elk_layout._worker", side_effect=worker), \
                    self.assertRaisesRegex(TraceError, "unpaired rejected input ordering"):
                optimize_graph(graph, layout_attempts=1)

    def test_rejected_alternative_cannot_win_even_with_preferred_policy_tiebreak(self):
        graph = build_graph(input_order_state())
        from liquid_tracer.elk_layout import layout_metrics

        def metrics(result, **kwargs):
            self.assertNotEqual(result.get("layout", {}).get("input_order", {}).get("policy"), "traced_first")
            return layout_metrics(result, **kwargs)

        # Identical geometry would normally give traced_first the better score.
        with patch("liquid_tracer.elk_layout._worker", side_effect=fallback_candidates), \
                patch("liquid_tracer.elk_layout.layout_metrics", side_effect=metrics):
            result = optimize_graph(graph, layout_attempts=1)
        self.assertEqual(result["layout"]["input_order"]["policy"], "geometry")
        self.assertNotIn("rejected_reason", result["layout"]["input_order"])
        self.assertEqual(result["layout"]["search"]["candidate_count"], 1)
        self.assertEqual(result["layout"]["search"]["successful_count"], 1)

    def test_rejected_alternative_still_undergoes_full_geometry_validation(self):
        graph = build_graph(input_order_state())
        mutations = [lambda candidate: candidate["edges"].pop(),
                     lambda candidate: candidate["nodes"].pop(),
                     lambda candidate: candidate["nodes"][0].update(x=float("nan")),
                     lambda candidate: candidate["edges"][0].update(sections=[]),
                     lambda candidate: candidate["edges"][0].update(labels=[]),
                     lambda candidate: candidate["edges"][0]["sections"][0]["endPoint"].update(x=float("nan"))]
        for mutation in mutations:
            def worker(request, seeds, **kwargs):
                candidates = fallback_candidates(request, seeds)
                mutation(candidates[1])
                return candidates

            with self.subTest(mutation=mutation), \
                    patch("liquid_tracer.elk_layout._worker", side_effect=worker), self.assertRaises(TraceError):
                optimize_graph(graph, layout_attempts=1)

    def test_fallback_does_not_bypass_geometry_validation(self):
        graph = build_graph(input_order_state())
        mutations = [lambda candidate: candidate["edges"].pop(),
                     lambda candidate: candidate["edges"][0].update(sections=[]),
                     lambda candidate: candidate["nodes"][0].update(x=float("nan")),
                     lambda candidate: candidate["edges"][0].update(labels=[])]
        for mutation in mutations:
            def worker(request, seeds, **kwargs):
                candidates = geometry_candidates(request, seeds)
                mutation(candidates[0])
                return candidates

            with self.subTest(mutation=mutation), \
                    patch("liquid_tracer.elk_layout._worker", side_effect=worker), self.assertRaises(TraceError):
                optimize_graph(graph, layout_attempts=1)

    def test_normal_geometry_choice_has_no_fallback_notice(self):
        for policy in ("geometry", "traced_first"):
            graph = {"layout": {"input_order": {"policy": policy}}}
            self.assertEqual(layout_notice(graph), LAYOUT_NOTICE)
        for policy, reason in (("traced_first", REASON), ("geometry", "PRIVATE")):
            graph = {"layout": {"input_order": {"policy": policy, "fallback_reason": reason}}}
            self.assertEqual(layout_notice(graph), LAYOUT_NOTICE)

    def test_worker_reports_fallback_with_fixed_public_text(self):
        events = []
        candidate = {"inputOrderPolicy": "geometry", "inputOrderFallback": REASON,
                     "message": "PRIVATE", "stderr": "PRIVATE"}
        process = Mock(returncode=0)
        process.communicate.return_value = (
            json.dumps({"version": "0.12.0", "candidates": [candidate]}), "PRIVATE")
        process.poll.return_value = 0
        with patch.dict(os.environ, {"LIQUID_TRACER_ROOT": str(ROOT), "LIQUID_NODE_BIN": "/synthetic/node"}), \
                patch("pathlib.Path.is_file", return_value=True), \
                patch("liquid_tracer.elk_layout.subprocess.Popen", return_value=process):
            self.assertEqual(_worker({}, [1], progress=events.append), [candidate])
        fallback = [event for event in events if event.get("stage") == "input_order_fallback"]
        self.assertEqual(len(fallback), 1)
        value = public_progress({**fallback[0], "message": "PRIVATE", "stderr": "PRIVATE",
                                 "inputOrderFallback": "PRIVATE"})
        self.assertEqual(value["message"], PROGRESS)
        self.assertEqual(value["stage"], "input_order_fallback")
        self.assertNotIn("PRIVATE", json.dumps(value))
        self.assertNotIn("PRIVATE", json.dumps(events))


if __name__ == "__main__":
    unittest.main()
