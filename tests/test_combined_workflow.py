"""Combined actions fail locally before ELK and preserve existing recovery."""

import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.investigation_boards import create_and_sync, generate_and_sync, list_boards
from liquid_tracer.plots import list_plots
from liquid_tracer.services import set_service
from tests import test_board_update_workflow


class CombinedWorkflowTests(unittest.TestCase):
    def setUp(self):
        test_board_update_workflow.BoardUpdateWorkflowTests.setUp(self)
        layout = patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph)
        self.layout = layout.start()
        self.addCleanup(layout.stop)

    def combined(self, goal="full", **kwargs):
        return generate_and_sync(self.case, goal, token=kwargs.pop("token", "synthetic-token"),
                                 transport=self.transport, interval=kwargs.pop("interval", 0),
                                 workers=kwargs.pop("workers", 1), **kwargs)

    def test_invalid_publication_options_fail_before_layout_and_writes(self):
        for kwargs in ({"max_items": -1}, {"max_items": True}, {"max_items": 1.5},
                       {"interval": float("nan")}, {"interval": True},
                       {"workers": 0}, {"workers": True}, {"workers": 5},
                       {"name": ""}, {"name": "bad\nname"}, {"team_id": "invalid/team"},
                       {"layout_mode": "replace"}, {"board_record_id": "not-a-target"},
                       {"token": "bad token"}, {"progress": "not-a-callback"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(TraceError):
                self.combined(**kwargs)
        self.layout.assert_not_called()
        self.assertEqual(self.transport.creations, [])
        self.assertEqual(list_plots(self.case), [])
        self.assertEqual(list_boards(self.case), [])

    def test_fresh_budget_rejects_before_elk_or_saving_a_preview(self):
        with self.assertRaisesRegex(TraceError, "above max-items=1"):
            self.combined(max_items=1)
        self.layout.assert_not_called()
        self.assertEqual(self.transport.creations, [])
        self.assertEqual(list_plots(self.case), [])

    def test_update_budget_counts_only_additions_and_checks_before_inventory(self):
        set_service(self.case, "SYNTHETIC-b-address", name="Boundary", stop_tracing=False, hop_limit=0)
        initial = self.combined()
        remote = self.transport.boards[initial["board_id"]]
        self.layout.reset_mock()
        same = self.combined(layout_mode="update", board_record_id=initial["record_id"], max_items=0)
        self.assertTrue(same["published"])
        self.layout.assert_not_called()
        set_service(self.case, "SYNTHETIC-b-address", name="Boundary", stop_tracing=False, hop_limit=2)
        remote.calls.clear()
        with self.assertRaisesRegex(TraceError, "above max-items=1"):
            self.combined(layout_mode="update", board_record_id=initial["record_id"], max_items=1)
        self.layout.assert_not_called()
        self.assertEqual(remote.calls, [])
        self.assertEqual(len(list_plots(self.case)), 2)
        self.assertEqual(len(self.transport.creations), 1)

    def test_uncertain_first_creation_blocks_new_generation_and_blind_creation_retry(self):
        calls = []

        def uncertain(*args):
            calls.append(args[:2])
            return 503, {}, b"{}"

        with self.assertRaisesRegex(TraceError, "uncertain"):
            generate_and_sync(self.case, "full", token="test", transport=uncertain, interval=0)
        record, = list_boards(self.case)
        self.assertTrue(record["preview_id"])
        self.assertEqual(record["preview_id"], record["creation_preview_id"])
        self.layout.reset_mock()
        with self.assertRaisesRegex(TraceError, "outcome is uncertain"):
            self.combined()
        with self.assertRaisesRegex(TraceError, "uncertain"):
            create_and_sync(self.case, record["preview_id"], token="test", transport=uncertain, interval=0)
        self.layout.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(list_plots(self.case)), 1)

    def test_acknowledged_board_sync_failure_resumes_without_regeneration(self):
        with patch("liquid_tracer.investigation_boards.sync_board", side_effect=TraceError("Synthetic failure")):
            with self.assertRaisesRegex(TraceError, "Synthetic failure"):
                self.combined()
        record, = list_boards(self.case)
        self.layout.reset_mock()
        recovered = create_and_sync(self.case, record["preview_id"], token="test", transport=self.transport, interval=0)
        self.assertEqual(recovered["record_id"], record["id"])
        self.layout.assert_not_called()
        self.assertEqual(len(self.transport.creations), 1)

    def test_later_update_failure_does_not_prevent_a_separate_new_board(self):
        initial = self.combined()
        registry_path = self.case / "miro" / "boards.json"
        registry = read_json(registry_path)
        record, = registry["boards"]
        record.update(status="sync_error", preview_id=initial["preview_id"][:-8] + "abcdefab",
                      sync_plan_sha256="synthetic-later-plan")
        save_json(registry_path, registry)
        another = self.combined(name="Separate new board")
        self.assertNotEqual(another["board_id"], initial["board_id"])
        self.assertEqual(len(self.transport.creations), 2)
        self.assertEqual(len(list_plots(self.case)), 2)

    def test_empty_update_retires_managed_items_in_the_same_action(self):
        initial = self.combined("pegouts")
        remote = self.transport.boards[initial["board_id"]]
        self.assertTrue(remote.items)
        set_service(self.case, "SYNTHETIC-b-address", name="Boundary", stop_tracing=True)
        result = self.combined("pegouts", layout_mode="update", board_record_id=initial["record_id"], max_items=0)
        self.assertTrue(result["empty"])
        self.assertTrue(result["published"])
        self.assertGreater(result["deleted"], 0)
        self.assertEqual(remote.items, {})
        self.assertEqual(len(self.transport.creations), 1)

    def test_update_rejects_creation_options_before_layout_or_remote_reads(self):
        initial = self.combined()
        remote = self.transport.boards[initial["board_id"]]
        remote.calls.clear()
        self.layout.reset_mock()
        for kwargs in ({"name": "Unexpected rename"}, {"team_id": "new-team"}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(TraceError, "only when creating"):
                self.combined(layout_mode="update", board_record_id=initial["record_id"], **kwargs)
        self.layout.assert_not_called()
        self.assertEqual(remote.calls, [])


if __name__ == "__main__":
    unittest.main()
