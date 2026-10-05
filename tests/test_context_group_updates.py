"""Scoped board updates can replace a summary after its input scope shrinks."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer import board_layout
from liquid_tracer.common import TraceError, read_json
from liquid_tracer.export import build_graph
from liquid_tracer.miro import make_plan, sync
from tests.test_board_projection_safety import scope_plan
from tests.test_input_order import child_input, input_order_state
from tests.test_presentation_annotations import AnnotationMiro
from tests.test_context_group_miro import group_context_inputs


class ContextGroupUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "miro.json"
        self.remote = AnnotationMiro()
        self.graph = build_graph(input_order_state(4, continuing=(3,)))
        self.graph.pop("activity_frames", None)
        self.graph["run"]["ancestor_runs"] = []
        self.initial = scope_plan(make_plan(group_context_inputs(self.graph, enabled=True)))
        self.group = next(iter(self.initial["context_group_items"]))
        self.sync(self.initial)

    def sync(self, plan):
        return sync(plan, "test-board", self.path, token="test-token", transport=self.remote, interval=0)

    def item(self, key):
        return self.remote.items[read_json(self.path)["items"][key]["id"]]

    def reduced(self):
        graph = deepcopy(self.graph)
        edge = next(edge for edge in graph["edges"] if edge["id"] == child_input(0))
        graph["edges"] = [item for item in graph["edges"] if item["id"] != edge["id"]]
        graph["nodes"] = [node for node in graph["nodes"] if node["id"] != edge["source"]]
        return group_context_inputs(graph, enabled=True)

    def prepare(self):
        snapshot = board_layout.capture("test-board", self.path, self.initial["namespace"],
                                         token="test-token", transport=self.remote, interval=0)
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph) as elk:
            graph = board_layout.prepare_graph(self.reduced(), snapshot)
        return scope_plan(make_plan(graph)), elk

    def test_changed_same_key_group_is_recreated_in_clear_area(self):
        self.item(self.group)["position"]["y"] += 2300
        old_group = deepcopy(self.item(self.group))
        target = self.initial["context_group_items"][self.group]["target"]
        old_target = deepcopy(self.item(target))
        plan, elk = self.prepare()
        self.assertIn(self.group, {node["id"] for node in elk.call_args.args[0]["nodes"]})
        self.sync(plan)
        self.assertNotEqual(self.item(self.group)["id"], old_group["id"])
        self.assertGreater(self.item(self.group)["position"]["x"], plan["board_layout"]["bounds"][2])
        self.assertEqual(self.item(target), old_target)
        self.assertNotIn(child_input(0), read_json(self.path)["items"])
        for index in (1, 2):
            self.assertEqual(self.item(child_input(index))["startItem"]["id"], self.item(self.group)["id"])

    def test_missing_inputs_remain_rejected_without_board_snapshot(self):
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "original input evidence"):
            self.sync(scope_plan(make_plan(self.reduced())))
        self.assertEqual(len(self.remote.writes), writes)

    def test_reviewed_summary_organization_can_be_replaced_without_removing_its_frame(self):
        frame = {"id": "analyst-frame", "type": "frame", "position": {"x": 5000, "y": 3000},
                 "geometry": {"width": 4000, "height": 3000}, "data": {"title": "Investigator frame"}}
        self.remote.items[frame["id"]] = deepcopy(frame)
        body = self.item(self.group)
        body["parent"] = {"id": frame["id"]}
        body["position"].update(x=4800, y=2800, relativeTo="canvas_center")
        body["geometry"].update(width=body["geometry"]["width"] + 40, rotation=30)
        plan, _ = self.prepare()
        self.sync(plan)
        self.assertEqual(self.remote.items[frame["id"]], frame)
        self.assertNotIn("parent", self.item(self.group))

    def test_manual_summary_note_remains_protected(self):
        self.item(self.group)["data"]["content"] += "<p>Investigator analysis</p>"
        plan, _ = self.prepare()
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "manual (?:text/style )?edits"):
            self.sync(plan)
        self.assertEqual(len(self.remote.writes), writes)

    def test_unmanaged_attachment_remains_protected(self):
        target = self.initial["context_group_items"][self.group]["target"]
        self.remote.items["manual-edge"] = {"id": "manual-edge", "type": "connector",
            "startItem": {"id": self.item(self.group)["id"]}, "endItem": {"id": self.item(target)["id"]}}
        plan, _ = self.prepare()
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "connector attaches"):
            self.sync(plan)
        self.assertEqual(len(self.remote.writes), writes)

    def test_lost_delete_response_resumes_same_group_replacement(self):
        plan, _ = self.prepare()
        self.remote.lose_delete = True
        with self.assertRaisesRegex(TraceError, "lost"):
            self.sync(plan)
        self.sync(plan)
        mapping = read_json(self.path)["items"]
        self.assertNotIn(child_input(0), mapping)
        self.assertEqual(set(self.remote.items), {record["id"] for record in mapping.values()})
