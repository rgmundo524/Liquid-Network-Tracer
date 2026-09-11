import copy
import json
import os
import random
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from liquid_tracer.common import TraceError
from liquid_tracer.elk_layout import (_LayoutTimeout, _worker, attachment_point, fallback_graph, layout_metrics,
                                     optimize_graph, segment_hits_node, segments_cross)
from liquid_tracer.export import build_graph
from tests.fixtures import fixture
from tests.test_layout import chain, state_from, txid


ROOT = Path(__file__).resolve().parents[1]
HAS_ELK = bool(shutil.which("node") and (ROOT / "layout/node_modules/elkjs/package.json").is_file())


def point(x, y):
    return {"x": x, "y": y}


def crossing_graph():
    def node(key, kind, x, y, column):
        return {"id": key, "kind": kind, "x": x, "y": y, "width": 80, "height": 80,
                "column": column, "label": "SYNTHETIC", "details": {}}
    return {"nodes": [node("a", "transaction", 100, 100, 1), node("b", "transaction", 100, 300, 1),
                      node("c", "address", 460, 300, 2), node("d", "address", 460, 100, 2)],
            "edges": [{"id": "ac", "source": "a", "target": "c"},
                      {"id": "bd", "source": "b", "target": "d"}],
            "fee_items": {}, "graph_options": {}, "presentation_version": 5}


class LayoutGeometryTests(unittest.TestCase):
    def test_crossings_exclude_shared_ports_and_collinear_lines(self):
        self.assertTrue(segments_cross(point(0, 0), point(10, 10), point(0, 10), point(10, 0)))
        self.assertFalse(segments_cross(point(0, 0), point(10, 10), point(0, 0), point(10, 0)))
        self.assertFalse(segments_cross(point(0, 0), point(10, 0), point(4, 0), point(12, 0)))
        metrics = layout_metrics(crossing_graph())
        self.assertEqual(metrics["crossings"], 1)
        self.assertEqual(metrics["node_intersections"], 0)

    def test_node_intersections_respect_circle_diamond_and_rectangle_boundaries(self):
        node = {"id": "n", "x": 0, "y": 0, "width": 20, "height": 20}
        for kind in ("address", "event", "transaction"):
            node["kind"] = kind
            self.assertTrue(segment_hits_node(point(-20, 0), point(20, 0), node))
            self.assertFalse(segment_hits_node(point(-20, 20), point(20, 20), node))
        node["kind"] = "event"
        self.assertFalse(segment_hits_node(point(7, 7), point(12, 7), node))
        node["kind"] = "address"
        self.assertTrue(segment_hits_node(point(7, 7), point(12, 7), node))
        self.assertFalse(segment_hits_node(point(8, 8), point(12, 8), node))
        node["kind"] = "transaction"
        self.assertTrue(segment_hits_node(point(8, 8), point(12, 8), node))

    def test_crossing_at_a_route_bend_is_counted_and_shared_attachment_is_not(self):
        graph = crossing_graph()
        graph["edges"][0].update(connector_shape="elbowed", route=[point(140, 100), point(250, 200), point(420, 300)])
        # The second line crosses exactly at the first edge's bend.
        graph["nodes"][1].update(x=250, y=340)
        graph["nodes"][3].update(x=250, y=60)
        graph["edges"][1]["attachment"] = {
            "startItem": {"position": {"x": "50%", "y": "0%"}},
            "endItem": {"position": {"x": "50%", "y": "100%"}}}
        self.assertEqual(layout_metrics(graph)["crossings"], 1)
        graph = crossing_graph()
        graph["edges"][1]["source"] = "a"
        self.assertEqual(layout_metrics(graph)["crossings"], 0)

    def test_metrics_budget_includes_broadphase_rejections_and_marks_lower_bound(self):
        graph = crossing_graph()
        graph["nodes"] = []
        graph["edges"] = []
        for index in range(100):
            for key, x, kind in (("s", 0, "transaction"), ("t", 10000, "address")):
                graph["nodes"].append({"id": key + str(index), "x": x, "y": index * 200,
                                       "width": 80, "height": 80, "kind": kind})
            graph["edges"].append({"id": str(index), "source": "s" + str(index), "target": "t" + str(index)})
        metrics = layout_metrics(graph, max_comparisons=20)
        self.assertTrue(metrics["truncated"])
        self.assertEqual(metrics["comparisons"], 20)
        self.assertEqual(metrics["crossings"], 0)

    def test_invalid_geometry_and_missing_endpoints_fail_before_worker(self):
        for mutate in (lambda graph: graph["nodes"][0].update(x=float("nan")),
                       lambda graph: graph["edges"][0].update(target="missing")):
            graph = crossing_graph()
            mutate(graph)
            with patch("liquid_tracer.elk_layout._worker") as worker:
                with self.assertRaises(TraceError):
                    optimize_graph(graph)
                worker.assert_not_called()

    def test_invalid_worker_response_cannot_change_input_graph(self):
        graph = crossing_graph()
        before = copy.deepcopy(graph)
        with patch("liquid_tracer.elk_layout._worker", return_value=[{"seed": 1, "nodes": [], "edges": []}]):
            with self.assertRaisesRegex(TraceError, "objects"):
                optimize_graph(graph)
        self.assertEqual(graph, before)

    def test_worker_strips_credentials_and_runtime_injection_environment(self):
        process = Mock(returncode=0, pid=12345)
        process.communicate.return_value = (json.dumps({"version": "0.12.0", "candidates": [{}]}), "")
        process.poll.return_value = 0
        with patch.dict(os.environ, {"LIQUID_TRACER_ROOT": str(ROOT), "MIRO_ACCESS_TOKEN": "SYNTHETIC-secret",
                                     "BLOCKSTREAM_CLIENT_SECRET": "SYNTHETIC-secret", "NODE_OPTIONS": "--eval=bad",
                                     "LIQUID_NODE_BIN": "/synthetic/node"}), \
                patch("pathlib.Path.is_file", return_value=True), \
                patch("liquid_tracer.elk_layout.subprocess.Popen", return_value=process) as popen:
            self.assertEqual(_worker({}, [1]), [{}])
        env = popen.call_args.kwargs["env"]
        self.assertNotIn("MIRO_ACCESS_TOKEN", env)
        self.assertNotIn("BLOCKSTREAM_CLIENT_SECRET", env)
        self.assertNotIn("NODE_OPTIONS", env)
        self.assertEqual(popen.call_args.args[0][0], "/synthetic/node")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_timeout_kills_worker_process_group(self):
        process = Mock(returncode=None, pid=12345)
        process.communicate.side_effect = [subprocess.TimeoutExpired("node", 30), ("", "")]
        process.poll.return_value = None
        with patch.dict(os.environ, {"LIQUID_TRACER_ROOT": str(ROOT), "LIQUID_NODE_BIN": "/synthetic/node"}), \
                patch("pathlib.Path.is_file", return_value=True), \
                patch("liquid_tracer.elk_layout.subprocess.Popen", return_value=process), \
                patch("liquid_tracer.elk_layout.os.killpg") as kill:
            with self.assertRaisesRegex(TraceError, "30-second"):
                _worker({}, [1])
            kill.assert_called_once()
            self.assertEqual(kill.call_args.args[0], 12345)


