import copy
import random
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.compaction import (COMPONENT_SPACING, EDGE_NODE_SPACING, LINKED_HORIZONTAL, NODE_SPACING,
                                      _Budget, _Index, _caption, _points, compact_graph)
from liquid_tracer.elk_layout import attachment_point, layout_metrics, optimize_graph
from liquid_tracer.export import build_graph
from liquid_tracer.layout_preview import render_svg
from tests.test_layout import state_from, txid
from tests.fixtures import output

ROOT = Path(__file__).resolve().parents[1]
HAS_ELK = bool(shutil.which("node") and (ROOT / "layout/node_modules/elkjs/package.json").is_file())


def node(key, kind, x, y=300, column=1):
    return {"id": key, "kind": kind, "x": x, "y": y, "width": 100, "height": 100,
            "column": column, "label": "SYNTHETIC " + key, "color": "#cceeff", "details": {}}


def edge(key, source, target, *, caption="vout 0", shape="straight"):
    return {"id": key, "source": source, "target": target, "label": caption,
            "quantity": "", "role": "traced_output", "connector_shape": shape,
            "attachment": {"startItem": {"position": {"x": "100%", "y": "50%"}},
                           "endItem": {"position": {"x": "0%", "y": "50%"}}}}


def graph(nodes=None, edges=None):
    result = {"nodes": nodes if nodes is not None else [node("t", "transaction", 0), node("a", "address", 1000, column=2)],
              "edges": edges if edges is not None else [edge("e", "t", "a")],
              "layout": {"algorithm": "elk_layered_v1", "main_top": 250, "main_bottom": 350,
                         "annotations": {"legend": {"x": 700, "y": -160}, "run": {"x": 700, "y": -480}}},
              "graph_options": {"connector_style": "straight"}, "fee_items": {},
              "run_id": "SYNTHETIC-compaction", "simulated": True, "notice": "Synthetic fixture",
              "connector_attachment": "transaction_ports_v2"}
    lookup = {item["id"]: item for item in result["nodes"]}
    for item in result["edges"]:
        points = _points(item, lookup)
        if item["connector_shape"] != "straight":
            first, last = points[0], points[-1]
            middle = (first[0] + last[0]) / 2
            points = [first, (middle, first[1]), (middle, last[1]), last]
        item["route"] = [{"x": x, "y": y} for x, y in points]
    return result


