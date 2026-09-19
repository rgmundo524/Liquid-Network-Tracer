"""Real ELK regressions for redundant caption padding and layout semantics."""

import copy
import unittest
from unittest.mock import patch

from liquid_tracer.elk_layout import optimize_graph, attachment_point
from liquid_tracer.edge_labels import caption_box, caption_size
from liquid_tracer.export import build_graph
from liquid_tracer.horizontal_spacing import HORIZONTAL_SPACING_VERSION
from liquid_tracer.layout_reuse import _fingerprint
from tests.test_elk_layout import HAS_ELK
from tests.test_layout import chain, state_from


def bounds(graph):
    nodes = graph["nodes"]
    return (min(n["x"] - n["width"] / 2 for n in nodes),
            min(n["y"] - n["height"] / 2 for n in nodes),
            max(n["x"] + n["width"] / 2 for n in nodes),
            max(n["y"] + n["height"] / 2 for n in nodes))


@unittest.skipUnless(HAS_ELK, "Install the pinned local ELK engine")
class HorizontalSpacingIntegrationTests(unittest.TestCase):
    def test_labeled_chain_shrinks_without_scaling_nodes_or_labels(self):
        graph = build_graph(state_from(chain(5)), merge_addresses=False)
        original = copy.deepcopy(graph)
        with patch("liquid_tracer.elk_layout.compact_candidate"):
            baseline = optimize_graph(graph, connector_style="elbowed", layout_attempts=3)
        result = optimize_graph(graph, connector_style="elbowed", layout_attempts=3)
        before, after = bounds(baseline), bounds(result)
        self.assertLess(after[2] - after[0], .85 * (before[2] - before[0]))
        self.assertEqual((after[1], after[3]), (before[1], before[3]))
        self.assertEqual(graph, original)
        self.assertEqual(_fingerprint(baseline), _fingerprint(result))
        self.assertEqual(result["layout"]["horizontal_spacing"]["version"], HORIZONTAL_SPACING_VERSION)
        nodes = {n["id"]: n for n in result["nodes"]}
        for edge in result["edges"]:
            source, target = nodes[edge["source"]], nodes[edge["target"]]
            gap = target["x"] - target["width"] / 2 - source["x"] - source["width"] / 2
            self.assertGreaterEqual(gap, 200 - 1e-6)
            box = caption_box(edge, [(p["x"], p["y"]) for p in edge["route"]])
            self.assertAlmostEqual(box[2] - box[0], caption_size(edge)["width"])
            self.assertGreater(box[0], source["x"] + source["width"] / 2)
            self.assertLess(box[2], target["x"] - target["width"] / 2)
            self.assertEqual(edge["route"][0], attachment_point(source, edge["attachment"]["startItem"]))
            self.assertEqual(edge["route"][-1], attachment_point(target, edge["attachment"]["endItem"]))

    def test_reused_address_returns_keep_topology_and_do_not_add_collisions(self):
        graph = build_graph(state_from(chain(5)), merge_addresses=True)
        with patch("liquid_tracer.elk_layout.compact_candidate"):
            baseline = optimize_graph(graph, connector_style="elbowed", layout_attempts=3)
        result = optimize_graph(graph, connector_style="elbowed", layout_attempts=3)
        self.assertEqual(_fingerprint(baseline), _fingerprint(result))
        before, after = bounds(baseline), bounds(result)
        self.assertLessEqual(after[2] - after[0], before[2] - before[0])
        before_metrics = baseline["layout"]["metrics"]["after"]
        after_metrics = result["layout"]["metrics"]["after"]
        for key in ("node_overlaps", "node_intersections", "crossings", "edge_length"):
            self.assertLessEqual(after_metrics[key], before_metrics[key] + 1e-6)
        self.assertTrue(any(edge["routing_exception"] == "return" for edge in result["edges"]))

