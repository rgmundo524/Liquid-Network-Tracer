import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
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
const script = new vm.Script(source + '\n globalThis.appTest = {state, dispatch, pollJob, newCase, dashboard, workspace, settingsPage, localGraph, elkGraph, compactGraph, currentCompaction, openActionDialog, readSettings, budgetFields, isBusy, pegoutsGraph, pegoutInput, currentPegoutSearch, suggestCenterNames, render, navigate, workflowInput, currentWorkflow};');
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
    nameColorsPanel() {return "";}, nameColorsAction() {return false;}, addressImportAction() {return false;},
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
      const form = {id: 'settings-form', values, reportValidity: () => true};
      listeners.submit({target: form, preventDefault() {}});
      await new Promise(setImmediate);
    },
    plotForm(values, valid = true) {
      const form = {id: 'plot-layout-form', values, reportValidity: () => valid};
      elements.set('#plot-layout-form', form);
      return form;
    },
    async submitPlotSettings(values, valid = true) {
      const form = this.plotForm(values, valid);
      listeners.submit({target: form, preventDefault() {}});
      await new Promise(setImmediate);
    },
    editPlotSettings(values) {
      const form = this.plotForm(values);
      listeners.input({target: {name: Object.keys(values)[0], id: '', form,
        closest: selector => selector === '#plot-layout-form' ? form : null}});
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
  assert.equal(view.state.draft.blockchain, 'liquid');
  assert.match(view.newCase(), /This lookup uses your Blockstream credits/);
  assert.match(view.newCase(), /<select name="blockchain" required><option value="liquid" selected>Liquid Network<\/option><\/select>/);
  assert.doesNotMatch(view.newCase(), /name="board"|Miro board URL or ID/);
  assert.doesNotMatch(view.newCase(), /Starting preferences/);
  view.state.draft.txids = txid;
  await view.dispatch('lookup');
  assert.deepEqual(view.calls[1], {path: '/api/lookup', body: {source: 'live', blockchain: 'liquid', txids: txid}});
  assert.equal(view.state.job.live, true);
  assert.match(view.newCase(), /<select name="blockchain" required disabled>/);
});

test('successful creation sends live source and resets the draft without sample seeds', async () => {
  const detail = {id: 'newcase', name: 'My investigation', run_defaults: defaults, runs: [], seeds: [`${txid}:0`]};
  const view = await harness((path) => path === '/api/cases' || path === '/api/cases/newcase' ? detail : undefined);
  await view.submit({name: detail.name, blockchain: 'liquid', txids: txid, seeds: `${txid}:0`});
  const create = view.calls.find(call => call.path === '/api/cases');
  assert.equal(create.body.source, 'live');
  assert.equal(create.body.blockchain, 'liquid');
  assert.equal(Object.hasOwn(create.body, 'board'), false);
  assert.deepEqual(create.body.seeds, [`${txid}:0`]);
  assert.equal(view.state.activeCase.id, detail.id);
  assert.equal(view.state.draft.txids, '');
  assert.equal(view.state.draft.seeds, '');
  assert.equal(view.state.draft.selected.size, 0);
  assert.equal(view.state.draft.blockchain, 'liquid');
  assert.equal(view.isBusy(), false);
});

