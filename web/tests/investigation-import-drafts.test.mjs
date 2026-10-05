import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {stripTypeScriptTypes} from 'node:module';
import {beforeEach, test} from 'node:test';
import vm from 'node:vm';
import * as input from '../src/scripts/input-import.ts';
import * as address from '../src/scripts/address-import.ts';
import * as nameImport from '../src/scripts/name-color-import.ts';
import * as change from '../src/scripts/change-outputs.ts';

const colorSource = stripTypeScriptTypes(
  readFileSync(new URL('../src/scripts/name-colors.ts', import.meta.url), 'utf8')
    .replace(/^import[\s\S]*?from ['"][^'"]+['"];\n/m, '')
    .replace(/^export /gm, ''), {mode: 'transform'});
const colorScript = new vm.Script(colorSource + '\nglobalThis.editor = {nameColorsPanel, nameColorsAction, nameColorsInput, resetNameColors};');
let colors, renders;
const txid = 'a'.repeat(64);
const catalog = {revision: 2, total: 1, rows: [{key: 'client', name: 'Client', variants: ['Client'],
  addresses: 1, enabled_addresses: 1, color: null}], roles: [], presets: [], notice: ''};
const lookup = {txid, current_vout: null, current_notes: '', revision: 2,
  outputs: [{vout: 0, selectable: true, address: 'output-address'}]};
const context = (caseId, post = async () => catalog) => ({caseId, busy: false, post,
  render() {renders.push(caseId);}, refresh: async () => {}, startLookup: async () => 'lookup-A'});
const deferred = () => {let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve};};
const edit = (fn, id, value, checked = false) => fn({id, value, checked});
beforeEach(() => {
  renders = [];
  globalThis.document = {querySelector() {return {focus() {}, disabled: false};}};
  const ctx = vm.createContext({...nameImport, document: globalThis.document});
  colorScript.runInContext(ctx); colors = ctx.editor;
  for (const id of ['B', 'A']) {
    input.resetInputImport(id); address.resetAddressImport(id);
    colors.resetNameColors(id); change.resetChangeOutputs(id);
  }
});

test('queued CSV files and policies remain isolated between investigation tabs; reset clears only its case', async () => {
  const csv = new TextEncoder().encode('Address,Name\naddress,Client');
  await input.inputImportAction('input-import-open', context('A'));
  await input.inputImportFiles({files: [{name: 'addresses.csv', size: csv.length, arrayBuffer: async () => csv.buffer}]}, () => {});
  edit(input.inputImportInput, 'input-import-policy-0', 'replace');
  input.inputImportPanel('B', false);
  await input.inputImportAction('input-import-open', context('B'));
  assert.doesNotMatch(input.inputImportPanel('B', false), /addresses\.csv/);
  const html = input.inputImportPanel('A', false);
  assert.match(html, /addresses\.csv/); assert.match(html, /value="replace" selected/);
  input.resetInputImport('B');
  assert.match(input.inputImportPanel('A', false), /addresses\.csv/);
  input.resetInputImport('A');
  await input.inputImportAction('input-import-open', context('A'));
  assert.doesNotMatch(input.inputImportPanel('A', false), /addresses\.csv/);
});

test('background CSV reads finish in their owning tab without rendering or blocking another tab', async () => {
  await input.inputImportAction('input-import-open', context('A'));
  const read = deferred(); const bytes = new TextEncoder().encode('Name,Color\nClient,#123456');
  const work = input.inputImportFiles({files: [{name: 'colors.csv', size: bytes.length, arrayBuffer: () => read.promise}]}, context('A').render);
  assert.equal(input.inputImportPending(), true);
  input.inputImportPanel('B', false); assert.equal(input.inputImportPending(), false);
  const before = renders.length;
  read.resolve(bytes.buffer); await work;
  assert.equal(renders.length, before); assert.equal(input.inputImportPanel('B', false), '');
  assert.match(input.inputImportPanel('A', false), /colors\.csv/); assert.equal(input.inputImportPending(), false);
});

test('advanced attribution drafts and previews resume after switching; explicit reset invalidates them', async () => {
  const response = deferred();
  edit(address.addressImportInput, 'attribution-text', 'Address,Name\naddress,Client A');
  edit(address.addressImportInput, 'attribution-replace', '', true);
  const request = address.addressImportAction('attribution-preview', context('A', () => response.promise));
  address.addressImportPanel('B', false); edit(address.addressImportInput, 'attribution-text', 'Client B');
  response.resolve({valid: true, approval_sha256: 'A-hash', unique_addresses: 1, counts: {},
    duplicate_rows: 0, active_stops_to_save: 0, changes: [], errors: [], notice: 'Reviewed for A'});
  await request;
  assert.match(address.addressImportPanel('B', false), /Client B/);
  assert.doesNotMatch(address.addressImportPanel('B', false), /Reviewed for A/);
  const html = address.addressImportPanel('A', false);
  assert.match(html, /Client A/); assert.match(html, /Reviewed for A/);
  assert.match(html, /id="attribution-replace" checked/);
  address.resetAddressImport('A');
  assert.doesNotMatch(address.addressImportPanel('A', false), /Client A|Reviewed for A/);
});

