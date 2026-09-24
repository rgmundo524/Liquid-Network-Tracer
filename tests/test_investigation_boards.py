"""Board management shares one investigation without conflating board maps."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from liquid_tracer.common import TraceError, canonical, digest, read_json, save_json
from liquid_tracer.investigation_boards import create_board, link_board, list_boards, sync_board
from liquid_tracer.investigations import create_investigation, read_case, update_case
from liquid_tracer.miro import make_plan
from tests.test_miro_sync import graph
from tests.test_presentation_annotations import AnnotationMiro


class InvestigationBoardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.case = create_investigation(Path(temporary.name), "Synthetic case", board="full-board")
        self.registry = self.case / "miro" / "boards.json"
        environment = patch.dict(os.environ, {"MIRO_ACCESS_TOKEN": "synthetic-token"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def source(self, goal="pegouts", run="one", extended=False):
        value = graph(run, extended)
        value["namespace"]["case_id"] = read_case(self.case)["case_id"]
        value["plot"] = {"goal": goal}
        return value, make_plan(value)

    def test_create_private_goal_board_once_and_multiple_boards_per_goal(self):
        original = read_case(self.case)
        responses = [canonical({"id": target}) for target in ("pegout-one", "pegout-two", "connection-one")]
        def transport(method, url, headers, body, timeout):
            self.assertEqual((method, url), ("POST", "https://api.miro.com/v2/boards"))
            self.assertEqual(read_json(self.registry)["boards"][-1]["status"], "pending_creation")
            self.assertEqual(json.loads(body)["policy"]["sharingPolicy"], {
                "access": "private", "organizationAccess": "private", "teamAccess": "private"})
            return 201, {}, responses.pop(0)
        remote = Mock(side_effect=transport)
        first = create_board(self.case, "pegouts", "Peg-out paths", transport=remote)
        self.assertTrue(first["created"])
        again = create_board(self.case, "pegouts", "Peg-out paths", transport=remote)
        self.assertEqual(again["id"], first["id"])
        self.assertTrue(again["reused"])
        second = create_board(self.case, "pegouts", "Peg-outs extended", transport=remote)
        self.assertNotEqual(first["id"], second["id"])
        create_board(self.case, "connections", "Seed connections", transport=remote)
        self.assertEqual(remote.call_count, 3)
        self.assertEqual(read_case(self.case), original)
        self.assertEqual(len(list_boards(self.case)), 4)
        self.assertNotIn("synthetic-token", self.registry.read_text())

    def test_uncertain_create_is_not_replayed_and_link_recovers_same_entry(self):
        remote = Mock(return_value=(503, {}, b"unsafe server body"))
        with self.assertRaisesRegex(TraceError, "uncertain"):
            create_board(self.case, "pegouts", "Paths", transport=remote)
        record = next(item for item in list_boards(self.case) if item["goal"] == "pegouts")
        self.assertEqual(record["status"], "pending_creation")
        self.assertFalse(record["can_sync"])
        with self.assertRaisesRegex(TraceError, "uncertain"):
            create_board(self.case, "pegouts", "Paths", transport=remote)
        recovered = link_board(self.case, "pegouts", "Paths", "recovered-board", record_id=record["id"])
        self.assertEqual(recovered["id"], record["id"])
        self.assertTrue(recovered["can_sync"])
        self.assertEqual(create_board(self.case, "pegouts", "Paths", transport=remote)["board_id"], "recovered-board")
        self.assertEqual(remote.call_count, 1)
        with self.assertRaisesRegex(TraceError, "cannot be replaced"):
            link_board(self.case, "pegouts", "Paths", "another-board", record_id=record["id"])

    def test_successful_post_with_failed_ack_save_retains_pending(self):
        real_save = save_json
        def fail_ack(path, data):
            if Path(path) == self.registry and data["boards"][0].get("board_id"):
                raise OSError("Synthetic disk error")
            real_save(path, data)
        remote = Mock(return_value=(201, {}, b'{"id":"acknowledged-board"}'))
        with patch("liquid_tracer.investigation_boards.save_json", side_effect=fail_ack):
            with self.assertRaisesRegex(TraceError, "acknowledged-board"):
                create_board(self.case, "pegouts", "Paths", transport=remote)
        with self.assertRaisesRegex(TraceError, "uncertain"):
            create_board(self.case, "pegouts", "Paths", transport=remote)
        self.assertEqual(remote.call_count, 1)

    def test_definite_rejection_can_retry_and_invalid_input_never_posts(self):
        remote = Mock(return_value=(403, {}, b"unsafe body"))
        with self.assertRaisesRegex(TraceError, "HTTP 403"):
            create_board(self.case, "pegouts", "Paths", transport=remote)
        self.assertEqual(read_json(self.registry)["boards"][0]["status"], "creation_rejected")
        remote.return_value = (201, {}, b'{"id":"created-board"}')
        self.assertTrue(create_board(self.case, "pegouts", "Paths", transport=remote)["created"])
        before = remote.call_count
        for goal, name in (("invalid", "Paths"), ("full", ""), ("full", "bad\nname")):
            with self.assertRaises(TraceError):
                create_board(self.case, goal, name, transport=remote)
        self.assertEqual(remote.call_count, before)

    def test_list_discovers_legacy_boards_without_rewriting_any_mapping(self):
        paths = []
        for goal, board in (("full", "old-full"), ("connections", "old-connections"), ("pegouts", "old-pegouts")):
            name = ("" if goal == "full" else goal + "-") + digest(board.encode())[:24] + ".json"
            path = self.case / "miro" / name
            save_json(path, {"board_id": board, "namespace": {"case_id": read_case(self.case)["case_id"]},
                             "items": {}, "runs": {}})
            paths.append(path)
        original = {path: path.read_bytes() for path in paths}
        records = list_boards(self.case)
        self.assertEqual({item["board_id"] for item in records}, {"full-board", "old-full", "old-connections", "old-pegouts"})
        self.assertEqual(sum(item["legacy_snapshot"] for item in records), 2)
        self.assertFalse(self.registry.exists())
        self.assertEqual({path: path.read_bytes() for path in paths}, original)

    def test_one_target_cannot_be_claimed_by_two_goals_or_replace_main_board(self):
        with self.assertRaisesRegex(TraceError, "already belongs"):
            link_board(self.case, "pegouts", "Paths", "full-board")
        link_board(self.case, "pegouts", "Paths", "new-board")
        with self.assertRaisesRegex(TraceError, "already belongs"):
            link_board(self.case, "connections", "Connections", "new-board")
        self.assertEqual(read_case(self.case)["miro_board"], "full-board")

    def test_same_board_refresh_grows_then_shrinks_preserving_surviving_ids(self):
        board = link_board(self.case, "pegouts", "Paths", "pegout-board")
        remote = AnnotationMiro()
        def publish(value, preview):
            with patch("liquid_tracer.plots.reviewed_plot", return_value=value):
                return sync_board(self.case, board["id"], preview, token="test", transport=remote, interval=0)
        publish(self.source(), "preview-one")
        state_path = self.case / board["state_file"]
        first = read_json(state_path)
        publish(self.source(run="two", extended=True), "preview-two")
        grown = read_json(state_path)
        for key, record in first["items"].items():
            self.assertEqual(grown["items"][key]["id"], record["id"])
        self.assertEqual(grown["namespace"]["case_id"], read_case(self.case)["case_id"] + ":" + board["id"])
        smaller = self.source(run="three")
        smaller[0]["run"]["ancestor_runs"] += ["two"]
        smaller = smaller[0], make_plan(smaller[0])
        report = publish(smaller, "preview-three")
        self.assertEqual(report["deleted"], 4)
        final = read_json(state_path)
        self.assertEqual(set(final["items"]), set(first["items"]))
        writes = len(remote.writes)
        publish(smaller, "preview-three")
        self.assertEqual(len(remote.writes), writes)
        record = next(item for item in list_boards(self.case) if item["id"] == board["id"])
        self.assertEqual((record["status"], record["preview_id"], record["run_id"]), ("synced", "preview-three", "three"))

    def test_wrong_goal_never_touches_remote(self):
        board = link_board(self.case, "pegouts", "Paths", "pegout-board")
        remote = Mock()
        with patch("liquid_tracer.plots.reviewed_plot", return_value=self.source(goal="connections")):
            with self.assertRaisesRegex(TraceError, "different goal"):
                sync_board(self.case, board["id"], "wrong-preview", token="test", transport=remote)
        remote.assert_not_called()

    def test_empty_plot_cannot_clear_or_touch_an_existing_board(self):
        board = link_board(self.case, "pegouts", "Paths", "pegout-board")
        remote = AnnotationMiro()
        with patch("liquid_tracer.plots.reviewed_plot", return_value=self.source()):
            sync_board(self.case, board["id"], "preview-one", token="test", transport=remote, interval=0)
        before_registry = self.registry.read_bytes()
        before, calls = copy.deepcopy(remote.items), len(remote.calls)
        empty, plan = self.source(run="two")
        empty["nodes"], empty["edges"] = [], []
        with patch("liquid_tracer.plots.reviewed_plot", return_value=(empty, plan)):
            with self.assertRaisesRegex(TraceError, "no matching paths"):
                sync_board(self.case, board["id"], "empty-preview", token="test", transport=remote, interval=0)
        self.assertEqual(self.registry.read_bytes(), before_registry)
        self.assertEqual(remote.items, before)
        self.assertEqual(len(remote.calls), calls)

    def test_failed_live_preflight_keeps_error_status_and_published_objects(self):
        board = link_board(self.case, "pegouts", "Paths", "pegout-board")
        remote = AnnotationMiro()
        with patch("liquid_tracer.plots.reviewed_plot", return_value=self.source(extended=True)):
            sync_board(self.case, board["id"], "preview-one", token="test", transport=remote, interval=0)
        state = read_json(self.case / board["state_file"])
        remote.items[state["items"]["addr:c"]["id"]]["data"]["content"] += " analyst note"
        before, writes = copy.deepcopy(remote.items), len(remote.writes)
        with patch("liquid_tracer.plots.reviewed_plot", return_value=self.source(run="two")):
            with self.assertRaisesRegex(TraceError, "manual"):
                sync_board(self.case, board["id"], "preview-two", token="test", transport=remote, interval=0)
        record = next(item for item in list_boards(self.case) if item["id"] == board["id"])
        self.assertEqual(record["status"], "sync_error")
        self.assertEqual(remote.items, before)
        self.assertEqual(len(remote.writes), writes)

    def test_two_boards_have_isolated_maps_and_goal_namespaces(self):
        records = [link_board(self.case, goal, goal, "managed-" + goal + "-board") for goal in ("full", "pegouts")]
        before = read_case(self.case)
        for board in records:
            remote = AnnotationMiro()
            with patch("liquid_tracer.plots.reviewed_plot", return_value=self.source(goal=board["goal"])):
                sync_board(self.case, board["id"], "preview", token="test", transport=remote, interval=0)
        first, second = [read_json(self.case / record["state_file"]) for record in records]
        self.assertNotEqual(first["namespace"], second["namespace"])
        self.assertEqual(read_case(self.case), before)

    def test_old_full_board_reuses_original_mapping_and_namespace(self):
        board = next(item for item in list_boards(self.case) if item["board_id"] == "full-board")
        remote = AnnotationMiro()
        source = self.source(goal="full")
        with patch("liquid_tracer.plots.reviewed_plot", return_value=source):
            sync_board(self.case, board["id"], "preview", token="test", transport=remote, interval=0)
        state = read_json(self.case / board["state_file"])
        self.assertEqual(state["namespace"], source[0]["namespace"])
        self.assertFalse(any(record.get("projection_proof") for record in state["items"].values()))


if __name__ == "__main__":
    unittest.main()
