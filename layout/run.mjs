// A local, data-only worker. No browser, network request, or evidence file access.

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
  let preserved = true;
  for (const [id, {west, east}] of orders) {
    const node = nodes.get(id);
    if (!node) throw new Error('Missing ordered layout node');
    // Inspect every node and both sides even after an order mismatch. Missing
    // or malformed ports are fatal; a rejected optional ordering is not.
    const inputs = orderedPorts(node, west, 'WEST');
    const outputs = orderedPorts(node, east, 'EAST');
    if (!sameOrder(inputs, west) || !sameOrder(outputs, east)) preserved = false;
  }
  return preserved;
}

function layoutCandidate(result, seed, branchProfile, inputOrderPolicy, branchBoundary) {
  // Snapshot data-only geometry before a second layout can mutate its request.
  // Sections contain nested points, so copying only their array is insufficient.
  return {
    seed, branchProfile, inputOrderPolicy, branchBoundary,
    nodes: result.children.map(({id, x, y, width, height, ports}) =>
      ({id, x, y, width, height, ports: (ports || []).map(({id, x, y}) => ({id, x, y}))})),
    edges: result.edges.map(({id, sections, labels}) => ({id, sections: structuredClone(sections),
      labels: (labels || []).map(({id, x, y, width, height}) => ({id, x, y, width, height}))})),
  };
}

