import assert from 'node:assert/strict';
import {beforeEach, test} from 'node:test';
import {changeOutputsAction as action, changeOutputsInput as input, changeOutputsPanel as panel,
  changeOutputsFile as file, changeOutputsLookupComplete as complete, changeOutputsPending as pending,
  resetChangeOutputs as reset} from '../src/scripts/change-outputs.ts';

const txid = 'a'.repeat(64), other = 'b'.repeat(64);
let nodes;
const catalog = (overrides = {}) => ({revision: 4, total: 1, offset: 0, limit: 100,
  rows: [{txid, vout: 1, notes: 'Existing assessment', updated_at: '2026-09-19'}], notice: 'Local annotations.', ...overrides});
const report = (overrides = {}) => ({txid, current_vout: 1, current_notes: 'Existing assessment', revision: 4,
  outputs: [{vout: 0, selectable: true, address: '<script>address</script>', value_text: '125000'},
    {vout: 1, selectable: true, address: 'Liquid address'}, {vout: 2, selectable: false, reason: 'Fee'}],
  notice: 'Investigator annotation.', ...overrides});
const review = (overrides = {}) => ({valid: true, approval_sha256: 'review-hash', unique_transactions: 1,
  counts: {add: 1, replace: 0, clear: 0, unchanged: 0, keep: 0}, duplicate_rows: 0, errors: [],
  changes: [{row: 2, txid, vout: 0, notes: '<script>note</script>', previous: 1, previous_notes: 'Old note', action: 'replace'}],
  notice: 'Local annotations.', ...overrides});
function context(post = async () => catalog(), startLookup = async () => 'job-1') {
  return {caseId: 'case A', busy: false, render() {}, post, startLookup};
}
function edit(id, value, checked = false) {return input({id: `change-outputs-${id}`, value, checked});}
async function open(ctx = context()) {await action('change-outputs-open', ctx);}
async function lookup(ctx = context(), id = txid) {edit('txid', id); await action('change-outputs-lookup', ctx);}
async function importOpen(ctx) {await action('change-outputs-import-open', ctx);}
async function preview(ctx) {await action('change-outputs-import-preview', ctx);}
async function apply(ctx) {await action('change-outputs-import-apply', ctx);}
function deferred() {let resolve, reject; const promise = new Promise((yes, no) => {resolve = yes; reject = no;}); return {resolve, reject, promise};}
beforeEach(() => {
  nodes = new Map();
  globalThis.document = {querySelector(selector) {
    if (!nodes.has(selector)) nodes.set(selector, {disabled: false, checked: false, textContent: '', focus() {}});
    return nodes.get(selector);
  }};
  reset('case A');
});

