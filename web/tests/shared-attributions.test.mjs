import assert from 'node:assert/strict';
import {beforeEach, test} from 'node:test';
import {sharedAttributionsAction as action, sharedAttributionsInput as input,
  sharedAttributionsFile as file, sharedAttributionsPanel as panel,
  sharedAttributionsPending as pending, resetSharedAttributions as reset,
} from '../src/scripts/shared-attributions.ts';

const BASE = '/api/shared-attributions';
const rule = {address: 'address-1', name: '<script>Exchange</script>', confidence: 'suspected',
  source: 'Research <source>', notes: 'Evidence & notes', observed_at: '2026-10-03', enabled: true};
const catalog = extra => ({library_id: 'library', revision: 3, total: 1, offset: 0, limit: 50, rows: [rule], ...extra});
const status = extra => ({enabled: false, revision: 7, shared_revision: 3, shared_count: 150, local_overrides: 2, ...extra});
const review = extra => ({valid: true, approval_sha256: 'exact-plan', counts: {add: 1, keep: 0, replace: 0, unchanged: 0},
  changes: [{row: 2, action: 'add', rule, previous: null}], errors: [], notice: 'Shared library review.',
  ignored_controls: {stop_tracing: 1, hop_limit: 1}, ...extra});
const csv = 'Address,Name,confidence,source,notes,observed_at,enabled\naddress-1,Exchange,suspected,Research,Note,2026-10-03,true\n';
let nodes;
function context(extra = {}) {
  return {caseId: null, busy: false, render() {},
    post: async (path, body) => path === BASE ? catalog({offset: body.offset}) : 'approve_plan' in body ? {changed: 1, revision: 4} : review(),
    get: async () => status(), readText: async () => ({text: csv, contentType: 'text/csv'}), refresh: async () => {}, ...extra};
}
const edit = (key, value = '', checked = false) => input({id: 'shared-attributions-' + key, value, checked});
const act = (name, ctx) => action('shared-attributions-' + name, ctx);
const choose = (text = csv, extra = {}) => {
  const bytes = new TextEncoder().encode(text);
  return file({files: [{name: 'attributions.csv', type: 'text/csv', size: bytes.length,
    arrayBuffer: async () => bytes.buffer, ...extra}]}, () => {});
};
const deferred = () => Promise.withResolvers();
beforeEach(() => {
  nodes = new Map();
  globalThis.document = {querySelector(selector) {
    if (!nodes.has(selector)) nodes.set(selector, {disabled: false, textContent: '', focus() {}});
    return nodes.get(selector);
  }};
  reset('case A'); reset('case B'); reset(null);
});

test('workspace library browse and pagination use bounded read-only queries and escape every displayed field', async () => {
  const calls = [];
  const ctx = context({post: async (path, body) => {calls.push({path, body}); return catalog({offset: body.offset, total: 151});},
    get: async () => {throw Error('Workspace has no case');}});
  assert.equal(panel(null, false), '');
  await act('open', ctx);
  const html = panel(null, false);
  assert.match(html, /Export shared CSV/);
  assert.match(html, /&lt;script&gt;Exchange&lt;\/script&gt;/);
  assert.match(html, /2026-10-03/);
  assert.match(html, /Evidence &amp; notes/);
  assert.doesNotMatch(html, /<script>|Use in this investigation/);
  edit('query', 'Exchange'); await act('load', ctx); await act('next', ctx);
  assert.deepEqual(calls.map(call => call.body), [{query: '', offset: 0, limit: 50},
    {query: 'Exchange', offset: 0, limit: 50}, {query: 'Exchange', offset: 50, limit: 50}]);
  await act('prev', ctx); assert.equal(calls.at(-1).body.offset, 0);
  assert.ok(calls.every(call => call.path === BASE));
});

test('import requires review and submits only the exact approved text, format, and conflict policy', async () => {
  const calls = [], refreshes = [];
  const ctx = context({post: async (path, body) => {calls.push({path, body});
    return path === BASE ? catalog() : body.approve_plan ? {changed: 2, revision: 4} : review();},
    refresh: async scope => {refreshes.push(scope);}});
  await act('open', ctx); edit('text', csv); edit('format', 'csv'); edit('policy', 'replace');
  await act('apply', ctx); assert.equal(calls.filter(call => call.path.endsWith('/import')).length, 0);
  await act('preview', ctx);
  const html = panel(null, false);
  assert.match(html, /all opted-in investigations/);
  assert.match(html, /Ignored local tracing controls: 1 stop_tracing values and 1 hop_limit values/);
  assert.match(html, /including enabled state, evidence fields, and blank values/);
  await act('apply', ctx);
  const imports = calls.filter(call => call.path.endsWith('/import'));
  assert.deepEqual(imports.map(call => call.body), [{text: csv, format: 'csv', policy: 'replace'},
    {text: csv, format: 'csv', policy: 'replace', approve_plan: 'exact-plan'}]);
  assert.deepEqual(refreshes, ['library']);
  assert.match(panel(null, false), /Saved 2 shared attribution changes/);
  await act('apply', ctx); assert.equal(calls.filter(call => call.path.endsWith('/import')).length, 2);
});

