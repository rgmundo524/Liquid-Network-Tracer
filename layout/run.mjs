// A local, data-only worker. No browser, network request, or evidence file access.
import ELK from 'elkjs/lib/elk.bundled.js';

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
  if (!request.graph || !Array.isArray(request.seeds) || request.seeds.length > 3) {
    throw new Error('Invalid layout request');
  }
  const elk = new ELK();
  const candidates = [];
  for (const seed of request.seeds) {
    // Large graphs use one seed. Reuse that graph instead of retaining a
    // second complete copy throughout ELK's calculation.
    const graph = request.seeds.length === 1 ? request.graph : structuredClone(request.graph);
    if (request.seeds.length === 1) request.graph = null;
    graph.layoutOptions['elk.randomSeed'] = String(seed);
    const result = await elk.layout(graph);
    // Only coordinates, ports, and routes cross the worker boundary.
    candidates.push({
      seed,
      nodes: result.children.map(({id, x, y, width, height, ports}) =>
        ({id, x, y, width, height, ports: (ports || []).map(({id, x, y}) => ({id, x, y}))})),
      edges: result.edges.map(({id, sections}) => ({id, sections})),
    });
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
