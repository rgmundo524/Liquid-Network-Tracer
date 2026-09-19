// A local, data-only worker. No browser, network request, or evidence file access.
import ELK from 'elkjs/lib/elk.bundled.js';

function inputPortOrders(graph) {
  const requested = graph.inputPortOrders;
  // This is adapter metadata, not an ELK option or graph property.
  delete graph.inputPortOrders;
  const orders = new Map();
  if (requested === undefined) return orders;
  if (!requested || typeof requested !== 'object' || Array.isArray(requested)
      || !Array.isArray(graph.children)) throw new Error('Invalid input port orders');
  const nodes = new Map(graph.children.map(node => [node.id, node]));
  if (nodes.size !== graph.children.length) throw new Error('Invalid input port orders');
  for (const [id, west] of Object.entries(requested)) {
    const node = nodes.get(id);
    if (!node || !Array.isArray(node.ports) || !Array.isArray(west) || west.length < 2
        || west.some(port => typeof port !== 'string') || new Set(west).size !== west.length) {
      throw new Error('Invalid input port orders');
    }
    const ports = new Map(node.ports.map(port => [port.id, port]));
    if (ports.size !== node.ports.length || node.ports.some(port =>
      typeof port.id !== 'string' || !['WEST', 'EAST'].includes(port.layoutOptions?.['elk.port.side']))) {
      throw new Error('Invalid input port orders');
    }
    const side = name => node.ports.filter(port => port.layoutOptions['elk.port.side'] === name);
    if (side('WEST').length !== west.length
        || west.some(port => ports.get(port)?.layoutOptions['elk.port.side'] !== 'WEST')) {
      throw new Error('Invalid input port orders');
    }
    orders.set(id, {west, east: side('EAST').map(port => port.id)});
  }
  return orders;
}

function orderedPorts(node, expected, side) {
  const ports = (node.ports || []).filter(port => port.layoutOptions?.['elk.port.side'] === side);
  const expectedIds = new Set(expected);
  if (ports.length !== expected.length || new Set(ports.map(port => port.id)).size !== ports.length
      || ports.some(port => !expectedIds.has(port.id) || !Number.isFinite(port.y))) {
    throw new Error('Invalid ordered layout ports');
  }
  return ports.sort((a, b) => a.y - b.y || a.id.localeCompare(b.id));
}

function sameOrder(ports, expected) {
  return ports.every((port, index) => port.id === expected[index]
    && (index === 0 || port.y > ports[index - 1].y));
}

function constrainInputOrder(graph, orders) {
  if (orders.size === 0) return null;
  const nodes = new Map(graph.children.map(node => [node.id, node]));
  const constraints = [];
  let needsLayout = false;
  for (const [id, {west, east}] of orders) {
    const node = nodes.get(id);
    if (!node) throw new Error('Missing ordered layout node');
    const inputs = orderedPorts(node, west, 'WEST');
    const outputs = orderedPorts(node, east, 'EAST');
    needsLayout ||= !sameOrder(inputs, west);
    constraints.push({node, west, east: outputs.map(port => port.id)});
  }
  if (!needsLayout) return null;
  for (const {node, west, east} of constraints) {
    // FIXED_ORDER is a node constraint, so retain ELK's chosen output order.
    // Indices run clockwise: EAST top-to-bottom, WEST bottom-to-top.
    const indices = new Map([...east, ...west.toReversed()].map((id, index) => [id, index]));
    node.layoutOptions['elk.portConstraints'] = 'FIXED_ORDER';
    for (const port of node.ports) port.layoutOptions['elk.port.index'] = String(indices.get(port.id));
  }
  // Keep only IDs across the second layout, not references to the first graph.
  return new Map(constraints.map(({node, west, east}) => [node.id, {west, east}]));
}

function validateInputOrder(graph, orders) {
  const nodes = new Map(graph.children.map(node => [node.id, node]));
  for (const [id, {west, east}] of orders) {
    const node = nodes.get(id);
    if (!node || !sameOrder(orderedPorts(node, west, 'WEST'), west)
        || !sameOrder(orderedPorts(node, east, 'EAST'), east)) {
      throw new Error('ELK did not preserve input port order');
    }
  }
}

