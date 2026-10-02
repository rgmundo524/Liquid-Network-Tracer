'use strict';

/*
 * Generated function excerpts below are from Eclipse Layout Kernel / elkjs
 * 0.12.0, distributed under the Eclipse Public License 2.0:
 * https://www.eclipse.org/legal/epl-2.0
 * Upstream source: https://github.com/eclipse/elk
 * This file replaces those recursive traversals with iterative equivalents.
 * SPDX-License-Identifier: EPL-2.0
 */

const {createHash} = require('node:crypto');
const {readFileSync} = require('node:fs');
const Module = require('node:module');
const {dirname} = require('node:path');

// ELK's network simplex auxiliary graph can contain many more vertices than
// the displayed graph. Keep the same algorithm and traversal order, but put
// its depth-first traversal stacks on the heap instead of the JS stack.
// These generated internals are private: reject any unreviewed engine build.
const ENGINE_BUILD = 'non_minified_iterative_network_simplex_v2';
const ENGINE_VERSION = '0.12.0';
const WORKER_SHA256 = 'f7617e622f565748e1c88a9f45d97b458bc9c3f4299c0fdc6ed778a92ef30683';

const REPLACEMENTS = [
  [
`function $connectedComponentsDFS(this$static, node){
  var edge, edge$iterator, opposite, port, port$iterator;
  this$static.nodeVisited[node.id_0] = true;
  $add_3(this$static.componentNodes, node);
  for (port$iterator = new ArrayList$1(node.ports); port$iterator.i < port$iterator.this$01.array.length;) {
    port = castTo($next_6(port$iterator), 12);
    for (edge$iterator = new LPort$CombineIter$1(port.connectedEdges); $hasNext_3(edge$iterator.firstIterator) || $hasNext_3(edge$iterator.secondIterator);) {
      edge = castTo($hasNext_3(edge$iterator.firstIterator)?$next_6(edge$iterator.firstIterator):$next_6(edge$iterator.secondIterator), 17);
      opposite = $getOpposite(port, edge).owner;
      this$static.nodeVisited[opposite.id_0] || $connectedComponentsDFS(this$static, opposite);
    }
  }
}`,
`function $connectedComponentsDFS(this$static, node){
  var edge, frame, frames, opposite;
  this$static.nodeVisited[node.id_0] = true;
  $add_3(this$static.componentNodes, node);
  frames = [{ports: new ArrayList$1(node.ports), port: null, edges: null}];
  while (frames.length) {
    frame = frames[frames.length - 1];
    if (frame.edges && ($hasNext_3(frame.edges.firstIterator) || $hasNext_3(frame.edges.secondIterator))) {
      edge = castTo($hasNext_3(frame.edges.firstIterator)?$next_6(frame.edges.firstIterator):$next_6(frame.edges.secondIterator), 17);
      opposite = $getOpposite(frame.port, edge).owner;
      if (!this$static.nodeVisited[opposite.id_0]) {
        this$static.nodeVisited[opposite.id_0] = true;
        $add_3(this$static.componentNodes, opposite);
        frames.push({ports: new ArrayList$1(opposite.ports), port: null, edges: null});
      }
      continue;
    }
    if (frame.ports.i >= frame.ports.this$01.array.length) {
      frames.pop();
      continue;
    }
    frame.port = castTo($next_6(frame.ports), 12);
    frame.edges = new LPort$CombineIter$1(frame.port.connectedEdges);
  }
}`,
  ],
  [
`function $dfs(this$static, node, mark){
  var edge, edge$iterator, other;
  if (mark[node.internalId]) {
    return;
  }
  mark[node.internalId] = true;
  for (edge$iterator = new ArrayList$1($getConnectedEdges(node)); edge$iterator.i < edge$iterator.this$01.array.length;) {
    edge = castTo($next_6(edge$iterator), 217);
    other = $getOther(edge, node);
    $dfs(this$static, other, mark);
  }
}`,
`function $dfs(this$static, node, mark){
  var edge, frame, frames, other;
  if (mark[node.internalId]) {
    return;
  }
  mark[node.internalId] = true;
  frames = [{node: node, iterator: new ArrayList$1($getConnectedEdges(node))}];
  while (frames.length) {
    frame = frames[frames.length - 1];
    if (frame.iterator.i >= frame.iterator.this$01.array.length) {
      frames.pop();
      continue;
    }
    edge = castTo($next_6(frame.iterator), 217);
    other = $getOther(edge, frame.node);
    if (!mark[other.internalId]) {
      mark[other.internalId] = true;
      frames.push({node: other, iterator: new ArrayList$1($getConnectedEdges(other))});
    }
  }
}`,
  ],
  [
`function $tightTreeDFS(this$static, node){
  var edge, edge$iterator, nodeCount, opposite;
  nodeCount = 1;
  node.treeNode = true;
  opposite = null;
  for (edge$iterator = new ArrayList$1($getConnectedEdges(node)); edge$iterator.i < edge$iterator.this$01.array.length;) {
    edge = castTo($next_6(edge$iterator), 217);
    if (!this$static.edgeVisited[edge.internalId]) {
      this$static.edgeVisited[edge.internalId] = true;
      opposite = $getOther(edge, node);
      if (edge.treeEdge) {
        nodeCount += $tightTreeDFS(this$static, opposite);
      }
       else if (!opposite.treeNode && edge.delta == edge.target.layer - edge.source.layer) {
        edge.treeEdge = true;
        $add_6(this$static.treeEdges, edge);
        nodeCount += $tightTreeDFS(this$static, opposite);
      }
    }
  }
  return nodeCount;
}`,
`function $tightTreeDFS(this$static, node){
  var edge, frame, frames, nodeCount, opposite;
  node.treeNode = true;
  frames = [{node: node, iterator: new ArrayList$1($getConnectedEdges(node)), count: 1}];
  while (frames.length) {
    frame = frames[frames.length - 1];
    if (frame.iterator.i >= frame.iterator.this$01.array.length) {
      nodeCount = frame.count;
      frames.pop();
      if (!frames.length) {
        return nodeCount;
      }
      frames[frames.length - 1].count += nodeCount;
      continue;
    }
    edge = castTo($next_6(frame.iterator), 217);
    if (this$static.edgeVisited[edge.internalId]) {
      continue;
    }
    this$static.edgeVisited[edge.internalId] = true;
    opposite = $getOther(edge, frame.node);
    if (!edge.treeEdge) {
      if (opposite.treeNode || edge.delta != edge.target.layer - edge.source.layer) {
        continue;
      }
      edge.treeEdge = true;
      $add_6(this$static.treeEdges, edge);
    }
    opposite.treeNode = true;
    frames.push({node: opposite, iterator: new ArrayList$1($getConnectedEdges(opposite)), count: 1});
  }
}`,
  ],
  [
`function $postorderTraversal(this$static, node){
  var edge, edge$iterator, lowest;
  lowest = $intern_0;
  for (edge$iterator = new ArrayList$1($getConnectedEdges(node)); edge$iterator.i < edge$iterator.this$01.array.length;) {
    edge = castTo($next_6(edge$iterator), 217);
    if (edge.treeEdge && !this$static.edgeVisited[edge.internalId]) {
      this$static.edgeVisited[edge.internalId] = true;
      lowest = $wnd.Math.min(lowest, $postorderTraversal(this$static, $getOther(edge, node)));
    }
  }
  this$static.poID[node.internalId] = this$static.postOrder;
  this$static.lowestPoID[node.internalId] = $wnd.Math.min(lowest, this$static.postOrder++);
  return this$static.lowestPoID[node.internalId];
}`,
`function $postorderTraversal(this$static, node){
  var edge, frame, frames, lowest, other;
  frames = [{node: node, iterator: new ArrayList$1($getConnectedEdges(node)), lowest: $intern_0}];
  while (frames.length) {
    frame = frames[frames.length - 1];
    if (frame.iterator.i >= frame.iterator.this$01.array.length) {
      this$static.poID[frame.node.internalId] = this$static.postOrder;
      lowest = $wnd.Math.min(frame.lowest, this$static.postOrder++);
      this$static.lowestPoID[frame.node.internalId] = lowest;
      frames.pop();
      if (!frames.length) {
        return lowest;
      }
      frame = frames[frames.length - 1];
      frame.lowest = $wnd.Math.min(frame.lowest, lowest);
      continue;
    }
    edge = castTo($next_6(frame.iterator), 217);
    if (edge.treeEdge && !this$static.edgeVisited[edge.internalId]) {
      this$static.edgeVisited[edge.internalId] = true;
      other = $getOther(edge, frame.node);
      frames.push({node: other, iterator: new ArrayList$1($getConnectedEdges(other)), lowest: $intern_0});
    }
  }
}`,
  ],
];