test('failed creation retains investigator inputs and releases the form for retry', async () => {
  const view = await harness(path => path === '/api/cases' ? {error: 'Could not save investigation.'} : undefined);
  await view.submit({name: 'Keep this name', blockchain: 'liquid', txids: txid, seeds: `${txid}:2`});
  assert.equal(view.state.draft.name, 'Keep this name');
  assert.equal(view.state.draft.txids, txid);
  assert.equal(view.state.draft.seeds, `${txid}:2`);
  assert.equal(view.state.draft.blockchain, 'liquid');
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
    assert.equal(view.state.draft.blockchain, 'liquid');
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
  view.state.page = 'case-settings';
  const caseId = 'case \'"><script>name</script>';
  const detail = {id: caseId, name: 'My investigation', run_defaults: defaults, runs: []};
  view.state.activeCase = detail;
  const expected = `/api/cases/${encodeURIComponent(caseId).replace(/'/g, '&#39;')}/input-exports/all`;
  for (const traced of [false, true]) {
    detail.runs = traced ? [{id: 'savedrun', transaction_count: 1, frontier_count: 0}] : [];
    detail.latest_run = traced ? 'savedrun' : undefined;
    view.state.job = {id: 'busy', status: 'running', action: 'trace'};
    const html = view.settingsPage();
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
  view.state.caseView = 'boards';
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
  view.state.caseView = 'boards';
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
  assert.doesNotMatch(view.newCase(), /name="group_context_inputs"/);
  await view.submit({name: detail.name, txids: txid, seeds: `${txid}:0`, group_context_inputs: 'on'});
  assert.equal(view.calls.find(call => call.path === '/api/cases').body.settings.group_context_inputs, true);
  view.openActionDialog('trace');
  assert.doesNotMatch(view.dialog.innerHTML, /name="group_context_inputs"/);
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
  assert.doesNotMatch(view.newCase(), /name="hub_addresses"/);
  await view.submit({name: detail.name, txids: txid, seeds: `${txid}:0`, hub_addresses: ` ${second}\n${first}\n\n${second} `});
  assert.deepEqual(view.calls.find(call => call.path === '/api/cases').body.settings.hub_addresses, [first, second]);
  view.openActionDialog('trace');
  assert.doesNotMatch(view.dialog.innerHTML, /name="hub_addresses"/);
  await view.submitDialog(defaults);
  const trace = view.calls.find(call => call.path === '/api/cases/hubcase/actions');
  assert.equal(trace.body.hops, defaults.hops);
  assert.equal(trace.body.settings, undefined);
  assert.deepEqual(view.state.activeCase.run_defaults.hub_addresses, [first, second]);
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


test('inherited layout attempts are available in Plot Layouts after creating an investigation', async () => {
  const detail = {id: 'layoutcase', name: 'Layout case', run_defaults: {...defaults, layout_attempts: 80}, runs: [], seeds: [`${txid}:0`]};
  const view = await harness(path => path === '/api/cases' || path === '/api/cases/layoutcase' ? detail : undefined);
  assert.doesNotMatch(view.newCase(), /Starting preferences|Uses workspace defaults/);
  assert.doesNotMatch(view.newCase(), /name="layout_attempts"/);
  await view.submit({name: detail.name, txids: txid, seeds: `${txid}:0`, layout_attempts: '80'});
  assert.equal(view.calls.find(call => call.path === '/api/cases').body.settings.layout_attempts, 80);
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /name="layout_attempts"[^>]*value="80"/);
});

test('workspace saves layout defaults for future investigations while existing cases keep their preferences', async () => {
  const inherited = {...defaults, layout_attempts: 70, connector_style: 'curved', include_fees: true,
    group_context_inputs: true, color_attribution_arrows: true, center_name: 'Treasury', hub_addresses: ['G' + 'a'.repeat(33)]};
  const detail = {id: 'layoutcase', name: 'Layout case', run_defaults: inherited, runs: []};
  let savedDefaults = {...inherited};
  const respond = (path, body) => {
    if (path === '/api/session') return {csrf: 'test', settings: savedDefaults, cases: [detail]};
    if (path === '/api/settings') {savedDefaults = body.settings; return {settings: savedDefaults};}
    if (path === '/api/cases/layoutcase') return detail;
  };
  const view = await harness(respond);
  view.state.page = 'settings';
  const html = view.settingsPage();
  for (const key of layoutKeys) assert.equal((html.match(new RegExp(`name="${key}"`, 'g')) || []).length, 1, key);
  assert.match(html, /Starting values for new investigations/);
  await view.submitSettings({hops: '4', layout_attempts: '35', connector_style: 'elbowed',
    include_fees_present: '1', group_context_inputs_present: '1', color_attribution_arrows_present: '1',
    center_name: '', hub_addresses: ''});
  const workspace = view.calls.find(call => call.path === '/api/settings').body.settings;
  assert.deepEqual(workspace, {...inherited, hops: 4, layout_attempts: 35, connector_style: 'elbowed',
    include_fees: false, group_context_inputs: false, color_attribution_arrows: false, center_name: '', hub_addresses: []});
  assert.deepEqual(JSON.parse(JSON.stringify(view.state.draft.settings)), workspace);
  const restarted = await harness(respond);
  assert.deepEqual(JSON.parse(JSON.stringify(restarted.state.draft.settings)), workspace);
  assert.deepEqual(detail.run_defaults, inherited);
  view.state.activeCase = detail;
  view.state.page = 'case-settings';
  assert.doesNotMatch(view.settingsPage(), /name="(?:layout_attempts|connector_style|include_fees|group_context_inputs|color_attribution_arrows|center_name|hub_addresses)"/);
  await view.submitSettings({name: detail.name, hops: '6'});
  const investigation = view.calls.find(call => call.path === '/api/cases/layoutcase/settings').body.settings;
  assert.equal(investigation.hops, 6);
  for (const key of ['layout_attempts', 'connector_style', 'include_fees', 'group_context_inputs', 'color_attribution_arrows', 'center_name', 'hub_addresses']) {
    assert.deepEqual(investigation[key], inherited[key]);
  }
});

test('unsaved workspace layout defaults survive navigation without changing investigation layouts', async () => {
  const view = await harness();
  view.navigate('settings');
  view.elements.set('#settings-form', {values: {layout_attempts: '51', connector_style: 'curved',
    include_fees_present: '1', include_fees: 'on', center_name: 'Workspace Treasury'}});
  view.navigate('dashboard');
  view.elements.delete('#settings-form');
  view.state.activeCase = workflowCase({run_defaults: {...defaults, center_name: 'Case Treasury'}});
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /name="center_name"[^>]*value="Case Treasury"/);
  view.navigate('settings');
  const html = view.settingsPage();
  assert.match(html, /name="layout_attempts"[^>]*value="51"/);
  assert.match(html, /name="connector_style"><option value="straight">Straight<\/option><option value="curved" selected>/);
  assert.match(html, /name="include_fees" type="checkbox" checked/);
  assert.match(html, /name="center_name"[^>]*value="Workspace Treasury"/);
  assert.equal(view.calls.some(call => call.body), false);
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
  view.state.caseView = 'boards';
  assert.match(view.workspace(), /Create \/ update Miro frames/);
  view.state.caseView = 'boards';
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
    view.state.caseView = 'boards';
    const control = view.workspace().match(/<button[^>]*data-action="miro-frames-dialog"[^>]*>/)?.[0];
    if (condition === 'board') assert.equal(control, undefined);
    else assert.match(control, /disabled/);
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
  view.state.caseView = 'boards';
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
  view.state.caseView = 'boards';
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
  view.state.caseView = 'boards';
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
  view.state.caseView = 'boards';
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


test('attribution arrow colors can be enabled and disabled in Plot Layouts before collection', async () => {
  const detail = {id: 'arrowcase', name: 'Named arrows', run_defaults: {...defaults, color_attribution_arrows: true}, runs: []};
  const view = await harness((path, body) => {
    if (path === '/api/cases/arrowcase/plot-settings') {
      Object.assign(detail.run_defaults, body.settings);
      return detail;
    }
    if (path === '/api/cases/arrowcase') return detail;
  });
  assert.doesNotMatch(view.newCase(), /name="color_attribution_arrows"/);
  view.state.activeCase = detail;
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /name="color_attribution_arrows" type="checkbox" checked/);
  assert.match(view.workspace(), /Color arrows by attribution/);
  await view.submitPlotSettings({color_attribution_arrows_present: '1'});
  assert.equal(view.calls.findLast(call => call.path === '/api/cases/arrowcase/plot-settings').body.settings.color_attribution_arrows, false);
  await view.submitPlotSettings({color_attribution_arrows_present: '1', color_attribution_arrows: 'on'});
  assert.equal(view.calls.findLast(call => call.path === '/api/cases/arrowcase/plot-settings').body.settings.color_attribution_arrows, true);
  assert.equal(view.calls.some(call => call.path.endsWith('/actions')), false);
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
  await view.dispatch("case-settings");
  assert.match(view.settingsPage(), /data-action="input-import-open"/);
  assert.doesNotMatch(view.settingsPage(), /data-action="address-import-open"/);
  view.state.page = 'addresses';
  await view.dispatch('input-import-open');
  assert.equal(view.state.page, 'case-settings');
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

test('real case-detail responses enable the saved-seed peg-out button and preserve empty and busy guards', async () => {
  // Synthetic browser fixtures previously included seeds the real API omitted.
  // Exercise the HTTP serializer instead of recreating its response by hand.
  const details = JSON.parse(execFileSync('python3', ['-m', 'web.tests.case_detail_fixture'], {
    cwd: new URL('../../', import.meta.url), encoding: 'utf8', timeout: 15000,
  }));
  const seeded = details.seeded;
  const view = await harness(path => path === `/api/cases/${seeded.id}` ? seeded
    : path === `/api/cases/${details.empty.id}` ? details.empty
    : path.endsWith('/actions') ? {id: 'pegjob', status: 'running'} : undefined);
  await view.dispatch('open-case', {dataset: {id: seeded.id}});
  const html = view.pegoutsGraph(view.state.activeCase);
  assert.match(html, /3 selected seed UTXOs from 2 starting transactions/);
  assert.match(html, /data-action="pegouts-start"/);
  assert.doesNotMatch(html, /data-action="pegouts-start"[^>]* disabled/);
  assert.doesNotMatch(html, /id="pegouts-txid"/);
  pegoutEdit(view, 'min', '2'); pegoutEdit(view, 'max', '4');
  await view.dispatch('pegouts-start');
  assert.deepEqual(view.calls.at(-1), {path: `/api/cases/${seeded.id}/actions`,
    body: {action: 'pegouts', min_hops: 2, max_hops: 4}});

  assert.match(view.pegoutsGraph(view.state.activeCase), /data-action="pegouts-start"[^>]* disabled/);
  const busyCalls = view.calls.length;
  await view.dispatch('pegouts-start');
  assert.equal(view.calls.length, busyCalls, 'a running job blocks duplicate submission');
  view.state.job = null;
  assert.doesNotMatch(view.pegoutsGraph(view.state.activeCase), /data-action="pegouts-start"[^>]* disabled/);

  await view.dispatch('open-case', {dataset: {id: details.empty.id}});
  assert.match(view.pegoutsGraph(view.state.activeCase), /data-action="pegouts-start"[^>]* disabled/);
  const emptyCalls = view.calls.length;
  await assert.rejects(view.dispatch('pegouts-start'), /no selected seed UTXOs/);
  assert.equal(view.calls.length, emptyCalls, 'an empty investigation never starts a default search');
});

test('peg-out search defaults to all saved selected seeds before the first full run', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'pegjob', status: 'running'} : undefined);
  view.state.activeCase = pegoutCase([], {seeds: [`${txid}:1`, `${txid}:4`, `${'b'.repeat(64)}:0`]});
  const html = view.pegoutsGraph(view.state.activeCase);
  assert.match(html, /data-action="pegouts-start"[^>]*>/);
  assert.match(html, /3 selected seed UTXOs from 2 starting transactions/);
  assert.match(html, /Each starting transaction is hop 0/);
  assert.match(html, /Unselected sibling outputs at the start are excluded/);
  assert.doesNotMatch(html, /id="pegouts-txid"/);
  pegoutEdit(view, 'min', '2'); pegoutEdit(view, 'max', '4');
  await view.dispatch('pegouts-start');
  assert.deepEqual(view.calls.at(-1), {path: '/api/cases/case1/actions', body: {action: 'pegouts', min_hops: 2, max_hops: 4}});
  assert.equal(view.state.job.live, true);
  assert.equal(view.state.job.action, 'pegouts');
});

test('peg-out custom origin is opt-in, starts empty, and uses the entered transaction', async () => {
  const view = await harness();
  view.state.activeCase = pegoutCase([], {seeds: [`${'b'.repeat(64)}:3`]});
  pegoutEdit(view, 'custom', '', true);
  const html = view.pegoutsGraph(view.state.activeCase);
  assert.match(html, /id="pegouts-txid"[^>]*value=""/);
  assert.match(html, /custom search considers all outputs/);
  pegoutEdit(view, 'txid', txid.toUpperCase()); pegoutEdit(view, 'min', '2'); pegoutEdit(view, 'max', '4');
  await view.dispatch('pegouts-start');
  assert.deepEqual(view.calls.at(-1), {path: '/api/cases/case1/actions', body: {action: 'pegouts', txid, min_hops: 2, max_hops: 4}});
  assert.equal(view.state.job.live, true);
  assert.equal(view.state.job.action, 'pegouts');
});

test('turning custom origin off restores seed scope and never sends its stale transaction', async () => {
  const view = await harness(); view.state.activeCase = pegoutCase([], {seeds: [`${txid}:2`]});
  pegoutEdit(view, 'custom', '', true); pegoutEdit(view, 'txid', 'b'.repeat(64));
  pegoutEdit(view, 'custom', '', false);
  assert.doesNotMatch(view.pegoutsGraph(view.state.activeCase), /id="pegouts-txid"/);
  await view.dispatch('pegouts-start');
  assert.deepEqual(view.calls.at(-1).body, {action: 'pegouts', min_hops: 0, max_hops: 10});
});

test('missing seeds prevent the default search and offer an explicit custom origin', async () => {
  const view = await harness(); view.state.activeCase = pegoutCase();
  const html = view.pegoutsGraph(view.state.activeCase);
  assert.match(html, /no selected seed UTXOs/);
  assert.match(html, /Use a different starting transaction/);
  assert.match(html, /data-action="pegouts-start"[^>]* disabled/);
  await assert.rejects(view.dispatch('pegouts-start'), /no selected seed UTXOs/);
  assert.equal(view.calls.length, 1);
  pegoutEdit(view, 'custom', '', true); pegoutEdit(view, 'txid', txid);
  assert.doesNotMatch(view.pegoutsGraph(view.state.activeCase), /data-action="pegouts-start"[^>]* disabled/);
  await view.dispatch('pegouts-start');
  assert.equal(view.calls.at(-1).body.txid, txid);
});

test('peg-out invalid IDs and inverted or noninteger ranges never start jobs', async () => {
  for (const [id, low, high] of [['bad', '0', '4'], [txid, '5', '4'], [txid, '-1', '4'],
    [txid, '0', '2.5'], [txid, '', '4'], [txid, '0', '2147483648']]) {
    const view = await harness(); view.state.activeCase = pegoutCase();
    pegoutEdit(view, 'custom', '', true);
    pegoutEdit(view, 'txid', id); pegoutEdit(view, 'min', low); pegoutEdit(view, 'max', high);
    await assert.rejects(view.dispatch('pegouts-start'), /transaction ID|hop limits/);
    assert.equal(view.calls.length, 1);
  }
});

test('peg-out hop zero is inclusive and fixture searches stay offline', async () => {
  const view = await harness(); view.state.activeCase = pegoutCase([], {fixture: true, seeds: [`${txid}:2`]});
  pegoutEdit(view, 'min', '0'); pegoutEdit(view, 'max', '0');
  await view.dispatch('pegouts-start');
  assert.equal(view.state.job.live, false);
  assert.deepEqual(view.calls.at(-1).body, {action: 'pegouts', min_hops: 0, max_hops: 0});
});

test('saved seed scope uses its captured selections alongside legacy transaction searches', async () => {
  const view = await harness();
  const saved = pegoutSearch({txid: undefined, seeds: [`${txid}:1`, `${txid}:4`, `${'b'.repeat(64)}:0`]});
  const legacy = pegoutSearch({id: '2'.repeat(16), txid: 'c'.repeat(64)});
  view.state.activeCase = pegoutCase([saved, legacy], {seeds: [`${'d'.repeat(64)}:2`]});
  const html = view.pegoutsGraph(view.state.activeCase);
  assert.match(html, /Investigation seeds: 1 selected seed UTXO from 1 starting transaction/);
  assert.match(html, /Saved scope: 3 selected seed UTXOs from 2 starting transactions/);
  assert.match(html, /All outputs of c/);
  assert.doesNotMatch(html, /undefined/);
  await view.dispatch('pegouts-resume');
  assert.deepEqual(view.calls.at(-1).body, {action: 'pegouts', resume: saved.id});
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

test('named-group layout is opt-in, saved in Plot Layouts, and retained by trace-only forms', async () => {
  const detail = {id: 'centeredcase', name: 'Treasury layout', run_defaults: {...defaults, center_name: 'Treasury Group'}, runs: []};
  const view = await harness((path, body) => {
    if (path === '/api/cases/centeredcase/plot-settings') {
      Object.assign(detail.run_defaults, body.settings);
      return detail;
    }
    if (path === '/api/cases/centeredcase') return detail;
  });
  assert.doesNotMatch(view.newCase(), /name="center_name"/);
  await view.dispatch('open-case', {dataset: {id: detail.id}});
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /name="center_name" maxlength="120" value="Treasury Group"/);
  assert.match(view.workspace(), /Layout only: tracing and all connections stay the same/);
  await view.submitPlotSettings({center_name: '  Treasury  '});
  assert.equal(view.calls.findLast(call => call.path === '/api/cases/centeredcase/plot-settings').body.settings.center_name, 'Treasury');
  const previous = {...defaults, hub_addresses: [], center_name: 'Treasury Group'};
  assert.equal(view.readSettings({values: defaults}, previous).center_name, 'Treasury Group');
  assert.equal(view.readSettings({values: {...defaults, center_name: ''}}, previous).center_name, '');
  assert.equal(view.readSettings({values: defaults}).center_name, '');
  await view.submitPlotSettings({center_name: ''});
  assert.equal(view.calls.findLast(call => call.path === '/api/cases/centeredcase/plot-settings').body.settings.center_name, '');
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
  view.state.page = 'case'; view.state.caseView = 'plots';
  const list = {innerHTML: ''};
  view.elements.set('#plot-layout-form input[name="center_name"]', {value: ' Treasury '});
  view.elements.set('#center-name-options', list);
  await view.suggestCenterNames();
  assert.deepEqual(view.calls.findLast(call => call.path.endsWith('/name-colors')).body, {query: 'Treasury', offset: 0, limit: 100});
  assert.match(list.innerHTML, /value="Treasury Group"/);
  assert.doesNotMatch(list.innerHTML, /Inactive/);
  assert.match(list.innerHTML, /Group &quot;&lt;test&gt;/);
  assert.doesNotMatch(list.innerHTML, /<test>/);
});

test('investigation tool views isolate panels and preserve the selected snapshot', async () => {
  const view = await harness();
  view.state.activeCase = {id: 'case', name: 'Case', run_defaults: defaults, latest_run: 'latest-id', runs: [{id: 'old-id'}, {id: 'latest-id'}]};
  view.state.selectedRun = 'old-id';
  assert.match(view.workspace(), /id="collection-panel"/);
  assert.doesNotMatch(view.workspace(), /Collection settings|Choose a plotting goal/);
  assert.doesNotMatch(view.workspace(), /id="plot-layouts-panel"|data-action="workflow-board-sync"/);
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /id="plot-layouts-panel"/);
  assert.match(view.workspace(), /Plot Layouts/);
  assert.doesNotMatch(view.workspace(), /Plot views/);
  assert.doesNotMatch(view.workspace(), /id="collection-panel"|data-action="workflow-board-sync"/);
  await view.dispatch('view-history');
  assert.match(view.workspace(), /CSV downloads/);
  assert.match(view.workspace(), /Run history/);
  assert.doesNotMatch(view.workspace(), /id="connection-hops"/);
  assert.equal(view.state.selectedRun, 'old-id');
  assert.equal(view.calls.length, 1, 'navigation performs no fetches or writes');
});

test('compact trace form submits only its hop allowance and retains all saved preferences', async () => {
  const settings = {...defaults, hops: 2, include_fees: true, group_context_inputs: true, color_attribution_arrows: true, center_name: 'Treasury', connector_style: 'curved'};
  const view = await harness(path => path.endsWith('/actions') ? {id: 'job1', status: 'running'} : undefined);
  view.state.activeCase = {id: 'case', name: 'Case', run_defaults: settings, runs: []};
  view.openActionDialog('trace');
  assert.match(view.dialog.innerHTML, /name="hops"/);
  assert.doesNotMatch(view.dialog.innerHTML, /name="(?:include_fees|layout_attempts|max_transactions|max_new_items|connector_style)"/);
  await view.submitDialog({hops: '5'});
  assert.deepEqual(view.calls.at(-1).body, {action: 'trace', run_id: 'latest', hops: 5});
  assert.equal(view.state.activeCase.run_defaults.hops, 2);
  assert.equal(view.state.activeCase.run_defaults.group_context_inputs, true);
});

test('partial settings forms preserve absent values and unchecked rendered controls clear flags', async () => {
  const view = await harness();
  const prior = {...defaults, hub_addresses: ['Ga'.repeat(17)], include_fees: true, group_context_inputs: true,
    center_name: 'Treasury', connector_style: 'curved', color_attribution_arrows: true};
  const partial = view.readSettings({values: {hops: '4'}}, prior);
  assert.equal(partial.hops, 4);
  for (const key of Object.keys(prior).filter(key => key !== 'hops')) assert.deepEqual(JSON.parse(JSON.stringify(partial[key])), prior[key]);
  const cleared = view.readSettings({values: {include_fees_present: '1', group_context_inputs_present: '1'}}, prior);
  assert.equal(cleared.include_fees, false); assert.equal(cleared.group_context_inputs, false);
  assert.equal(cleared.color_attribution_arrows, true);
});

const workflowPlot = (goal, id, extra = {}) => ({preview_id: id, goal, run_id: 'saved1', min_hops: 0,
  max_hops: goal === 'full' ? null : 10, created_at: '2026-09-24T00:00:00Z', status: 'plotted',
  node_count: 3, edge_count: 2, transaction_count: 1, reviewable: true,
  notice: 'Saved-data-only plot. No additional transactions were fetched.',
  artifact: {preview_id: id, downloads: [], preview_url: `/files/case1/previews/${id}/graph.html`}, ...extra});
const workflowBoard = (goal, id, extra = {}) => ({id, name: `${goal} board`, goal, board_id: `miro-${id}`,
  board_url: `https://miro.com/app/board/miro-${id}/`, status: 'linked', legacy_snapshot: false, can_sync: true, ...extra});
const workflowCase = (extra = {}) => ({id: 'case1', name: 'Shared evidence', seeds: [`${txid}:0`], seed_count: 1,
  run_defaults: defaults, runs: [{id: 'saved1', status: 'bounded_complete', max_hops: 10}], latest_run: 'saved1',
  plots: [], boards: [], ...extra});
const workflowEdit = (view, id, value) => view.workflowInput({id: 'workflow-' + id, value});
const layoutKeys = ['layout_attempts', 'connector_style', 'include_fees', 'color_attribution_arrows',
  'group_context_inputs', 'center_name', 'hub_addresses'];

test('Plot Layouts owns every layout control and explains persistence without starting collection', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({runs: [], latest_run: undefined});
  await view.dispatch('view-plots');
  const html = view.workspace();
  assert.match(html, /id="plot-layout-form"/);
  for (const key of layoutKeys) assert.equal((html.match(new RegExp(`name="${key}"`, 'g')) || []).length, 1, key);
  for (const label of ['Separate branch hubs', 'Center named group', 'Group isolated context inputs',
    'Color arrows by attribution', 'Connector appearance', 'Save layout settings']) assert.ok(html.includes(label), label);
  assert.match(html, /saved (?:locally )?(?:with|for|in|per) (?:this |each |the )?investigation/i);
  assert.doesNotMatch(html, /Graph settings/);
  assert.match(html, /data-action="plot-settings-save"/);
  assert.equal(view.calls.length, 1);
});

