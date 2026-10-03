"""Address proximity through section assembly, with synthetic workers only."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from liquid_tracer.elk_layout import _apply_candidate, _request_graph, layout_metrics
from liquid_tracer.trace_section_geometry import assemble
from liquid_tracer.trace_sections import iter_candidates
from tests.test_elk_layout import synthetic_candidate
from tests.test_trace_sections import sections


def displaced_address_graph():
    # A local section puts the connecting address below BOTH of its TXs,
    # reproducing the reported placement without reading private case data.
    nodes = [("tx:parent", "transaction", 0), ("address", "address", 1),
             ("tx:child", "transaction", 2), ("side-a", "transaction", 0),
             ("side-b", "transaction", 0), ("side-c", "transaction", 0)]
    return {
        "nodes": [{"id": key, "kind": kind, "column": column,
                   "x": column * 500, "y": 0, "width": 160, "height": 160,
                   "details": {"fixture": key}} for key, kind, column in nodes],
        "edges": [
            {"id": "out:parent:1", "source": "tx:parent", "target": "address", "outpoint": "parent:1",
             "label": "vout 1", "quantity": "?? ??", "role": "candidate_output"},
            {"id": "in:child:0", "source": "address", "target": "tx:child", "outpoint": "parent:1",
             "label": "vin 0", "quantity": "?? ??", "role": "traced_input",
             "details": {"vin": {"txid": "parent", "vout": 1}, "validated_trace_link": {"outpoint": "parent:1",
                         "relationship": "observed_utxo_spend"}}}],
        "fee_items": {}, "graph_options": {"layout_style": "trace"},
    }


def section_worker(request, seeds):
    candidates = synthetic_candidate(request, seeds)
    hints = {"tx:parent": 0, "tx:child": 350, "side-a": 400, "side-b": 700,
             "side-c": 1000, "address": 1000}
    for candidate in candidates:
        ports = {}
        for node in candidate["nodes"]:
            node["y"] = hints.get(node["id"], 0)
            ports.update({port["id"]: {"x": node["x"] + port["x"],
                                      "y": node["y"] + port["y"]}
                          for port in node["ports"]})
        by_id = {edge["id"]: edge for edge in request["edges"]}
        for edge in candidate["edges"]:
            original = by_id[edge["id"]]
            edge["sections"] = [{"startPoint": ports[original["sources"][0]],
                                  "endPoint": ports[original["targets"][0]]}]
    return candidates


class AddressPlacementPipelineTests(unittest.TestCase):
    def test_distant_address_moves_before_routes_without_changing_evidence(self):
        graph = displaced_address_graph()
        request, ports, fees = _request_graph(graph)
        group = [node["id"] for node in graph["nodes"]]
        structure = {"spine": [], "edges": set(), "branches": [set(group)],
                     "excluded_hubs": set(), "shared_hubs": set()}
        original = deepcopy((graph, request, structure))
        old = assemble({**request, "nodeShapes": {n["id"]: n["kind"] for n in graph["nodes"]}},
                       [group], [section_worker(request, [1])[0]], [])
        old_nodes = {n["id"]: n for n in old["nodes"]}
        self.assertGreater(old_nodes["address"]["y"], old_nodes["tx:parent"]["y"])
        self.assertGreater(old_nodes["address"]["y"], old_nodes["tx:child"]["y"])
        metadata = {}
        with patch("liquid_tracer.trace_sections.iter_sections", side_effect=sections), \
             patch("liquid_tracer.elk_layout._worker", side_effect=AssertionError("No real ELK")):
            output = list(iter_candidates(graph, request, [1], section_worker,
                          lambda *_: lambda event: None, metadata, structure=structure))
        candidate = output[0][2][0]
        applied = _apply_candidate(graph, candidate, ports, fees, "elbowed")
        new_nodes = {n["id"]: n for n in applied["nodes"]}
        self.assertNotEqual(new_nodes["tx:parent"]["y"], new_nodes["tx:child"]["y"])
        self.assertGreaterEqual(new_nodes["address"]["y"],
                                min(new_nodes[k]["y"] for k in ("tx:parent", "tx:child")))
        self.assertLessEqual(new_nodes["address"]["y"],
                             max(new_nodes[k]["y"] for k in ("tx:parent", "tx:child")))
        self.assertEqual(candidate["sectionGeometry"]["address_placement"]["moved"], 1)
        self.assertEqual((graph, request, structure), original)
        self.assertEqual(len(applied["nodes"]), len(graph["nodes"]))
        self.assertEqual(len(applied["edges"]), len(graph["edges"]))
        for before, after in zip(graph["nodes"], applied["nodes"]):
            self.assertEqual({k: after[k] for k in before if k not in {"x", "y"}},
                             {k: v for k, v in before.items() if k not in {"x", "y"}})
        for before, after in zip(graph["edges"], applied["edges"]):
            self.assertEqual({k: after[k] for k in before}, before)
        metrics = layout_metrics(applied)
        self.assertEqual(metrics["node_overlaps"], 0)
        self.assertEqual(metrics["node_intersections"], 0)
        self.assertEqual(metrics["connector_overlaps"], 0)
        old_length = sum(abs(a[axis] - b[axis]) for edge in old["edges"]
                         for section in edge["sections"]
                         for a, b in zip([section["startPoint"], *section.get("bendPoints", [])],
                                         [*section.get("bendPoints", []), section["endPoint"]])
                         for axis in ("x", "y"))
        new_length = sum(abs(a[axis] - b[axis]) for edge in applied["edges"]
                         for a, b in zip(edge["route"], edge["route"][1:]) for axis in ("x", "y"))
        self.assertLess(new_length, old_length)


if __name__ == "__main__":
    unittest.main()
