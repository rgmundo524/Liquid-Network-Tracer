import copy
import unittest

from liquid_tracer.trace_route_cleanup import cleanup_routes


def point(x, y):
    return {"x": x, "y": y}


def route(identity, points, labels=None):
    return {"id": identity, "sections": [{"startPoint": point(*points[0]),
            "bendPoints": [point(*value) for value in points[1:-1]], "endPoint": point(*points[-1])}],
            "labels": labels or []}


def points(edge):
    value = edge["sections"][0]
    return [value["startPoint"], *value.get("bendPoints", []), value["endPoint"]]


def fixture(*, caption=True):
    nodes = [{"id": "source", "x": 20, "y": 40, "width": 80, "height": 160},
             {"id": "target", "x": 600, "y": 40, "width": 80, "height": 160}]
    labels = [{"id": "label:e", "text": "caption", "x": 265, "y": 219, "width": 100, "height": 24}] if caption else []
    edges = [route("e", [(100, 120), (300, 120), (300, 250), (330, 250), (330, 100), (600, 100)], labels)]
    return nodes, edges


def step_fixture(*, caption=True, upward=False, backward_step=False):
    nodes = [{"id": "source", "x": 20, "y": 40, "width": 80, "height": 160},
             {"id": "target", "x": 600, "y": 300, "width": 80, "height": 160}]
    first_x, second_x = (330, 300) if backward_step else (300, 330)
    coordinates = [(100, 120), (first_x, 120), (first_x, 250),
                   (second_x, 250), (second_x, 380), (600, 380)]
    labels = [{"id": "label:e", "text": "caption", "x": 265, "y": 219,
               "width": 100, "height": 24}] if caption else []
    if upward:
        for node in nodes:
            node["y"] = 500 - node["y"] - node["height"]
        coordinates = [(x, 500 - y) for x, y in coordinates]
    return nodes, [route("e", coordinates, labels)]


def clean(nodes, edges, **options):
    return cleanup_routes(nodes, edges, {"e": ("source", "target")}, width=800, height=500, **options)


