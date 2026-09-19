"""Current change selections travel from saved evidence through ELK to Miro."""

import contextlib
import functools
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.change_outputs import set_change_output
from liquid_tracer.cli import (compact_preview_run, layout_preview_run, main,
                               saved_graph, sync_run, verified_compaction_preview)
from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.elk_layout import optimize_graph
from liquid_tracer.layout_reuse import reusable_elk_preview
from liquid_tracer.miro import sync as real_sync
from tests.fixtures import A, B, fixture
from tests.test_miro_sync import FakeMiro


class ChangeWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = self.root / "case"
        self.fixture = self.root / "fixture.json"
        save_json(self.fixture, fixture())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["trace", "--case", str(self.case), "--fixture", str(self.fixture),
                                   "--seed", A + ":0", "--hops", "1"]), 0)
        self.archive = {str(p): p.read_bytes() for p in (self.case / "runs").rglob("*") if p.is_file()}

    def assert_rows(self, graph):
        nodes = {node["id"]: node for node in graph["nodes"]}
        outputs = [edge for edge in graph["edges"] if edge["source"] == "tx:" + A]
        chosen = next(edge for edge in outputs if edge["outpoint"] == A + ":0")
        parent_y = nodes["tx:" + A]["y"]
        self.assertEqual(nodes[chosen["target"]]["y"], parent_y)
        self.assertEqual(nodes["tx:" + B]["y"], parent_y)
        for edge in outputs:
            if edge is not chosen:
                self.assertGreater(nodes[edge["target"]]["y"], parent_y)
        self.assertEqual(len(graph["layout"]["change_outputs"]["applied"]), 1)
        self.assertEqual(graph["layout"]["change_outputs"]["skipped"], [])
        return chosen["target"]

    def test_preview_compaction_and_miro_preserve_rows_without_mutating_evidence(self):
        remote = FakeMiro()
        adapter = functools.partial(real_sync, token="SYNTHETIC-token", transport=remote, interval=0)
        baseline = optimize_graph(saved_graph(self.case)[2])
        with patch("liquid_tracer.cli.sync", adapter):
            initial = sync_run(self.case, "latest", "SYNTHETIC-change-board")
            mapping = read_json(initial["state_file"])["items"]
            live_parent = remote.items[mapping["tx:" + A]["id"]]
            live_parent["position"].update(x=9000, y=-8000)
            ids = set(remote.items)

            set_change_output(self.case, A, 0, "Synthetic change selection")
            product = layout_preview_run(self.case)
            graph = read_json(Path(product["directory"]) / "graph.json")
            change_key = self.assert_rows(graph)
            self.assertEqual(graph["layout"]["metrics"]["after"]["node_overlaps"], 0)

            normal = sync_run(self.case, "latest", "SYNTHETIC-change-board")
            self.assertEqual(normal["created"], 0)
            self.assertEqual(set(remote.items), ids)
            self.assertEqual((live_parent["position"]["x"], live_parent["position"]["y"]), (9000, -8000))

            sync_run(self.case, "latest", "SYNTHETIC-change-board", reorganize=True)
            mapping = read_json(initial["state_file"])["items"]
            y = remote.items[mapping["tx:" + A]["id"]]["position"]["y"]
            for key in (change_key, "tx:" + B):
                self.assertEqual(remote.items[mapping[key]["id"]]["position"]["y"], y)
            sibling = next(edge["target"] for edge in graph["edges"] if edge["id"] == "out:" + A + ":1")
            self.assertGreater(remote.items[mapping[sibling]["id"]]["position"]["y"], y)

            compact = compact_preview_run(self.case)
            compact_graph = read_json(Path(compact["directory"]) / "graph.json")
            self.assert_rows(compact_graph)
            verified_compaction_preview(self.case, "latest", compact["preview_id"])

            set_change_output(self.case, A, None)
            current = saved_graph(self.case)[2]
            self.assertNotIn("change_outputs", current)
            self.assertIsNone(reusable_elk_preview(current, self.case / "previews", "straight"))
            with self.assertRaisesRegex(TraceError, "Service assessments changed"):
                verified_compaction_preview(self.case, "latest", compact["preview_id"])
            restored = optimize_graph(current)
            self.assertEqual([(n["id"], n["x"], n["y"]) for n in restored["nodes"]],
                             [(n["id"], n["x"], n["y"]) for n in baseline["nodes"]])

        self.assertEqual(self.archive, {str(p): p.read_bytes() for p in (self.case / "runs").rglob("*") if p.is_file()})
