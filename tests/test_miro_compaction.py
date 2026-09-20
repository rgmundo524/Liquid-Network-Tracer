"""Compaction applies reviewed positions without globally scaling manual sizes."""

import copy
import random
import shutil
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.common import TraceError, canonical, digest, read_json
from liquid_tracer.compaction import compact_graph
from liquid_tracer.elk_layout import optimize_graph
from liquid_tracer.export import build_graph
from liquid_tracer.miro import _bounds, _bounds_collision, make_plan, sync, sync_frames, validate_plan
from liquid_tracer.miro_frames import activity_frames
from tests.test_miro_frame_sync import FrameMiro, contains, framed_graph
from tests.test_miro_layout import FEE_SHAPE, presented
from tests.test_miro_sync import graph
from tests.fixtures import fixture
from tests.test_layout import chain, state_from


def compact_plan(*, compact=True, spread=500, run="one"):
    value = graph(run)
    for index, node in enumerate(value["nodes"]):
        node["x"] = index * spread
    value["layout"] = {"algorithm": "elk_layered_v1"}
    if compact:
        value["layout"]["compaction"] = {
            "algorithm": "local_address_components_v1", "version": 1,
            "clearances": {"linked_horizontal": 200.0, "node_node": 80.0,
                           "components": 120.0, "edge_node": 60.0, "edge_edge": 22.0}}
    value["activity_frames"] = activity_frames(value)
    return make_plan(value)


class MiroCompactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "miro.json"
        self.remote = FrameMiro()

    def sync(self, plan, **kwargs):
        return sync(plan, "board=", self.path, token="synthetic-token", interval=0,
                    transport=self.remote, **kwargs)

    def frame(self, plan):
        return sync_frames(plan, "board=", self.path, token="synthetic-token", interval=0,
                           transport=self.remote)

    def item(self, key):
        return self.remote.items[read_json(self.path)["items"][key]["id"]]

    def test_enlarged_address_that_fits_keeps_exact_positions_and_live_geometry(self):
        initial = compact_plan(compact=False, spread=1500)
        self.sync(initial)
        self.frame(initial)
        ids = {key: record["id"] for key, record in read_json(self.path)["items"].items()}
        address = self.item("addr:b")
        address["geometry"].update({"width": 300, "height": 160, "rotation": 10})
        address["data"]["content"] = "Investigator's annotation"
        address["style"]["fillColor"] = "#123456"
        original = copy.deepcopy(address)
        plan = compact_plan()
        report = self.sync(plan, reorganize=True)
        self.assertGreater(report["moved"], 0)
        for item in plan["shapes"]:
            self.assertEqual(self.item(item["key"])["position"]["x"], item["body"]["position"]["x"])
            self.assertEqual(self.item(item["key"])["position"]["y"], item["body"]["position"]["y"])
            self.assertEqual(self.item(item["key"])["id"], ids[item["key"]])
        self.assertEqual(address["geometry"], original["geometry"])
        self.assertEqual(address["data"], original["data"])
        self.assertEqual(address["style"], original["style"])
        self.frame(plan)
        activity = plan["activity_frames"]["activities"][0]["key"]
        rotated = _bounds(address, "addr:b")
        fitted = copy.deepcopy(address)
        fitted["geometry"].update({"width": rotated[2], "height": rotated[3]})
        self.assertTrue(contains(self.item(activity), fitted))
        self.assertTrue(contains(self.item("frame:graph"), self.item(activity)))
        frame_patch = [payload for method, _, payload in self.remote.writes
                       if method == "PATCH" and "geometry" in payload]
        self.assertTrue(frame_patch)
        # A second application does not move already-compacted objects again.
        self.assertEqual(self.sync(plan, reorganize=True)["moved"], 0)

    def test_incompatible_live_size_blocks_before_state_or_board_writes(self):
        self.sync(compact_plan(compact=False, spread=1500))
        self.item("addr:b")["geometry"]["width"] = 700
        before = self.path.read_bytes()
        calls = len(self.remote.calls)
        with self.assertRaisesRegex(TraceError, "Restore affected shapes.*No board writes made"):
            self.sync(compact_plan(), reorganize=True)
        self.assertEqual(before, self.path.read_bytes())
        self.assertTrue(all(method == "GET" for method, _, _ in self.remote.calls[calls:]))

    def test_rotated_tall_shape_blocks_even_if_its_unrotated_width_would_fit(self):
        self.sync(compact_plan(compact=False, spread=1500))
        self.item("addr:b")["geometry"].update({"width": 160, "height": 700, "rotation": 90})
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "dimensions or rotations"):
            self.sync(compact_plan(), reorganize=True)
        self.assertEqual(len(self.remote.writes), writes)

    def test_existing_ordinary_sync_and_reorganize_behaviors_are_preserved(self):
        self.sync(compact_plan(compact=False, spread=1500))
        self.item("addr:b")["geometry"]["width"] = 700
        original_positions = {key: copy.deepcopy(self.item(key)["position"])
                              for key in ("addr:a", "tx:1", "addr:b")}
        self.sync(compact_plan())
        self.assertEqual(original_positions, {key: self.item(key)["position"] for key in original_positions})
        self.sync(compact_plan(compact=False), reorganize=True)
        self.assertEqual(self.item("tx:1")["position"]["x"], 500 * 700 / 160)

    def test_retained_run_note_only_translates_compact_group_and_frames_refit(self):
        self.sync(compact_plan(compact=False, spread=1500))
        note = self.item("run:one")
        note["position"].update({"x": 500, "y": 50})
        previous = copy.deepcopy(note)
        plan = compact_plan(run="two")
        self.sync(plan, reorganize=True)
        translations = {(self.item(item["key"])["position"]["x"] - item["body"]["position"]["x"],
                         self.item(item["key"])["position"]["y"] - item["body"]["position"]["y"])
                        for item in plan["shapes"]}
        self.assertEqual(len(translations), 1)
        self.assertNotEqual(translations, {(0, 0)})
        self.assertEqual(note, previous)
        self.frame(plan)
        self.assertTrue(contains(self.item("frame:graph"), note))

    def test_marker_does_not_bypass_local_geometry_validation(self):
        plan = compact_plan(spread=300)
        with self.assertRaisesRegex(TraceError, "minimum spacing"):
            self.sync(plan, reorganize=True)
        self.assertEqual(self.remote.calls, [])
        self.assertFalse(self.path.exists())

    def test_invalid_metadata_is_rejected_even_with_a_recomputed_checksum(self):
        for field, value in (("version", True), ("algorithm", "invented"),
                             ("clearances", {"node_node": 0})):
            with self.subTest(field=field):
                plan = compact_plan()
                plan["layout"]["compaction"][field] = value
                plan["sha256"] = digest(canonical({key: value for key, value in plan.items() if key != "sha256"}))
                with self.assertRaisesRegex(TraceError, "Malformed compact"):
                    validate_plan(plan)
        self.assertEqual(self.remote.calls, [])

    def test_live_growth_cannot_create_new_sibling_frame_overlap(self):
        value = framed_graph()
        value["layout"] = copy.deepcopy(compact_plan()["layout"])
        plan = make_plan(value)
        self.sync(plan)
        # Main shapes retain 200 units of vertical clearance. The extra frame
        # title/padding would nevertheless overlap the adjacent activity frame.
        self.item("addr:b:branch2")["geometry"]["height"] = 1040
        self.sync(plan, reorganize=True)
        before = self.path.read_bytes()
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "activity frames overlap"):
            self.frame(plan)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.remote.writes), writes)

    def test_existing_frame_overlap_remains_valid_when_not_worsened_by_sizes(self):
        value = framed_graph()
        for node in value["nodes"]:
            if node["id"].endswith(":branch3"):
                node["y"] -= 1300
        value["layout"] = copy.deepcopy(compact_plan()["layout"])
        plan = make_plan(value)
        self.sync(plan)
        self.item("addr:b:branch2")["geometry"]["height"] = 180
        self.sync(plan, reorganize=True)
        self.frame(plan)

    def test_fee_row_retains_existing_spacing_and_return_links_keep_exceptions(self):
        value = presented(include_fees=True)
        value["layout"] = copy.deepcopy(compact_plan()["layout"])
        fee = next(node for node in value["nodes"] if node["id"] == FEE_SHAPE)
        fee["x"] = 450  # Fee connection points slightly right but is not forward flow.
        plan = make_plan(value)
        self.sync(plan, reorganize=True)
        # A returned connection may have increasing center x while its fixed
        # perimeter attachments route backward. Preserve ELK's explicit reason.
        value = graph()
        value["nodes"][1].update({"x": 40, "y": 500})
        value["edges"][0]["routing_exception"] = "return"
        value["layout"] = copy.deepcopy(compact_plan()["layout"])
        validate_plan(make_plan(value))

    @unittest.skipUnless(shutil.which("node") and (Path(__file__).resolve().parents[1]
                         / "layout/node_modules/elkjs/package.json").is_file(), "Local ELK installation required")
    def test_real_compacted_fee_row_and_merged_address_cycles_produce_valid_plans(self):
        transactions = {data["txid"]: data for key, data in fixture().items() if not key.endswith("outspends")}
        for merged, fees, data in ((False, True, transactions), (True, False, chain(10))):
            with self.subTest(merged=merged, fees=fees):
                state = state_from(data)
                state["ancestor_runs"] = []
                value = compact_graph(optimize_graph(build_graph(state, merge_addresses=merged, include_fees=fees)))
                plan = make_plan(value)
                validate_plan(plan)
                if fees:
                    fee_ids = {key for key, proof in value["fee_items"].items() if proof["endpoint"] == "shapes"}
                    row = sorted((node for node in value["nodes"] if node["id"] in fee_ids), key=lambda node: node["x"])
                    self.assertGreater(len(row), 1)
                    self.assertEqual(row[1]["x"] - row[0]["x"] - (row[0]["width"] + row[1]["width"]) / 2, 70)
                else:
                    self.assertTrue(any(edge["routing_exception"] == "return" for edge in plan["connectors"]))

    def test_rectangle_sweep_agrees_with_pairwise_oracle_and_large_column(self):
        rng = random.Random(23)
        for count in (2, 10, 100):
            for _ in range(20):
                bounds = {str(index): (rng.uniform(-3000, 3000), rng.uniform(-3000, 3000),
                                      rng.uniform(10, 400), rng.uniform(10, 400))
                          for index in range(count)}
                values = list(bounds.values())
                expected = any(abs(a[0] - b[0]) < (a[2] + b[2]) / 2 + 80
                               and abs(a[1] - b[1]) < (a[3] + b[3]) / 2 + 80
                               for i, a in enumerate(values) for b in values[i + 1:])
                self.assertEqual(_bounds_collision(bounds, 80) is not None, expected)
        column = {str(index): (0, index * 240, 160, 160) for index in range(10000)}
        self.assertIsNone(_bounds_collision(column, 80))
        column["conflict"] = (0, 700, 160, 160)
        self.assertIsNotNone(_bounds_collision(column, 80))


if __name__ == "__main__":
    unittest.main()
