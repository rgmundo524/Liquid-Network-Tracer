import copy
import unittest

from liquid_tracer.common import TraceError
from liquid_tracer.trace_section_geometry import _color, assemble


def request_for(columns, links, *, sizes=None):
    sizes = sizes or {}
    children = [{"id": key, "width": sizes.get(key, (80, 60))[0], "height": sizes.get(key, (80, 60))[1],
                 "ports": [], "layoutOptions": {"elk.partitioning.partition": str(column)}}
                for key, column in columns.items()]
    lookup = {node["id"]: node for node in children}
    edges = []
    for index, (source, target, source_side, target_side) in enumerate(links):
        for key, suffix, side in ((source, "s", source_side), (target, "t", target_side)):
            lookup[key]["ports"].append({"id": f"p{index}{suffix}", "width": 0, "height": 0,
                                        "layoutOptions": {"elk.port.side": side}})
        edges.append({"id": f"e{index}", "sources": [f"p{index}s"], "targets": [f"p{index}t"],
                      "labels": [{"id": f"label:e{index}", "text": "caption", "width": 220 + index % 3 * 30,
                                  "height": 30 + index % 2 * 12}]})
    return {"children": children, "edges": edges}


def candidates_for(request, groups):
    lookup = {node["id"]: node for node in request["children"]}
    return [{"seed": 17, "nodes": [{"id": key, "y": index * 100, "height": lookup[key]["height"]}
                                     for index, key in enumerate(group)]}
            for group in groups]


def points(edge):
    section = edge["sections"][0]
    return [section["startPoint"], *section.get("bendPoints", []), section["endPoint"]]


def hits_interior(a, b, box):
    x, y, width, height = (box[key] for key in ("x", "y", "width", "height"))
    if a["x"] == b["x"]:
        return x < a["x"] < x + width and max(a["y"], b["y"]) > y and min(a["y"], b["y"]) < y + height
    return y < a["y"] < y + height and max(a["x"], b["x"]) > x and min(a["x"], b["x"]) < x + width


def boxes_overlap(a, b):
    return (max(a["x"], b["x"]) < min(a["x"] + a["width"], b["x"] + b["width"])
            and max(a["y"], b["y"]) < min(a["y"] + a["height"], b["y"] + b["height"]))


