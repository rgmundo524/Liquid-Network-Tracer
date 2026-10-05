import assert from 'node:assert/strict';
import {beforeEach, test} from 'node:test';
import {normalizeSeeds, sameSeeds, seedEditorAction as action, seedEditorInput as input,
  seedEditorPanel as panel, seedEditorPending as pending, forgetSeedEditor as forget,
} from '../src/scripts/seed-editor.ts';

const txA = 'a'.repeat(64), txB = 'b'.repeat(64);
const original = {seeds: [`${txA}:0`], revision: 7};
const deferred = () => Promise.withResolvers();
const act = (name, ctx) => action('seed-editor-' + name, ctx);
const edit = (value, caseId = 'case A') => input({id: 'seed-editor-text', value, dataset: {caseId}});
function context(overrides = {}) {
  return {caseId: 'case A', busy: false, render() {}, get: async () => original,
    post: async (_path, body) => ({seeds: body.seeds, revision: 8}), saved() {}, ...overrides};
}
beforeEach(() => {forget('case A'); forget('case B'); forget('unsafe /?#');});

test('seed normalization accepts delimited outpoints, canonicalizes case and vout, and removes duplicates', () => {
  assert.deepEqual(normalizeSeeds(` ${txB.toUpperCase()}:0002,${txA}:10\n${txA}:01\t${txB}:2 `),
    [`${txA}:1`, `${txA}:10`, `${txB}:2`]);
  assert.deepEqual(normalizeSeeds(`${txA}:4294967295`), [`${txA}:4294967295`]);
  assert.equal(sameSeeds([`${txB.toUpperCase()}:002`, `${txA}:0`, `${txA}:0`], [`${txA}:0`, `${txB}:2`]), true);
  assert.equal(sameSeeds([`${txA}:0`], [`${txA}:1`]), false);
  assert.equal(sameSeeds([], []), true);
  assert.equal(sameSeeds(undefined, []), false);
  assert.equal(sameSeeds(['invalid'], ['invalid']), false);
});

test('empty, malformed, fractional, negative, and out-of-range seed outputs are rejected', () => {
  for (const value of ['', ' , \n', txA, `${txA}:`, `${txA}:-1`, `${txA}:1.5`, `${txA}:1e2`,
    `${txA}:4294967296`, `${txA}:9007199254740993`, `${'g'.repeat(64)}:0`, `${txA}:0:1`, '<script>:0'])
    assert.throws(() => normalizeSeeds(value), /Enter at least one|full 64-character/, value);
});

test('open reads only the exact escaped case route and saving submits canonical outputs at the loaded revision', async () => {
  const calls = [], saves = [];
  const ctx = context({caseId: 'unsafe /?#', get: async path => {calls.push({path}); return original;},
    post: async (path, body) => {calls.push({path, body}); return {seeds: body.seeds, revision: 8};},
    saved: selection => saves.push(selection)});
  assert.equal(panel(ctx.caseId, false), '');
  await act('open', ctx);
  assert.deepEqual(calls, [{path: '/api/cases/unsafe%20%2F%3F%23/seeds'}]);
  edit(`${txB.toUpperCase()}:01,${txB}:1`, ctx.caseId);
  await act('save', ctx);
  assert.deepEqual(calls.at(-1), {path: '/api/cases/unsafe%20%2F%3F%23/seeds',
    body: {seeds: [`${txB}:1`], expected_revision: 7}});
  assert.deepEqual(saves, [{seeds: [`${txB}:1`], revision: 8}]);
  assert.match(panel(ctx.caseId, false), /Existing previews keep their original outputs/);
});

test('invalid draft validation prevents writes and preserves safe error and editable text', async () => {
  let writes = 0;
  const ctx = context({post: async () => {writes++; return original;}});
  await act('open', ctx); edit('<img src=x>'); await act('save', ctx);
  assert.equal(writes, 0);
  assert.match(panel('case A', false), /role="alert"/);
  assert.match(panel('case A', false), /&lt;img src=x&gt;/);
  assert.doesNotMatch(panel('case A', false), /<img/);
  assert.equal(pending('case A'), false);
});

