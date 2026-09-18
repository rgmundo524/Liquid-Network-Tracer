import assert from 'node:assert/strict';
import {beforeEach, test} from 'node:test';
import {
  nameColorImportAction as action, nameColorImportInput as input,
  nameColorImportFile as file, nameColorImportPanel as panel,
  nameColorImportPending as pending, resetNameColorImport as reset,
} from '../src/scripts/name-color-import.ts';

let nodes;
const name = '<img src=x onerror=alert(1)>';
const makeReview = (overrides = {}) => ({
  valid: true, approval_sha256: 'review-hash', format: 'csv', policy: 'keep',
  counts: {add: 1, replace: 0, clear: 0, unchanged: 0, keep: 0}, input_rows: 1,
  unique_names: 1, duplicate_rows: 0, errors: [], notice: 'Local name colors only.',
  changes: [{row: 2, name, key: name, color: '#00AAbb', previous: '#000000', action: 'add', addresses: 2}],
  ...overrides,
});
function context(post = async () => makeReview()) {
  return {caseId: 'case A', busy: false, render() {}, post, refresh: async () => {}};
}
function edit(id, value, checked = false) { return input({id: `name-color-import-${id}`, value, checked}); }
async function open(ctx = context()) { await action('name-color-import-open', ctx); }
async function preview(ctx = context()) { await action('name-color-import-preview', ctx); }
async function apply(ctx = context()) { await action('name-color-import-apply', ctx); }
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject};
}
beforeEach(() => {
  nodes = new Map();
  globalThis.document = {querySelector(selector) {
    if (!nodes.has(selector)) nodes.set(selector, {disabled: false, checked: false, textContent: '', focus() {}});
    return nodes.get(selector);
  }};
  reset('case A');
});

test('preview is read-only; apply requires checkbox and submits the exact reviewed payload', async () => {
  const calls = []; let refreshes = 0;
  const ctx = context(async (path, payload) => {
    calls.push({path, payload});
    return 'approve_plan' in payload ? {changed: 1, revision: 2} : makeReview();
  });
  ctx.refresh = async () => {refreshes += 1;};
  await open(ctx);
  edit('text', 'Name,Color\nExample Exchange,#00AAbb\n');
  await apply(ctx);
  assert.equal(calls.length, 0);
  await preview(ctx);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, '/api/cases/case%20A/name-color-import');
  assert.deepEqual(calls[0].payload, {text: 'Name,Color\nExample Exchange,#00AAbb\n', format: 'auto', policy: 'keep'});
  await apply(ctx);
  assert.equal(calls.length, 1);
  edit('approved', '', true);
  await apply(ctx);
  assert.deepEqual(calls[1].payload, {...calls[0].payload, approve_plan: 'review-hash'});
  assert.equal(refreshes, 1);
  assert.match(panel('case A', false), /Saved 1 name color assignment/);
  await apply(ctx);
  assert.equal(calls.length, 2, 'approval is consumed after saving');
});

for (const [id, value, checked] of [['text', 'new contents', false], ['format', 'json', false], ['replace', '', true]]) {
  test(`changing ${id} invalidates both review and approval`, async () => {
    let calls = 0;
    const ctx = context(async () => {calls += 1; return makeReview();});
    await open(ctx); await preview(ctx); edit('approved', '', true);
    edit(id, value, checked);
    assert.equal(nodes.get('#name-color-import-apply').disabled, true);
    assert.equal(nodes.get('#name-color-import-approved').checked, false);
    assert.equal(nodes.get('#name-color-import-approved').disabled, true);
    await apply(ctx);
    assert.equal(calls, 1);
    assert.doesNotMatch(panel('case A', false), /unique names:/);
  });
}

