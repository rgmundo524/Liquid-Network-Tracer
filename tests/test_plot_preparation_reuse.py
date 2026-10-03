"""Reuse is confined to verified files within one active publication."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.investigation_boards import _publication_review, create_and_sync, list_boards, sync_board
from liquid_tracer.plots import preview_plot, reviewed_plot
from liquid_tracer.plot_performance import PlotTimings
from tests import test_plot_snapshot_concurrency as fixtures


class PreparationReuseTests(unittest.TestCase):
    setUp = fixtures.PlotSnapshotConcurrencyTests.setUp

    def test_one_review_for_create_and_sync_but_later_sync_reviews_again(self):
        plot = preview_plot(self.case, "full")
        self.layout.reset_mock()
        with patch("liquid_tracer.plots.reviewed_plot", wraps=reviewed_plot) as review:
            result = create_and_sync(self.case, plot["preview_id"], token="test", transport=self.remote, interval=0)
            self.assertEqual(review.call_count, 1)
            sync_board(self.case, result["record_id"], plot["preview_id"], token="test", transport=self.remote, interval=0)
            self.assertEqual(review.call_count, 2)
        self.assertEqual(len(self.remote.creations), 1)
        self.layout.assert_not_called()
        self.assertTrue({"load_source", "build_graph", "layout", "export_preview", "build_plan", "write_exports"}
                        <= plot["timings"].keys())
        self.assertTrue({"review_saved_plot", "recheck_saved_plot", "create_board", "write_miro"}
                        <= result["timings"].keys())
        self.assertTrue(all(value >= 0 for value in result["timings"].values()))

    def test_source_mutation_during_board_creation_prevents_item_writes(self):
        plot = preview_plot(self.case, "full")
        source = self.archive / "trace.json"
        def transport(method, url, *args):
            response = self.remote(method, url, *args)
            if method == "POST" and url.endswith("/v2/boards"):
                source.write_bytes(source.read_bytes() + b" ")
            return response
        with self.assertRaisesRegex(TraceError, "changed during publication"):
            create_and_sync(self.case, plot["preview_id"], token="test", transport=transport, interval=0)
        self.assertEqual(len(self.remote.creations), 1)
        self.assertFalse(next(iter(self.remote.boards.values())).items)
        self.assertEqual(list_boards(self.case)[0]["status"], "linked")

    def test_same_size_preview_edit_with_restored_mtime_is_detected(self):
        plot = preview_plot(self.case, "full")
        target = Path(plot["directory"]) / "graph.svg"
        def transport(method, url, *args):
            response = self.remote(method, url, *args)
            if method == "POST" and url.endswith("/v2/boards"):
                before = target.stat()
                data = target.read_bytes()
                target.write_bytes(data[:-1] + (b"!" if data[-1:] != b"!" else b"?"))
                os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
            return response
        with self.assertRaisesRegex(TraceError, "changed during publication"):
            create_and_sync(self.case, plot["preview_id"], token="test", transport=transport, interval=0)
        self.assertFalse(next(iter(self.remote.boards.values())).items)

    def test_review_is_bound_to_case_preview_and_context_lifetime(self):
        plot = preview_plot(self.case, "full")
        with _publication_review(self.case, plot["preview_id"], reuse=True) as review:
            self.assertIs(review.check(self.case, plot["preview_id"]), review)
            with self.assertRaisesRegex(TraceError, "not active"):
                review.check(self.case.parent, plot["preview_id"])
            with self.assertRaisesRegex(TraceError, "not active"):
                review.check(self.case, "a" * 16 + "-plots-" + "b" * 8)
        with self.assertRaisesRegex(TraceError, "not active"):
            review.check(self.case, plot["preview_id"])

    def test_mutation_during_initial_review_is_rejected(self):
        import liquid_tracer.plots as plots
        plot = preview_plot(self.case, "full")
        real_review = plots._review_source
        def mutate(*args, **kwargs):
            real_review(*args, **kwargs)
            source = self.archive / "trace.json"
            source.write_bytes(source.read_bytes() + b" ")
        with patch.object(plots, "_review_source", side_effect=mutate):
            with self.assertRaisesRegex(TraceError, "changed while being reviewed"):
                create_and_sync(self.case, plot["preview_id"], token="test", transport=self.remote, interval=0)
        self.assertFalse(self.remote.creations)

    def test_later_publication_cannot_use_previous_review_to_ignore_tampering(self):
        plot = preview_plot(self.case, "full")
        result = create_and_sync(self.case, plot["preview_id"], token="test", transport=self.remote, interval=0)
        target = Path(plot["directory"]) / "graph.svg"
        target.write_bytes(target.read_bytes() + b" ")
        with self.assertRaisesRegex(TraceError, "Saved plot changed"):
            sync_board(self.case, result["record_id"], plot["preview_id"], token="test", transport=self.remote, interval=0)


class PlotTimingTests(unittest.TestCase):
    def test_stage_reports_elapsed_and_accumulates_repeated_work(self):
        events = []
        timer = PlotTimings(events.append)
        with patch("liquid_tracer.plot_performance.time.monotonic", side_effect=[1, 3, 5, 6]):
            with timer.measure("build_graph"):
                pass
            with timer.measure("build_graph"):
                pass
        self.assertEqual(timer.snapshot(), {"build_graph": 3})
        self.assertEqual([e["elapsed_seconds"] for e in events if e["completed"]], [2, 1])
        self.assertEqual(events[-1]["phase"], "plot_build_graph")

    def test_cancellation_is_not_reported_as_completion(self):
        events = []
        timer = PlotTimings(events.append)
        with self.assertRaises(KeyboardInterrupt):
            with timer.measure("layout"):
                raise KeyboardInterrupt
        self.assertFalse(any(event["completed"] for event in events))
        self.assertIn("layout", timer.snapshot())

    def test_reporter_failure_does_not_change_result(self):
        def broken(event):
            raise OSError("synthetic observer failure")
        timer = PlotTimings(broken)
        with timer.measure("build_graph"):
            value = 42
        self.assertEqual(value, 42)