// Only these fixed codes and adapter-owned values leave a failed worker.
// ELK errors can contain graph IDs, labels, paths or whole parser excerpts.
const diagnostic = {version: 1, stage: 'read_request'};
function failureCode(error) {
  if (diagnostic.stage === 'load_engine') return 'elk_worker_setup';
  if (['read_request', 'validate_request'].includes(diagnostic.stage)) return 'elk_invalid_request';
  const message = typeof error?.message === 'string' ? error.message : '';
  const name = typeof error?.name === 'string' ? error.name : '';
  const signature = `${name}\n${message}`;
  if (['Invalid ordered layout ports', 'Missing ordered layout node', 'ELK did not preserve input port order'].includes(message)) return 'elk_input_order';
  // Exceptions while inspecting or encoding returned geometry must remain
  // fatal. A malformed result is not a failed stochastic layout candidate.
  if (['order_constraints', 'validate_input_order', 'serialize'].includes(diagnostic.stage)) return 'elk_invalid_output';
  if (/javascript heap out of memory|reached heap limit|ineffective mark-compacts near heap limit|FatalProcessOutOfMemory/i.test(signature)) return 'heap_exhausted';
  if (/out of memory|out-of-memory|cannot allocate memory/i.test(signature)) return 'memory_exhausted';
  if (/maximum call stack size exceeded|too much recursion|StackOverflowError/i.test(signature)) return 'stack_limit';
  const knownClasses = [
    ['UnsupportedGraphException', 'elk_unsupported_graph'],
    ['UnsupportedConfigurationException', 'elk_unsupported_configuration'],
    ['ArrayIndexOutOfBoundsException|BasicIndexOutOfBoundsException|IndexOutOfBoundsException|StringIndexOutOfBoundsException|NegativeArraySizeException', 'elk_index_error'],
    ['IllegalStateException|ConcurrentModificationException|NoSuchElementException|EmptyStackException', 'elk_illegal_state'],
    ['IllegalArgumentException', 'elk_illegal_argument'],
    ['NullPointerException', 'elk_null_pointer'],
    ['AssertionError', 'elk_assertion'],
    ['TypeError|ClassCastException|ArrayStoreException', 'elk_type_error'],
    ['ReferenceError', 'elk_reference_error'],
  ];
  for (const [classes, code] of knownClasses) {
    if (new RegExp(`\\b(?:${classes})\\b`).test(signature)) return code;
  }
  return 'elk_engine_error';
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
  diagnostic.stage = 'validate_request';
  if (!request.graph || !Array.isArray(request.seeds) || request.seeds.length === 0 || request.seeds.length > 3
      || request.seeds.some(seed => !Number.isInteger(seed) || seed <= 0 || seed > 2147483647)) {
    throw new Error('Invalid layout request');
  }
  const requestedProfile = request.graph.branchProfile;
  if (requestedProfile !== undefined && !['balanced', 'flow_weighted'].includes(requestedProfile)) {
    throw new Error('Invalid branch placement profile');
  }
  delete request.graph.branchProfile;
  const boundaryOrdering = request.graph.boundaryOrdering === undefined ? false : request.graph.boundaryOrdering;
  const branchNodeOrder = request.graph.branchNodeOrder;
  const centerNodeOrder = request.graph.centerNodeOrder;
  delete request.graph.boundaryOrdering;
  delete request.graph.branchNodeOrder;
  delete request.graph.centerNodeOrder;
  if (typeof boundaryOrdering !== 'boolean') throw new Error('Invalid branch boundary ordering');
  if (branchNodeOrder !== undefined) {
    const childIds = new Set(request.graph.children.map(node => node.id));
    if (!Array.isArray(branchNodeOrder) || branchNodeOrder.length !== childIds.size
        || new Set(branchNodeOrder).size !== childIds.size
        || branchNodeOrder.some(id => typeof id !== 'string' || !childIds.has(id))) {
      throw new Error('Invalid branch boundary node order');
    }
  }
  if (boundaryOrdering && !branchNodeOrder) throw new Error('Missing branch boundary node order');
  if (centerNodeOrder !== undefined) {
    const childIds = new Set(request.graph.children.map(node => node.id));
    if (!Array.isArray(centerNodeOrder) || centerNodeOrder.length !== childIds.size
        || new Set(centerNodeOrder).size !== childIds.size
        || centerNodeOrder.some(id => typeof id !== 'string' || !childIds.has(id))) {
      throw new Error('Invalid named group node order');
    }
  }
  const orders = inputPortOrders(request.graph);
  const organizeBranches = [1, 2].includes(request.graph.branchOrganization);
  delete request.graph.branchOrganization;
  // Load and construct inside the diagnostic boundary so setup failures do
  // not expose module paths or get retried as stochastic seed failures.
  diagnostic.stage = 'load_engine';
  const {default: ELK} = await import('elkjs/lib/elk.bundled.js');
  const elk = new ELK();
  if (typeof elk.layout !== 'function') throw new Error('Invalid layout engine');
  diagnostic.stage = 'validate_request';
  const candidates = [];
  for (const [seedIndex, seed] of request.seeds.entries()) {
    // The Python search sends one seed at a time. Reuse that graph instead of retaining a
    // second complete copy throughout ELK's calculation.
    let graph = request.seeds.length === 1 ? request.graph : structuredClone(request.graph);
    if (request.seeds.length === 1) request.graph = null;
    if (boundaryOrdering || centerNodeOrder) {
      const children = new Map(graph.children.map(node => [node.id, node]));
      graph.children = (centerNodeOrder || branchNodeOrder).map(id => children.get(id));
      const ranks = new Map();
      graph.children.forEach((child, index) => {
        ranks.set(child.id, index);
        for (const port of child.ports || []) ranks.set(port.id, index);
      });
      graph.edges.sort((first, second) => ranks.get(first.sources[0]) - ranks.get(second.sources[0])
        || ranks.get(first.targets[0]) - ranks.get(second.targets[0]) || first.id.localeCompare(second.id));
      // Preserve dependency partitions and let ELK place ports and route all
      // edges. Only vertical node order is constrained for this alternative.
      graph.layoutOptions['elk.layered.considerModelOrder.strategy'] = 'NODES_AND_EDGES';
      graph.layoutOptions['elk.layered.crossingMinimization.forceNodeModelOrder'] = 'true';
      // Keep disconnected members in the same central band instead of packing
      // their components into unrelated rectangles after the layout.
      if (centerNodeOrder) graph.layoutOptions['elk.separateConnectedComponents'] = 'false';
    }
    graph.layoutOptions['elk.randomSeed'] = String(seed);
    // Explicit metadata preserves the placement profile when seeds are sent
    // separately. Keep the historical defaults for direct batched callers.
    const branchProfile = centerNodeOrder ? 'flow_weighted' : requestedProfile ?? (organizeBranches && (request.seeds.length === 1 || seedIndex > 0)
      ? 'flow_weighted' : 'balanced');
    Object.assign(diagnostic, {seed, branch_profile: branchProfile,
      input_order_policy: 'geometry', stage: 'geometry_layout'});
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
    diagnostic.stage = 'order_constraints';
    const firstCandidate = layoutCandidate(result, seed, branchProfile, 'geometry', boundaryOrdering);
    const constraints = constrainInputOrder(result, orders);
    if (constraints) {
      // Compare ELK's crossing-aware port order with the historical traced-first
      // order. This retains the already calculated result, without another run.
      candidates.push(firstCandidate);
      // Reuse the first result as the second request rather than cloning a
      // large graph. Fixed indices override the first pass's port positions.
      Object.assign(diagnostic, {input_order_policy: 'traced_first', stage: 'traced_first_layout'});
      result = await elk.layout(result);
      diagnostic.stage = 'validate_input_order';
      const ordered = validateInputOrder(result, constraints);
      const secondCandidate = layoutCandidate(result, seed, branchProfile, 'traced_first', boundaryOrdering);
      if (!ordered) {
        // The first snapshot retains ELK's crossing-aware geometry and will
        // still undergo the Python adapter's complete layout validation.
        // Do not let an optional port-order preference discard that layout.
        firstCandidate.inputOrderFallback = 'traced_first_order_not_preserved';
        // Return the rejected alternative for full adapter validation too.
        // An ordering mismatch must not hide unrelated malformed geometry.
        secondCandidate.inputOrderRejected = 'traced_first_order_not_preserved';
      }
      candidates.push(secondCandidate);
    } else {
      // Identical policies need only one candidate; retain the preferred order.
      firstCandidate.inputOrderPolicy = 'traced_first';
      candidates.push(firstCandidate);
    }
  }
  diagnostic.stage = 'serialize';
  process.stdout.write(JSON.stringify({version: '0.12.0', candidates}));
  // Include parsing, both layout passes and result serialization in the OS
  // high-water mark. This is process RAM, not just the JavaScript heap. Keep
  // advisory telemetry separate from the geometry response and never include
  // graph labels, paths, environment values or arbitrary runtime messages.
  try {
    const peakRssMiB = Math.ceil(process.resourceUsage().maxRSS / 1024);
    if (Number.isSafeInteger(peakRssMiB) && peakRssMiB > 0 && peakRssMiB <= 2147483647) {
      process.stderr.write(`LIQUID_ELK_USAGE ${JSON.stringify({version: 1, peak_rss_mb: peakRssMiB})}\n`);
    }
  } catch {
    // A missing measurement leaves the Python scheduler in serial mode.
  }
} catch (error) {
  // Do not echo user graph input, parser excerpts, paths, or environment values.
  process.stderr.write(`LIQUID_ELK_FAILURE ${JSON.stringify({...diagnostic, code: failureCode(error)})}\n`);
  process.exitCode = 1;
}