class TraceRouteCleanupTests(unittest.TestCase):
    def test_monotone_steps_use_fewer_bends_even_without_shorter_manhattan_length(self):
        for upward in (False, True):
            for backward_step in (False, True):
                with self.subTest(upward=upward, backward_step=backward_step):
                    nodes, edges = step_fixture(upward=upward, backward_step=backward_step)
                    original = copy.deepcopy((nodes, edges))
                    result, stats = clean(nodes, edges)
                    self.assertEqual(stats["applied"], 1)
                    self.assertEqual(stats["steps_applied"], 1)
                    self.assertEqual(stats["bends_removed"], 2)
                    self.assertEqual(stats["length_removed"], 60 if backward_step else 0)
                    self.assertEqual(points(result[0])[::3], points(edges[0])[::5])
                    self.assertEqual(len(points(result[0])), 4)
                    self.assertEqual((nodes, edges), original)
                    self.assertEqual((result, stats), clean(nodes, edges))
                    for field in ("id", "text", "width", "height"):
                        self.assertEqual(result[0]["labels"][0][field], edges[0]["labels"][0][field])

    def test_step_retains_unchanged_contact_but_rejects_new_crossing_touch_and_overlap(self):
        nodes, edges = step_fixture(caption=False)
        edges.append(route("existing", [(500, 300), (500, 450)]))
        result, stats = clean(nodes, edges)
        self.assertEqual(stats["applied"], 1)
        self.assertEqual(result[1], edges[1])
        # Each obstruction blocks the newly occupied source-track extension
        # and the alternative target-track extension, in distinct ways.
        pairs = [([(290, 300), (310, 300)], [(320, 200), (340, 200)]),
                 ([(290, 300), (300, 300)], [(330, 200), (340, 200)]),
                 ([(300, 280), (300, 330)], [(330, 150), (330, 220)])]
        for pair in pairs:
            with self.subTest(obstructions=pair):
                nodes, edges = step_fixture(caption=False)
                edges.extend(route("other-" + str(index), values) for index, values in enumerate(pair))
                result, stats = clean(nodes, edges)
                self.assertEqual(stats["applied"], 0)
                self.assertEqual(result, edges)

    def test_step_uses_second_track_when_only_first_would_hit_an_object(self):
        nodes, edges = step_fixture(caption=False)
        nodes.append({"id": "obstacle", "x": 295, "y": 290, "width": 10, "height": 20})
        result, stats = clean(nodes, edges)
        self.assertEqual(stats["applied"], 1)
        self.assertEqual(points(result[0])[1]["x"], 330)
        nodes.append({"id": "other-obstacle", "x": 325, "y": 170, "width": 10, "height": 20})
        result, stats = clean(nodes, edges)
        self.assertEqual(stats["applied"], 0)
        self.assertEqual(result, edges)

    def test_step_with_no_safe_caption_slot_remains_unchanged(self):
        nodes, edges = step_fixture()
        blockers = [{"id": "blocker-1", "text": "other", "x": 120, "y": 70, "width": 470, "height": 110},
                    {"id": "blocker-2", "text": "other", "x": 120, "y": 340, "width": 470, "height": 85}]
        edges.append(route("other", [(10, 470), (700, 470)], blockers))
        result, stats = clean(nodes, edges)
        self.assertEqual(stats["applied"], 0)
        self.assertEqual(result, edges)

    def test_step_budget_exhaustion_keeps_original_geometry_and_caption(self):
        nodes, edges = step_fixture()
        for options in ({"max_checks": 0}, {"max_checks": 10}, {"max_edge_checks": 10},
                        {"max_index_items": 2}):
            with self.subTest(options=options):
                result, stats = clean(nodes, edges, **options)
                self.assertEqual(result, edges)
                self.assertEqual(stats["applied"], 0)
                self.assertLessEqual(stats["checks"], stats["check_limit"])

    def test_backward_endpoints_remain_a_return_even_if_accidentally_nominated(self):
        nodes, edges = step_fixture(caption=False)
        edges = [route("e", [(650, 120), (700, 120), (700, 250), (10, 250), (10, 380), (30, 380)])]
        result, stats = clean(nodes, edges)
        self.assertEqual(result, edges)
        self.assertEqual(stats["eligible"], 0)

    def test_shortens_u_and_relocates_caption_without_changing_endpoints_or_inputs(self):
        nodes, edges = fixture()
        original = copy.deepcopy((nodes, edges))
        result, stats = clean(nodes, edges)
        self.assertEqual(stats["applied"], 1)
        self.assertGreater(stats["length_removed"], 0)
        self.assertEqual(len(points(result[0])), 4)
        self.assertEqual(points(result[0])[::3], points(edges[0])[::5])
        self.assertEqual((nodes, edges), original)
        for field in ("id", "text", "width", "height"):
            self.assertEqual(result[0]["labels"][0][field], edges[0]["labels"][0][field])
        self.assertNotEqual(result[0]["labels"], edges[0]["labels"])
        self.assertEqual((result, stats), clean(nodes, edges))

    def test_unchanged_crossings_do_not_prevent_safe_shortcut(self):
        nodes, edges = fixture(caption=False)
        edges.append(route("existing-crossing", [(500, 50), (500, 300)]))
        result, stats = clean(nodes, edges)
        self.assertEqual(stats["applied"], 1)
        self.assertEqual(result[1], edges[1])

    def test_new_touch_or_crossing_or_overlap_rejects_both_candidates(self):
        for obstruction in ([(315, 50), (315, 300)], [(315, 100), (315, 120)],
                            [(290, 100), (340, 100), (340, 120), (290, 120)]):
            with self.subTest(obstruction=obstruction):
                nodes, edges = fixture(caption=False)
                edges.append(route("other", obstruction))
                result, stats = clean(nodes, edges)
                self.assertEqual(stats["applied"], 0)
                self.assertEqual(result, edges)

    def test_target_track_can_succeed_after_source_track_crossing_rejected(self):
        nodes, edges = fixture(caption=False)
        edges.append(route("other", [(290, 110), (310, 110)]))
        result, stats = clean(nodes, edges)
        self.assertEqual(stats["applied"], 1)
        self.assertEqual(points(result[0])[1]["x"], 330)

    def test_new_node_or_caption_conflicts_keep_original(self):
        nodes, edges = fixture()
        nodes.append({"id": "obstacle", "x": 310, "y": 90, "width": 10, "height": 40})
        result, stats = clean(nodes, edges)
        self.assertEqual(stats["applied"], 0)
        self.assertEqual(result, edges)
        nodes, edges = fixture()
        # Surround both long stubs with existing caption boxes. The original
        # caption is below this region and is retained if no clean slot exists.
        blocker = {"id": "blocker", "text": "other", "x": 140, "y": 65, "width": 420, "height": 95}
        edges.append(route("other", [(10, 400), (700, 400)], [blocker]))
        result, stats = clean(nodes, edges)
        self.assertEqual(stats["applied"], 0)
        self.assertEqual(result, edges)

    def test_genuine_return_and_non_orthogonal_routes_are_preserved(self):
        nodes, edges = fixture(caption=False)
        # An authentic return is excluded by the caller's directional/column
        # check, even if some of its bends happen to form a U.
        returned = route("return", [(650, 200), (700, 200), (700, 350), (10, 350), (10, 200), (30, 200)])
        edges.append(returned)
        result, stats = clean(nodes, edges)
        self.assertEqual(result[1], returned)
        self.assertEqual(stats["eligible"], 1)
        edges.append(route("unknown", [(0, 0), (50, 50)]))
        result, stats = clean(nodes, edges)
        self.assertEqual(result, edges)
        self.assertTrue(stats["unsupported_geometry"])

    def test_budget_and_index_limits_fail_closed(self):
        for options in ({"max_checks": 0}, {"max_edge_checks": 0}, {"max_index_items": 1}):
            with self.subTest(options=options):
                nodes, edges = fixture()
                result, stats = clean(nodes, edges, **options)
                self.assertEqual(result, edges)
                self.assertEqual(stats["applied"], 0)
                self.assertLessEqual(stats["checks"], stats["check_limit"])
                self.assertTrue(stats["budget_exhausted"] or stats["index_limit_reached"])

    def test_circle_and_diamond_ports_remain_on_their_actual_perimeters(self):
        from liquid_tracer.elk_layout import segment_hits_node

        nodes = [{"id": "source", "x": 20, "y": 40, "width": 160, "height": 160},
                 {"id": "target", "x": 600, "y": 40, "width": 160, "height": 160}]
        # Ellipse east at y=160 has dx=sqrt(80²-40²); diamond west at
        # y=100 is x=620. These points are inside bounding rectangles.
        start = (100 + (80 ** 2 - 40 ** 2) ** .5, 160)
        edges = [route("e", [start, (300, 160), (300, 250), (330, 250), (330, 100), (620, 100)])]
        result, stats = clean(nodes, edges, node_shapes={"source": "address", "target": "event"})
        self.assertEqual(stats["applied"], 1)
        self.assertEqual(points(result[0])[0], point(*start))
        self.assertEqual(points(result[0])[-1], point(620, 100))
        for a, b in zip(points(result[0]), points(result[0])[1:]):
            for node in nodes:
                centered = {**node, "x": node["x"] + node["width"] / 2,
                            "y": node["y"] + node["height"] / 2,
                            "kind": "address" if node["id"] == "source" else "event"}
                self.assertFalse(segment_hits_node(a, b, centered))

    def test_relocated_caption_does_not_cross_candidate_route(self):
        nodes, edges = fixture()
        result, stats = clean(nodes, edges)
        self.assertEqual(stats["applied"], 1)
        label = result[0]["labels"][0]
        for a, b in zip(points(result[0]), points(result[0])[1:]):
            self.assertFalse(max(min(a["x"], b["x"]), label["x"]) <= min(max(a["x"], b["x"]), label["x"] + label["width"])
                             and max(min(a["y"], b["y"]), label["y"]) <= min(max(a["y"], b["y"]), label["y"] + label["height"]))

    def test_later_routes_check_accepted_geometry_not_stale_original_tracks(self):
        nodes, edges = fixture(caption=False)
        edges.append(route("later", [(100, 90), (280, 90), (280, 400), (350, 400), (350, 110), (600, 110)]))
        eligible = {edge["id"]: ("source", "target") for edge in edges}
        result, stats = cleanup_routes(nodes, edges, eligible, width=800, height=500)
        self.assertEqual(stats["applied"], 1)
        self.assertEqual(len(points(result[0])), 4)
        # The later source-track shortcut would intersect the newly accepted
        # first route at (300,110), which its original U-route did not occupy.
        self.assertEqual(result[1], edges[1])

    def test_reported_pegout_detour_keeps_long_amount_caption_clear(self):
        # Geometry from the reported T3 peg-out, without its transaction data.
        nodes = [{"id": "source", "x": 190101.2, "y": 36674, "width": 160, "height": 160},
                 {"id": "target", "x": 190852, "y": 36674, "width": 160, "height": 160}]
        caption = {"id": "caption:e", "text": "vout 0 · 13.22215589 L-BTC",
                   "x": 190462.6, "y": 36862, "width": 240.8, "height": 24}
        edges = [route("e", [(190261.2, 36754), (190572, 36754), (190572, 36893),
                             (190594, 36893), (190594, 36714), (190892, 36714)], [caption]),
                 route("context", [(190615, 36400), (190615, 37400)])]
        result, stats = cleanup_routes(nodes, edges, {"e": ("source", "target")},
            node_shapes={"source": "transaction", "target": "event"}, width=195029, height=135810)
        self.assertEqual(stats["applied"], 1)
        self.assertEqual(len(points(result[0])), 4)
        self.assertEqual(points(result[0])[::3], points(edges[0])[::5])
        self.assertEqual(stats["length_removed"], 278)
        self.assertEqual(result[0]["labels"][0]["text"], caption["text"])
        self.assertEqual(result[1], edges[1])


if __name__ == "__main__":
    unittest.main()
