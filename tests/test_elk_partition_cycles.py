"""Cycle breaking and partition constraints must agree on column order."""

import copy
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from liquid_tracer.elk_layout import _apply_candidate, _request_graph


ROOT = Path(__file__).resolve().parents[1]
NODE = os.environ.get("LIQUID_NODE_BIN") or shutil.which("node")
HAS_ELK = bool(NODE and (ROOT / "layout/node_modules/elkjs/lib/elk-worker.js").is_file())
CYCLE = "elk.layered.cycleBreaking.strategy"
GROUP = "elk.layered.considerModelOrder.groupModelOrder.cycleBreakingId"
GROUP_ORDER = "elk.layered.considerModelOrder.groupModelOrder.cbGroupOrderStrategy"
PARTITION = "elk.partitioning.partition"


def cycle_graph():
    # Two cyclic regions connected across columns. Greedy reversal can point
    # their bridge backward, after which partition constraints create a cycle.
    pairs = [("a", "b"), ("b", "a"), ("b", "c"),
             ("c", "d"), ("c", "e"), ("c", "f"), ("c", "g"),
             ("d", "h"), ("e", "h"), ("f", "h"), ("g", "h"), ("h", "c")]
    return {
        "nodes": [{"id": key, "kind": "transaction", "column": int(key > "b"),
                   "x": 200 * int(key > "b"), "y": index * 100,
                   "width": 100, "height": 80, "label": "SYNTHETIC", "details": {}}
                  for index, key in enumerate("abcdefgh")],
        "edges": [{"id": f"e{index:02}", "source": source, "target": target, "label": "caption"}
                  for index, (source, target) in enumerate(pairs)],
        "fee_items": {}, "graph_options": {}, "presentation_version": 5,
    }


class PartitionCycleRequestTests(unittest.TestCase):
    def test_sparse_negative_columns_use_ordered_cycle_groups(self):
        graph = cycle_graph()
        for node in graph["nodes"]:
            node["column"] = -100 if node["column"] == 0 else 1000000
        original = copy.deepcopy(graph)
        request, _, _ = _request_graph(graph)
        self.assertEqual(graph, original)
        self.assertEqual(request["layoutOptions"][CYCLE], "MODEL_ORDER")
        self.assertEqual(request["layoutOptions"][GROUP_ORDER], "ENFORCED")
        self.assertEqual(request["layoutOptions"]["elk.partitioning.activate"], "true")
        self.assertNotIn("elk.layered.layering.strategy", request["layoutOptions"])
        for node in request["children"]:
            options = node["layoutOptions"]
            low = node["id"] in ("a", "b")
            self.assertEqual(options[PARTITION], "-100" if low else "1000000")
            self.assertEqual(options[GROUP], "0" if low else "1")

    def test_output_alignment_retains_its_interactive_strategies(self):
        graph = cycle_graph()
        graph["layout"] = {"output_alignment": {
            "columns": {node["id"]: index * 2 for index, node in enumerate(graph["nodes"])}}}
        request, _, _ = _request_graph(graph)
        options = request["layoutOptions"]
        self.assertEqual(options[CYCLE], "INTERACTIVE")
        self.assertEqual(options["elk.layered.layering.strategy"], "INTERACTIVE")
        self.assertEqual(options["elk.partitioning.activate"], "false")
        self.assertTrue(request["outputAlignmentSpacers"])
        self.assertEqual(set(request["outputAlignmentPositions"]),
                         {node["id"] for node in request["children"]})


