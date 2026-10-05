"""Zero removes Miro's optional item cap, not validation or publication safety."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError, read_json
from liquid_tracer.connections import publish_connections
from liquid_tracer.investigation_boards import _publication_budget, create_and_sync, list_boards
from liquid_tracer.investigations import create_investigation, read_case
from liquid_tracer.miro import make_plan, publish, sync, sync_frames
from liquid_tracer.pegouts import publish_pegouts
from tests.test_board_workflow import BoardRemote
from tests.test_miro_frame_sync import FrameMiro, framed_graph
from tests.test_miro_sync import FakeMiro, graph


def large_graph(case_id=None, count=751):
    value = graph()
    prototype = value["nodes"][0]
    value["nodes"] = [{**copy.deepcopy(prototype), "id": "address:" + str(index),
                       "label": "Synthetic address " + str(index), "x": index * 250}
                      for index in range(count)]
    value["edges"] = []
    if case_id is not None:
        value["namespace"]["case_id"] = case_id
    return value


class UnlimitedMiroItemsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "mapping.json"

    def test_large_dry_run_and_early_board_preflight_accept_zero_and_default(self):
        value = large_graph()
        plan = make_plan(value)
        for options in ({}, {"max_items": 0}):
            report = sync(plan, "board", self.path, dry_run=True, **options)
            self.assertGreater(report["new_items"], 750)
            self.assertEqual(report["max_items"], 0)
        _publication_budget(self.root, value, None, 0)
        self.assertFalse(self.path.exists())
        with self.assertRaisesRegex(TraceError, "above max-items=750"):
            sync(plan, "board", self.path, max_items=750, dry_run=True)
        with self.assertRaisesRegex(TraceError, "above max-items=750"):
            _publication_budget(self.root, value, None, 750)

    def test_registered_board_creation_accepts_more_than_old_default_and_keeps_finite_preflight(self):
        case = create_investigation(self.root, "Large board")
        value = large_graph(read_case(case)["case_id"])
        value["plot"] = {"goal": "full", "layout_mode": "fresh"}
        plan = make_plan(value)
        remote = BoardRemote()
        with patch("liquid_tracer.plots.reviewed_plot", return_value=(value, plan)):
            with self.assertRaisesRegex(TraceError, "above max-items=750"):
                create_and_sync(case, "saved-review", max_items=750, token="test", transport=remote, interval=0)
            self.assertEqual(remote.creations, [])
            result = create_and_sync(case, "saved-review", token="test", transport=remote, interval=0)
            self.assertTrue(result["created_board"])
            self.assertGreater(result["created"], 750)
            self.assertEqual(len(remote.creations), 1)
            repeated = create_and_sync(case, "saved-review", max_items=0, token="test", transport=remote, interval=0)
            self.assertTrue(repeated["reused_board"])
            self.assertEqual(len(remote.creations), 1)
        self.assertEqual(len(list_boards(case)), 1)

    def test_update_zero_adds_objects_while_finite_cap_rejects_without_remote_calls(self):
        remote = FakeMiro()
        sync(make_plan(graph()), "board", self.path, token="test", transport=remote, interval=0)
        before = len(remote.calls)
        plan = make_plan(graph("two", extended=True))
        with self.assertRaisesRegex(TraceError, "4 new items"):
            sync(plan, "board", self.path, max_items=3, token="test", transport=remote, interval=0)
        self.assertEqual(len(remote.calls), before)
        result = sync(plan, "board", self.path, max_items=0, token="test", transport=remote, interval=0)
        self.assertEqual(result["created"], 4)

    def test_frame_creation_zero_is_unlimited_and_finite_limit_still_preflights(self):
        remote = FrameMiro()
        plan = make_plan(framed_graph())
        sync(plan, "board", self.path, token="test", transport=remote, interval=0)
        before = len(remote.calls)
        with self.assertRaisesRegex(TraceError, "4 new items"):
            sync_frames(plan, "board", self.path, max_items=1, token="test", transport=remote, interval=0)
        self.assertEqual(len(remote.calls), before)
        result = sync_frames(plan, "board", self.path, token="test", transport=remote, interval=0)
        self.assertEqual(result["new_frames"], 4)
        self.assertEqual(result["created"], 4)

    def test_legacy_large_publication_accepts_zero_and_resumes_without_duplicates(self):
        remote = FakeMiro()
        plan = make_plan(large_graph())
        with self.assertRaisesRegex(TraceError, "above max-items=750"):
            publish(plan, "board", self.path, max_items=750, token="test", transport=remote, interval=0)
        self.assertEqual(remote.calls, [])
        result = publish(plan, "board", self.path, token="test", transport=remote, interval=0)
        self.assertGreater(result["items"], 750)
        writes = len(remote.writes)
        publish(plan, "board", self.path, max_items=0, token="test", transport=remote, interval=0)
        self.assertEqual(len(remote.writes), writes)

    def test_unlimited_preserves_plan_ownership_recovery_and_budget_type_checks(self):
        remote = FakeMiro()
        plan = make_plan(graph())
        for value in (True, False, -1, 0.0, 1.5, "0", None):
            for operation in (sync, sync_frames, publish):
                with self.subTest(value=value, operation=operation.__name__), self.assertRaises(TraceError):
                    operation(plan, "board", self.path, max_items=value, token="test", transport=remote, interval=0)
        self.assertEqual(remote.calls, [])
        corrupt = copy.deepcopy(plan)
        corrupt["shapes"][0]["body"]["position"]["x"] += 1
        with self.assertRaisesRegex(TraceError, "checksum"):
            sync(corrupt, "board", self.path, max_items=0, token="test", transport=remote, interval=0)
        remote.lose_next_post = True
        with self.assertRaisesRegex(TraceError, "lost response"):
            sync(plan, "board", self.path, max_items=0, token="test", transport=remote, interval=0)
        self.assertTrue(read_json(self.path)["pending_creations"])
        before = len(remote.calls)
        with self.assertRaisesRegex(TraceError, "outcome is uncertain"):
            sync(plan, "board", self.path, max_items=0, token="test", transport=remote, interval=0)
        self.assertEqual(len(remote.calls), before)

    def test_snapshot_publication_wrappers_default_to_unlimited_and_keep_main_board_protected(self):
        case = create_investigation(self.root, "Snapshots", board="main-board")
        value, plan = graph(), make_plan(graph())
        for operation, review in ((publish_connections, "liquid_tracer.connections.reviewed_connections"),
                                  (publish_pegouts, "liquid_tracer.pegouts.reviewed_pegouts")):
            with self.subTest(operation=operation.__name__), patch(review, return_value=(value, plan)), \
                    patch("liquid_tracer.miro.publish", return_value={"items": 6}) as writer:
                with self.assertRaisesRegex(TraceError, "protected"):
                    operation(case, "review", "main-board")
                writer.assert_not_called()
                operation(case, "review", "other-board")
                self.assertEqual(writer.call_args.kwargs["max_items"], 0)
                operation(case, "review", "other-board", max_items=50)
                self.assertEqual(writer.call_args.kwargs["max_items"], 50)


if __name__ == "__main__":
    unittest.main()