test('generating a plot saves changed layout settings before queuing the selected saved-data goal', async () => {
  const detail = workflowCase();
  const first = 'G' + 'a'.repeat(33), second = 'H' + 'b'.repeat(33);
  const view = await harness((path, body) => {
    if (path === '/api/cases/case1/plot-settings') {
      detail.run_defaults = {...detail.run_defaults, ...body.settings};
      return detail;
    }
    if (path === '/api/cases/case1/actions') return {id: 'plotjob', status: 'running'};
  });
  view.state.activeCase = detail;
  await view.dispatch('view-plots');
  view.plotForm({layout_attempts: '40', connector_style: 'curved', include_fees_present: '1', include_fees: 'on',
    group_context_inputs_present: '1', group_context_inputs: 'on', color_attribution_arrows_present: '1',
    color_attribution_arrows: 'on', center_name: '  Treasury  ', hub_addresses: `${second}\n${first}\n${second}`});
  await view.dispatch('workflow-plot');
  const writes = view.calls.filter(call => call.body);
  assert.deepEqual(writes.map(call => call.path), ['/api/cases/case1/plot-settings', '/api/cases/case1/actions']);
  assert.deepEqual(writes[0].body, {settings: {layout_attempts: 40, connector_style: 'curved', include_fees: true,
    group_context_inputs: true, color_attribution_arrows: true, center_name: 'Treasury', hub_addresses: [first, second]}});
  assert.deepEqual(writes[1].body, {action: 'plot', goal: 'full', run_id: 'saved1', min_hops: 0, max_hops: 0});
  assert.equal(view.state.job.live, false);
});

