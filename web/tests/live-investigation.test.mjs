import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {stripTypeScriptTypes} from 'node:module';
import {test} from 'node:test';
import vm from 'node:vm';
import * as frameRecovery from '../src/scripts/frame-recovery.ts';

// Execute the real application handlers with a small DOM and offline HTTP stub.
// The attribution panels are unrelated to new-investigation and lookup behavior.
const source = stripTypeScriptTypes(
  readFileSync(new URL('../src/scripts/app.ts', import.meta.url), 'utf8')
    .replace(/^import .*\n/gm, '')
    .replace(/^export \{\};\n/m, '')
    .replace('void initialize().catch(', 'globalThis.startup = initialize().catch('),
  {mode: 'transform'},
);
const script = new vm.Script(source + '\n globalThis.appTest = {state, dispatch, pollJob, newCase, dashboard, workspace, settingsPage, localGraph, elkGraph, compactGraph, currentCompaction, openActionDialog, readSettings, budgetFields, isBusy, pegoutsGraph, pegoutInput, currentPegoutSearch, suggestCenterNames};');
const txid = 'a'.repeat(64);
const defaults = {hops: 1, max_transactions: 20, max_outpoints: 100, max_requests: 30,
  max_seconds: 60, max_new_items: 750, layout_attempts: 25, connector_style: 'straight'};

async function harness(respond = () => undefined) {
  frameRecovery.resetFrameRecovery();
  const calls = [], listeners = {}, dialogListeners = {}, notifications = [], importActions = [];
  const elements = new Map();
  let currentForm = null;
  const app = {innerHTML: '', addEventListener(name, callback) {listeners[name] = callback;}};
  const dialog = {innerHTML: '', open: false, addEventListener(name, callback) {dialogListeners[name] = callback;},
    close() {if (this.open) {this.open = false; dialogListeners.close?.();}}, showModal() {this.open = true;}, querySelector() {return null;}};
  const context = vm.createContext({
    Error, URL, console, ...frameRecovery,
    resetNameColors() {}, resetAddressImport() {}, resetChangeOutputs() {}, resetInputImport() {},
    inputImportPending() {return false;}, inputImportPanel() {return "";},
    inputImportAction(action, context) {
      if (!action.startsWith('input-import-')) return false;
      if (!context.busy) importActions.push({action, caseId: context.caseId});
      return true;
    },
    changeOutputsPending() {return false;}, changeOutputsPanel() {return "";},
    changeOutputsAction() {return false;}, changeOutputsLookupComplete() {return false;},
    nameColorsAction() {return false;}, addressImportAction() {return false;},
    document: {
      querySelector(selector) {
        if (selector === '#app') return app;
        if (selector === '#action-dialog') return dialog;
        if (selector === '#new-case-form') return currentForm;
        if (selector === '#notifications') return {append(node) {notifications.push(node.textContent);}};
        return elements.get(selector) ?? null;
      },
      addEventListener() {},
      createElement() {return {remove() {}};},
    },
    window: {addEventListener() {}},
    history: {replaceState() {}},
    location: {hash: '', pathname: '/', reload() {}},
    setInterval() {}, setTimeout() {return 1;}, clearTimeout() {},
    FormData: class {
      constructor(form) {this.values = form.values;}
      get(key) {return this.values[key] ?? null;}
      has(key) {return key in this.values;}
    },
    async fetch(path, options) {
      const body = options.body === undefined ? undefined : JSON.parse(options.body);
      calls.push({path, body});
      const response = respond(path, body, calls.length)
        ?? (path === '/api/session' ? {csrf: 'test', settings: defaults, cases: []} : {});
      return {ok: !response.error, status: response.error ? 400 : 200, async json() {return response;}};
    },
  });
  script.runInContext(context);
  await context.startup;
  return {
    ...context.appTest, app, dialog, calls, notifications, importActions, elements,
    async submitDialog(values = {}) {
      dialogListeners.submit({target: {values, reportValidity: () => true}, preventDefault() {}});
      await new Promise(setImmediate);
    },
    async submitSettings(values) {
      const form = {id: 'settings-form', values: {...defaults, ...values}, reportValidity: () => true};
      listeners.submit({target: form, preventDefault() {}});
      await new Promise(setImmediate);
    },
    async submit(values) {
      currentForm = {id: 'new-case-form', values: {...defaults, ...values}, reportValidity: () => true};
      listeners.submit({target: currentForm, preventDefault() {}});
      await new Promise(setImmediate);
      currentForm = null;
    },
  };
}

test('startup offers an empty live investigation without fetching bundled sample transactions', async () => {
  const view = await harness();
  assert.deepEqual(view.calls, [{path: '/api/session', body: undefined}]);
  assert.equal(view.state.draft.txids, '');
  assert.equal(view.state.draft.seeds, '');
  assert.match(view.newCase(), /This lookup uses your Blockstream credits/);
  view.state.draft.txids = txid;
  await view.dispatch('lookup');
  assert.deepEqual(view.calls[1], {path: '/api/lookup', body: {source: 'live', txids: txid}});
  assert.equal(view.state.job.live, true);
});

test('successful creation sends live source and resets the draft without sample seeds', async () => {
  const detail = {id: 'newcase', name: 'My investigation', run_defaults: defaults, runs: [], seeds: [`${txid}:0`]};
  const view = await harness((path) => path === '/api/cases' || path === '/api/cases/newcase' ? detail : undefined);
  await view.submit({name: detail.name, txids: txid, seeds: `${txid}:0`, board: ''});
  const create = view.calls.find(call => call.path === '/api/cases');
  assert.equal(create.body.source, 'live');
  assert.deepEqual(create.body.seeds, [`${txid}:0`]);
  assert.equal(view.state.activeCase.id, detail.id);
  assert.equal(view.state.draft.txids, '');
  assert.equal(view.state.draft.seeds, '');
  assert.equal(view.state.draft.selected.size, 0);
  assert.equal(view.isBusy(), false);
});

test('failed creation retains investigator inputs and releases the form for retry', async () => {
  const view = await harness(path => path === '/api/cases' ? {error: 'Could not save investigation.'} : undefined);
  await view.submit({name: 'Keep this name', txids: txid, seeds: `${txid}:2`, board: ''});
  assert.equal(view.state.draft.name, 'Keep this name');
  assert.equal(view.state.draft.txids, txid);
  assert.equal(view.state.draft.seeds, `${txid}:2`);
  assert.match(view.state.error, /Could not save investigation/);
  assert.equal(view.isBusy(), false);
});