function layoutCandidate(result, seed, branchProfile, inputOrderPolicy) {
  // Snapshot data-only geometry before a second layout can mutate its request.
  // Sections contain nested points, so copying only their array is insufficient.
  return {
    seed, branchProfile, inputOrderPolicy,
    nodes: result.children.map(({id, x, y, width, height, ports}) =>
      ({id, x, y, width, height, ports: (ports || []).map(({id, x, y}) => ({id, x, y}))})),
    edges: result.edges.map(({id, sections, labels}) => ({id, sections: structuredClone(sections),
      labels: (labels || []).map(({id, x, y, width, height}) => ({id, x, y, width, height}))})),
  };
}

try {
  let request;
  {
    const chunks = [];
    for await (const chunk of process.stdin) {
      chunks.push(chunk);
    }
    request = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    // The parsed graph is sufficient; do not retain its encoded input too.
    chunks.length = 0;
  }
  if (!request.graph || !Array.isArray(request.seeds) || request.seeds.length === 0 || request.seeds.length > 3
      || request.seeds.some(seed => !Number.isInteger(seed) || seed <= 0 || seed > 2147483647)) {
    throw new Error('Invalid layout request');
  }
  const requestedProfile = request.graph.branchProfile;
  if (requestedProfile !== undefined && !['balanced', 'flow_weighted'].includes(requestedProfile)) {
    throw new Error('Invalid branch placement profile');
  }
  delete request.graph.branchProfile;
  const orders = inputPortOrders(request.graph);
  const organizeBranches = request.graph.branchOrganization === 1;
  delete request.graph.branchOrganization;
  const elk = new ELK();
  const candidates = [];
  for (const [seedIndex, seed] of request.seeds.entries()) {
    // The Python search sends one seed at a time. Reuse that graph instead of retaining a
    // second complete copy throughout ELK's calculation.
    let graph = request.seeds.length === 1 ? request.graph : structuredClone(request.graph);
    if (request.seeds.length === 1) request.graph = null;
    graph.layoutOptions['elk.randomSeed'] = String(seed);
    // Explicit metadata preserves the placement profile when seeds are sent
    // separately. Keep the historical defaults for direct batched callers.
    const branchProfile = requestedProfile ?? (organizeBranches && (request.seeds.length === 1 || seedIndex > 0)
      ? 'flow_weighted' : 'balanced');
    if (organizeBranches) {
      graph.layoutOptions['elk.layered.nodePlacement.strategy'] = branchProfile === 'flow_weighted'
        ? 'NETWORK_SIMPLEX' : 'BRANDES_KOEPF';
      if (branchProfile === 'balanced') {
        for (const edge of graph.edges) delete edge.layoutOptions?.['elk.layered.priority.straightness'];
      } else {
        // Network simplex can place a routing dummy between two real nodes.
        // Keep their combined edge clearances at least the node clearance;
        // otherwise 35 + dummy + 35 can violate our 80-unit compaction rule.
        const nodeSpacing = Number(graph.layoutOptions['elk.spacing.nodeNode'] ?? 80);
        const edgeSpacing = Number(graph.layoutOptions['elk.spacing.edgeNode'] ?? 35);
        graph.layoutOptions['elk.spacing.edgeNode'] = String(Math.max(edgeSpacing, nodeSpacing / 2));
      }
    }
    let result = await elk.layout(graph);
    graph = null;
    const firstCandidate = layoutCandidate(result, seed, branchProfile, 'geometry');
    const constraints = constrainInputOrder(result, orders);
    if (constraints) {
      // Compare ELK's crossing-aware port order with the historical traced-first
      // order. This retains the already calculated result, without another run.
      candidates.push(firstCandidate);
      // Reuse the first result as the second request rather than cloning a
      // large graph. Fixed indices override the first pass's port positions.
      result = await elk.layout(result);
      validateInputOrder(result, constraints);
      candidates.push(layoutCandidate(result, seed, branchProfile, 'traced_first'));
    } else {
      // Identical policies need only one candidate; retain the preferred order.
      firstCandidate.inputOrderPolicy = 'traced_first';
      candidates.push(firstCandidate);
    }
  }
  process.stdout.write(JSON.stringify({version: '0.12.0', candidates}));
} catch (error) {
  // Do not echo user graph input, parser excerpts, paths, or environment values.
  const message = typeof error?.message === 'string' ? error.message : '';
  if (/maximum call stack size exceeded|too much recursion|StackOverflowError/i.test(message)) {
    process.stderr.write('Maximum call stack size exceeded in the local ELK worker.\n');
  } else {
    process.stderr.write('The local ELK layout worker could not calculate a layout.\n');
  }
  process.exitCode = 1;
}