for (const [key, value] of [['text', 'new CSV'], ['format', 'json'], ['policy', 'replace']]) {
  test(`changing import ${key} invalidates review and disables Apply immediately`, async () => {
    let writes = 0;
    const ctx = context({post: async (path, body) => {if (body.approve_plan) writes++; return path === BASE ? catalog() : review();}});
    await act('open', ctx); edit('text', csv); await act('preview', ctx); edit(key, value);
    assert.equal(nodes.get('#shared-attributions-apply').disabled, true);
    await act('apply', ctx); assert.equal(writes, 0);
    assert.doesNotMatch(panel(null, false), /Review shared library changes/);
  });
}

test('pasting into an initially empty draft enables Review without replacing the textarea DOM', async () => {
  const ctx = context(); await act('open', ctx);
  edit('text', csv);
  assert.equal(nodes.get('#shared-attributions-preview').disabled, false);
  edit('text', '  ');
  assert.equal(nodes.get('#shared-attributions-preview').disabled, true);
});

test('invalid plans render safe errors and cannot apply, and review rows are paginated', async () => {
  const changes = Array.from({length: 101}, (_, index) => ({row: index + 2, action: 'add', rule: {...rule, address: 'address-' + index}, previous: null}));
  let writes = 0;
  const ctx = context({post: async (path, body) => {if (body.approve_plan) writes++;
    return path === BASE ? catalog() : review({valid: false, approval_sha256: null, changes, errors: [{row: 2, message: '<img> invalid'}]});}});
  await act('open', ctx); edit('text', csv); await act('preview', ctx);
  assert.match(panel(null, false), /&lt;img&gt; invalid/);
  assert.match(panel(null, false), /Changes 1–50 of 101/);
  await act('review-next', ctx); assert.match(panel(null, false), /Changes 51–100 of 101/);
  await act('apply', ctx); assert.equal(writes, 0);
});

test('late import review cannot restore approval after input edits or after a reset', async () => {
  const result = deferred();
  const ctx = context({post: async path => path === BASE ? catalog() : result.promise});
  await act('open', ctx); edit('text', csv);
  const work = act('preview', ctx); edit('text', 'changed');
  result.resolve(review()); await work;
  assert.doesNotMatch(panel(null, false), /Review shared library changes/);
  const second = deferred(), ctx2 = context({post: async path => path === BASE ? catalog() : second.promise});
  edit('text', csv); const work2 = act('preview', ctx2); reset(null);
  second.resolve(review()); await work2;
  assert.equal(panel(null, false), '');
});

test('case sharing is reviewed at the current local revision before an exact opt-in POST', async () => {
  const calls = [], refreshes = [];
  let reads = 0;
  const ctx = context({caseId: 'case A', get: async path => {calls.push({path}); return status({revision: 7 + reads++});},
    post: async (path, body) => {calls.push({path, body}); return path === BASE ? catalog() : status({enabled: body.enabled, revision: 9});},
    refresh: async scope => {refreshes.push(scope);}});
  await act('open', ctx); edit('enabled', '', true);
  await act('use-apply', ctx);
  assert.equal(calls.filter(call => call.body && call.path !== BASE).length, 0);
  await act('use-review', ctx);
  assert.match(panel('case A', false), /150 shared entries will be available. 2 local overrides/);
  assert.match(panel('case A', false), /including disabled local entries/);
  await act('use-apply', ctx);
  assert.deepEqual(calls.at(-1), {path: '/api/cases/case%20A/shared-attributions', body: {enabled: true, expected_revision: 8}});
  assert.deepEqual(refreshes, ['case']);
  assert.match(panel('case A', false), /Shared attributions enabled for this investigation/);
});

test('changing the sharing choice invalidates its review and stale backend approval errors require review again', async () => {
  let posts = 0;
  const ctx = context({caseId: 'case A', post: async path => {if (path === BASE) return catalog(); posts++; throw Error('Settings changed. Review again.');}});
  await act('open', ctx); edit('enabled', '', true); await act('use-review', ctx); edit('enabled', '', false);
  await act('use-apply', ctx); assert.equal(posts, 0);
  await act('use-review', ctx); await act('use-apply', ctx); await act('use-apply', ctx);
  assert.equal(posts, 1);
  assert.match(panel('case A', false), /Settings changed. Review again./);
  assert.equal(nodes.get('#shared-attributions-use-apply').disabled, true);
});