class SectionGeometryTests(unittest.TestCase):
    def assert_safe(self, request, result):
        node_ids = [node["id"] for node in result["nodes"]]
        self.assertEqual(set(node_ids), {node["id"] for node in request["children"]})
        self.assertEqual(len(node_ids), len(set(node_ids)))
        ports = {port["id"]: {"x": node["x"] + port["x"], "y": node["y"] + port["y"]}
                 for node in result["nodes"] for port in node["ports"]}
        self.assertEqual(set(ports), {port["id"] for node in request["children"] for port in node["ports"]})
        self.assertEqual([edge["id"] for edge in request["edges"]], [edge["id"] for edge in result["edges"]])
        labels = []
        for original, edge in zip(request["edges"], result["edges"]):
            route = points(edge)
            self.assertEqual(route[0], ports[original["sources"][0]])
            self.assertEqual(route[-1], ports[original["targets"][0]])
            for a, b in zip(route, route[1:]):
                self.assertTrue(a["x"] == b["x"] or a["y"] == b["y"], (edge["id"], a, b))
                for node in result["nodes"]:
                    self.assertFalse(hits_interior(a, b, node), (edge["id"], a, b, node["id"]))
            self.assertEqual(len(original.get("labels", [])), len(edge["labels"]))
            for expected, actual in zip(original.get("labels", []), edge["labels"]):
                self.assertEqual(expected, {key: actual[key] for key in expected})
                self.assertGreaterEqual(actual["x"], 0)
                self.assertGreaterEqual(actual["y"], 0)
                self.assertLessEqual(actual["x"] + actual["width"], result["width"])
                self.assertLessEqual(actual["y"] + actual["height"], result["height"])
                for node in result["nodes"]:
                    self.assertFalse(boxes_overlap(actual, node), (edge["id"], node["id"]))
                labels.append(actual)
        for index, a in enumerate(result["nodes"]):
            for b in result["nodes"][index + 1:]:
                self.assertFalse(boxes_overlap(a, b), (a["id"], b["id"]))
        for index, a in enumerate(labels):
            for b in labels[index + 1:]:
                self.assertFalse(boxes_overlap(a, b), (a["id"], b["id"]))

    def test_shared_join_return_and_terminal_ports_keep_exact_identity_and_clear_routes(self):
        request = request_for({"seed": -2, "hub": -1, "a": 0, "b": 0, "join": 1, "end": 2}, [
            ("seed", "hub", "EAST", "WEST"), ("hub", "a", "EAST", "WEST"),
            ("hub", "b", "EAST", "WEST"), ("a", "join", "EAST", "WEST"),
            ("b", "join", "EAST", "WEST"), ("join", "end", "EAST", "WEST"),
            ("join", "hub", "EAST", "NORTH"), ("b", "a", "SOUTH", "NORTH"),
            ("a", "a", "EAST", "NORTH"), ("end", "seed", "WEST", "SOUTH"),
        ], sizes={"hub": (120, 120), "a": (310, 90), "end": (100, 170)})
        request["children"][0]["ports"].append({"id": "unused", "layoutOptions": {"elk.port.side": "WEST"}})
        groups = [["seed", "hub"], ["a", "b"], ["join", "end"]]
        candidates = candidates_for(request, groups)
        before = copy.deepcopy((request, groups, candidates))
        result = assemble(request, groups, candidates, {"seed", "hub", "a", "join", "end"})
        self.assert_safe(request, result)
        self.assertEqual((request, groups, candidates), before)
        self.assertEqual(result["seed"], 17)
        centers = {node["y"] + node["height"] / 2 for node in result["nodes"] if node["id"] != "b"}
        self.assertEqual(len(centers), 1)

    def test_all_port_side_combinations_are_orthogonal_and_outside_nodes(self):
        sides = ("NORTH", "SOUTH", "EAST", "WEST")
        links = [("a", "b", source, target) for source in sides for target in sides]
        links += [("b", "a", source, target) for source in sides for target in sides]
        request = request_for({"a": 0, "b": 0, "obstacle": 1}, links)
        groups = [["a", "b", "obstacle"]]
        result = assemble(request, groups, candidates_for(request, groups), [])
        self.assert_safe(request, result)
        segments = [(edge["id"], a, b) for edge in result["edges"]
                    for a, b in zip(points(edge), points(edge)[1:])]
        for index, (edge, a, b) in enumerate(segments):
            for other, c, d in segments[index + 1:]:
                if edge == other:
                    continue
                for fixed, varying in (("x", "y"), ("y", "x")):
                    if a[fixed] == b[fixed] == c[fixed] == d[fixed]:
                        overlap = (min(max(a[varying], b[varying]), max(c[varying], d[varying]))
                                   - max(min(a[varying], b[varying]), min(c[varying], d[varying])))
                        self.assertLessEqual(overlap, 0, (edge, other, fixed))

    def test_same_column_backbone_members_cannot_overlap(self):
        request = request_for({"a": 0, "b": 0, "c": 1}, [("a", "c", "EAST", "WEST"),
                                                          ("b", "c", "EAST", "WEST")])
        request["centerNodeOrder"] = ["b", "a", "c"]
        groups = [["a"], ["b"], ["c"]]
        result = assemble(request, groups, candidates_for(request, groups), ["a", "b", "c"])
        self.assert_safe(request, result)
        positions = {node["id"]: node for node in result["nodes"]}
        self.assertEqual(positions["b"]["y"], positions["c"]["y"])
        self.assertGreater(positions["a"]["y"], positions["b"]["y"])

    def test_node_order_inside_a_section_follows_local_layout(self):
        request = request_for({"a": 0, "b": 0, "c": 0}, [])
        groups = [["a", "b", "c"]]
        candidates = candidates_for(request, groups)
        candidates[0]["nodes"][0]["y"] = 500
        result = assemble(request, groups, candidates, [])
        self.assertEqual([node["id"] for node in sorted(result["nodes"], key=lambda node: node["y"])], ["b", "c", "a"])

    def test_invalid_partitions_and_worker_identity_fail_closed(self):
        request = request_for({"a": 0, "b": 1}, [])
        for groups in ([["a", "a"]], [["a"]], [["a", "b", "unknown"]]):
            with self.subTest(groups=groups), self.assertRaises(TraceError):
                assemble(request, groups, [{"nodes": []}], [])
        with self.assertRaises(TraceError):
            assemble(request, [["a", "b"]], [{"nodes": [{"id": "a", "y": 0, "height": 60}]}], [])

    def test_interval_tracks_reuse_only_disjoint_spans(self):
        lanes, count = _color([(0, 3, "a"), (1, 2, "b"), (3, 4, "c"), (5, 6, "d")])
        self.assertEqual(count, 2)
        self.assertNotEqual(lanes["a"], lanes["b"])
        self.assertNotEqual(lanes["a"], lanes["c"])
        self.assertEqual(lanes["a"], lanes["d"])

    def test_primary_backbone_edges_are_straight_and_captioned(self):
        request = request_for({"seed": 0, "hub": 1, "next": 2, "branch": 2}, [
            ("seed", "hub", "EAST", "WEST"), ("hub", "next", "EAST", "WEST"),
            ("hub", "branch", "EAST", "WEST")], sizes={"hub": (120, 120)})
        groups = [["seed"], ["hub"], ["next", "branch"]]
        result = assemble(request, groups, candidates_for(request, groups), ["seed", "hub", "next"])
        self.assert_safe(request, result)
        for edge in result["edges"][:2]:
            route = points(edge)
            self.assertEqual(len(route), 2)
            self.assertEqual(route[0]["y"], route[1]["y"])
            self.assertLess(route[0]["x"], route[1]["x"])

    def test_only_verified_backbone_connection_gets_the_straight_lane(self):
        request = request_for({"a": 0, "b": 1}, [("a", "b", "EAST", "WEST"),
                                                ("a", "b", "EAST", "WEST")])
        request['backboneEdges'] = {'e1'}
        groups = [['a'], ['b']]
        result = assemble(request, groups, candidates_for(request, groups), ['a', 'b'])
        self.assertEqual(len(points(result['edges'][1])), 2)
        self.assertGreater(len(points(result['edges'][0])), 2)
        self.assert_safe(request, result)

    def test_shape_projection_survives_real_candidate_application(self):
        from liquid_tracer.elk_layout import _apply_candidate, segment_hits_node
        request = request_for({"a": 0, "hub": 1, "end": 2}, [
            ("a", "hub", "EAST", "WEST"), ("a", "hub", "EAST", "WEST"),
            ("hub", "end", "EAST", "WEST"), ("end", "hub", "SOUTH", "NORTH"),
            ("hub", "end", "SOUTH", "NORTH")], sizes={"hub": (120, 120), "end": (140, 70)})
        request["nodeShapes"] = {"a": "transaction", "hub": "address", "end": "event"}
        graph = {"nodes": [{"id": node["id"], "kind": request["nodeShapes"][node["id"]],
                            "x": 0, "y": 0, "width": node["width"], "height": node["height"],
                            "column": int(node["layoutOptions"]["elk.partitioning.partition"]), "details": {}}
                           for node in request["children"]],
                 "edges": [], "fee_items": {}, "graph_options": {}}
        owners = {port["id"]: node["id"] for node in request["children"] for port in node["ports"]}
        port_map = {}
        for edge in request["edges"]:
            edge.pop("labels")
            source, target = edge["sources"][0], edge["targets"][0]
            port_map[edge["id"]] = [source, target]
            graph["edges"].append({"id": edge["id"], "source": owners[source], "target": owners[target]})
        groups = [["a", "hub", "end"]]
        result = assemble(request, groups, candidates_for(request, groups), ["a"])
        applied = _apply_candidate(graph, result, port_map, set(), "elbowed")
        for edge in applied["edges"]:
            for a, b in zip(edge["route"], edge["route"][1:]):
                # The adapter rounds raw coordinates and percentage anchors;
                # permit only that subpixel precision, not a diagonal stub.
                self.assertLess(min(abs(a["x"] - b["x"]), abs(a["y"] - b["y"])), .001)
                for node in applied["nodes"]:
                    if node["id"] not in (edge["source"], edge["target"]):
                        self.assertFalse(segment_hits_node(a, b, node))

    def test_large_chain_uses_bounded_route_segments_without_recursion(self):
        count = 1500
        request = request_for({str(index): index for index in range(count)}, [
            (str(index), str(index + 1), "EAST", "WEST") for index in range(count - 1)])
        groups = [[str(index) for index in range(start, min(start + 50, count))]
                  for start in range(0, count, 50)]
        result = assemble(request, groups, candidates_for(request, groups), list(map(str, range(count))))
        self.assertEqual(len(result["nodes"]), count)
        self.assertEqual(len(result["edges"]), count - 1)
        self.assertLessEqual(max(len(points(edge)) for edge in result["edges"]), 6)
        self.assertEqual(result["sectionGeometry"]["rows"], 1)


if __name__ == "__main__":
    unittest.main()