test('unchanged layout settings skip the save request when generating a plot', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'plotjob', status: 'running'} : undefined);
  view.state.activeCase = workflowCase();
  await view.dispatch('view-plots');
  view.plotForm({layout_attempts: '25', connector_style: 'straight', include_fees_present: '1',
    group_context_inputs_present: '1', color_attribution_arrows_present: '1', center_name: '', hub_addresses: ''});
  await view.dispatch('workflow-plot');
  assert.equal(view.calls.filter(call => call.path.endsWith('/plot-settings')).length, 0);
  assert.equal(view.calls.filter(call => call.path.endsWith('/actions')).length, 1);
});

test('saving a filtered layout preserves full-trace options and reloads saved preferences in a new session', async () => {
  const hubs = ['G' + 'a'.repeat(33)];
  const detail = workflowCase({runs: [], latest_run: undefined, run_defaults: {...defaults,
    include_fees: true, group_context_inputs: true, hub_addresses: hubs}});
  const respond = (path, body) => {
    if (path === '/api/cases/case1/plot-settings') {
      detail.run_defaults = {...detail.run_defaults, ...body.settings};
      return detail;
    }
    if (path === '/api/cases/case1') return detail;
  };
  const view = await harness(respond);
  view.state.activeCase = detail;
  await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  const disabledFields = view.workspace().match(/<fieldset[^>]*\bdisabled[^>]*>[\s\S]*?<\/fieldset>/)?.[0];
  assert.ok(disabledFields);
  for (const key of ['include_fees', 'group_context_inputs', 'hub_addresses']) assert.ok(disabledFields.includes(`name="${key}"`));
  view.plotForm({layout_attempts: '61', connector_style: 'curved', center_name: 'Saved Treasury',
    color_attribution_arrows_present: '1', color_attribution_arrows: 'on'});
  await view.dispatch('plot-settings-save');
  const saved = view.calls.find(call => call.path.endsWith('/plot-settings')).body.settings;
  assert.deepEqual(Object.keys(saved).sort(), [...layoutKeys].sort());
  assert.equal(saved.include_fees, true);
  assert.equal(saved.group_context_inputs, true);
  assert.deepEqual(saved.hub_addresses, hubs);
  assert.equal(view.calls.some(call => call.path.endsWith('/actions')), false);
  const restarted = await harness(respond);
  await restarted.dispatch('open-case', {dataset: {id: 'case1'}});
  await restarted.dispatch('view-plots');
  assert.match(restarted.workspace(), /name="layout_attempts"[^>]*value="61"/);
  assert.match(restarted.workspace(), /value="Saved Treasury"/);
  assert.equal(restarted.state.settings.layout_attempts, 25, 'per-investigation saves do not overwrite workspace preferences');
});