for (const live of [true, false]) {
  test(`recovered ${live ? 'live' : 'synthetic'} lookup ${live ? 'restores' : 'cannot populate'} a live draft`, async () => {
    let jobReads = 0;
    const view = await harness(path => {
      if (path === '/api/session') return {csrf: 'test', settings: defaults, cases: [], active_job: jobReads ? null : 'lookup1'};
      if (path === '/api/jobs/lookup1') return {id: 'lookup1', action: 'lookup', live,
        status: jobReads++ ? 'succeeded' : 'running', result: {transactions: [{txid, outputs: []}]}};
    });
    assert.equal(view.state.job.live, live);
    await view.pollJob();
    assert.equal(view.state.job, null);
    if (live) {
      assert.equal(view.state.draft.txids, txid);
      assert.equal(view.state.draft.reports.length, 1);
    } else {
      assert.equal(view.state.draft.txids, '');
      assert.equal(view.state.draft.reports.length, 0);
      assert.match(view.state.error, /lookup used synthetic data/);
    }
  });
}

test('saved fixture investigations retain synthetic provenance and offline trace routing', async () => {
  const view = await harness();
  const detail = {id: 'savedcase', name: 'Archived investigation', fixture: true, run_defaults: defaults, runs: []};
  view.state.cases = [detail];
  view.state.activeCase = detail;
  assert.match(view.dashboard(), /Synthetic data/);
  view.openActionDialog('trace');
  assert.match(view.dialog.innerHTML, /Offline synthetic data/);
  assert.doesNotMatch(view.dialog.innerHTML, /Uses your Blockstream API credits/);
  view.openActionDialog('miro-create');
  assert.match(view.dialog.innerHTML, /SYNTHETIC DATA · Archived investigation/);
  assert.match(view.dialog.innerHTML, /Changes your Miro workspace/);
});

test('input CSV download is available before and after tracing, including while busy', async () => {
  const view = await harness();
  const caseId = 'case \'"><script>name</script>';
  const detail = {id: caseId, name: 'My investigation', run_defaults: defaults, runs: []};
  view.state.activeCase = detail;
  const expected = `/api/cases/${encodeURIComponent(caseId).replace(/'/g, '&#39;')}/input-exports/all`;
  for (const traced of [false, true]) {
    detail.runs = traced ? [{id: 'savedrun', transaction_count: 1, frontier_count: 0}] : [];
    detail.latest_run = traced ? 'savedrun' : undefined;
    view.state.job = {id: 'busy', status: 'running', action: 'trace'};
    const html = view.workspace();
    assert.ok(html.includes(`href="${expected}" download>`));
    assert.match(html, /Export input CSVs/);
    assert.match(html, /all saved attributions, name colors, and change outputs across every page/);
    assert.match(html, /Unsaved edits are excluded/);
    assert.doesNotMatch(html, /<script>/);
    assert.equal((html.match(/\/input-exports\/all/g) || []).length, 1);
    const link = html.match(/<a[^>]*\/input-exports\/all[^>]*>/)[0];
    assert.doesNotMatch(link, /disabled|data-action/);
  }
  assert.equal(view.calls.length, 1, 'rendering download links does not start requests or jobs');
});

test('recovered change-output lookup cannot overwrite the new-investigation starting outputs', async () => {
  let jobReads = 0;
  const view = await harness(path => {
    if (path === '/api/session') return {csrf: 'test', settings: defaults, cases: [], active_job: jobReads ? null : 'change1'};
    if (path === '/api/jobs/change1') return {id: 'change1', action: 'change-output-lookup', case_id: 'case A', live: true,
      status: jobReads++ ? 'succeeded' : 'running', result: {txid, outputs: [{vout: 1, selectable: true}], current_vout: 1}};
  });
  view.state.draft.txids = 'Investigator draft';
  view.state.draft.seeds = `${txid}:0`;
  await view.pollJob();
  assert.equal(view.state.job, null);
  assert.equal(view.state.draft.txids, 'Investigator draft');
  assert.equal(view.state.draft.seeds, `${txid}:0`);
  assert.equal(view.state.draft.reports.length, 0);
  assert.match(view.notifications.at(-1), /Open Change outputs and load the transaction again/);
});

const pendingFrame = {title: 'Activity one', x: 10, y: 20, width: 500, height: 400};
const frameReview = extra => ({schema_version: 1, recovery: 'pending_frame_review', review_id: 'a'.repeat(64),
  run_id: 'interrupted', pending_frame: pendingFrame, candidates: [{...pendingFrame, id: 'frame-1'}],
  potential_match_count: 0, can_confirm_absent: false, ...extra});

async function recoveryHarness(review = frameReview(), failure = null) {
  let recovered = false;
  const detail = () => ({id: 'case1', name: 'My investigation', miro_board: 'board1', run_defaults: defaults,
    runs: [{id: 'interrupted'}, {id: 'latest-run'}], latest_run: 'latest-run',
    miro_recovery: {pending_count: recovered ? 0 : 1, can_confirm_empty: false, can_recover_frame: !recovered}});
  const view = await harness((path, body) => {
    if (path === '/api/cases/case1/actions') return {id: body.action === 'miro-frame-review' ? 'review1' : 'recover1', status: 'running', live: true};
    if (path === '/api/jobs/review1') return {status: 'succeeded', result: review};
    if (path === '/api/jobs/recover1') {
      if (failure) return {status: 'failed', message: failure};
      recovered = true;
      return {status: 'succeeded', result: {recovery: review.candidates.length ? 'adopted_frame' : 'confirmed_absent_frame',
        run_id: 'interrupted', resolved_count: 1, remaining_pending: 0, resume_action: review.resume_action || 'miro-frames'}};
    }
    if (path === '/api/cases/case1') return detail();
  });
  view.state.activeCase = detail();
  view.state.page = 'case';
  return view;
}

