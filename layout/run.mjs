// A local, data-only worker. No browser, network request, or evidence file access.
import ELK from 'elkjs/lib/elk.bundled.js';

const chunks = [];
try {
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }
  const request = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if (!request.graph || !Array.isArray(request.seeds) || request.seeds.length > 3) {
    throw new Error('Invalid layout request');
  }
  const elk = new ELK();
  const candidates = [];
  for (const seed of request.seeds) {
    const graph = structuredClone(request.graph);
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
} catch {
  // Do not echo user graph input, parser excerpts, paths, or environment values.
  process.stderr.write('The local ELK layout worker could not calculate a layout.\n');
  process.exitCode = 1;
}