test('a failed layout save retains edits and prevents generation until the save succeeds', async () => {
  const view = await harness(path => path.endsWith('/plot-settings') ? {error: 'Cannot save layout settings.'} : undefined);
  view.state.activeCase = workflowCase();
  await view.dispatch('view-plots');
  view.plotForm({layout_attempts: '45', center_name: 'Retain this group'});
  await assert.rejects(view.dispatch('workflow-plot'), /Cannot save layout settings/);
  assert.equal(view.calls.some(call => call.path.endsWith('/actions')), false);
  assert.equal(view.state.activeCase.run_defaults.layout_attempts, 25);
  assert.match(view.workspace(), /name="layout_attempts"[^>]*value="45"/);
  assert.match(view.workspace(), /value="Retain this group"/);
  assert.equal(view.isBusy(), false);
});

test('invalid layout controls prevent both saving and queuing a plot', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase();
  await view.dispatch('view-plots');
  view.plotForm({layout_attempts: '0'}, false);
  await view.dispatch('workflow-plot');
  await view.submitPlotSettings({layout_attempts: '0'}, false);
  assert.equal(view.calls.length, 1);
});

test('layout drafts survive tab navigation and remain isolated between investigations', async () => {
  const cases = {first: workflowCase({id: 'first', run_defaults: {...defaults, center_name: 'First saved'}}),
    second: workflowCase({id: 'second', run_defaults: {...defaults, center_name: 'Second saved'}})};
  const view = await harness(path => Object.values(cases).find(detail => path === `/api/cases/${detail.id}`));
  await view.dispatch('open-case', {dataset: {id: 'first'}});
  await view.dispatch('view-plots');
  view.editPlotSettings({center_name: 'First unsaved', layout_attempts: '33'});
  view.elements.delete('#plot-layout-form');
  await view.dispatch('view-collect');
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /value="First unsaved"/);
  assert.match(view.workspace(), /name="layout_attempts"[^>]*value="33"/);
  await view.dispatch('open-case', {dataset: {id: 'second'}});
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /value="Second saved"/);
  assert.doesNotMatch(view.workspace(), /First unsaved/);
  view.editPlotSettings({center_name: 'Second unsaved'});
  view.elements.delete('#plot-layout-form');
  await view.dispatch('open-case', {dataset: {id: 'first'}});
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /value="First unsaved"/);
  assert.doesNotMatch(view.workspace(), /Second unsaved/);
  assert.equal(view.calls.some(call => call.body), false, 'draft navigation does not silently persist settings');
});

test('all plotting goals are visible and plot only the selected saved run without fetching', async () => {
  for (const goal of ['full', 'connections', 'pegouts']) {
    const view = await harness(path => path.endsWith('/actions') ? {id: 'plotjob', status: 'running'} : undefined);
    view.state.activeCase = workflowCase({runs: [{id: 'older'}, {id: 'saved1'}]});
    view.state.selectedRun = 'older';
    await view.dispatch('view-plots');
    const html = view.workspace();
    for (const name of ['Full trace', 'Starter connections', 'Paths to peg-outs']) assert.match(html, new RegExp(name));
    assert.doesNotMatch(html, /data-action="(?:trace-dialog|pegouts-start|miro-sync-dialog|workflow-board-sync)"/);
    await view.dispatch('plot-goal', {dataset: {goal}});
    workflowEdit(view, 'min-hops', '2'); workflowEdit(view, 'max-hops', '10');
    await view.dispatch('workflow-plot');
    assert.deepEqual(view.calls.at(-1), {path: '/api/cases/case1/actions', body: {
      action: 'plot', goal, run_id: 'older', min_hops: goal === 'pegouts' ? 2 : 0, max_hops: goal === 'full' ? 0 : 10}});
    assert.equal(view.state.job.live, false);
    assert.equal(view.calls.filter(call => call.path.endsWith('/actions')).length, 1);
  }
});

test('plot views explain missing collection, retain all goals, and reject invalid bounds locally', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({runs: [], latest_run: undefined});
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /Collect transaction data before generating a plot/);
  assert.match(view.workspace(), /data-action="workflow-plot"[^>]*disabled/);
  assert.equal((view.workspace().match(/data-action="plot-goal"/g) || []).length, 3);
  await assert.rejects(view.dispatch('workflow-plot'), /Collect transaction data first/);
  view.state.activeCase = workflowCase();
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  workflowEdit(view, 'min-hops', '11'); workflowEdit(view, 'max-hops', '10');
  await assert.rejects(view.dispatch('workflow-plot'), /hop limits/);
  assert.equal(view.calls.length, 1);
});

