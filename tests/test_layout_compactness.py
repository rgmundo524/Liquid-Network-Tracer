"""Trace compactness is measurable and never buys worse path clarity."""

import copy
import math
import random
import unittest
from unittest.mock import patch

from liquid_tracer.elk_layout import _candidate_score, optimize_graph
from liquid_tracer.layout_compactness import compactness_metrics


def graph_fixture():
    return {
        "nodes": [
            {"id": "source", "kind": "transaction", "column": 0, "x": 0, "y": 0,
             "width": 100, "height": 100, "details": {"evidence": "source"}},
            {"id": "target", "kind": "transaction", "column": 1, "x": 1000, "y": 0,
             "width": 100, "height": 100, "details": {"evidence": "target"}}],
        "edges": [{"id": "connection", "source": "source", "target": "target",
                   "outpoint": "source:0", "details": {"evidence": "connection"}}],
        "graph_options": {"layout_style": "trace"}, "fee_items": {},
    }


def score(compactness, *, standard=False, overlaps=0, intersections=0, shared=0,
          coincident=0, inversions=0, drift=0, crossings=0, truncated=False,
          miro_crossings=None, named_alignment=0, boundary_interleavings=0,
          flow_inversions=0, sibling_interleavings=0):
    metrics = dict(node_overlaps=overlaps, node_intersections=intersections,
                   crossings=crossings, connector_overlaps=shared, edge_length=100,
                   truncated=truncated)
    return _candidate_score(
        metrics, {**metrics, "crossings": crossings if miro_crossings is None else miro_crossings},
        dict(endpoint_order_inversions=inversions, coincident_ports=coincident),
        dict(weighted_vertical_travel=0),
        dict(flow_order_inversions=flow_inversions, sibling_interleavings=sibling_interleavings,
             transaction_distance=0, transaction_center_drift=0),
        dict(interleavings=boundary_interleavings, boundary_depth=0, interbranch_travel=0),
        dict(alignment_deviation=named_alignment, center_offset=0), "traced_first",
        trace=dict(enabled=not standard, edge_count=10, spine_alignment=drift,
                   terminal_distance=0, branch_interleaving=0), compactness=compactness)


class CompactnessMetricsTests(unittest.TestCase):
    def test_canvas_area_density_and_attached_direct_length(self):
        graph = graph_fixture()
        original = copy.deepcopy(graph)
        measured = compactness_metrics(graph)
        self.assertEqual(measured["canvas_width"], 1100)
        self.assertEqual(measured["canvas_height"], 100)
        self.assertEqual(measured["canvas_area"], 110000)
        self.assertEqual(measured["node_box_area"], 20000)
        self.assertEqual(measured["area_per_node_area"], 5.5)
        self.assertAlmostEqual(measured["node_box_density"], 1 / 5.5, places=6)
        # Perimeter-to-perimeter length is 900; center distance would be 1000.
        self.assertEqual(measured["connector_length_p95"], 900)
        self.assertEqual(measured["connector_length_p95_normalized"], 9)
        self.assertEqual(measured["connector_detour_mean_normalized"], 0)
        self.assertEqual(graph, original)

    def test_visible_caption_bounds_and_routes_contribute_to_footprint(self):
        graph = graph_fixture()
        baseline = compactness_metrics(graph)
        edge = graph["edges"][0]
        edge.update(connector_shape="elbowed", route=[{"x": 50, "y": 0},
                    {"x": 50, "y": 500}, {"x": 950, "y": 500}, {"x": 950, "y": 0}])
        routed = compactness_metrics(graph)
        self.assertEqual(routed["canvas_height"], 550)
        self.assertEqual(routed["connector_length_p95"], 1900)
        self.assertEqual(routed["connector_detour_p95_normalized"], 10)
        edge["label"] = "W" * 200
        labeled = compactness_metrics(graph)
        self.assertGreater(labeled["canvas_width"], routed["canvas_width"])
        edge["caption_display"] = "details_only"
        self.assertEqual(compactness_metrics(graph), routed)
        edge["connector_shape"] = "straight"
        self.assertEqual(compactness_metrics(graph), baseline)

    def test_normalized_metrics_ignore_translation_scale_and_collection_order(self):
        graph = graph_fixture()
        graph["edges"].append({**copy.deepcopy(graph["edges"][0]), "id": "second"})
        expected = compactness_metrics(graph)
        translated = copy.deepcopy(graph)
        for node in translated["nodes"]:
            node.update(x=node["x"] * 2 + 2345, y=node["y"] * 2 - 6789,
                        width=node["width"] * 2, height=node["height"] * 2)
        random.Random(17).shuffle(translated["nodes"])
        random.Random(11).shuffle(translated["edges"])
        measured = compactness_metrics(translated)
        for field in ("area_per_node_area", "node_box_density", "connector_length_p95_normalized",
                      "connector_detour_mean_normalized", "connector_detour_p95_normalized"):
            self.assertEqual(measured[field], expected[field])
        self.assertEqual(measured["canvas_area"], expected["canvas_area"] * 4)
        self.assertEqual(measured["connector_length_p95"], expected["connector_length_p95"] * 2)

    def test_empty_coincident_endpoints_and_ignored_fee_row_are_finite(self):
        empty = compactness_metrics({"nodes": [], "edges": []})
        self.assertTrue(all(value == 0 for key, value in empty.items() if key != "version"))
        graph = graph_fixture()
        graph["nodes"][1]["x"] = 100  # Both attachment endpoints are x=50.
        measured = compactness_metrics(graph)
        self.assertEqual(measured["connector_length_p95"], 0)
        self.assertTrue(all(math.isfinite(value) for value in measured.values()))
        graph["nodes"].append({"id": "fee", "kind": "event", "column": 0, "x": 99999,
                               "y": -99999, "width": 100, "height": 100})
        graph["edges"].append({"id": "fee-edge", "source": "source", "target": "fee"})
        graph["fee_items"] = {"fee": {"endpoint": "shapes"}, "fee-edge": {"endpoint": "connectors"}}
        self.assertEqual(compactness_metrics(graph), measured)

    def test_length_percentile_exposes_long_tail_instead_of_only_average(self):
        graph = graph_fixture()
        graph["nodes"] = []
        graph["edges"] = []
        for index in range(1, 21):
            graph["nodes"].extend([
                {"id": f"a{index}", "kind": "transaction", "x": 0, "y": index * 300,
                 "width": 20, "height": 20},
                {"id": f"b{index}", "kind": "transaction", "x": index * 100, "y": index * 300,
                 "width": 20, "height": 20}])
            graph["edges"].append({"id": str(index), "source": f"a{index}", "target": f"b{index}"})
        measured = compactness_metrics(graph)
        self.assertEqual(measured["connector_length_p95"], 1880)
        self.assertEqual(measured["connector_length_p95_normalized"], 94)


