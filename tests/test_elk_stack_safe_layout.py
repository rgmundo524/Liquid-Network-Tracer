"""The bounded-stack traversal preserves the pinned engine's geometry."""

import copy
import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
HAS_ELK = bool(NODE and (ROOT / "layout/node_modules/elkjs/lib/elk-worker.js").is_file())
HARNESS = r"""
const fs = require('node:fs');
const ELK = require('elkjs/lib/elk-api.js');
const {Worker} = process.argv[1] === 'patched'
  ? require('./elk-stack-safe.cjs').loadWorker()
  : require('elkjs/lib/elk-worker.js');
const elk = new ELK({workerFactory: url => new Worker(url)});
async function main() {
  const graphs = JSON.parse(fs.readFileSync(0, 'utf8'));
  const results = [];
  for (const graph of graphs) results.push(await elk.layout(graph));
  process.stdout.write(JSON.stringify(results));
}
main().catch(error => {console.error(error); process.exitCode = 1;});
"""


def graph(name, pairs, *, isolated=(), ordered=False):
    ids = sorted({endpoint for pair in pairs for endpoint in pair} | set(isolated))
    options = {"elk.algorithm": "layered", "elk.direction": "RIGHT",
               "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
               "elk.layered.layering.strategy": "NETWORK_SIMPLEX",
               "elk.edgeRouting": "ORTHOGONAL", "elk.spacing.nodeNode": "80"}
    if ordered:
        options.update({"elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
                        "elk.layered.crossingMinimization.forceNodeModelOrder": "true",
                        "elk.separateConnectedComponents": "false"})
    return {"id": name, "layoutOptions": options,
            "children": [
                {"id": key, "width": 100 + index * 3, "height": 80 + index * 2,
                 "layoutOptions": {"elk.portConstraints": "FIXED_SIDE"},
                 "ports": [{"id": key + suffix, "width": 0, "height": 0,
                            "layoutOptions": {"elk.port.side": side}}
                           for suffix, side in (("_in", "WEST"), ("_out", "EAST"))]}
                for index, key in enumerate(ids)],
            "edges": [
                {"id": "edge_" + str(index), "sources": [source + "_out"],
                 "targets": [target + "_in"],
                 "layoutOptions": {"elk.layered.priority.straightness": str(index % 3 + 1)},
                 "labels": [{"id": "label_" + str(index), "text": "caption", "width": 42, "height": 12}]}
                for index, (source, target) in enumerate(pairs)]}


@unittest.skipUnless(HAS_ELK, "local ELK package is not installed")
class StackSafeLayoutTests(unittest.TestCase):
    def test_stock_and_iterative_engine_produce_identical_complete_layouts(self):
        cases = [
            graph("branching", [("a", "b"), ("a", "c"), ("b", "d"), ("b", "e"),
                                ("c", "f"), ("e", "g"), ("f", "g")]),
            graph("disconnected", [("a", "b"), ("b", "c"), ("d", "e"), ("d", "f")],
                  isolated=("g", "h")),
            graph("long_edges", [("a", "b"), ("b", "c"), ("c", "d"), ("d", "e"),
                                 ("e", "f"), ("a", "e"), ("a", "f"), ("b", "f")]),
            graph("centered_order", [("a", "c"), ("b", "c"), ("c", "d"), ("c", "e"),
                                     ("d", "f"), ("e", "g"), ("b", "g")], ordered=True),
        ]
        inputs = []
        for seed in (1, 7):
            for case in cases:
                request = copy.deepcopy(case)
                request["layoutOptions"]["elk.randomSeed"] = str(seed)
                inputs.append(request)
        results = {}
        # Separate processes avoid shared GWT globals between engine builds.
        for mode in ("stock", "patched"):
            run = subprocess.run([NODE, "-e", HARNESS, mode], input=json.dumps(inputs),
                                 cwd=ROOT / "layout", text=True, capture_output=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr)
            results[mode] = json.loads(run.stdout)
        self.assertEqual(len(results["patched"]), len(inputs))
        for request, original, patched in zip(inputs, results["stock"], results["patched"]):
            with self.subTest(graph=request["id"], seed=request["layoutOptions"]["elk.randomSeed"]):
                self.assertEqual(patched, original)
                self.assertTrue(all(edge.get("sections") for edge in patched["edges"]))


if __name__ == "__main__":
    unittest.main()
