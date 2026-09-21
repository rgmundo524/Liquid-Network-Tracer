import assert from 'node:assert/strict';
import {beforeEach, test} from 'node:test';
import {inputImportAction as action, inputImportInput as input, inputImportFiles as files,
  inputImportPanel as panel, inputImportPending as pending, resetInputImport as reset,
} from '../src/scripts/input-import.ts';

let nodes;
const samples = [
  {name: 'colors.csv', text: 'Name,Color\nNew Exchange,#123456\n', kind: 'auto', policy: 'keep'},
  {name: 'addresses.csv', text: 'Address,Name,stop_tracing,hop_limit\naddress,New Exchange,false,3\n', kind: 'auto', policy: 'keep'},
  {name: 'change.csv', text: `Txid,ChangeVout\n${'a'.repeat(64)},0\n`, kind: 'auto', policy: 'keep'},
];
const kinds = ['name-colors', 'attributions', 'change-outputs'];
function makeReview(overrides = {}) {
  return {valid: true, approval_sha256: 'exact-batch-hash', base_revision: 1,
    files: samples.map((sample, i) => ({...sample, kind: kinds[i], counts: {add: 1}, changes: [], errors: [], notice: 'Local review.'})),
    errors: [], counts: {add: 3}, notice: 'All files are saved together.', ...overrides};
}
function context(post = async () => makeReview()) {
  return {caseId: 'case A', busy: false, render() {}, post, refresh: async () => {}};
}
function selected(sample, overrides = {}) {
  const buffer = new TextEncoder().encode(sample.text);
  return {name: sample.name, size: buffer.length, async arrayBuffer() {return buffer.buffer;}, ...overrides};
}
async function choose(list = samples) {await files({files: list.map(sample => selected(sample))}, () => {});}
async function open(ctx = context()) {await action('input-import-open', ctx);}
async function preview(ctx = context()) {await action('input-import-preview', ctx);}
async function apply(ctx = context()) {await action('input-import-apply', ctx);}
function edit(id, value, checked = false) {return input({id: `input-import-${id}`, value, checked});}
function button(index) {return {dataset: {importIndex: String(index)}};}
function deferred() {
  let resolve;
  const promise = new Promise(done => {resolve = done;});
  return {promise, resolve};
}

beforeEach(() => {
  nodes = new Map();
  globalThis.document = {querySelector(selector) {
    if (!nodes.has(selector)) nodes.set(selector, {disabled: false, checked: false, textContent: '', focus() {}});
    return nodes.get(selector);
  }};
  reset('case A');
});

test('three files use one picker, one preview and one exact approved batch in any file order', async () => {
  const calls = []; let refreshed = 0;
  const ctx = context(async (path, body) => {
    calls.push({path, body});
    return body.approve_plan ? {changed: 3} : makeReview();
  });
  ctx.refresh = async () => {refreshed += 1;};
  await open(ctx); await choose();
  const html = panel('case A', false);
  assert.match(html, /type="file"[^>]* multiple/);
  for (const label of ['Detected: Name colors', 'Detected: Address attributions', 'Detected: Change outputs']) assert.ok(html.includes(label));
  await apply(ctx); assert.equal(calls.length, 0);
  await preview(ctx); await apply(ctx); assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], {path: '/api/cases/case%20A/input-import', body: {files: samples}});
  edit('approved', '', true); await apply(ctx);
  assert.deepEqual(calls[1].body, {...calls[0].body, approve_plan: 'exact-batch-hash'});
  assert.equal(refreshed, 1);
  assert.match(panel('case A', false), /Saved 3 changes from 3 CSV files/);
  await apply(ctx); assert.equal(calls.length, 2, 'saving consumes approval');
});

test('type overrides and replacement policies invalidate approval and appear in the next preview', async () => {
  const calls = []; const ctx = context(async (_path, body) => {calls.push(body); return makeReview();});
  await open(ctx); await choose(); await preview(ctx); edit('approved', '', true);
  edit('policy-1', 'replace');
  assert.equal(nodes.get('#input-import-apply').disabled, true);
  assert.equal(nodes.get('#input-import-approved').checked, false);
  assert.equal(nodes.get('#input-import-approved').disabled, true);
  edit('kind-0', 'name-colors'); await apply(ctx); assert.equal(calls.length, 1);
  await preview(ctx);
  assert.equal(calls[1].files[0].kind, 'name-colors');
  assert.equal(calls[1].files[1].policy, 'replace');
  assert.match(panel('case A', false), /Updating a saved hop limit/);
});