function patchEngineSource(source, version) {
  if (version !== ENGINE_VERSION) {
    throw new Error('Unsupported ELK engine version for stack-safe network simplex; expected 0.12.0');
  }
  if (typeof source !== 'string'
      || createHash('sha256').update(source, 'utf8').digest('hex') !== WORKER_SHA256) {
    throw new Error('Unsupported ELK worker source for stack-safe network simplex; reinstall the pinned layout dependencies');
  }
  for (const [original, replacement] of REPLACEMENTS) {
    const index = source.indexOf(original);
    if (index < 0 || index !== source.lastIndexOf(original)) {
      throw new Error('ELK network simplex traversal source does not match the reviewed implementation');
    }
    // A function replacement keeps generated names containing '$' literal.
    source = source.replace(original, () => replacement);
  }
  return source;
}

function loadWorker() {
  const engineRequire = Module.createRequire(__filename);
  const filename = engineRequire.resolve('elkjs/lib/elk-worker.js');
  const packageFile = engineRequire.resolve('elkjs/package.json');
  const {version} = JSON.parse(readFileSync(packageFile, 'utf8'));
  const source = patchEngineSource(readFileSync(filename, 'utf8'), version);
  // Compile an isolated CommonJS instance. Do not rewrite node_modules or
  // replace its normal require-cache entry. Preserve the readable filename.
  const workerModule = new Module(filename, module);
  workerModule.filename = filename;
  workerModule.paths = Module._nodeModulePaths(dirname(filename));
  workerModule._compile(source, filename);
  return workerModule.exports;
}

module.exports = {ENGINE_BUILD, patchEngineSource, loadWorker};