test('an unreadable library still permits reviewed opt-out and never presents missing counts as zero', async () => {
  const calls = [];
  const unavailable = status({enabled: true, shared_count: null, shared_revision: null, local_overrides: null, shared_unavailable: true});
  const ctx = context({caseId: 'case A', get: async () => unavailable,
    post: async (path, body) => {if (path === BASE) throw Error('Shared library is corrupt'); calls.push(body); return {...unavailable, enabled: body.enabled, revision: 8};}});
  await act('open', ctx);
  assert.match(panel('case A', false), /Shared library unavailable/);
  assert.doesNotMatch(panel('case A', false), /0 shared entries|0 local overrides/);
  edit('enabled', '', false); await act('use-review', ctx); await act('use-apply', ctx);
  assert.deepEqual(calls, [{enabled: false, expected_revision: 7}]);
  edit('enabled', '', true); await act('use-review', ctx); await act('use-apply', ctx);
  assert.equal(calls.length, 1);
  assert.match(panel('case A', false), /before enabling sharing/);
});

test('the investigation shortcut copies local CSV into the draft without importing or enabling sharing', async () => {
  const paths = [], posts = [];
  const ctx = context({caseId: 'case A', readText: async path => {paths.push(path); return {text: csv, contentType: 'text/csv'};},
    post: async (path, body) => {posts.push({path, body}); return catalog();}});
  await act('open', ctx); await act('from-case', ctx);
  assert.deepEqual(paths, ['/api/cases/case%20A/input-exports/attributions']);
  assert.equal(posts.length, 1);
  assert.match(panel('case A', false), /copied into the draft. Review before saving/);
  assert.match(panel('case A', false), /address-1,Exchange/);
});

test('ZIP exports and invalid UTF-8 files are rejected with useful instructions and consume old approvals', async () => {
  const ctx = context({caseId: 'case A', readText: async () => ({text: '', contentType: 'application/zip'})});
  await act('open', ctx); edit('text', csv); await act('preview', ctx); await act('from-case', ctx);
  assert.match(panel('case A', false), /Extract it, then import each attribution CSV part separately/);
  assert.doesNotMatch(panel('case A', false), /Review shared library changes/);
  await choose(csv, {name: 'export.zip'});
  assert.match(panel('case A', false), /ZIP of CSV parts/);
  await choose(csv, {arrayBuffer: async () => Uint8Array.of(0xff).buffer});
  assert.match(panel('case A', false), /UTF-8 CSV or JSON/);
});

test('file and pasted data enforce UTF-8 byte size, and delayed file reads cannot replace later edits', async () => {
  const ctx = context(); await act('open', ctx);
  await choose(csv, {size: 512 * 1024 + 1});
  assert.match(panel(null, false), /exceeds 512 KiB/);
  edit('text', 'é'.repeat(262145)); await act('preview', ctx);
  assert.match(panel(null, false), /exceeds 512 KiB/);
  const result = deferred();
  const work = choose(csv, {arrayBuffer: () => result.promise});
  edit('text', 'newer CSV'); result.resolve(new TextEncoder().encode(csv).buffer); await work;
  assert.match(panel(null, false), />newer CSV<\/textarea>/);
});

test('case switching during reads or opt-in writes cannot move messages or data into a different case', async () => {
  const result = deferred(), refreshes = [];
  const ctx = context({caseId: 'case A', post: async path => path === BASE ? catalog() : result.promise,
    refresh: async scope => refreshes.push(scope)});
  await act('open', ctx); edit('enabled', '', true); await act('use-review', ctx);
  const work = act('use-apply', ctx);
  await act('open', context({caseId: 'case B'}));
  result.resolve(status({enabled: true, revision: 8})); await work;
  assert.doesNotMatch(panel('case B', false), /Shared attributions enabled/);
  assert.match(panel('case A', false), /Shared attributions enabled/);
  assert.deepEqual(refreshes, ['case']);
});

test('pending or busy imports cannot double-submit and saved results survive a view refresh failure', async () => {
  const result = deferred(); let writes = 0;
  const ctx = context({post: async (path, body) => {if (path === BASE) return catalog(); if (body.approve_plan) {writes++; return result.promise;} return review();},
    refresh: async () => {throw Error('view failed');}});
  await act('open', ctx); edit('text', csv); await act('preview', {...ctx, busy: true});
  await act('apply', ctx); assert.equal(writes, 0);
  await act('preview', ctx); const work = act('apply', ctx);
  assert.equal(pending(null), true); await act('apply', ctx); assert.equal(writes, 1);
  result.resolve({changed: 1, revision: 4}); await work;
  assert.match(panel(null, false), /Saved 1 shared attribution changes/);
  assert.match(panel(null, false), /View could not refresh: view failed/);
  assert.equal(pending(null), false);
});
