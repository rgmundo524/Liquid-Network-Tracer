import copy
import unittest

from liquid_tracer.common import TraceError
from liquid_tracer.horizontal_spacing import HORIZONTAL_SPACING_VERSION, compact_candidate


def candidate():
    return {
        "seed": 1,
        "nodes": [
            {"id": "a", "x": 0, "y": 0, "width": 160, "height": 160,
             "ports": [{"id": "out", "x": 160, "y": 80}]},
            {"id": "b", "x": 686.4, "y": 0, "width": 160, "height": 160,
             "ports": [{"id": "in", "x": 0, "y": 80}]},
        ],
        "edges": [{"id": "ab", "sections": [{
            "startPoint": {"x": 160, "y": 80}, "endPoint": {"x": 686.4, "y": 80}}],
            "labels": [{"id": "label:ab", "x": 360, "y": 40, "width": 126.4, "height": 24}]}],
    }


class HorizontalSpacingTests(unittest.TestCase):
    def test_caption_padding_shrinks_without_resizing_or_moving_ports(self):
        original = candidate()
        result = compact_candidate(copy.deepcopy(original))
        self.assertAlmostEqual(result["nodes"][1]["x"], 518.4)
        self.assertAlmostEqual(result["edges"][0]["labels"][0]["x"], 276)
        report = result["horizontal_spacing"]
        self.assertEqual(report, {"version": HORIZONTAL_SPACING_VERSION, "removed_width": 168.0,
                                  "removed_bands": 2})
        for before, after in zip(original["nodes"], result["nodes"]):
            self.assertEqual({k: v for k, v in before.items() if k != "x"},
                             {k: v for k, v in after.items() if k != "x"})
        self.assertEqual(result["edges"][0]["labels"][0]["width"], 126.4)
        self.assertEqual(result["edges"][0]["sections"][0]["endPoint"]["x"],
                         result["nodes"][1]["x"])

    def test_original_node_clearance_and_already_tighter_gaps_stay_unchanged(self):
        for x in (360, 300, 160):
            raw = candidate()
            raw["nodes"][1]["x"] = x
            raw["edges"][0]["labels"] = []
            raw["edges"][0]["sections"][0]["endPoint"]["x"] = x
            result = compact_candidate(raw)
            self.assertEqual(result["nodes"][1]["x"], x)
            self.assertEqual(result["horizontal_spacing"]["removed_width"], 0)

    def test_empty_graph_is_valid(self):
        result = compact_candidate({"nodes": [], "edges": []})
        self.assertEqual(result["horizontal_spacing"]["removed_bands"], 0)
        self.assertEqual(result["horizontal_spacing"]["removed_width"], 0)

    def test_vertical_channels_and_relative_point_order_are_preserved(self):
        raw = candidate()
        raw["nodes"][1]["x"] = 2000
        raw["nodes"][1]["y"] = 500
        raw["edges"][0]["labels"] = []
        section = raw["edges"][0]["sections"][0]
        section["bendPoints"] = [{"x": 600, "y": 80}, {"x": 600, "y": 300},
                                 {"x": 1000, "y": 300}, {"x": 1000, "y": 580}]
        section["endPoint"] = {"x": 2000, "y": 580}
        result = compact_candidate(raw)
        points = result["edges"][0]["sections"][0]["bendPoints"]
        self.assertEqual([p["y"] for p in points], [80, 300, 300, 580])
        self.assertEqual(points[0]["x"], points[1]["x"])
        self.assertEqual(points[2]["x"], points[3]["x"])
        self.assertEqual(points[2]["x"] - points[0]["x"], 22)
        self.assertGreaterEqual(points[0]["x"] - 160, 60)
        self.assertGreaterEqual(result["nodes"][1]["x"] - points[-1]["x"], 60)

    def test_wide_obstacle_at_another_y_blocks_the_whole_band(self):
        raw = candidate()
        raw["nodes"].append({"id": "obstacle", "x": 100, "y": 5000, "width": 700, "height": 160})
        result = compact_candidate(raw)
        self.assertEqual(result["nodes"][1]["x"], 686.4)
        self.assertEqual(result["horizontal_spacing"]["removed_width"], 0)

    def test_second_pass_is_idempotent(self):
        result = compact_candidate(candidate())
        before = copy.deepcopy(result)
        compact_candidate(result)
        self.assertEqual(result["nodes"], before["nodes"])
        self.assertEqual(result["edges"], before["edges"])
        self.assertEqual(result["horizontal_spacing"]["removed_width"], 0)

    def test_malformed_geometry_fails_before_any_mutation(self):
        for value in (None, True, float("inf"), "20", 10 ** 400):
            raw = candidate()
            raw["edges"][0]["sections"][0]["endPoint"]["x"] = value
            before = copy.deepcopy(raw)
            with self.subTest(value=str(value)[:20]), self.assertRaises(TraceError):
                compact_candidate(raw)
            self.assertEqual(raw, before)


if __name__ == "__main__":
    unittest.main()
