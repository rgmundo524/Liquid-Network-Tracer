import copy
import unittest

from liquid_tracer.miro_legend_updates import (
    geometry_snapshot, remember_geometry, resize_updates,
)


def shape(*, width=1300, height=260, x=700, y=-160, content="generated", font="14"):
    return {"data": {"content": content}, "style": {"fontSize": font},
            "geometry": {"width": width, "height": height},
            "position": {"x": x, "y": y, "origin": "center"}}


def record(body):
    return {"endpoint": "shapes", "managed": {
        "data": copy.deepcopy(body["data"]), "style": copy.deepcopy(body["style"])}}


class MiroLegendUpdateTests(unittest.TestCase):
    def setUp(self):
        self.actual = shape()
        self.desired = shape(width=1400, height=500, content="new rows")
        self.plan = {"shapes": [{"key": "legend", "body": self.desired}]}
        self.state = {"items": {"legend": record(self.actual)}}
        self.remote = {"legend": self.actual}

    def updates(self, removed=()):
        return resize_updates(self.plan, self.state, self.remote, set(removed))

    def test_legacy_growth_preserves_left_and_bottom_edges_without_mutating_inputs(self):
        before = copy.deepcopy((self.plan, self.state, self.remote))
        self.assertEqual(self.updates(), {"legend": {
            "geometry": {"width": 1400., "height": 500.},
            "position": {"x": 750., "y": -280., "origin": "center"}}})
        self.assertEqual((self.plan, self.state, self.remote), before)

    def test_smaller_plan_never_shrinks_or_moves_the_legend(self):
        self.desired["geometry"] = {"width": 900, "height": 200}
        self.actual["position"] = {"x": -99, "y": 51}
        self.assertEqual(self.updates(), {})

    def test_width_only_growth_keeps_current_height(self):
        self.desired["geometry"]["height"] = 100
        patch = self.updates()["legend"]
        self.assertEqual(patch["geometry"], {"width": 1400, "height": 260})
        self.assertEqual(patch["position"]["y"], -160)

    def test_manual_content_font_geometry_and_rotation_are_preserved(self):
        changes = [
            ("data", "content", "Analyst annotation"),
            ("style", "fontSize", 18),
            ("geometry", "width", 1500),
            ("geometry", "height", 250),
            ("geometry", "rotation", 30),
        ]
        original = copy.deepcopy(self.actual)
        for group, field, value in changes:
            with self.subTest(field=field):
                self.actual.clear()
                self.actual.update(copy.deepcopy(original))
                self.actual[group][field] = value
                self.assertEqual(self.updates(), {})

    def test_server_numeric_font_representation_is_not_a_manual_edit(self):
        self.actual["style"]["fontSize"] = 14
        self.assertIn("legend", self.updates())

    def test_acknowledged_geometry_allows_subsequent_growth_after_recovery(self):
        first = self.updates()["legend"]
        self.actual.update(first)
        self.actual["data"]["content"] = self.desired["data"]["content"]
        remember_geometry(self.state["items"]["legend"], self.actual)
        self.assertEqual(self.updates(), {})
        self.desired["geometry"]["height"] = 700
        second = self.updates()["legend"]
        self.assertEqual(second["geometry"]["height"], 700)
        self.assertEqual(second["position"]["y"], -380)

    def test_obstacles_move_growth_upward_with_clearance(self):
        obstacle = shape(width=100, height=100, x=750, y=-500)
        self.remote["tx"] = obstacle
        self.state["items"]["tx"] = record(obstacle)
        patch = self.updates()["legend"]
        self.assertEqual(patch["position"]["y"] + 500 / 2, -550 - 60)
        self.assertEqual(patch["position"]["x"], 750)

    def test_a_chain_of_obstacles_is_cleared_in_one_upward_pass(self):
        for index, y in enumerate((-500, -900, -1300)):
            key = "tx" + str(index)
            obstacle = shape(width=100, height=100, x=750, y=y)
            self.remote[key] = obstacle
            self.state["items"][key] = record(obstacle)
        patch = self.updates()["legend"]
        self.assertEqual(patch["position"]["y"] + 500 / 2, -1350 - 60)

    def test_existing_pages_grow_together_without_overlapping(self):
        upper = shape(width=700, height=300, x=400, y=-800)
        upper_desired = shape(width=700, height=600, x=400, y=-800, content="new rows")
        self.plan["shapes"].append({"key": "legend:2", "body": upper_desired})
        self.plan["presentation_items"] = {"legend:2": {"kind": "legend"}}
        self.remote["legend:2"] = upper
        self.state["items"]["legend:2"] = record(upper)
        remember_geometry(self.state["items"]["legend:2"], upper)
        self.desired["geometry"]["height"] = 900
        updates = self.updates()
        lower_top = updates["legend"]["position"]["y"] - 900 / 2
        upper_bottom = updates["legend:2"]["position"]["y"] + 600 / 2
        self.assertGreaterEqual(lower_top - upper_bottom, 60)

    def test_removed_items_frames_and_distant_shapes_do_not_block_growth(self):
        for key, endpoint, x in (("removed", "shapes", 750), ("frame", "frames", 750),
                                  ("distant", "shapes", 5000)):
            obstacle = shape(width=100, height=100, x=x, y=-500)
            self.remote[key] = obstacle
            self.state["items"][key] = {**record(obstacle), "endpoint": endpoint}
        self.assertEqual(self.updates({"removed"})["legend"]["position"]["y"], -280)
        self.assertEqual(self.updates({"legend"}), {})

    def test_unrecognized_new_page_geometry_requires_a_managed_baseline(self):
        self.plan["shapes"][0]["key"] = "legend:2"
        self.plan["presentation_items"] = {"legend:2": {"kind": "legend"}}
        self.remote["legend:2"] = self.remote.pop("legend")
        self.state["items"]["legend:2"] = self.state["items"].pop("legend")
        self.actual["geometry"] = {"width": 700, "height": 300}
        self.assertEqual(self.updates(), {})
        remember_geometry(self.state["items"]["legend:2"], self.actual)
        self.assertIn("legend:2", self.updates())

    def test_invalid_snapshots_do_not_replace_a_valid_baseline(self):
        target = {"legend_geometry": {"width": 700, "height": 300}}
        for geometry in ({}, {"width": float("nan"), "height": 300},
                         {"width": 700, "height": -1}, {"width": True, "height": 300}):
            with self.subTest(geometry=geometry):
                self.assertIsNone(geometry_snapshot({"geometry": geometry}))
                remember_geometry(target, {"geometry": geometry})
                self.assertEqual(target["legend_geometry"], {"width": 700, "height": 300})


if __name__ == "__main__":
    unittest.main()