test('interrupted frame browser flow reviews first, explicitly adopts, and selects the interrupted run without syncing', async () => {
  const view = await recoveryHarness();
  assert.match(view.workspace(), /Recover interrupted frame/);
  await view.dispatch('miro-frame-review');
  assert.deepEqual(view.calls.find(call => call.path.endsWith('/actions')).body, {action: 'miro-frame-review'});
  assert.equal(view.state.job.live, true);
  await view.pollJob();
  assert.equal(view.dialog.open, true);
  assert.doesNotMatch(view.dialog.innerHTML, /<input[^>]* checked/);
  await view.submitDialog({frame_item_id: 'frame-1'});
  assert.deepEqual(view.calls.filter(call => call.path.endsWith('/actions')).at(-1).body,
    {action: 'miro-frame-recover', review_id: 'a'.repeat(64), item_id: 'frame-1'});
  await view.pollJob();
  assert.equal(view.state.selectedRun, 'interrupted');
  assert.equal(view.state.activeCase.miro_recovery.pending_count, 0);
  assert.match(view.workspace(), /Choose Create \/ update Miro frames to finish framing the saved graph/);
  assert.equal(view.calls.filter(call => call.path.endsWith('/actions')).length, 2);
});

test('absence confirmation sends no run/settings and a failed recheck requires a fresh review', async () => {
  const view = await recoveryHarness(frameReview({candidates: [], can_confirm_absent: true}), 'Board changed. Review the frame again.');
  await view.dispatch('miro-frame-review');
  await view.pollJob();
  assert.match(view.dialog.innerHTML, /expected frame is absent/);
  await view.submitDialog({confirm_frame_absent: 'on'});
  assert.deepEqual(view.calls.filter(call => call.path.endsWith('/actions')).at(-1).body,
    {action: 'miro-frame-recover', review_id: 'a'.repeat(64), confirm_absent: true});
  await view.pollJob();
  assert.match(view.state.error, /Board changed/);
  assert.equal(view.state.activeCase.miro_recovery.pending_count, 1);
  await view.submitDialog({confirm_frame_absent: 'on'});
  assert.equal(view.calls.filter(call => call.path.endsWith('/actions')).length, 2);
  assert.match(view.state.error, /Review the interrupted frame again/);
});

test('canceling or navigating away invalidates the frame review and ignores its late job', async () => {
  const view = await recoveryHarness();
  await view.dispatch('miro-frame-review');
  await view.pollJob();
  view.dialog.close();
  await view.submitDialog({frame_item_id: 'frame-1'});
  assert.equal(view.calls.filter(call => call.path.endsWith('/actions')).length, 1);
  await view.dispatch('miro-frame-review');
  await view.dispatch('dashboard');
  await view.pollJob();
  assert.equal(view.dialog.open, false);
  assert.match(view.notifications.at(-1), /Choose Recover interrupted frame again/);
  assert.equal(view.calls.filter(call => call.path.endsWith('/actions')).length, 2);
});


