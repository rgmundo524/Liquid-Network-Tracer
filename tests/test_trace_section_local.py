"""Compact section envelopes, using bounded synthetic geometry only."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.trace_section_geometry import assemble, assemble_gutters
from liquid_tracer.trace_section_local import boundary_requests, local_candidates
from liquid_tracer.trace_sections import SECTION_LAYOUT_VERSION, iter_candidates
from tests import test_trace_section_geometry as geometry_tests
from tests.test_trace_section_geometry import (candidates_for, request_for,
                                              points, boxes_overlap)
from tests.test_trace_sections import sections
from tests.test_trace_layout import fixture
from liquid_tracer.elk_layout import _apply_candidate, _request_graph


def prepared(request, groups, backbone=()):
    """Use the tested gutter primitive as a synthetic section worker."""
    lookup = {node["id"]: node for node in request["children"]}
    owner = {port["id"]: node["id"] for node in lookup.values() for port in node["ports"]}
    requests = [{"children": [lookup[key] for key in group], "edges": [edge for edge in request["edges"]
                 if owner[edge["sources"][0]] in group and owner[edge["targets"][0]] in group],
                 "layoutOptions": {}, "sectionLayout": True} for group in groups]
    requests = boundary_requests(request, groups, requests)
    candidates = []
    for item in requests:
        singleton = [[node["id"]] for node in item["children"]]
        core = set(backbone) & set(item["localSection"]["node_ids"])
        direct = set(request.get("backboneEdges", ())) & set(item["localSection"]["internal_edge_ids"])
        for key, proof in item["localSection"]["boundary_edges"].items():
            if proof["edge_id"] in request.get("backboneEdges", ()):
                core.add(proof["anchor"])
                direct.add(key)
        candidates.append(assemble_gutters({**item, "backboneEdges": direct}, singleton,
                                           candidates_for(item, singleton), core))
    local = local_candidates(request, requests, candidates, set(backbone), 17, {},
                             set(request.get("backboneEdges", ())))
    return {**request, "localSectionGeometry": True, "sectionRequests": requests}, local


class LocalSectionTests(unittest.TestCase):
    def assert_safe(self, request, result):
        geometry_tests.SectionGeometryTests().assert_safe(request, result)
        for i, a in enumerate(result["sectionGeometry"]["sections"]):
            for b in result["sectionGeometry"]["sections"][i + 1:]:
                self.assertFalse(boxes_overlap(a, b))

    def test_local_routes_ports_and_labels_are_translated_without_stretching(self):
        request = request_for({"a": 0, "b": 1, "c": 2, "d": 3}, [
            ("a", "b", "EAST", "WEST"), ("b", "c", "EAST", "WEST"), ("c", "d", "EAST", "WEST")])
        groups = [["a", "b"], ["c", "d"]]
        planned, candidates = prepared(request, groups)
        before = deepcopy((planned, candidates))
        result = assemble(planned, groups, candidates, [])
        self.assert_safe(request, result)
        self.assertEqual((planned, candidates), before)
        nodes = {node["id"]: node for node in result["nodes"]}
        edges = {edge["id"]: edge for edge in result["edges"]}
        for item, candidate in zip(planned["sectionRequests"], candidates):
            originals = {node["id"]: node for node in candidate["nodes"]}
            key = item["localSection"]["node_ids"][0]
            dx, dy = nodes[key]["x"] - originals[key]["x"], nodes[key]["y"] - originals[key]["y"]
            for key in item["localSection"]["node_ids"]:
                self.assertAlmostEqual(nodes[key]["x"] - originals[key]["x"], dx)
                self.assertAlmostEqual(nodes[key]["y"] - originals[key]["y"], dy)
                self.assertEqual(nodes[key]["ports"], originals[key]["ports"])
            for original in candidate["edges"]:
                if original["id"] not in item["localSection"]["internal_edge_ids"]:
                    continue
                for a, b in zip(points(original), points(edges[original["id"]])):
                    self.assertAlmostEqual(b["x"] - a["x"], dx)
                    self.assertAlmostEqual(b["y"] - a["y"], dy)
                for a, b in zip(original["labels"], edges[original["id"]]["labels"]):
                    self.assertAlmostEqual(b["x"] - a["x"], dx)
                    self.assertAlmostEqual(b["y"] - a["y"], dy)
        self.assertEqual(result["sectionGeometry"]["preserved_internal_edges"], 2)
        self.assertEqual(result["sectionGeometry"]["cross_section_edges"], 1)

    def test_unrelated_dense_section_does_not_expand_other_internal_distances(self):
        request = request_for({"a": 0, "b": 1, "x": 0, "y": 1}, [
            ("a", "b", "EAST", "WEST"), *[("x", "y", "EAST", "WEST")] * 20])
        groups = [["a", "b"], ["x", "y"]]
        results = []
        for width in (250, 6000):
            changed = deepcopy(request)
            for edge in changed["edges"][1:]:
                edge["labels"][0]["width"] = width
            planned, candidates = prepared(changed, groups)
            results.append(assemble(planned, groups, candidates, []))
        def relative(result):
            nodes = {node["id"]: node for node in result["nodes"]}
            route = points(result["edges"][0])
            return (nodes["b"]["x"] - nodes["a"]["x"], nodes["b"]["y"] - nodes["a"]["y"],
                    [(p["x"] - nodes["a"]["x"], p["y"] - nodes["a"]["y"]) for p in route])
        self.assertEqual(relative(results[0]), relative(results[1]))
        for result in results:
            self.assertEqual(len(result["nodes"]), 4)
            self.assertEqual(len(result["edges"]), 21)

    def test_local_caption_demand_does_not_expand_every_global_column(self):
        columns = {f"n{i}": i for i in range(30)} | {"x": 0, "y": 1}
        links = [(f"n{i}", f"n{i+1}", "EAST", "WEST") for i in range(29)]
        request = request_for(columns, links + [("x", "y", "EAST", "WEST")] * 12)
        for edge in request["edges"][29:]:
            edge["labels"][0]["width"] = 5000
        backbone = [f"n{i}" for i in range(30)]
        request["backboneEdges"] = {f"e{i}" for i in range(29)}
        groups = [backbone, ["x", "y"]]
        old = assemble_gutters(request, groups, candidates_for(request, groups), backbone)
        planned, candidates = prepared(request, groups, backbone)
        result = assemble(planned, groups, candidates, backbone)
        self.assertLess(result["width"], old["width"] / 3)
        self.assert_safe(request, result)

    def test_cross_section_return_and_shared_address_have_one_identity_and_safe_routes(self):
        request = request_for({"start": 0, "hub": 1, "a": 2, "b": 3}, [
            ("start", "hub", "EAST", "WEST"), ("hub", "a", "EAST", "WEST"),
            ("a", "b", "EAST", "WEST"), ("b", "hub", "EAST", "NORTH")])
        request["backboneEdges"] = {"e0", "e1", "e2"}
        groups = [["start", "hub"], ["a", "b"]]
        planned, candidates = prepared(request, groups, ["start", "hub", "a", "b"])
        result = assemble(planned, groups, candidates, ["start", "hub", "a", "b"])
        self.assert_safe(request, result)
        nodes = {node["id"]: node for node in result["nodes"]}
        self.assertLess(nodes["start"]["x"], nodes["hub"]["x"])
        self.assertLess(nodes["hub"]["x"], nodes["a"]["x"])
        self.assertLess(nodes["a"]["x"], nodes["b"]["x"])
        self.assertGreater(points(result["edges"][3])[0]["x"], points(result["edges"][3])[-1]["x"])

    def test_metadata_is_exact_deterministic_and_helpers_never_escape(self):
        request = request_for({"a": 0, "b": 1, "c": 2}, [
            ("a", "b", "EAST", "WEST"), ("b", "c", "EAST", "WEST")])
        groups = [["a", "b"], ["c"]]
        planned, candidates = prepared(request, groups)
        result = assemble(planned, groups, candidates, [])
        self.assertEqual(result, assemble(planned, groups, candidates, []))
        geometry = result["sectionGeometry"]
        self.assertEqual(geometry["version"], SECTION_LAYOUT_VERSION)
        self.assertEqual([section["node_ids"] for section in geometry["sections"]], groups)
        self.assertLessEqual(geometry["max_section_probes"], geometry["placement_probe_limit"])
        self.assertEqual({node["id"] for node in result["nodes"]}, {"a", "b", "c"})
        self.assertEqual({edge["id"] for edge in result["edges"]}, {"e0", "e1"})
        corrupt = deepcopy(candidates)
        corrupt[0]["edges"].pop()
        with self.assertRaises(TraceError):
            assemble(planned, groups, corrupt, [])

    def test_incompatible_rejoin_span_refines_locally_without_reversing_dependencies(self):
        request = request_for({"a": 0, "b": 1, "c": 2, "d": 3, "x": 1, "y": 2}, [
            ("a", "b", "EAST", "WEST"), ("b", "c", "EAST", "WEST"),
            ("c", "d", "EAST", "WEST"), ("a", "x", "EAST", "WEST"),
            ("x", "y", "EAST", "WEST"), ("y", "d", "EAST", "WEST")])
        request["edges"][4]["labels"][0]["width"] = 6000
        request["backboneEdges"] = {"e0", "e1", "e2"}
        backbone = ["a", "b", "c", "d"]
        groups = [backbone, ["x", "y"]]
        planned, candidates = prepared(request, groups, backbone)
        result = assemble(planned, groups, candidates, backbone)
        self.assertGreater(result["sectionGeometry"]["refinement_rounds"], 0)
        self.assertLessEqual(result["sectionGeometry"]["refinement_rounds"], 2)
        nodes = {node["id"]: node for node in result["nodes"]}
        owners = {port["id"]: node["id"] for node in request["children"] for port in node["ports"]}
        for edge in request["edges"]:
            self.assertLess(nodes[owners[edge["sources"][0]]]["x"], nodes[owners[edge["targets"][0]]]["x"])
        self.assert_safe(request, result)

    def test_tiny_worker_caps_fall_back_locally_without_oversized_worker(self):
        graph, _, _ = fixture()
        request, _, _ = _request_graph(graph)
        for cap in (1, 2):
            with self.subTest(cap=cap), patch("liquid_tracer.trace_sections.MAX_SECTION_NODES", cap), \
                    patch("liquid_tracer.trace_sections.iter_sections", side_effect=sections), \
                    patch("liquid_tracer.elk_layout._worker", side_effect=AssertionError("No oversized jobs")) as worker:
                result = list(iter_candidates(graph, request, [1], worker, lambda *_: lambda _: None, {}))[0][2][0]
            self.assertEqual(len(result["nodes"]), len(graph["nodes"]))
            self.assertEqual(result["sectionGeometry"]["local_fallback_sections"], len(graph["nodes"]))
            worker.assert_not_called()

    def test_wide_branch_cannot_reverse_a_real_return_connection(self):
        request = request_for({"a": 0, "b": 1, "c": 2, "d": 3, "x": 1, "y": 2}, [
            ("a", "b", "EAST", "WEST"), ("b", "c", "EAST", "WEST"),
            ("c", "d", "EAST", "WEST"), ("a", "x", "EAST", "WEST"),
            ("x", "y", "EAST", "WEST"), ("d", "y", "EAST", "NORTH")])
        request["edges"][4]["labels"][0]["width"] = 6000
        request["backboneEdges"] = {"e0", "e1", "e2"}
        backbone = ["a", "b", "c", "d"]
        groups = [backbone, ["x", "y"]]
        planned, candidates = prepared(request, groups, backbone)
        result = assemble(planned, groups, candidates, backbone)
        nodes = {node["id"]: node for node in result["nodes"]}
        self.assertLess(nodes["x"]["x"], nodes["y"]["x"])
        self.assertGreater(nodes["d"]["x"], nodes["y"]["x"])
        self.assertGreater(result["sectionGeometry"]["refinement_rounds"], 0)
        self.assert_safe(request, result)

    def test_shared_address_does_not_reverse_an_indirect_transaction_dependency(self):
        columns = {"tx:a": 0, "tx:b": 1, "tx:c": 2, "tx:d": 3, "tx:r": 0,
                   "tx:p": 1, "tx:t": 0, "hub": 3, "tx:q": 2}
        graph = {"nodes": [{"id": key, "kind": "address" if key == "hub" else "transaction",
                           "column": column, "x": column * 400, "y": 0,
                           "width": 100, "height": 100, "details": {}} for key, column in columns.items()],
                 "edges": [], "fee_items": {}, "graph_options": {"layout_style": "trace"}}
        links = [("tx:a", "tx:b"), ("tx:b", "tx:c"), ("tx:c", "tx:d"),
                 ("tx:r", "tx:p"), ("tx:t", "hub"), ("tx:p", "hub"), ("hub", "tx:q")]
        for index, (a, b) in enumerate(links):
            graph["edges"].append({"id": f"e{index}", "source": a, "target": b,
                "label": "W" * (100 if index == 3 else 200 if index == 4 else 1),
                "outpoint": "p:0" if index in (5, 6) else f"{a}:0",
                "role": "traced_input" if index == 6 else "candidate_output"})
        child = next(node for node in graph["nodes"] if node["id"] == "tx:q")
        child["details"] = {"transaction": {"vin": [{"txid": "p", "vout": 0}]}}
        request, ports, fees = _request_graph(graph)
        self.assertIn(("tx:p", "tx:q"), request["forwardNodePairs"])
        request["backboneEdges"] = {"e0", "e1", "e2"}
        groups = [["tx:a", "tx:b", "tx:c", "tx:d"], ["tx:r", "tx:p"], ["tx:t", "hub"], ["tx:q"]]
        planned, candidates = prepared(request, groups, groups[0])
        candidate = assemble(planned, groups, candidates, groups[0])
        result = _apply_candidate(graph, candidate, ports, fees, "elbowed")
        nodes = {node["id"]: node for node in result["nodes"]}
        self.assertLess(nodes["tx:p"]["x"], nodes["tx:q"]["x"])
        self.assertGreater(result["layout"]["section_geometry"]["refinement_rounds"], 0)
        self.assert_safe(request, candidate)


if __name__ == "__main__":
    unittest.main()