class LargeGraphFallbackTests(unittest.TestCase):
    def test_actual_graph_above_10000_nodes_retains_every_relationship_without_worker(self):
        graph = build_graph(state_from(chain(5000)))
        self.assertEqual(len(graph["nodes"]), 10001)
        with patch("liquid_tracer.elk_layout._worker") as worker:
            result = optimize_graph(graph)
        worker.assert_not_called()
        self.assertEqual(result["nodes"], graph["nodes"])
        self.assertEqual({edge["id"]: (edge["source"], edge["target"], edge["outpoint"], edge["quantity"])
                          for edge in result["edges"]},
                         {edge["id"]: (edge["source"], edge["target"], edge["outpoint"], edge["quantity"])
                          for edge in graph["edges"]})
        self.assertEqual(result["layout"]["algorithm"], "dependency_layers_v1")
        self.assertEqual(result["layout"]["fallback_reason"], "size_limit")
        self.assertEqual(result["layout"]["placement"], "complete_graph_v1")
        self.assertIn("10,001 objects", result["layout"]["fallback_notice"])
        self.assertEqual(result["layout"]["metrics"]["candidate_count"], 0)
        self.assertNotIn("selected_seed", result["layout"]["metrics"])
        self.assertFalse(result["layout"]["crossing_optimization"])
        self.assertNotIn("fallback_reason", graph["layout"])
        self.assertTrue(all("attachment" not in edge for edge in graph["edges"]))

    def test_connection_size_limit_falls_back_but_validation_still_runs_first(self):
        graph = crossing_graph()
        with patch("liquid_tracer.elk_layout.MAX_EDGES", 1), \
                patch("liquid_tracer.elk_layout._worker") as worker:
            result = optimize_graph(graph)
            self.assertEqual(result["layout"]["fallback_reason"], "size_limit")
            graph["edges"][0]["target"] = "missing"
            with self.assertRaisesRegex(TraceError, "invalid graph connections"):
                optimize_graph(graph)
        worker.assert_not_called()

    def test_only_actual_worker_timeout_uses_fallback_and_progress_explains_it(self):
        graph = crossing_graph()
        before, progress = copy.deepcopy(graph), []
        with patch("liquid_tracer.elk_layout._worker", side_effect=_LayoutTimeout("timeout")):
            result = optimize_graph(graph, progress=progress.append)
        self.assertEqual(result["layout"]["fallback_reason"], "timeout")
        self.assertIn("full dependency layout", progress[-1]["message"])
        self.assertEqual(progress[-1]["completed"], 1)
        self.assertEqual(graph, before)
        with patch("liquid_tracer.elk_layout._worker", side_effect=TraceError("Missing dependency")):
            with self.assertRaisesRegex(TraceError, "Missing dependency"):
                optimize_graph(graph)

    def test_fallback_keeps_transaction_sides_fees_evidence_and_seed_colors(self):
        state = state_from({data["txid"]: data for key, data in fixture().items() if not key.endswith("outspends")})
        state["seeds"] = [key + ":0" for key in state["transactions"]]
        graph = build_graph(state, include_fees=True)
        before = copy.deepcopy(graph)
        result = fallback_graph(graph, connector_style="curved", reason="mermaid_timeout")
        self.assertEqual(result["nodes"], graph["nodes"])
        self.assertEqual(result["fee_items"], graph["fee_items"])
        self.assertEqual(result["layout"]["fee_row_y"], graph["layout"]["fee_row_y"])
        nodes = {node["id"]: node for node in result["nodes"]}
        transaction_ports = {}
        for original, edge in zip(graph["edges"], result["edges"]):
            self.assertEqual(original, {key: value for key, value in edge.items()
                                        if key not in ("attachment", "route", "connector_shape", "routing_exception")})
            for field, key, expected in (("startItem", edge["source"], "100%"), ("endItem", edge["target"], "0%")):
                node, port = nodes[key], edge["attachment"][field]
                if node["kind"] == "transaction":
                    self.assertEqual(port["position"]["x"], expected)
                    transaction_ports.setdefault((key, field), []).append(port["position"]["y"])
                elif node["kind"] == "address":
                    absolute = attachment_point(node, port)
                    radius = ((absolute["x"] - node["x"]) / (node["width"] / 2)) ** 2 + ((absolute["y"] - node["y"]) / (node["height"] / 2)) ** 2
                    self.assertAlmostEqual(radius, 1, places=6)
            self.assertEqual(edge["connector_shape"], "elbowed" if edge["routing_exception"] else "curved")
        self.assertTrue(any(len(set(values)) > 1 for values in transaction_ports.values()))
        self.assertEqual(graph, before)

    def test_merged_address_returns_are_routed_deterministically_without_reversing_transactions(self):
        graph = build_graph(state_from(chain(10)), merge_addresses=True)
        shuffled = copy.deepcopy(graph)
        random.Random(19).shuffle(shuffled["nodes"])
        random.Random(3).shuffle(shuffled["edges"])
        first, second = fallback_graph(graph), fallback_graph(shuffled)
        self.assertEqual({edge["id"]: (edge["attachment"], edge["route"]) for edge in first["edges"]},
                         {edge["id"]: (edge["attachment"], edge["route"]) for edge in second["edges"]})
        self.assertEqual({node["id"]: (node["x"], node["y"]) for node in first["nodes"]},
                         {node["id"]: (node["x"], node["y"]) for node in graph["nodes"]})
        returns = [edge for edge in first["edges"] if edge["routing_exception"] == "return"]
        self.assertTrue(returns)
        self.assertTrue(all(edge["connector_shape"] == "elbowed" and len(edge["route"]) > 2 for edge in returns))