test('context input grouping defaults off and survives new-case and trace form submission', async () => {
  const detail = {id: 'groupcase', name: 'Grouped case', run_defaults: {...defaults, include_fees: false, group_context_inputs: true}, runs: [], seeds: [`${txid}:0`]};
  const view = await harness(path => path === '/api/cases' || path === '/api/cases/groupcase' ? detail : undefined);
  assert.match(view.newCase(), /name="group_context_inputs" type="checkbox"\//);
  await view.submit({name: detail.name, txids: txid, seeds: `${txid}:0`, group_context_inputs: 'on'});
  assert.equal(view.calls.find(call => call.path === '/api/cases').body.settings.group_context_inputs, true);
  view.openActionDialog('trace');
  assert.match(view.dialog.innerHTML, /name="group_context_inputs" type="checkbox" checked/);
  const settings = view.readSettings({values: {...defaults, group_context_inputs: 'on'}});
  assert.equal(settings.group_context_inputs, true);
  assert.equal(view.readSettings({values: defaults}).group_context_inputs, false);
});

test('changing grouping invalidates ELK and compact previews and blocks stale compact application', async () => {
  const view = await harness();
  const settings = {...defaults, include_fees: false, group_context_inputs: false};
  const artifact = {include_fees: false, connector_style: 'straight', layout_attempts: 25, preview_id: 'preview1', preview_url: '/files/artifacts/graph.html',
    downloads: [{name: 'details.html', url: '/files/artifacts/details.html'}], compaction: {unchanged: true}};
  const detail = {id: 'case', name: 'Case', run_defaults: settings, miro_board: 'board', runs: [{id: 'run1'}], latest_run: 'run1', artifacts: {run1: {compact: artifact}}};
  view.state.activeCase = detail;
  assert.match(view.elkGraph(artifact, true, settings), /Open detail pages/);
  assert.match(view.compactGraph(artifact, true, detail), /Open detail pages/);
  assert.ok(view.currentCompaction());
  settings.group_context_inputs = true;
  for (const html of [view.elkGraph(artifact, true, settings), view.compactGraph(artifact, true, detail)]) {
    assert.match(html, /different graph settings/);
    assert.doesNotMatch(html, /<iframe|Open detail pages|Apply compact layout to Miro/);
  }
  assert.equal(view.currentCompaction(), undefined);
  view.openActionDialog('miro-compact');
  assert.equal(view.dialog.open, false);
  artifact.group_context_inputs = true;
  assert.match(view.elkGraph(artifact, true, settings), /<iframe/);
  assert.match(view.compactGraph(artifact, true, detail), /Apply compact layout to Miro/);
});

test('partial ELK success stays visible in saved ELK and compact previews', async () => {
  const view = await harness();
  const settings = {...defaults, include_fees: false, group_context_inputs: false};
  const counts = {crossings: 0, node_overlaps: 0, node_intersections: 0};
  const metrics = {before: counts, after: counts, estimated: true,
    attempt_count: 25, attempted_count: 25, successful_count: 24, failed_count: 1};
  const artifact = {include_fees: false, connector_style: 'straight', layout_attempts: 25,
    preview_id: 'preview1', preview_url: '/files/artifacts/graph.html', downloads: [],
    layout_metrics: metrics, compaction: {unchanged: true}};
  const detail = {id: 'case', name: 'Case', run_defaults: settings, miro_board: 'board'};
  for (const html of [view.elkGraph(artifact, true, settings), view.compactGraph(artifact, true, detail)]) {
    assert.match(html, /24 of 25 layout attempts succeeded; 1 failed\. Best completed layout retained\./);
    assert.match(html, /<iframe/);
  }
  metrics.successful_count = 25;
  metrics.failed_count = 0;
  assert.doesNotMatch(view.elkGraph(artifact, true, settings), /Some layout attempts failed/);
  metrics.successful_count = '<script>PRIVATE</script>';
  metrics.failed_count = 1;
  const html = view.elkGraph(artifact, true, settings);
  assert.doesNotMatch(html, /PRIVATE|Some layout attempts failed/);
});

test('detail pages are optional and their links accept only local artifact URLs', async () => {
  const view = await harness();
  const settings = {...defaults, include_fees: false, group_context_inputs: false};
  const artifact = {include_fees: false, connector_style: 'straight', layout_attempts: 25, preview_url: '/files/artifacts/graph.html', downloads: []};
  assert.doesNotMatch(view.elkGraph(artifact, true, settings), /Open detail pages/);
  artifact.downloads = [{name: 'details.html', url: 'javascript:alert(1)'}];
  assert.doesNotMatch(view.elkGraph(artifact, true, settings), /javascript:|Open detail pages/);
});

test('separate branch hubs are normalized at creation and preserved by the trace-only form', async () => {
  const first = 'G' + 'a'.repeat(33), second = 'H' + 'b'.repeat(33);
  const detail = {id: 'hubcase', name: 'Hub case', run_defaults: {...defaults, include_fees: false, group_context_inputs: false, hub_addresses: [first, second]}, runs: [], seeds: [`${txid}:0`]};
  const view = await harness(path => path === '/api/cases' || path === '/api/cases/hubcase' ? detail
    : path === '/api/cases/hubcase/actions' ? {id: 'trace1', status: 'running'} : undefined);
  assert.match(view.newCase(), /Separate branch hubs/);
  await view.submit({name: detail.name, txids: txid, seeds: `${txid}:0`, hub_addresses: ` ${second}\n${first}\n\n${second} `});
  assert.deepEqual(view.calls.find(call => call.path === '/api/cases').body.settings.hub_addresses, [first, second]);
  view.openActionDialog('trace');
  assert.doesNotMatch(view.dialog.innerHTML, /name="hub_addresses"/);
  await view.submitDialog(defaults);
  const trace = view.calls.find(call => call.path === '/api/cases/hubcase/actions');
  assert.deepEqual(trace.body.settings.hub_addresses, [first, second]);
});

test('hub selection changes invalidate previews while duplicate and reordered lists remain equivalent', async () => {
  const view = await harness();
  const first = 'G' + 'a'.repeat(33), second = 'H' + 'b'.repeat(33);
  const settings = {...defaults, include_fees: false, group_context_inputs: false, hub_addresses: [first, second]};
  const artifact = {include_fees: false, connector_style: 'straight', layout_attempts: 25, preview_id: 'preview1', preview_url: '/files/artifacts/graph.html',
    downloads: [], compaction: {unchanged: true}, hub_addresses: [second, first, second]};
  const detail = {id: 'case', name: 'Case', run_defaults: settings, miro_board: 'board', runs: [{id: 'run1'}], latest_run: 'run1', artifacts: {run1: {compact: artifact}}};
  view.state.activeCase = detail;
  assert.match(view.elkGraph(artifact, true, settings), /<iframe/);
  assert.match(view.compactGraph(artifact, true, detail), /<iframe/);
  settings.hub_addresses = [first];
  assert.match(view.elkGraph(artifact, true, settings), /different graph settings/);
  assert.match(view.compactGraph(artifact, true, detail), /different graph settings/);
  assert.equal(view.currentCompaction(), undefined);
  view.openActionDialog('miro-compact');
  assert.equal(view.dialog.open, false);
});


test('layout attempts default to 25 and new-investigation forms send the chosen search size', async () => {
  const detail = {id: 'layoutcase', name: 'Layout case', run_defaults: {...defaults, layout_attempts: 80}, runs: [], seeds: [`${txid}:0`]};
  const view = await harness(path => path === '/api/cases' || path === '/api/cases/layoutcase' ? detail : undefined);
  assert.match(view.newCase(), /Layout attempts/);
  assert.match(view.newCase(), /name="layout_attempts" type="number" min="1" max="1000" step="1" required value="25"/);
  assert.match(view.newCase(), /Independent of trace hops/);
  await view.submit({name: detail.name, txids: txid, seeds: `${txid}:0`, layout_attempts: '80'});
  assert.equal(view.calls.find(call => call.path === '/api/cases').body.settings.layout_attempts, 80);
  assert.match(view.workspace(), /<dt>Layout attempts<\/dt><dd>80<\/dd>/);
});

test('workspace and investigation settings submit layout attempts independently', async () => {
  const detail = {id: 'layoutcase', name: 'Layout case', run_defaults: {...defaults, layout_attempts: 70}, runs: []};
  const view = await harness(path => path === '/api/cases/layoutcase' ? detail : undefined);
  view.state.page = 'settings';
  assert.match(view.settingsPage(), /name="layout_attempts"[^>]+value="25"/);
  await view.submitSettings({layout_attempts: '100'});
  assert.equal(view.calls.find(call => call.path === '/api/settings').body.settings.layout_attempts, 100);
  view.state.activeCase = detail;
  view.state.page = 'case-settings';
  assert.match(view.settingsPage(), /name="layout_attempts"[^>]+value="70"/);
  await view.submitSettings({name: detail.name, board: '', layout_attempts: '90'});
  assert.equal(view.calls.find(call => call.path === '/api/cases/layoutcase/settings').body.settings.layout_attempts, 90);
});

test('a limited form preserves the prior layout-attempt count when the input is absent', async () => {
  const view = await harness();
  const {layout_attempts, ...limited} = defaults;
  assert.equal(view.readSettings({values: limited}).layout_attempts, 25);
  assert.equal(view.readSettings({values: limited}, {...defaults, hub_addresses: [], layout_attempts: 150}).layout_attempts, 150);
  assert.equal(view.readSettings({values: {...limited, layout_attempts: '1'}}, {...defaults, hub_addresses: [], layout_attempts: 150}).layout_attempts, 1);
});

test('legacy or different layout-attempt counts invalidate both previews and compact application', async () => {
  const view = await harness();
  const settings = {...defaults, include_fees: false, group_context_inputs: false, hub_addresses: []};
  const artifact = {include_fees: false, connector_style: 'straight', preview_id: 'preview1', preview_url: '/files/artifacts/graph.html',
    downloads: [], compaction: {unchanged: true}};
  const detail = {id: 'case', name: 'Case', run_defaults: settings, miro_board: 'board', runs: [{id: 'run1'}], latest_run: 'run1', artifacts: {run1: {compact: artifact}}};
  view.state.activeCase = detail;
  for (const attempts of [undefined, 1, 24, 26]) {
    if (attempts === undefined) delete artifact.layout_attempts;
    else artifact.layout_attempts = attempts;
    for (const html of [view.elkGraph(artifact, true, settings), view.compactGraph(artifact, true, detail)]) {
      assert.match(html, /different graph settings/);
      assert.doesNotMatch(html, /<iframe|Apply compact layout to Miro/);
    }
    assert.equal(view.currentCompaction(), undefined);
  }
  artifact.layout_attempts = 25;
  assert.match(view.elkGraph(artifact, true, settings), /<iframe/);
  assert.match(view.compactGraph(artifact, true, detail), /Apply compact layout to Miro/);
  settings.layout_attempts = 100;
  assert.equal(view.currentCompaction(), undefined);
});


test('frames use a separate confirmed live job for the selected snapshot', async () => {
  const detail = {id: 'case1', name: 'Completed graph', miro_board: 'board1', run_defaults: defaults,
    runs: [{id: 'first'}, {id: 'last'}], latest_run: 'last', miro_recovery: {pending_count: 0}};
  const view = await harness((path) => path.endsWith('/actions')
    ? {id: 'frames1', status: 'running', live: true} : undefined);
  view.state.activeCase = detail;
  view.state.selectedRun = 'first';
  assert.match(view.workspace(), /Create \/ update Miro frames/);
  assert.match(view.workspace(), /When the graph is finished, create its frames separately/);
  await view.dispatch('miro-frames-dialog');
  assert.equal(view.dialog.open, true);
  assert.match(view.dialog.innerHTML, /current Miro positions/);
  assert.match(view.dialog.innerHTML, /Changes your Miro workspace/);
  assert.equal(view.calls.filter(call => call.path.endsWith('/actions')).length, 0);
  await view.submitDialog();
  assert.deepEqual(view.calls.find(call => call.path.endsWith('/actions')).body,
    {action: 'miro-frames', run_id: 'first'});
  assert.equal(view.state.job.live, true);
});

test('frame controls require a saved run, board, idle workspace, and resolved recovery', async () => {
  for (const condition of ['run', 'board', 'busy', 'recovery']) {
    const view = await harness();
    view.state.activeCase = {id: 'case1', name: 'Graph', miro_board: condition === 'board' ? null : 'board1',
      run_defaults: defaults, runs: condition === 'run' ? [] : [{id: 'run1'}],
      latest_run: condition === 'run' ? undefined : 'run1',
      miro_recovery: {pending_count: condition === 'recovery' ? 1 : 0}};
    if (condition === 'busy') view.state.job = {id: 'busy', status: 'running', action: 'trace'};
    const control = view.workspace().match(/<button[^>]*data-action="miro-frames-dialog"[^>]*>/)[0];
    assert.match(control, /disabled/);
    await view.dispatch('miro-frames-dialog');
    assert.equal(view.dialog.open, false, condition);
    assert.equal(view.calls.length, 1);
  }
});

test('a pending recovery appearing after frame confirmation opened blocks submission', async () => {
  const view = await harness();
  view.state.activeCase = {id: 'case1', name: 'Graph', miro_board: 'board1', run_defaults: defaults,
    runs: [{id: 'run1'}], latest_run: 'run1', miro_recovery: {pending_count: 0}};
  await view.dispatch('miro-frames-dialog');
  assert.equal(view.dialog.open, true);
  view.state.activeCase.miro_recovery.pending_count = 1;
  await view.submitDialog();
  assert.equal(view.calls.filter(call => call.path.endsWith('/actions')).length, 0);
});


test('legacy interrupted graph sync recovery guides one graph sync before separate framing', async () => {
  const view = await recoveryHarness(frameReview({resume_action: 'miro-sync'}));
  await view.dispatch('miro-frame-review');
  await view.pollJob();
  assert.match(view.dialog.innerHTML, /finish Sync to Miro for this snapshot before choosing Create \/ update Miro frames/);
  await view.submitDialog({frame_item_id: 'frame-1'});
  await view.pollJob();
  assert.match(view.workspace(), /Finish Sync to Miro for this snapshot, then choose Create \/ update Miro frames/);
  assert.equal(view.calls.filter(call => call.path.endsWith('/actions')).length, 2);
});


test('frame result counts distinguish frame updates from detached graph children', async () => {
  const view = await harness();
  view.state.activeCase = {id: 'case1', name: 'Graph', miro_board: 'board1', run_defaults: defaults,
    runs: [{id: 'run1'}], latest_run: 'run1'};
  view.state.results.set('case1', {action: 'miro-frames', result: {
    run_id: 'run1', created: 3, created_frames: 3, updated: 29, updated_frames: 2, deleted: 1}});
  const html = view.workspace();
  assert.match(html, /<strong>2<\/strong>Updated frames/);
  assert.doesNotMatch(html, /<strong>29<\/strong>Updated frames/);
});

test('fresh board action stays available during old-board recovery and pins reviewed snapshot/source', async () => {
  const detail = {id: 'case1', name: 'Fresh graph', miro_board: 'OLD=', latest_run: 'saved1',
    run_defaults: defaults, runs: [{id: 'saved1'}, {id: 'saved2'}],
    miro_recovery: {pending_count: 1}};
  const view = await harness(path => path.endsWith('/actions') ? {id: 'rebuild1', status: 'running'} : undefined);
  view.state.activeCase = detail;
  const control = view.workspace().match(/<button[^>]*data-action="miro-rebuild-dialog"[^>]*>/)[0];
  assert.doesNotMatch(control, /disabled/);
  await view.dispatch('miro-rebuild-dialog');
  assert.match(view.dialog.innerHTML, /previous board and its comments and manual edits stay there/i);
  assert.match(view.dialog.innerHTML, /name="max_new_items"/);
  assert.match(view.dialog.innerHTML, /all graph objects and connections/);
  assert.match(view.dialog.innerHTML, /Create frames separately/);
  view.state.selectedRun = 'saved2';
  detail.miro_board = 'CHANGED';
  await view.submitDialog({board_name: 'Fresh copy', max_new_items: '5000'});
  assert.deepEqual(view.calls.find(call => call.path.endsWith('/actions')).body, {
    action: 'miro-rebuild', run_id: 'saved1', source_board: 'OLD=', name: 'Fresh copy', max_new_items: 5000});
});

test('resume rebuild forwards original source and saved run after replacement became linked', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'resume1', status: 'running'} : undefined);
  view.state.activeCase = {id: 'case1', name: 'Fresh graph', miro_board: 'NEW=', latest_run: 'later',
    run_defaults: defaults, runs: [{id: 'original'}, {id: 'later'}],
    miro_rebuild: {status: 'syncing', previous_board_id: 'OLD=', board_id: 'NEW=', run_id: 'original', name: 'Saved copy'}};
  assert.match(view.workspace(), /Resume board rebuild/);
  await view.dispatch('miro-rebuild-dialog');
  assert.match(view.dialog.innerHTML, /value="Saved copy" readonly/);
  await view.submitDialog({board_name: 'Saved copy', max_new_items: '6000'});
  assert.deepEqual(view.calls.find(call => call.path.endsWith('/actions')).body, {
    action: 'miro-rebuild', run_id: 'original', source_board: 'OLD=', name: 'Saved copy', max_new_items: 6000});
});

