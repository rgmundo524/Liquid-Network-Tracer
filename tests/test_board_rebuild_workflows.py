"""Fresh-board publication through real saved evidence, ELK and sync journals."""

import contextlib
import copy
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from liquid_tracer.board_rebuild import rebuild_board, rebuild_status
from liquid_tracer.cli import main, sync_run
from liquid_tracer.common import TraceError, canonical, digest, read_json, save_json
from liquid_tracer.investigations import create_investigation, read_case, update_case
from liquid_tracer.miro import sync, sync_frames
from liquid_tracer.services import set_service
from tests.fixtures import A, C, D, X, fixture, output
from tests.test_elk_layout import HAS_ELK
from tests.test_miro_frame_sync import FrameMiro


class RebuildMiro:
    """Board-scoped fake HTTP service, including definite and uncertain failures."""

    OLD = "SYNTHETIC-OLD="

    def __init__(self):
        self.boards = {self.OLD: FrameMiro()}
        self.calls = []
        self.created_boards = []
        self.reject_connector = False
        self.uncertain_creation = False
        self.lock = threading.RLock()

    def __call__(self, method, url, headers, body, timeout):
        with self.lock:
            path = urlsplit(url).path
            payload = json.loads(body) if body else None
            self.calls.append((method, path, copy.deepcopy(payload)))
            if path == "/v2/boards":
                if method != "POST":
                    raise AssertionError("Unexpected board request")
                target = "SYNTHETIC-NEW-" + str(len(self.created_boards) + 1) + "="
                remote = self.boards[target] = FrameMiro()
                remote.counter = 1000 * (len(self.created_boards) + 1)
                self.created_boards.append(target)
                if self.uncertain_creation:
                    self.uncertain_creation = False
                    return 503, {}, b"{}"
                return 201, {}, canonical({"id": target, "name": payload["name"]})
            target = unquote(path.split("/")[3])
            if target not in self.boards:
                raise AssertionError("Graph request for an unknown board: " + target)
            if method == "POST" and path.endswith("/connectors") and self.reject_connector:
                self.reject_connector = False
                return 403, {}, b"{}"
            return self.boards[target](method, url, headers, body, timeout)


