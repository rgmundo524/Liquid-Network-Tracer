"""Large route inventories retain every regional collision hazard."""
import copy
import random
import unittest
from unittest.mock import patch

from liquid_tracer.trace_route_cleanup import MAX_INDEX_ITEMS, cleanup_routes
from tests.test_trace_route_cleanup import fixture, points, route, step_fixture


def padding(count=32):
    return [route("far-" + str(index), [(1000 + index * 30, 1000), (1010 + index * 30, 1000)])
            for index in range(count)]


def clean(nodes, edges, **kwargs):
    return cleanup_routes(nodes, edges, {"e": ("source", "target")}, width=800, height=500,
                          max_index_items=kwargs.pop("max_index_items", 16), **kwargs)


class TraceRouteRegionTests(unittest.TestCase):
    def test_inventory_above_default_cap_still_cleans_small_clear_region(self):
        nodes, edges = fixture()
        # 20,001 remote five-segment paths exceed the production 100,000
        # primitive cap without contributing a hazard in the small test area.
        for index in range(MAX_INDEX_ITEMS // 5 + 1):
            x = 1000 + index * 30
            edges.append(route("far-" + str(index), [(x, 1000), (x + 5, 1000), (x + 5, 1010),
                                                     (x + 10, 1010), (x + 10, 1005), (x + 20, 1005)]))
        original = copy.deepcopy(edges[0])
        result, stats = clean(nodes, edges, max_index_items=MAX_INDEX_ITEMS)
        self.assertGreater(stats["total_index_items"], MAX_INDEX_ITEMS)
        self.assertEqual(stats["index_mode"], "regional")
        self.assertEqual(stats["applied"], 1)
        self.assertLessEqual(stats["index_items"], MAX_INDEX_ITEMS)
        self.assertEqual(stats["coarse_index_items"], len(nodes) + len(edges))
        self.assertFalse(stats["partial"])
        self.assertEqual(edges[0], original)
        self.assertTrue(all(actual is old for actual, old in zip(result[1:], edges[1:])))

    def test_large_node_inventory_uses_regions_instead_of_global_node_cap(self):
        nodes, edges = fixture(caption=False)
        nodes.extend({"id": "far-" + str(index), "x": 1000 + index * 30, "y": 1000,
                      "width": 10, "height": 10} for index in range(40))
        result, stats = clean(nodes, edges)
        self.assertEqual(stats["applied"], 1)
        self.assertEqual(stats["index_mode"], "regional")
        self.assertEqual(len(points(result[0])), 4)

    def test_long_route_with_both_endpoints_outside_region_still_blocks(self):
        nodes, edges = fixture(caption=False)
        edges.extend(padding())
        edges.append(route("long-crossing", [(315, -10000), (315, 10000)]))
        result, stats = clean(nodes, edges)
        self.assertEqual(stats["index_mode"], "regional")
        self.assertEqual(stats["applied"], 0)
        self.assertEqual(result, edges)
        self.assertFalse(stats["partial"])

    def test_detached_foreign_caption_outside_owner_route_region_is_indexed(self):
        nodes, edges = fixture()
        edges.extend(padding())
        caption = {"id": "foreign-caption", "x": 140, "y": 65, "width": 420, "height": 95}
        edges.append(route("far-route-local-caption", [(5000, 5000), (6000, 5000)], [caption]))
        result, stats = clean(nodes, edges)
        self.assertEqual(stats["applied"], 0)
        self.assertEqual(result, edges)
        self.assertFalse(stats["partial"])

    def test_obstacle_origin_outside_region_with_spanning_body_is_indexed(self):
        nodes, edges = fixture(caption=False)
        edges.extend(padding())
        nodes.append({"id": "spanning", "x": 310, "y": -2000, "width": 10, "height": 2200})
        result, stats = clean(nodes, edges)
        self.assertEqual(stats["applied"], 0)
        self.assertEqual(result, edges)

    def test_dense_region_can_skip_without_preventing_later_clear_region(self):
        nodes, edges = fixture(caption=False)
        edges[0]["id"] = "a"
        nodes.extend({"id": "dense-" + str(index), "x": 305, "y": 110,
                      "width": 2, "height": 2} for index in range(30))
        other_nodes, other_edges = fixture(caption=False)
        for node in other_nodes:
            node["id"] += "-later"
            node["y"] += 1000
        for section in other_edges[0]["sections"]:
            for item in [section["startPoint"], *section["bendPoints"], section["endPoint"]]:
                item["y"] += 1000
        other_edges[0]["id"] = "b"
        nodes.extend(other_nodes)
        edges.extend(other_edges)
        result, stats = cleanup_routes(nodes, edges, {"a": ("source", "target"),
            "b": ("source-later", "target-later")}, width=800, height=1600, max_index_items=16)
        self.assertEqual(result[0], edges[0])
        self.assertEqual(len(points(result[1])), 4)
        self.assertEqual(stats["applied"], 1)
        self.assertEqual(stats["index_limit_skipped"], 1)
        self.assertEqual(stats["candidates_skipped"], 1)
        self.assertTrue(stats["partial"])
        self.assertLessEqual(stats["index_items"], 16)

    def test_budget_exhaustion_keeps_original_without_escaping_limits(self):
        nodes, edges = fixture()
        edges.extend(padding())
        for options in ({"max_checks": 0}, {"max_checks": 10}, {"max_edge_checks": 10}):
            with self.subTest(options=options):
                result, stats = clean(nodes, edges, **options)
                self.assertEqual(result, edges)
                self.assertEqual(stats["applied"], 0)
                self.assertTrue(stats["partial"])
                self.assertEqual(stats["candidates_skipped"], 1)
                self.assertLessEqual(stats["checks"], stats["check_limit"])
                self.assertLessEqual(stats["checks"], stats["per_edge_check_limit"])

    def test_accepted_geometry_refits_coarse_index_before_next_candidate(self):
        nodes, edges = fixture(caption=False)
        edges.append(route("later", [(100, 90), (280, 90), (280, 400), (350, 400), (350, 110), (600, 110)]))
        edges.extend(padding())
        result, stats = cleanup_routes(nodes, edges, {"e": ("source", "target"), "later": ("source", "target")},
                                       width=800, height=500, max_index_items=16)
        self.assertEqual(stats["applied"], 1)
        self.assertEqual(len(points(result[0])), 4)
        self.assertEqual(result[1:], edges[1:])
        self.assertEqual((result, stats), cleanup_routes(nodes, edges,
            {"e": ("source", "target"), "later": ("source", "target")}, width=800, height=500, max_index_items=16))

    def test_region_mode_preserves_return_and_rejects_unsupported_geometry(self):
        nodes, edges = fixture(caption=False)
        edges.extend(padding())
        returned = route("return", [(650, 200), (700, 200), (700, 350), (10, 350), (10, 200), (30, 200)])
        edges.append(returned)
        result, _ = clean(nodes, edges)
        self.assertEqual(result[-1], returned)
        edges.append(route("diagonal", [(10000, 10000), (11000, 11000)]))
        result, stats = clean(nodes, edges)
        self.assertEqual(result, edges)
        self.assertTrue(stats["unsupported_geometry"])

    def test_query_interrupt_propagates_without_mutating_input(self):
        nodes, edges = fixture()
        edges.extend(padding())
        original = copy.deepcopy((nodes, edges))
        with patch("liquid_tracer.trace_route_cleanup._Index.query", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                clean(nodes, edges)
        self.assertEqual((nodes, edges), original)

    def test_regional_step_and_caption_match_safe_global_result(self):
        for upward in (False, True):
            nodes, edges = step_fixture(upward=upward)
            expected, _ = clean(nodes, edges, max_index_items=1000)
            edges.extend(padding())
            result, stats = clean(nodes, edges)
            self.assertEqual(result[0], expected[0])
            self.assertEqual(stats["steps_applied"], 1)
            self.assertEqual(points(result[0])[0], points(edges[0])[0])
            self.assertEqual(points(result[0])[-1], points(edges[0])[-1])

    def test_regional_and_complete_global_hazard_queries_agree_on_random_fixtures(self):
        rng = random.Random(713)
        for case in range(120):
            nodes, edges = (fixture(caption=bool(case % 2)) if case % 3 else
                            step_fixture(caption=bool(case % 2), upward=bool(case % 4)))
            for number in range(5):
                x, y = rng.randint(105, 590), rng.randint(10, 450)
                nodes.append({"id": "obstacle-" + str(number), "x": x, "y": y,
                              "width": rng.randint(1, 50), "height": rng.randint(1, 50)})
                x2, y2 = rng.randint(105, 590), rng.randint(10, 450)
                label = {"x": rng.randint(80, 580), "y": rng.randint(10, 450),
                         "width": rng.randint(10, 90), "height": rng.randint(10, 30)}
                edges.append(route("other-" + str(number), [(x, y), (x2, y), (x2, y2)], [label]))
            edges.extend(padding(80))
            original = copy.deepcopy((nodes, edges))
            expected, _ = clean(nodes, edges, max_index_items=1000, max_edge_checks=10000)
            actual, stats = clean(nodes, edges, max_index_items=64, max_edge_checks=10000)
            with self.subTest(case=case):
                self.assertEqual(actual, expected)
                self.assertEqual(stats["index_mode"], "regional")
                self.assertFalse(stats["partial"])
                self.assertEqual((nodes, edges), original)


if __name__ == "__main__":
    unittest.main()
