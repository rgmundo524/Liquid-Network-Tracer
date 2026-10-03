"""Miro's omitted empty connector fields do not masquerade as analyst edits."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.common import TraceError, canonical, digest, read_json
from liquid_tracer.context_groups import group_context_inputs
from liquid_tracer.export import build_graph
from liquid_tracer.miro import make_plan, sync
from tests.test_board_projection_safety import projection_plan
from tests.test_input_order import child_input, input_order_state
from tests.test_presentation_annotations import AnnotationMiro


def rehash(plan):
    plan["sha256"] = digest(canonical({key: value for key, value in plan.items()
                                        if key != "sha256"}))
    return plan


def unlabelled(plan):
    plan = copy.deepcopy(plan)
    for connector in plan["connectors"]:
        connector["body"]["captions"] = []
        connector["body"]["style"]["fontSize"] = "11"
    return rehash(plan)


class EmptyCaptionMiro(AnnotationMiro):
    """Only responses omit these optional fields, including create/list replies."""

    def __init__(self, omit=True):
        super().__init__()
        self.omit = omit

    def __call__(self, *args):
        status, headers, body = super().__call__(*args)
        if not self.omit or not body:
            return status, headers, body
        response = json.loads(body)
        items = response.get("data", []) if isinstance(response.get("data"), list) else [response]
        for item in items:
            if item.get("type") == "connector" and item.get("captions") == []:
                item.pop("captions")
                if item.get("style", {}).get("fontSize") == "11":
                    item["style"].pop("fontSize")
        return status, headers, canonical(response)


class EmptyCaptionSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.counter = 0

    def start(self, plan, *, legacy=False):
        self.counter += 1
        self.path = self.directory / (str(self.counter) + ".json")
        self.remote = EmptyCaptionMiro(omit=not legacy)
        self.sync(plan)
        self.remote.omit = True

    def sync(self, plan, **options):
        return sync(plan, "test-board", self.path, token="synthetic-token", interval=0,
                    transport=self.remote, **options)

    def item(self, key):
        return self.remote.items[read_json(self.path)["items"][key]["id"]]

    def context_plans(self):
        graph = build_graph(input_order_state())
        graph.pop("activity_frames", None)
        graph["run"]["ancestor_runs"] = []
        return (unlabelled(make_plan(graph)),
                unlabelled(make_plan(group_context_inputs(graph, enabled=True))))

    def assert_blocked(self, plan, **options):
        writes = len(self.remote.writes)
        before = copy.deepcopy(self.remote.items)
        with self.assertRaisesRegex(TraceError, "manual.*edit"):
            self.sync(plan, **options)
        self.assertEqual(len(self.remote.writes), writes)
        self.assertEqual(self.remote.items, before)

    def test_ordinary_sync_is_idempotent_and_allows_attribution_colour_change(self):
        initial = unlabelled(projection_plan())
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.start(initial, legacy=legacy)
                writes = len(self.remote.writes)
                report = self.sync(initial)
                self.assertEqual(report["conflicts"], [])
                self.assertEqual(report["updated"], 0)
                self.assertEqual(len(self.remote.writes), writes)
                changed = copy.deepcopy(initial)
                for connector in changed["connectors"]:
                    connector["body"]["style"]["strokeColor"] = "#123456"
                report = self.sync(rehash(changed))
                self.assertEqual(report["conflicts"], [])
                for connector in changed["connectors"]:
                    self.assertEqual(self.item(connector["key"])["style"]["strokeColor"], "#123456")
                    self.assertEqual(self.item(connector["key"])["captions"], [])
                writes = len(self.remote.writes)
                self.sync(changed)
                self.assertEqual(len(self.remote.writes), writes)

    def test_context_regrouping_and_restoration_accept_omitted_empty_fields(self):
        plain, grouped = self.context_plans()
        group_key = next(iter(grouped["context_group_items"]))
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.start(plain, legacy=legacy)
                self.sync(grouped, reorganize=True)
                self.assertIn(group_key, read_json(self.path)["items"])
                self.sync(plain, reorganize=True)
                self.assertNotIn(group_key, read_json(self.path)["items"])
                mapping = read_json(self.path)["items"]
                self.assertEqual(set(self.remote.items), {record["id"] for record in mapping.values()})

    def test_projection_shrink_accepts_omitted_empty_fields(self):
        initial = unlabelled(projection_plan(extended=True))
        reduced = unlabelled(projection_plan("two"))
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.start(initial, legacy=legacy)
                report = self.sync(reduced)
                self.assertEqual(report["deleted"], 4)
                self.assertFalse({"tx:2", "addr:c", "input:2:0", "output:2:0"}
                                 .intersection(read_json(self.path)["items"]))
                writes = len(self.remote.writes)
                self.sync(reduced)
                self.assertEqual(len(self.remote.writes), writes)

    def test_real_caption_and_style_edits_still_block_retirement_without_writes(self):
        contexts = (*self.context_plans(), child_input(0), {"reorganize": True})
        projections = (unlabelled(projection_plan(extended=True)),
                       unlabelled(projection_plan("two")), "input:2:0", {})
        for scope, (initial, target, key, options) in (("context", contexts), ("projection", projections)):
            for legacy in (False, True):
                for field, value in (("captions", [{"content": "Analyst annotation", "position": "50%"}]),
                                     ("fontSize", "14"), ("strokeColor", "#123456")):
                    with self.subTest(scope=scope, legacy=legacy, field=field):
                        self.start(initial, legacy=legacy)
                        item = self.item(key)
                        (item if field == "captions" else item["style"])[field] = value
                        self.assert_blocked(target, **options)

    def test_removing_a_nonempty_caption_still_blocks_retirement_without_writes(self):
        contexts = (*self.context_plans(), child_input(0), {"reorganize": True})
        projections = (unlabelled(projection_plan(extended=True)),
                       unlabelled(projection_plan("two")), "input:2:0", {})
        for scope, (initial, target, key, options) in (("context", contexts), ("projection", projections)):
            with self.subTest(scope=scope):
                initial = copy.deepcopy(initial)
                connector = next(item for item in initial["connectors"] if item["key"] == key)
                connector["body"]["captions"] = [{"content": "Original label", "position": "50%"}]
                self.start(rehash(initial))
                self.item(key)["captions"] = []
                self.assert_blocked(target, **options)


if __name__ == "__main__":
    unittest.main()