test('peg-out endpoints default off and optional booleans are submitted only for this goal', async () => {
  for (const goal of ['full', 'connections', 'pegouts']) {
    for (const [includeUnspent, includeUnspendable] of [[false, false], [true, false], [false, true], [true, true]]) {
      const view = await harness(path => path.endsWith('/actions') ? {id: 'plotjob', status: 'running'} : undefined);
      view.state.activeCase = workflowCase();
      await view.dispatch('view-plots');
      assert.doesNotMatch(view.workspace(), /id="workflow-include-(?:unspent|unspendable)"/);
      await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
      for (const id of ['unspent', 'unspendable']) {
        const input = view.workspace().match(new RegExp(`<input id="workflow-include-${id}"[^>]*>`))?.[0];
        assert.ok(input);
        assert.doesNotMatch(input, /checked/);
      }
      assert.match(view.workspace(), /Peg-out requests are always included/);
      assert.match(view.workspace(), /observed unspent in the selected collection run/);
      assert.match(view.workspace(), /Unchecked outputs and outputs stopped only by a hop limit are not counted as unspent/);
      view.workflowInput({id: 'workflow-include-unspent', checked: includeUnspent});
      view.workflowInput({id: 'workflow-include-unspendable', checked: includeUnspendable});
      await view.dispatch('plot-goal', {dataset: {goal}});
      if (goal !== 'pegouts') assert.doesNotMatch(view.workspace(), /id="workflow-include-(?:unspent|unspendable)"/);
      await view.dispatch('workflow-plot');
      assert.deepEqual(view.calls.at(-1).body, {action: 'plot', goal, run_id: 'saved1',
        min_hops: 0, max_hops: goal === 'full' ? 0 : 10,
        ...(goal === 'pegouts' && includeUnspent ? {include_unspent: true} : {}),
        ...(goal === 'pegouts' && includeUnspendable ? {include_unspendable: true} : {})});
      assert.equal(view.state.job.live, false);
      assert.equal(view.calls.filter(call => call.body).length, 1, 'endpoint choices do not update settings or collect data');
    }
  }
});

test('endpoint and context drafts survive tab and goal changes without leaking into another investigation', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase();
  await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  view.workflowInput({id: 'workflow-include-unspent', checked: true});
  view.workflowInput({id: 'workflow-include-unspendable', checked: true});
  view.workflowInput({id: 'workflow-include-context', checked: true});
  await view.dispatch('view-boards');
  await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'connections'}});
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  for (const id of ['unspent', 'unspendable', 'context']) assert.match(view.workspace(), new RegExp(`id="workflow-include-${id}"[^>]*checked`));
  view.workflowInput({id: 'workflow-include-unspent', checked: false});
  view.workflowInput({id: 'workflow-include-context', checked: false});
  await view.dispatch('view-plots');
  assert.doesNotMatch(view.workspace(), /id="workflow-include-unspent"[^>]*checked/);
  assert.doesNotMatch(view.workspace(), /id="workflow-include-context"[^>]*checked/);
  view.workflowInput({id: 'workflow-include-context', checked: true});
  view.state.activeCase = workflowCase({id: 'other'});
  await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  for (const id of ['unspent', 'unspendable', 'context']) assert.doesNotMatch(view.workspace(), new RegExp(`id="workflow-include-${id}"[^>]*checked`));
  assert.equal(view.calls.length, 1);
});

test('context addresses default off, describe display only, and submit only for peg-out layouts', async () => {
  for (const goal of ['full', 'connections', 'pegouts']) {
    for (const includeContext of [false, true]) {
      const view = await harness(path => path.endsWith('/actions') ? {id: 'plotjob', status: 'running'} : undefined);
      view.state.activeCase = workflowCase();
      await view.dispatch('view-plots');
      assert.doesNotMatch(view.workspace(), /id="workflow-include-context"/);
      await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
      const html = view.workspace();
      assert.match(html, /<legend>Context display<\/legend>/);
      assert.match(html, /Include context addresses/);
      assert.match(html, /Show other input addresses and sibling output addresses of displayed transactions/);
      assert.match(html, /Context adds no tracing or fetching and is excluded from endpoint match counts/);
      assert.doesNotMatch(html, /id="workflow-include-context"[^>]*checked/);
      view.workflowInput({id: 'workflow-include-context', checked: includeContext});
      view.workflowInput({id: 'workflow-include-unspent', checked: true});
      await view.dispatch('plot-goal', {dataset: {goal}});
      if (goal !== 'pegouts') assert.doesNotMatch(view.workspace(), /id="workflow-include-context"/);
      await view.dispatch('workflow-plot');
      assert.deepEqual(view.calls.at(-1).body, {action: 'plot', goal, run_id: 'saved1',
        min_hops: 0, max_hops: goal === 'full' ? 0 : 10,
        ...(goal === 'pegouts' ? {include_unspent: true} : {}),
        ...(goal === 'pegouts' && includeContext ? {include_context: true} : {})});
      assert.equal(view.state.job.live, false);
      assert.equal(view.calls.filter(call => call.body).length, 1, 'context display neither updates settings nor collects data');
      assert.equal(view.workflowInput({id: 'workflow-include-context', checked: !includeContext}), false);
      assert.equal(view.currentWorkflow(view.state.activeCase).includeContext, includeContext, 'busy jobs preserve the draft');
    }
  }
});

test('saved context scope and separate connection counts appear across plots, downloads, and boards', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'syncjob', status: 'running'} : undefined);
  const contextual = workflowPlot('pegouts', 'contextual', {query: {include_context: true},
    match_count: 2, endpoint_count: 2, context_edge_count: 5});
  view.state.activeCase = workflowCase({plots: [contextual, workflowPlot('pegouts', 'paths', {match_count: 2})],
    boards: [workflowBoard('pegouts', 'context-board', {preview_id: 'contextual'})]});
  for (const page of ['plots', 'history', 'boards']) {
    await view.dispatch('view-' + page);
    const html = view.workspace();
    assert.match(html, /Saved layout: Context addresses included\. 5 context connections, excluded from endpoint counts\./);
    assert.match(html, /Results: 2 peg-out requests\./);
    const picker = html.match(/<select id="workflow-(?:plot-picker|board-plot)"[^>]*>[\s\S]*?<\/select>/)?.[0];
    assert.ok(picker);
    assert.match(picker, /value="contextual"[^>]*>[^<]*Peg-outs · 2 endpoints · Context addresses included/);
    assert.match(picker, /value="paths"[^>]*>[^<]*Peg-outs · 2 endpoints · Paths only/);
  }
  assert.match(view.workspace(), /<button class="board-choice[^>]*>[\s\S]*?Context addresses included/);
  await view.dispatch('workflow-board-sync');
  assert.deepEqual(view.calls.at(-1).body, {action: 'board-sync', record_id: 'context-board', preview_id: 'contextual', reorganize: false});
});

test('context toggles do not change the scope of a selected saved layout', async () => {
  for (const savedContext of [false, true]) {
    const view = await harness();
    view.state.activeCase = workflowCase({plots: [workflowPlot('pegouts', 'saved', {
      ...(savedContext ? {query: {include_context: true}} : {}), match_count: 2})]});
    await view.dispatch('view-plots');
    await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
    view.workflowInput({id: 'workflow-include-context', checked: !savedContext});
    for (const page of ['plots', 'history']) {
      await view.dispatch('view-' + page);
      const html = view.workspace();
      assert.match(html, new RegExp(`Saved layout: ${savedContext ? 'Context addresses included' : 'Paths only'}\\.`));
      assert.doesNotMatch(html, new RegExp(`Saved layout: ${savedContext ? 'Paths only' : 'Context addresses included'}\\.`));
      assert.doesNotMatch(html, /undefined context connections|NaN context connections/);
    }
    assert.equal(view.calls.length, 1, 'changing context display neither regenerates nor updates saved plots');
  }
});

