"""Investigator CSV review, isolated additions and scoped removals end to end."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer import input_import
from liquid_tracer.common import TraceError, read_json
from liquid_tracer.elk_layout import optimize_graph
from liquid_tracer.investigation_boards import create_and_sync, list_boards, sync_board
from liquid_tracer.investigations import create_investigation, update_case
from liquid_tracer.plots import _query, preview_plot, reviewed_plot
from tests.test_attribution_convergence import graph_state, tx
from tests.test_board_workflow import BoardRemote
from tests.test_connections import saved_case
from tests.test_elk_layout import HAS_ELK
from tests.test_pegout_paths import add_pegout


@unittest.skipUnless(HAS_ELK, "Local ELK dependencies are required")
class BoardUpdateWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.case = create_investigation(Path(temporary.name), "Investigator revisions",
                                         seeds=[tx("a") + ":0"], run_defaults={"layout_attempts": 1})
        state = graph_state((("a:0", "b"), ("b:0", "c"), ("c:0", "d")), seeds=("a:0",))
        add_pegout(state, tx("d"))
        self.state, self.archive = saved_case(self.case, state)
        self.original_archive = {path.name: path.read_bytes() for path in self.archive.iterdir() if path.is_file()}
        self.transport = BoardRemote()
        credentials = patch.dict("os.environ", {"MIRO_ACCESS_TOKEN": "synthetic-offline-token"})
        credentials.start()
        self.addCleanup(credentials.stop)
        for name in ("liquid_tracer.api.Esplora.get", "liquid_tracer.address_counts.ensure_counts"):
            blocker = patch(name, side_effect=AssertionError("Saved-data plotting must not fetch"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def upload(self, hops, color="#123456", stop=False):
        files = [
            {"name": "attributions.csv", "policy": "replace", "text":
             "Address,Name,stop_tracing,hop_limit\nSYNTHETIC-b-address,Exchange," + str(stop).lower() + "," + str(hops) + "\n"},
            {"name": "colors.csv", "policy": "replace", "text": "Name,Color\nExchange," + color + "\n"},
            {"name": "change.csv", "policy": "replace", "text": "Txid,ChangeVout\n" + tx("b") + ",0\n"},
        ]
        review = input_import.preview_import(self.case, files)
        self.assertTrue(review["valid"], review["errors"])
        input_import.apply_import(self.case, files, approval_sha256=review["approval_sha256"])

    def publish(self, goal="full", **query):
        preview = preview_plot(self.case, goal, **query)
        result = create_and_sync(self.case, preview["preview_id"], token="synthetic-offline-token",
                                 transport=self.transport, interval=0)
        self.record = next(record for record in list_boards(self.case) if record["id"] == result["record_id"])
        self.remote = self.transport.boards[result["board_id"]]
        return result

    def mapping(self):
        return read_json(self.case / self.record["state_file"])["items"]

    def item(self, key):
        return self.remote.items[self.mapping()[key]["id"]]

    def prepare(self, goal="full", **query):
        return preview_plot(self.case, goal, layout_mode="update", board_record_id=self.record["id"],
                            token="synthetic-offline-token", transport=self.transport, interval=0, **query)

    def sync(self, preview):
        return sync_board(self.case, self.record["id"], preview["preview_id"], token="synthetic-offline-token",
                          transport=self.transport, interval=0)

    def note(self):
        note = {"id": "analyst-note", "type": "shape", "data": {"shape": "rectangle", "content": "Keep investigator note"},
                "position": {"x": 40000, "y": 300}, "geometry": {"width": 1000, "height": 500}}
        self.remote.items[note["id"]] = deepcopy(note)
        return note

    def test_csv_revision_stages_additions_then_retires_moved_out_of_scope_objects(self):
        self.upload(0)
        self.publish()
        self.item("tx:" + tx("b"))["position"].update(x=3000, y=-1500)
        note = self.note()
        initial_mapping = self.mapping()
        initial_positions = {key: deepcopy(self.remote.items[record["id"]]["position"])
                             for key, record in initial_mapping.items() if record["endpoint"] == "shapes"}
        self.upload(2, "#654321")
        before = deepcopy(self.remote.items)
        with patch("liquid_tracer.elk_layout.optimize_graph", wraps=optimize_graph) as elk:
            preview = self.prepare()
        self.assertEqual(self.remote.items, before)
        elk.assert_called_once()
        graph, _ = reviewed_plot(self.case, preview["preview_id"])
        expected_new = {node["id"] for node in graph["nodes"]} - initial_mapping.keys()
        self.assertEqual({node["id"] for node in elk.call_args.args[0]["nodes"]}, expected_new)
        result = self.sync(preview)
        self.assertGreater(result["created"], 0)
        self.assertEqual(self.remote.items[note["id"]], note)
        for key, position in initial_positions.items():
            self.assertEqual(self.item(key)["position"], position)
        for key in expected_new:
            body = self.item(key)
            self.assertGreater(body["position"]["x"] - body["geometry"]["width"] / 2, 40500)
        self.assertEqual(self.item("liquid:address:SYNTHETIC-b-address")["style"]["fillColor"], "#654321")
        self.assertIn("Change", self.item("out:" + tx("b") + ":0")["captions"][0]["content"])
        self.item("tx:" + tx("c"))["position"].update(x=-1200, y=9200)
        self.upload(0, "#654321")
        reduced = self.prepare()
        report = self.sync(reduced)
        self.assertGreater(report["deleted"], 0)
        self.assertFalse({"tx:" + tx("c"), "tx:" + tx("d")}.intersection(self.mapping()))
        self.assertEqual(self.remote.items[note["id"]], note)
        self.assertEqual(self.item("tx:" + tx("b"))["position"], initial_positions["tx:" + tx("b")])
        self.assertEqual(self.original_archive,
                         {path.name: path.read_bytes() for path in self.archive.iterdir() if path.is_file()})
        self.assertEqual(len(self.transport.creations), 1)

    def test_empty_pegout_update_retires_projection_and_preserves_unmanaged_note(self):
        self.publish("pegouts", max_hops=3)
        note = self.note()
        self.item("tx:" + tx("d"))["position"]["y"] += 2000
        self.upload(0, stop=True)
        preview = self.prepare("pegouts", max_hops=3)
        self.assertTrue(preview["empty"])
        report = self.sync(preview)
        self.assertGreater(report["deleted"], 0)
        self.assertEqual(self.mapping(), {})
        self.assertEqual(self.remote.items, {note["id"]: note})

    def test_markerless_starter_board_update_adds_complete_io_preserving_ids_and_manual_positions(self):
        from tests.test_connections_complete_workflow import complete_state, csv_rows, input_output_keys

        self.state, self.archive = saved_case(self.case, complete_state())
        before_archive = {path.name: path.read_bytes() for path in self.archive.iterdir() if path.is_file()}

        def legacy_query(*args, **kwargs):
            return _query(*args, **{**kwargs, "transaction_io": None})

        with patch("liquid_tracer.plots._query", side_effect=legacy_query):
            self.publish("connections")
        self.item("tx:" + tx("c"))["position"].update(x=3000, y=-1500)
        note = self.note()
        old_mapping = deepcopy(self.mapping())
        old_positions = {key: deepcopy(self.remote.items[record["id"]]["position"])
                         for key, record in old_mapping.items() if record["endpoint"] == "shapes"}
        update_case(self.case, {"run_defaults": {"group_context_inputs": True}})
        before = deepcopy(self.remote.items)
        with patch("liquid_tracer.elk_layout.optimize_graph", wraps=optimize_graph) as elk:
            preview = self.prepare("connections")
        self.assertEqual(self.remote.items, before)
        graph, _ = reviewed_plot(self.case, preview["preview_id"])
        self.assertEqual(graph["address_mode"], "outpoint_occurrences")
        self.assertEqual(preview["query"]["transaction_io"], "complete")
        new_nodes = {node["id"] for node in graph["nodes"]} - old_mapping.keys()
        self.assertTrue(new_nodes)
        elk.assert_called_once()
        self.assertEqual({node["id"] for node in elk.call_args.args[0]["nodes"]}, new_nodes)
        self.assertEqual({(row["Transaction Hash"], row["Direction"], row["Number of I/O"])
                          for row in csv_rows(preview)}, input_output_keys(self.state, {tx(name) for name in "acb"}))
        result = self.sync(preview)
        self.assertGreater(result["created"], 0)
        self.assertEqual(result["deleted"], 0)
        for key, original in old_mapping.items():
            self.assertEqual(self.mapping()[key]["id"], original["id"])
        for key, original in old_positions.items():
            self.assertEqual(self.item(key)["position"], original)
        self.assertEqual(self.remote.items[note["id"]], note)
        self.assertEqual(len(self.transport.creations), 1)
        self.assertEqual(before_archive,
                         {path.name: path.read_bytes() for path in self.archive.iterdir() if path.is_file()})

    def test_board_drift_after_update_review_prevents_all_writes(self):
        self.upload(0)
        self.publish()
        self.upload(2)
        preview = self.prepare()
        self.item("tx:" + tx("a"))["position"]["x"] += 100
        before = deepcopy(self.remote.items)
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "board changed"):
            self.sync(preview)
        self.assertEqual(self.remote.items, before)
        self.assertEqual(len(self.remote.writes), writes)