test('invalid review displays errors safely and cannot be approved or applied', async () => {
  let calls = 0;
  const ctx = context(async () => {calls += 1; return makeReview({valid: false, approval_sha256: null,
    errors: [{row: 2, message: '<script>unknown name</script>'}]});});
  await open(ctx); await preview(ctx); edit('approved', '', true); await apply(ctx);
  const html = panel('case A', false);
  assert.equal(calls, 1);
  assert.match(html, /&lt;script&gt;unknown name&lt;\/script&gt;/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(html, /<script>|<img /);
  assert.match(html, /background-color:#00AAbb/);
});

test('only valid hex values are emitted into swatch styles', async () => {
  const ctx = context(async () => makeReview({changes: [{row: 2, name: 'Client', key: 'client',
    color: 'red;position:fixed', previous: '\" onmouseover=alert(1)', action: 'replace', addresses: 1}]}));
  await open(ctx); await preview(ctx);
  const html = panel('case A', false);
  assert.doesNotMatch(html, /background-color:/);
  assert.match(html, /&quot; onmouseover/);
});

test('late preview response cannot restore approval after input edits', async () => {
  const response = deferred();
  const ctx = context(() => response.promise);
  await open(ctx);
  const work = preview(ctx);
  assert.equal(pending(), true);
  edit('text', 'Name,Color\nChanged,#112233');
  response.resolve(makeReview()); await work;
  assert.equal(pending(), false);
  assert.doesNotMatch(panel('case A', false), /unique names:/);
});

test('switching cases and back discards an earlier preview response', async () => {
  const response = deferred();
  const ctx = context(() => response.promise);
  await open(ctx); const work = preview(ctx);
  reset('case B'); reset('case A'); await open();
  response.resolve(makeReview()); await work;
  assert.doesNotMatch(panel('case A', false), /unique names:/);
});

test('busy or pending handlers do not issue requests or double-apply', async () => {
  let calls = 0;
  const response = deferred();
  const ctx = context(async () => {calls += 1; return calls === 1 ? makeReview() : response.promise;});
  await preview({...ctx, busy: true}); assert.equal(calls, 0);
  await preview(ctx); edit('approved', '', true);
  const work = apply(ctx);
  await apply(ctx); await preview(ctx);
  assert.equal(calls, 2);
  response.resolve({changed: 1, revision: 3}); await work;
});

test('an apply response from a previous case cannot refresh the active case', async () => {
  const response = deferred(); let refreshes = 0;
  const ctx = context(async (_path, payload) => 'approve_plan' in payload ? response.promise : makeReview());
  ctx.refresh = async () => {refreshes += 1;};
  await open(ctx); await preview(ctx); edit('approved', '', true);
  const work = apply(ctx); reset('case B');
  response.resolve({changed: 1}); await work;
  assert.equal(refreshes, 0);
  assert.doesNotMatch(panel('case B', false), /Saved/);
});

test('stale plan errors remove approval and require a new preview', async () => {
  const ctx = context(async (_path, payload) => {
    if ('approve_plan' in payload) throw new Error('Colors changed. Preview again.');
    return makeReview();
  });
  await open(ctx); await preview(ctx); edit('approved', '', true); await apply(ctx);
  assert.match(panel('case A', false), /Colors changed\. Preview again\./);
  assert.doesNotMatch(panel('case A', false), /unique names:/);
  assert.equal(pending(), false);
});

test('a refresh failure preserves the successful import result', async () => {
  const ctx = context(async (_path, payload) => 'approve_plan' in payload ? {changed: 1, revision: 3} : makeReview());
  ctx.refresh = async () => {throw new Error('Connection interrupted.');};
  await open(ctx); await preview(ctx); edit('approved', '', true); await apply(ctx);
  assert.match(panel('case A', false), /Saved 1 name color assignment/);
  assert.match(panel('case A', false), /color list could not refresh: Connection interrupted/);
});

test('file import sends contents instead of a filesystem path and resets the format', async () => {
  const calls = [];
  const ctx = context(async (_path, payload) => {calls.push(payload); return makeReview();});
  await open(ctx); edit('format', 'csv');
  await file({files: [{size: 40, name: '<unsafe>.json', text: async () => '[{"Name":"Client","Color":"#ABCDEF"}]'}]}, ctx.render);
  await preview(ctx);
  assert.equal(calls[0].format, 'auto');
  assert.equal(calls[0].text, '[{"Name":"Client","Color":"#ABCDEF"}]');
  assert.equal(Object.keys(calls[0]).length, 3);
  assert.match(panel('case A', false), /&lt;unsafe&gt;\.json/);
});

test('file read completion cannot replace newer edits or another case draft', async () => {
  const read = deferred();
  const work = file({files: [{size: 50, name: 'old.csv', text: () => read.promise}]}, () => {});
  edit('text', 'Newer text');
  read.resolve('Name,Color\nOld,#112233'); await work;
  await open();
  assert.match(panel('case A', false), /Newer text/);
  assert.doesNotMatch(panel('case A', false), /old.csv/);
  const otherRead = deferred();
  const otherWork = file({files: [{size: 50, name: 'old.csv', text: () => otherRead.promise}]}, () => {});
  reset('case B'); reset('case A'); await open();
  otherRead.resolve('Name,Color\nOld,#112233'); await otherWork;
  assert.doesNotMatch(panel('case A', false), /old.csv|Old,#112233/);
});

test('file and UTF-8 pasted content enforce 512 KiB before sending', async () => {
  let read = false, posted = false;
  await open();
  await file({files: [{size: 512 * 1024 + 1, name: 'large.csv', text: async () => {read = true; return '';}}]}, () => {});
  assert.equal(read, false);
  assert.match(panel('case A', false), /exceeds 512 KiB/);
  edit('text', 'é'.repeat(262145));
  await preview(context(async () => {posted = true; return makeReview();}));
  assert.equal(posted, false);
  assert.match(panel('case A', false), /exceeds 512 KiB/);
});

test('replacement preview includes clear actions and paginates large imports', async () => {
  const calls = [];
  const changes = Array.from({length: 101}, (_, i) => ({row: i + 2, name: `Name ${i}`, key: `name ${i}`,
    color: null, previous: '#112233', action: 'clear', addresses: 1}));
  const ctx = context(async (_path, payload) => {calls.push(payload); return makeReview({changes, unique_names: 101});});
  await open(ctx); edit('replace', '', true); await preview(ctx);
  assert.equal(calls[0].policy, 'replace');
  assert.match(panel('case A', false), /Rows 1–100 of 101/);
  assert.match(panel('case A', false), /Clear assignment/);
  assert.doesNotMatch(panel('case A', false), />Name 100</);
  await action('name-color-import-next', ctx);
  assert.match(panel('case A', false), /Rows 101–101 of 101/);
  assert.match(panel('case A', false), />Name 100</);
});