test('saved endpoint scope and counts remain visible in plot, download, and board selection', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'syncjob', status: 'running'} : undefined);
  const terminal = workflowPlot('pegouts', 'terminal', {query: {include_unspent: true, include_unspendable: true},
    match_count: 0, endpoint_count: 3, endpoint_counts: {pegout: 0, unspent: 2, unspendable: 1}});
  view.state.activeCase = workflowCase({plots: [terminal, workflowPlot('pegouts', 'earlier', {match_count: 2})],
    boards: [workflowBoard('pegouts', 'terminal-board', {preview_id: 'terminal'})]});
  for (const page of ['plots', 'history', 'boards']) {
    await view.dispatch('view-' + page);
    const html = view.workspace();
    assert.match(html, /Peg-outs \+ unspent UTXOs \+ unspendable outputs · 3 endpoints/);
    assert.match(html, /0 peg-out requests · 2 unspent UTXOs · 1 unspendable outputs/);
    const picker = html.match(/<select id="workflow-(?:plot-picker|board-plot)"[^>]*>[\s\S]*?<\/select>/)?.[0];
    assert.ok(picker);
    assert.match(picker, /value="terminal"[^>]*>[^<]*Peg-outs \+ unspent UTXOs \+ unspendable outputs · 3 endpoints/);
    assert.match(picker, /value="earlier"[^>]*>[^<]*Peg-outs · 2 endpoints/);
  }
  assert.doesNotMatch(view.workspace().match(/<button[^>]*data-action="workflow-board-sync"[^>]*>/)[0], /disabled/);
  await view.dispatch('workflow-board-sync');
  assert.deepEqual(view.calls.at(-1).body, {action: 'board-sync', record_id: 'terminal-board', preview_id: 'terminal', reorganize: false});
});

test('historical peg-out layouts keep their peg-out-only summary regardless of current choices', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({plots: [workflowPlot('pegouts', 'old', {match_count: 2})]});
  await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  view.workflowInput({id: 'workflow-include-unspent', checked: true});
  view.workflowInput({id: 'workflow-include-unspendable', checked: true});
  await view.dispatch('view-history');
  assert.match(view.workspace(), /Endpoint types: Peg-outs\. Results: 2 peg-out requests\./);
  assert.doesNotMatch(view.workspace(), /Endpoint types: Peg-outs \+|0 unspent UTXOs|0 unspendable outputs/);
});

test('central board manager creates or links boards before collection for each goal', async () => {
  for (const goal of ['full', 'connections', 'pegouts']) {
    for (const method of ['create', 'link']) {
      const view = await harness(path => path.endsWith('/actions') ? {id: 'boardjob', status: 'running'} : undefined);
      view.state.activeCase = workflowCase({runs: [], latest_run: undefined});
      await view.dispatch('view-boards');
      assert.match(view.workspace(), /No boards yet/);
      workflowEdit(view, 'board-goal', goal); workflowEdit(view, 'board-name', 'My board');
      workflowEdit(view, 'board-url', 'https://miro.com/app/board/target=/');
      await view.dispatch('workflow-board-' + method);
      assert.deepEqual(view.calls.at(-1).body, {action: 'board-' + method, goal, name: 'My board',
        ...(method === 'link' ? {board: 'https://miro.com/app/board/target=/'} : {})});
      assert.equal(view.state.job.live, method === 'create');
    }
  }
});

test('shared sync buttons pin the selected board and a matching plot without changing full-trace board', async () => {
  for (const reorganize of [false, true]) {
    const view = await harness(path => path.endsWith('/actions') ? {id: 'syncjob', status: 'running'} : undefined);
    view.state.activeCase = workflowCase({miro_board: 'original-full',
      plots: [workflowPlot('full', 'fullplot'), workflowPlot('pegouts', 'pegplot')],
      boards: [workflowBoard('full', 'fullboard'), workflowBoard('pegouts', 'pegboard')]});
    await view.dispatch('view-boards');
    await view.dispatch('workflow-board-select', {dataset: {record: 'pegboard'}});
    const html = view.workspace();
    assert.match(html, /miro-pegboard/);
    assert.match(html, /value="pegplot"/);
    assert.doesNotMatch(html, /value="fullplot"|data-action="miro-frames-dialog"|data-action="miro-sync-dialog"/);
    await view.dispatch(reorganize ? 'workflow-board-organize' : 'workflow-board-sync');
    assert.deepEqual(view.calls.at(-1).body, {action: 'board-sync', record_id: 'pegboard', preview_id: 'pegplot', reorganize});
    assert.equal(view.state.activeCase.miro_board, 'original-full');
  }
});

test('archived boards remain visible, stale plots cannot sync, and busy jobs block duplicate writes', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'job', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({plots: [workflowPlot('pegouts', 'stale', {reviewable: false, reason: 'Settings changed'})],
    boards: [workflowBoard('pegouts', 'managed'), workflowBoard('pegouts', 'old', {legacy_snapshot: true, can_sync: false})]});
  await view.dispatch('view-boards');
  assert.match(view.workspace(), /Archived snapshot/);
  assert.match(view.workspace(), /data-action="workflow-board-sync"[^>]*disabled/);
  await assert.rejects(view.dispatch('workflow-board-sync'), /matching saved plot/);
  await view.dispatch('workflow-board-select', {dataset: {record: 'old'}});
  assert.doesNotMatch(view.workspace(), /data-action="workflow-board-sync"/);
  view.state.job = {id: 'busy', action: 'board-sync'};
  await view.dispatch('workflow-board-create');
  assert.equal(view.calls.length, 1);
});

test('interrupted board creation links the known Miro board to its original registry entry', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'linkjob', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({boards: [workflowBoard('pegouts', 'pending', {board_id: null, status: 'pending_creation', can_sync: false})]});
  await view.dispatch('view-boards');
  assert.match(view.workspace(), /Link created board to this entry/);
  workflowEdit(view, 'recovery-url', 'created-board');
  await view.dispatch('workflow-board-recover');
  assert.deepEqual(view.calls.at(-1).body, {action: 'board-link', record_id: 'pending', goal: 'pegouts', name: 'pegouts board', board: 'created-board'});
});

test('job completion selects the generated plot or initialized board from the refreshed API response', async () => {
  for (const action of ['plot', 'board-create', 'board-link']) {
    const plot = workflowPlot('pegouts', 'newplot'), board = workflowBoard('pegouts', 'newboard');
    const detail = workflowCase({plots: [plot], boards: [board]});
    const view = await harness(path => path === '/api/jobs/job' ? {status: 'succeeded', result: action === 'plot' ? plot : board}
      : path === '/api/cases/case1' ? detail : undefined);
    view.state.activeCase = workflowCase();
    view.state.job = {id: 'job', action, caseId: 'case1'};
    await view.pollJob();
    assert.equal(view.state.caseView, action === 'plot' ? 'plots' : 'boards');
    assert.equal(view.currentWorkflow(detail)[action === 'plot' ? 'plot' : 'board'], action === 'plot' ? 'newplot' : 'newboard');
  }
});