test('uncertain board creation cannot request another board; completed rebuild can use current board', async () => {
  const view = await harness();
  const detail = {id: 'case1', name: 'Fresh graph', miro_board: 'NEW=', latest_run: 'saved1',
    run_defaults: defaults, runs: [{id: 'saved1'}],
    miro_rebuild: {status: 'pending', previous_board_id: 'OLD=', run_id: 'saved1', name: 'Saved copy', notice: 'Inspect your Miro boards.'}};
  view.state.activeCase = detail;
  const control = view.workspace().match(/<button[^>]*data-action="miro-rebuild-dialog"[^>]*>/)[0];
  assert.match(control, /disabled/);
  await view.dispatch('miro-rebuild-dialog');
  assert.equal(view.dialog.open, false);
  detail.miro_rebuild.status = 'complete';
  await view.dispatch('miro-rebuild-dialog');
  assert.equal(view.dialog.open, true);
  assert.match(view.dialog.innerHTML, /board\/NEW%3D/);
});

test('failed rebuild refreshes receipt status and renders a resume action', async () => {
  const refreshed = {id: 'case1', name: 'Fresh graph', miro_board: 'NEW=', latest_run: 'saved1',
    run_defaults: defaults, runs: [{id: 'saved1'}],
    miro_rebuild: {status: 'syncing', previous_board_id: 'OLD=', board_id: 'NEW=', run_id: 'saved1', name: 'Saved copy'}};
  const view = await harness(path => path === '/api/jobs/rebuild1'
    ? {status: 'failed', message: 'Publication interrupted.'}
    : path === '/api/cases/case1' ? refreshed : undefined);
  view.state.activeCase = {...refreshed, miro_board: 'OLD=', miro_rebuild: undefined};
  view.state.job = {id: 'rebuild1', action: 'miro-rebuild', caseId: 'case1', live: true};
  await view.pollJob();
  assert.equal(view.state.activeCase.miro_board, 'NEW=');
  assert.match(view.workspace(), /Resume board rebuild/);
});

