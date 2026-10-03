"""Fresh-board publication remains resumable and does not overwrite old reviews."""

import copy
from unittest.mock import patch
import unittest

from liquid_tracer.common import TraceError, canonical, read_json
from liquid_tracer.investigation_boards import create_and_sync, link_board, list_boards, sync_board
from liquid_tracer.investigations import update_case
from liquid_tracer.miro import make_plan, sync as sync_engine
from liquid_tracer.plots import list_plots, preview_plot, reviewed_plot
from liquid_tracer.services import set_service
from tests import test_investigation_boards, test_plots
from tests.test_presentation_annotations import AnnotationMiro


class BoardRemote:
    def __init__(self):
        self.boards = {}
        self.creations = []

    def __call__(self, method, url, headers, body, timeout):
        if method == "POST" and url == "https://api.miro.com/v2/boards":
            board = "created-board-" + str(len(self.boards) + 1)
            self.boards[board] = AnnotationMiro()
            self.creations.append(body)
            return 201, {}, canonical({"id": board})
        board = url.split("/boards/", 1)[1].split("/", 1)[0]
        return self.boards[board](method, url, headers, body, timeout)


class BoardWorkflowTests(unittest.TestCase):
    setUp = test_investigation_boards.InvestigationBoardTests.setUp
    source = test_investigation_boards.InvestigationBoardTests.source

    def fresh(self):
        graph, plan = self.source()
        graph["plot"]["layout_mode"] = "fresh"
        return graph, plan

    def test_create_and_sync_preflights_budget_before_creating_empty_board(self):
        remote = BoardRemote()
        with patch("liquid_tracer.plots.reviewed_plot", return_value=self.fresh()):
            with self.assertRaisesRegex(TraceError, "max-items"):
                create_and_sync(self.case, "review-a", max_items=1, token="test", transport=remote, interval=0)
        self.assertFalse(remote.creations)
        self.assertFalse(self.registry.exists())

    def test_repeated_fresh_review_resumes_same_board_preserving_manual_movement(self):
        remote = BoardRemote()
        with patch("liquid_tracer.plots.reviewed_plot", return_value=self.fresh()):
            first = create_and_sync(self.case, "review-a", "Paths", token="test", transport=remote, interval=0)
            record = next(item for item in list_boards(self.case) if item["id"] == first["record_id"])
            mapping = read_json(self.case / record["state_file"])
            item = remote.boards[first["board_id"]].items[mapping["items"]["addr:a"]["id"]]
            item["position"].update(x=2000, y=3000)
            moved = copy.deepcopy(item)
            second = create_and_sync(self.case, "review-a", "Renamed request", token="test", transport=remote, interval=0)
        self.assertEqual(first["board_id"], second["board_id"])
        self.assertEqual(len(remote.creations), 1)
        self.assertTrue(first["created_board"])
        self.assertTrue(second["reused_board"])
        self.assertEqual(item, moved)

    def test_distinct_fresh_reviews_with_same_name_create_separate_boards(self):
        remote = BoardRemote()
        with patch("liquid_tracer.plots.reviewed_plot", return_value=self.fresh()):
            first = create_and_sync(self.case, "review-a", "Paths", token="test", transport=remote, interval=0)
            second = create_and_sync(self.case, "review-b", "Paths", token="test", transport=remote, interval=0)
        self.assertNotEqual(first["board_id"], second["board_id"])
        self.assertNotEqual(first["record_id"], second["record_id"])
        self.assertEqual(len(remote.creations), 2)

    def test_failure_after_creation_reuses_acknowledged_board_on_retry(self):
        remote = BoardRemote()
        with patch("liquid_tracer.plots.reviewed_plot", return_value=self.fresh()):
            with patch("liquid_tracer.investigation_boards.sync_board", side_effect=TraceError("Synthetic sync failure")):
                with self.assertRaisesRegex(TraceError, "Synthetic"):
                    create_and_sync(self.case, "review-a", token="test", transport=remote, interval=0)
            result = create_and_sync(self.case, "review-a", token="test", transport=remote, interval=0)
        self.assertEqual(len(remote.creations), 1)
        self.assertTrue(result["reused_board"])
        self.assertGreater(result["created"], 0)

    def test_fresh_layout_cannot_be_sent_to_an_arbitrary_linked_board(self):
        record = link_board(self.case, "pegouts", "Existing", "existing-board")
        with patch("liquid_tracer.plots.reviewed_plot", return_value=self.fresh()):
            with self.assertRaisesRegex(TraceError, "Create and sync"):
                sync_board(self.case, record["id"], "review-a", transport=lambda *a: self.fail("No remote request expected"))

    def test_empty_fresh_layout_never_creates_a_board(self):
        graph, plan = self.fresh()
        graph["nodes"] = []
        with patch("liquid_tracer.plots.reviewed_plot", return_value=(graph, plan)):
            with self.assertRaisesRegex(TraceError, "no matching paths"):
                create_and_sync(self.case, "review-a", transport=lambda *a: self.fail("No remote request expected"))

    def test_update_cannot_be_published_to_another_board_or_create_fresh_board(self):
        record = link_board(self.case, "pegouts", "Existing", "existing-board")
        graph, plan = self.source()
        graph["plot"].update(layout_mode="update", board_record_id=record["id"], board_id="other-board")
        with patch("liquid_tracer.plots.reviewed_plot", return_value=(graph, plan)):
            for action in (lambda: create_and_sync(self.case, "review-a"),
                           lambda: sync_board(self.case, record["id"], "review-a")):
                with self.assertRaises(TraceError):
                    action()

    def test_legacy_update_pruning_is_rejected_before_board_writes(self):
        from liquid_tracer.board_layout import capture, prepare_graph
        record = next(item for item in list_boards(self.case) if item.get("legacy"))
        remote = AnnotationMiro()
        source = self.source(goal="full", extended=True)
        with patch("liquid_tracer.plots.reviewed_plot", return_value=source):
            sync_board(self.case, record["id"], "old-review", token="test", transport=remote, interval=0)
        graph, _ = self.source(goal="full", run="two")
        snapshot = capture(record["board_id"], self.case / record["state_file"], graph["namespace"],
                           token="test", transport=remote, interval=0)
        graph = prepare_graph(graph, snapshot)
        graph["plot"].update(layout_mode="update", board_record_id=record["id"], board_id=record["board_id"])
        before, writes = copy.deepcopy(remote.items), len(remote.writes)
        with patch("liquid_tracer.plots.reviewed_plot", return_value=(graph, make_plan(graph))):
            with self.assertRaisesRegex(TraceError, "older full-trace board"):
                sync_board(self.case, record["id"], "new-review", token="test", transport=remote, interval=0)
        self.assertEqual(remote.items, before)
        self.assertEqual(len(remote.writes), writes)

    def test_legacy_update_can_add_and_restyle_and_repeated_sync_is_idempotent(self):
        from liquid_tracer.board_layout import capture, prepare_graph
        record = next(item for item in list_boards(self.case) if item.get("legacy"))
        remote = AnnotationMiro()
        source = self.source(goal="full")
        with patch("liquid_tracer.plots.reviewed_plot", return_value=source):
            sync_board(self.case, record["id"], "old-review", token="test", transport=remote, interval=0)
        graph, _ = self.source(goal="full", run="two", extended=True)
        graph["nodes"][0]["color"] = "#123456"
        snapshot = capture(record["board_id"], self.case / record["state_file"], graph["namespace"],
                           token="test", transport=remote, interval=0)
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda value, **kw: value):
            graph = prepare_graph(graph, snapshot)
        graph["plot"].update(layout_mode="update", board_record_id=record["id"], board_id=record["board_id"])
        with patch("liquid_tracer.plots.reviewed_plot", return_value=(graph, make_plan(graph))):
            report = sync_board(self.case, record["id"], "new-review", token="test", transport=remote, interval=0)
            self.assertEqual(report["created"], 4)
            mapping = read_json(self.case / record["state_file"])["items"]
            self.assertEqual(remote.items[mapping["addr:a"]["id"]]["style"]["fillColor"], "#123456")
            writes = len(remote.writes)
            sync_board(self.case, record["id"], "new-review", token="test", transport=remote, interval=0)
            self.assertEqual(len(remote.writes), writes)