@unittest.skipUnless(HAS_ELK, "Node and pinned ELK dependency are required")
class PartitionCycleLayoutTests(unittest.TestCase):
    def worker(self, request, seeds):
        return subprocess.run([str(NODE), "--max-old-space-size=512", str(ROOT / "layout/run.mjs")],
                              cwd=ROOT, input=json.dumps({"graph": request, "seeds": seeds}),
                              text=True, capture_output=True, timeout=60, check=False)

    def request(self):
        graph = cycle_graph()
        request, ports, fees = _request_graph(graph)
        request.update(branchProfile="flow_weighted", boundaryOrdering=False,
                       branchNodeOrder=list("abcdefgh"))
        return graph, request, ports, fees

    def assert_complete(self, graph, request, candidate, ports, fees):
        self.assertEqual({node["id"] for node in candidate["nodes"]}, set("abcdefgh"))
        self.assertEqual(len(candidate["nodes"]), 8)
        self.assertEqual(len(candidate["edges"]), 12)
        actual_edges = {edge["id"]: edge for edge in candidate["edges"]}
        self.assertEqual(set(actual_edges), {edge["id"] for edge in request["edges"]})
        positions = {}
        for node in candidate["nodes"]:
            for field in ("x", "y", "width", "height"):
                self.assertTrue(math.isfinite(node[field]))
            for port in node["ports"]:
                positions[port["id"]] = {"x": node["x"] + port["x"], "y": node["y"] + port["y"]}
        for expected in request["edges"]:
            actual = actual_edges[expected["id"]]
            self.assertEqual(len(actual["sections"]), 1)
            section = actual["sections"][0]
            for endpoint, port_key in (("startPoint", "sources"), ("endPoint", "targets")):
                for axis in ("x", "y"):
                    self.assertAlmostEqual(section[endpoint][axis], positions[expected[port_key][0]][axis])
            self.assertEqual(len(actual["labels"]), 1)
            label, requested = actual["labels"][0], expected["labels"][0]
            self.assertEqual(label["id"], requested["id"])
            self.assertEqual((label["width"], label["height"]), (requested["width"], requested["height"]))
            self.assertTrue(all(math.isfinite(label[axis]) for axis in ("x", "y")))
        # Exercise the application validation, not just ELK's success status.
        result = _apply_candidate(graph, candidate, ports, fees, "elbowed")
        self.assertEqual([(edge["id"], edge["source"], edge["target"]) for edge in result["edges"]],
                         [(edge["id"], edge["source"], edge["target"]) for edge in graph["edges"]])
        self.assertEqual(result["layout"]["edge_labels"]["reserved_count"], 12)
        columns = {node["id"]: node["column"] for node in graph["nodes"]}
        left = [node for node in candidate["nodes"] if columns[node["id"]] == 0]
        right = [node for node in candidate["nodes"] if columns[node["id"]] == 1]
        self.assertLess(max(node["x"] + node["width"] for node in left), min(node["x"] for node in right))

    def test_greedy_reproduces_label_index_failure_and_ordered_groups_recover(self):
        graph, request, ports, fees = self.request()
        request["layoutOptions"]["elk.layered.crossingMinimization.greedySwitch.type"] = "OFF"
        legacy = copy.deepcopy(request)
        legacy["layoutOptions"][CYCLE] = "GREEDY"
        legacy["layoutOptions"].pop(GROUP_ORDER)
        for node in legacy["children"]:
            node["layoutOptions"].pop(GROUP)
        failed = self.worker(legacy, [1])
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("elk_index_error", failed.stderr)
        self.assertIn("$ithDummyNode", failed.stderr)
        successful = self.worker(request, [1])
        self.assertEqual(successful.returncode, 0, successful.stderr + successful.stdout)
        candidates = json.loads(successful.stdout)["candidates"]
        self.assertEqual(len(candidates), 1)
        self.assert_complete(graph, request, candidates[0], ports, fees)

    def test_columns_and_captions_survive_seed_and_branch_order_variants(self):
        for order in (list("abcdefgh"), list("hgfedcba")):
            for boundary in (False, True):
                with self.subTest(order=order, boundary=boundary):
                    graph, request, ports, fees = self.request()
                    request.update(branchNodeOrder=order, boundaryOrdering=boundary)
                    run = self.worker(request, [1, 7])
                    self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
                    candidates = json.loads(run.stdout)["candidates"]
                    self.assertEqual([item["seed"] for item in candidates], [1, 7])
                    for candidate in candidates:
                        self.assert_complete(graph, request, candidate, ports, fees)


if __name__ == "__main__":
    unittest.main()