class CompactnessSelectionTests(unittest.TestCase):
    def setUp(self):
        self.compact = compactness_metrics(graph_fixture())
        wide = graph_fixture()
        wide["nodes"][1]["x"] = 100000
        self.wide = compactness_metrics(wide)

    def test_equal_clarity_prefers_smaller_canvas_but_standard_is_unchanged(self):
        self.assertLess(score(self.compact), score(self.wide))
        self.assertEqual(score(self.compact, standard=True), score(self.wide, standard=True))

    def test_equal_area_uses_detour_and_long_connector_tail_to_break_ties(self):
        detoured = {**self.compact, "connector_detour_p95_normalized": 5}
        longer = {**self.compact, "connector_length_p95_normalized": 100}
        self.assertLess(score(self.compact), score(detoured))
        self.assertLess(score(self.compact), score(longer))

    def test_compactness_cannot_buy_collisions_shared_arrows_or_worse_path_clarity(self):
        for loss in (dict(overlaps=1), dict(intersections=1), dict(shared=1),
                     dict(coincident=1), dict(inversions=1), dict(drift=.01),
                     dict(crossings=1), dict(truncated=True)):
            with self.subTest(loss=loss):
                self.assertLess(score(self.wide), score(self.compact, **loss))

    def test_raw_crossings_and_individual_order_clarity_precede_canvas_area(self):
        for loss in (dict(named_alignment=.1), dict(boundary_interleavings=1),
                     dict(flow_inversions=1), dict(sibling_interleavings=1)):
            with self.subTest(loss=loss):
                self.assertLess(score(self.wide), score(self.compact, **loss))
        # Compensate for crossing cost with a straighter path, keeping the
        # existing aggregate readability exactly equal. Raw crossing ties
        # must still be protected before considering the smaller canvas.
        for loss in (dict(crossings=1, miro_crossings=0),
                     dict(crossings=0, miro_crossings=1)):
            with self.subTest(loss=loss):
                self.assertLess(score(self.wide, drift=1),
                                score(self.compact, drift=.95, **loss))

    def test_omitted_compactness_retains_historical_trace_score(self):
        self.assertEqual(score(None, drift=1, crossings=2),
                         (False, 0, 0, 0, 0, 0, 4.8, 0, 2, 2, 0, 0, 0, 0, 0,
                          0, 0, 0, 0, 0, 0, 0, 0, 100, 100, False))

    def test_selected_and_candidate_metrics_are_saved_without_evidence_changes(self):
        graph = graph_fixture()
        graph["edges"] = []  # Isolate equal-clarity candidates with differing spacing.
        original = copy.deepcopy(graph)
        def candidate(seed, spacing):
            nodes = [{"id": node["id"], "x": index * spacing, "y": 0,
                      "width": 100, "height": 100, "ports": []}
                     for index, node in enumerate(graph["nodes"])]
            return {"seed": seed, "nodes": nodes, "edges": [], "inputOrderPolicy": "geometry"}
        candidates = [(1, 1, [candidate(1, 1000)]), (2, 7, [candidate(7, 300)])]
        with patch("liquid_tracer.elk_layout.iter_attempts", return_value=iter(candidates)), \
                patch("liquid_tracer.elk_layout.compact_candidate"):
            result = optimize_graph(graph, "elbowed", layout_attempts=2)
        self.assertEqual(graph, original)
        self.assertEqual(result["layout"]["search"]["selected_seed"], 7)
        reports = result["layout"]["search"]["compactness_candidates"]
        self.assertEqual([(item["seed"], item["selected"]) for item in reports], [(1, False), (7, True)])
        self.assertLess(reports[1]["metrics"]["canvas_area"], reports[0]["metrics"]["canvas_area"])
        self.assertEqual(result["layout"]["compactness"], compactness_metrics(result))
        self.assertEqual({node["id"]: node["details"] for node in result["nodes"]},
                         {node["id"]: node["details"] for node in original["nodes"]})
        self.assertEqual(result["edges"], original["edges"])


if __name__ == "__main__":
    unittest.main()
