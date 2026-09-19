"""Layout search settings reach previews and sync without changing evidence."""

import contextlib
import copy
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import (compact_preview_run, layout_preview_run, layout_search_attempts,
                               main, parser, saved_graph, sync_run, verified_compaction_preview)
from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.connections import preview_connections
from liquid_tracer.elk_layout import optimize_graph
from liquid_tracer.investigations import create_investigation, read_case, update_case
from liquid_tracer.layout_reuse import reusable_elk_preview
from liquid_tracer.layout_search import DEFAULT_LAYOUT_ATTEMPTS, layout_seeds
from liquid_tracer.web import public_graph_options, public_layout_metrics
from tests.fixtures import A, fixture
from tests.test_connections import saved_case


class LayoutAttemptWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = self.root / "case"
        source = self.root / "fixture.json"
        save_json(source, fixture())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["trace", "--case", str(self.case), "--fixture", str(source),
                                   "--seed", A + ":0", "--hops", "1"]), 0)
        self.set_attempts(5)
        self.archive = self.case / "runs" / read_case(self.case)["latest_run"]
        self.evidence = self.snapshot(self.archive)

    @staticmethod
    def snapshot(directory):
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}

    def set_attempts(self, value):
        settings = read_case(self.case).get("run_defaults", {})
        update_case(self.case, {"run_defaults": {**settings, "layout_attempts": value}})

    def test_saved_setting_and_one_action_override_reach_saved_preview(self):
        first = layout_preview_run(self.case)
        graph = read_json(first["graph"])
        self.assertEqual(first["layout_attempts"], 5)
        self.assertEqual(graph["graph_options"]["layout_attempts"], 5)
        self.assertEqual(graph["layout"]["search"]["seeds"], list(layout_seeds(5)))
        self.assertEqual(graph["layout"]["metrics"]["attempt_count"], 5)
        second = layout_preview_run(self.case, layout_attempts=2)
        self.assertEqual(read_json(second["graph"])["layout"]["search"]["attempt_count"], 2)
        self.assertEqual(read_case(self.case)["run_defaults"]["layout_attempts"], 5)
        self.assertEqual(self.snapshot(self.archive), self.evidence)

    def test_sync_reuses_matching_search_and_recalculates_changed_count(self):
        product = layout_preview_run(self.case)
        expected = read_json(product["graph"])
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=AssertionError("Redundant ELK")), \
             patch("liquid_tracer.cli.sync", return_value={"dry_run": True}) as sync:
            report = sync_run(self.case, "latest", "SYNTHETIC=", dry_run=True)
        self.assertEqual(report["layout_attempts"], 5)
        self.assertEqual(sync.call_args.args[0]["layout"]["search"], expected["layout"]["search"])
        self.set_attempts(2)
        with patch("liquid_tracer.elk_layout.optimize_graph", wraps=optimize_graph) as worker, \
             patch("liquid_tracer.cli.sync", return_value={"dry_run": True}):
            changed = sync_run(self.case, "latest", "SYNTHETIC=", dry_run=True)
        worker.assert_called_once()
        self.assertEqual(worker.call_args.kwargs["layout_attempts"], 2)
        self.assertEqual(changed["layout_attempts"], 2)
        self.assertEqual(self.snapshot(self.archive), self.evidence)

    def test_cache_rejects_old_or_different_search_metadata(self):
        product = layout_preview_run(self.case)
        path = Path(product["directory"])
        original = saved_graph(self.case)[2]
        saved = read_json(path / "graph.json")
        report = read_json(path / "layout-report.json")
        self.assertNotIn("layout_attempts", original["graph_options"])
        self.assertEqual(reusable_elk_preview(original, self.case / "previews", layout_attempts=5), saved)
        self.assertIsNone(reusable_elk_preview(original, self.case / "previews", layout_attempts=2))
        for change in ("missing", "version", "count", "seeds", "option"):
            altered = copy.deepcopy(saved)
            if change == "missing":
                altered["layout"].pop("search")
                altered["graph_options"].pop("layout_attempts")
            elif change == "version":
                altered["layout"]["search"]["version"] += 1
            elif change == "count":
                altered["layout"]["search"]["attempt_count"] = 2
            elif change == "seeds":
                altered["layout"]["search"]["seeds"][-1] += 1
            else:
                altered["graph_options"]["layout_attempts"] = 2
            save_json(path / "graph.json", altered)
            save_json(path / "layout-report.json", {**report, "layout": altered["layout"]})
            with self.subTest(change=change):
                self.assertIsNone(reusable_elk_preview(original, self.case / "previews", layout_attempts=5))

    def test_compact_review_preserves_frozen_attempts_and_rejects_override(self):
        product = compact_preview_run(self.case, layout_attempts=2)
        identity = product["preview_id"]
        _, meta = verified_compaction_preview(self.case, "latest", identity)
        self.assertEqual(meta["graph_options"]["layout_attempts"], 2)
        self.set_attempts(7)
        with patch("liquid_tracer.cli.sync", return_value={"dry_run": True}) as sync:
            result = sync_run(self.case, "latest", "SYNTHETIC=", dry_run=True,
                              reorganize=True, compact_preview=identity)
        self.assertEqual(result["layout_attempts"], 2)
        self.assertEqual(sync.call_args.args[0]["graph_options"]["layout_attempts"], 2)
        with self.assertRaisesRegex(TraceError, "layout-attempts overrides"):
            sync_run(self.case, "latest", "SYNTHETIC=", dry_run=True, reorganize=True,
                     compact_preview=identity, layout_attempts=5)
        with self.assertRaisesRegex(TraceError, "layout-attempts overrides"):
            sync_run(self.case, "latest", "SYNTHETIC=", dry_run=True,
                     plan_path=self.archive / "miro-plan.json", layout_attempts=5)
        self.assertEqual(self.snapshot(self.archive), self.evidence)

    def test_cli_and_resolver_validate_without_issuing_requests(self):
        self.assertEqual(layout_search_attempts({}), DEFAULT_LAYOUT_ATTEMPTS)
        self.assertEqual(layout_search_attempts({"run_defaults": {"layout_attempts": 9}}, 3), 3)
        for command in ("layout-preview", "compact-preview", "miro-sync"):
            base = [command, "--case", str(self.case)]
            self.assertIsNone(parser().parse_args(base).layout_attempts)
            self.assertEqual(parser().parse_args(base + ["--layout-attempts", "13"]).layout_attempts, 13)
        for value in (0, 1001, True, 1.5, "5"):
            with self.subTest(value=value), self.assertRaises(TraceError):
                layout_search_attempts({}, value)
        with patch("liquid_tracer.cli.ensure_graph_counts", side_effect=AssertionError("No requests")), \
             contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["layout-preview", "--case", str(self.case), "--layout-attempts", "0"]), 1)

    def test_connection_preview_uses_investigation_attempts(self):
        case = create_investigation(self.root / "connections", "Seed search", run_defaults={"layout_attempts": 2})
        _, archive = saved_case(case)
        evidence = self.snapshot(archive)
        with patch("liquid_tracer.address_counts.ensure_counts", return_value=None), \
             patch("liquid_tracer.elk_layout.optimize_graph", wraps=optimize_graph) as worker:
            result = preview_connections(case)
        worker.assert_called_once()
        self.assertEqual(worker.call_args.kwargs["layout_attempts"], 2)
        self.assertEqual(result["layout_attempts"], 2)
        self.assertEqual(read_json(result["graph"])["layout"]["search"]["attempt_count"], 2)
        self.assertEqual(self.snapshot(archive), evidence)

    def test_browser_reports_only_recorded_valid_attempt_counts(self):
        self.assertNotIn("layout_attempts", public_graph_options({}))
        self.assertEqual(public_graph_options({"layout_attempts": 5})["layout_attempts"], 5)
        for invalid in (None, True, "private", 1001):
            self.assertIsNone(public_graph_options({"layout_attempts": invalid}))
        metrics = {phase: {"crossings": 0, "node_overlaps": 0, "node_intersections": 0}
                   for phase in ("before", "after")}
        self.assertNotIn("attempt_count", public_layout_metrics(metrics))
        self.assertEqual(public_layout_metrics({**metrics, "attempt_count": 5})["attempt_count"], 5)
        self.assertNotIn("attempt_count", public_layout_metrics({**metrics, "attempt_count": "private"}))


if __name__ == "__main__":
    unittest.main()
