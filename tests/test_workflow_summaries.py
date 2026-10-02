"""Bounded plot metadata for investigation navigation; selected plots stay verified."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError, save_json
from liquid_tracer.investigations import create_investigation, read_case
from liquid_tracer.plots import preview_plot, reviewed_plot
from liquid_tracer.workflow_api import case_boards, case_workflow, plot_artifact, selected_plot
from tests.test_attribution_convergence import graph_state, tx
from tests.test_connections import saved_case
from tests.test_pegout_paths import add_pegout


class WorkflowSummaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = create_investigation(self.root, "Saved plots", seeds=[tx("a") + ":0"])
        self.identity = read_case(self.case)["case_id"]

    def report(self, number=1, **changes):
        preview_id = "a" * 16 + "-plots-" + f"{number:08x}"
        directory = self.case / "previews" / preview_id
        directory.mkdir(parents=True)
        report = {"schema_version": 1, "case_id": self.identity, "run_id": "a" * 16,
                  "goal": "full", "query": {}, "created_at": f"2026-10-02T00:00:{number % 60:02d}Z",
                  "node_count": 3, "edge_count": 2, "transaction_count": 1,
                  "status": "plotted", "saved_data_only": True,
                  "archive_sha256": "b" * 64, "private_path": str(self.case)}
        report.update(changes)
        save_json(directory / "plot.json", report)
        (directory / "graph.html").write_text("test placeholder")
        (directory / "SHA256SUMS").write_text("completion marker, not verified")
        return directory

    def real_plot(self, goal="pegouts"):
        state = graph_state((("a:0", "b"),), seeds=("a:0", "b:0"))
        add_pegout(state, tx("b"))
        saved_case(self.case, state)
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph):
            return preview_plot(self.case, goal)

    def test_lightweight_reads_only_small_reports_and_never_maps_or_evidence(self):
        directory = self.report(reviewable=True, validation_pending=False, empty=True)
        (directory / "graph.json").write_text("not a graph")
        (directory / "miro-plan.json").write_text("not a plan")
        opened = []
        original = Path.open

        def metadata_only(path, *args, **kwargs):
            opened.append(path)
            self.assertIn(path.name, {"case.json", "plot.json"})
            return original(path, *args, **kwargs)

        with ExitStack() as stack:
            for name in ("liquid_tracer.plots.list_plots", "liquid_tracer.plots.reviewed_plot",
                         "liquid_tracer.investigation_boards.list_boards", "liquid_tracer.miro_state.load_state",
                         "liquid_tracer.cli.verify_export"):
                stack.enter_context(patch(name, side_effect=AssertionError("unexpected expensive read")))
            stack.enter_context(patch.object(Path, "open", metadata_only))
            result = case_workflow(self.case, lightweight=True)
        self.assertEqual(set(result), {"plots"})
        self.assertEqual(len(result["plots"]), 1)
        item = result["plots"][0]
        self.assertFalse(item["reviewable"])
        self.assertTrue(item["validation_pending"])
        self.assertFalse(item["empty"])
        self.assertEqual(item["preview_id"], directory.name)
        self.assertEqual(item["node_count"], 3)
        self.assertTrue(item["artifact"]["preview_url"].endswith("/graph.html"))
        self.assertNotIn(str(self.case), json.dumps(result))
        self.assertNotIn("archive_sha256", json.dumps(result))
        self.assertEqual(sum(path.name == "plot.json" for path in opened), 1)

    def test_reports_are_bounded_by_recency_and_size_before_open(self):
        directories = [self.report(number) for number in range(1, 5)]
        for number, directory in enumerate(directories, 1):
            os.utime(directory, ns=(number, number))
        (directories[-1] / "plot.json").write_bytes(b"x" * 1025)
        os.utime(directories[-1], ns=(4, 4))
        opened = []
        original = Path.open

        def capture(path, *args, **kwargs):
            if path.name == "plot.json":
                opened.append(path.parent.name)
            return original(path, *args, **kwargs)

        with patch("liquid_tracer.workflow_api.PLOT_SUMMARY_LIMIT", 2), \
                patch("liquid_tracer.workflow_api.PLOT_SUMMARY_BYTES", 1024), \
                patch.object(Path, "open", capture):
            result = case_workflow(self.case, lightweight=True)
        self.assertEqual(opened, [directories[2].name])
        self.assertEqual([item["id"] for item in result["plots"]], [directories[2].name])

    def test_invalid_identity_schema_counts_and_unfinished_reports_are_skipped(self):
        good = self.report()
        invalid = ({"case_id": "b" * 32}, {"run_id": "b" * 16}, {"goal": "unknown"},
                   {"schema_version": True}, {"schema_version": 2}, {"query": []},
                   {"node_count": True}, {"edge_count": -1}, {"transaction_count": "1"},
                   {"created_at": None}, {"input_snapshot_version": True}, {"csv_export_version": 1})
        for number, changes in enumerate(invalid, 2):
            self.report(number, **changes)
        unfinished = self.report(20)
        (unfinished / "SHA256SUMS").unlink()
        result = case_workflow(self.case, lightweight=True)
        self.assertEqual([item["id"] for item in result["plots"]], [good.name])

    def test_symlink_reports_directories_and_artifacts_are_not_exposed(self):
        good = self.report()
        linked_report = self.report(2)
        (linked_report / "plot.json").unlink()
        (linked_report / "plot.json").symlink_to(good / "plot.json")
        linked_artifact = self.report(3)
        (linked_artifact / "graph.html").unlink()
        (linked_artifact / "graph.html").symlink_to(good / "graph.html")
        (self.case / "previews" / ("a" * 16 + "-plots-00000004")).symlink_to(good, target_is_directory=True)
        result = case_workflow(self.case, lightweight=True)
        self.assertEqual([item["id"] for item in result["plots"]], [good.name])

    def test_selected_plot_is_the_only_plot_verified(self):
        plot = self.real_plot()
        self.report(99)
        with patch("liquid_tracer.plots.reviewed_plot", wraps=reviewed_plot) as review, \
                patch("liquid_tracer.plots.list_plots", side_effect=AssertionError("must not inspect other plots")):
            item = selected_plot(self.case, plot["preview_id"])
        review.assert_called_once_with(self.case, plot["preview_id"])
        self.assertTrue(item["reviewable"])
        self.assertFalse(item["validation_pending"])
        self.assertIn("preview_url", item["artifact"])
        self.assertEqual({value["name"] for value in item["artifact"]["downloads"] if value["name"].endswith(".csv")},
                         {"transactions.csv", "endpoints.csv"})

    def test_slow_selected_verification_does_not_block_metadata_listing(self):
        plot = self.real_plot()
        started, release = threading.Event(), threading.Event()
        from liquid_tracer.plots import _review_source

        def slow_source(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise AssertionError("test did not release source validation")
            return _review_source(*args, **kwargs)

        with ThreadPoolExecutor(max_workers=2) as executor, \
                patch("liquid_tracer.plots._review_source", side_effect=slow_source):
            selected = executor.submit(selected_plot, self.case, plot["preview_id"])
            try:
                self.assertTrue(started.wait(2))
                listing = executor.submit(case_workflow, self.case, lightweight=True).result(timeout=2)
                self.assertEqual(listing["plots"][0]["preview_id"], plot["preview_id"])
                self.assertTrue(listing["plots"][0]["validation_pending"])
                self.assertFalse(selected.done())
            finally:
                release.set()
            self.assertTrue(selected.result(timeout=5)["reviewable"])

    def test_listing_corrupt_plot_does_not_bypass_selected_or_download_validation(self):
        plot = self.real_plot()
        directory = Path(plot["directory"])
        (directory / "graph.html").write_text("changed since manifest")
        item = case_workflow(self.case, lightweight=True)["plots"][0]
        self.assertTrue(item["validation_pending"])
        self.assertFalse(item["reviewable"])
        for reader in (selected_plot, plot_artifact):
            with self.subTest(reader=reader.__name__), self.assertRaisesRegex(TraceError, "changed"):
                reader(self.case, plot["preview_id"])
        from liquid_tracer.plot_csv import build_plot_csv
        with self.assertRaisesRegex(TraceError, "changed"):
            build_plot_csv(self.case, plot["preview_id"], "endpoints.csv")

    def test_legacy_workflow_default_and_separate_boards_keep_authoritative_reads(self):
        board = {"id": "board-" + "b" * 32, "name": "Existing board", "goal": "full",
                 "board_id": "BOARD=", "can_sync": True, "state_file": "/private/mapping.json"}
        with patch("liquid_tracer.plots.list_plots", return_value=[]) as plots, \
                patch("liquid_tracer.investigation_boards.list_boards", return_value=[board]) as boards:
            legacy = case_workflow(self.case)
            separate = case_boards(self.case)
        plots.assert_called_once_with(self.case)
        self.assertEqual(boards.call_count, 2)
        self.assertEqual(legacy, {"plots": [], **separate})
        self.assertTrue(separate["boards"][0]["can_sync"])
        self.assertNotIn("state_file", separate["boards"][0])
        with patch("liquid_tracer.investigation_boards.list_boards", side_effect=TraceError("private path")):
            failed = case_boards(self.case)
        self.assertEqual(failed["boards"], [])
        self.assertNotIn("private", failed["boards_notice"])


if __name__ == "__main__":
    unittest.main()