@unittest.skipUnless(HAS_ELK, "Run liquid-layout-setup to install the pinned local ELK engine")
class RealElkTests(unittest.TestCase):
    def assert_topology_and_evidence_unchanged(self, before, after):
        before_copy, after_copy = copy.deepcopy(before), copy.deepcopy(after)
        for graph in (before_copy, after_copy):
            for key in ("layout", "connector_attachment", "presentation_version"):
                graph.pop(key, None)
            graph.get("graph_options", {}).pop("connector_style", None)
            for node in graph["nodes"]:
                for key in ("x", "y", "width", "height"):
                    node.pop(key, None)
            for edge in graph["edges"]:
                for key in ("attachment", "route", "connector_shape", "routing_exception"):
                    edge.pop(key, None)
        self.assertEqual(before_copy, after_copy)

    def test_real_elk_removes_crossing_without_mutating_graph(self):
        graph = crossing_graph()
        original = copy.deepcopy(graph)
        result = optimize_graph(graph)
        self.assertEqual(graph, original)
        self.assert_topology_and_evidence_unchanged(graph, result)
        self.assertEqual(result["layout"]["algorithm"], "elk_layered_v1")
        self.assertEqual(result["layout"]["metrics"]["before"]["crossings"], 1)
        self.assertEqual(result["layout"]["metrics"]["after"]["crossings"], 0)
        self.assertEqual(result["layout"]["metrics"]["after"]["node_overlaps"], 0)
        self.assertTrue(all(edge["connector_shape"] == "straight" for edge in result["edges"]))

    def test_related_provided_transactions_still_follow_dependency_order(self):
        state = state_from(chain(10))
        state["seeds"] = [key + ":0" for key in state["transactions"]]
        graph = build_graph(state)
        result = optimize_graph(graph)
        self.assert_topology_and_evidence_unchanged(graph, result)
        nodes = {node["id"]: node for node in result["nodes"]}
        positions = [nodes["tx:" + txid(index)]["x"] for index in range(10)]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(len(set(positions)), 10)
        self.assertEqual(result["layout"]["metrics"]["after"]["node_intersections"], 0)

    def test_split_join_ports_are_spread_and_fees_do_not_change_main_layout(self):
        state = state_from({data["txid"]: data for key, data in fixture().items() if not key.endswith("outspends")})
        hidden = optimize_graph(build_graph(state))
        shown_original = build_graph(state, include_fees=True)
        shown = optimize_graph(shown_original)
        self.assert_topology_and_evidence_unchanged(shown_original, shown)
        fee_ids = {key for key, value in shown["fee_items"].items() if value["endpoint"] == "shapes"}
        self.assertEqual({node["id"]: (node["x"], node["y"]) for node in hidden["nodes"]},
                         {node["id"]: (node["x"], node["y"]) for node in shown["nodes"] if node["id"] not in fee_ids})
        fees = sorted((node for node in shown["nodes"] if node["id"] in fee_ids), key=lambda node: node["x"])
        original_fees = sorted((node for node in shown_original["nodes"] if node["id"] in fee_ids), key=lambda node: node["x"])
        self.assertEqual([node["id"] for node in fees], [node["id"] for node in original_fees])
        self.assertTrue(all(node["y"] == shown["layout"]["fee_row_y"] for node in fees))
        self.assertLess(max(node["y"] + node["height"] / 2 for node in fees), shown["layout"]["main_top"])
        nodes = {node["id"]: node for node in hidden["nodes"]}
        tx_ports = {}
        for edge in hidden["edges"]:
            for endpoint, key, expected in (("startItem", edge["source"], "100%"), ("endItem", edge["target"], "0%")):
                node, port = nodes[key], edge["attachment"][endpoint]
                absolute = attachment_point(node, port)
                if node["kind"] == "transaction":
                    self.assertEqual(port["position"]["x"], expected)
                    tx_ports.setdefault((key, endpoint), []).append(port["position"]["y"])
                elif node["kind"] == "address":
                    radius = ((absolute["x"] - node["x"]) / (node["width"] / 2)) ** 2 + ((absolute["y"] - node["y"]) / (node["height"] / 2)) ** 2
                    self.assertAlmostEqual(radius, 1, places=6)
        self.assertTrue(any(len(set(values)) > 1 for values in tx_ports.values()))

    def test_merged_address_cycle_uses_elk_routes_while_transaction_order_stays_forward(self):
        graph = build_graph(state_from(chain(10)), merge_addresses=True)
        result = optimize_graph(graph)
        self.assert_topology_and_evidence_unchanged(graph, result)
        nodes = {node["id"]: node for node in result["nodes"]}
        positions = [nodes["tx:" + txid(index)]["x"] for index in range(10)]
        self.assertEqual(positions, sorted(positions))
        returns = [edge for edge in result["edges"] if edge["routing_exception"] == "return"]
        self.assertTrue(returns)
        self.assertTrue(all(edge["connector_shape"] == "elbowed" and len(edge["route"]) > 2 for edge in returns))
        self.assertLess(result["layout"]["metrics"]["after"]["node_intersections"],
                        result["layout"]["metrics"]["before"]["node_intersections"])

    def test_deterministic_positions_under_node_and_edge_order(self):
        graph = build_graph(state_from(chain(5)), merge_addresses=True)
        shuffled = copy.deepcopy(graph)
        random.Random(19).shuffle(shuffled["nodes"])
        random.Random(3).shuffle(shuffled["edges"])
        first, second = optimize_graph(graph), optimize_graph(shuffled)
        self.assertEqual({node["id"]: (node["x"], node["y"]) for node in first["nodes"]},
                         {node["id"]: (node["x"], node["y"]) for node in second["nodes"]})
        self.assertEqual({edge["id"]: edge["route"] for edge in first["edges"]},
                         {edge["id"]: edge["route"] for edge in second["edges"]})

    def test_curved_is_optional_while_returns_remain_routed(self):
        result = optimize_graph(build_graph(state_from(chain(3))), connector_style="curved")
        self.assertEqual(result["graph_options"]["connector_style"], "curved")
        self.assertTrue(all(edge["connector_shape"] == "curved" for edge in result["edges"]))


if __name__ == "__main__":
    unittest.main()