test('real populated case-detail contract feeds saved plot previews and common board sync controls', async () => {
  const details = JSON.parse(execFileSync('python3', ['-m', 'web.tests.case_detail_fixture'], {
    cwd: new URL('../../', import.meta.url), encoding: 'utf8', timeout: 15000,
  }));
  for (const detail of Object.values(details)) {
    assert.ok(Array.isArray(detail.plots)); assert.ok(Array.isArray(detail.boards));
  }
  const detail = details.workflow;
  assert.equal(detail.plots.length, 1); assert.equal(detail.boards.length, 1);
  const view = await harness(path => path === `/api/cases/${detail.id}` ? detail
    : path.endsWith('/actions') ? {id: 'syncjob', status: 'running'} : undefined);
  await view.dispatch('open-case', {dataset: {id: detail.id}});
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /<iframe/);
  assert.match(view.workspace(), /Paths to peg-outs/);
  assert.match(view.workspace(), /Collection hop limit: 10/);
  await view.dispatch('view-boards');
  assert.match(view.workspace(), /Peg-out case board/);
  assert.doesNotMatch(view.workspace().match(/<button[^>]*data-action="workflow-board-sync"[^>]*>/)[0], /disabled/);
  await view.dispatch('workflow-board-sync');
  assert.deepEqual(view.calls.at(-1).body, {action: 'board-sync', record_id: detail.boards[0].id,
    preview_id: detail.plots[0].preview_id, reorganize: false});
});

test('interrupted sync permits its saved plot retry and excludes newer plots until recovery completes', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'retry', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({plots: [workflowPlot('pegouts', 'newer'), workflowPlot('pegouts', 'saved')],
    boards: [workflowBoard('pegouts', 'interrupted', {pending_count: 1, preview_id: 'saved', status: 'interrupted'})]});
  await view.dispatch('view-boards');
  assert.match(view.workspace(), /value="saved"/);
  assert.doesNotMatch(view.workspace(), /value="newer"/);
  await view.dispatch('workflow-board-sync');
  assert.deepEqual(view.calls.at(-1).body, {action: 'board-sync', record_id: 'interrupted', preview_id: 'saved', reorganize: false});
});

test('empty plots cannot sync and unavailable registries show the API notice', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({plots: [workflowPlot('pegouts', 'empty', {empty: true, node_count: 0})],
    boards: [workflowBoard('pegouts', 'managed')], plots_notice: 'Plot access temporarily blocked.',
    boards_notice: 'Restore the saved board registry.'});
  await view.dispatch('view-boards');
  assert.match(view.workspace(), /Restore the saved board registry/);
  assert.match(view.workspace(), /data-action="workflow-board-sync"[^>]*disabled/);
  await assert.rejects(view.dispatch('workflow-board-sync'), /matching saved plot/);
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /Plot access temporarily blocked/);
  assert.match(view.workspace(), /No matching paths were found/);
});

test('settings direct board management to the central tab and preserve the original full-board binding', async () => {
  const detail = workflowCase({miro_board: 'original-full'});
  const view = await harness(path => path === '/api/cases/case1' ? detail : undefined);
  view.state.activeCase = detail; view.state.page = 'case-settings';
  assert.match(view.settingsPage(), /Manage investigation boards/);
  assert.doesNotMatch(view.settingsPage(), /name="board"/);
  await view.submitSettings({name: detail.name});
  assert.equal(view.calls.find(call => call.path.endsWith('/settings')).body.board, 'original-full');
});

test('one saved ELK plot feeds SVG export and Miro sync without generating another layout', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'syncjob', status: 'running'} : undefined);
  const selected = workflowPlot('pegouts', 'reviewed', {artifact: {preview_id: 'reviewed',
    preview_url: '/files/case1/previews/reviewed/graph.html',
    downloads: [{name: 'graph.svg', url: '/files/case1/previews/reviewed/graph.svg'}]}});
  view.state.activeCase = workflowCase({plots: [workflowPlot('pegouts', 'newer'), selected],
    boards: [workflowBoard('full', 'overview'), workflowBoard('pegouts', 'cashouts'), workflowBoard('pegouts', 'second-cashouts')]});
  await view.dispatch('view-plots');
  workflowEdit(view, 'plot-picker', 'reviewed');
  const html = view.workspace();
  assert.match(html, /Sync with Miro/);
  assert.match(html, /Download ELK SVG/);
  assert.match(html, /href="\/files\/case1\/previews\/reviewed\/graph.svg"/);
  await view.dispatch('plot-boards');
  assert.equal(view.state.caseView, 'boards');
  assert.equal(view.currentWorkflow(view.state.activeCase).boardPlot, 'reviewed');
  assert.doesNotMatch(view.workspace(), /Generate matching plot|data-action="board-plot-goal"/);
  await view.dispatch('workflow-board-select', {dataset: {record: 'second-cashouts'}});
  assert.equal(view.currentWorkflow(view.state.activeCase).boardPlot, 'reviewed');
  assert.equal(view.calls.length, 1, 'choosing a destination reuses the generated plot without requests');
  await view.dispatch('workflow-board-sync');
  assert.deepEqual(view.calls.at(-1).body, {action: 'board-sync', record_id: 'second-cashouts', preview_id: 'reviewed', reorganize: false});
});

test('plot handoff never silently substitutes a different goal or an interrupted older plot', async () => {
  for (const board of [workflowBoard('full', 'overview'),
    workflowBoard('pegouts', 'interrupted', {pending_count: 1, preview_id: 'older', status: 'interrupted'})]) {
    const view = await harness();
    view.state.activeCase = workflowCase({plots: [workflowPlot('pegouts', 'chosen'),
      workflowPlot('pegouts', 'older'), workflowPlot('full', 'fullplot')], boards: [board]});
    await view.dispatch('view-plots');
    workflowEdit(view, 'plot-picker', 'chosen');
    await view.dispatch('plot-boards');
    assert.equal(view.currentWorkflow(view.state.activeCase).boardGoal, 'pegouts');
    await assert.rejects(view.dispatch('workflow-board-sync'), /matching saved plot|selected plot|compatible|managed board/);
    assert.equal(view.calls.length, 1, 'a missing or interrupted destination cannot publish another plot');
  }
});

test('settings data tools preserve an unsaved investigation draft and remain outside its save form', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase();
  await view.dispatch('case-settings');
  view.elements.set('#settings-form', {values: {...defaults, name: 'Unsaved investigation name', hops: '7', center_name: 'Treasury'}});
  await view.dispatch('input-import-open');
  assert.equal(view.state.page, 'case-settings');
  await view.dispatch('change-outputs-open');
  assert.equal(view.state.page, 'case-settings');
  const html = view.settingsPage();
  assert.match(html, /value="Unsaved investigation name"/);
  assert.match(html, /name="hops"[^>]*value="7"/);
  assert.doesNotMatch(html, /name="center_name"/);
  const form = html.match(/<form id="settings-form"[\s\S]*?<\/form>/)?.[0];
  assert.ok(form);
  assert.doesNotMatch(form, /data-action="(?:input-import-open|change-outputs-open|addresses)"/);
  for (const action of ['input-import-open', 'change-outputs-open', 'addresses']) assert.ok(html.includes(`data-action="${action}"`));
  assert.equal(view.calls.length, 1, 'opening settings tools neither saves preferences nor starts collection');
});
