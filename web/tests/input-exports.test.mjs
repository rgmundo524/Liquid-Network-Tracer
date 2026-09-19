import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {stripTypeScriptTypes} from 'node:module';
import {beforeEach, test} from 'node:test';
import vm from 'node:vm';
import {addressImportPanel, addressImportInput, resetAddressImport} from '../src/scripts/address-import.ts';
import {changeOutputsPanel, changeOutputsAction, changeOutputsInput, resetChangeOutputs} from '../src/scripts/change-outputs.ts';
import * as nameImport from '../src/scripts/name-color-import.ts';

// Execute the real parent color editor while supplying its real import-panel module.
const colorsSource = stripTypeScriptTypes(
  readFileSync(new URL('../src/scripts/name-colors.ts', import.meta.url), 'utf8')
    .replace(/^import[\s\S]*?from ['"][^'"]+['"];\n/m, '')
    .replace(/^export /gm, ''), {mode: 'transform'});
const colorsScript = new vm.Script(colorsSource + '\nglobalThis.colorsTest = {nameColorsPanel, nameColorsAction, nameColorsInput, resetNameColors};');
const caseId = 'case \'"><script>unsafe</script>';
const escapedId = encodeURIComponent(caseId).replace(/'/g, '&#39;');
let colors;

beforeEach(() => {
  globalThis.document = {querySelector() {return {focus() {}};}};
  const ctx = vm.createContext({...nameImport, document: globalThis.document});
  colorsScript.runInContext(ctx);
  colors = ctx.colorsTest;
  colors.resetNameColors(caseId);
  resetAddressImport(caseId);
  resetChangeOutputs(caseId);
});

function checkLink(html, kind) {
  assert.ok(html.includes(`href="/api/cases/${escapedId}/input-exports/${kind}" download>`));
  assert.equal((html.match(new RegExp(`/input-exports/${kind}`, 'g')) || []).length, 1);
  assert.match(html, /Export saved CSV/);
  assert.match(html, /across every page/);
  assert.match(html, /Unsaved edits/);
  assert.match(html, /column headers/);
  assert.match(html, /ZIP of CSV parts/);
  assert.doesNotMatch(html, /<script>/);
  const link = html.match(new RegExp(`<a[^>]+/input-exports/${kind}[^>]*>`))[0];
  assert.doesNotMatch(link, /disabled|data-action/);
}

test('attribution export always links to all saved entries while input text remains unsaved', () => {
  addressImportInput({id: 'attribution-text', value: 'Address,Name\nunsaved,address'});
  checkLink(addressImportPanel(caseId, true), 'attributions');
});

test('name-color export stays available in the editor and open importer without duplicate links', async () => {
  const context = {caseId, busy: false, render() {}, async post() {return {
    revision: 1, total: 101, offset: 0, limit: 100, presets: [], rows: [], roles: [], notice: 'Saved names',
  };}};
  await colors.nameColorsAction('name-colors-open', context);
  colors.nameColorsInput({id: 'name-colors-query', value: 'filtered name'});
  checkLink(colors.nameColorsPanel(caseId, true), 'name-colors');
  await colors.nameColorsAction('name-color-import-open', context);
  nameImport.nameColorImportInput({id: 'name-color-import-text', value: 'Name,Color\nunsaved,#123456'});
  const html = colors.nameColorsPanel(caseId, true);
  checkLink(html, 'name-colors');
  assert.match(html, /name-color-import-panel/);
  assert.match(html, /graph-role colors are excluded/);
});

test('change-output export remains independent of lookup drafts, filters, and import state', async () => {
  const context = {caseId, busy: false, render() {}, async post() {return {
    revision: 1, total: 101, offset: 0, limit: 100, rows: [], notice: 'Saved annotations',
  };}, async startLookup() {throw new Error('No lookup required');}};
  await changeOutputsAction('change-outputs-open', context);
  changeOutputsInput({id: 'change-outputs-query', value: 'filtered transaction'});
  changeOutputsInput({id: 'change-outputs-txid', value: 'a'.repeat(64)});
  checkLink(changeOutputsPanel(caseId, true), 'change-outputs');
  await changeOutputsAction('change-outputs-import-open', context);
  changeOutputsInput({id: 'change-outputs-text', value: 'Txid,ChangeVout\nunsaved,1'});
  const html = changeOutputsPanel(caseId, true);
  checkLink(html, 'change-outputs');
  assert.match(html, /change-outputs-import-title/);
});
