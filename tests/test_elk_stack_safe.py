"""The pinned ELK connectivity and simplex walks keep their results without recursion."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "layout" / "node_modules" / "elkjs" / "lib" / "elk-worker.js"
NODE = os.environ.get("LIQUID_NODE_BIN") or shutil.which("node")

# These test-only hooks are appended to an isolated compilation of the actual
# installed engine. All graph objects and collections are ELK's own classes.
# Nothing is inserted into production source, node_modules, or require.cache.
TEST_HOOKS = r"""
module.exports.testHooks = {
  create: function(spec) {
    var graph = new NGraph, nodes = [], edges = [];
    for (var i = 0; i < spec.count; i++) {
      var node = $create_2(new NNode$NNodeBuilder, graph);
      node.internalId = i;
      node.layer = spec.layers ? spec.layers[i] : i;
      node.treeNode = (spec.treeNodes || []).includes(i);
      nodes.push(node);
    }
    for (var i = 0; i < spec.edges.length; i++) {
      var definition = spec.edges[i];
      var edge = $create_1($target($source($delta(new NEdge$NEdgeBuilder,
        definition[2] === undefined ? 1 : definition[2]), nodes[definition[0]]), nodes[definition[1]]));
      edge.internalId = i;
      edge.treeEdge = definition[3] === true;
      edges.push(edge);
    }
    var state = {graph_0: graph, edgeVisited: edges.map(function(_, i) {
      return (spec.visited || []).includes(i);
    }), treeEdges: new LinkedHashSet_0(capacity_0(edges.length)),
      poID: new Array(nodes.length).fill(0), lowestPoID: new Array(nodes.length).fill(0),
      postOrder: spec.postOrder || 1};
    for (var i = 0; i < edges.length; i++) {
      edges[i].treeEdge && $add_6(state.treeEdges, edges[i]);
    }
    return {graph: graph, nodes: nodes, edges: edges, state: state,
      mark: nodes.map(function(_, i) { return (spec.marked || []).includes(i); })};
  },
  snapshot: function(fixture, value) {
    var selected = [], iterator = fixture.state.treeEdges.map_0.keySet_0().iterator_0();
    while (iterator.hasNext_0()) selected.push(iterator.next_1().internalId);
    return {value: value === undefined ? 'undefined' : value, mark: fixture.mark,
      treeNodes: fixture.nodes.map(function(n) { return n.treeNode; }),
      treeEdges: fixture.edges.map(function(e) { return e.treeEdge; }), selected: selected,
      edgeVisited: fixture.state.edgeVisited, poID: fixture.state.poID,
      lowestPoID: fixture.state.lowestPoID, postOrder: fixture.state.postOrder};
  },
  run: function(spec, action) {
    var fixture = this.create(spec), root = fixture.nodes[spec.root || 0], value;
    if (action === 'dfs') value = $dfs(fixture.graph, root, fixture.mark);
    else if (action === 'tight') value = $tightTreeDFS(fixture.state, root);
    else if (action === 'post') value = $postorderTraversal(fixture.state, root);
    else throw new Error('Unknown test action');
    return this.snapshot(fixture, value);
  },
  createComponents: function(spec) {
    var graph = new LGraph, nodes = [];
    for (var i = 0; i < spec.count; i++) {
      var node = new LNode(graph);
      node.testId = node.id_0 = i;
      for (var p = 0; p < (spec.ports ? spec.ports[i] : 1); p++) {
        $setNode(new LPort, node);
      }
      nodes.push(node);
    }
    var order = spec.order || nodes.map(function(_, i) { return i; });
    order.forEach(function(i) { $add_3(graph.layerlessNodes, nodes[i]); });
    spec.edges.forEach(function(definition) {
      var edge = new LEdge;
      $setSource_0(edge, nodes[definition[0]].ports.array[definition[2] || 0]);
      $setTarget_0(edge, nodes[definition[1]].ports.array[definition[3] || 0]);
    });
    return {graph: graph, nodes: nodes, state: new NetworkSimplexLayerer};
  },
  components: function(spec, repeats) {
    var fixture = this.createComponents(spec), snapshots = [];
    // Exercise the wrapper's visited-array reuse/reset branch as well as its
    // initial allocation, using the real compiled layerer state each time.
    for (var n = 0; n < (repeats || 1); n++) {
      var components = $connectedComponents(fixture.state, fixture.graph.layerlessNodes);
      var lists = [], iterator = $listIterator_2(components, 0);
      while (iterator.currentNode != iterator.this$01.tail) {
        lists.push($next_9(iterator).array.map(function(node) { return node.testId; }));
      }
      snapshots.push({components: lists, visited: Array.from(fixture.state.nodeVisited)});
    }
    return snapshots;
  },
  componentWalk: function(spec) {
    var fixture = this.createComponents(spec);
    fixture.state.nodeVisited = fixture.nodes.map(function(_, i) {
      return (spec.visited || []).includes(i);
    });
    fixture.state.componentNodes = new ArrayList;
    $connectedComponentsDFS(fixture.state, fixture.nodes[spec.root || 0]);
    return {component: fixture.state.componentNodes.array.map(function(node) { return node.testId; }),
      visited: fixture.state.nodeVisited};
  },
  dfs: $dfs, tight: $tightTreeDFS, post: $postorderTraversal
};
"""


@unittest.skipUnless(NODE and WORKER.is_file(), "Node and pinned ELK dependency are required")
class ElkStackSafeTests(unittest.TestCase):
    def node(self, body):
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const helper = require('./layout/elk-stack-safe.cjs');
const filename = require.resolve('./layout/node_modules/elkjs/lib/elk-worker.js');
const source = fs.readFileSync(filename, 'utf8');
const transformed = helper.patchEngineSource(source, '0.12.0');
function compile(text) {
  const instance = new Module(filename);
  instance.filename = filename;
  instance.paths = Module._nodeModulePaths(path.dirname(filename));
  instance._compile(text + '\n' + TEST_HOOKS, filename);
  return instance.exports.testHooks;
}
""".replace("TEST_HOOKS", json.dumps(TEST_HOOKS)) + body
        result = subprocess.run([str(NODE), "-e", script], cwd=ROOT,
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_only_the_reviewed_engine_build_can_be_patched(self):
        result = self.node(r"""
assert.equal(helper.ENGINE_BUILD, 'non_minified_iterative_network_simplex_v2');
assert.throws(() => helper.patchEngineSource(source, '0.13.0'), /Unsupported ELK engine version/);
assert.throws(() => helper.patchEngineSource(source, null), /Unsupported ELK engine version/);
assert.throws(() => helper.patchEngineSource(source + '\n', '0.12.0'), /Unsupported ELK worker source/);
assert.throws(() => helper.patchEngineSource(source.replace('function $dfs(', 'function $different('), '0.12.0'),
  /Unsupported ELK worker source/);
assert.throws(() => helper.patchEngineSource(null, '0.12.0'), /Unsupported ELK worker source/);
assert.throws(() => helper.patchEngineSource(transformed, '0.12.0'), /Unsupported ELK worker source/);
console.log(JSON.stringify({checks: 7}));
""")
        self.assertEqual(result["checks"], 7)

    def test_loading_does_not_modify_installed_worker_or_require_cache(self):
        result = self.node(r"""
assert.equal(require.cache[filename], undefined);
const isolated = helper.loadWorker();
assert.equal(typeof isolated.Worker, 'function');
assert.equal(require.cache[filename], undefined);
assert.equal(fs.readFileSync(filename, 'utf8'), source);
// Also preserve an existing, normally loaded engine's identity and exports.
const original = require(filename);
const cached = require.cache[filename];
const second = helper.loadWorker();
assert.equal(require.cache[filename], cached);
assert.equal(require(filename), original);
assert.notEqual(second.Worker, original.Worker);
assert.equal(fs.readFileSync(filename, 'utf8'), source);
console.log(JSON.stringify({isolated: true}));
""")
        self.assertTrue(result["isolated"])

    def test_all_walks_match_recursive_results_on_small_graphs(self):
        result = self.node(r"""
const original = compile(source), patched = compile(transformed);
const fixed = [
  {count: 1, edges: []},
  {count: 7, edges: [[0, 1, 1, true], [1, 2, 1, true], [0, 2, 2], [2, 0, 1],
    [3, 4, 1, true], [4, 5, 2]], root: 0},
  {count: 7, edges: [[0, 1, 1, true], [1, 2, 1, true], [0, 2, 2], [2, 0, 1],
    [3, 4, 1, true], [4, 5, 2]], root: 3, postOrder: 17},
  {count: 4, edges: [[0, 1, 1], [0, 1, 1], [1, 2, 1], [0, 3, 1]], marked: [0]},
  {count: 5, edges: [[0, 1, 1, true], [1, 2, 1, true], [1, 3, 2], [3, 4, 1]],
    visited: [0], marked: [1], treeNodes: [3], root: 1},
];
let checks = 0;
for (const spec of fixed) for (const action of ['dfs', 'tight', 'post']) {
  assert.deepEqual(patched.run(spec, action), original.run(spec, action), action);
  checks++;
}
// Deterministic branching trees, parallel edges, non-tight edges, disconnected
// components, and non-tree cycles. Compare every observable traversal result,
// including tree insertion order, numbering, visited flags, and return value.
let seed = 0x71942;
function random(max) { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed % max; }
for (let trial = 0; trial < 120; trial++) {
  const count = 2 + random(49), split = 1 + random(count), edges = [];
  for (let target = 1; target < count; target++) {
    const start = target < split ? 0 : split;
    if (target === start) continue;
    const from = start + random(target - start);
    edges.push([from, target, target - from, random(2) === 0]);
  }
  for (let extra = 0; extra < count; extra++) {
    const from = random(count), to = random(count);
    if (from === to || (from < split) !== (to < split)) continue;
    edges.push([from, to, to - from + random(2)]);
  }
  const spec = {count, edges, root: random(count), postOrder: 1 + random(20),
    visited: edges.flatMap((_, i) => random(7) === 0 ? [i] : []),
    marked: Array.from({length: count}, (_, i) => i).filter(() => random(13) === 0)};
  for (const action of ['dfs', 'tight', 'post']) {
    assert.deepEqual(patched.run(spec, action), original.run(spec, action), `${trial}:${action}`);
    checks++;
  }
}
console.log(JSON.stringify({checks}));
""")
        self.assertEqual(result["checks"], 375)

    def test_layered_components_keep_original_membership_and_traversal_order(self):
        result = self.node(r"""
const original = compile(source), patched = compile(transformed);
const fixed = [
  {count: 0, edges: []},
  {count: 1, ports: [0], edges: []},
  // Isolated nodes and components of increasing/equal sizes check the wrapper's
  // largest-component-first behavior, which is not a full size sort.
  {count: 11, edges: [[1, 2], [3, 4], [4, 5], [6, 7], [7, 8], [9, 10]]},
  // Ports 0 and 2 are empty at the root. Incoming edges must be traversed before
  // outgoing edges within port 1, then port 3 must resume after its descendants.
  {count: 7, ports: [4, 2, 2, 1, 1, 0, 1], edges: [
    [0, 1, 1, 0], [2, 0, 0, 1], [0, 3, 3, 0], [1, 4, 1, 0],
    [0, 1, 1, 0], [1, 2, 0, 1], [2, 2, 1, 1], [4, 0, 0, 1],
  ]},
  {count: 4, ports: [3, 1, 1, 1], order: [3, 1, 0, 2], edges: [
    [0, 1, 1, 0], [1, 2], [2, 0, 0, 2], [3, 3],
  ]},
];
let checks = 0;
for (const spec of fixed) {
  const expected = original.components(spec, 2), actual = patched.components(spec, 2);
  assert.deepEqual(actual, expected);
  assert.deepEqual(actual[0], actual[1], 'visited state must reset between calls');
  checks++;
}
assert.deepEqual(patched.components(fixed[2])[0].components,
  [[3, 4, 5], [1, 2], [0], [6, 7, 8], [9, 10]]);
assert.deepEqual(patched.components(fixed[3])[0].components, [[0, 2, 1, 4, 3], [5], [6]]);
assert.deepEqual(patched.components(fixed[4])[0].components, [[1, 0, 2], [3]]);
let seed = 0x83e123;
function random(max) { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed % max; }
for (let trial = 0; trial < 100; trial++) {
  const count = 2 + random(48), ports = Array.from({length: count}, () => 1 + random(4));
  const edges = [], split = 1 + random(count), order = Array.from({length: count}, (_, i) => i);
  for (let i = 0; i < count * 3; i++) {
    const from = random(count), to = random(count);
    if ((from < split) !== (to < split)) continue;
    edges.push([from, to, random(ports[from]), random(ports[to])]);
    if (random(7) === 0) edges.push(edges[edges.length - 1]);
  }
  for (let i = count - 1; i > 0; i--) {
    const j = random(i + 1); [order[i], order[j]] = [order[j], order[i]];
  }
  const spec = {count, ports, edges, order};
  assert.deepEqual(patched.components(spec, 2), original.components(spec, 2), `trial ${trial}`);
  checks++;
}
console.log(JSON.stringify({checks}));
""")
        self.assertEqual(result["checks"], 105)

    def test_layered_component_walk_preserves_previsited_node_behavior(self):
        result = self.node(r"""
const original = compile(source), patched = compile(transformed);
const graph = {count: 4, edges: [[0, 1], [0, 2], [1, 3], [3, 0], [0, 0]]};
for (const visited of [[], [0], [1], [0, 1, 3]]) {
  const spec = {...graph, visited};
  assert.deepEqual(patched.componentWalk(spec), original.componentWalk(spec));
}
assert.deepEqual(patched.componentWalk({...graph, visited: [0, 1, 3]}),
  {component: [0, 2], visited: [true, true, true, true]});
console.log(JSON.stringify({checks: 4}));
""")
        self.assertEqual(result["checks"], 4)

    def test_deep_layered_components_avoid_the_reported_stack_failure(self):
        result = self.node(r"""
const original = compile(source), patched = compile(transformed);
const count = 50000;
const spec = {count, edges: Array.from({length: count - 1}, (_, i) => [i, i + 1])};
assert.throws(() => original.components(spec), error =>
  error instanceof RangeError && /call stack/i.test(error.message));
const actual = patched.components(spec, 2);
const expected = {components: [Array.from({length: count}, (_, i) => i)],
  visited: Array(count).fill(true)};
assert.deepEqual(actual, [expected, expected]);
console.log(JSON.stringify({count, original: 'RangeError', patched: 'success'}));
""")
        self.assertEqual(result, {"count": 50000, "original": "RangeError", "patched": "success"})

    def test_deep_connectivity_walk_avoids_the_reported_stack_failure(self):
        self.deep_walk("dfs")

    def test_deep_tight_tree_walk_avoids_the_next_recursive_stack_failure(self):
        self.deep_walk("tight")

    def test_deep_postorder_walk_retains_all_subtree_numbers(self):
        self.deep_walk("post")

    def deep_walk(self, action):
        result = self.node(r"""
const action = ACTION;
const original = compile(source), patched = compile(transformed);
const count = 50000;
const spec = {count, edges: Array.from({length: count - 1}, (_, i) => [i, i + 1, 1, action === 'post'])};
assert.throws(() => original.run(spec, action), error =>
  error instanceof RangeError && /call stack/i.test(error.message));
const actual = patched.run(spec, action);
if (action === 'dfs') {
  assert.equal(actual.value, 'undefined');
  assert.equal(actual.mark.filter(Boolean).length, count);
} else if (action === 'tight') {
  assert.equal(actual.value, count);
  assert.equal(actual.treeNodes.filter(Boolean).length, count);
  assert.deepEqual(actual.selected, Array.from({length: count - 1}, (_, i) => i));
  assert.equal(actual.edgeVisited.filter(Boolean).length, count - 1);
} else {
  assert.equal(actual.value, 1);
  assert.equal(actual.postOrder, count + 1);
  assert.deepEqual(actual.poID, Array.from({length: count}, (_, i) => count - i));
  assert.equal(actual.lowestPoID.every(value => value === 1), true);
  assert.equal(actual.edgeVisited.filter(Boolean).length, count - 1);
}
console.log(JSON.stringify({action, count, original: 'RangeError', patched: 'success'}));
""".replace("ACTION", json.dumps(action)))
        self.assertEqual(result, {"action": action, "count": 50000,
                                  "original": "RangeError", "patched": "success"})


if __name__ == "__main__":
    unittest.main()