class CompactionTests(unittest.TestCase):
    def assert_ports(self, compacted):
        nodes = {node["id"]: node for node in compacted["nodes"]}
        for edge in compacted["edges"]:
            for endpoint, field, index in (("source", "startItem", 0), ("target", "endItem", -1)):
                expected = attachment_point(nodes[edge[endpoint]], edge["attachment"][field])
                self.assertAlmostEqual(edge["route"][index]["x"], expected["x"], places=4)
                self.assertAlmostEqual(edge["route"][index]["y"], expected["y"], places=4)

    def test_terminal_address_shrinks_without_changing_minimum_spacing_or_input(self):
        original = graph()
        before = copy.deepcopy(original)
        result = compact_graph(original)
        self.assertEqual(original, before)
        self.assertEqual(result["nodes"][1]["x"], 300)
        self.assertEqual(result["nodes"][0], original["nodes"][0])
        self.assertEqual(result["nodes"][1]["x"] - result["nodes"][0]["x"] - 100, LINKED_HORIZONTAL)
        report = result["layout"]["compaction"]
        self.assertLess(report["after"]["main"]["width"], report["before"]["main"]["width"])
        self.assertEqual(report["moved_addresses"], 1)
        self.assertEqual(result["layout"]["algorithm"], "elk_layered_v1")
        self.assert_ports(result)

    def test_external_input_moves_to_consumer_and_vertical_excess_is_reduced(self):
        original = graph([node("a", "address", 0, 1000), node("t", "transaction", 1000)], [edge("e", "a", "t")])
        result = compact_graph(original)
        moved = result["nodes"][0]
        self.assertEqual((moved["x"], moved["y"]), (700, 300))
        self.assertLess(result["layout"]["compaction"]["after"]["main"]["height"],
                        result["layout"]["compaction"]["before"]["main"]["height"])
        self.assert_ports(result)

    def test_intermediate_address_can_leave_its_elk_column(self):
        original = graph([node("t", "transaction", 0), node("a", "address", 1000, column=4),
                          node("u", "transaction", 2000, column=5)],
                         [edge("one", "t", "a"), edge("two", "a", "u")])
        result = compact_graph(original)
        self.assertEqual(result["nodes"][1]["x"], 300)
        self.assertEqual(result["nodes"][1]["column"], 4)
        self.assertEqual([result["nodes"][i] for i in (0, 2)], [original["nodes"][i] for i in (0, 2)])
        self.assertEqual(result["layout"]["compaction"]["before"]["main"]["edge_length"],
                         result["layout"]["compaction"]["after"]["main"]["edge_length"])
        self.assert_ports(result)

    def test_route_bends_repaired_and_tx_ports_preserved_for_elbows(self):
        original = graph([node("t", "transaction", 0), node("a", "address", 1000, 1000, 2)],
                         [edge("one", "t", "a", shape="elbowed")])
        result = compact_graph(original)
        self.assertGreater(result["layout"]["compaction"]["moved_addresses"], 0)
        self.assertEqual(result["edges"][0]["attachment"], original["edges"][0]["attachment"])
        self.assertEqual(result["edges"][0]["connector_shape"], "elbowed")
        self.assert_ports(result)
        render_svg(result)

    def test_address_does_not_jump_sibling_order_or_overlap_siblings(self):
        original = graph([node("t", "transaction", 0, 1000), node("a", "address", 1000, 300, 2),
                          node("b", "address", 1000, 600, 2), node("c", "address", 1000, 900, 2)],
                         [edge(key, "t", key, caption="") for key in ("a", "b", "c")])
        result = compact_graph(original)
        addresses = result["nodes"][1:]
        self.assertEqual([n["y"] for n in addresses], sorted(n["y"] for n in addresses))
        for i, first in enumerate(addresses):
            for second in addresses[i + 1:]:
                self.assertTrue(abs(first["x"] - second["x"]) >= 100 + NODE_SPACING
                                or abs(first["y"] - second["y"]) >= 100 + NODE_SPACING)
        self.assert_ports(result)

    def test_full_miro_caption_can_prevent_small_gap(self):
        original = graph(edges=[edge("e", "t", "a", caption="SYNTHETIC long " * 5)])
        result = compact_graph(original)
        moved = result["nodes"][1]
        self.assertGreater(moved["x"], 300)
        self.assertLess(moved["x"], 1000)
        nodes = {n["id"]: n for n in result["nodes"]}
        box = _caption(result["edges"][0], _points(result["edges"][0], nodes))
        self.assertGreaterEqual(box[0], 50)
        self.assertLessEqual(box[2], moved["x"] - 50)

    def test_existing_object_and_edge_channels_block_unsafe_move(self):
        original = graph([node("t", "transaction", 0), node("a", "address", 1000, column=2),
                          node("barrier", "transaction", 300)], [edge("e", "t", "a")])
        # The baseline edge already crosses the barrier. Compaction is not
        # allowed to use a truncated global count to bless a still-unsafe move.
        with patch("liquid_tracer.elk_layout.layout_metrics", return_value={"truncated": True, "crossings": 0}):
            result = compact_graph(original)
        self.assertEqual(result["nodes"][1], original["nodes"][1])
        self.assertEqual(result["edges"], original["edges"])

    def test_merged_returning_address_is_preserved(self):
        original = graph([node("t", "transaction", 0), node("u", "transaction", 1000),
                          node("a", "address", 500)], [edge("one", "t", "a"), edge("two", "u", "a")])
        result = compact_graph(original)
        self.assertEqual(result["nodes"], original["nodes"])
        self.assertTrue(result["layout"]["compaction"]["unchanged"])

    def test_components_translate_whole_trees_and_include_edge_labels(self):
        original = graph([node("t", "transaction", 0), node("a", "address", 300, column=2),
                          node("u", "transaction", 1800, 1800), node("b", "address", 2100, 1800, 2)],
                         [edge("one", "t", "a"), edge("two", "u", "b")])
        from liquid_tracer.miro_frames import activity_frames
        original["activity_frames"] = activity_frames(original)
        result = compact_graph(original)
        report = result["layout"]["compaction"]
        self.assertGreater(report["moved_components"], 0)
        self.assertLess(report["after"]["main"]["area"], report["before"]["main"]["area"])
        self.assertEqual(result["activity_frames"], original["activity_frames"])
        first, last = result["nodes"][2:]
        self.assertEqual(last["x"] - first["x"], 300)
        self.assertEqual(last["y"] - first["y"], 0)
        self.assertTrue(abs(first["y"] - result["nodes"][0]["y"]) >= 100 + COMPONENT_SPACING
                        or first["x"] - result["nodes"][1]["x"] >= 100 + COMPONENT_SPACING)
        self.assert_ports(result)

    def test_packed_activity_frames_have_room_for_their_titles(self):
        from liquid_tracer.miro_frames import activity_frames, frame_bodies
        original = graph([node("t", "transaction", 0), node("a", "address", 300, column=2),
                          node("u", "transaction", 0, 1800), node("b", "address", 300, 1800, 2)],
                         [edge("one", "t", "a"), edge("two", "u", "b")])
        original["activity_frames"] = activity_frames(original)
        result = compact_graph(original)
        frames = frame_bodies(result["activity_frames"], {n["id"]: (n["x"], n["y"], n["width"], n["height"]) for n in result["nodes"]})[1:]
        self.assertGreater(result["layout"]["compaction"]["moved_components"], 0)
        first, second = frames
        dy = abs(first["body"]["position"]["y"] - second["body"]["position"]["y"])
        gap = dy - (first["body"]["geometry"]["height"] + second["body"]["geometry"]["height"]) / 2
        self.assertGreaterEqual(gap, COMPONENT_SPACING)

    def test_moving_node_is_explicitly_checked_against_changed_caption(self):
        from liquid_tracer.compaction import _Geometry, _box, _touch
        original = graph([node("t", "transaction", 0, 0), node("a", "address", 1000, -180)],
                         [edge("one", "t", "a", caption="X" * 28)])
        for n in original["nodes"]:
            n.update(width=160, height=160)
        nodes = {n["id"]: n for n in original["nodes"]}
        edges = {e["id"]: e for e in original["edges"]}
        points = {key: _points(e, nodes) for key, e in edges.items()}
        geometry = _Geometry(nodes, edges, points, _Budget(10))
        nodes["a"]["x"] = 360
        proposed = {"one": _points(edges["one"], nodes)}
        self.assertTrue(_touch(_caption(edges["one"], proposed["one"]), _box(nodes["a"])))
        self.assertFalse(geometry.safe("a", proposed, (-1000, -1000, 2000, 2000)))

    def test_repaired_route_preserves_larger_elk_between_layer_clearance(self):
        from liquid_tracer.compaction import _Geometry
        original = graph([node("t", "transaction", 0), node("a", "address", 1000),
                          node("obstacle", "transaction", 500, 395)],
                         [edge("one", "t", "a", caption="")])
        nodes = {n["id"]: n for n in original["nodes"]}
        edges = {e["id"]: e for e in original["edges"]}
        points = {key: _points(e, nodes) for key, e in edges.items()}
        geometry = _Geometry(nodes, edges, points, _Budget(10))
        nodes["a"]["x"] = 700
        proposed = {"one": _points(edges["one"], nodes)}
        # The route would pass 45 units from the obstacle. That exceeds ELK's
        # same-layer 35 but violates its existing between-layer minimum of 60.
        with patch("liquid_tracer.compaction.EDGE_NODE_SPACING", 35):
            self.assertTrue(geometry.safe("a", proposed, (-50, 200, 1050, 450)))
        self.assertEqual(EDGE_NODE_SPACING, 60)
        self.assertFalse(geometry.safe("a", proposed, (-50, 200, 1050, 450)))

    def test_moved_node_preserves_larger_clearance_from_unchanged_route(self):
        from liquid_tracer.compaction import _Geometry
        original = graph([node("t", "transaction", 0), node("a", "address", 1000),
                          node("u", "transaction", -1000, 395), node("v", "transaction", 1500, 395)],
                         [edge("one", "t", "a", caption=""), edge("two", "u", "v", caption="")])
        nodes = {n["id"]: n for n in original["nodes"]}
        edges = {e["id"]: e for e in original["edges"]}
        points = {key: _points(e, nodes) for key, e in edges.items()}
        geometry = _Geometry(nodes, edges, points, _Budget(10))
        nodes["a"]["x"] = 300
        proposed = {"one": _points(edges["one"], nodes)}
        with patch("liquid_tracer.compaction.EDGE_NODE_SPACING", 35):
            self.assertTrue(geometry.safe("a", proposed, (-1050, 200, 1550, 450)))
        self.assertFalse(geometry.safe("a", proposed, (-1050, 200, 1550, 450)))

    def test_caption_reserves_miro_separator_when_quantity_is_empty(self):
        item = edge("one", "t", "a", caption="X")
        box = _caption(item, [(0, 0), (100, 0)])
        self.assertAlmostEqual(box[2] - box[0], len("X · ") * 11 * .8 + 12)
        self.assertEqual(compact_graph(graph())["layout"]["compaction"]["clearances"]["edge_node"], 60)

    def test_repaired_elbow_cannot_backtrack_over_its_own_line(self):
        from liquid_tracer.compaction import _Geometry
        original = graph(edges=[edge("one", "t", "a", caption="", shape="elbowed")])
        nodes = {n["id"]: n for n in original["nodes"]}
        edges = {e["id"]: e for e in original["edges"]}
        points = {key: _points(e, nodes) for key, e in edges.items()}
        geometry = _Geometry(nodes, edges, points, _Budget(10))
        nodes["a"]["x"] = 300
        proposed = {"one": [(50, 300), (240, 300), (180, 300), (250, 300)]}
        self.assertFalse(geometry.safe("a", proposed, (-50, 250, 1050, 350)))

    def test_metrics_describe_compacted_geometry_and_keep_original_elk_metrics(self):
        original = graph()
        original["layout"]["metrics"] = {"before": {"edge_length": 1234}, "after": layout_metrics(original)}
        result = compact_graph(original)
        self.assertEqual(result["layout"]["elk_metrics"], original["layout"]["metrics"])
        self.assertEqual(result["layout"]["metrics"]["after"], layout_metrics(result))
        self.assertLess(result["layout"]["metrics"]["after"]["edge_length"],
                        result["layout"]["metrics"]["before"]["edge_length"])

    def test_fee_chronological_row_and_incident_components_are_fixed(self):
        original = graph([node("t", "transaction", 0), node("a", "address", 1000, column=2),
                          node("u", "transaction", 2000, 2000), node("b", "address", 3000, 2000, 2),
                          node("f", "event", 100, -100), node("g", "event", 330, -100)],
                         [edge("one", "t", "a"), edge("two", "u", "b"),
                          edge("fee1", "t", "f", shape="elbowed"), edge("fee2", "u", "g", shape="elbowed")])
        original["fee_items"] = {key: {"endpoint": "shapes"} for key in ("f", "g")}
        for item in original["edges"][2:]:
            item["routing_exception"] = "fee"
        result = compact_graph(original)
        self.assertEqual(result["nodes"][-2:], original["nodes"][-2:])
        self.assertEqual(result["edges"][2:], original["edges"][2:])
        self.assertEqual(result["layout"]["compaction"]["moved_components"], 0)
        self.assertEqual(result["layout"]["compaction"]["fee_components_preserved"], 2)
        self.assert_ports(result)

    def test_deterministic_across_input_order_and_progress_failure(self):
        original = graph([node("t", "transaction", 0), node("a", "address", 1000, column=2),
                          node("u", "transaction", 1800, 1800), node("b", "address", 2800, 1800, 2)],
                         [edge("one", "t", "a"), edge("two", "u", "b")])
        shuffled = copy.deepcopy(original)
        random.Random(7).shuffle(shuffled["nodes"])
        random.Random(9).shuffle(shuffled["edges"])
        def fail(event):
            raise RuntimeError("synthetic sink unavailable")
        first, second = compact_graph(original), compact_graph(shuffled, progress=fail)
        self.assertEqual({n["id"]: (n["x"], n["y"]) for n in first["nodes"]},
                         {n["id"]: (n["x"], n["y"]) for n in second["nodes"]})
        self.assertEqual(first["layout"]["compaction"], second["layout"]["compaction"])

    def test_budget_exhaustion_preserves_complete_graph(self):
        original = graph()
        with patch("liquid_tracer.compaction._Budget.spend", return_value=False):
            result = compact_graph(original)
        self.assertEqual(result["nodes"], original["nodes"])
        self.assertEqual(result["edges"], original["edges"])
        self.assertTrue(result["layout"]["compaction"]["truncated"])

    def test_invalid_graph_rejected_and_no_new_worker_started(self):
        with self.assertRaises(TraceError):
            compact_graph({"layout": {"algorithm": "dependency_layers_v1"}})
        original = graph()
        original["edges"][0]["attachment"]["startItem"]["position"]["x"] = "invalid"
        with self.assertRaises(TraceError):
            compact_graph(original)

    def test_spatial_index_limits_dense_work_instead_of_returning_partial_matches(self):
        budget = _Budget(1)
        index = _Index(budget)
        for number in range(5000):
            index.add(str(number), (0, 0, 50, 50))
        self.assertIsNone(index.query((0, 0, 20, 20)))
        self.assertTrue(budget.truncated)

    def test_graph_above_old_object_limit_retains_every_item(self):
        count = 11000
        nodes = [node(str(i), "transaction", i * 300, 300) for i in range(count)]
        edges = [edge("e" + str(i), str(i), str(i + 1), caption="") for i in range(count - 1)]
        original = graph(nodes, edges)
        result = compact_graph(original)
        self.assertEqual(result["nodes"], original["nodes"])
        self.assertEqual(result["edges"], original["edges"])
        self.assertTrue(result["layout"]["compaction"]["unchanged"])

    @unittest.skipUnless(HAS_ELK, "Local ELK dependency is required")
    def test_real_elk_output_compacts_waste_and_remains_valid_miro_plan(self):
        transactions, used = {}, {}
        rng = random.Random(0)
        names = [txid(f"compact-probe-{i}") for i in range(8)]
        for i, key in enumerate(names):
            previous = []
            for j in range(i):
                if rng.random() < .35:
                    vout = used.get(j, 0)
                    used[j] = vout + 1
                    previous.append({"txid": names[j], "vout": vout,
                                     "prevout": output(f"SYNTHETIC-probe-{j}-{vout}")})
            transactions[key] = {"txid": key, "vin": previous, "vout": [], "status": {}}
        for i, key in enumerate(names):
            transactions[key]["vout"] = [output(f"SYNTHETIC-probe-{i}-{vout}") for vout in range(used.get(i, 0) + 1)]
        state = state_from(transactions)
        state["ancestor_runs"] = []
        original = optimize_graph(build_graph(state))
        result = compact_graph(original)
        report = result["layout"]["compaction"]
        self.assertGreater(report["accepted_moves"], 0)
        self.assertLess(report["after"]["main"]["address_distance"], report["before"]["main"]["address_distance"])
        for dimension in ("width", "height"):
            self.assertLessEqual(report["after"]["main"][dimension], report["before"]["main"][dimension])
        before_metrics, after_metrics = layout_metrics(original), layout_metrics(result)
        self.assertFalse(after_metrics["truncated"])
        for key in ("crossings", "node_overlaps", "node_intersections"):
            self.assertLessEqual(after_metrics[key], before_metrics[key])
        self.assert_ports(result)
        from liquid_tracer.miro import make_plan, validate_plan
        validate_plan(make_plan(result))
        render_svg(result)