test('attribution file reads cannot overwrite newer edits after returning to their tab', async () => {
  const read = deferred();
  const work = address.addressImportFile({files: [{size: 10, name: 'old.csv', text: () => read.promise}]}, context('A').render);
  address.addressImportPanel('B', false); address.addressImportPanel('A', false);
  edit(address.addressImportInput, 'attribution-text', 'New analysis');
  read.resolve('Old analysis'); await work;
  assert.match(address.addressImportPanel('A', false), /New analysis/);
  assert.doesNotMatch(address.addressImportPanel('A', false), /Old analysis|old\.csv/);
});

test('name color drafts, filters, and nested import text survive switching without crossing cases', async () => {
  await colors.nameColorsAction('name-colors-open', context('A'));
  edit(colors.nameColorsInput, 'name-colors-query', 'Client A');
  edit(colors.nameColorsInput, 'name-colors-hex-0', '#123456');
  await colors.nameColorsAction('name-color-import-open', context('A'));
  edit(colors.nameColorsInput, 'name-color-import-text', 'Name,Color\nImport A,#abcdef');
  colors.nameColorsPanel('B', false);
  await colors.nameColorsAction('name-colors-open', context('B'));
  edit(colors.nameColorsInput, 'name-colors-hex-0', '#654321');
  const html = colors.nameColorsPanel('A', false);
  assert.match(html, /value="Client A"/); assert.match(html, /value="#123456"/);
  assert.match(html, /Import A,#abcdef/); assert.doesNotMatch(html, /#654321/);
  colors.resetNameColors('B'); assert.match(colors.nameColorsPanel('A', false), /#123456/);
  colors.resetNameColors('A'); assert.equal(colors.nameColorsPanel('A', false), '');
  assert.doesNotMatch(nameImport.nameColorImportPanel('A', false), /Import A/);
});

test('background name color preview updates its retained draft and not the active investigation', async () => {
  const response = deferred();
  await nameImport.nameColorImportAction('name-color-import-open', context('A'));
  edit(nameImport.nameColorImportInput, 'name-color-import-text', 'A colors');
  const work = nameImport.nameColorImportAction('name-color-import-preview', context('A', () => response.promise));
  nameImport.nameColorImportPanel('B', false); assert.equal(nameImport.nameColorImportPending(), false);
  response.resolve({valid: true, approval_sha256: 'colors-A', unique_names: 1, counts: {},
    duplicate_rows: 0, changes: [], errors: [], notice: 'Reviewed A colors'});
  await work;
  assert.doesNotMatch(nameImport.nameColorImportPanel('B', false), /Reviewed A colors/);
  assert.match(nameImport.nameColorImportPanel('A', false), /Reviewed A colors/);
});

test('change-output lookups complete offscreen and retain selected vouts, notes, and import drafts', async () => {
  await change.changeOutputsAction('change-outputs-open', context('A'));
  edit(change.changeOutputsInput, 'change-outputs-txid', txid);
  const start = deferred();
  const work = change.changeOutputsAction('change-outputs-lookup', {...context('A'), startLookup: () => start.promise});
  change.changeOutputsPanel('B', false); start.resolve('lookup-A'); await work;
  assert.equal(change.changeOutputsLookupComplete('A', 'lookup-A', lookup), true);
  assert.equal(change.changeOutputsPanel('B', false), '');
  assert.match(change.changeOutputsPanel('A', false), /output-address/);
  edit(change.changeOutputsInput, 'change-outputs-vout-0', '0', true);
  edit(change.changeOutputsInput, 'change-outputs-notes', 'Keep my change rationale');
  await change.changeOutputsAction('change-outputs-import-open', context('A'));
  edit(change.changeOutputsInput, 'change-outputs-text', 'Txid,ChangeVout\nDraft A,0');
  change.changeOutputsPanel('B', false);
  const html = change.changeOutputsPanel('A', false);
  assert.match(html, /id="change-outputs-vout-0"[^>]* checked/);
  assert.match(html, /Keep my change rationale/); assert.match(html, /Draft A,0/);
  change.resetChangeOutputs('A'); assert.equal(change.changeOutputsPanel('A', false), '');
  assert.equal(change.changeOutputsLookupComplete('A', 'lookup-A', lookup), false);
});

test('background import success consumes its owning approval without changing another case', async () => {
  const response = deferred();
  const review = {valid: true, approval_sha256: 'changes-A', unique_transactions: 1, counts: {},
    duplicate_rows: 0, changes: [], errors: [], notice: 'A change preview'};
  const ctx = context('A', async (_path, body) => body.approve_plan ? response.promise : ('query' in body ? catalog : review));
  await change.changeOutputsAction('change-outputs-open', ctx);
  await change.changeOutputsAction('change-outputs-import-open', ctx);
  await change.changeOutputsAction('change-outputs-import-preview', ctx);
  const work = change.changeOutputsAction('change-outputs-import-apply', ctx);
  change.changeOutputsPanel('B', false); response.resolve({changed: 1}); await work;
  assert.equal(change.changeOutputsPanel('B', false), '');
  const html = change.changeOutputsPanel('A', false);
  assert.match(html, /Saved 1 change-output annotation/); assert.doesNotMatch(html, /A change preview/);
});

test('background attribution apply invokes its owning refresh and leaves the active draft untouched', async () => {
  const applied = deferred(); const refreshed = [];
  const review = {valid: true, approval_sha256: 'address-A', unique_addresses: 1, counts: {},
    duplicate_rows: 0, active_stops_to_save: 0, changes: [], errors: [], notice: 'A assessment preview'};
  const ctx = context('A', async (_path, body) => body.approve_plan ? applied.promise : review);
  ctx.refresh = async () => {refreshed.push(ctx.caseId);};
  edit(address.addressImportInput, 'attribution-text', 'A assessment');
  await address.addressImportAction('attribution-preview', ctx);
  const work = address.addressImportAction('attribution-apply', ctx);
  address.addressImportPanel('B', false);
  edit(address.addressImportInput, 'attribution-text', 'B unsaved assessment');
  applied.resolve({changed: 1}); await work;
  assert.deepEqual(refreshed, ['A']);
  assert.match(address.addressImportPanel('B', false), /B unsaved assessment/);
  assert.doesNotMatch(address.addressImportPanel('B', false), /Saved 1 assessments/);
  assert.match(address.addressImportPanel('A', false), /Saved 1 assessments/);
});

test('background name-color apply refreshes only its owning cached catalog', async () => {
  const applied = deferred(); const requested = []; let saved = false;
  const review = {valid: true, approval_sha256: 'colors-A', unique_names: 1, counts: {},
    duplicate_rows: 0, changes: [], errors: [], notice: 'A color preview'};
  const ctx = context('A', async (path, body) => {
    requested.push(path);
    if (body.approve_plan) {await applied.promise; saved = true; return {changed: 1};}
    if (path.endsWith('/name-color-import')) return review;
    return {...catalog, rows: [{...catalog.rows[0], color: saved ? '#112233' : null}]};
  });
  await colors.nameColorsAction('name-colors-open', ctx);
  await colors.nameColorsAction('name-color-import-open', ctx);
  edit(colors.nameColorsInput, 'name-color-import-text', 'Name,Color\nClient,#112233');
  await colors.nameColorsAction('name-color-import-preview', ctx);
  const work = colors.nameColorsAction('name-color-import-apply', ctx);
  colors.nameColorsPanel('B', false);
  await colors.nameColorsAction('name-colors-open', context('B'));
  edit(colors.nameColorsInput, 'name-colors-hex-0', '#abcdef');
  applied.resolve(); await work;
  assert.equal(requested.at(-1), '/api/cases/A/name-colors');
  const active = colors.nameColorsPanel('B', false);
  assert.match(active, /value="#abcdef"/); assert.doesNotMatch(active, /Saved: #112233/);
  const restored = colors.nameColorsPanel('A', false);
  assert.match(restored, /Saved: #112233/); assert.match(restored, /Saved 1 name color assignment/);
});

test('empty investigations can import palettes and edit saved unused colors', async () => {
  let result = {...catalog, total: 0, rows: []};
  const writes = [];
  const ctx = context('A', async (_path, payload) => {
    if (payload.updates) {writes.push(payload); return {changed: 1};}
    return result;
  });
  await colors.nameColorsAction('name-colors-open', ctx);
  let html = colors.nameColorsPanel('A', false);
  assert.match(html, /Import a color palette now/);
  assert.doesNotMatch(html, /data-action="input-import-open" disabled/);
  await colors.nameColorsAction('name-color-import-open', ctx);
  assert.match(colors.nameColorsPanel('A', false), /id="name-color-import-panel"/);
  result = {...catalog, rows: [{key: 'unknown service', name: 'Unknown Service',
    variants: ['Unknown Service'], addresses: 0, enabled_addresses: 0, color: '#bdbdbd'}]};
  await colors.nameColorsAction('name-colors-load', ctx);
  html = colors.nameColorsPanel('A', false);
  assert.match(html, /Unused in this investigation/);
  assert.match(html, /Saved: #bdbdbd/);
  edit(colors.nameColorsInput, 'name-colors-hex-0', '#123456');
  await colors.nameColorsAction('name-colors-save', ctx, {dataset: {colorIndex: '0'}});
  assert.equal(writes[0].updates[0].name, 'Unknown Service');
  assert.equal(writes[0].updates[0].color, '#123456');
  await colors.nameColorsAction('name-colors-clear', ctx, {dataset: {colorIndex: '0'}});
  assert.equal(writes[1].updates[0].color, null);
});