test('completed rebuild displays both board links and selects the published snapshot', async () => {
  const detail = {id: 'case1', name: 'Fresh graph', miro_board: 'NEW=', latest_run: 'later',
    run_defaults: defaults, runs: [{id: 'original'}, {id: 'later'}]};
  const result = {run_id: 'original', board_id: 'NEW=', previous_board_id: 'OLD=', rebuild_status: 'complete'};
  const view = await harness(path => path === '/api/jobs/rebuild1' ? {status: 'succeeded', result}
    : path === '/api/cases/case1' ? detail : undefined);
  view.state.activeCase = {...detail, miro_board: 'OLD='};
  view.state.selectedRun = 'later';
  view.state.job = {id: 'rebuild1', action: 'miro-rebuild', caseId: 'case1', live: true};
  await view.pollJob();
  assert.equal(view.state.selectedRun, 'original');
  const html = view.workspace();
  assert.match(html, /Open rebuilt board/);
  assert.match(html, /Open previous board/);
  assert.match(html, /board\/NEW%3D/);
  assert.match(html, /board\/OLD%3D/);
});


test('attribution arrow colors can be enabled and disabled in investigation settings', async () => {
  const detail = {id: 'arrowcase', name: 'Named arrows', run_defaults: {...defaults, color_attribution_arrows: true}, runs: []};
  const view = await harness(path => path === '/api/cases/arrowcase' ? detail : undefined);
  assert.match(view.newCase(), /name="color_attribution_arrows" type="checkbox"\//);
  view.state.activeCase = detail;
  view.state.page = 'case-settings';
  assert.match(view.settingsPage(), /name="color_attribution_arrows" type="checkbox" checked/);
  assert.match(view.settingsPage(), /Color arrows by attribution/);
  await view.submitSettings({name: detail.name, color_attribution_arrows_present: '1', color_attribution_arrows: 'on'});
  assert.equal(view.calls.findLast(call => call.path === '/api/cases/arrowcase/settings').body.settings.color_attribution_arrows, true);
  await view.submitSettings({name: detail.name, color_attribution_arrows_present: '1'});
  assert.equal(view.calls.findLast(call => call.path === '/api/cases/arrowcase/settings').body.settings.color_attribution_arrows, false);
});

test('limited forms preserve attribution arrow settings and explicit unchecked controls disable them', async () => {
  const view = await harness();
  const previous = {...defaults, hub_addresses: [], color_attribution_arrows: true};
  assert.equal(view.readSettings({values: defaults}, previous).color_attribution_arrows, true);
  assert.equal(view.readSettings({values: defaults}).color_attribution_arrows, false);
  assert.equal(view.readSettings({values: {...defaults, color_attribution_arrows_present: '1'}}, previous).color_attribution_arrows, false);
  assert.equal(view.readSettings({values: {...defaults, color_attribution_arrows: 'on'}}).color_attribution_arrows, true);
});

test('changing attribution arrow colors invalidates Mermaid, ELK and compact previews', async () => {
  const view = await harness();
  const settings = {...defaults, include_fees: false, group_context_inputs: false, hub_addresses: [], color_attribution_arrows: false};
  const artifact = {...settings, downloads: [], preview_url: '/files/artifacts/graph.html', compaction: {unchanged: true}};
  delete artifact.color_attribution_arrows;
  const detail = {id: 'case', name: 'Case', run_defaults: settings, miro_board: 'board', runs: [{id: 'run1'}], latest_run: 'run1', artifacts: {run1: {compact: artifact}}};
  view.state.activeCase = detail;
  assert.match(view.localGraph(artifact, true, settings), /<iframe/);
  assert.match(view.elkGraph(artifact, true, settings), /<iframe/);
  assert.ok(view.currentCompaction());
  settings.color_attribution_arrows = true;
  for (const html of [view.localGraph(artifact, true, settings), view.elkGraph(artifact, true, settings), view.compactGraph(artifact, true, detail)]) {
    assert.match(html, /different graph settings/);
    assert.doesNotMatch(html, /<iframe/);
  }
  assert.equal(view.currentCompaction(), undefined);
  artifact.color_attribution_arrows = true;
  assert.match(view.localGraph(artifact, true, settings), /<iframe/);
  assert.match(view.elkGraph(artifact, true, settings), /<iframe/);
  assert.ok(view.currentCompaction());
});


test('one CSV import entry is available before the first trace and opens from review pages', async () => {
  const view = await harness();
  view.state.activeCase = {id: 'csvcase', name: 'CSV investigation', run_defaults: defaults, runs: [], seeds: [`${txid}:0`]};
  assert.match(view.workspace(), /data-action="input-import-open"[^>]*>Import CSV files/);
  assert.doesNotMatch(view.workspace(), /data-action="address-import-open"/);
  view.state.page = 'addresses';
  await view.dispatch('input-import-open');
  assert.equal(view.state.page, 'case');
  assert.deepEqual(view.importActions, [{action: 'input-import-open', caseId: 'csvcase'}]);
  assert.equal(view.calls.length, 1, 'opening the shared importer needs no lookup or trace');
  view.state.job = {id: 'running', action: 'trace', caseId: 'csvcase'};
  await view.dispatch('input-import-open');
  assert.equal(view.importActions.length, 1, 'running jobs keep importing disabled');
});

const pegoutSearch = (overrides = {}) => ({id: '1'.repeat(16), txid, min_hops: 2, max_hops: 4,
  status: 'paused', stop_reason: 'request_limit', match_count: 1,
  artifact: {preview_id: '1'.repeat(16) + '-pegouts-12345678', downloads: [],
    preview_url: '/files/case1/previews/pegouts/graph.html'}, ...overrides});
const pegoutCase = (searches = [], overrides = {}) => ({id: 'case1', name: 'Peg-out investigation',
  run_defaults: defaults, runs: [], seeds: [], pegout_searches: searches, ...overrides});
const pegoutEdit = (view, id, value, checked = false) => view.pegoutInput({id: 'pegouts-' + id, value, checked});

test('peg-out search is available before a full run, validates range, and starts a real bounded search', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'pegjob', status: 'running'} : undefined);
  view.state.activeCase = pegoutCase();
  assert.match(view.pegoutsGraph(view.state.activeCase), /data-action="pegouts-start"[^>]*>/);
  assert.match(view.pegoutsGraph(view.state.activeCase), /Hop 0 is that transaction/);
  pegoutEdit(view, 'txid', txid.toUpperCase()); pegoutEdit(view, 'min', '2'); pegoutEdit(view, 'max', '4');
  await view.dispatch('pegouts-start');
  assert.deepEqual(view.calls.at(-1), {path: '/api/cases/case1/actions', body: {action: 'pegouts', txid, min_hops: 2, max_hops: 4}});
  assert.equal(view.state.job.live, true);
  assert.equal(view.state.job.action, 'pegouts');
});