test('stale revision failure retains the draft until explicit reload retrieves the new revision', async () => {
  const bodies = [], saves = []; let revision = 7;
  const ctx = context({get: async () => ({...original, revision}),
    post: async (_path, body) => {bodies.push(body); if (body.expected_revision !== revision) throw Error('Starting outputs changed. Reload before saving.'); return {seeds: body.seeds, revision: ++revision};},
    saved: selection => saves.push(selection)});
  await act('open', ctx); edit(`${txB}:3`); revision = 8; await act('save', ctx);
  assert.match(panel('case A', false), new RegExp(`${txB}:3`));
  assert.match(panel('case A', false), /Starting outputs changed. Reload before saving/);
  assert.deepEqual(saves, []);
  await act('reload', ctx); assert.match(panel('case A', false), new RegExp(`${txA}:0`));
  edit(`${txB}:3`); await act('save', ctx);
  assert.equal(bodies.at(-1).expected_revision, 8);
  assert.equal(saves.length, 1);
});

test('busy and pending state prevent duplicate requests and input changes during a write', async () => {
  const result = deferred(); let reads = 0, writes = 0;
  const ctx = context({get: async () => {reads++; return original;}, post: () => {writes++; return result.promise;}});
  await act('open', {...ctx, busy: true}); assert.equal(reads, 0);
  await act('open', ctx); edit(`${txB}:2`);
  await act('save', {...ctx, busy: true}); assert.equal(writes, 0);
  const work = act('save', ctx); assert.equal(pending('case A'), true);
  edit(`${txA}:4`); await act('save', ctx); await act('reload', ctx);
  assert.equal(writes, 1); assert.equal(reads, 1);
  assert.match(panel('case A', false), /data-action="seed-editor-save" disabled/);
  result.resolve({seeds: [`${txB}:2`], revision: 8}); await work;
  assert.match(panel('case A', false), new RegExp(`${txB}:2`));
  assert.equal(pending('case A'), false);
});

test('close and reopen preserve unsaved input without another read', async () => {
  let reads = 0;
  const ctx = context({get: async () => {reads++; return original;}});
  await act('open', ctx); edit(`${txB}:2`); await act('close', ctx);
  assert.equal(panel('case A', false), '');
  await act('open', ctx); assert.equal(reads, 1);
  assert.match(panel('case A', false), new RegExp(`${txB}:2`));
  assert.equal(input({id: 'not-seeds', value: 'other', dataset: {caseId: 'case A'}}), false);
});

test('separate investigations keep their draft and pending state isolated', async () => {
  const result = deferred(), saves = [];
  const ctxA = context({post: () => result.promise, saved: selection => saves.push(selection)});
  await act('open', ctxA); edit(`${txB}:2`); const work = act('save', ctxA);
  await act('open', context({caseId: 'case B'})); edit(`${txB}:4`, 'case B');
  assert.equal(pending('case B'), false);
  result.resolve({seeds: [`${txB}:2`], revision: 8}); await work;
  assert.match(panel('case B', false), new RegExp(`${txB}:4`));
  assert.doesNotMatch(panel('case B', false), /Starting outputs saved/);
  assert.deepEqual(saves, [{seeds: [`${txB}:2`], revision: 8}]);
});

for (const operation of ['read', 'write']) {
  test(`forgetting an investigation invalidates an in-flight ${operation} without restoring or saving its response`, async () => {
    const result = deferred(); let saves = 0, renders = 0;
    const ctx = context({get: operation === 'read' ? () => result.promise : async () => original,
      post: () => result.promise, saved: () => {saves++;}, render: () => {renders++;}});
    if (operation === 'write') {await act('open', ctx); edit(`${txB}:3`);}
    const work = act(operation === 'read' ? 'open' : 'save', ctx);
    forget('case A'); const before = renders;
    await act('open', context());
    result.resolve({seeds: [`${txB}:3`], revision: 8}); await work;
    assert.equal(saves, 0); assert.equal(renders, before);
    assert.equal(pending('case A'), false);
    assert.match(panel('case A', false), new RegExp(`${txA}:0`));
    assert.doesNotMatch(panel('case A', false), new RegExp(`${txB}:3`));
  });
}