@unittest.skipUnless(HAS_ELK, "Run liquid-layout-setup to install the pinned local ELK engine")
class BoardRebuildWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "fixture.json"
        data = fixture()
        # The second run discovers two independent context inputs at C.
        data["/tx/" + C]["vin"].append({
            "txid": X, "vout": 2, "prevout": output("SYNTHETIC-other-coinput"),
            "is_coinbase": False, "is_pegin": False,
        })
        save_json(self.source, data)
        self.case = create_investigation(self.root, "Fresh board workflow", fixture=str(self.source))
        self.first_run = self.trace("--seed", A + ":0", "--hops", "1")
        self.run = self.trace("--resume", self.first_run, "--additional-hops", "1")
        self.archive = self.case / "runs" / self.run
        update_case(self.case, {"miro_board": RebuildMiro.OLD,
                               "run_defaults": {"layout_attempts": 2, "connector_style": "elbowed",
                                                "group_context_inputs": True, "include_fees": False}})
        self.remote = RebuildMiro()
        self.old_path = self.mapping_path(RebuildMiro.OLD)
        old_plan = read_json(self.case / "runs" / self.first_run / "miro-plan.json")
        sync(old_plan, RebuildMiro.OLD, self.old_path, token="test-token",
             transport=self.remote, interval=0)
        sync_frames(old_plan, RebuildMiro.OLD, self.old_path, token="test-token",
                    transport=self.remote, interval=0)
        self.remote.boards[RebuildMiro.OLD].items["analyst-note"] = {
            "id": "analyst-note", "type": "sticky_note", "data": {"content": "Keep this note"}}
        self.old_items = copy.deepcopy(self.remote.boards[RebuildMiro.OLD].items)
        self.old_state = self.old_path.read_bytes()
        self.old_calls = copy.deepcopy(self.remote.boards[RebuildMiro.OLD].calls)
        self.evidence = self.snapshot(self.case / "runs")
        self.before_calls = len(self.remote.calls)
        environment = patch.dict(os.environ, {"MIRO_ACCESS_TOKEN": "synthetic-token"})
        environment.start()
        self.addCleanup(environment.stop)

    @staticmethod
    def snapshot(directory):
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}

    def trace(self, *options):
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(io.StringIO()):
            status = main(["trace", "--case", str(self.case), "--fixture", str(self.source), *options])
        self.assertEqual(status, 0, captured.getvalue())
        return json.loads(captured.getvalue())["run_id"]

    def mapping_path(self, board):
        return self.case / "miro" / (digest(board.encode())[:24] + ".json")

    def rebuild(self, **options):
        return rebuild_board(self.case, source_board=options.pop("source_board", RebuildMiro.OLD),
                             transport=self.remote, **options)

    def assert_old_board_and_evidence_unchanged(self):
        self.assertEqual(self.remote.boards[RebuildMiro.OLD].items, self.old_items)
        self.assertEqual(self.remote.boards[RebuildMiro.OLD].calls, self.old_calls)
        self.assertEqual(self.old_path.read_bytes(), self.old_state)
        self.assertEqual(self.snapshot(self.case / "runs"), self.evidence)

    def assert_no_automatic_frames(self):
        self.assertFalse(any(path.endswith("/frames") for _, path, _ in self.remote.calls[self.before_calls:]))

    def test_rebuild_uses_current_presentation_and_independent_mapping_without_touching_old_board(self):
        set_service(self.case, "SYNTHETIC-branch-A", name="Updated service", confidence="confirmed",
                    stop_tracing=False, source="Synthetic review")
        result = self.rebuild(run_id=self.run)
        self.assertEqual(result["previous_board_id"], RebuildMiro.OLD)
        self.assertEqual(result["rebuild_status"], "complete")
        self.assertEqual(read_case(self.case)["miro_board"], result["board_id"])
        current = read_json(self.mapping_path(result["board_id"]))
        plan = current["frame_plan"]
        self.assertEqual(current["latest_run_id"], self.run)
        self.assertEqual(plan["graph_options"]["layout_attempts"], 2)
        self.assertTrue(plan["graph_options"]["group_context_inputs"])
        self.assertTrue(plan["context_group_items"])
        self.assertFalse(plan["include_fees"])
        self.assertTrue(any("Updated service" in shape["body"]["data"]["content"]
                            for shape in plan["shapes"]))
        self.assertTrue(any(edge["body"]["shape"] == "elbowed" for edge in plan["connectors"]))
        self.assertIn("tx:" + C, current["items"])
        self.assertNotIn("tx:" + D, current["items"])
        self.assertNotIn("run:" + self.first_run, current["items"])
        self.assertEqual(set(current["items"]), {item["key"] for item in plan["shapes"] + plan["connectors"]})
        old_ids = {item["id"] for item in read_json(self.old_path)["items"].values()}
        self.assertTrue(old_ids.isdisjoint(item["id"] for item in current["items"].values()))
        request = next(body for method, path, body in self.remote.calls if path == "/v2/boards")
        self.assertEqual(request["policy"]["sharingPolicy"], {
            "access": "private", "organizationAccess": "private", "teamAccess": "private"})
        self.assert_old_board_and_evidence_unchanged()
        self.assert_no_automatic_frames()

    def test_layout_and_item_budget_fail_before_board_creation(self):
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=TraceError("Synthetic layout failure")):
            with self.assertRaisesRegex(TraceError, "Synthetic layout failure"):
                self.rebuild(run_id=self.run)
        with self.assertRaisesRegex(TraceError, "max-items|item.*budget"):
            self.rebuild(run_id=self.run, max_new_items=1)
        self.assertEqual(self.remote.calls[self.before_calls:], [])
        self.assertEqual(self.remote.created_boards, [])
        self.assertEqual(read_case(self.case)["miro_board"], RebuildMiro.OLD)
        self.assert_old_board_and_evidence_unchanged()

    def test_partial_sync_resumes_same_board_and_frozen_plan_after_settings_change(self):
        self.remote.reject_connector = True
        with self.assertRaisesRegex(TraceError, "403"):
            self.rebuild(run_id="latest")
        self.assertEqual(len(self.remote.created_boards), 1)
        target = self.remote.created_boards[0]
        initial = read_json(self.mapping_path(target))
        existing_shapes = {key: record["id"] for key, record in initial["items"].items()
                           if record["endpoint"] == "shapes"}
        self.assertTrue(existing_shapes)
        self.assertFalse(initial["pending_creations"])
        set_service(self.case, "SYNTHETIC-branch-A", name="Changed after interruption",
                    confidence="confirmed", stop_tracing=False)
        settings = read_case(self.case)["run_defaults"]
        update_case(self.case, {"run_defaults": {**settings, "layout_attempts": 3,
                                                "group_context_inputs": False}})
        self.trace("--resume", self.run, "--additional-hops", "1")
        self.evidence = self.snapshot(self.case / "runs")
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=AssertionError("Resume must use prepared layout")):
            result = self.rebuild(run_id="latest")
        self.assertEqual(result["board_id"], target)
        self.assertEqual(result["run_id"], self.run)
        self.assertEqual(len(self.remote.created_boards), 1)
        completed = read_json(self.mapping_path(target))
        self.assertEqual({key: completed["items"][key]["id"] for key in existing_shapes}, existing_shapes)
        plan = completed["frame_plan"]
        self.assertEqual(plan["graph_options"]["layout_attempts"], 2)
        self.assertTrue(plan["context_group_items"])
        self.assertFalse(any("Changed after interruption" in item["body"]["data"]["content"]
                             for item in plan["shapes"]))
        self.assertNotIn("tx:" + D, completed["items"])
        self.assertEqual(len(self.remote.boards[target].items), len(completed["items"]))
        self.assert_old_board_and_evidence_unchanged()
        self.assert_no_automatic_frames()

    def test_unconfirmed_board_creation_never_reposts_or_switches_existing_board(self):
        self.remote.uncertain_creation = True
        with self.assertRaisesRegex(TraceError, "uncertain|confirm"):
            self.rebuild(run_id=self.run)
        calls = len(self.remote.calls)
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=AssertionError("Pending creation must stop early")):
            with self.assertRaisesRegex(TraceError, "uncertain|confirm"):
                self.rebuild(run_id=self.run)
        self.assertEqual(len(self.remote.calls), calls)
        self.assertEqual(len(self.remote.created_boards), 1)
        self.assertFalse(self.remote.boards[self.remote.created_boards[0]].items)
        self.assertEqual(read_case(self.case)["miro_board"], RebuildMiro.OLD)
        self.assert_old_board_and_evidence_unchanged()

    def test_normal_sync_with_updated_presentation_completes_interrupted_rebuild(self):
        self.remote.reject_connector = True
        with self.assertRaisesRegex(TraceError, "403"):
            self.rebuild(run_id=self.run)
        target = self.remote.created_boards[0]
        receipt_path = next((self.case / "miro" / "rebuilds").glob("*/receipt.json"))
        interrupted = read_json(receipt_path)
        self.assertEqual(rebuild_status(self.case)["status"], "syncing")
        set_service(self.case, "SYNTHETIC-branch-A", name="Reviewed after interruption",
                    confidence="confirmed", stop_tracing=False)
        settings = read_case(self.case)["run_defaults"]
        update_case(self.case, {"run_defaults": {**settings, "layout_attempts": 3}})

        def simulated_sync(*args, **kwargs):
            return sync(*args, **{**kwargs, "transport": self.remote, "interval": 0})

        with patch("liquid_tracer.cli.sync", side_effect=simulated_sync):
            result = sync_run(self.case, self.run)
        self.assertNotEqual(result["plan_sha256"], interrupted["plan_sha256"])
        self.assertEqual(result["board_id"], target)
        completed = read_json(self.mapping_path(target))
        self.assertNotIn(interrupted["plan_sha256"], completed["runs"][self.run]["plan_sha256s"])
        self.assertEqual(completed["frame_plan"]["graph_options"]["layout_attempts"], 3)
        self.assertTrue(any("Reviewed after interruption" in item["body"]["data"]["content"]
                            for item in completed["frame_plan"]["shapes"]))
        before_status = receipt_path.read_bytes()
        self.assertEqual(rebuild_status(self.case)["status"], "complete")
        self.assertEqual(receipt_path.read_bytes(), before_status)

        later = self.trace("--resume", self.run, "--additional-hops", "1")
        self.evidence = self.snapshot(self.case / "runs")
        with patch("liquid_tracer.cli.sync", side_effect=simulated_sync):
            sync_run(self.case, later, reorganize=True)
        self.assertEqual(read_json(self.mapping_path(target))["latest_run_id"], later)
        self.assertEqual(rebuild_status(self.case)["status"], "complete")
        state = self.mapping_path(target).read_bytes()
        contents = copy.deepcopy(self.remote.boards[target].items)
        next_board = self.rebuild(source_board=target)
        self.assertNotEqual(next_board["board_id"], target)
        self.assertEqual(next_board["run_id"], later)
        self.assertEqual(next_board["previous_board_id"], target)
        self.assertEqual(len(self.remote.created_boards), 2)
        self.assertEqual(self.mapping_path(target).read_bytes(), state)
        self.assertEqual(self.remote.boards[target].items, contents)
        self.assert_old_board_and_evidence_unchanged()
        self.assert_no_automatic_frames()

    def test_completed_retry_reuses_board_and_new_source_allows_another_rebuild(self):
        first = self.rebuild(run_id=self.run)
        target = first["board_id"]
        state = self.mapping_path(target).read_bytes()
        contents = copy.deepcopy(self.remote.boards[target].items)
        calls = len(self.remote.calls)
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=AssertionError("Completed retry must not re-layout")):
            again = self.rebuild(run_id=self.run)
        self.assertEqual(again["board_id"], target)
        self.assertEqual(len(self.remote.calls), calls)
        next_board = self.rebuild(run_id=self.run, source_board=target)
        self.assertNotEqual(next_board["board_id"], target)
        self.assertEqual(next_board["previous_board_id"], target)
        self.assertEqual(len(self.remote.created_boards), 2)
        self.assertEqual(self.mapping_path(target).read_bytes(), state)
        self.assertEqual(self.remote.boards[target].items, contents)
        self.assert_old_board_and_evidence_unchanged()
        self.assert_no_automatic_frames()


if __name__ == "__main__":
    unittest.main()