test('peg-out invalid IDs and inverted or noninteger ranges never start jobs', async () => {
  for (const [id, low, high] of [['bad', '0', '4'], [txid, '5', '4'], [txid, '-1', '4'],
    [txid, '0', '2.5'], [txid, '', '4'], [txid, '0', '2147483648']]) {
    const view = await harness(); view.state.activeCase = pegoutCase();
    pegoutEdit(view, 'txid', id); pegoutEdit(view, 'min', low); pegoutEdit(view, 'max', high);
    await assert.rejects(view.dispatch('pegouts-start'), /transaction ID|hop limits/);
    assert.equal(view.calls.length, 1);
  }
});

test('peg-out hop zero is inclusive and fixture searches stay offline', async () => {
  const view = await harness(); view.state.activeCase = pegoutCase([], {fixture: true});
  pegoutEdit(view, 'txid', txid); pegoutEdit(view, 'min', '0'); pegoutEdit(view, 'max', '0');
  await view.dispatch('pegouts-start');
  assert.equal(view.state.job.live, false);
  assert.deepEqual(view.calls.at(-1).body, {action: 'pegouts', txid, min_hops: 0, max_hops: 0});
});

test('peg-out resume and preview target the independent search rather than the main run', async () => {
  for (const [action, field, live] of [['pegouts-resume', 'resume', true], ['pegouts-preview', 'search_id', false]]) {
    const view = await harness(); const search = pegoutSearch();
    view.state.activeCase = pegoutCase([search], {latest_run: 'main-run'});
    view.state.selectedRun = 'main-run';
    await view.dispatch(action);
    assert.deepEqual(view.calls.at(-1).body, {action: action === 'pegouts-resume' ? 'pegouts' : action, [field]: search.id});
    assert.equal(view.state.selectedRun, 'main-run');
    assert.equal(view.state.job.live, live);
  }
});

test('peg-out partial and empty results describe coverage and retain recovery controls', async () => {
  const view = await harness(); view.state.activeCase = pegoutCase([pegoutSearch({match_count: 0, recoverable: true})]);
  const html = view.pegoutsGraph(view.state.activeCase);
  assert.match(html, /Additional peg-outs may remain undiscovered/);
  assert.match(html, /No matching peg-out found in the searched data/);
  assert.match(html, /Recover and resume search/);
  assert.doesNotMatch(html, /data-action="miro-pegouts"/);
  assert.doesNotMatch(html, /title="Peg-out paths"/);
});

test('peg-out publication requires approval for the exact selected preview and board', async () => {
  const view = await harness(); const one = pegoutSearch(), two = pegoutSearch({id: '2'.repeat(16),
    artifact: {preview_id: '2'.repeat(16) + '-pegouts-87654321', downloads: []}});
  view.state.activeCase = pegoutCase([one, two]);
  await assert.rejects(view.dispatch('miro-pegouts'), /confirm publication/);
  pegoutEdit(view, 'board', 'board-url'); pegoutEdit(view, 'confirm', '', true);
  pegoutEdit(view, 'history', two.id);
  await assert.rejects(view.dispatch('miro-pegouts'), /confirm publication/);
  pegoutEdit(view, 'board', 'board-url'); pegoutEdit(view, 'confirm', '', true);
  pegoutEdit(view, 'board', 'other-board');
  await assert.rejects(view.dispatch('miro-pegouts'), /confirm publication/);
  pegoutEdit(view, 'confirm', '', true);
  await view.dispatch('miro-pegouts');
  assert.deepEqual(view.calls.at(-1).body, {action: 'miro-pegouts', preview_id: two.artifact.preview_id,
    board: 'other-board', confirm_pegouts: true});
  assert.equal(view.state.job.live, true);
});

