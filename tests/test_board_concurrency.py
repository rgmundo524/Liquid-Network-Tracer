"""Independent processes publish different boards without losing registry edits."""

import fcntl
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from liquid_tracer.common import TraceError, canonical, read_json
from liquid_tracer.investigation_boards import (
    _check_unfinished_creation, create_and_sync, create_board, link_board, sync_board,
)
from liquid_tracer.investigations import create_investigation, read_case, save_plot_settings
from liquid_tracer.miro import make_plan
from liquid_tracer.services import set_service
from tests.test_board_workflow import BoardRemote
from tests.test_miro_sync import graph
from tests.test_presentation_annotations import AnnotationMiro


def _source(case, goal="full", fresh=False):
    value = graph("one")
    value["namespace"]["case_id"] = read_case(case)["case_id"]
    value["plot"] = {"goal": goal, **({"layout_mode": "fresh"} if fresh else {})}
    return value, make_plan(value)


def _publish(case, record, entered, release, output, fail=False):
    """Pause inside the actual sync engine's first transport call."""
    remote = AnnotationMiro()
    first = True

    def transport(*args):
        nonlocal first
        if first:
            first = False
            entered.set()
            if not release.wait(timeout=15):
                raise AssertionError("Publication was never released")
            if fail:
                raise TraceError("Synthetic remote failure")
        return remote(*args)

    try:
        with patch("liquid_tracer.plots.reviewed_plot", return_value=_source(case, record["goal"])):
            result = sync_board(case, record["id"], "review-" + record["goal"], token="test",
                                transport=transport, interval=0, workers=1)
        output.put((record["id"], "ok", result))
    except Exception as error:
        output.put((record["id"], "error", str(error)))


def _create(case, name, entered, release, output, reject=False, publish=False):
    remote = BoardRemote()

    def transport(method, url, headers, body, timeout):
        if method == "POST" and url == "https://api.miro.com/v2/boards":
            entered.set()
            if not release.wait(timeout=15):
                raise AssertionError("Creation was never released")
            if reject:
                return 403, {}, b"{}"
            if not publish:
                return 201, {}, canonical({"id": "created-" + name})
        return remote(method, url, headers, body, timeout)

    try:
        if publish:
            with patch("liquid_tracer.plots.reviewed_plot", return_value=_source(case, fresh=True)):
                result = create_and_sync(case, "review-" + name, name, token="test",
                                         transport=transport, interval=0, workers=1)
        else:
            result = create_board(case, "full", name, token="test", transport=transport)
        output.put((name, "ok", result))
    except Exception as error:
        output.put((name, "error", str(error)))


class BoardConcurrencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.case = create_investigation(Path(temporary.name), "Concurrent boards")
        self.registry = self.case / "miro" / "boards.json"
        self.context = multiprocessing.get_context("spawn")
        self.output = self.context.Queue()
        self.addCleanup(self.output.close)

    def start(self, target, *args, **kwargs):
        entered, release = self.context.Event(), self.context.Event()
        process = self.context.Process(target=target,
                                       args=(self.case, *args, entered, release, self.output), kwargs=kwargs)
        process.start()

        def stop():
            release.set()
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        self.addCleanup(stop)
        return process, entered, release

    def records(self):
        return {item["id"]: item for item in read_json(self.registry)["boards"]}

    def finish(self, running):
        for _, _, release in running:
            release.set()
        results = [self.output.get(timeout=15) for _ in running]
        for process, _, _ in running:
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
        return {identity: (status, result) for identity, status, result in results}

    def test_two_processes_sync_different_boards_and_preserve_a_concurrent_new_entry(self):
        boards = [link_board(self.case, goal, goal, "target-" + goal) for goal in ("full", "pegouts")]
        running = [self.start(_publish, board) for board in boards]
        for _, entered, _ in running:
            self.assertTrue(entered.wait(timeout=10), "Both publishers must reach remote work together")
        self.assertEqual({item["status"] for item in self.records().values()}, {"syncing"})
        third = link_board(self.case, "connections", "Added while syncing", "third-target")
        results = self.finish(running)
        saved = self.records()
        self.assertEqual(set(saved), {item["id"] for item in [*boards, third]})
        for board in boards:
            self.assertEqual(results[board["id"]][0], "ok", results)
            self.assertEqual(saved[board["id"]]["status"], "synced")
            self.assertEqual(saved[board["id"]]["preview_id"], "review-" + board["goal"])
            mapping = read_json(self.case / board["state_file"])
            self.assertTrue(mapping["items"])
            self.assertTrue(mapping["namespace"]["case_id"].endswith(":" + board["id"]))
        self.assertEqual(saved[third["id"]]["status"], "linked")

    def test_one_sync_failure_does_not_erase_another_boards_completion(self):
        boards = [link_board(self.case, goal, goal, "target-" + goal) for goal in ("full", "pegouts")]
        running = [self.start(_publish, board, fail=index == 0) for index, board in enumerate(boards)]
        for _, entered, _ in running:
            self.assertTrue(entered.wait(timeout=10))
        results = self.finish(running)
        self.assertEqual(results[boards[0]["id"]][0], "error")
        self.assertIn("Synthetic remote failure", results[boards[0]["id"]][1])
        self.assertEqual(results[boards[1]["id"]][0], "ok")
        saved = self.records()
        self.assertEqual(saved[boards[0]["id"]]["status"], "sync_error")
        self.assertEqual(saved[boards[1]["id"]]["status"], "synced")
        self.assertEqual(saved[boards[0]["id"]]["preview_id"], "review-full")

    def test_same_board_is_busy_and_investigation_inputs_stay_protected(self):
        board = link_board(self.case, "full", "Working board", "target-full")
        running = self.start(_publish, board)
        self.assertTrue(running[1].wait(timeout=10))
        before = self.registry.read_bytes()
        with patch("liquid_tracer.plots.reviewed_plot") as review:
            with self.assertRaisesRegex(TraceError, "This Miro board is busy"):
                sync_board(self.case, board["id"], "other-review")
            review.assert_not_called()
        for action in (
            lambda: link_board(self.case, "full", "Renamed", board["board_id"], record_id=board["id"]),
            lambda: create_board(self.case, "full", "Working board", token="test"),
        ):
            with self.assertRaisesRegex(TraceError, "This Miro board is busy"):
                action()
        with self.assertRaisesRegex(TraceError, "operation is active"):
            save_plot_settings(self.case, {"connector_style": "curved"})
        with self.assertRaisesRegex(TraceError, "trace is running"):
            set_service(self.case, "SYNTHETIC-address", name="Changed during sync", stop_tracing=True)
        for name in ("trace.lock", "case.lock"):
            with (self.case / name).open("a") as handle:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(self.finish([running])[board["id"]][0], "ok")
        save_plot_settings(self.case, {"connector_style": "curved"})

    def test_parallel_creations_merge_acknowledgment_and_rejection_without_lost_records(self):
        running = [self.start(_create, name, reject=name == "rejected") for name in ("accepted", "rejected")]
        for _, entered, _ in running:
            self.assertTrue(entered.wait(timeout=10), "Both board POSTs must overlap")
        pending = {item["name"]: item for item in self.records().values()}
        self.assertEqual({item["status"] for item in pending.values()}, {"pending_creation"})
        remote = Mock()
        with self.assertRaisesRegex(TraceError, "This Miro board is busy"):
            create_board(self.case, "full", "accepted", token="test", transport=remote)
        with self.assertRaisesRegex(TraceError, "This Miro board is busy"):
            link_board(self.case, "full", "accepted", "recovery-target", record_id=pending["accepted"]["id"])
        remote.assert_not_called()
        third = link_board(self.case, "connections", "Third", "third-target")
        results = self.finish(running)
        self.assertEqual(results["accepted"][0], "ok")
        self.assertEqual(results["rejected"][0], "error")
        self.assertIn("HTTP 403", results["rejected"][1])
        saved = self.records()
        self.assertEqual(saved[pending["accepted"]["id"]]["board_id"], "created-accepted")
        self.assertEqual(saved[pending["rejected"]["id"]]["status"], "creation_rejected")
        self.assertEqual(saved[third["id"]]["status"], "linked")

    def test_create_and_sync_keeps_data_stable_across_board_post(self):
        running = self.start(_create, "fresh", publish=True)
        self.assertTrue(running[1].wait(timeout=10))
        # Another fresh workflow is allowed while a different creation has a
        # live owner. A failed/crashed POST still requires explicit recovery.
        _check_unfinished_creation(self.case)
        with self.assertRaisesRegex(TraceError, "trace is running"):
            set_service(self.case, "SYNTHETIC-address", name="Changed during creation", stop_tracing=True)
        with self.assertRaisesRegex(TraceError, "operation is active"):
            save_plot_settings(self.case, {"connector_style": "curved"})
        other = link_board(self.case, "connections", "Another board", "other-target")
        result = self.finish([running])["fresh"]
        self.assertEqual(result[0], "ok", result)
        saved = self.records()
        self.assertEqual(saved[result[1]["record_id"]]["status"], "synced")
        self.assertEqual(saved[other["id"]]["status"], "linked")

    def test_abandoned_creation_still_requires_recovery_before_another_new_board(self):
        with self.assertRaisesRegex(TraceError, "uncertain"):
            create_board(self.case, "full", "Abandoned", creation_preview_id="saved-review", token="test",
                         transport=Mock(return_value=(503, {}, b"{}")))
        with self.assertRaisesRegex(TraceError, "resume saved plot saved-review"):
            _check_unfinished_creation(self.case)

    def test_stale_plot_rejection_does_not_disturb_a_different_active_board(self):
        boards = [link_board(self.case, goal, goal, "target-" + goal) for goal in ("full", "pegouts")]
        running = self.start(_publish, boards[0])
        self.assertTrue(running[1].wait(timeout=10))
        before = self.registry.read_bytes()
        remote = Mock()
        with patch("liquid_tracer.plots.reviewed_plot", side_effect=TraceError("Evidence changed; regenerate the plot")):
            with self.assertRaisesRegex(TraceError, "regenerate the plot"):
                sync_board(self.case, boards[1]["id"], "stale-preview", transport=remote)
        remote.assert_not_called()
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(self.finish([running])[boards[0]["id"]][0], "ok")
        self.assertEqual(self.records()[boards[1]["id"]]["status"], "linked")


if __name__ == "__main__":
    unittest.main()