test('reselecting a filename replaces its queued contents; remove and clear require a new preview', async () => {
  let payload;
  const ctx = context(async (_path, body) => {payload = body; return makeReview();});
  await open(ctx); await choose(); await preview(ctx); edit('approved', '', true);
  await choose([{...samples[1], text: 'Address,Name\nnewaddress,New name\n'}]);
  await apply(ctx);
  assert.equal(payload.files[1].text, samples[1].text);
  await preview(ctx);
  assert.equal(payload.files.length, 3);
  assert.equal(payload.files[1].text, 'Address,Name\nnewaddress,New name\n');
  await action('input-import-remove', ctx, button(0));
  await preview(ctx); assert.equal(payload.files.length, 2);
  await action('input-import-clear', ctx);
  const html = panel('case A', false);
  assert.doesNotMatch(html, /addresses\.csv|Combined review/);
  assert.match(html, /data-action="input-import-preview" disabled/);
});

test('ambiguous headers require a type choice and all error/content text is escaped', async () => {
  const unsafe = '<img src=x onerror=alert(1)>.csv';
  const ctx = context(async () => makeReview({valid: false, approval_sha256: null,
    files: [{name: unsafe, kind: 'auto', counts: {}, changes: [], errors: []}],
    errors: [{file: unsafe, row: 2, message: '<script>Ambiguous CSV columns</script>'}]}));
  await open(ctx); await choose([{name: unsafe, text: 'Address,Name,Color\na,b,#123456\n'}]);
  assert.match(panel('case A', false), /Choose a type if the columns are ambiguous/);
  await preview(ctx); edit('approved', '', true);
  const html = panel('case A', false);
  assert.match(html, /&lt;script&gt;Ambiguous CSV columns&lt;\/script&gt;/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;\.csv/);
  assert.doesNotMatch(html, /<script>|<img /);
  assert.match(html, /id="input-import-apply"[^>]* disabled/);
});