test('peg-out completion selects its search and preserves the full-trace run selection', async () => {
  const search = pegoutSearch();
  const view = await harness(path => path === '/api/jobs/pegjob' ? {status: 'succeeded', result: {search_id: search.id, status: 'paused'}}
    : path === '/api/cases/case1' ? pegoutCase([search], {latest_run: 'main-run'}) : undefined);
  view.state.activeCase = pegoutCase([], {latest_run: 'main-run'}); view.state.selectedRun = 'older-main-run';
  view.state.job = {id: 'pegjob', action: 'pegouts', caseId: 'case1', live: true};
  await view.pollJob();
  assert.equal(view.state.selectedRun, 'older-main-run');
  assert.equal(view.currentPegoutSearch(view.state.activeCase).id, search.id);
  assert.match(view.notifications.join(' '), /search paused/);
});

for (const status of ['failed', 'canceled']) {
  test(`peg-out ${status} job reloads the saved checkpoint for recovery`, async () => {
    const search = pegoutSearch({recoverable: true});
    const view = await harness(path => path === '/api/jobs/pegjob' ? {status, message: 'Stopped'}
      : path === '/api/cases/case1' ? pegoutCase([search]) : undefined);
    view.state.activeCase = pegoutCase(); view.state.job = {id: 'pegjob', action: 'pegouts', caseId: 'case1', live: true};
    await view.pollJob();
    assert.equal(view.currentPegoutSearch(view.state.activeCase).id, search.id);
    assert.equal(view.calls.some(call => call.path === '/api/cases/case1'), true);
  });
}

test('peg-out preview URLs and transaction text are rendered safely', async () => {
  const view = await harness(); view.state.activeCase = pegoutCase([pegoutSearch({txid: '<script>private</script>',
    artifact: {preview_id: 'safe', downloads: [], preview_url: 'javascript:alert(1)'}})]);
  const html = view.pegoutsGraph(view.state.activeCase);
  assert.doesNotMatch(html, /<script>|javascript:|<iframe/);
  assert.match(html, /&lt;script&gt;/);
});

test('named-group layout is opt-in, saved per case, and retained by trace-only forms', async () => {
  const detail = {id: 'centeredcase', name: 'Treasury layout', run_defaults: {...defaults, center_name: 'Treasury Group'}, runs: []};
  const view = await harness(path => path === '/api/cases/centeredcase' ? detail : undefined);
  assert.match(view.newCase(), /name="center_name" maxlength="120" value=""/);
  await view.dispatch('open-case', {dataset: {id: detail.id}});
  await view.dispatch('case-settings');
  assert.match(view.settingsPage(), /name="center_name" maxlength="120" value="Treasury Group"/);
  assert.match(view.settingsPage(), /Layout only: tracing and all connections stay the same/);
  assert.match(view.settingsPage(), /Use Sync and reorganize/);
  await view.submitSettings({name: detail.name, center_name: '  Treasury  '});
  assert.equal(view.calls.findLast(call => call.path === '/api/cases/centeredcase/settings').body.settings.center_name, 'Treasury');
  const previous = {...defaults, hub_addresses: [], center_name: 'Treasury Group'};
  assert.equal(view.readSettings({values: defaults}, previous).center_name, 'Treasury Group');
  assert.equal(view.readSettings({values: {...defaults, center_name: ''}}, previous).center_name, '');
  assert.equal(view.readSettings({values: defaults}).center_name, '');
  await view.submitSettings({name: detail.name, center_name: ''});
  assert.equal(view.calls.findLast(call => call.path === '/api/cases/centeredcase/settings').body.settings.center_name, '');
});

test('changing the centered group makes saved ELK and compact previews stale', async () => {
  const view = await harness();
  const settings = {...defaults, include_fees: false, group_context_inputs: false, hub_addresses: [], center_name: ''};
  const artifact = {include_fees: false, connector_style: 'straight', layout_attempts: 25, downloads: [], preview_id: 'preview1',
    preview_url: '/files/artifacts/graph.html', compaction: {unchanged: true}};
  view.state.activeCase = {id: 'centeredcase', name: 'Treasury layout', run_defaults: settings, runs: [{id: 'run1'}], latest_run: 'run1', artifacts: {run1: {compact: artifact}}};
  assert.match(view.elkGraph(artifact, true, settings), /<iframe/);
  assert.ok(view.currentCompaction());
  settings.center_name = 'Treasury Group';
  assert.doesNotMatch(view.elkGraph(artifact, true, settings), /<iframe/);
  assert.equal(view.currentCompaction(), undefined);
  artifact.center_name = 'Treasury Group';
  assert.match(view.elkGraph(artifact, true, settings), /<iframe/);
  assert.ok(view.currentCompaction());
  settings.center_name = 'Other Group';
  assert.doesNotMatch(view.elkGraph(artifact, true, settings), /<iframe/);
});

test('named-group suggestions use local active attribution names and escape option text', async () => {
  const view = await harness(path => path === '/api/cases/centeredcase/name-colors' ? {rows: [
    {name: 'Treasury Group', enabled_addresses: 4}, {name: 'Inactive', enabled_addresses: 0},
    {name: 'Group "<test>', enabled_addresses: 2},
  ]} : undefined);
  view.state.activeCase = {id: 'centeredcase', name: 'Case', run_defaults: defaults, runs: []};
  view.state.page = 'case-settings';
  const list = {innerHTML: ''};
  view.elements.set('#settings-form input[name="center_name"]', {value: ' Treasury '});
  view.elements.set('#center-name-options', list);
  await view.suggestCenterNames();
  assert.deepEqual(view.calls.findLast(call => call.path.endsWith('/name-colors')).body, {query: 'Treasury', offset: 0, limit: 100});
  assert.match(list.innerHTML, /value="Treasury Group"/);
  assert.doesNotMatch(list.innerHTML, /Inactive/);
  assert.match(list.innerHTML, /Group &quot;&lt;test&gt;/);
  assert.doesNotMatch(list.innerHTML, /<test>/);
});
