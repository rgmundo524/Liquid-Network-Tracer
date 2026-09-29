"""Combined plotting and publication uses durable plans and existing recovery."""

from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError, read_json
from liquid_tracer.elk_layout import optimize_graph
from liquid_tracer.investigation_boards import create_and_sync, list_boards
from liquid_tracer.plots import list_plots, preview_plot, reviewed_plot
from liquid_tracer.services import set_service
from tests.test_attribution_convergence import tx
from tests import test_board_update_workflow as board_fixtures
from tests.test_elk_layout import HAS_ELK


@unittest.skipUnless(HAS_ELK, "Local ELK dependencies are required")
class CombinedLifecycleTests(unittest.TestCase):
    setUp = board_fixtures.BoardUpdateWorkflowTests.setUp
    upload = board_fixtures.BoardUpdateWorkflowTests.upload
    mapping = board_fixtures.BoardUpdateWorkflowTests.mapping
    item = board_fixtures.BoardUpdateWorkflowTests.item
    note = board_fixtures.BoardUpdateWorkflowTests.note

    def combined(self, goal="full", **kwargs):
        from liquid_tracer.investigation_boards import generate_and_sync
        return generate_and_sync(self.case, goal, token="synthetic-offline-token",
                                 transport=kwargs.pop("transport", self.transport), interval=0,
                                 workers=1, **kwargs)

    def select_board(self, result):
        self.record = next(item for item in list_boards(self.case) if item["id"] == result["record_id"])
        self.remote = self.transport.boards[result["board_id"]]

    def test_fresh_snapshot_is_complete_and_bound_before_the_first_remote_write(self):
        observations = []

        def transport(method, url, headers, body, timeout):
            if method == "POST" and url == "https://api.miro.com/v2/boards":
                plots = list_plots(self.case)
                self.assertEqual(len(plots), 1)
                preview_id = plots[0]["preview_id"]
                graph, plan = reviewed_plot(self.case, preview_id)
                self.assertTrue({node["id"] for node in graph["nodes"]}
                                <= {shape["key"] for shape in plan["shapes"]})
                record, = list_boards(self.case)
                self.assertEqual(record["preview_id"], preview_id)
                self.assertEqual(record["creation_preview_id"], preview_id)
                self.assertEqual(record["run_id"], graph["run_id"])
                self.assertEqual(record["status"], "pending_creation")
                observations.append(preview_id)
            return self.transport(method, url, headers, body, timeout)

        result = self.combined(name="Investigator working graph", transport=transport)
        self.assertEqual(observations, [result["preview_id"]])
        self.assertTrue(result["published"])
        self.assertTrue(result["created_board"])
        self.assertEqual(result["status"], "synced")
        self.assertTrue(Path(result["directory"]).joinpath("SHA256SUMS").is_file())
        self.assertEqual(self.original_archive,
                         {path.name: path.read_bytes() for path in self.archive.iterdir() if path.is_file()})

    def test_empty_fresh_saves_result_without_creating_a_board_or_running_elk(self):
        with patch("liquid_tracer.elk_layout.optimize_graph", wraps=optimize_graph) as elk:
            result = self.combined("pegouts", max_hops=0, name="No matching paths")
        self.assertTrue(result["empty"])
        self.assertFalse(result["published"])
        self.assertFalse(result["created_board"])
        self.assertEqual(result["status"], "empty")
        self.assertEqual(list_boards(self.case), [])
        self.assertEqual(self.transport.creations, [])
        self.assertEqual(len(list_plots(self.case)), 1)
        reviewed_plot(self.case, result["preview_id"])
        elk.assert_not_called()

    def test_update_only_lays_out_additions_and_preserves_investigator_organization(self):
        self.upload(0)
        self.select_board(self.combined(name="Working graph"))
        self.item("tx:" + tx("b"))["position"].update(x=4000, y=-2000)
        note = self.note()
        positions = {key: deepcopy(self.remote.items[record["id"]]["position"])
                     for key, record in self.mapping().items() if record["endpoint"] == "shapes"}
        old_keys = set(self.mapping())
        self.upload(2, "#654321")
        with patch("liquid_tracer.elk_layout.optimize_graph", wraps=optimize_graph) as elk:
            result = self.combined(layout_mode="update", board_record_id=self.record["id"])
        graph, _ = reviewed_plot(self.case, result["preview_id"])
        new_keys = {node["id"] for node in graph["nodes"]} - old_keys
        self.assertTrue(result["published"])
        elk.assert_called_once()
        self.assertEqual({node["id"] for node in elk.call_args.args[0]["nodes"]}, new_keys)
        for key, position in positions.items():
            self.assertEqual(self.item(key)["position"], position)
        for key in new_keys:
            item = self.item(key)
            self.assertGreater(item["position"]["x"] - item["geometry"]["width"] / 2, 40500)
        self.assertEqual(self.remote.items[note["id"]], note)
        self.assertEqual(len(self.transport.creations), 1)

    def test_csv_change_between_layout_and_publication_prevents_board_creation(self):
        def changed_after_layout(*args, **kwargs):
            result = preview_plot(*args, **kwargs)
            set_service(self.case, "SYNTHETIC-b-address", name="Revised", stop_tracing=True)
            return result

        with patch("liquid_tracer.plots.preview_plot", side_effect=changed_after_layout):
            with self.assertRaisesRegex(TraceError, "changed|regenerate"):
                self.combined(name="Stale graph")
        self.assertEqual(self.transport.creations, [])
        self.assertEqual(list_boards(self.case), [])

    def test_live_movement_between_layout_and_update_prevents_all_remote_writes(self):
        self.upload(0)
        self.select_board(self.combined(name="Working graph"))
        self.upload(2)
        writes_before = len(self.remote.writes)

        def changed_after_layout(*args, **kwargs):
            result = preview_plot(*args, **kwargs)
            self.item("tx:" + tx("a"))["position"]["x"] += 500
            return result

        with patch("liquid_tracer.plots.preview_plot", side_effect=changed_after_layout):
            with self.assertRaisesRegex(TraceError, "board changed"):
                self.combined(layout_mode="update", board_record_id=self.record["id"])
        self.assertEqual(len(self.remote.writes), writes_before)
        self.assertEqual(len(self.transport.creations), 1)

    def test_partial_publication_resumes_the_saved_plan_without_elk_or_duplicate_board(self):
        rejected = False

        def transport(method, url, headers, body, timeout):
            nonlocal rejected
            if method == "POST" and urlsplit(url).path.endswith("/connectors") and not rejected:
                rejected = True
                return 400, {}, b"{}"
            return self.transport(method, url, headers, body, timeout)

        with self.assertRaisesRegex(TraceError, "HTTP 400"):
            self.combined(name="Interrupted graph", transport=transport)
        record, = list_boards(self.case)
        self.assertTrue(record["preview_id"])
        self.assertEqual(record["preview_id"], record["creation_preview_id"])
        saved_ids = {key: item["id"] for key, item in read_json(self.case / record["state_file"])["items"].items()}
        self.assertTrue(saved_ids)
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=AssertionError("Recovery must reuse saved layout")):
            resumed = create_and_sync(self.case, record["preview_id"], token="synthetic-offline-token",
                                      transport=self.transport, interval=0, workers=1)
        self.assertEqual(resumed["record_id"], record["id"])
        self.assertEqual(len(self.transport.creations), 1)
        mapping = read_json(self.case / record["state_file"])["items"]
        for key, identity in saved_ids.items():
            self.assertEqual(mapping[key]["id"], identity)
        self.assertEqual(len(self.transport.boards[record["board_id"]].items), len(mapping))

    def test_acknowledged_failed_board_does_not_block_explicit_fresh_plot_after_csv_revision(self):
        def transport(method, url, headers, body, timeout):
            if method == "POST" and urlsplit(url).path.endswith("/connectors"):
                return 400, {}, b"{}"
            return self.transport(method, url, headers, body, timeout)

        with self.assertRaisesRegex(TraceError, "HTTP 400"):
            self.combined(name="Earlier graph", transport=transport)
        previous, = list_boards(self.case)
        saved_items = deepcopy(self.transport.boards[previous["board_id"]].items)
        set_service(self.case, "SYNTHETIC-b-address", name="Changed scope", stop_tracing=True)
        with self.assertRaisesRegex(TraceError, "changed|regenerate"):
            reviewed_plot(self.case, previous["preview_id"])
        result = self.combined(name="Revised graph")
        self.assertTrue(result["published"])
        self.assertNotEqual(result["board_id"], previous["board_id"])
        self.assertEqual(len(self.transport.creations), 2)
        self.assertEqual(self.transport.boards[previous["board_id"]].items, saved_items)


if __name__ == "__main__":
    unittest.main()
