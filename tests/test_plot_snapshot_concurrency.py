"""Saved plot inputs isolate layouts/publication from the live collector."""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError, digest, read_json, save_json
from liquid_tracer.investigation_boards import (
    _board_lock, create_and_sync, generate_and_sync, list_boards, sync_board,
)
from liquid_tracer.investigations import create_investigation, read_case, save_plot_settings
from liquid_tracer.plots import _settings, list_plots, plot_files, preview_plot, reviewed_plot
from liquid_tracer.services import set_service
from tests.test_attribution_convergence import graph_state, tx
from tests.test_board_workflow import BoardRemote
from tests.test_connections import saved_case


class PlotSnapshotConcurrencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.case = create_investigation(Path(temporary.name), "Snapshot concurrency",
            seeds=[tx("a") + ":0"], run_defaults={"layout_attempts": 1})
        state = graph_state((("a:0", "c"), ("c:0", "b")), seeds=("a:0",))
        self.state, self.archive = saved_case(self.case, state)
        layout = patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph)
        self.layout = layout.start()
        self.addCleanup(layout.stop)
        self.remote = BoardRemote()

    @contextmanager
    def collecting(self):
        # The actual collector owns this OS lock for its entire run. Keeping
        # it exclusive here reproduces that boundary for every plotting stage.
        with (self.case / "trace.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def assert_case_unlocked(self):
        with (self.case / "case.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def rehash(self, directory):
        (directory / "SHA256SUMS").write_text("".join(
            digest((directory / name).read_bytes()) + "  " + name + "\n"
            for name in sorted(plot_files(directory) - {"SHA256SUMS"})))

    def test_collecting_allows_new_layout_review_listing_and_board_publication(self):
        phases = []

        def layout(graph, **kwargs):
            self.assert_case_unlocked()
            phases.append("layout")
            return graph

        def transport(*args):
            self.assert_case_unlocked()
            phases.append("miro")
            return self.remote(*args)

        self.layout.side_effect = layout
        with self.collecting():
            plot = preview_plot(self.case, "full")
            reviewed_plot(self.case, plot["preview_id"])
            self.assertTrue(list_plots(self.case)[0]["reviewable"])
            first = create_and_sync(self.case, plot["preview_id"], token="test",
                                    transport=transport, interval=0, workers=1)
            again = sync_board(self.case, first["record_id"], plot["preview_id"], token="test",
                               transport=transport, interval=0, workers=1)
        self.assertIn("layout", phases)
        self.assertIn("miro", phases)
        self.assertEqual(len(self.remote.creations), 1)
        self.assertEqual(again["created"], 0)

    def test_preferences_can_change_during_elk_without_retargeting_captured_inputs(self):
        settings = _settings(read_case(self.case))
        settings.update(connector_style="curved", layout_attempts=3)

        def layout(graph, **kwargs):
            self.assert_case_unlocked()
            set_service(self.case, "SYNTHETIC-c-address", name="Stop", stop_tracing=True)
            save_plot_settings(self.case, {"connector_style": "straight"})
            self.assertEqual(kwargs["connector_style"], "curved")
            return graph

        self.layout.side_effect = layout
        plot = preview_plot(self.case, "full", layout_settings=settings)
        graph, _ = reviewed_plot(self.case, plot["preview_id"])
        self.assertEqual(graph["plot"]["layout_settings"], settings)
        self.assertIn("tx:" + tx("b"), {node["id"] for node in graph["nodes"]})
        inputs = read_json(Path(plot["directory"]) / "inputs.json")
        self.assertEqual(inputs["run_id"], self.state["run_id"])
        self.assertFalse(inputs["service_controls"].get("rules"))
        self.layout.side_effect = lambda graph, **kwargs: graph
        fresh = preview_plot(self.case, "full")
        current, _ = reviewed_plot(self.case, fresh["preview_id"])
        self.assertNotIn("tx:" + tx("b"), {node["id"] for node in current["nodes"]})

    def test_input_snapshot_tampering_is_rejected_even_if_manifest_is_rehashed(self):
        plot = preview_plot(self.case, "full")
        directory = Path(plot["directory"])
        original = read_json(directory / "inputs.json")
        for change in ("case_id", "run_id", "service_controls", "address_tx_counts", "captured_at"):
            inputs = deepcopy(original)
            inputs[change] = {} if change in ("service_controls", "address_tx_counts") else "changed"
            if inputs == original:
                inputs[change] = {"changed": True}
            save_json(directory / "inputs.json", inputs)
            self.rehash(directory)
            with self.subTest(change=change), self.assertRaisesRegex(TraceError, "input snapshot"):
                reviewed_plot(self.case, plot["preview_id"])

    def test_update_holds_one_board_lock_through_capture_layout_and_publication(self):
        set_service(self.case, "SYNTHETIC-c-address", name="Limited", stop_tracing=True)
        first = generate_and_sync(self.case, "full", token="test", transport=self.remote, interval=0)
        record = next(item for item in list_boards(self.case) if item["id"] == first["record_id"])
        set_service(self.case, "SYNTHETIC-c-address", name="Limited", stop_tracing=False)
        stages = []

        def check(stage):
            self.assert_case_unlocked()
            with self.assertRaisesRegex(TraceError, "Miro board is busy"):
                with _board_lock(self.case, record["id"]):
                    pass
            with _board_lock(self.case, "unrelated-board"):
                pass
            stages.append(stage)

        def layout(graph, **kwargs):
            check("layout")
            return graph

        def transport(*args):
            check("read" if args[0] == "GET" else "write")
            return self.remote(*args)

        self.layout.side_effect = layout
        with self.collecting():
            result = generate_and_sync(self.case, "full", layout_mode="update",
                board_record_id=record["id"], token="test", transport=transport, interval=0, workers=1)
        self.assertTrue(result["published"])
        self.assertTrue({"layout", "read", "write"}.issubset(stages))
        with _board_lock(self.case, record["id"]):
            pass


if __name__ == "__main__":
    unittest.main()
