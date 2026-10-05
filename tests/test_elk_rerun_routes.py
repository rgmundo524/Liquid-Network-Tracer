"""Fixed-input-order reruns must route from their own coordinates."""

import copy
import unittest

from liquid_tracer.elk_layout import _request_graph, _worker
from liquid_tracer.export import build_graph
from tests.test_elk_layout import HAS_ELK
from tests.test_input_order import child_input, input_order_state


@unittest.skipUnless(HAS_ELK, "Run liquid-layout-setup to install the pinned local ELK engine")
class ElkRerunRouteTests(unittest.TestCase):
    def candidates(self, count=4, continuing=(1, 3)):
        graph = build_graph(input_order_state(count, continuing=continuing))
        request, _, _ = _request_graph(graph)
        original = copy.deepcopy(request)
        candidates = _worker(request, [1], heap_mb=512)
        self.assertEqual(request, original)
        self.assertEqual([item["inputOrderPolicy"] for item in candidates],
                         ["geometry", "traced_first"])
        for candidate in candidates:
            self.assertEqual({node["id"] for node in candidate["nodes"]},
                             {node["id"] for node in request["children"]})
            self.assertEqual({edge["id"] for edge in candidate["edges"]},
                             {edge["id"] for edge in request["edges"]})
        return candidates

    def test_straight_rerun_does_not_keep_first_pass_bends(self):
        first, second = self.candidates()
        def section(candidate):
            return next(edge for edge in candidate["edges"]
                        if edge["id"] == child_input(3))["sections"][0]
        before, after = section(first), section(second)
        # This actual ELK fixture changes a dogleg into a straight connection.
        # The first candidate must keep its own bends for fair comparison.
        self.assertGreater(len(before["bendPoints"]), 0)
        self.assertNotEqual(before["startPoint"]["y"], before["endPoint"]["y"])
        self.assertEqual(after["startPoint"]["y"], after["endPoint"]["y"])
        self.assertEqual(after.get("bendPoints", []), [])

    def test_every_rerun_route_is_orthogonal_and_attached_to_current_ports(self):
        for count, continuing in ((4, (1, 3)), (6, (1, 3, 5)), (12, (2, 10))):
            with self.subTest(count=count):
                for candidate in self.candidates(count, continuing):
                    ports = {port["id"]: {"x": node["x"] + port["x"],
                                          "y": node["y"] + port["y"]}
                             for node in candidate["nodes"] for port in node["ports"]}
                    for edge in candidate["edges"]:
                        self.assertEqual(len(edge["sections"]), 1)
                        section = edge["sections"][0]
                        points = [section["startPoint"], *section.get("bendPoints", []),
                                  section["endPoint"]]
                        for point, field in ((points[0], "incomingShape"),
                                             (points[-1], "outgoingShape")):
                            for axis in ("x", "y"):
                                self.assertAlmostEqual(point[axis], ports[section[field]][axis])
                        for a, b in zip(points, points[1:]):
                            self.assertTrue(abs(a["x"] - b["x"]) < 1e-6
                                            or abs(a["y"] - b["y"]) < 1e-6,
                                            f"Stale diagonal in {candidate['inputOrderPolicy']} {edge['id']}")


if __name__ == "__main__":
    unittest.main()
