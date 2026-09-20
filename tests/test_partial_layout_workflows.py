"""A failed later seed cannot discard a reviewed, validated earlier layout."""

import contextlib
import copy
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import compact_preview_run, layout_preview_run, main, saved_graph, sync_run
from liquid_tracer.common import read_json, save_json
from liquid_tracer.elk_errors import ElkWorkerFailure
from liquid_tracer.elk_layout import _worker
from liquid_tracer.investigations import read_case, update_case
from liquid_tracer.layout_reuse import reusable_elk_preview
from liquid_tracer.layout_search import layout_seeds
from liquid_tracer.web import public_layout_metrics
from tests.fixtures import A, fixture


class PartialLayoutWorkflows(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.case = Path(temporary.name) / "case"
        source = Path(temporary.name) / "fixture.json"
        save_json(source, fixture())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["trace", "--case", str(self.case), "--fixture", str(source),
                                   "--seed", A + ":0", "--hops", "1"]), 0)
        update_case(self.case, {"run_defaults": {**read_case(self.case).get("run_defaults", {}),
                                                "layout_attempts": 5}})
        self.archive = self.case / "runs" / read_case(self.case)["latest_run"]
        self.evidence = self.snapshot(self.archive)
        self.calls = []

    @staticmethod
    def snapshot(directory):
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}

    def worker_with_failed_third_seed(self, graph, seeds, progress=None):
        self.calls.extend(seeds)
        if seeds == [19]:
            raise ElkWorkerFailure("Synthetic engine failure", failure_code="unknown_exit", returncode=1)
        return _worker(graph, seeds, progress=progress)

    def preview(self, *, compact=False):
        action = compact_preview_run if compact else layout_preview_run
        with patch("liquid_tracer.elk_layout._worker", side_effect=self.worker_with_failed_third_seed):
            product = action(self.case)
        self.assertEqual(self.calls, list(layout_seeds(5)))
        self.assertEqual(self.snapshot(self.archive), self.evidence)
        graph = read_json(product["graph"])
        search = graph["layout"]["search"]
        self.assertEqual((search["attempted_count"], search["successful_count"], search["failed_count"]), (5, 4, 1))
        self.assertNotEqual(search["selected_seed"], 19)
        self.assertIn("4 of 5 layout attempts succeeded; 1 failed", Path(product["directory"], "graph.html").read_text())
        return product, graph

    def test_partial_preview_reused_for_sync_without_retracing_or_recalculating(self):
        product, graph = self.preview()
        before = self.snapshot(self.case)
        events = []
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=AssertionError("Must reuse reviewed layout")), \
             patch("liquid_tracer.cli.Esplora", side_effect=AssertionError("Must not retrace")):
            report = sync_run(self.case, "latest", "SYNTHETIC=", dry_run=True, progress=events.append)
        self.assertEqual(report["layout_metrics"], graph["layout"]["metrics"])
        self.assertEqual(public_layout_metrics(report["layout_metrics"])["failed_count"], 1)
        self.assertIn("reusing_layout", [event["phase"] for event in events])
        self.assertEqual(self.snapshot(self.case), before)

    def test_partial_compact_preview_keeps_counts_and_warning_through_apply(self):
        product, graph = self.preview(compact=True)
        self.assertEqual(graph["layout"]["metrics"]["failed_count"], 1)
        self.assertEqual(graph["layout"]["elk_metrics"]["failed_count"], 1)
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=AssertionError("Must reuse approved compact layout")):
            report = sync_run(self.case, "latest", "SYNTHETIC=", dry_run=True, reorganize=True,
                              compact_preview=product["preview_id"])
        self.assertEqual(public_layout_metrics(report["layout_metrics"])["successful_count"], 4)
        self.assertEqual(self.snapshot(self.archive), self.evidence)

    def test_partial_cache_rejects_inconsistent_outcomes_even_with_matching_report(self):
        product, graph = self.preview()
        directory = Path(product["directory"])
        report = read_json(directory / "layout-report.json")
        original = saved_graph(self.case)[2]
        self.assertEqual(reusable_elk_preview(original, self.case / "previews", layout_attempts=5), graph)
        mutations = {
            "missing outcomes": lambda s, m: s.pop("successful_count"),
            "unrun attempt": lambda s, m: s.update(attempted_count=4, successful_count=3),
            "zero successes": lambda s, m: s.update(successful_count=0, failed_count=5),
            "boolean count": lambda s, m: s.update(failed_count=True),
            "missing failure": lambda s, m: s.update(failed_attempts=[]),
            "wrong failure index": lambda s, m: s["failed_attempts"][0].update(attempt_index=2),
            "unknown failure code": lambda s, m: s["failed_attempts"][0].update(failure_code="private text"),
            "fatal failure code": lambda s, m: s["failed_attempts"][0].update(failure_code="elk_invalid_output"),
            "winner failed": lambda s, m: (s.update(selected_seed=19), m.update(selected_seed=19)),
            "winner absent": lambda s, m: (s.update(selected_seed=2), m.update(selected_seed=2)),
            "too many candidates": lambda s, m: (s.update(candidate_count=9), m.update(candidate_count=9)),
            "metrics disagree": lambda s, m: m.update(successful_count=5, failed_count=0),
            "boolean seed": lambda s, m: s["seeds"].__setitem__(0, True),
        }
        for name, mutate in mutations.items():
            altered = copy.deepcopy(graph)
            mutate(altered["layout"]["search"], altered["layout"]["metrics"])
            save_json(directory / "graph.json", altered)
            save_json(directory / "layout-report.json", {**report, "layout": altered["layout"]})
            with self.subTest(name=name):
                self.assertIsNone(reusable_elk_preview(original, self.case / "previews", layout_attempts=5))


if __name__ == "__main__":
    unittest.main()
