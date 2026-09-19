"""The worker retains crossing-aware geometry before the priority rerun mutates it."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

# A mutating layout engine reproduces the ownership risk when a previous ELK
# result is reused as its next request. All coordinates identify their call.
MUTATING_ELK = """
module.exports = class ELK {
  constructor() { this.calls = 0; }
  async layout(graph) {
    const stamp = ++this.calls * 100;
    const node = graph.children[0];
    Object.assign(node, {x: stamp, y: 0});
    const fixed = node.layoutOptions['elk.portConstraints'] === 'FIXED_ORDER';
    for (let i = 0; i < node.ports.length; ++i) {
      const port = node.ports[i];
      port.x = 0;
      port.y = fixed ? 100 - 20 * Number(port.layoutOptions['elk.port.index']) : 20 + 20 * i;
    }
    const edge = graph.edges[0];
    edge.sections ??= [{id: 'section', startPoint: {}, endPoint: {}, bendPoints: [{}]}];
    Object.assign(edge.sections[0].startPoint, {x: stamp, y: 1});
    Object.assign(edge.sections[0].endPoint, {x: stamp + 2, y: 3});
    Object.assign(edge.sections[0].bendPoints[0], {x: stamp + 4, y: 5});
    edge.labels ??= [{id: 'label', text: 'private caption'}];
    Object.assign(edge.labels[0], {x: stamp, y: 7, width: 20, height: 10});
    return graph;
  }
};
"""


def request_graph(west=("w1", "w0")):
    return {"id": "root", "branchOrganization": 1, "layoutOptions": {},
            "inputPortOrders": {"transaction": list(west)},
            "children": [{"id": "transaction", "width": 160, "height": 160,
                          "layoutOptions": {}, "private": "not output",
                          "ports": [{"id": name, "layoutOptions": {"elk.port.side": side}}
                                    for name, side in (("w0", "WEST"), ("w1", "WEST"), ("e0", "EAST"))]}],
            "edges": [{"id": "edge", "sources": ["w0"], "targets": ["e0"],
                       "layoutOptions": {"elk.layered.priority.straightness": "8"}}]}


@unittest.skipUnless(NODE, "Node is not installed")
class WorkerPortCandidateTests(unittest.TestCase):
    def run_worker(self, graph, seeds):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker = root / "run.mjs"
            shutil.copyfile(ROOT / "layout" / "run.mjs", worker)
            module = root / "node_modules" / "elkjs" / "lib" / "elk.bundled.js"
            module.parent.mkdir(parents=True)
            module.write_text(MUTATING_ELK)
            result = subprocess.run([NODE, str(worker)],
                                    input=json.dumps({"graph": graph, "seeds": seeds}),
                                    text=True, capture_output=True, timeout=15, check=True)
        return json.loads(result.stdout)["candidates"]

    def test_first_geometry_survives_mutating_rerun_without_evidence(self):
        geometric, forced = self.run_worker(request_graph(), [1])
        self.assertEqual([geometric["inputOrderPolicy"], forced["inputOrderPolicy"]],
                         ["geometry", "traced_first"])
        for candidate, stamp in ((geometric, 100), (forced, 200)):
            self.assertEqual(candidate["nodes"][0]["x"], stamp)
            self.assertEqual(candidate["edges"][0]["sections"][0]["startPoint"]["x"], stamp)
            self.assertEqual(candidate["edges"][0]["sections"][0]["bendPoints"][0]["x"], stamp + 4)
            self.assertEqual(candidate["edges"][0]["labels"][0]["x"], stamp)
            self.assertNotIn("private", candidate["nodes"][0])
            self.assertNotIn("layoutOptions", candidate["nodes"][0])
            self.assertNotIn("text", candidate["edges"][0]["labels"][0])
        def order(candidate):
            ports = candidate["nodes"][0]["ports"]
            return [port["id"] for port in sorted(ports, key=lambda item: item["y"])
                    if port["id"].startswith("w")]
        self.assertEqual(order(geometric), ["w0", "w1"])
        self.assertEqual(order(forced), ["w1", "w0"])

    def test_satisfied_order_keeps_one_candidate_and_one_layout_per_seed(self):
        candidates = self.run_worker(request_graph(("w0", "w1")), [1, 7, 19])
        self.assertEqual([candidate["inputOrderPolicy"] for candidate in candidates],
                         ["traced_first"] * 3)
        self.assertEqual([candidate["nodes"][0]["x"] for candidate in candidates], [100, 200, 300])

    def test_profiles_follow_seed_not_number_of_retained_candidates(self):
        candidates = self.run_worker(request_graph(), [1, 7, 19])
        self.assertEqual([candidate["seed"] for candidate in candidates], [1, 1, 7, 7, 19, 19])
        self.assertEqual([candidate["branchProfile"] for candidate in candidates],
                         ["balanced"] * 2 + ["flow_weighted"] * 4)
        self.assertEqual([candidate["nodes"][0]["x"] for candidate in candidates],
                         [100, 200, 300, 400, 500, 600])