test('review includes tracing choices, safe color swatches, previous values, and paginated row details', async () => {
  const ctx = context(async () => makeReview({files: [
    {name: 'addresses.csv', kind: 'attributions', changes: [{row: 2, action: 'replace',
      rule: {address: '<address>', name: 'Service', confidence: 'suspected', stop_tracing: false, hop_limit: 4},
      previous: {name: 'Service', confidence: 'confirmed', stop_tracing: true, hop_limit: 1}}]},
    {name: 'colors.csv', kind: 'name-colors', changes: Array.from({length: 51}, (_, i) => ({row: i + 2, action: 'replace',
      name: `Name ${i}`, color: i ? '#123456' : 'red;position:fixed', previous: '#abcdef'}))},
    {name: 'change.csv', kind: 'change-outputs', changes: [{row: 2, action: 'clear', txid: 'a'.repeat(64),
      vout: null, previous: 1, notes: '<note>', previous_notes: 'Before'}]},
  ]}));
  await open(ctx); await choose(); await preview(ctx);
  let html = panel('case A', false);
  assert.match(html, /Continue tracing · hop_limit: 4/); assert.match(html, /Stop tracing · hop_limit: 1/);
  assert.match(html, /background-color:#abcdef/); assert.doesNotMatch(html, /background-color:red/);
  assert.match(html, /&lt;address&gt;/); assert.match(html, /&lt;note&gt;/);
  assert.match(html, /Clear designation/); assert.match(html, /Rows 1–50 of 51/);
  assert.doesNotMatch(html, />Name 50</);
  await action('input-import-next', ctx, button(1));
  html = panel('case A', false); assert.match(html, /Rows 51–51 of 51/); assert.match(html, />Name 50</);
});

test('a late preview cannot restore review or approval after options change', async () => {
  const response = deferred(); const ctx = context(() => response.promise);
  await open(ctx); await choose(); const request = preview(ctx);
  assert.equal(pending(), true);
  edit('policy-1', 'replace'); response.resolve(makeReview()); await request;
  assert.equal(pending(), false);
  assert.doesNotMatch(panel('case A', false), /Combined review/);
});

test('case switching discards late previews and file reads even when returning to the original case', async () => {
  const response = deferred(); const ctx = context(() => response.promise);
  await open(ctx); await choose(); const request = preview(ctx);
  reset('case B'); reset('case A'); await open(); response.resolve(makeReview()); await request;
  assert.doesNotMatch(panel('case A', false), /Combined review/);
  const reading = deferred();
  const read = files({files: [selected(samples[0], {arrayBuffer: () => reading.promise})]}, () => {});
  reset('case B'); reset('case A'); await open();
  reading.resolve(new TextEncoder().encode(samples[0].text).buffer); await read;
  assert.doesNotMatch(panel('case A', false), /colors\.csv/);
});

test('late apply from another case does not refresh the new case', async () => {
  const response = deferred(); let refreshed = 0;
  const ctx = context(async (_path, body) => body.approve_plan ? response.promise : makeReview());
  ctx.refresh = async () => {refreshed += 1;};
  await open(ctx); await choose(); await preview(ctx); edit('approved', '', true);
  const request = apply(ctx); reset('case B'); response.resolve({changed: 3}); await request;
  assert.equal(refreshed, 0);
});

test('busy and pending actions prevent double requests and file selection', async () => {
  let calls = 0; const response = deferred();
  const ctx = context(async (_path, body) => {calls += 1; return body.approve_plan ? response.promise : makeReview();});
  await open(ctx);
  await files({files: [selected(samples[0])]}, () => {}, true);
  assert.doesNotMatch(panel('case A', false), /colors\.csv/);
  await choose(); await preview({...ctx, busy: true}); assert.equal(calls, 0);
  await preview(ctx); edit('approved', '', true);
  const request = apply(ctx); await apply(ctx); await preview(ctx);
  assert.equal(calls, 2); response.resolve({changed: 3}); await request;
});

test('stale server plans clear approval and display the reason', async () => {
  const ctx = context(async (_path, body) => {
    if (body.approve_plan) throw new Error('Settings changed. Preview again.');
    return makeReview();
  });
  await open(ctx); await choose(); await preview(ctx); edit('approved', '', true); await apply(ctx);
  assert.match(panel('case A', false), /Settings changed\. Preview again\./);
  assert.equal(nodes.get('#input-import-approved').checked, false);
});

test('a failed read preserves the entire existing queue and never saves replacement characters', async () => {
  let payload; const ctx = context(async (_path, body) => {payload = body; return makeReview();});
  await open(ctx); await choose([samples[0]]);
  await files({files: [selected(samples[1]), selected(samples[2], {
    arrayBuffer: async () => new Uint8Array([0xc3, 0x28]).buffer,
  })]}, () => {});
  assert.match(panel('case A', false), /change\.csv: must contain valid UTF-8 text/);
  await preview(ctx);
  assert.deepEqual(payload.files, [samples[0]]);
});

test('the strict file reader preserves a valid UTF-8 BOM and exact content', async () => {
  let payload; const sample = {...samples[0], text: '\uFEFF' + samples[0].text};
  const ctx = context(async (_path, body) => {payload = body; return makeReview();});
  await open(ctx); await choose([sample]);
  assert.match(panel('case A', false), /Detected: Name colors/);
  await preview(ctx); assert.equal(payload.files[0].text, sample.text);
});

test('size, file count and CSV-only checks do not read invalid selections or alter the queue', async () => {
  let reads = 0;
  const unread = overrides => selected(samples[0], {arrayBuffer() {reads += 1; throw new Error('must not read');}, ...overrides});
  await open();
  await files({files: [unread({size: 512 * 1024 + 1})]}, () => {});
  assert.match(panel('case A', false), /exceeds 512 KiB/);
  await files({files: [unread({name: 'data.json'})]}, () => {});
  assert.match(panel('case A', false), /choose a \.csv file/);
  await files({files: [unread({}), unread({}), unread({}), unread({})]}, () => {});
  assert.match(panel('case A', false), /Choose up to three CSV files/);
  assert.equal(reads, 0);
  await choose(); await files({files: [unread({name: 'fourth.csv'})]}, () => {});
  assert.match(panel('case A', false), /Three files are already queued/);
  assert.equal(reads, 0);
});