class BoardAwarePlotTests(unittest.TestCase):
    setUp = test_plots.PlotTests.setUp

    def seed_board(self):
        from liquid_tracer.investigation_boards import _board_plan
        fresh = preview_plot(self.case, "full")
        graph, _ = reviewed_plot(self.case, fresh["preview_id"])
        record = link_board(self.case, "full", "Investigator working board", "existing-board")
        remote = AnnotationMiro()
        old = copy.deepcopy(graph)
        new_id = old["nodes"][-1]["id"]
        old["nodes"] = [node for node in old["nodes"] if node["id"] != new_id]
        old["edges"] = [edge for edge in old["edges"] if new_id not in (edge["source"], edge["target"])]
        old.pop("activity_frames", None)
        sync_engine(_board_plan(make_plan(old), record), record["board_id"], self.case / record["state_file"],
                    token="test", transport=remote, interval=0)
        return record, remote, new_id

    def test_update_reads_selected_board_and_lays_out_only_new_nodes(self):
        record, remote, new_id = self.seed_board()
        state = read_json(self.case / record["state_file"])
        key = next(key for key in state["items"] if key != "legend" and state["items"][key]["endpoint"] == "shapes")
        body = remote.items[state["items"][key]["id"]]
        body["position"].update(x=2500, y=2100)
        before = copy.deepcopy(remote.items)
        self.layout.reset_mock()
        remote.calls.clear()
        result = preview_plot(self.case, "full", layout_mode="update", board_record_id=record["id"],
                              token="test", transport=remote, interval=0)
        graph, plan = reviewed_plot(self.case, result["preview_id"])
        self.assertEqual(result["layout_mode"], "update")
        self.assertEqual(result["board_record_id"], record["id"])
        self.assertEqual(result["board_id"], record["board_id"])
        self.assertEqual(result["update_counts"]["new_nodes"], 1)
        self.assertEqual(graph["board_layout"], plan["board_layout"])
        retained = next(node for node in graph["nodes"] if node["id"] == key)
        self.assertEqual((retained["x"], retained["y"]), (2500, 2100))
        self.layout.assert_called_once()
        self.assertEqual([node["id"] for node in self.layout.call_args.args[0]["nodes"]], [new_id])
        self.assertEqual(remote.items, before)
        self.assertTrue(remote.calls)
        self.assertFalse(remote.writes)
        listed = next(plot for plot in list_plots(self.case) if plot["preview_id"] == result["preview_id"])
        self.assertTrue(listed["reviewable"])

    def test_update_requires_a_matching_target_before_remote_requests(self):
        target = link_board(self.case, "pegouts", "Wrong goal", "other-board")
        for kwargs in ({"layout_mode": "update"}, {"layout_mode": "fresh", "board_record_id": target["id"]},
                       {"layout_mode": "update", "board_record_id": target["id"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(TraceError):
                preview_plot(self.case, "full", transport=lambda *a: self.fail("No remote request expected"), **kwargs)

    def test_empty_update_captures_target_without_running_elk(self):
        target = link_board(self.case, "pegouts", "Paths", "existing-board")
        remote = AnnotationMiro()
        result = preview_plot(self.case, "pegouts", max_hops=0, layout_mode="update", board_record_id=target["id"],
                              token="test", transport=remote, interval=0)
        graph, plan = reviewed_plot(self.case, result["preview_id"])
        self.assertTrue(result["empty"])
        self.assertEqual(plan["shapes"], [])
        self.assertEqual(plan["board_layout"], graph["board_layout"])
        self.layout.assert_not_called()
        self.assertFalse(remote.writes)

    def test_updated_stop_rule_can_review_and_remove_all_pegout_items(self):
        from liquid_tracer.investigation_boards import _board_plan
        fresh = preview_plot(self.case, "pegouts")
        _, plan = reviewed_plot(self.case, fresh["preview_id"])
        record = link_board(self.case, "pegouts", "Paths", "existing-board")
        remote = AnnotationMiro()
        sync_engine(_board_plan(plan, record), record["board_id"], self.case / record["state_file"],
                    token="test", transport=remote, interval=0)
        note = {"id": "investigator-note", "type": "shape", "data": {"content": "Keep this note", "shape": "rectangle"},
                "geometry": {"width": 200, "height": 100}, "position": {"x": 8000, "y": 8000}}
        remote.items[note["id"]] = copy.deepcopy(note)
        set_service(self.case, "SYNTHETIC-c-address", name="Stop", stop_tracing=True)
        result = preview_plot(self.case, "pegouts", layout_mode="update", board_record_id=record["id"],
                              token="test", transport=remote, interval=0)
        self.assertTrue(result["empty"])
        with patch("liquid_tracer.miro.sync", wraps=sync_engine):
            synced = sync_board(self.case, record["id"], result["preview_id"], token="test", transport=remote, interval=0)
        self.assertGreater(synced["deleted"], 0)
        self.assertEqual(remote.items, {note["id"]: note})

    def test_legacy_scope_removal_is_rejected_before_board_snapshot_or_elk(self):
        update_case(self.case, {"miro_board": "legacy-full-board"})
        record = next(item for item in list_boards(self.case) if item.get("legacy"))
        fresh = preview_plot(self.case, "full")
        _, plan = reviewed_plot(self.case, fresh["preview_id"])
        remote = AnnotationMiro()
        sync_engine(plan, record["board_id"], self.case / record["state_file"],
                    token="test", transport=remote, interval=0)
        set_service(self.case, "SYNTHETIC-a-address", name="Stop", stop_tracing=True)
        before = copy.deepcopy(remote.items)
        self.layout.reset_mock()
        with self.assertRaisesRegex(TraceError, "older full-trace board"):
            preview_plot(self.case, "full", layout_mode="update", board_record_id=record["id"],
                         token="test", transport=lambda *a: self.fail("No remote reads needed for unsupported removals"))
        self.layout.assert_not_called()
        self.assertEqual(remote.items, before)


if __name__ == "__main__":
    unittest.main()