test('transaction lookup is separate from new-case lookup and displays one change choice plus None', async () => {
  const calls = []; const ctx = context(undefined, async id => {calls.push(id); return 'job-1';});
  await open(ctx); await lookup(ctx, txid.toUpperCase());
  assert.deepEqual(calls, [txid]);
  assert.equal(complete('case A', 'job-1', report()), true);
  const html = panel('case A', false);
  assert.match(html, /None\. Use normal ELK/);
  assert.match(html, /id="change-outputs-vout-1"[^>]* checked/);
  assert.match(html, /id="change-outputs-vout-2"[^>]* disabled/);
  assert.match(html, /&lt;script&gt;address&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});

test('save sends selected vout, notes, and the loaded revision; None clears', async () => {
  const calls = [];
  const ctx = context(async (path, body) => {calls.push({path, body}); return 'txid' in body ? {changed: 1, revision: 5} : catalog({revision: 5});});
  await open(ctx); await lookup(ctx); complete('case A', 'job-1', report());
  edit('vout-0', '0', true); edit('notes', 'Based on analysis');
  await action('change-outputs-save', ctx);
  assert.deepEqual(calls[1], {path: '/api/cases/case%20A/change-outputs', body: {txid, vout: 0, notes: 'Based on analysis', expected_revision: 4}});
  edit('vout-none', '', true); await action('change-outputs-save', ctx);
  assert.equal(calls[3].body.vout, null);
  assert.equal(calls[3].body.expected_revision, 5);
  assert.match(panel('case A', false), /Sync and reorganize/);
});

test('non-selectable and forged output choices cannot change the selected output', async () => {
  const calls = [];
  const ctx = context(async (_path, body) => {calls.push(body); return 'txid' in body ? {changed: 0, revision: 4} : catalog();});
  await open(ctx); await lookup(ctx); complete('case A', 'job-1', report());
  edit('vout-2', '2', true); edit('vout-999', '999', true);
  await action('change-outputs-save', ctx);
  assert.equal(calls[1].vout, 1);
});

test('editing a hash invalidates old output selections and stale job completions', async () => {
  const ctx = context(); await open(ctx); await lookup(ctx);
  edit('txid', other);
  assert.equal(complete('case A', 'job-1', report()), false);
  assert.doesNotMatch(panel('case A', false), /value="1" aria-label="Use vout/);
  assert.equal(nodes.get('#change-outputs-save').disabled, true);
  await action('change-outputs-save', ctx);
  assert.match(panel('case A', false), /load transaction outputs before saving/);
});

test('old, recovered, and other-case lookup jobs do not overwrite the current editor', async () => {
  const ctx = context(); await open(ctx); await lookup(ctx);
  assert.equal(complete('case A', 'old-job', report()), false);
  assert.equal(complete('case B', 'job-1', report()), false);
  reset('case B'); reset('case A'); await open(ctx);
  assert.equal(complete('case A', 'job-1', report()), false);
  assert.equal(complete('case A', 'recovered-job', report()), false);
});

test('late job-start response cannot register after editing or switching cases', async () => {
  const start = deferred(); const ctx = context(undefined, () => start.promise);
  await open(ctx); const work = lookup(ctx);
  edit('txid', other); start.resolve('job-1'); await work;
  assert.equal(complete('case A', 'job-1', report({txid: other})), false);
});

test('bad hash never starts a lookup; lookup errors remain readable', async () => {
  let started = false; const ctx = context(undefined, async () => {started = true; return 'job-1';});
  await open(ctx); await lookup(ctx, 'bad'); assert.equal(started, false);
  assert.match(panel('case A', false), /64-character hexadecimal/);
  await lookup(ctx); complete('case A', 'job-1', undefined, '<script>failed</script>');
  assert.match(panel('case A', false), /&lt;script&gt;failed&lt;\/script&gt;/);
});

test('saved mappings can be searched, paginated, edited and cleared with catalog revision', async () => {
  const calls = [], started = [];
  const ctx = context(async (_path, body) => {calls.push(body); return 'txid' in body ? {changed: 1, revision: 5} : catalog({total: 101});}, async id => {started.push(id); return 'job-1';});
  await open(ctx); edit('query', 'assessment'); await action('change-outputs-load', ctx);
  assert.equal(calls[1].query, 'assessment');
  await action('change-outputs-next', ctx); assert.equal(calls[2].offset, 100);
  await action('change-outputs-edit', ctx, {dataset: {changeIndex: '0'}}); assert.deepEqual(started, [txid]);
  await action('change-outputs-clear', ctx, {dataset: {changeIndex: '0'}});
  assert.deepEqual(calls[3], {txid, vout: null, notes: '', expected_revision: 4});
});

test('stale save errors invalidate the output editor and import approval', async () => {
  const ctx = context(async (_path, body) => {if ('txid' in body) throw new Error('Annotations changed. Reload outputs.'); return catalog();});
  await open(ctx); await lookup(ctx); complete('case A', 'job-1', report()); await action('change-outputs-save', ctx);
  assert.match(panel('case A', false), /Annotations changed/);
  assert.doesNotMatch(panel('case A', false), /id="change-outputs-save"/);
});

test('reviewed import applies exact contents and policy only after approval', async () => {
  const calls = [];
  const ctx = context(async (path, body) => {
    calls.push({path, body});
    if (path.endsWith('/change-outputs')) return catalog();
    return 'approve_plan' in body ? {changed: 1} : review();
  });
  await open(ctx); await importOpen(ctx); edit('text', `Txid,ChangeVout,Notes\n${txid},0,Assessment`);
  await apply(ctx); assert.equal(calls.length, 1);
  await preview(ctx); assert.equal(calls.length, 2);
  assert.doesNotMatch(panel('case A', false), /change-outputs-import-approved/);
  assert.equal(calls[1].path, '/api/cases/case%20A/change-output-import');
  assert.equal(calls[1].body.policy, 'keep'); await apply(ctx);
  assert.deepEqual(calls[2].body, {...calls[1].body, approve_plan: 'review-hash'});
  assert.match(panel('case A', false), /Saved 1 change-output annotation/);
  await apply(ctx); assert.equal(calls.length, 4);
});

for (const [id, value, checked] of [['text', 'Changed', false], ['format', 'json', false], ['replace', '', true]]) {
  test(`editing import ${id} invalidates preview and approval`, async () => {
    let calls = 0;
    const ctx = context(async () => {calls += 1; return review();});
    await importOpen(ctx); await preview(ctx);
    edit(id, value, checked); await apply(ctx);
    assert.equal(calls, 1); assert.equal(nodes.get('#change-outputs-import-apply').disabled, true);
  });
}

test('invalid review escapes errors and notes, cannot apply, and paginates', async () => {
  const changes = Array.from({length: 101}, (_, index) => ({...review().changes[0], row: index + 2}));
  const ctx = context(async (_path, body) => 'query' in body ? catalog() : review({valid: false, approval_sha256: null,
    changes, unique_transactions: 101, errors: [{row: 2, message: '<script>bad output</script>'}]}));
  await open(ctx); await importOpen(ctx); await preview(ctx); await apply(ctx);
  let html = panel('case A', false);
  assert.match(html, /&lt;script&gt;bad output&lt;\/script&gt;/);
  assert.match(html, /&lt;script&gt;note&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/); assert.match(html, /Rows 1–100 of 101/);
  await action('change-outputs-import-next', ctx); html = panel('case A', false);
  assert.match(html, /Rows 101–101 of 101/);
});

test('late preview responses cannot restore approval after newer edits or a case switch', async () => {
  const response = deferred(); const ctx = context(() => response.promise);
  const work = preview(ctx); assert.equal(pending(), true);
  edit('text', 'Newer content'); response.resolve(review()); await work;
  assert.equal(pending(), false); await apply(ctx);
  const another = deferred(); const work2 = preview(context(() => another.promise));
  reset('case B'); reset('case A'); another.resolve(review()); await work2;
  await open(); await importOpen(context()); assert.doesNotMatch(panel('case A', false), /unique transactions:/);
});

test('busy and pending guards prevent duplicate writes and lookups', async () => {
  let calls = 0, starts = 0;
  const response = deferred();
  const ctx = context(async () => {calls += 1; return response.promise;}, async () => {starts += 1; return 'job';});
  await action('change-outputs-open', {...ctx, busy: true}); assert.equal(calls, 0);
  const work = preview(ctx); await preview(ctx); await lookup(ctx); assert.equal(calls, 1); assert.equal(starts, 0);
  response.resolve(review()); await work;
});

test('file reads are bounded, send contents instead of paths, and discard stale completions', async () => {
  const calls = [];
  const ctx = context(async (_path, body) => {calls.push(body); return 'query' in body ? catalog() : review();});
  await open(ctx); await importOpen(ctx); edit('format', 'json');
  await file({files: [{size: 100, name: '<unsafe>.csv', text: async () => `Txid,ChangeVout\n${txid},0`}]}, ctx.render);
  await preview(ctx); assert.equal(calls[1].format, 'auto'); assert.ok(calls[1].text.startsWith('Txid,'));
  assert.equal(Object.keys(calls[1]).length, 3); assert.match(panel('case A', false), /&lt;unsafe&gt;\.csv/);
  let read = false;
  await file({files: [{size: 512 * 1024 + 1, name: 'big.csv', text: async () => {read = true; return '';}}]}, ctx.render);
  assert.equal(read, false); assert.match(panel('case A', false), /exceeds 512 KiB/);
  const waiting = deferred();
  const work = file({files: [{size: 10, name: 'old.csv', text: () => waiting.promise}]}, ctx.render);
  edit('text', 'Newer content'); waiting.resolve('OLD CONTENT'); await work;
  assert.match(panel('case A', false), /Newer content/); assert.doesNotMatch(panel('case A', false), /OLD CONTENT/);
});

test('UTF-8 byte bound is checked for pasted content before sending', async () => {
  let posted = false; const ctx = context(async () => {posted = true; return review();});
  edit('text', 'é'.repeat(262145)); await preview(ctx);
  assert.equal(posted, false);
});

test('successful apply stays reported if list refresh fails; approval is consumed', async () => {
  let calls = 0;
  const ctx = context(async (_path, body) => {
    calls += 1;
    if ('query' in body) {if (calls > 1) throw new Error('Connection interrupted'); return catalog();}
    return 'approve_plan' in body ? {changed: 1} : review();
  });
  await open(ctx); await importOpen(ctx); await preview(ctx); await apply(ctx);
  assert.match(panel('case A', false), /Saved 1 change-output annotation/);
  assert.match(panel('case A', false), /list could not refresh: Connection interrupted/);
  const count = calls; await apply(ctx); assert.equal(calls, count);
});

test('late apply or saved-list responses cannot overwrite a different case', async () => {
  const response = deferred(); let refreshed = false;
  const ctx = context(async (_path, body) => {
    if ('approve_plan' in body) return response.promise;
    if ('query' in body) {refreshed = true; return catalog();}
    return review();
  });
  await preview(ctx); const work = apply(ctx);
  reset('case B'); response.resolve({changed: 1}); await work;
  assert.equal(refreshed, false);
  await open({...context(), caseId: 'case B'}); assert.doesNotMatch(panel('case B', false), /Saved 1/);
});
