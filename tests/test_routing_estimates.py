import copy
import unittest

from liquid_tracer.elk_layout import segment_hits_node
from liquid_tracer.edge_labels import caption_box, caption_size, route_signature
from liquid_tracer.routing_estimates import estimated_miro_route, route_variants


def node(key, x, y, kind="transaction", size=100):
    return {"id": key, "kind": kind, "x": x, "y": y, "width": size, "height": size}


def edge(start=(100, 50), end=(0, 50), **extra):
    return {"id": "e", "source": "a", "target": "b", "connector_shape": "elbowed",
            "attachment": {field: {"position": {"x": f"{value[0]}%", "y": f"{value[1]}%"}}
                           for field, value in (("startItem", start), ("endItem", end))}, **extra}


class RoutingEstimateTests(unittest.TestCase):
    def assert_clear_endpoints(self, route, nodes):
        for a, b in zip(route, route[1:]):
            self.assertTrue(a[0] == b[0] or a[1] == b[1], route)
            for value in nodes.values():
                self.assertFalse(segment_hits_node({"x": a[0], "y": a[1]},
                                                   {"x": b[0], "y": b[1]}, value), route)

    def test_forward_facing_ports_keep_midpoint_estimate(self):
        nodes = {"a": node("a", 0, 0), "b": node("b", 400, 200)}
        route = estimated_miro_route(edge(), nodes)
        self.assertEqual(route, [(50, 0), (200, 0), (200, 200), (350, 200)])
        self.assert_clear_endpoints(route, nodes)

    def test_same_east_return_ports_use_outside_u(self):
        nodes = {"a": node("a", 400, 0), "b": node("b", 0, 200, "address")}
        route = estimated_miro_route(edge(end=(100, 50), routing_exception="return"), nodes)
        self.assertEqual(route, [(450, 0), (510, 0), (510, 200), (50, 200)])
        self.assert_clear_endpoints(route, nodes)

    def test_same_west_ports_use_outside_u(self):
        nodes = {"a": node("a", 0, 0, "address"), "b": node("b", 400, 200)}
        route = estimated_miro_route(edge(start=(0, 50)), nodes)
        self.assertEqual(route, [(-50, 0), (-110, 0), (-110, 200), (350, 200)])
        self.assert_clear_endpoints(route, nodes)

    def test_reversed_facing_ports_depart_outward_before_returning(self):
        nodes = {"a": node("a", 400, 0), "b": node("b", 0, 100)}
        route = estimated_miro_route(edge(routing_exception="return"), nodes)
        self.assertGreater(route[1][0], route[0][0])
        self.assertLess(route[-2][0], route[-1][0])
        self.assert_clear_endpoints(route, nodes)

    def test_fee_bottom_attachment_is_approached_from_below(self):
        nodes = {"a": node("a", 0, 200), "b": node("b", 400, 0, "event")}
        value = edge(end=(50, 100), routing_exception="fee")
        route = estimated_miro_route(value, nodes)
        self.assertEqual(route[0], (50, 200))
        self.assertEqual(route[-1], (400, 50))
        self.assertGreater(route[1][0], route[0][0])
        self.assertGreater(route[-2][1], route[-1][1])
        self.assert_clear_endpoints(route, nodes)

    def test_short_gap_does_not_force_a_stub_through_the_other_endpoint(self):
        nodes = {"a": node("a", 0, 0), "b": node("b", 120, 0)}
        route = estimated_miro_route(edge(end=(100, 50)), nodes)
        self.assertGreater(route[1][0], route[0][0])
        self.assertGreater(route[-2][0], route[-1][0])
        self.assert_clear_endpoints(route, nodes)

    def test_straight_style_preserves_straight_segment_and_ignores_bends(self):
        nodes = {"a": node("a", 0, 0), "b": node("b", 400, 200)}
        value = edge(connector_shape="straight", route=[{"x": 50, "y": 0},
                     {"x": -999, "y": -999}, {"x": 350, "y": 200}])
        self.assertEqual(estimated_miro_route(value, nodes), [(50, 0), (350, 200)])
        self.assertEqual(route_variants(value, nodes), [[(50, 0), (350, 200)]])

    def test_variants_keep_saved_detour_and_current_attachments_without_mutation(self):
        nodes = {"a": node("a", 400, 0), "b": node("b", 0, 200, "address")}
        value = edge(end=(100, 50), routing_exception="return", route=[
            {"x": 999, "y": 999}, {"x": 600, "y": 0}, {"x": 600, "y": -200},
            {"x": 100, "y": -200}, {"x": 100, "y": 200}, {"x": -999, "y": -999}])
        original = copy.deepcopy((value, nodes))
        variants = route_variants(value, nodes)
        self.assertEqual(len(variants), 2)
        self.assertEqual(variants[0][1], (600, 0))
        for route in variants:
            self.assertEqual(route[0], (450, 0))
            self.assertEqual(route[-1], (50, 200))
        self.assertEqual(variants, route_variants(value, dict(reversed(list(nodes.items())))))
        self.assertEqual((value, nodes), original)

    def test_variants_deduplicate_equivalent_saved_geometry(self):
        nodes = {"a": node("a", 0, 0), "b": node("b", 400, 200)}
        value = edge(route=[{"x": 50, "y": 0}, {"x": 100, "y": 0}, {"x": 200, "y": 0},
                            {"x": 200, "y": 200}, {"x": 350, "y": 200}])
        saved = [(point["x"], point["y"]) for point in value["route"]]
        self.assertEqual(route_variants(value, nodes), [saved])

    def test_saved_collinear_points_preserve_native_caption_signature_and_position(self):
        nodes = {"a": node("a", 0, 0), "b": node("b", 400, 200)}
        value = edge(label="vin 0", quantity="0.1 L-BTC", route=[
            {"x": 50, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 0},
            {"x": 200, "y": 0}, {"x": 200, "y": 200}, {"x": 350, "y": 200}])
        saved = [(point["x"], point["y"]) for point in value["route"]]
        size = caption_size(value)
        value["label_layout"] = {"x": 300, "y": -100, **size,
                                 "route_signature": route_signature(saved)}
        variants = route_variants(value, nodes)
        self.assertEqual(variants, [saved])
        self.assertEqual(route_signature(variants[0]), value["label_layout"]["route_signature"])
        self.assertEqual(caption_box(value, variants[0]),
                         (300, -100, 300 + size["width"], -100 + size["height"]))
        self.assertNotEqual(caption_box(value, variants[0]), caption_box(value, variants[0], use_layout=False))

    def test_snap_to_and_missing_attachments_use_perimeter_endpoints(self):
        nodes = {"a": node("a", 0, 0), "b": node("b", 400, 200)}
        value = edge(attachment={"startItem": {"snapTo": "right"}, "endItem": {"snapTo": "left"}})
        expected = estimated_miro_route(value, nodes)
        del value["attachment"]
        self.assertEqual(estimated_miro_route(value, nodes), expected)


if __name__ == "__main__":
    unittest.main()
