import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {readFileSync} from 'node:fs';
import {stripTypeScriptTypes} from 'node:module';
import {test} from 'node:test';
import vm from 'node:vm';
import * as frameRecovery from '../src/scripts/frame-recovery.ts';
import {collectionPerformancePanel} from '../src/scripts/collection-performance.ts';
import {createTaskNotifications} from '../src/scripts/task-notifications.ts';

const seedEditorSource = stripTypeScriptTypes(
  readFileSync(new URL('../src/scripts/seed-editor.ts', import.meta.url), 'utf8')
    .replace(/^export /gm, ''), {mode: 'transform'});
const seedEditorScript = new vm.Script('Object.assign(globalThis, (() => {' + seedEditorSource +
  '\nreturn {seedEditorPanel, seedEditorAction, seedEditorInput, seedEditorPending, forgetSeedEditor, sameSeeds};})());');

// Execute the real application handlers with a small DOM and offline HTTP stub.
// The attribution panels are unrelated to new-investigation and lookup behavior.
const source = stripTypeScriptTypes(
  readFileSync(new URL('../src/scripts/app.ts', import.meta.url), 'utf8')
    .replace(/^import .*\n/gm, '')
    .replace(/^export \{\};\n/m, '')
    .replace('void initialize().catch(', 'globalThis.startup = initialize().catch('),
  {mode: 'transform'},
);
const script = new vm.Script(source + '\n Object.defineProperty(state, "job", {get() {return runningJobs()[0] || null;}, set(job) {state.jobs.clear(); if (job && state.activeCase && state.page === "dashboard") state.page = "case"; if (job) state.jobs.set(job.id, {status: "running", generation: pageGeneration, caseId: state.activeCase?.id, ...job});}}); globalThis.appTest = {state, dispatch, pollJob: (id) => pollJob(id || runningJobs()[0]?.id), startJob, discoverJobs, runningJobs, jobBanner, jobProgress, cancelJob, openCase, newCase, dashboard, workspace, settingsPage, localGraph, elkGraph, compactGraph, currentCompaction, openActionDialog, readSettings, budgetFields, isBusy, pegoutsGraph, pegoutInput, currentPegoutSearch, suggestCenterNames, suggestHopReferenceNames, render, navigate, workflowInput, currentWorkflow, scopeAnalysisInput, currentScopeDraft, actionBusy, taskActivity, refreshCaseDetail, refreshSharedCollection, loadCaseSection, loadVisibleSections, verifySelectedPlot, refreshSession, deleteCaches: {investigationViews, workflowDrafts, pegoutDrafts, boardDrafts, plotLayoutDrafts, layoutFormDrafts, scopeDrafts, settingsDrafts, previewLibraries, sectionErrors, scopeDetails}};');
const txid = 'a'.repeat(64);
const defaults = {hops: 1, hop_reference_name: '', max_transactions: 20, max_outpoints: 100, max_requests: 30,
  max_seconds: 60, max_new_items: 750, layout_attempts: 25, connector_style: 'straight', layout_style: 'standard'};

async function harness(respond = () => undefined, {hash = '', storage = new Map(), importAction, importPending = () => false, systemNotification, changeOutputHandlers = {}, sharedAttributionHandlers = {}} = {}) {
  frameRecovery.resetFrameRecovery();
  const calls = [], methods = [], listeners = {}, windowListeners = {}, dialogListeners = {}, notifications = [], importActions = [];
  const editorResets = [], downloads = [], downloadBlobs = [], revokedDownloads = [];
  const toastElements = [], timers = new Map();
  let timerId = 0;
  let focusCount = 0;
  const elements = new Map();
  let currentForm = null;
  const app = {innerHTML: '', addEventListener(name, callback) {listeners[name] = callback;}};
  const dialog = {innerHTML: '', open: false, addEventListener(name, callback) {dialogListeners[name] = callback;},
    close() {if (this.open) {this.open = false; dialogListeners.close?.();}}, showModal() {this.open = true;}, querySelector() {return null;}};
  const context = vm.createContext({
    Error, Blob, URL: class extends URL {
      static createObjectURL(blob) {downloadBlobs.push(blob); return `blob:export-${downloadBlobs.length}`;}
      static revokeObjectURL(url) {revokedDownloads.push(url);}
    }, console, ...frameRecovery, collectionPerformancePanel, createTaskNotifications,
    sharedAttributionsPanel() {return "";}, sharedAttributionsAction() {return false;},
    sharedAttributionsInput() {return false;}, sharedAttributionsFile: async () => {},
    sharedAttributionsPending() {return false;}, selectSharedAttributions() {}, resetSharedAttributions() {},
    ...sharedAttributionHandlers,
    inputImportInput() {return false;}, changeOutputsInput() {return false;}, nameColorsInput() {return false;}, addressImportInput() {return false;},
    resetNameColors(caseId) {editorResets.push({editor: 'colors', caseId});},
    resetAddressImport(caseId) {editorResets.push({editor: 'addresses', caseId});},
    resetChangeOutputs(caseId) {editorResets.push({editor: 'change-outputs', caseId});}, resetInputImport() {},
    selectNameColors() {}, selectAddressImport() {}, selectChangeOutputs() {}, selectInputImport() {},
    inputImportPending: importPending, inputImportPanel() {return "";},
    inputImportAction(action, context) {
      if (importAction) return importAction(action, context);
      if (!action.startsWith('input-import-')) return false;
      if (!context.busy) importActions.push({action, caseId: context.caseId});
      return true;
    },
    changeOutputsPending() {return false;}, changeOutputsPanel() {return "";},
    changeOutputsAction() {return false;}, changeOutputsLookupComplete() {return false;},
    ...changeOutputHandlers,
    addressImportPanel() {return "";}, nameColorsPanel() {return "";}, nameColorsAction() {return false;}, addressImportAction() {return false;},
    document: {
      querySelector(selector) {
        if (selector === '#app') return app;
        if (selector === '#action-dialog') return dialog;
        if (selector === '#new-case-form') return currentForm;
        if (selector === '#notifications') return {append(node) {notifications.push(node.textContent); toastElements.push(node);}};
        if (selector === '#job-tasks') return {set outerHTML(value) {
          app.innerHTML = app.innerHTML.replace(/<div id="job-tasks">[\s\S]*?<\/header>/, value + '</header>');
          const progress = elements.get('#job-progress'); if (progress) progress.innerHTML = value;
          const message = elements.get('#job-message'); if (message) message.textContent = [...context.appTest.state.jobs.values()][0]?.progress?.message || '';
        }};
        return elements.get(selector) ?? null;
      },
      addEventListener() {},
      body: {append() {}},
      createElement(tag) {return {tag, removed: false, attributes: {}, setAttribute(name, value) {this.attributes[name] = value;}, remove() {this.removed = true;}, click() {if (tag === 'a') downloads.push({url: this.href, filename: this.download}); this.onclick?.();}};},
    },
    window: {addEventListener(name, callback) {windowListeners[name] = callback;}, scrollY: 0, scrollTo() {},
      Notification: systemNotification, focus() {focusCount++;},
      localStorage: {getItem(key) {return storage.get(key) ?? null;}, setItem(key, value) {storage.set(key, value);}},
      sessionStorage: {getItem(key) {return storage.get(key) ?? null;}, setItem(key, value) {storage.set(key, value);}, removeItem(key) {storage.delete(key);}}},
    history: {replaceState() {}},
    location: {hash, pathname: '/', reload() {}},
    setInterval() {}, setTimeout(fn, ms) {const id = ++timerId; timers.set(id, {fn, ms}); return id;}, clearTimeout(id) {timers.delete(id);},
    FormData: class {
      constructor(form) {this.values = form.values;}
      get(key) {return this.values[key] ?? null;}
      has(key) {return key in this.values;}
    },
    async fetch(path, options) {
      const body = options.body === undefined ? undefined : JSON.parse(options.body);
      calls.push({path, body});
      methods.push(options.method || 'GET');
      const response = await Promise.resolve(respond(path, body, calls.length))
        ?? (path === '/api/session' ? {csrf: 'test', settings: defaults, cases: []} : {});
      return {ok: !response.error, status: response.error ? 400 : 200, async json() {return response;}};
    },
  });
  seedEditorScript.runInContext(context);
  script.runInContext(context);
  await context.startup;
  return {
    ...context.appTest, app, dialog, calls, methods, notifications, importActions, elements, storage, windowListeners, editorResets, downloads, downloadBlobs, revokedDownloads,
    toastElements, timers,
    async setHash(value) {context.location.hash = value; windowListeners.hashchange?.(); await new Promise(setImmediate);},
    editSeeds(value, caseId = this.state.activeCase?.id) {
      listeners.input({target: {id: 'seed-editor-text', value, dataset: {caseId}}});
    },
    changeBlockchain(value) {
      currentForm = {id: 'new-case-form', values: {...defaults, name: this.state.draft.name, txids: this.state.draft.txids, seeds: this.state.draft.seeds, blockchain: value}, reportValidity: () => true};
      listeners.change({target: {id: '', name: 'blockchain', value, closest: selector => selector === '#new-case-form' ? currentForm : null}});
      currentForm = null;
    },
    changeRun(value) {listeners.change({target: {id: "run-picker", value, closest() {return null;}}});}, get focusCount() {return focusCount;},
    chooseSharedAttributionsFile() {listeners.change({target: {id: 'shared-attributions-file', files: []}});},
    async submitService(values) {
      const form = {id: 'service-form', values, reportValidity: () => true};
      elements.set('#service-form', form);
      listeners.submit({target: form, preventDefault() {}});
      await new Promise(setImmediate);
    },
    async submitDialog(values = {}) {
      dialogListeners.submit({target: {values, reportValidity: () => true}, preventDefault() {}});
      await new Promise(setImmediate);
    },
    changeBudgetSettings(values) {
      const form = {id: 'settings-form', values, reportValidity: () => true};
      elements.set('#settings-form', form);
      listeners.change({target: {id: 'budget-limits-enabled', name: 'budget_limits_enabled',
        closest: selector => selector === '#settings-form' ? form : null}});
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
    changeLayoutStyle(value, values = {}, workspace = false) {
      const form = workspace ? {id: 'settings-form', values: {...values, layout_style: value}} : this.plotForm({...values, layout_style: value});
      form.querySelector = selector => selector === 'input[name="group_context_inputs"]' ? {
        set checked(checked) {if (checked) form.values.group_context_inputs = 'on'; else delete form.values.group_context_inputs;},
      } : null;
      if (workspace) elements.set('#settings-form', form);
      const target = {id: '', name: 'layout_style', value, form,
        closest: selector => selector.includes('#' + form.id) ? form : null};
      // Browsers fire input before change on a select; both events preserve
      // the independent context-grouping preference.
      listeners.input({target});
      listeners.change({target});
      return form;
    },
    editPlotSettings(values) {
      const form = this.plotForm(values);
      listeners.input({target: {name: Object.keys(values)[0], id: '', form,
        closest: selector => selector === '#plot-layout-form' ? form : null}});
    },
    newForm(values) {
      currentForm = {id: 'new-case-form', values: {...defaults, ...values}, reportValidity: () => true};
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
  assert.match(view.newCase(), /<select name="blockchain" required><option value="liquid" selected>Liquid Network<\/option><option value="bitcoin">Bitcoin Network<\/option><\/select>/);
  assert.doesNotMatch(view.newCase(), /name="board"|Miro board URL or ID/);
  assert.doesNotMatch(view.newCase(), /Starting preferences/);
  view.navigate('new');
  view.state.draft.txids = txid;
  await view.dispatch('lookup');
  assert.deepEqual(view.calls[1], {path: '/api/lookup', body: {source: 'live', blockchain: 'liquid', txids: txid}});
  assert.equal(view.state.job.live, true);
  assert.match(view.newCase(), /<select name="blockchain" required disabled>/);
});

test('seed transactions show full IDs and grouped numeric vouts across investigation tabs', async () => {
  const view = await harness();
  const other = 'b'.repeat(64);
  const seeds = [`${txid}:10`, `${other}:1`, `${txid}:0`, `${txid}:2`, `${txid.toUpperCase()}:02`];
  view.state.activeCase = {id: 'case', name: 'Seed fixture', run_defaults: defaults, seeds};
  for (const tab of ['collect', 'plots', 'history']) {
    view.state.caseView = tab;
    const panel = view.workspace().match(/<section class="panel seed-panel"[\s\S]*?<\/section>/)[0];
    assert.match(panel, /2 transactions · 4 selected outputs/);
    assert.equal((panel.match(/<tr><td>/g) || []).length, 2);
    assert.match(panel, new RegExp(`href="https://blockstream.info/liquid/tx/${txid}"[^>]*>${txid}</a>`));
    assert.match(panel, /<span class="seed-vout">0<\/span> <span class="seed-vout">2<\/span> <span class="seed-vout">10<\/span>/);
    assert.match(panel, /Selected starting outputs for this investigation/);
  }
  assert.deepEqual(view.state.activeCase.seeds, seeds);
  assert.equal(view.calls.length, 1); // Rendering does not trigger lookup or collection.
});

test('seed transactions follow snapshot selections and support legacy and explicitly empty runs', async () => {
  const view = await harness();
  const earlier = 'b'.repeat(64), configured = 'c'.repeat(64);
  view.state.activeCase = {id: 'case', name: 'Saved seeds', run_defaults: defaults, latest_run: 'recent',
    seeds: [], runs: [{id: 'recent', status: 'bounded_complete', seeds: [`${txid}:0`]},
      {id: 'earlier', status: 'bounded_complete', seeds: [`${earlier}:2`]},
      {id: 'legacy', status: 'bounded_complete'}, {id: 'empty', status: 'bounded_complete', seeds: []}]};
  const panel = () => view.workspace().match(/<section class="panel seed-panel"[\s\S]*?<\/section>/)[0];
  assert.match(panel(), new RegExp(txid));
  assert.match(panel(), /Starting outputs recorded in this snapshot/);
  view.state.activeCase.seeds = [`${configured}:3`];
  view.state.selectedRun = 'earlier';
  assert.match(panel(), new RegExp(earlier));
  assert.doesNotMatch(panel(), new RegExp(`${txid}|${configured}`));
  view.state.selectedRun = 'legacy';
  assert.match(panel(), new RegExp(configured));
  assert.match(panel(), /Selected starting outputs for this investigation/);
  view.state.selectedRun = 'empty';
  assert.match(panel(), /No seed outputs are recorded for this snapshot/);
  assert.doesNotMatch(panel(), new RegExp(configured));
});

test('seed display rejects malformed outpoints and keeps synthetic transaction IDs offline', async () => {
  const view = await harness();
  view.state.activeCase = {id: 'case', name: 'Synthetic seeds', run_defaults: defaults, fixture: true,
    seeds: [`${txid}:0`, `${txid}:4294967296`, `${txid}:-1`, '<img src=x onerror=alert(1)>:0', null]};
  const panel = view.workspace().match(/<section class="panel seed-panel"[\s\S]*?<\/section>/)[0];
  assert.match(panel, /1 transaction · 1 selected output/);
  assert.match(panel, new RegExp(`<span class="mono seed-txid">${txid}</span>`));
  assert.doesNotMatch(panel, /<a |<img|onerror|4294967296/);
});

test('collected-data timings follow the selected snapshot and stay absent on older runs', async () => {
  const view = await harness();
  view.state.activeCase = {id: 'case', name: 'Timing fixture', run_defaults: defaults, latest_run: 'recent',
    runs: [{id: 'recent', status: 'bounded_complete', performance: {schema_version: 1, tracing_seconds: 125,
      address_counts_seconds: 20, request_count: 500}},
      {id: 'earlier', status: 'bounded_complete', performance: {schema_version: 1, tracing_seconds: 65}},
      {id: 'legacy', status: 'bounded_complete'}]};
  let html = view.workspace();
  assert.match(html, /<dt>Tracing<\/dt><dd>2 min 5 s<\/dd>/);
  assert.match(html, /<dt>Address counts<\/dt><dd>20 s<\/dd>/);
  view.state.selectedRun = 'earlier';
  view.state.caseView = 'history';
  html = view.workspace();
  assert.match(html, /<dt>Tracing<\/dt><dd>1 min 5 s<\/dd>/);
  assert.doesNotMatch(html, /<dt>Address counts|2 min 5 s/);
  view.state.selectedRun = 'legacy';
  assert.doesNotMatch(view.workspace(), /aria-label="Collection performance"/);
});

test('successful creation sends live source and resets the draft without sample seeds', async () => {
  const detail = {id: 'newcase', name: 'My investigation', run_defaults: defaults, runs: [], seeds: [`${txid}:0`]};
  const view = await harness((path) => path === '/api/cases' || path === '/api/cases/newcase/overview' ? detail : undefined);
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
  test(`recovered ${live ? 'live' : 'synthetic'} lookup never overwrites this tab's draft`, async () => {
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
      assert.equal(view.state.draft.txids, '');
      assert.equal(view.state.draft.reports.length, 0);
      await view.dispatch('load-job-outputs', {dataset: {id: 'lookup1'}});
      assert.equal(view.state.draft.txids, txid);
      assert.equal(view.state.draft.reports.length, 1);
    } else {
      assert.equal(view.state.draft.txids, '');
      assert.equal(view.state.draft.reports.length, 0);
      assert.match(view.state.jobs.get('lookup1').outcomeError, /lookup used synthetic data/);
    }
  });
}

test('collection polling updates hop depth in the header and then restores address-count and export progress', async () => {
  let progress = {phase: 'collecting', completed: 0, total: 10, message: 'Processing hop 0 of 10'};
  const view = await harness(path => path === '/api/jobs/collection' ? {status: 'running', progress} : undefined);
  const progressElement = {innerHTML: ''}, messageElement = {textContent: ''};
  view.elements.set('#job-progress', progressElement);
  view.elements.set('#job-message', messageElement);
  view.state.job = {id: 'collection', action: 'trace', message: 'Collecting data', started: Date.now()};
  await view.pollJob();
  view.render();
  const header = view.app.innerHTML.match(/<header class="workspace-header">([\s\S]*?)<\/header>/)?.[1];
  assert.match(header, /Hop progress/);
  assert.match(header, /Hop 0 \/ 10/);
  assert.match(header, /<progress[^>]*max="10" value="0"/);
  assert.match(header, /Hop depth, not a time estimate/);
  assert.doesNotMatch(view.app.innerHTML.split('<main')[1], /job-progress-bar/);
  for (const hop of [1, 4, 10]) {
    progress = {phase: 'collecting', completed: hop, total: 10, message: `Processing hop ${hop} of 10`};
    await view.pollJob();
    assert.equal(messageElement.textContent, `Processing hop ${hop} of 10`);
    assert.match(progressElement.innerHTML, new RegExp(`Hop ${hop} / 10`));
    assert.match(progressElement.innerHTML, new RegExp(`max="10" value="${hop}"`));
    assert.match(progressElement.innerHTML, new RegExp(`aria-valuetext="Processing hop ${hop} of 10"`));
    assert.doesNotMatch(progressElement.innerHTML, /completed|100%/i);
  }
  progress = {phase: 'address_counts', completed: 4, total: 26, message: 'Fetching address transaction counts'};
  await view.pollJob();
  assert.match(progressElement.innerHTML, /Current stage · address counts/);
  assert.match(progressElement.innerHTML, /4 \/ 26/);
  assert.match(progressElement.innerHTML, /max="26" value="4"/);
  assert.doesNotMatch(progressElement.innerHTML, /Hop progress|Hop 4/);
  progress = {phase: 'exporting_collection', completed: 0, total: 1, message: 'Saving collected transaction data and downloads'};
  await view.pollJob();
  assert.match(progressElement.innerHTML, /Current stage · exporting collection/);
  assert.match(progressElement.innerHTML, /max="1" value="0"/);
  assert.doesNotMatch(progressElement.innerHTML, /Hop progress/);
});

test('reloading collection recovers sanitized cumulative hop progress without using additional-hop defaults', async () => {
  const progress = JSON.parse(execFileSync('python3', ['-c', [
    'import json',
    'from liquid_tracer.progress import public_progress',
    'event = {"phase": "collecting", "completed": 6, "total": 11, "message": "private provider message"}',
    'print(json.dumps(public_progress(public_progress(event))))',
  ].join('\n')], {cwd: new URL('../../', import.meta.url), encoding: 'utf8', timeout: 15000}));
  const view = await harness(path => {
    if (path === '/api/session') return {csrf: 'test', settings: {...defaults, hops: 1}, cases: [], active_job: 'collection'};
    if (path === '/api/jobs/collection') return {id: 'collection', action: 'trace', status: 'running', started_at: 100, progress};
  });
  assert.equal(view.state.job.progress.total, 11);
  assert.match(view.app.innerHTML, /Processing hop 6 of 11/);
  assert.match(view.app.innerHTML, /Hop 6 \/ 11/);
  assert.match(view.app.innerHTML, /max="11" value="6"/);
  assert.doesNotMatch(view.app.innerHTML, /private provider message|of 11;.*of 11/);
  assert.deepEqual(view.calls.map(call => call.path), ['/api/session', '/api/jobs/collection']);
});

test('finished and paused collection retain the actual frontier rather than filling to the hop target', async () => {
  let progress;
  let status = 'running';
  const view = await harness(path => path === '/api/jobs/collection' ? {status, message: 'Request budget reached', progress} : undefined);
  const element = {innerHTML: ''};
  view.elements.set('#job-progress', element);
  view.state.job = {id: 'collection', action: 'trace'};
  for (const [phase, prefix] of [['collection_complete', 'Collection finished; last processing'], ['collection_paused', 'Collection paused while processing'], ['collection_error', 'Collection stopped while processing']]) {
    progress = {phase, completed: 3, total: 10, message: `${prefix} hop 3 of 10`};
    await view.pollJob();
    assert.match(element.innerHTML, /Hop 3 \/ 10/);
    assert.match(element.innerHTML, /max="10" value="3"/);
    assert.match(element.innerHTML, new RegExp(`aria-valuetext="${prefix} hop 3 of 10"`));
    assert.doesNotMatch(element.innerHTML, /value="10"|100%|Collected through/);
  }
  status = 'failed';
  await view.pollJob();
  assert.equal(view.state.jobs.get('collection').outcomeError, 'Request budget reached Last reported stage: Collection stopped while processing hop 3 of 10.');
});

test('zero-hop collection has a determinate starting-transactions indicator without premature completion', async () => {
  let progress;
  const view = await harness(path => path === '/api/jobs/collection' ? {status: 'running', progress} : undefined);
  const element = {innerHTML: ''};
  view.elements.set('#job-progress', element);
  view.state.job = {id: 'collection', action: 'trace'};
  for (const phase of ['collecting', 'collection_paused', 'collection_error', 'collection_complete']) {
    progress = {phase, completed: 0, total: 0, message: ''};
    await view.pollJob();
    assert.match(element.innerHTML, /Hop 0 \/ 0/);
    assert.match(element.innerHTML, /Starting transactions only \(hop 0\)/);
    assert.match(element.innerHTML, new RegExp(`max="1" value="${phase === 'collection_complete' ? 1 : 0}"`));
    assert.doesNotMatch(element.innerHTML, /max="0"|NaN|Infinity/);
  }
  progress = {phase: 'collection_empty', completed: 0, total: 0, message: 'No eligible queued outputs to collect'};
  await view.pollJob();
  assert.match(element.innerHTML, /Current stage · collection empty/);
  assert.doesNotMatch(element.innerHTML, /Hop progress|Hop 0|Starting transactions|max=/);
});

test('invalid hop counts fall back to an indeterminate stage without rendering impossible depth', async () => {
  let progress;
  const view = await harness(path => path === '/api/jobs/collection' ? {status: 'running', progress} : undefined);
  const element = {innerHTML: ''};
  view.elements.set('#job-progress', element);
  view.state.job = {id: 'collection', action: 'trace'};
  for (const [completed, total] of [[-1, 10], [11, 10], [1.5, 10], [2, 10.5], ['2', 10], [true, 10], [NaN, 10], [2, Infinity], [2, Number.MAX_SAFE_INTEGER + 1], [undefined, 10], [2, null]]) {
    progress = {phase: 'collecting', completed, total, message: 'Collecting data'};
    await view.pollJob();
    assert.match(element.innerHTML, /Current stage · collecting/);
    assert.match(element.innerHTML, /<progress class="job-progress-bar" aria-label="collecting"><\/progress>/);
    assert.doesNotMatch(element.innerHTML, /Hop progress|aria-valuetext|NaN|Infinity|max=/);
  }
});

test('ELK stays indeterminate and Miro item and retry indicators retain their existing units', async () => {
  let progress;
  const view = await harness(path => path === '/api/jobs/layout' ? {status: 'running', progress} : undefined);
  const element = {innerHTML: ''};
  view.elements.set('#job-progress', element);
  view.state.job = {id: 'layout', action: 'miro-sync'};
  progress = {phase: 'optimizing', completed: 1, total: 3, message: 'Optimizing graph'};
  await view.pollJob();
  assert.match(element.innerHTML, /Current stage · optimizing/);
  assert.doesNotMatch(element.innerHTML, /max=|Hop progress/);
  progress = {phase: 'creating', completed: 5, total: 40, message: 'Adding new Miro items'};
  await view.pollJob();
  assert.match(element.innerHTML, /5 \/ 40/);
  assert.match(element.innerHTML, /max="40" value="5"/);
  progress = {phase: 'waiting', completed: 5, total: 40, retry_after: 20, message: 'Waiting before retrying a Miro request'};
  await view.pollJob();
  assert.match(element.innerHTML, /Waiting 20 seconds before retrying Miro/);
  assert.doesNotMatch(element.innerHTML, /Hop progress/);
});

test('opening a saved investigation shows header feedback immediately and suppresses duplicate opens', async () => {
  const request = Promise.withResolvers();
  const detail = {id: 'savedcase', name: 'Saved <investigation>', run_defaults: defaults, runs: []};
  const view = await harness(path => path === '/api/cases/savedcase/overview' ? request.promise : undefined);
  view.state.cases = [detail];
  view.state.job = {id: 'active', action: 'trace', message: 'Collecting transactions', live: false};
  const opening = view.dispatch('open-case', {dataset: {id: detail.id}});
  await new Promise(setImmediate);
  assert.equal(view.state.page, 'dashboard');
  const header = view.app.innerHTML.match(/<header class="workspace-header">[\s\S]*?<\/header>/)[0];
  assert.match(header, /id="case-opening"[\s\S]*role="status" aria-live="polite"/);
  assert.match(header, /Opening Saved &lt;investigation&gt;/);
  assert.match(header, /Loading investigation overview\./);
  assert.match(header, /Collecting transactions/);
  assert.match(view.app.innerHTML, /data-case="savedcase" disabled/);
  assert.match(view.app.innerHTML, /data-action="open-case" disabled/);
  assert.doesNotMatch(view.app.innerHTML.match(/<button[^>]*data-page="new"[^>]*>/)[0], /disabled/);
  await view.dispatch('open-case', {dataset: {id: detail.id}});
  assert.equal(view.calls.filter(call => call.path === '/api/cases/savedcase/overview').length, 1);
  request.resolve(detail);
  await opening;
  assert.equal(view.state.activeCase.id, detail.id);
  assert.equal(view.state.openingCase, null);
  assert.equal(view.state.job.id, 'active');
  assert.doesNotMatch(view.app.innerHTML, /id="case-opening"/);
});

for (const failed of [false, true]) {
  test(`navigation ignores a late investigation ${failed ? 'error' : 'response'}`, async () => {
    const request = Promise.withResolvers();
    const view = await harness(path => path === '/api/cases/slowcase/overview' ? request.promise : undefined);
    const opening = view.dispatch('open-case', {dataset: {id: 'slowcase'}});
    await new Promise(setImmediate);
    view.navigate('new');
    assert.doesNotMatch(view.app.innerHTML, /id="case-opening"/);
    if (failed) request.reject(new Error('Old investigation error'));
    else request.resolve({id: 'slowcase', name: 'Slow case', runs: []});
    await opening;
    assert.equal(view.state.page, 'new');
    assert.equal(view.state.activeCase, null);
    assert.equal(view.state.error, '');
    assert.deepEqual(view.notifications, []);
  });

  test(`a newer investigation stays selected after an earlier ${failed ? 'error' : 'response'}`, async () => {
    const first = Promise.withResolvers();
    const second = {id: 'second', name: 'Second case', runs: []};
    const view = await harness(path => path === '/api/cases/first/overview' ? first.promise
      : path === '/api/cases/second/overview' ? second : undefined);
    const opening = view.dispatch('open-case', {dataset: {id: 'first'}});
    await new Promise(setImmediate);
    await view.dispatch('open-case', {dataset: {id: 'second'}});
    if (failed) first.reject(new Error('Earlier investigation error'));
    else first.resolve({id: 'first', name: 'First case', runs: []});
    await opening;
    assert.equal(view.state.activeCase.id, 'second');
    assert.equal(view.state.page, 'case');
    assert.equal(view.state.error, '');
    assert.equal(view.state.openingCase, null);
    assert.doesNotMatch(view.app.innerHTML, /id="case-opening"/);
  });
}

test('a failed investigation open shows its error, clears loading, and permits retry', async () => {
  const detail = {id: 'savedcase', name: 'Saved investigation', runs: []};
  let failed = true;
  const view = await harness(path => path === '/api/cases/savedcase/overview'
    ? failed ? {error: 'Saved records could not be read.'} : detail : undefined);
  view.state.cases = [detail];
  await view.dispatch('open-case', {dataset: {id: detail.id}});
  assert.equal(view.state.page, 'dashboard');
  assert.match(view.state.error, /Could not open Saved investigation\. Saved records could not be read\./);
  assert.equal(view.state.openingCase, null);
  assert.doesNotMatch(view.app.innerHTML, /id="case-opening"|The local server is unavailable/);
  assert.doesNotMatch(view.app.innerHTML.match(/<button[^>]*data-action="open-case"[^>]*>/)[0], /disabled/);
  failed = false;
  await view.dispatch('open-case', {dataset: {id: detail.id}});
  assert.equal(view.state.activeCase.id, detail.id);
  assert.equal(view.state.error, '');
});

test('an unavailable investigation deep link keeps the available workspace and saved list open', async () => {
  const detail = {id: 'savedcase', name: 'Saved investigation', runs: []};
  const view = await harness(path => path === '/api/session' ? {csrf: 'test', settings: defaults, cases: [detail]}
    : path === '/api/cases/savedcase/overview' ? {error: 'Investigation not found'} : undefined,
    {hash: '#case/savedcase'});
  assert.equal(view.state.page, 'dashboard');
  assert.match(view.app.innerHTML, /Saved investigations/);
  assert.match(view.app.innerHTML, /Could not open Saved investigation\. Investigation not found/);
  assert.doesNotMatch(view.app.innerHTML, /The local server is unavailable|id="retry-start"|id="case-opening"/);
});

test('a failed session request still reports that the local server is unavailable', async () => {
  const view = await harness(path => path === '/api/session' ? {error: 'Server is offline'} : undefined,
    {hash: '#case/savedcase'});
  assert.match(view.app.innerHTML, /The local server is unavailable/);
  assert.equal(view.calls.length, 1);
});

test('opening feedback preserves unsaved new-investigation fields before rendering', async () => {
  const request = Promise.withResolvers();
  const view = await harness(path => path === '/api/cases/savedcase/overview' ? request.promise : undefined);
  view.navigate('new');
  view.newForm({name: 'Unsaved name', txids: txid, seeds: `${txid}:3`, blockchain: 'liquid'});
  const opening = view.dispatch('open-case', {dataset: {id: 'savedcase'}});
  await new Promise(setImmediate);
  assert.equal(view.state.draft.name, 'Unsaved name');
  assert.equal(view.state.draft.txids, txid);
  assert.equal(view.state.draft.seeds, `${txid}:3`);
  request.resolve({id: 'savedcase', name: 'Saved investigation', runs: []});
  await opening;
});

test('opening feedback saves current investigation settings and address notes before rendering', async () => {
  const request = Promise.withResolvers();
  const view = await harness(path => path === '/api/cases/othercase/overview' ? request.promise : undefined);
  view.state.activeCase = {id: 'current', name: 'Current case', run_defaults: defaults, runs: []};
  view.navigate('case-settings');
  view.elements.set('#settings-form', {values: {...defaults, name: 'Unsaved case name', max_transactions: 37, budget_limits_enabled: 'on'}});
  view.elements.set('#service-form', {values: {service_name: 'Unsaved attribution', service_notes: 'Keep my notes',
    service_enabled: 'on', service_confidence: 'confirmed', service_source: 'My analysis', service_hop_limit: '5'}});
  const opening = view.dispatch('open-case', {dataset: {id: 'othercase'}});
  await new Promise(setImmediate);
  view.elements.delete('#settings-form');
  view.elements.delete('#service-form');
  view.navigate('case-settings');
  assert.match(view.settingsPage(), /value="Unsaved case name"/);
  assert.match(view.settingsPage(), /name="max_transactions"[^>]*value="37"/);
  assert.equal(view.state.addressReview.name, 'Unsaved attribution');
  assert.equal(view.state.addressReview.notes, 'Keep my notes');
  assert.equal(view.state.addressReview.hopLimit, '5');
  request.resolve({id: 'othercase', name: 'Other case', runs: []});
  await opening;
  assert.equal(view.state.activeCase.id, 'current');
});

test('returning to the current investigation tools abandons an in-flight case switch', async () => {
  for (const action of ['view-history', 'input-import-open', 'change-outputs-open', 'name-colors-open']) {
    const request = Promise.withResolvers();
    const view = await harness(path => path === '/api/cases/othercase/overview' ? request.promise : undefined);
    view.state.activeCase = {id: 'current', name: 'Current case', runs: []};
    view.state.page = 'case';
    const opening = view.dispatch('open-case', {dataset: {id: 'othercase'}});
    await new Promise(setImmediate);
    await view.dispatch(action);
    request.resolve({id: 'othercase', name: 'Other case', runs: []});
    await opening;
    assert.equal(view.state.activeCase.id, 'current', action);
    assert.equal(view.state.openingCase, null, action);
    assert.equal(view.state.page, action === 'view-history' ? 'case' : 'case-settings', action);
  }
});

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
    assert.match(html, /all locally saved attributions, name colors, and change outputs across every page/);
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
    if (path === '/api/cases/case1/overview') return detail();
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
  view.state.jobs.delete('review1');
  await view.dispatch('miro-frame-review');
  await view.dispatch('dashboard');
  await view.pollJob();
  assert.equal(view.dialog.open, false);
  assert.match(view.notifications.at(-1), /Choose Recover interrupted frame again/);
  assert.equal(view.calls.filter(call => call.path.endsWith('/actions')).length, 2);
});


test('context input grouping defaults off and survives new-case and trace form submission', async () => {
  const detail = {id: 'groupcase', name: 'Grouped case', run_defaults: {...defaults, include_fees: false, group_context_inputs: true}, runs: [], seeds: [`${txid}:0`]};
  const view = await harness(path => path === '/api/cases' || path === '/api/cases/groupcase/overview' ? detail : undefined);
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
  const view = await harness(path => path === '/api/cases' || path === '/api/cases/hubcase/overview' ? detail
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
  const view = await harness(path => path === '/api/cases' || path === '/api/cases/layoutcase/overview' ? detail : undefined);
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
    if (path === '/api/cases/layoutcase/overview') return detail;
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
  assert.deepEqual(workspace, {...inherited, budget_limits_enabled: false, hops: 4, layout_attempts: 35, connector_style: 'elbowed',
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
    run_defaults: {...defaults, budget_limits_enabled: true}, runs: [{id: 'saved1'}, {id: 'saved2'}],
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
    run_defaults: {...defaults, budget_limits_enabled: true}, runs: [{id: 'original'}, {id: 'later'}],
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
    : path === '/api/cases/case1/overview' ? refreshed : undefined);
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
    : path === '/api/cases/case1/overview' ? detail : undefined);
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
    if (path === '/api/cases/arrowcase/overview') return detail;
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
  const view = await harness(path => path === `/api/cases/${seeded.id}/overview` ? seeded
    : path === `/api/cases/${details.empty.id}/overview` ? details.empty
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
    : path === '/api/cases/case1/overview' ? pegoutCase([search], {latest_run: 'main-run'}) : undefined);
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
      : path === '/api/cases/case1/overview' ? pegoutCase([search]) : undefined);
    view.state.activeCase = pegoutCase(); view.state.job = {id: 'pegjob', action: 'pegouts', caseId: 'case1', live: true};
    await view.pollJob();
    assert.equal(view.currentPegoutSearch(view.state.activeCase).id, search.id);
    assert.equal(view.calls.some(call => call.path === '/api/cases/case1/overview'), true);
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
    if (path === '/api/cases/centeredcase/overview') return detail;
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
  assert.match(view.workspace(), /Plots & Miro/);
  assert.doesNotMatch(view.workspace(), /Plot views/);
  assert.doesNotMatch(view.workspace(), /id="collection-panel"|data-action="workflow-board-sync"/);
  await view.dispatch('view-history');
  assert.match(view.workspace(), /CSV downloads/);
  assert.match(view.workspace(), /Run history/);
  assert.doesNotMatch(view.workspace(), /id="connection-hops"/);
  assert.equal(view.state.selectedRun, 'old-id');
  assert.equal(view.calls.length, 1, 'navigation performs no fetches or writes');
});

test('compact trace form submits its hop allowance and hop basis while retaining other saved preferences', async () => {
  const settings = {...defaults, hops: 2, include_fees: true, group_context_inputs: true, color_attribution_arrows: true, center_name: 'Treasury', connector_style: 'curved'};
  const view = await harness(path => path.endsWith('/actions') ? {id: 'job1', status: 'running'} : undefined);
  view.state.activeCase = {id: 'case', name: 'Case', run_defaults: settings, runs: []};
  view.openActionDialog('trace');
  assert.match(view.dialog.innerHTML, /name="hops"/);
  assert.doesNotMatch(view.dialog.innerHTML, /name="(?:include_fees|layout_attempts|max_transactions|max_new_items|connector_style)"/);
  await view.submitDialog({hops: '5'});
  assert.deepEqual(view.calls.at(-1).body, {action: 'trace', run_id: 'latest', hops: 5, hop_reference_name: ''});
  assert.equal(view.state.activeCase.run_defaults.hops, 2);
  assert.equal(view.state.activeCase.run_defaults.group_context_inputs, true);
});

test('named-group collection saves only its hop basis and keeps original seeds and centered layout', async () => {
  const settings = {...defaults, hops: 3, hop_reference_name: 'Perp', center_name: 'Service', hub_addresses: []};
  const detail = workflowCase({run_defaults: settings});
  const view = await harness(path => path.endsWith('/actions') ? {id: 'trace', status: 'running'} : undefined);
  view.state.activeCase = detail;
  assert.match(view.workspace(), /Hops from named group: Perp/);
  view.openActionDialog('trace');
  assert.match(view.dialog.innerHTML, /name="hop_reference_name" maxlength="120" value="Perp"/);
  assert.match(view.dialog.innerHTML, /First run, changed starting outputs, or changed named group: maximum hops/);
  assert.match(view.dialog.innerHTML, /Same starting outputs and hop basis: additional hops/);
  assert.match(view.dialog.innerHTML, /At the hop limit, the next spending transaction is checked/);
  assert.doesNotMatch(view.dialog.innerHTML, /name="center_name"/);
  await view.submitDialog({hops: '2', hop_reference_name: '  Other group  '});
  assert.deepEqual(view.calls.at(-1).body, {action: 'trace', run_id: 'latest', hops: 2, hop_reference_name: 'Other group'});
  assert.equal(detail.run_defaults.hop_reference_name, 'Other group');
  assert.equal(detail.run_defaults.center_name, 'Service');
  assert.equal(detail.run_defaults.hops, 3);
  assert.deepEqual(detail.seeds, [`${txid}:0`]);
  assert.equal(view.calls.some(call => call.path.endsWith('/settings')), false);
  const reopened = await harness();
  reopened.state.activeCase = detail;
  reopened.openActionDialog('trace');
  assert.match(reopened.dialog.innerHTML, /name="hop_reference_name"[^>]*value="Other group"/);
  reopened.state.caseView = 'plots';
  assert.doesNotMatch(reopened.workspace(), /name="hop_reference_name"/);
  assert.match(reopened.workspace(), /name="center_name"[^>]*value="Service"/);
});

test('blank collection basis explicitly restores seed hops and rejected collection preserves its preference', async () => {
  for (const reject of [false, true]) {
    const view = await harness(path => path.endsWith('/actions')
      ? reject ? {error: 'Invalid collection request'} : {id: 'trace', status: 'running'} : undefined);
    view.state.activeCase = workflowCase({run_defaults: {...defaults, hop_reference_name: 'Perp'}});
    view.openActionDialog('trace');
    await view.submitDialog({hops: '0', hop_reference_name: '  '});
    assert.deepEqual(view.calls.at(-1).body, {action: 'trace', run_id: 'latest', hops: 0, hop_reference_name: ''});
    assert.equal(view.state.activeCase.run_defaults.hop_reference_name, reject ? 'Perp' : '');
  }
});

test('legacy settings use seed hops while unrelated forms preserve an existing named-group preference', async () => {
  const view = await harness();
  const legacy = {...defaults, hub_addresses: []};
  delete legacy.hop_reference_name;
  assert.equal(view.readSettings({values: {}}, legacy).hop_reference_name, '');
  const selected = {...legacy, hop_reference_name: 'Perp'};
  assert.equal(view.readSettings({values: {hops: '6'}}, selected).hop_reference_name, 'Perp');
  assert.equal(view.readSettings({values: {hop_reference_name: ''}}, selected).hop_reference_name, '');
});

test('collection name suggestions are local escaped enabled names and ignore replies after closing', async () => {
  let resolveNames;
  const view = await harness(path => path.endsWith('/name-colors') ? new Promise(resolve => {resolveNames = resolve;}) : undefined);
  view.state.activeCase = workflowCase();
  view.openActionDialog('trace');
  const input = {value: ' Perp '}, list = {innerHTML: ''};
  view.elements.set('#action-form input[name="hop_reference_name"]', input);
  view.elements.set('#hop-reference-name-options', list);
  const pending = view.suggestHopReferenceNames();
  assert.deepEqual(view.calls.at(-1).body, {query: 'Perp', offset: 0, limit: 100});
  resolveNames({rows: [{name: 'Perp "<one>', enabled_addresses: 2}, {name: 'Disabled', enabled_addresses: 0}]});
  await pending;
  assert.match(list.innerHTML, /Perp &quot;&lt;one&gt;/);
  assert.doesNotMatch(list.innerHTML, /Disabled|<one>/);
  const stale = view.suggestHopReferenceNames();
  view.dialog.close();
  resolveNames({rows: [{name: 'Stale reply', enabled_addresses: 1}]});
  await stale;
  assert.doesNotMatch(list.innerHTML, /Stale reply/);
});

test('saved run and plot hop descriptions follow their recorded basis rather than current collection preference', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({run_defaults: {...defaults, hop_reference_name: 'Next group'},
    runs: [{id: 'saved1', status: 'bounded_complete', max_hops: 2, collected_hops: 2, hop_reference_name: 'Perp <A>'},
      {id: 'legacy', status: 'bounded_complete', max_hops: 5, collected_hops: 5}],
    plots: [workflowPlot('pegouts', 'plot1', {hop_reference_name: 'Original group'})]});
  assert.match(view.workspace(), /Deepest collected group-relative hop/);
  assert.match(view.workspace(), /Hops from named group: Perp &lt;A&gt;/);
  view.state.caseView = 'history';
  assert.match(view.workspace(), /<th>Counted from<\/th>/);
  assert.match(view.workspace(), /<td>Perp &lt;A&gt;<\/td>/);
  assert.match(view.workspace(), /<td>Starting transactions<\/td>/);
  view.state.caseView = 'plots';
  assert.match(view.workspace(), /Hops from named group: Perp &lt;A&gt;/);
  assert.match(view.workspace(), /Hops from named group: Original group/);
  assert.doesNotMatch(view.workspace(), /Hops from named group: Next group/);
  view.state.selectedRun = 'legacy';
  assert.match(view.workspace(), /Hops from starting transactions/);
  assert.match(view.workspace(), /Starting transactions are hop 0/);
});

test('group-relative progress labels resets including a zero-hop ceiling without implying seed-only collection', async () => {
  let progress = {phase: 'collecting', completed: 2, total: 3, hop_reference_name: 'Perp <A>'};
  const view = await harness(path => path === '/api/jobs/trace' ? {status: 'running', progress} : undefined);
  const element = {innerHTML: ''};
  view.elements.set('#job-progress', element);
  view.state.job = {id: 'trace', action: 'trace'};
  for (const hop of [2, 0, 1]) {
    progress = {...progress, completed: hop};
    await view.pollJob();
    assert.match(element.innerHTML, new RegExp(`Group-relative hop ${hop} / 3`));
    assert.match(element.innerHTML, /returns to the group reset to hop 0/);
    assert.match(element.innerHTML, /Perp &lt;A&gt;/);
  }
  progress = {...progress, completed: 0, total: 0};
  await view.pollJob();
  assert.match(element.innerHTML, /Group-relative hop 0 \/ 0/);
  assert.match(element.innerHTML, /max="1" value="0"/);
  assert.doesNotMatch(element.innerHTML, /Starting transactions only/);
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
const boardControl = (record, caseId = 'case1') => ({dataset: {record, caseId}});
const boardEdit = (view, record, field, value, caseId = 'case1') => view.workflowInput({
  id: `workflow-board-${field}-${record}`, value, dataset: {record, caseId, boardField: field},
});
const boardCardHtml = (view, record) => view.workspace().match(new RegExp(`<article[^>]*data-board-record="${record}"[\\s\\S]*?<\/article>`))?.[0];
const layoutDefaults = {layout_style: 'standard', layout_attempts: 25, connector_style: 'straight', include_fees: false, color_attribution_arrows: false, group_context_inputs: false, center_name: '', hub_addresses: []};
const layoutKeys = ['layout_style', 'layout_attempts', 'connector_style', 'include_fees', 'color_attribution_arrows',
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
  assert.match(html, /Each generation captures this selected run, investigation rules, and layout settings/);
  assert.doesNotMatch(html, /Graph settings/);
  assert.match(html, /data-action="plot-settings-save"/);
  assert.equal(view.calls.length, 1);
});

test('generating a plot captures changed layout settings without mutating investigation defaults', async () => {
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
  assert.deepEqual(writes.map(call => call.path), ['/api/cases/case1/actions']);
  assert.deepEqual(writes[0].body, {action: 'plot', layout_mode: 'fresh', goal: 'full', run_id: 'saved1', min_hops: 0, max_hops: 10, hop_basis: 'original_seeds', layout_settings: {layout_style: 'standard', layout_attempts: 40, connector_style: 'curved', include_fees: true,
    group_context_inputs: true, color_attribution_arrows: true, center_name: 'Treasury', hub_addresses: [first, second]}});
  assert.equal(detail.run_defaults.layout_attempts, 25);
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

test('saving a peg-out layout updates fees and hubs and reloads saved preferences', async () => {
  const hubs = ['G' + 'a'.repeat(33)];
  const editedHub = 'H' + 'b'.repeat(33);
  const detail = workflowCase({runs: [], latest_run: undefined, run_defaults: {...defaults,
    include_fees: true, group_context_inputs: true, hub_addresses: hubs}});
  const respond = (path, body) => {
    if (path === '/api/cases/case1/plot-settings') {
      detail.run_defaults = {...detail.run_defaults, ...body.settings};
      return detail;
    }
    if (path === '/api/cases/case1/overview') return detail;
  };
  const view = await harness(respond);
  view.state.activeCase = detail;
  await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  const disabledFields = view.workspace().match(/<fieldset[^>]*\bdisabled[^>]*>[\s\S]*?<\/fieldset>/g) || [];
  assert.ok(!disabledFields.some(fieldset => fieldset.includes('name="hub_addresses"')));
  assert.ok(!disabledFields.some(fieldset => fieldset.includes('name="include_fees"')));
  assert.doesNotMatch(contextGroupingFields(view), /disabled/);
  view.plotForm({layout_attempts: '61', connector_style: 'curved', center_name: 'Saved Treasury',
    include_fees_present: '1', color_attribution_arrows_present: '1', color_attribution_arrows: 'on',
    hub_addresses: ` ${editedHub}\n${editedHub} `});
  await view.dispatch('plot-settings-save');
  const saved = view.calls.find(call => call.path.endsWith('/plot-settings')).body.settings;
  assert.deepEqual(Object.keys(saved).sort(), [...layoutKeys].sort());
  assert.equal(saved.include_fees, false);
  assert.equal(saved.group_context_inputs, true);
  assert.deepEqual(saved.hub_addresses, [editedHub]);
  assert.equal(view.calls.some(call => call.path.endsWith('/actions')), false);
  const restarted = await harness(respond);
  await restarted.dispatch('open-case', {dataset: {id: 'case1'}});
  await restarted.dispatch('view-plots');
  assert.match(restarted.workspace(), /name="layout_attempts"[^>]*value="61"/);
  assert.match(restarted.workspace(), /value="Saved Treasury"/);
  assert.ok(restarted.workspace().includes(editedHub));
  assert.doesNotMatch(restarted.workspace(), /name="include_fees"[^>]*checked/);
  assert.equal(restarted.state.settings.layout_attempts, 25, 'per-investigation saves do not overwrite workspace preferences');
});


test('branch hub controls are available for full and peg-out layouts and stay disabled for connections', async () => {
  const editedHub = 'H' + 'b'.repeat(33);
  for (const goal of ['full', 'pegouts', 'connections']) {
    const view = await harness(path => path.endsWith('/actions') ? {id: 'hub-job', status: 'running'} : undefined);
    view.state.activeCase = workflowCase();
    await view.dispatch('view-plots');
    await view.dispatch('plot-goal', {dataset: {goal}});
    const fields = view.workspace().match(/<fieldset class="layout-fields full-trace-fields"[^>]*>[\s\S]*?<\/fieldset>/)?.[0];
    assert.ok(fields);
    assert.match(fields, /<legend>Branch hubs<\/legend>/);
    assert.match(fields, /name="hub_addresses"/);
    if (goal === 'connections') {
      assert.match(fields, /disabled/);
    } else {
      assert.doesNotMatch(fields, /disabled/);
      view.plotForm({hub_addresses: ` ${editedHub}\n${editedHub} `});
      await view.dispatch('workflow-plot');
      const request = view.calls.find(call => call.path.endsWith('/actions'));
      assert.deepEqual(request.body.layout_settings.hub_addresses, [editedHub]);
      assert.equal(request.body.goal, goal);
      assert.deepEqual(view.state.activeCase.run_defaults.hub_addresses || [], []);
    }
  }
});

test('a failed explicit defaults save retains edits and does not submit generation', async () => {
  const view = await harness(path => path.endsWith('/plot-settings') ? {error: 'Cannot save layout settings.'} : undefined);
  view.state.activeCase = workflowCase();
  await view.dispatch('view-plots');
  view.plotForm({layout_attempts: '45', center_name: 'Retain this group'});
  await assert.rejects(view.dispatch('plot-settings-save'), /Cannot save layout settings/);
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
  const view = await harness(path => Object.values(cases).find(detail => path === `/api/cases/${detail.id}/overview`));
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
    for (const name of ['Full trace', 'Starter connections', 'Paths to endpoints']) assert.match(html, new RegExp(name));
    assert.doesNotMatch(html, /data-action="(?:trace-dialog|pegouts-start|miro-sync-dialog|workflow-board-sync)"/);
    await view.dispatch('plot-goal', {dataset: {goal}});
    workflowEdit(view, 'min-hops', '2'); workflowEdit(view, 'max-hops', '10');
    await view.dispatch('workflow-plot');
    assert.deepEqual(view.calls.at(-1), {path: '/api/cases/case1/actions', body: {
      action: 'plot', layout_settings: layoutDefaults, layout_mode: 'fresh', goal, run_id: 'older', min_hops: goal === 'pegouts' ? 2 : 0, max_hops: 10, ...(goal === 'connections' ? {connection_scope: 'hop_limited'} : {hop_basis: 'original_seeds'}), ...(goal === 'pegouts' ? {include_unspent: true, include_unspendable: true, include_attributed_stops: true} : {})}});
    assert.equal(view.state.job.live, false);
    assert.equal(view.calls.filter(call => call.path.endsWith('/actions')).length, 1);
  }
});

test('Starter connections has its own hop limit and ignores stale peg-out limits', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'plotjob', status: 'running'} : undefined);
  view.state.activeCase = workflowCase();
  await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  assert.match(view.workspace(), /id="workflow-max-hops"/);
  workflowEdit(view, 'min-hops', 'bad'); workflowEdit(view, 'max-hops', '-1');
  await view.dispatch('plot-goal', {dataset: {goal: 'connections'}});
  assert.doesNotMatch(view.workspace(), /id="workflow-(?:min|max)-hops"/);
  assert.match(view.workspace(), /Attribution hop limits and stop-tracing rules are ignored; labels remain visible/);
  assert.match(view.workspace(), /id="workflow-connection-max-hops"[^>]*value="10"/);
  assert.match(view.workspace(), /No additional transactions are fetched/);
  await view.dispatch('workflow-plot');
  assert.equal(view.calls.at(-1).body.goal, 'connections');
  assert.equal(view.calls.at(-1).body.min_hops, 0);
  assert.equal(view.calls.at(-1).body.max_hops, 10);
  assert.equal(view.calls.at(-1).body.connection_scope, 'hop_limited');
});

test('saved Starter layouts distinguish shortest, all-saved, bounded and legacy scopes', async () => {
  for (const scope of ['shortest', 'all_saved', 'hop_limited', undefined]) {
    const allSaved = scope === 'all_saved';
    const plot = workflowPlot('connections', 'connections1',
      {max_hops: allSaved || scope === 'shortest' ? null : 3, ...(scope ? {query: {connection_scope: scope}} : {})});
    const view = await harness();
    view.state.activeCase = workflowCase({plots: [plot], boards: [workflowBoard('connections', 'board1', {preview_id: plot.preview_id})]});
    for (const page of ['view-plots', 'view-history', 'view-boards']) {
      await view.dispatch(page);
      const html = view.workspace();
      assert.match(html, scope === 'shortest' ? /Shortest connections/ : allSaved ? /All saved connections/ : scope === 'hop_limited' ? /Within 3 hops of each starter/ : /Hops 0–3/);
      if (scope === 'shortest') {
        assert.match(html, /One shortest saved route per connected ordered pair/);
        assert.doesNotMatch(html, /All verified saved connections|Legacy bounded connections/);
      }
      if (!scope) {
        assert.match(html, /Legacy bounded connections/);
        assert.doesNotMatch(html, /Connecting paths within 3 ordinary transaction steps/);
      }
      assert.doesNotMatch(html, /Hops 0–null/);
      if (allSaved) assert.match(html, /All verified saved connections/);
    }
  }
});

test('standalone Starter preview starts without a hop input', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'connectionjob', status: 'running'} : undefined);
  view.state.activeCase = workflowCase();
  await view.dispatch('view-history');
  assert.equal(view.elements.has('#connection-hops'), false);
  await view.dispatch('connections');
  assert.deepEqual(view.calls.at(-1), {path: '/api/cases/case1/actions', body: {action: 'connections', run_id: 'saved1'}});
});

test('standalone Starter history labels only marked snapshots as complete transaction accounting', async () => {
  for (const complete of [true, false]) {
    const view = await harness();
    view.state.activeCase = workflowCase({artifacts: {saved1: {connections: {downloads: [],
      include_fees: complete, connection_scope: 'all_saved', connection_count: 2, group_context_inputs: true,
      ...(complete ? {transaction_io: 'complete', context_edge_count: 9} : {})}}}});
    await view.dispatch('view-history');
    assert.match(view.workspace(), complete
      ? /Saved layout: All transaction inputs and outputs, including fees\. Isolated context inputs grouped\. 9 context connections, excluded from starter-pair counts\./
      : /Saved layout: Paths only\./);
    assert.doesNotMatch(view.workspace(), /undefined context connections|NaN context connections/);
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

test('endpoint types default on and optional booleans are submitted only for this goal', async () => {
  for (const goal of ['full', 'connections', 'pegouts']) {
    for (const [includeUnspent, includeUnspendable] of [[false, false], [true, false], [false, true], [true, true]]) {
      const view = await harness(path => path.endsWith('/actions') ? {id: 'plotjob', status: 'running'} : undefined);
      view.state.activeCase = workflowCase();
      await view.dispatch('view-plots');
      assert.doesNotMatch(view.workspace(), /id="workflow-include-(?:unspent|unspendable)"/);
      await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
      for (const id of ['unspent', 'unspendable', 'attributed-stops']) {
        const input = view.workspace().match(new RegExp(`<input id="workflow-include-${id}"[^>]*>`))?.[0];
        assert.ok(input);
        assert.match(input, /checked/);
      }
      assert.match(view.workspace(), /Peg-out requests are always included/);
      assert.match(view.workspace(), /observed unspent in the selected collection run/);
      assert.match(view.workspace(), /Unchecked outputs and outputs stopped only by a hop limit are not counted as unspent/);
      view.workflowInput({id: 'workflow-include-unspent', checked: includeUnspent});
      view.workflowInput({id: 'workflow-include-unspendable', checked: includeUnspendable});
      await view.dispatch('plot-goal', {dataset: {goal}});
      if (goal !== 'pegouts') assert.doesNotMatch(view.workspace(), /id="workflow-include-(?:unspent|unspendable)"/);
      await view.dispatch('workflow-plot');
      assert.deepEqual(view.calls.at(-1).body, {action: 'plot', layout_settings: layoutDefaults, layout_mode: 'fresh', goal, run_id: 'saved1',
        min_hops: 0, max_hops: 10, ...(goal === 'connections' ? {connection_scope: 'hop_limited'} : {hop_basis: 'original_seeds'}),
        ...(goal === 'pegouts' && includeUnspent ? {include_unspent: true} : {}),
        ...(goal === 'pegouts' && includeUnspendable ? {include_unspendable: true} : {}), ...(goal === 'pegouts' ? {include_attributed_stops: true} : {})});
      assert.equal(view.state.job.live, false);
      assert.equal(view.calls.filter(call => call.body).length, 1, 'endpoint choices do not update settings or collect data');
    }
  }
});

test('endpoint drafts survive tab and goal changes without leaking into another investigation', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase();
  await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  view.workflowInput({id: 'workflow-include-unspent', checked: true});
  view.workflowInput({id: 'workflow-include-unspendable', checked: true});
  await view.dispatch('view-boards');
  await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'connections'}});
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  for (const id of ['unspent', 'unspendable']) assert.match(view.workspace(), new RegExp(`id="workflow-include-${id}"[^>]*checked`));
  view.workflowInput({id: 'workflow-include-unspent', checked: false});
  await view.dispatch('view-plots');
  assert.doesNotMatch(view.workspace(), /id="workflow-include-unspent"[^>]*checked/);
  view.state.activeCase = workflowCase({id: 'other'});
  await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  for (const id of ['unspent', 'unspendable', 'attributed-stops']) assert.match(view.workspace(), new RegExp(`id="workflow-include-${id}"[^>]*checked`));
  assert.equal(view.calls.length, 1);
});

test('peg-out layouts describe non-fee I/O and optional fees without an optional context toggle', async () => {
  for (const goal of ['full', 'connections', 'pegouts']) {
    const view = await harness(path => path.endsWith('/actions') ? {id: 'plotjob', status: 'running'} : undefined);
    view.state.activeCase = workflowCase();
    await view.dispatch('view-plots');
    await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
    const html = view.workspace();
    assert.match(html, /Every included transaction shows all its inputs and non-fee outputs/);
    assert.match(html, /Fee outputs follow the Include transaction fee flows setting/);
    assert.match(html, /Outputs on excluded branches remain visible without continuing those branches/);
    assert.match(html, /These extra objects do not add endpoint matches or change the selected endpoint totals/);
    assert.doesNotMatch(html, /id="workflow-include-context"|Include context addresses/);
    assert.equal(view.workflowInput({id: 'workflow-include-context', checked: false}), false);
    view.workflowInput({id: 'workflow-include-unspent', checked: true});
    await view.dispatch('plot-goal', {dataset: {goal}});
    await view.dispatch('workflow-plot');
    assert.deepEqual(view.calls.at(-1).body, {action: 'plot', layout_settings: layoutDefaults, layout_mode: 'fresh', goal, run_id: 'saved1',
      min_hops: 0, max_hops: 10, ...(goal === 'connections' ? {connection_scope: 'hop_limited'} : {hop_basis: 'original_seeds'}),
      ...(goal === 'pegouts' ? {include_unspent: true, include_unspendable: true, include_attributed_stops: true} : {})});
    assert.equal(view.state.job.live, false);
    assert.equal(view.calls.filter(call => call.body).length, 1, 'display choices neither update settings nor collect data');
  }
});

const contextGroupingFields = view => view.workspace().match(/<fieldset class="layout-fields context-grouping-fields"[^>]*>[\s\S]*?<\/fieldset>/)?.[0];

test('context grouping is available for every plot goal and preserves edits across goals', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase();
  await view.dispatch('view-plots');
  assert.doesNotMatch(contextGroupingFields(view), /disabled/);
  view.plotForm({group_context_inputs_present: '1', group_context_inputs: 'on'});
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  view.elements.delete('#plot-layout-form');
  assert.doesNotMatch(contextGroupingFields(view), /disabled/);
  assert.match(contextGroupingFields(view), /name="group_context_inputs"[^>]*checked/);
  assert.match(contextGroupingFields(view), /bundle repeated context inputs from the same address to the same transaction/);
  assert.match(contextGroupingFields(view), /Shared or named addresses stay visible, and traced inputs keep their own arrows/);
  view.plotForm({layout_attempts: '47', connector_style: 'curved', center_name: 'Draft treasury',
    group_context_inputs_present: '1'});
  await view.dispatch('plot-goal', {dataset: {goal: 'connections'}});
  view.elements.delete('#plot-layout-form');
  assert.doesNotMatch(contextGroupingFields(view), /disabled/);
  assert.doesNotMatch(contextGroupingFields(view), /name="group_context_inputs"[^>]*checked/);
  assert.match(view.workspace(), /name="layout_attempts"[^>]*value="47"/);
  assert.match(view.workspace(), /value="Draft treasury"/);
  assert.match(view.workspace(), /value="curved" selected/);
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  assert.doesNotMatch(contextGroupingFields(view), /disabled|name="group_context_inputs"[^>]*checked/);
  view.plotForm({group_context_inputs_present: '1', group_context_inputs: 'on'});
  await view.dispatch('plot-goal', {dataset: {goal: 'connections'}});
  view.elements.delete('#plot-layout-form');
  assert.doesNotMatch(contextGroupingFields(view), /disabled/);
  assert.match(contextGroupingFields(view), /name="group_context_inputs"[^>]*checked/);
  for (const goal of ['full', 'pegouts']) {
    await view.dispatch('plot-goal', {dataset: {goal}});
    assert.doesNotMatch(contextGroupingFields(view), /disabled/);
    assert.match(contextGroupingFields(view), /name="group_context_inputs"[^>]*checked/);
    assert.match(view.workspace(), /value="Draft treasury"/);
  }
  assert.equal(view.calls.length, 1, 'draft changes neither save nor queue work');
});

test('standalone Starter preview history uses the captured fee choice', async () => {
  for (const includeFees of [false, true]) {
    const view = await harness();
    view.state.activeCase = workflowCase({run_defaults: {...defaults, include_fees: !includeFees},
      artifacts: {saved1: {connections: {downloads: [], include_fees: includeFees,
        transaction_io: 'complete', connection_scope: 'all_saved', connection_count: 2}}}});
    await view.dispatch('view-history');
    assert.ok(view.workspace().includes(`Saved layout: All transaction inputs and ${includeFees ? 'outputs, including fees' : 'non-fee outputs'}.`));
    assert.equal(view.calls.filter(call => call.body).length, 0);
  }
});

test('new and older investigations default to hidden fees for every plot goal', async () => {
  for (const missingSetting of [false, true]) {
    const run_defaults = {...defaults, include_fees: false};
    if (missingSetting) delete run_defaults.include_fees;
    const view = await harness();
    view.state.activeCase = workflowCase({run_defaults});
    await view.dispatch('view-plots');
    for (const goal of ['full', 'connections', 'pegouts']) {
      await view.dispatch('plot-goal', {dataset: {goal}});
      const fees = view.workspace().match(/<fieldset class="layout-fields fee-flow-fields"[^>]*>[\s\S]*?<\/fieldset>/)?.[0];
      assert.ok(fees);
      assert.doesNotMatch(fees, /disabled|name="include_fees"[^>]*checked/);
      assert.match(fees, /Off by default for every plot type/);
    }
    assert.equal(view.calls.filter(call => call.body).length, 0, 'checking defaults queues no work');
  }
});

test('every plot goal enables fee opt-in and submits both states for preview and board generation', async () => {
  for (const goal of ['full', 'connections', 'pegouts']) for (const includeFees of [false, true]) for (const action of ['workflow-plot', 'workflow-plot-sync']) {
    const hubs = ['G' + 'a'.repeat(33)];
    const detail = workflowCase({run_defaults: {...defaults, include_fees: !includeFees, hub_addresses: hubs}});
    const view = await harness(path => path.endsWith('/actions') ? {id: 'fee-job', status: 'running'} : undefined);
    view.state.activeCase = detail;
    await view.dispatch('view-plots');
    await view.dispatch('plot-goal', {dataset: {goal}});
    const html = view.workspace();
    const fees = html.match(/<fieldset class="layout-fields fee-flow-fields"[^>]*>[\s\S]*?<\/fieldset>/)?.[0];
    assert.ok(fees);
    assert.doesNotMatch(fees, /disabled/);
    assert.match(fees, /name="include_fees_present" value="1"/);
    assert.match(fees, /name="include_fees" type="checkbox"/);
    if (includeFees) assert.doesNotMatch(fees, /name="include_fees"[^>]*checked/);
    else assert.match(fees, /name="include_fees"[^>]*checked/);
    const fullOnly = html.match(/<fieldset class="layout-fields full-trace-fields"[^>]*>[\s\S]*?<\/fieldset>/)?.[0];
    if (goal === 'connections') assert.match(fullOnly, /disabled/);
    else assert.doesNotMatch(fullOnly, /disabled/);
    assert.match(fullOnly, /name="hub_addresses"/);
    view.plotForm({include_fees_present: '1', ...(includeFees ? {include_fees: 'on'} : {})});
    await view.dispatch(action);
    const writes = view.calls.filter(call => call.body);
    assert.equal(writes.length, 1);
    assert.equal(writes[0].path, '/api/cases/case1/actions');
    assert.equal(writes[0].body.action, action === 'workflow-plot' ? 'plot' : 'plot-sync');
    assert.equal(writes[0].body.goal, goal);
    assert.equal(writes[0].body.layout_settings.include_fees, includeFees);
    assert.deepEqual(writes[0].body.layout_settings.hub_addresses, hubs);
    assert.equal(detail.run_defaults.include_fees, !includeFees, 'generation keeps saved defaults intact');
  }
});

test('saved connection and peg-out fee visibility follows each snapshot across plots, downloads and boards', async () => {
  for (const goal of ['connections', 'pegouts']) for (const includeFees of [false, true]) {
    const view = await harness();
    const plot = workflowPlot(goal, 'fee-snapshot', {query: {transaction_io: 'complete'},
      layout_settings: {...layoutDefaults, include_fees: includeFees}, match_count: 2});
    view.state.activeCase = workflowCase({run_defaults: {...defaults, include_fees: !includeFees}, plots: [plot],
      boards: [workflowBoard(goal, 'fee-board', {preview_id: plot.preview_id})]});
    for (const page of ['plots', 'history', 'boards']) {
      await view.dispatch('view-' + page);
      const html = view.workspace();
      const expected = `All transaction inputs and ${includeFees ? 'outputs' : 'non-fee outputs'} · isolated inputs separate`;
      assert.ok(html.includes(`Saved layout: ${expected}.`));
      const picker = html.match(/<select id="workflow-(?:plot-picker|board-plot-[^"\s]+)"[^>]*>[\s\S]*?<\/select>/)?.[0];
      assert.ok(picker.includes(expected));
      if (goal === 'pegouts') assert.match(html, /Results: 2 peg-out requests/);
      if (page === 'plots') {
        const summary = html.match(/<details class="tool-details"><summary>Saved layout settings<\/summary>[\s\S]*?<\/details>/)?.[0];
        assert.match(summary, new RegExp(`Fee flows ${includeFees ? 'shown' : 'hidden'}\\.`));
        if (goal === 'pegouts') assert.match(summary, /0 separate branch hubs/);
      }
    }
    assert.equal(view.calls.length, 1, 'viewing saved settings does not regenerate or update a plot');
  }
});

test('Starter connections captures context grouping, permits fee opt-in and keeps hubs disabled', async () => {
  for (const grouped of [true, false]) {
    const view = await harness(path => path.endsWith('/actions') ? {id: 'plotjob', status: 'running'} : undefined);
    view.state.activeCase = workflowCase({run_defaults: {...defaults, group_context_inputs: !grouped}});
    await view.dispatch('view-plots');
    await view.dispatch('plot-goal', {dataset: {goal: 'connections'}});
    const html = view.workspace();
    assert.match(html, /Every included transaction shows all its inputs and non-fee outputs/);
    assert.match(html, /Outputs on excluded branches remain visible without continuing those branches/);
    assert.match(html, /The transaction CSV follows the same fee choice; extra context does not add starter-pair matches/);
    assert.match(html, /Starter connection layouts show every transaction input and non-fee output/);
    assert.doesNotMatch(html, /id="workflow-include-context"|id="workflow-include-unspent"/);
    assert.doesNotMatch(contextGroupingFields(view), /disabled/);
    const fullOnly = html.match(/<fieldset class="layout-fields full-trace-fields"[^>]*>[\s\S]*?<\/fieldset>/)?.[0];
    assert.match(fullOnly, /disabled/);
    assert.match(fullOnly, /name="hub_addresses"/);
    assert.doesNotMatch(fullOnly, /name="include_fees"/);
    const fees = html.match(/<fieldset class="layout-fields fee-flow-fields"[^>]*>[\s\S]*?<\/fieldset>/)?.[0];
    assert.doesNotMatch(fees, /disabled/);
    assert.match(fees, /name="include_fees"/);
    view.plotForm({group_context_inputs_present: '1', ...(grouped ? {group_context_inputs: 'on'} : {})});
    await view.dispatch('workflow-plot');
    assert.deepEqual(view.calls.at(-1).body, {action: 'plot', layout_mode: 'fresh', goal: 'connections', run_id: 'saved1',
      min_hops: 0, max_hops: 10, connection_scope: 'hop_limited', layout_settings: {...layoutDefaults, group_context_inputs: grouped}});
    assert.equal(view.calls.filter(call => call.body).length, 1, 'plotting does not collect or mutate preferences');
  }
});

test('saved Starter layouts distinguish complete transaction context from legacy paths across plots, downloads and boards', async () => {
  for (const complete of [true, false]) {
    const view = await harness();
    const plot = workflowPlot('connections', 'starter-context', {max_hops: null,
      query: {connection_scope: 'all_saved', ...(complete ? {transaction_io: 'complete'} : {})},
      layout_settings: {...layoutDefaults, group_context_inputs: true, include_fees: complete},
      connection_count: 2, ...(complete ? {context_edge_count: 9} : {})});
    view.state.activeCase = workflowCase({plots: [plot],
      boards: [workflowBoard('connections', 'starter-board', {preview_id: plot.preview_id})]});
    await view.dispatch('view-plots');
    await view.dispatch('plot-goal', {dataset: {goal: 'connections'}});
    for (const page of ['plots', 'history', 'boards']) {
      await view.dispatch('view-' + page);
      const html = view.workspace();
      assert.match(html, /All verified saved connections/);
      assert.match(html, complete
        ? /Saved layout: All transaction inputs and outputs · isolated inputs grouped\. 9 context connections, excluded from starter-pair counts\./
        : /Saved layout: Paths only\./);
      const picker = html.match(/<select id="workflow-(?:plot-picker|board-plot-[^"\s]+)"[^>]*>[\s\S]*?<\/select>/)?.[0];
      assert.match(picker, complete ? /All transaction inputs and outputs · isolated inputs grouped/ : /Paths only/);
      assert.doesNotMatch(html, /undefined context connections|NaN context connections/);
      if (page === 'plots') {
        const summary = html.match(/<details class="tool-details"><summary>Saved layout settings<\/summary>[\s\S]*?<\/details>/)?.[0];
        if (complete) assert.match(summary, /Isolated context inputs grouped/);
        else assert.doesNotMatch(summary, /Isolated context inputs/);
      }
    }
    assert.equal(view.calls.length, 1, 'new defaults do not rewrite saved layouts');
  }
});

test('peg-out generation captures grouped and separate context preferences for each job', async () => {
  for (const grouped of [true, false]) {
    const detail = workflowCase({run_defaults: {...defaults, group_context_inputs: !grouped}});
    const view = await harness((path, body) => {
      if (path.endsWith('/plot-settings')) {
        detail.run_defaults = {...detail.run_defaults, ...body.settings};
        return detail;
      }
      if (path.endsWith('/actions')) return {id: 'plotjob', status: 'running'};
    });
    view.state.activeCase = detail;
    await view.dispatch('view-plots');
    await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
    view.plotForm({group_context_inputs_present: '1', ...(grouped ? {group_context_inputs: 'on'} : {})});
    await view.dispatch('workflow-plot');
    const writes = view.calls.filter(call => call.body);
    assert.deepEqual(writes.map(call => call.path), ['/api/cases/case1/actions']);
    assert.deepEqual(writes[0].body, {action: 'plot', layout_mode: 'fresh', goal: 'pegouts', run_id: 'saved1', min_hops: 0,
      max_hops: 10, hop_basis: 'original_seeds', include_unspent: true, include_unspendable: true, include_attributed_stops: true, layout_settings: {...layoutDefaults, group_context_inputs: grouped}});
    assert.equal(view.workflowInput({id: 'workflow-include-context', checked: false}), false);
  }
});

test('saved context grouping labels use each plot snapshot across plot, download, and Miro board views', async () => {
  for (const grouped of [true, false]) {
    const view = await harness();
    const layout_settings = {layout_attempts: 25, connector_style: 'curved', include_fees: false,
      color_attribution_arrows: false, center_name: '', hub_addresses: [], group_context_inputs: grouped};
    const plot = workflowPlot('pegouts', 'saved-context', {query: {include_context: true}, layout_settings});
    view.state.activeCase = workflowCase({run_defaults: {...defaults, group_context_inputs: !grouped}, plots: [plot],
      boards: [workflowBoard('pegouts', 'context-board', {preview_id: plot.preview_id})]});
    await view.dispatch('view-plots');
    await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
    for (const page of ['plots', 'history', 'boards']) {
      await view.dispatch('view-' + page);
      const html = view.workspace(), expected = grouped ? 'grouped' : 'separate';
      assert.match(html, new RegExp(`Saved layout: Context addresses included · isolated inputs ${expected}\\.`));
      const picker = html.match(/<select id="workflow-(?:plot-picker|board-plot-[^"\s]+)"[^>]*>[\s\S]*?<\/select>/)?.[0];
      assert.match(picker, new RegExp(`Context addresses included · isolated inputs ${expected}`));
      if (page === 'plots') assert.match(html, new RegExp(`Isolated context inputs ${expected}\\.`));
      if (page === 'boards') assert.match(boardCardHtml(view, 'context-board'),
        new RegExp(`Context addresses included · isolated inputs ${expected}`));
    }
    assert.equal(view.calls.length, 1, 'different draft settings do not rewrite or regenerate the saved plot');
  }
});

test('collection and new peg-out controls explain attribution limits without changing global hop controls', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase();
  await view.dispatch('view-collect');
  assert.match(view.workspace(), /The collection hop ceiling, explicit tracing stops and any enabled run budgets apply/);
  assert.match(view.workspace(), /Attribution CSV hop_limit values are ignored, including zero/);
  assert.match(view.workspace(), /0 additional hops fills eligible gaps within its existing ceiling/);
  await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  assert.match(view.workspace(), /Endpoint paths ignore attribution CSV hop_limit values/);
  assert.match(view.workspace(), /Explicit stop-tracing rules and the selected hop range still apply/);
  assert.match(view.workspace(), /id="workflow-max-hops"[^>]*value="10"/);
});

test('saved peg-out scope reports the captured attribution policy for old and new layouts', async () => {
  for (const ignore of [false, true]) {
    const view = await harness();
    const plot = workflowPlot('pegouts', 'policy', {query: {transaction_io: 'complete',
      ...(ignore ? {attribution_hop_limits: 'ignore'} : {})}});
    view.state.activeCase = workflowCase({plots: [plot],
      boards: [workflowBoard('pegouts', 'policy-board', {preview_id: plot.preview_id})]});
    for (const page of ['plots', 'history', 'boards']) {
      await view.dispatch('view-' + page);
      assert.match(view.workspace(), ignore
        ? /Attribution hop limits ignored; explicit stop-tracing rules and the selected hop range applied/
        : /Attribution hop limits, stop-tracing rules and the selected hop range applied/);
    }
  }
});

test('complete I/O saved scope appears across plots, downloads and boards without changing endpoint counts', async () => {
  const view = await harness();
  const plot = workflowPlot('pegouts', 'complete', {query: {transaction_io: 'complete'},
    layout_settings: {...layoutDefaults, group_context_inputs: true, include_fees: true},
    match_count: 2, endpoint_count: 2, context_edge_count: 9});
  view.state.activeCase = workflowCase({plots: [plot],
    boards: [workflowBoard('pegouts', 'complete-board', {preview_id: plot.preview_id})]});
  for (const page of ['plots', 'history', 'boards']) {
    await view.dispatch('view-' + page);
    const html = view.workspace();
    assert.match(html, /Saved layout: All transaction inputs and outputs · isolated inputs grouped\. 9 context connections, excluded from endpoint counts\./);
    assert.match(html, /Results: 2 peg-out requests\./);
    const picker = html.match(/<select id="workflow-(?:plot-picker|board-plot-[^"\s]+)"[^>]*>[\s\S]*?<\/select>/)?.[0];
    assert.match(picker, /All transaction inputs and outputs · isolated inputs grouped/);
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
    assert.match(html, /Saved layout: Context addresses included · isolated inputs separate\. 5 context connections, excluded from endpoint counts\./);
    assert.match(html, /Results: 2 peg-out requests\./);
    const picker = html.match(/<select id="workflow-(?:plot-picker|board-plot-[^"\s]+)"[^>]*>[\s\S]*?<\/select>/)?.[0];
    assert.ok(picker);
    assert.match(picker, /value="contextual"[^>]*>[^<]*Peg-outs · 2 endpoints · Context addresses included/);
    assert.match(picker, /value="paths"[^>]*>[^<]*Peg-outs · 2 endpoints · Paths only/);
  }
  assert.match(view.workspace(), /<article class="board-card[^>]*>[\s\S]*?Context addresses included/);
  await view.dispatch('workflow-board-sync', boardControl('context-board'));
  assert.deepEqual(view.calls.at(-1).body, {action: 'board-sync', record_id: 'context-board', preview_id: 'contextual', reorganize: false});
});

test('complete I/O defaults do not rewrite the scope of a selected legacy layout', async () => {
  for (const savedContext of [false, true]) {
    const view = await harness();
    view.state.activeCase = workflowCase({plots: [workflowPlot('pegouts', 'saved', {
      ...(savedContext ? {query: {include_context: true}} : {}), match_count: 2})]});
    await view.dispatch('view-plots');
    await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
    assert.doesNotMatch(view.workspace(), /id="workflow-include-context"/);
    for (const page of ['plots', 'history']) {
      await view.dispatch('view-' + page);
      const html = view.workspace();
      assert.match(html, new RegExp(`Saved layout: ${savedContext ? 'Context addresses included · isolated inputs separate' : 'Paths only'}\\.`));
      assert.doesNotMatch(html, new RegExp(`Saved layout: ${savedContext ? 'Paths only' : 'Context addresses included · isolated inputs separate'}\\.`));
      assert.doesNotMatch(html, /undefined context connections|NaN context connections/);
    }
    assert.equal(view.calls.length, 1, 'new display defaults neither regenerate nor update saved plots');
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
    const picker = html.match(/<select id="workflow-(?:plot-picker|board-plot-[^"\s]+)"[^>]*>[\s\S]*?<\/select>/)?.[0];
    assert.ok(picker);
    assert.match(picker, /value="terminal"[^>]*>[^<]*Peg-outs \+ unspent UTXOs \+ unspendable outputs · 3 endpoints/);
    assert.match(picker, /value="earlier"[^>]*>[^<]*Peg-outs · 2 endpoints/);
  }
  assert.doesNotMatch(view.workspace().match(/<button[^>]*data-action="workflow-board-sync"[^>]*>/)[0], /disabled/);
  await view.dispatch('workflow-board-sync', boardControl('terminal-board'));
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

const pegoutCsvArtifact = (id, {oldFiles = false} = {}) => ({preview_id: id,
  downloads: [
    {name: 'transactions.csv', url: `/api/cases/case1/plot-exports/${id}/transactions.csv`},
    {name: 'endpoints.csv', url: `/api/cases/case1/plot-exports/${id}/endpoints.csv`},
    {name: 'graph.svg', url: `/files/case1/previews/${id}/graph.svg`},
    ...(oldFiles ? ['path-transactions.csv', 'trace-endpoints.csv'].map(name => ({name,
      url: `/files/case1/previews/${id}/${name}`})) : []),
  ]});

test('saved peg-out paths show exactly full accounting and endpoint table CSVs in plots and history', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({plots: [workflowPlot('pegouts', 'terminal', {
    query: {include_unspent: true, include_unspendable: true, include_context: true},
    artifact: pegoutCsvArtifact('terminal', {oldFiles: true}),
  })]});
  for (const page of ['plots', 'history']) {
    await view.dispatch('view-' + page);
    const html = view.workspace();
    assert.match(html, /href="\/api\/cases\/case1\/plot-exports\/terminal\/transactions.csv"[^>]*>[\s\S]*?All trace transactions CSV<\/a>/);
    assert.match(html, /href="\/api\/cases\/case1\/plot-exports\/terminal\/endpoints.csv"[^>]*>[\s\S]*?Endpoints only CSV<\/a>/);
    assert.match(html, /full input\/output accounting for this saved trace/);
    assert.match(html, /ending-output table with source, source value, deposit\/peg-out transaction, address, receiving entity, status, and peg-out LBTC/);
    assert.match(html, /Unspent outputs are labeled Dormant/);
    assert.doesNotMatch(html, /Generate this plot again to add|One row per transaction|download="(?:path-transactions|trace-endpoints).csv"/);
    assert.equal((html.match(/href="(?:\/files\/case1\/previews\/terminal\/|\/api\/cases\/case1\/plot-exports\/terminal\/)[^"]*\.csv"/g) || []).length, 2);
    if (page === 'history') {
      assert.doesNotMatch(html, /Plot inputs and outputs CSV/);
      assert.match(html, /Full-run CSV downloads/);
      assert.match(html, /selected collection run, across its full trace/);
    }
  }
  assert.equal(view.calls.length, 1, 'rendering exports does not regenerate or refetch the saved plot');
});

test('older saved peg-out plots get endpoint download from the API without new artifact files or regeneration', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({plots: [workflowPlot('pegouts', 'old', {
    artifact: pegoutCsvArtifact('old'),
  })]});
  for (const page of ['plots', 'history']) {
    await view.dispatch('view-' + page);
    const html = view.workspace();
    assert.match(html, /href="\/api\/cases\/case1\/plot-exports\/old\/endpoints.csv"/);
    assert.match(html, /href="\/api\/cases\/case1\/plot-exports\/old\/transactions.csv"/);
    assert.doesNotMatch(html, /Generate this plot again to add/);
    if (page === 'plots') assert.doesNotMatch(html.match(/<button[^>]*data-action="plot-boards"[^>]*>/)[0], /disabled/);
  }
});

test('CSV choices follow selected peg-out plot and stay off other plotting goals', async () => {
  const view = await harness();
  const terminal = workflowPlot('pegouts', 'terminal', {artifact: pegoutCsvArtifact('terminal')});
  view.state.activeCase = workflowCase({plots: [terminal, workflowPlot('full', 'full'), workflowPlot('connections', 'connections')]});
  for (const goal of ['full', 'connections']) {
    workflowEdit(view, 'plot-picker', goal);
    for (const page of ['plots', 'history']) {
      await view.dispatch('view-' + page);
      assert.doesNotMatch(view.workspace(), /All trace transactions CSV|Endpoints only CSV/);
    }
  }
  workflowEdit(view, 'plot-picker', 'terminal');
  assert.match(view.workspace(), /download="transactions.csv"/);
  assert.match(view.workspace(), /download="endpoints.csv"/);
});

test('changed settings block syncing but retain both CSV downloads from the valid saved archive', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({plots: [workflowPlot('pegouts', 'earlier', {
    reviewable: false, reason: 'Settings changed.', artifact: pegoutCsvArtifact('earlier'),
  })]});
  for (const page of ['plots', 'history']) {
    await view.dispatch('view-' + page);
    const html = view.workspace();
    assert.match(html, /download="transactions.csv"/);
    assert.match(html, /download="endpoints.csv"/);
    assert.doesNotMatch(html, /Generate this plot again to add|syncing remain available/);
    if (page === 'plots') assert.match(html.match(/<button[^>]*data-action="workflow-write-miro"[^>]*>/)[0], /disabled/);
  }
});

test('missing saved artifacts do not invent CSV download links', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({plots: [workflowPlot('pegouts', 'missing', {
    reviewable: false, reason: 'Saved layout files are unavailable.', artifact: undefined,
  })]});
  for (const page of ['plots', 'history']) {
    await view.dispatch('view-' + page);
    const html = view.workspace();
    assert.doesNotMatch(html, /All trace transactions CSV|Endpoints only CSV|Generate this plot again to add/);
    assert.match(html, /Saved layout files are unavailable/);
  }
});

test('legacy standalone peg-out searches expose full accounting and dynamic endpoint table exports', async () => {
  const view = await harness();
  const id = '1'.repeat(16) + '-pegouts-12345678';
  view.state.activeCase = pegoutCase([pegoutSearch({artifact: pegoutCsvArtifact(id)})]);
  const html = view.pegoutsGraph(view.state.activeCase);
  assert.match(html, /download="transactions.csv"[^>]*>[\s\S]*?All trace transactions CSV<\/a>/);
  assert.match(html, /download="endpoints.csv"[^>]*>[\s\S]*?Endpoints only CSV<\/a>/);
  assert.match(html, new RegExp(`href="/api/cases/case1/plot-exports/${id}/endpoints.csv"`));
  assert.equal((html.match(/download="[^"]*\.csv"/g) || []).length, 2);
  assert.doesNotMatch(html, /Generate this plot again to add|>Transaction CSV<\/a>/);
});

test('endpoint download links permit only local saved files and the dedicated CSV export route', async () => {
  const view = await harness();
  for (const url of ['https://example.com/endpoints.csv', '//example.com/endpoints.csv',
    '/api/cases/case1/actions', '/api/cases/case1/plot-exports/terminal/endpoints.csv?redirect=other',
    '/api/cases/case1/plot-exports/terminal/endpoints.csv\n']) {
    view.state.activeCase = workflowCase({plots: [workflowPlot('pegouts', 'terminal', {
      artifact: {downloads: [{name: 'endpoints.csv', url}]},
    })]});
    await view.dispatch('view-history');
    assert.doesNotMatch(view.workspace(), /download="endpoints.csv"/);
  }
});

test('central board manager links existing boards before collection for each goal', async () => {
  for (const goal of ['full', 'connections', 'pegouts']) {
    for (const method of ['link']) {
      const view = await harness(path => path.endsWith('/actions') ? {id: 'boardjob', status: 'running'} : undefined);
      view.state.activeCase = workflowCase({runs: [], latest_run: undefined});
      await view.dispatch('view-boards');
      assert.match(view.workspace(), /No boards yet/);
      workflowEdit(view, 'board-goal', goal); workflowEdit(view, 'link-board-name', 'My board');
      workflowEdit(view, 'board-url', 'https://miro.com/app/board/target=/');
      await view.dispatch('workflow-board-' + method);
      assert.deepEqual(view.calls.at(-1).body, {action: 'board-' + method, goal, name: 'My board',
        ...(method === 'link' ? {board: 'https://miro.com/app/board/target=/'} : {})});
      assert.equal(view.state.job.live, method === 'create');
    }
  }
});

test('each persistent card pins its board and matching plot without changing full-trace board', async () => {
  for (const reorganize of [false, true]) {
    const view = await harness(path => path.endsWith('/actions') ? {id: 'syncjob', status: 'running'} : undefined);
    view.state.activeCase = workflowCase({miro_board: 'original-full',
      plots: [workflowPlot('full', 'fullplot'), workflowPlot('pegouts', 'pegplot')],
      boards: [workflowBoard('full', 'fullboard'), workflowBoard('pegouts', 'pegboard')]});
    await view.dispatch('view-boards');
    const html = boardCardHtml(view, 'pegboard');
    assert.ok(boardCardHtml(view, 'fullboard'), 'other board stays visible');
    assert.match(html, /miro-pegboard/);
    assert.match(html, /value="pegplot"/);
    assert.doesNotMatch(html, /value="fullplot"|data-action="miro-frames-dialog"|data-action="miro-sync-dialog"/);
    await view.dispatch(reorganize ? 'workflow-board-organize' : 'workflow-board-sync', boardControl('pegboard'));
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
  await assert.rejects(view.dispatch('workflow-board-sync', boardControl('managed')), /matching saved plot/);
  assert.doesNotMatch(boardCardHtml(view, 'old'), /data-action="workflow-board-sync"/);
  view.state.job = {id: 'busy', action: 'board-sync'};
  await view.dispatch('workflow-board-create');
  assert.equal(view.calls.length, 1);
});

test('interrupted board creation links the known Miro board to its original registry entry', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'linkjob', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({boards: [workflowBoard('pegouts', 'pending', {board_id: null, status: 'pending_creation', can_sync: false})]});
  await view.dispatch('view-boards');
  assert.match(view.workspace(), /Link created board to this entry/);
  boardEdit(view, 'pending', 'recoveryUrl', 'created-board');
  await view.dispatch('workflow-board-recover', boardControl('pending'));
  assert.deepEqual(view.calls.at(-1).body, {action: 'board-link', record_id: 'pending', goal: 'pegouts', name: 'pegouts board', board: 'created-board'});
});

test('job completion selects the generated plot or initialized board from the refreshed API response', async () => {
  for (const action of ['plot', 'board-create', 'board-create-sync', 'board-link']) {
    const plot = workflowPlot('pegouts', 'newplot'), board = workflowBoard('pegouts', 'newboard');
    const detail = workflowCase({plots: [plot], boards: [board]});
    const view = await harness(path => path === '/api/jobs/job' ? {status: 'succeeded', result: action === 'plot' ? plot : board}
      : path === '/api/cases/case1/overview' ? detail : undefined);
    view.state.activeCase = workflowCase();
    view.state.job = {id: 'job', action, caseId: 'case1'};
    await view.pollJob();
    assert.equal(view.state.caseView, 'plots');
    assert.equal(view.currentWorkflow(detail)[action === 'plot' ? 'plot' : 'board'], action === 'plot' ? 'newplot' : 'newboard');
  }
});

test('real populated case-detail contract feeds fresh previews into create and sync, while linked boards require updates', async () => {
  const details = JSON.parse(execFileSync('python3', ['-m', 'web.tests.case_detail_fixture'], {
    cwd: new URL('../../', import.meta.url), encoding: 'utf8', timeout: 15000,
  }));
  for (const detail of Object.values(details)) {
    assert.ok(Array.isArray(detail.plots)); assert.ok(Array.isArray(detail.boards));
  }
  const detail = details.workflow;
  assert.equal(detail.plots.length, 1); assert.equal(detail.boards.length, 1);
  const view = await harness(path => path === `/api/cases/${detail.id}/overview` ? detail
    : path.endsWith('/actions') ? {id: 'syncjob', status: 'running'} : undefined);
  await view.dispatch('open-case', {dataset: {id: detail.id}});
  await view.dispatch('view-plots');
  assert.doesNotMatch(savedPreviewPanel(view), /<iframe/);
  assert.match(savedPreviewPanel(view), /Open preview/);
  assert.match(view.workspace(), /Paths to endpoints/);
  assert.match(view.workspace(), /Collection hop limit: 10/);
  await view.dispatch('view-boards');
  assert.match(view.workspace(), /Peg-out case board/);
  assert.match(view.workspace().match(/<button[^>]*data-action="workflow-board-sync"[^>]*>/)[0], /disabled/);
  assert.equal(detail.plots[0].layout_mode, 'fresh');
  await view.dispatch('workflow-board-prepare', boardControl(detail.boards[0].id, detail.id));
  assert.equal(view.currentWorkflow(detail).layoutBoard, detail.boards[0].id);
  assert.equal(view.currentWorkflow(detail).layoutMode, 'update');
  await view.dispatch('view-boards');
  await view.dispatch('workflow-board-create-sync');
  assert.deepEqual(view.calls.at(-1).body, {action: 'board-create-sync',
    preview_id: detail.plots[0].preview_id, name: detail.name + ' · Paths to endpoints'});
});

test('interrupted sync permits its saved plot retry and excludes newer plots until recovery completes', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'retry', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({plots: [workflowPlot('pegouts', 'newer'), workflowPlot('pegouts', 'saved')],
    boards: [workflowBoard('pegouts', 'interrupted', {pending_count: 1, preview_id: 'saved', status: 'interrupted'})]});
  await view.dispatch('view-boards');
  assert.match(view.workspace(), /value="saved"/);
  assert.doesNotMatch(boardCardHtml(view, 'interrupted'), /value="newer"/);
  await view.dispatch('workflow-board-sync', boardControl('interrupted'));
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
  await assert.rejects(view.dispatch('workflow-board-sync', boardControl('managed')), /matching saved plot/);
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /Plot access temporarily blocked/);
  assert.match(view.workspace(), /No matching paths were found/);
});

test('settings direct board management to the central tab and preserve the original full-board binding', async () => {
  const detail = workflowCase({miro_board: 'original-full'});
  const view = await harness(path => path === '/api/cases/case1/overview' ? detail : undefined);
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
  assert.match(html, /Choose Miro board/);
  assert.match(html, /Download ELK SVG/);
  assert.match(html, /href="\/files\/case1\/previews\/reviewed\/graph.svg"/);
  await view.dispatch('plot-boards');
  assert.equal(view.state.caseView, 'plots');
  assert.equal(view.currentWorkflow(view.state.activeCase).boardPlot, 'reviewed');
  assert.doesNotMatch(view.workspace(), /Generate matching plot|data-action="board-plot-goal"/);
  assert.match(boardCardHtml(view, 'cashouts'), /value="reviewed" selected/);
  assert.match(boardCardHtml(view, 'second-cashouts'), /value="newer" selected/);
  boardEdit(view, 'second-cashouts', 'plot', 'reviewed');
  assert.equal(view.calls.length, 1, 'choosing a destination reuses the generated plot without requests');
  await view.dispatch('workflow-board-sync', boardControl('second-cashouts'));
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

test('collected data shows actual snapshot hops separately from the configured collection limit', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({latest_run: 'latest', runs: [
    {id: 'latest', status: 'paused', stop_reason: 'transaction_limit', max_hops: 10, collected_hops: 4},
    {id: 'earlier', status: 'bounded_complete', max_hops: 2, collected_hops: 1},
    {id: 'seed', status: 'bounded_complete', max_hops: 0, collected_hops: 0},
  ]});
  for (const [id, collected, limit] of [['latest', 4, 10], ['earlier', 1, 2], ['seed', 0, 0]]) {
    view.state.selectedRun = id;
    for (const page of ['collect', 'plots', 'boards', 'history']) {
      await view.dispatch('view-' + page);
      const html = view.workspace();
      assert.match(html, new RegExp(`<span>Hops collected</span><strong>${collected}</strong>`));
      assert.match(html, new RegExp(`Collection hop limit: ${limit}`));
      assert.match(html, /Deepest saved transaction hop/);
      assert.match(html, /Starting transactions are hop 0; individual branches may stop earlier/);
    }
  }
  assert.equal(view.calls.length, 1, 'viewing hop counts does not fetch or plot data');
});

test('unknown collected depth is not replaced by the requested limit and new cases have no collected metric', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({runs: [{id: 'saved1', status: 'saved', max_hops: 10}]});
  const html = view.workspace();
  assert.match(html, /<span>Hops collected<\/span><strong class="text-value">Not recorded<\/strong>/);
  assert.match(html, /Collection hop limit: 10/);
  view.state.activeCase = workflowCase({latest_run: undefined, runs: []});
  assert.match(view.workspace(), /Ready to collect/);
  assert.doesNotMatch(view.workspace(), /<span>Hops collected<\/span>/);
});

test('same-goal cards keep independent saved layouts and drafts across tabs, refreshes, and investigations', async () => {
  const detail = workflowCase({plots: [workflowPlot('pegouts', 'newest', {input_snapshot_version: 1}), workflowPlot('pegouts', 'first-saved', {input_snapshot_version: 1}), workflowPlot('pegouts', 'second-saved', {input_snapshot_version: 1})],
    boards: [workflowBoard('pegouts', 'first', {preview_id: 'first-saved'}), workflowBoard('pegouts', 'second', {preview_id: 'second-saved'})]});
  const view = await harness(path => path.endsWith('/actions') ? {id: 'syncjob', status: 'running'} : undefined);
  view.state.activeCase = detail;
  await view.dispatch('view-boards');
  assert.equal((view.workspace().match(/data-board-record=/g) || []).length, 2);
  for (const id of ['first', 'second']) {
    const card = boardCardHtml(view, id);
    assert.match(card, new RegExp(`value="${id}-saved" selected`));
    assert.match(card, new RegExp(`href="https://miro.com/app/board/miro-${id}/"`));
    assert.match(card, new RegExp(`data-action="workflow-board-sync"[^>]*data-record="${id}"[^>]*data-case-id="case1"`));
  }
  boardEdit(view, 'first', 'plot', 'newest');
  await view.dispatch('view-plots');
  await view.dispatch('view-boards');
  view.state.activeCase = structuredClone(detail);
  assert.match(boardCardHtml(view, 'first'), /value="newest" selected/);
  assert.match(boardCardHtml(view, 'second'), /value="second-saved" selected/);
  view.state.activeCase = workflowCase({...detail, id: 'case2'});
  assert.match(boardCardHtml(view, 'first'), /value="first-saved" selected/);
  assert.equal(boardEdit(view, 'first', 'plot', 'newest'), false, 'old case controls cannot change another case');
  await assert.rejects(view.dispatch('workflow-board-sync', boardControl('first')), /matching saved plot/);
  view.state.activeCase = detail;
  assert.match(boardCardHtml(view, 'first'), /value="newest" selected/);
  await view.dispatch('workflow-board-sync', boardControl('second'));
  assert.deepEqual(view.calls.at(-1).body, {action: 'board-sync', record_id: 'second', preview_id: 'second-saved', reorganize: false});
  const count = view.calls.length;
  assert.equal(boardEdit(view, 'first', 'plot', 'first-saved'), true);
  assert.doesNotMatch(boardCardHtml(view, 'first'), /data-action="workflow-board-sync"[^>]*disabled/);
  assert.match(boardCardHtml(view, 'second'), /data-action="workflow-board-sync"[^>]*disabled/);
  await view.dispatch('workflow-board-organize', boardControl('first'));
  assert.equal(view.calls.length, count + 1, 'a different board can start while work is in progress');
  assert.deepEqual(view.calls.at(-1).body, {action: 'board-sync', record_id: 'first', preview_id: 'first-saved', reorganize: true});
  const reloaded = await harness();
  reloaded.state.activeCase = detail;
  await reloaded.dispatch('view-boards');
  assert.match(boardCardHtml(reloaded, 'first'), /value="first-saved" selected/, 'reload restores each saved board mapping');
  assert.match(boardCardHtml(reloaded, 'second'), /value="second-saved" selected/);
});

test('new API null previews default to an eligible plot while unavailable saved or explicit choices fail closed', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({plots: [workflowPlot('pegouts', 'newest'), workflowPlot('pegouts', 'stale', {reviewable: false})],
    boards: [workflowBoard('pegouts', 'new', {preview_id: null}), workflowBoard('pegouts', 'saved', {preview_id: 'stale'})]});
  await view.dispatch('view-boards');
  assert.match(boardCardHtml(view, 'new'), /value="newest" selected/);
  assert.match(boardCardHtml(view, 'saved'), /data-action="workflow-board-sync"[^>]*disabled/);
  await assert.rejects(view.dispatch('workflow-board-sync', boardControl('saved')), /matching saved plot/);
  boardEdit(view, 'new', 'plot', 'deleted');
  assert.doesNotMatch(boardCardHtml(view, 'new'), /value="newest" selected/);
  await assert.rejects(view.dispatch('workflow-board-sync', boardControl('new')), /matching saved plot/);
  for (const control of [undefined, boardControl('missing'), boardControl('new', 'wrong-case')]) {
    await assert.rejects(view.dispatch('workflow-board-sync', control), /matching saved plot/);
  }
  assert.equal(view.calls.length, 1);
});

test('pending creation recovery fields and actions belong to their own persistent cards', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'recovery', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({boards: ['first', 'second'].map(id => workflowBoard('pegouts', id,
    {board_id: null, board_url: null, preview_id: null, status: 'pending_creation', can_sync: false}))});
  await view.dispatch('view-boards');
  boardEdit(view, 'first', 'recoveryUrl', 'miro-first');
  boardEdit(view, 'second', 'recoveryUrl', 'miro-second');
  await view.dispatch('view-history');
  await view.dispatch('view-boards');
  assert.match(boardCardHtml(view, 'first'), /value="miro-first"/);
  assert.match(boardCardHtml(view, 'second'), /value="miro-second"/);
  for (const control of [undefined, boardControl('missing'), boardControl('first', 'wrong-case')]) {
    await assert.rejects(view.dispatch('workflow-board-recover', control), /board entry from this investigation/);
  }
  await view.dispatch('workflow-board-recover', boardControl('second'));
  assert.deepEqual(view.calls.at(-1).body, {action: 'board-link', record_id: 'second', goal: 'pegouts', name: 'pegouts board', board: 'miro-second'});
  const count = view.calls.length;
  await view.dispatch('workflow-board-recover', boardControl('first'));
  assert.equal(view.calls.length, count);
  assert.equal(boardEdit(view, 'first', 'recoveryUrl', 'changed'), false);
});

test('interrupted status without pending items locks a previous card draft to its saved retry plot', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'retry', status: 'running'} : undefined);
  const board = workflowBoard('pegouts', 'interrupted', {preview_id: 'saved'});
  view.state.activeCase = workflowCase({plots: [workflowPlot('pegouts', 'newer'), workflowPlot('pegouts', 'saved')], boards: [board]});
  await view.dispatch('view-boards');
  boardEdit(view, board.id, 'plot', 'newer');
  board.status = 'interrupted'; board.pending_count = 0;
  const card = boardCardHtml(view, board.id);
  assert.match(card, /Saved layout to resume/);
  assert.match(card, /data-board-field="plot"[^>]*disabled/);
  assert.match(card, /value="saved" selected/);
  assert.doesNotMatch(card, /value="newer"/);
  assert.equal(boardEdit(view, board.id, 'plot', 'newer'), false);
  await view.dispatch('workflow-board-sync', boardControl(board.id));
  assert.equal(view.calls.at(-1).body.preview_id, 'saved');
});

test('failed and canceled managed-board jobs refresh persisted cards without losing the failure or changing cases', async () => {
  for (const action of ['board-create', 'board-create-sync', 'board-link', 'board-sync']) {
    for (const status of ['failed', 'canceled']) {
      const pending = workflowBoard('pegouts', 'persisted', {board_id: action === 'board-sync' ? 'linked-board' : null,
        status: action === 'board-sync' ? 'interrupted' : 'pending_creation', can_sync: action === 'board-sync'});
      const updated = workflowCase({boards: [pending]});
      const view = await harness(path => path === '/api/jobs/job' ? {status, message: 'Original operation message'} : path === '/api/cases/case1/overview' ? updated : undefined);
      view.state.activeCase = workflowCase(); view.state.caseView = 'boards';
      view.state.job = {id: 'job', action, caseId: 'case1'};
      await view.pollJob();
      assert.ok(boardCardHtml(view, 'persisted'));
      if (status === 'failed') assert.equal(view.state.error, 'Original operation message');
    }
  }
  const failedRefresh = await harness(path => path === '/api/jobs/job' ? {status: 'failed', message: 'Original sync failure'} : path === '/api/cases/case1/overview' ? {error: 'Refresh failed'} : undefined);
  failedRefresh.state.activeCase = workflowCase(); failedRefresh.state.job = {id: 'job', action: 'board-sync', caseId: 'case1'};
  await failedRefresh.pollJob();
  assert.equal(failedRefresh.state.error, 'Original sync failure');
  const switched = await harness(path => path === '/api/jobs/job' ? {status: 'failed', message: 'Previous case failure'} : undefined);
  switched.state.activeCase = workflowCase({id: 'another-case'}); switched.state.job = {id: 'job', action: 'board-create', caseId: 'case1'};
  await switched.pollJob();
  assert.equal(switched.state.activeCase.id, 'another-case');
  assert.equal(switched.calls.filter(call => call.path === '/api/cases/case1/overview').length, 0);
});

function miroConflictReport(overrides = {}) {
  return {kind: 'miro_edit_conflicts', board_id: 'board/with?special&characters', truncated: false,
    items: [{key: 'context:<group>', item_id: 'item/with?special&characters', kind: 'shape',
      object_url: 'javascript:alert("unsafe")', changes: [
        {field: 'data.content', saved: {present: true, value: '<p>Address & notes</p>', type: 'string'},
          current: {present: true, value: '<img src=x onerror="alert(1)">', type: 'string'}},
        {field: 'style.fillColor', saved: {present: true, value: '#ffffff', type: 'string'},
          current: {present: true, value: '#ff0000', type: 'string'}},
        {field: 'data.empty', saved: {present: false}, current: {present: true, value: '', type: 'string'}},
      ], truncated: false}], ...overrides};
}

test('Miro conflict failure keeps escaped saved/current differences through refresh and links directly to the object', async () => {
  const report = miroConflictReport();
  report.items[0].changes.push({field: 'data.longText',
    saved: {present: true, value: '[Excerpt starting at character 100] saved text', truncated: true},
    current: {present: true, value: '[Excerpt starting at character 100] edited text', truncated: true}});
  const view = await harness(path => path === '/api/jobs/job'
    ? {status: 'failed', message: 'Miro objects differ from the last sync.', edit_conflicts: report}
    : path === '/api/cases/case1/overview' ? workflowCase() : undefined);
  view.state.activeCase = workflowCase(); view.state.page = 'case';
  view.state.job = {id: 'job', action: 'plot-sync', caseId: 'case1', progress: {phase: 'syncing'}};
  await view.pollJob();
  assert.match(view.state.error, /Miro objects differ from the last sync/);
  assert.match(view.app.innerHTML, /Locate the Miro changes/);
  assert.match(view.app.innerHTML, /Last synced value/);
  assert.match(view.app.innerHTML, /Current Miro value/);
  assert.match(view.app.innerHTML, /&lt;p&gt;Address &amp; notes&lt;\/p&gt;/);
  assert.match(view.app.innerHTML, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/);
  assert.match(view.app.innerHTML, /context:&lt;group&gt;/);
  assert.match(view.app.innerHTML, /Not set/);
  assert.match(view.app.innerHTML, /Empty string/);
  assert.match(view.app.innerHTML, /Excerpt only; not the complete value/);
  assert.match(view.app.innerHTML, /Use excerpts to locate a change, not to replace the complete object text/);
  assert.match(view.app.innerHTML, /href="https:\/\/miro\.com\/app\/board\/board%2Fwith%3Fspecial%26characters\/\?moveToWidget=item%2Fwith%3Fspecial%26characters" target="_blank" rel="noopener noreferrer"/);
  assert.doesNotMatch(view.app.innerHTML, /<img|javascript:|<group>|<p>Address/);
  assert.match(view.app.innerHTML, /regenerate the board update, and retry/);
  assert.ok(view.calls.some(call => call.path === '/api/cases/case1/overview'));
  view.render();
  assert.match(view.app.innerHTML, /Locate the Miro changes/);
  await view.dispatch('dismiss-error');
  assert.equal(view.state.editConflicts, null);
  assert.doesNotMatch(view.app.innerHTML, /Locate the Miro changes/);
});

test('Miro conflict reports are isolated from other investigations and cleared before a new action', async () => {
  const report = miroConflictReport();
  const view = await harness(path => path === '/api/jobs/job'
    ? {status: 'failed', message: 'Old case conflict.', edit_conflicts: report}
    : path === '/api/cases/case2/overview' ? workflowCase({id: 'case2'})
    : path === '/api/lookup' ? {id: 'new-job', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({id: 'case2'}); view.state.page = 'case';
  view.state.job = {id: 'job', action: 'board-sync', caseId: 'case1'};
  await view.pollJob();
  assert.equal(view.state.editConflicts, null);
  assert.doesNotMatch(view.app.innerHTML, /Locate the Miro changes|context:&lt;group&gt;/);
  view.state.editConflicts = {caseId: 'case1', report};
  view.render();
  assert.doesNotMatch(view.app.innerHTML, /Locate the Miro changes/);
  await view.dispatch('open-case', {dataset: {id: 'case2'}});
  assert.equal(view.state.editConflicts, null);
  view.state.error = 'Previous failure';
  view.state.editConflicts = {caseId: 'case2', report};
  view.state.draft.txids = txid;
  await view.dispatch('lookup');
  assert.equal(view.state.editConflicts, null);
  assert.doesNotMatch(view.app.innerHTML, /Locate the Miro changes/);
});

test('generic or malformed failed-job reports leave the existing error UI intact', async () => {
  for (const edit_conflicts of [undefined, {}, {kind: 'miro_edit_conflicts', board_id: 'board', items: [null, {}]}]) {
    const view = await harness(path => path === '/api/jobs/job'
      ? {status: 'failed', message: 'Original action failure.', edit_conflicts}
      : path === '/api/cases/case1/overview' ? workflowCase() : undefined);
    view.state.activeCase = workflowCase(); view.state.page = 'case';
    view.state.job = {id: 'job', action: 'board-sync', caseId: 'case1'};
    await view.pollJob();
    assert.equal(view.state.error, 'Original action failure.');
    assert.match(view.app.innerHTML, /Unable to complete the action/);
    assert.match(view.app.innerHTML, /Original action failure\./);
    assert.doesNotMatch(view.app.innerHTML, /Locate the Miro changes/);
  }
});

test('original full trace tools stay inside their own board card and archived cards remain viewable', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({miro_board: 'miro-original', boards: [workflowBoard('full', 'original'),
    workflowBoard('full', 'another'), workflowBoard('pegouts', 'archive', {legacy_snapshot: true, can_sync: false})]});
  await view.dispatch('view-boards');
  assert.match(boardCardHtml(view, 'original'), /Original full trace board tools/);
  assert.match(boardCardHtml(view, 'original'), /data-action="miro-frames-dialog"/);
  assert.doesNotMatch(boardCardHtml(view, 'another'), /miro-frames-dialog|miro-rebuild-dialog/);
  assert.match(boardCardHtml(view, 'archive'), /Archived snapshot/);
  assert.match(boardCardHtml(view, 'archive'), /Open Miro board/);
  assert.doesNotMatch(boardCardHtml(view, 'archive'), /workflow-board-sync|data-board-field="plot"/);
});

test('plot handoff names only a compatible card containing the requested layout', async () => {
  const pegout = workflowPlot('pegouts', 'requested'), full = workflowPlot('full', 'fullplot');
  const original = workflowCase({plots: [pegout, full], boards: [workflowBoard('pegouts', 'cashouts')]});
  const created = workflowBoard('full', 'overview');
  const view = await harness(path => path === '/api/jobs/job' ? {status: 'succeeded', result: created}
    : path === '/api/cases/case1/overview' ? {...original, boards: [...original.boards, created]} : undefined);
  view.state.activeCase = original;
  await view.dispatch('view-plots');
  await view.dispatch('plot-boards');
  assert.match(view.workspace(), /Ready in pegouts board below/);
  view.state.job = {id: 'job', action: 'board-create', caseId: 'case1'};
  await view.pollJob();
  assert.doesNotMatch(view.workspace(), /Ready in full board below/);
  assert.match(boardCardHtml(view, 'cashouts'), /value="requested" selected/);
  assert.match(boardCardHtml(view, 'overview'), /value="fullplot" selected/);
});

test('new layouts explicitly choose fresh mode without reading Miro', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'plotjob', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({boards: [workflowBoard('full', 'existing')]});
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /id="workflow-layout-mode"/);
  assert.match(view.workspace(), /value="fresh" selected/);
  assert.match(view.workspace(), /current attribution, change-output and color rules/);
  assert.doesNotMatch(view.workspace(), /id="workflow-layout-board"/);
  await view.dispatch('workflow-plot');
  assert.equal(view.calls.at(-1).body.layout_mode, 'fresh');
  assert.equal('board_record_id' in view.calls.at(-1).body, false);
  assert.equal(view.state.job.live, false);
});

test('board update generation is bound to the selected matching goal and reads live Miro', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'plotjob', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({boards: [workflowBoard('full', 'full'), workflowBoard('pegouts', 'cashouts'),
    workflowBoard('pegouts', 'busy', {status: 'interrupted'}), workflowBoard('pegouts', 'archive', {can_sync: false})]});
  await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  workflowEdit(view, 'layout-mode', 'update');
  const selector = view.workspace().match(/<select id="workflow-layout-board"[\s\S]*?<\/select>/)[0];
  assert.match(selector, /value="cashouts"/);
  assert.doesNotMatch(selector, /value="(?:full|busy|archive)"/);
  assert.match(view.workspace(), /data-action="workflow-plot"[^>]*disabled/);
  await assert.rejects(view.dispatch('workflow-plot'), /Select an available board/);
  workflowEdit(view, 'layout-board', 'full');
  await assert.rejects(view.dispatch('workflow-plot'), /Select an available board/);
  assert.equal(view.calls.length, 1);
  workflowEdit(view, 'layout-board', 'cashouts');
  assert.doesNotMatch(view.workspace().match(/<button[^>]*data-action="workflow-plot"[^>]*>/)[0], /disabled/);
  await view.dispatch('workflow-plot');
  assert.deepEqual(view.calls.at(-1).body, {action: 'plot', goal: 'pegouts', run_id: 'saved1', min_hops: 0, max_hops: 10, hop_basis: 'original_seeds',
    layout_mode: 'update', board_record_id: 'cashouts', include_unspent: true, include_unspendable: true, include_attributed_stops: true, layout_settings: layoutDefaults});
  assert.equal(view.state.job.live, true);
});

test('layout destinations retain a case draft but clear incompatible goals and case targets', async () => {
  const view = await harness();
  const detail = workflowCase({boards: [workflowBoard('full', 'full')]});
  view.state.activeCase = detail;
  await view.dispatch('view-plots');
  workflowEdit(view, 'layout-mode', 'update'); workflowEdit(view, 'layout-board', 'full');
  await view.dispatch('view-boards'); await view.dispatch('view-plots');
  assert.equal(view.currentWorkflow(detail).layoutBoard, 'full');
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  assert.equal(view.currentWorkflow(detail).layoutBoard, '');
  view.state.activeCase = workflowCase({id: 'second'});
  await view.dispatch('view-plots');
  assert.equal(view.currentWorkflow(view.state.activeCase).layoutMode, 'fresh');
  assert.equal(view.currentWorkflow(view.state.activeCase).layoutBoard, '');
});

test('create and sync requires a fresh reviewed plot and submits the exact selected layout', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'createjob', status: 'running'} : undefined);
  const fresh = workflowPlot('pegouts', 'fresh', {layout_mode: 'fresh'});
  view.state.activeCase = workflowCase({plots: [workflowPlot('full', 'old'), fresh,
    workflowPlot('full', 'update', {layout_mode: 'update', board_record_id: 'board', board_id: 'miro-board'}),
    workflowPlot('full', 'stale', {layout_mode: 'fresh', reviewable: false}),
    workflowPlot('full', 'empty', {layout_mode: 'fresh', empty: true, node_count: 0})]});
  await view.dispatch('view-boards');
  assert.doesNotMatch(view.workspace(), /data-action="workflow-board-create"/);
  const picker = view.workspace().match(/<select id="workflow-create-plot"[\s\S]*?<\/select>/)[0];
  assert.match(picker, /value="fresh" selected/);
  assert.doesNotMatch(picker, /value="(?:old|update|stale|empty)"/);
  workflowEdit(view, 'create-plot', 'update');
  await assert.rejects(view.dispatch('workflow-board-create-sync'), /reviewed New board layout/);
  workflowEdit(view, 'create-plot', 'fresh'); workflowEdit(view, 'saved-board-name', 'Reviewed paths');
  await view.dispatch('workflow-board-create-sync');
  assert.deepEqual(view.calls.at(-1).body, {action: 'board-create-sync', preview_id: 'fresh', name: 'Reviewed paths'});
  assert.equal(view.state.job.live, true);
  const calls = view.calls.length;
  await view.dispatch('workflow-board-create-sync');
  assert.equal(view.calls.length, calls);
});

test('board-aware plot handoff targets only its indexed board and renders removal counts', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'syncjob', status: 'running'} : undefined);
  const update = workflowPlot('pegouts', 'update', {layout_mode: 'update', board_record_id: 'second',
    board_id: 'miro-second', board_name: 'Second board', update_counts: {new_nodes: 2, retained_nodes: 3,
      removed_nodes: 1, new_connectors: 4, removed_connectors: 2}});
  view.state.activeCase = workflowCase({plots: [update, workflowPlot('pegouts', 'fresh', {layout_mode: 'fresh'})],
    boards: [workflowBoard('pegouts', 'first'), workflowBoard('pegouts', 'second')]});
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /Update existing board:<\/strong> Second board/);
  assert.match(view.workspace(), /Objects to add: 2 · Objects kept: 3 · Objects to remove: 1/);
  await view.dispatch('plot-boards');
  assert.equal(view.currentWorkflow(view.state.activeCase).board, 'second');
  assert.doesNotMatch(boardCardHtml(view, 'first'), /value="update"/);
  const second = boardCardHtml(view, 'second');
  assert.match(second, /value="update" selected/);
  assert.match(second, />Apply saved update<\/button>/);
  assert.doesNotMatch(second, /workflow-board-organize/);
  await assert.rejects(view.dispatch('workflow-board-organize', boardControl('second')), /preserving existing positions/);
  await view.dispatch('workflow-board-sync', boardControl('second'));
  assert.deepEqual(view.calls.at(-1).body, {action: 'board-sync', record_id: 'second', preview_id: 'update', reorganize: false});
});

test('prepare update selects the card target and removal-only layouts remain publishable', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'syncjob', status: 'running'} : undefined);
  const empty = workflowPlot('pegouts', 'empty', {layout_mode: 'update', board_record_id: 'cashouts',
    board_id: 'miro-cashouts', empty: true, node_count: 0, edge_count: 0,
    update_counts: {new_nodes: 0, retained_nodes: 0, removed_nodes: 3, new_connectors: 0, removed_connectors: 2}});
  view.state.activeCase = workflowCase({plots: [empty], boards: [workflowBoard('pegouts', 'cashouts')]});
  await view.dispatch('view-boards');
  await view.dispatch('workflow-board-prepare', boardControl('cashouts'));
  const draft = view.currentWorkflow(view.state.activeCase);
  assert.equal(view.state.caseView, 'plots'); assert.equal(draft.goal, 'pegouts');
  assert.equal(draft.layoutMode, 'update'); assert.equal(draft.layoutBoard, 'cashouts');
  assert.doesNotMatch(view.workspace(), /No matching paths were found/);
  await view.dispatch('plot-boards');
  assert.doesNotMatch(boardCardHtml(view, 'cashouts').match(/<button[^>]*data-action="workflow-board-sync"[^>]*>/)[0], /disabled/);
  await view.dispatch('workflow-board-sync', boardControl('cashouts'));
  assert.equal(view.calls.at(-1).body.preview_id, 'empty');
});

test('fresh creation recovery can resume only its recorded layout and linked ordinary boards cannot consume it', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'syncjob', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({plots: [workflowPlot('full', 'fresh', {layout_mode: 'fresh'})], boards: [
    workflowBoard('full', 'recover', {creation_preview_id: 'fresh'}), workflowBoard('full', 'ordinary')]});
  await view.dispatch('view-boards');
  assert.match(boardCardHtml(view, 'recover'), /Finish create & sync/);
  assert.doesNotMatch(boardCardHtml(view, 'ordinary'), /value="fresh"/);
  await assert.rejects(view.dispatch('workflow-board-sync', boardControl('ordinary')), /matching saved plot/);
  await view.dispatch('workflow-board-sync', boardControl('recover'));
  assert.deepEqual(view.calls.at(-1).body, {action: 'board-sync', record_id: 'recover', preview_id: 'fresh', reorganize: false});
});

test('successful update layout generation selects its exact board plot after refreshing', async () => {
  const update = workflowPlot('pegouts', 'update', {layout_mode: 'update', board_record_id: 'cashouts', board_id: 'miro-cashouts'});
  const detail = workflowCase({plots: [update], boards: [workflowBoard('pegouts', 'cashouts', {preview_id: 'previous'})]});
  const view = await harness(path => path === '/api/jobs/job' ? {status: 'succeeded', result: update}
    : path === '/api/cases/case1/overview' ? detail : undefined);
  view.state.activeCase = workflowCase(); view.state.job = {id: 'job', action: 'plot', caseId: 'case1', live: true};
  await view.pollJob(); await view.dispatch('view-boards');
  assert.match(boardCardHtml(view, 'cashouts'), /value="update" selected/);
});

test('Plots and Miro show preview first with unique controls and a primary preview action', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({boards: [workflowBoard('full', 'board')],
    plots: [workflowPlot('full', 'fresh', {layout_mode: 'fresh'})]});
  await view.dispatch('view-boards');
  const html = view.workspace();
  const navigation = html.match(/<nav class="case-navigation"[\s\S]*?<\/nav>/)[0];
  assert.equal(view.state.caseView, 'plots');
  assert.match(navigation, /Plots & Miro/);
  assert.doesNotMatch(navigation, /view-boards/);
  for (const id of ['plot-layouts-panel', 'miro-boards-panel', 'saved-plots-panel']) assert.match(html, new RegExp(`id="${id}"`));
  const ids = [...html.matchAll(/\sid="([^"]+)"/g)].map(match => match[1]);
  assert.equal(ids.length, new Set(ids).size, 'merged controls must not duplicate DOM IDs');
  assert.ok(html.indexOf('id="workflow-board-name"') < html.indexOf('data-action="workflow-plot-sync"'));
  assert.match(html, /class="btn primary" data-action="workflow-plot"[^>]*>[\s\S]*?Generate preview/);
  assert.match(html, /<details class="tool-details"><summary>Advanced: generate and publish in one step<\/summary>/);
  assert.ok(html.indexOf('id="saved-plots-panel"') < html.indexOf('data-action="workflow-plot-sync"'));
  assert.ok(html.indexOf('id="saved-plots-panel"') < html.indexOf('id="miro-boards-panel"'));
  assert.doesNotMatch(savedPreviewPanel(view), /<iframe|<img/);
  assert.match(savedPreviewPanel(view), /Open preview/);
  assert.match(html, /<details class="panel" id="create-sync-board-panel"><summary/);
});

test('combined new board action captures settings and submits generation and publication once', async () => {
  const detail = workflowCase();
  const view = await harness((path, body) => {
    if (path.endsWith('/plot-settings')) {detail.run_defaults = {...detail.run_defaults, ...body.settings}; return detail;}
    if (path.endsWith('/actions')) return {id: 'combined', status: 'running', live: true};
  });
  view.state.activeCase = detail;
  await view.dispatch('view-plots');
  workflowEdit(view, 'board-name', 'Investigator overview');
  view.plotForm({connector_style: 'curved'});
  await view.dispatch('workflow-plot-sync');
  const writes = view.calls.filter(call => call.body);
  assert.equal(writes.length, 1);
  assert.deepEqual(writes[0].body, {action: 'plot-sync', goal: 'full', run_id: 'saved1', min_hops: 0,
    max_hops: 10, hop_basis: 'original_seeds', layout_mode: 'fresh', name: 'Investigator overview', layout_settings: {...layoutDefaults, connector_style: 'curved'}});
  assert.equal(view.state.job.live, true);
  const count = view.calls.length;
  await view.dispatch('workflow-plot-sync');
  assert.equal(view.calls.length, count, 'busy job prevents duplicate publication');
});

test('combined update binds the exact board and includes endpoint choices', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'combined', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({boards: [workflowBoard('pegouts', 'cashouts')]});
  await view.dispatch('workflow-board-prepare', boardControl('cashouts'));
  workflowEdit(view, 'min-hops', '2'); workflowEdit(view, 'max-hops', '11');
  view.workflowInput({id: 'workflow-include-unspent', checked: true});
  view.workflowInput({id: 'workflow-include-unspendable', checked: true});
  assert.match(view.workspace(), /Generate & update board/);
  await view.dispatch('workflow-plot-sync');
  assert.deepEqual(view.calls.at(-1).body, {action: 'plot-sync', goal: 'pegouts', run_id: 'saved1',
    min_hops: 2, max_hops: 11, hop_basis: 'original_seeds', layout_mode: 'update', board_record_id: 'cashouts',
    include_unspent: true, include_unspendable: true, include_attributed_stops: true, layout_settings: layoutDefaults});
  assert.equal(view.state.job.live, true);
});

test('switching update targets restores each board query and keeps unsaved board and fresh drafts', async () => {
  const first = workflowPlot('pegouts', 'firstplot', {layout_mode: 'update', min_hops: 1, max_hops: 7,
    query: {include_unspent: true, include_context: true},
    layout_settings: {...defaults, include_fees: false, group_context_inputs: true, color_attribution_arrows: false, center_name: 'First', hub_addresses: []}});
  const second = workflowPlot('pegouts', 'secondplot', {layout_mode: 'update', min_hops: 4, max_hops: 12,
    query: {include_unspendable: true},
    layout_settings: {...defaults, connector_style: 'curved', include_fees: false, group_context_inputs: false, color_attribution_arrows: true, center_name: 'Second', hub_addresses: []}});
  const view = await harness();
  view.state.activeCase = workflowCase({plots: [first, second], boards: [
    workflowBoard('pegouts', 'first', {preview_id: 'firstplot'}), workflowBoard('pegouts', 'second', {preview_id: 'secondplot'})]});
  await view.dispatch('view-plots');
  workflowEdit(view, 'board-name', 'Fresh draft');
  await view.dispatch('workflow-board-prepare', boardControl('first'));
  let draft = view.currentWorkflow(view.state.activeCase);
  assert.equal(draft.minHops, '1'); assert.equal(draft.maxHops, '7');
  assert.equal(draft.includeUnspent, true);
  assert.match(view.workspace(), /name="center_name"[^>]*value="First"/);
  workflowEdit(view, 'max-hops', '9');
  workflowEdit(view, 'layout-board', 'second');
  draft = view.currentWorkflow(view.state.activeCase);
  assert.equal(draft.minHops, '4'); assert.equal(draft.maxHops, '12');
  assert.equal(draft.includeUnspent, false); assert.equal(draft.includeUnspendable, true);
  assert.match(view.workspace(), /name="center_name"[^>]*value="Second"/);
  workflowEdit(view, 'layout-board', 'first');
  assert.equal(draft.maxHops, '9');
  assert.match(view.workspace(), /name="center_name"[^>]*value="First"/);
  workflowEdit(view, 'layout-mode', 'fresh');
  assert.equal(draft.goal, 'full'); assert.equal(draft.boardName, 'Fresh draft');
  assert.match(view.workspace(), /name="center_name"[^>]*value=""/);
  assert.equal(view.calls.length, 1);
});

test('successful combined creation selects the new board for future updates and retains SVG output', async () => {
  const plot = workflowPlot('full', 'fresh', {layout_mode: 'fresh', artifact: {preview_id: 'fresh',
    preview_url: '/files/case1/previews/fresh/graph.html', downloads: [{name: 'graph.svg', url: '/files/case1/previews/fresh/graph.svg'}]}});
  const board = workflowBoard('full', 'newboard', {status: 'synced', preview_id: 'fresh', creation_preview_id: 'fresh'});
  const detail = workflowCase({plots: [plot], boards: [board]});
  const view = await harness(path => path === '/api/jobs/job' ? {status: 'succeeded', result: {...plot,
    published: true, record_id: 'newboard', board_url: board.board_url}}
    : path === '/api/cases/case1/overview' ? detail : undefined);
  view.state.activeCase = workflowCase(); view.state.job = {id: 'job', action: 'plot-sync', caseId: 'case1', live: true};
  await view.pollJob();
  const draft = view.currentWorkflow(detail);
  assert.equal(view.state.caseView, 'plots'); assert.equal(draft.plot, 'fresh');
  assert.equal(draft.layoutMode, 'update'); assert.equal(draft.layoutBoard, 'newboard');
  assert.match(view.workspace(), /Open synced Miro board/);
  assert.match(view.workspace(), /href="\/files\/case1\/previews\/fresh\/graph.svg"/);
  assert.match(view.workspace(), /Generate & update board/);
});

test('empty combined fresh result reports no board and preserves its saved preview', async () => {
  const plot = workflowPlot('pegouts', 'empty', {layout_mode: 'fresh', empty: true, node_count: 0, edge_count: 0});
  const detail = workflowCase({plots: [plot]});
  const view = await harness(path => path === '/api/jobs/job' ? {status: 'succeeded', result: {...plot, published: false}}
    : path === '/api/cases/case1/overview' ? detail : undefined);
  view.state.activeCase = workflowCase(); view.state.job = {id: 'job', action: 'plot-sync', caseId: 'case1', live: true};
  await view.pollJob();
  assert.equal(view.currentWorkflow(detail).layoutMode, 'fresh');
  assert.equal(view.currentWorkflow(detail).plot, 'empty');
  assert.match(view.notifications.at(-1), /no Miro board was created/);
  assert.match(view.workspace(), /Preview saved; no board created/);
  assert.doesNotMatch(view.workspace(), /Open synced Miro board/);
});

test('failed combined fresh publication refreshes and selects exact saved recovery without regenerating', async () => {
  const plot = workflowPlot('full', 'saved', {layout_mode: 'fresh'});
  const board = workflowBoard('full', 'partial', {status: 'interrupted', pending_count: 1,
    preview_id: 'saved', creation_preview_id: 'saved'});
  const detail = workflowCase({plots: [plot], boards: [board]});
  const view = await harness(path => path === '/api/jobs/job' ? {status: 'failed', message: 'Sync interrupted.'}
    : path === '/api/cases/case1/overview' ? detail : path.endsWith('/actions') ? {id: 'resume', status: 'running'} : undefined);
  view.state.activeCase = workflowCase(); view.state.job = {id: 'job', action: 'plot-sync', caseId: 'case1', live: true};
  await view.pollJob();
  assert.equal(view.currentWorkflow(detail).layoutMode, 'update');
  assert.equal(view.currentWorkflow(detail).layoutBoard, 'partial');
  assert.match(boardCardHtml(view, 'partial'), /<details class="tool-details" open>/);
  assert.match(boardCardHtml(view, 'partial'), /Resume board sync/);
  await view.dispatch('workflow-board-sync', boardControl('partial'));
  assert.deepEqual(view.calls.at(-1).body, {action: 'board-sync', record_id: 'partial', preview_id: 'saved', reorganize: false});
});

test('uncertain creation blocks a new combined create and offers linking instead of retrying POST', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({plots: [workflowPlot('full', 'saved', {layout_mode: 'fresh'})],
    boards: [workflowBoard('full', 'unknown', {board_id: null, status: 'pending_creation', can_sync: false,
      creation_preview_id: 'saved', preview_id: 'saved'})]});
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /data-action="workflow-plot-sync"[^>]*disabled/);
  assert.match(boardCardHtml(view, 'unknown'), /Link created board to this entry/);
  assert.doesNotMatch(boardCardHtml(view, 'unknown'), /Retry saved board creation/);
  await assert.rejects(view.dispatch('workflow-plot-sync'), /uncertain board creation/);
  await assert.rejects(view.dispatch('workflow-board-retry-create', boardControl('unknown')), /Only a rejected/);
  assert.equal(view.calls.length, 1);
});

test('known rejected creation retries its recorded preview and permits deliberate new generation', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'retry', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({plots: [workflowPlot('full', 'saved', {layout_mode: 'fresh'})],
    boards: [workflowBoard('full', 'rejected', {name: 'Original board name', board_id: null, status: 'creation_rejected',
      can_sync: false, creation_preview_id: 'saved', preview_id: 'saved'})]});
  await view.dispatch('view-plots');
  assert.match(boardCardHtml(view, 'rejected'), /Retry saved board creation/);
  assert.doesNotMatch(boardCardHtml(view, 'rejected'), /workflow-recovery-url/);
  assert.doesNotMatch(view.workspace().match(/<button[^>]*data-action="workflow-plot-sync"[^>]*>/)[0], /disabled/);
  await view.dispatch('workflow-board-retry-create', boardControl('rejected'));
  assert.deepEqual(view.calls.at(-1).body, {action: 'board-create-sync', preview_id: 'saved', name: 'Original board name'});
});

test('failed later update and stale initial layouts do not block deliberate independent new boards', async () => {
  for (const preview of ['later-update', 'initial']) {
    const view = await harness(path => path.endsWith('/actions') ? {id: 'new', status: 'running'} : undefined);
    view.state.activeCase = workflowCase({plots: [workflowPlot('full', 'initial', {layout_mode: 'fresh', reviewable: false})],
      boards: [workflowBoard('full', 'existing', {status: 'sync_error', creation_preview_id: 'initial', preview_id: preview})]});
    await view.dispatch('view-plots');
    assert.doesNotMatch(view.workspace().match(/<button[^>]*data-action="workflow-plot-sync"[^>]*>/)[0], /disabled/);
    await view.dispatch('workflow-plot-sync');
    assert.equal(view.calls.at(-1).body.layout_mode, 'fresh');
    assert.equal(view.calls.at(-1).body.action, 'plot-sync');
  }
});

// Concurrent jobs exercise the actual handlers and multi-job state directly.
// The state.job accessor above adapts older single-job fixtures only.
const multiCase = (id, changes = {}) => ({id, name: `Investigation ${id}`, run_defaults: {...defaults}, runs: [], ...changes});
const activeTask = (id, case_id, changes = {}) => ({id, case_id, action: 'trace', status: 'running', live: false,
  message: `Working on ${id}`, cancellable: true, started_at: 100, ...changes});

test('session reconnect restores every investigation task and gates only its current scope', async () => {
  const jobs = [activeTask('a', 'alpha'), activeTask('b', 'beta')];
  const view = await harness(path => path === '/api/session'
    ? {csrf: 'test', settings: defaults, cases: [multiCase('alpha'), multiCase('beta')], active_jobs: jobs}
    : path.startsWith('/api/cases/') ? multiCase(path.split('/')[3]) : undefined);
  assert.equal(view.runningJobs().length, 2);
  assert.match(view.jobBanner(), /2 active tasks/);
  assert.match(view.jobBanner(), /Investigation alpha/);
  assert.match(view.jobBanner(), /Investigation beta/);
  await view.openCase('alpha'); assert.equal(view.isBusy(), true);
  await view.openCase('gamma'); assert.equal(view.isBusy(), false);
  view.navigate('new'); assert.equal(view.isBusy(), false);
  view.navigate('settings'); assert.equal(view.isBusy(), false);
  assert.match(view.jobBanner(), /data-action="cancel-job" data-id="a"/);
  assert.match(view.jobBanner(), /data-action="cancel-job" data-id="b"/);
});

test('different investigations start concurrently and cancel targets exactly one job', async () => {
  const jobs = new Map();
  const view = await harness((path, body) => {
    if (path.endsWith('/actions')) {
      const caseId = path.split('/')[3], task = activeTask(`${caseId}-job`, caseId, {action: body.action});
      jobs.set(task.id, task); return task;
    }
    if (path.endsWith('/cancel')) {
      const id = path.split('/')[3]; jobs.get(id).status = 'cancelling';
      return {...jobs.get(id), cancellable: false};
    }
    if (path.startsWith('/api/cases/')) return multiCase(path.split('/')[3]);
    if (path.startsWith('/api/jobs/')) return jobs.get(path.split('/')[3]);
  });
  await view.openCase('alpha');
  assert.equal(await view.startJob('/api/cases/alpha/actions', {action: 'trace'}, 'trace', false, 'alpha'), 'alpha-job');
  assert.equal(await view.startJob('/api/cases/alpha/actions', {action: 'trace'}, 'trace', false, 'alpha'), null);
  await view.openCase('beta');
  assert.equal(view.isBusy(), false);
  await view.startJob('/api/cases/beta/actions', {action: 'trace'}, 'trace', false, 'beta');
  assert.equal(view.runningJobs().length, 2);
  await view.dispatch('cancel-job', {dataset: {id: 'alpha-job'}});
  assert.deepEqual(view.calls.filter(call => call.path.endsWith('/cancel')).map(call => call.path), ['/api/jobs/alpha-job/cancel']);
  await Promise.all([view.pollJob('alpha-job'), view.pollJob('beta-job')]);
  assert.equal(view.state.jobs.get('alpha-job').status, 'cancelling');
  assert.equal(view.state.jobs.get('beta-job').status, 'running');
  jobs.get('alpha-job').status = 'canceled';
  await view.pollJob('alpha-job');
  assert.equal(view.runningJobs().length, 1);
  assert.equal(view.state.jobs.get('beta-job').status, 'running');
  assert.equal(view.state.activeCase.id, 'beta');
});

test('background failure stays in its task and cannot clear another case error or conflicts', async () => {
  const view = await harness(path => {
    if (path === '/api/session') return {csrf: 'test', settings: defaults, cases: [], active_jobs: [activeTask('a', 'alpha')]};
    if (path === '/api/jobs/a') return {id: 'a', status: 'failed', message: 'Alpha failed'};
  });
  view.state.activeCase = multiCase('beta'); view.state.page = 'case-settings';
  view.state.error = 'Keep beta error'; view.state.editConflicts = {caseId: 'beta', report: {kind: 'beta'}};
  await view.pollJob('a');
  assert.equal(view.state.error, 'Keep beta error');
  assert.equal(view.state.editConflicts.caseId, 'beta');
  assert.match(view.jobBanner(), /Alpha failed/);
  assert.equal(view.state.page, 'case-settings');
});

test('own job completion after switching tabs preserves the selected case view and drafts', async () => {
  const detail = multiCase('alpha');
  const view = await harness(path => {
    if (path.endsWith('/actions')) return activeTask('a', 'alpha', {action: 'plot'});
    if (path === '/api/jobs/a') return {id: 'a', status: 'succeeded', result: {preview_id: 'ready'}};
    if (path === '/api/cases/alpha/overview') return {...detail, plots: []};
  });
  await view.openCase('alpha');
  await view.startJob('/api/cases/alpha/actions', {action: 'plot'}, 'plot', false, 'alpha');
  await view.dispatch('view-history');
  await view.pollJob('a');
  assert.equal(view.state.caseView, 'history');
  assert.equal(view.state.results.get('alpha').result.preview_id, 'ready');
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, '');
});

test('discovery of another tab active and short completed jobs refreshes data without changing view', async () => {
  let listed = [activeTask('a', 'alpha')];
  const view = await harness(path => {
    if (path === '/api/jobs') return {jobs: listed};
    if (path === '/api/cases/beta/overview') return multiCase('beta', {latest_run: 'new-run'});
  });
  view.state.activeCase = multiCase('beta'); view.state.page = 'case'; view.state.caseView = 'history';
  await view.discoverJobs(); assert.equal(view.runningJobs().length, 1);
  assert.equal(view.isBusy(), false);
  listed = [...listed, activeTask('b', 'beta', {status: 'succeeded', result: {run_id: 'new-run'}})];
  await view.discoverJobs();
  assert.equal(view.state.activeCase.latest_run, 'new-run');
  assert.equal(view.state.caseView, 'history');
  assert.equal(view.state.jobs.get('b').status, 'succeeded');
  await view.dispatch('dismiss-job', {dataset: {id: 'b'}});
  await view.discoverJobs(); assert.equal(view.state.jobs.has('b'), false);
});

for (const action of ['trace', 'address-counts', 'plot-sync']) {
  test(`new investigation loads outputs while the previously viewed case runs ${action}`, async () => {
    const collecting = activeTask('existing', 'alpha', {action,
      resource_kind: action === 'plot-sync' ? 'board' : 'collection', resource_key: action === 'plot-sync' ? 'BOARD=' : undefined});
    const lookup = activeTask('lookup', null, {action: 'lookup', live: true, resource_kind: 'exclusive'});
    const view = await harness(path => path === '/api/session'
      ? {csrf: 'test', settings: defaults, cases: [multiCase('alpha')], active_jobs: [collecting]}
      : path === '/api/cases/alpha/overview' ? multiCase('alpha')
      : path === '/api/lookup' ? lookup
      : path === '/api/jobs/lookup' ? {...lookup, status: 'succeeded', result: {transactions: [{txid, outputs: []}]}}
      : undefined);
    await view.openCase('alpha');
    view.navigate('new');
    view.newForm({name: 'Independent investigation', txids: txid});
    assert.equal(view.state.activeCase.id, 'alpha', 'previous case remains cached during new-case entry');
    assert.equal(view.isBusy(), false);
    await view.dispatch('lookup');
    assert.equal(view.calls.filter(call => call.path === '/api/lookup').length, 1);
    assert.equal(view.runningJobs().length, 2);
    assert.equal(view.state.jobs.get('lookup').caseId, undefined);
    await view.dispatch('lookup');
    assert.equal(view.calls.filter(call => call.path === '/api/lookup').length, 1, 'duplicate lookup stays blocked');
    await view.pollJob('lookup');
    assert.equal(view.state.draft.reports[0].txid, txid);
    assert.equal(view.state.draft.name, 'Independent investigation');
    assert.equal(view.state.jobs.get('existing').status, 'running');
  });
}

test('real null-case lookup gates only new investigation and changed hashes survive completion', async () => {
  const task = activeTask('lookup', null, {action: 'lookup', live: true});
  const view = await harness(path => path === '/api/lookup' ? task : path === '/api/jobs/lookup'
    ? {...task, status: 'succeeded', result: {transactions: [{txid, outputs: []}]}} : undefined);
  view.navigate('new'); view.state.draft.txids = txid;
  await view.startJob('/api/lookup', {}, 'lookup', true);
  assert.equal(view.isBusy(), true);
  view.navigate('settings'); assert.equal(view.isBusy(), false);
  view.navigate('new'); view.state.draft.txids = 'b'.repeat(64);
  await view.pollJob('lookup');
  assert.equal(view.state.draft.txids, 'b'.repeat(64));
  assert.equal(view.state.draft.reports.length, 0);
  view.newForm({name: 'Keep my name', txids: 'b'.repeat(64)});
  await view.dispatch('load-job-outputs', {dataset: {id: 'lookup'}});
  assert.equal(view.state.draft.txids, txid);
  assert.equal(view.state.draft.name, 'Keep my name');
});

test('discovery before a job POST returns retains local lookup ownership', async () => {
  let resolvePost;
  const posted = new Promise(resolve => {resolvePost = resolve;});
  const task = activeTask('lookup', null, {action: 'lookup', live: true});
  const view = await harness(path => {
    if (path === '/api/lookup') return posted;
    if (path === '/api/jobs') return {jobs: [task]};
    if (path === '/api/jobs/lookup') return {...task, status: 'succeeded', result: {transactions: [{txid, outputs: []}]}};
  });
  view.navigate('new'); view.state.draft.txids = txid;
  const starting = view.startJob('/api/lookup', {}, 'lookup', true);
  await view.discoverJobs();
  assert.equal(view.state.jobs.get('lookup').generation, undefined);
  resolvePost(task); await starting;
  assert.equal(typeof view.state.jobs.get('lookup').generation, 'number');
  await view.pollJob('lookup');
  assert.equal(view.state.draft.reports.length, 1);
});

test('Miro publication tasks show no cancellation control even alongside cancellable calculations', async () => {
  const view = await harness(path => path === '/api/session' ? {csrf: 'test', settings: defaults, cases: [], active_jobs: [
    activeTask('sync', 'alpha', {action: 'board-sync', cancellable: false}), activeTask('plot', 'beta', {action: 'plot'}),
  ]} : undefined);
  assert.doesNotMatch(view.jobBanner(), /data-action="cancel-job" data-id="sync"/);
  assert.match(view.jobBanner(), /data-action="cancel-job" data-id="plot"/);
  await view.cancelJob('sync');
  assert.equal(view.calls.some(call => call.path.endsWith('/cancel')), false);
});

test('a long-running oldest task remains the newest outcome after many newer tasks finish', async () => {
  const old = activeTask('oldest', 'alpha', {started_at: 1});
  const finished = Array.from({length: 35}, (_, index) => activeTask(`newer-${index}`, `other-${index}`,
    {status: 'succeeded', started_at: index + 2, finished_at: index + 3, message: 'Finished'}));
  const view = await harness(path => {
    if (path === '/api/session') return {csrf: 'test', settings: defaults, cases: [], active_jobs: [old]};
    if (path === '/api/jobs') return {jobs: [old, ...finished]};
    if (path === '/api/jobs/oldest') return {...old, status: 'succeeded', finished_at: 100, message: 'Oldest just completed'};
    if (path === '/api/cases/alpha/overview') return multiCase('alpha');
  });
  await view.discoverJobs();
  assert.equal(view.state.jobs.size, 33); // All active work plus the most recent 32 outcomes.
  await view.pollJob('oldest');
  assert.equal(view.state.jobs.has('oldest'), true);
  assert.equal(view.state.jobs.size, 32);
  const html = view.jobBanner();
  assert.match(html, /Oldest just completed/);
  assert.ok(html.indexOf('data-task="oldest"') < html.indexOf('data-task="newer-34"'));
});

test('one investigation can collect, calculate independent plots, and publish distinct boards concurrently', async () => {
  let next = 0;
  const view = await harness((path, body) => path.endsWith('/actions')
    ? activeTask(`parallel-${++next}`, 'case1', {action: body.action}) : undefined);
  view.state.activeCase = workflowCase({boards: [workflowBoard('full', 'one'), workflowBoard('full', 'two')], plots: [workflowPlot('full', 'ready', {input_snapshot_version: 1})]});
  await view.dispatch('view-plots');
  const start = (action, extra = {}) => view.startJob('/api/cases/case1/actions', {action, ...extra}, action, false, 'case1');
  assert.ok(await start('trace'));
  assert.equal(await start('address-counts'), null, 'address counts and collection share the collector');
  assert.ok(await start('plot', {layout_mode: 'fresh', run_id: 'latest'}));
  assert.ok(await start('plot', {layout_mode: 'fresh', run_id: 'latest'}));
  assert.ok(await start('board-sync', {record_id: 'one', preview_id: 'ready'}));
  assert.ok(await start('board-sync', {record_id: 'two', preview_id: 'ready'}));
  assert.equal(view.runningJobs().length, 5);
  assert.equal(await start('board-sync', {record_id: 'one'}), null);
  assert.equal(await start('plot-sync', {layout_mode: 'update', board_record_id: 'two'}), null);
  assert.equal(await start('board-link', {board: 'another'}), null, 'saved-input mutations remain exclusive');
  assert.deepEqual(view.calls.filter(call => call.body?.action === 'plot').map(call => call.body.run_id), ['saved1', 'saved1']);
  assert.equal(view.state.jobs.get('parallel-2').source_run_id, 'saved1');
  assert.match(view.workspace(), /data-action="workflow-plot"(?![^>]*disabled)/);
  assert.match(view.workspace(), /data-action="plot-settings-save"[^>]*disabled/);
});

test('collection buttons remain enabled during a board operation but duplicate board writes are disabled', async () => {
  const boards = [workflowBoard('full', 'one'), workflowBoard('full', 'two')];
  const detail = workflowCase({boards, plots: [workflowPlot('full', 'fullplot', {input_snapshot_version: 1})]});
  const view = await harness(path => path === '/api/session'
    ? {csrf: 'test', settings: defaults, cases: [detail], active_jobs: [activeTask('sync', 'case1', {
      action: 'board-sync', resource_kind: 'board', resource_key: boards[0].board_id})]} : undefined);
  view.state.activeCase = detail;
  view.state.page = 'case';
  assert.doesNotMatch(view.workspace().match(/data-action="trace-dialog"[^>]*>/)[0], /disabled/);
  view.openActionDialog('trace');
  assert.equal(view.dialog.open, true);
  view.state.caseView = 'plots';
  assert.match(boardCardHtml(view, 'one'), /data-action="workflow-board-sync"[^>]*disabled/);
  assert.doesNotMatch(boardCardHtml(view, 'two'), /data-action="workflow-board-sync"[^>]*disabled/);
});

test('same canonical board cannot be written through another investigation, and fresh creation is independent of previews', async () => {
  const board = workflowBoard('full', 'record');
  const view = await harness(path => path === '/api/session'
    ? {csrf: 'test', settings: defaults, cases: [], active_jobs: [activeTask('other', 'othercase', {
      action: 'board-sync', resource_kind: 'board', resource_key: board.board_id})]} :
      path.endsWith('/actions') ? activeTask('create', 'case1', {action: 'plot-sync'}) : undefined);
  view.state.activeCase = workflowCase({boards: [board], plots: [workflowPlot('full', 'ready', {input_snapshot_version: 1})]});
  assert.equal(view.actionBusy('board-sync', {record_id: board.id, preview_id: 'ready'}), true);
  assert.equal(view.actionBusy('plot', {layout_mode: 'fresh'}), false);
  await view.startJob('/api/cases/case1/actions', {action: 'plot-sync', layout_mode: 'fresh'}, 'plot-sync', true, 'case1');
  assert.equal(view.actionBusy('board-create-sync'), true);
  assert.equal(view.actionBusy('plot', {layout_mode: 'fresh'}), false);
  assert.equal(view.actionBusy('trace'), false);
});

test('starting a second same-case plot and editing its draft prevents an earlier result replacing those choices', async () => {
  let next = 0;
  const detail = workflowCase();
  const view = await harness((path, body) => {
    if (path.endsWith('/actions')) return activeTask(`plot-${++next}`, 'case1', {action: body.action});
    if (path.startsWith('/api/jobs/')) return {id: path.split('/').at(-1), status: 'succeeded', result: {preview_id: 'old-result'}};
    if (path === '/api/cases/case1/overview') return detail;
  });
  view.state.activeCase = detail;
  await view.dispatch('view-plots');
  await view.dispatch('workflow-plot');
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  workflowEdit(view, 'max-hops', '7');
  view.editPlotSettings({center_name: 'New draft'});
  await view.dispatch('workflow-plot');
  workflowEdit(view, 'max-hops', '9');
  await view.pollJob('plot-1');
  await view.pollJob('plot-2');
  const draft = view.currentWorkflow(detail);
  assert.equal(draft.goal, 'pegouts');
  assert.equal(draft.maxHops, '9');
  assert.notEqual(draft.plot, 'old-result');
  assert.match(view.workspace(), /value="New draft"/);
  assert.equal(view.calls.find(call => call.body?.goal === 'pegouts').body.max_hops, 7);
});

test('collection completion preserves the concrete run selected by another active layout', async () => {
  const detail = workflowCase();
  let next = 0;
  const view = await harness((path, body) => {
    if (path.endsWith('/actions')) return activeTask(`job-${++next}`, 'case1', {action: body.action});
    if (path === '/api/jobs/job-1') return {id: 'job-1', status: 'succeeded', result: {run_id: 'newer'}};
    if (path === '/api/cases/case1/overview') return {...detail, latest_run: 'newer', runs: [{id: 'newer'}, ...detail.runs]};
  });
  view.state.activeCase = detail;
  await view.dispatch('view-plots');
  await view.startJob('/api/cases/case1/actions', {action: 'trace'}, 'trace', false, 'case1');
  await view.dispatch('workflow-plot');
  await view.pollJob('job-1');
  assert.equal(view.state.selectedRun, 'saved1');
  assert.equal(view.state.activeCase.latest_run, 'newer');
  assert.equal(view.state.jobs.get('job-2').source_run_id, 'saved1');
});

test('task header separates calculations, API work, and waits and identifies the resource blocker', async () => {
  const jobs = [
    activeTask('compute', 'case1', {progress: {phase: 'optimizing', stage: 'calculating', total: 1, completed: 0, message: 'Calculating'}}),
    activeTask('fetch', 'case1', {progress: {phase: 'collecting', total: 10, completed: 3, message: 'Fetching'}}),
    activeTask('miro', 'case1', {progress: {phase: 'creating', total: 20, completed: 5, message: 'Publishing'}}),
    activeTask('wait', 'case1', {progress: {phase: 'optimizing', stage: 'resource_wait', wait_reason: 'memory',
      total: 1, completed: 0, message: 'Waiting', running_layouts: 1, waiting_layouts: 2, reserved_heap_mb: 4096}}),
    activeTask('credentials', 'case1', {execution_state: 'credentials_wait'}),
  ];
  const view = await harness(path => path === '/api/session'
    ? {csrf: 'test', settings: defaults, cases: [], active_jobs: jobs} : undefined);
  const html = view.jobBanner();
  assert.match(html, /1 computing · 2 using APIs · 2 waiting/);
  assert.match(html, /Waiting for memory allowance/);
  assert.match(html, /1 calculating · 2 waiting/);
  assert.match(html, /A reservation is an allowance, not measured RAM use/);
  assert.match(html, /Waiting for credential prompt/);
  const waited = view.jobProgress(view.state.jobs.get('wait'));
  assert.doesNotMatch(waited, /<progress/);
  for (const [reason, label] of [['cpu', 'CPU capacity'], ['fifo', 'earlier layout'], ['memory_retry', 'larger-memory retry']]) {
    view.state.jobs.get('wait').progress.wait_reason = reason;
    assert.match(view.jobBanner(), new RegExp(label));
  }
});

test('older saved layouts keep exclusive admission while newly generated board updates can use snapshots', async () => {
  const detail = workflowCase({boards: [workflowBoard('full', 'one', {preview_id: 'legacy'})],
    plots: [workflowPlot('full', 'legacy'), workflowPlot('full', 'modern', {input_snapshot_version: 1})]});
  const view = await harness(path => path === '/api/session'
    ? {csrf: 'test', settings: defaults, cases: [], active_jobs: [activeTask('collect', 'case1', {resource_kind: 'collection'})]} : undefined);
  view.state.activeCase = detail;
  view.state.page = 'case'; view.state.caseView = 'plots';
  assert.equal(view.actionBusy('board-sync', {record_id: 'one', preview_id: 'legacy'}), true);
  assert.equal(view.actionBusy('board-create-sync', {preview_id: 'legacy'}), true);
  assert.equal(view.actionBusy('board-sync', {record_id: 'one', preview_id: 'modern'}), false);
  assert.equal(view.actionBusy('plot-sync', {layout_mode: 'update', board_record_id: 'one'}), false);
  assert.doesNotMatch(boardCardHtml(view, 'one'), /data-action="workflow-board-prepare"[^>]*disabled/);
  assert.match(boardCardHtml(view, 'one'), /data-action="workflow-board-sync"[^>]*disabled/);
});

test('resuming saved creation checks its existing board identity instead of the new-board slot', async () => {
  const board = workflowBoard('full', 'one', {creation_preview_id: 'saved'});
  const view = await harness(path => path === '/api/session'
    ? {csrf: 'test', settings: defaults, cases: [], active_jobs: [activeTask('publish', 'case1', {
      action: 'board-sync', resource_kind: 'board', resource_key: board.board_id})]} : undefined);
  view.state.activeCase = workflowCase({boards: [board], plots: [workflowPlot('full', 'saved', {input_snapshot_version: 1})]});
  assert.equal(view.actionBusy('board-create-sync', {preview_id: 'saved'}), true);
  assert.equal(view.actionBusy('plot-sync', {layout_mode: 'fresh'}), false);
});

test('plot refresh arriving before trace completion preserves the selected source run', async () => {
  const before = workflowCase();
  const after = {...before, latest_run: 'newer', runs: [{id: 'newer'}, ...before.runs]};
  let next = 0;
  const view = await harness((path, body) => {
    if (path.endsWith('/actions')) return activeTask(`ordered-${++next}`, 'case1', {action: body.action});
    if (path === '/api/jobs/ordered-1') return {id: 'ordered-1', status: 'succeeded', result: {run_id: 'newer'}};
    if (path === '/api/jobs/ordered-2') return {id: 'ordered-2', status: 'succeeded', result: {preview_id: 'layout', run_id: 'saved1'}};
    if (path === '/api/cases/case1/overview') return after;
  });
  view.state.activeCase = before;
  await view.dispatch('view-plots');
  await view.startJob('/api/cases/case1/actions', {action: 'trace'}, 'trace', false, 'case1');
  await view.dispatch('workflow-plot');
  await view.pollJob('ordered-2');
  assert.equal(view.state.activeCase.latest_run, 'newer');
  assert.equal(view.state.selectedRun, 'saved1', 'plot refresh cannot follow a newer collection');
  await view.pollJob('ordered-1');
  assert.equal(view.state.selectedRun, 'saved1');
});

test('discovery and failed or canceled board refreshes preserve displayed run selection', async () => {
  for (const status of ['discovery', 'failed', 'canceled']) {
    const before = workflowCase();
    const after = {...before, latest_run: 'newer', runs: [{id: 'newer'}, ...before.runs]};
    const view = await harness(path => {
      if (path === '/api/jobs') return {jobs: [activeTask('external', 'case1', {status: 'succeeded'})]};
      if (path.endsWith('/actions')) return activeTask('board', 'case1', {action: 'plot-sync'});
      if (path === '/api/jobs/board') return {id: 'board', status, message: 'Finished'};
      if (path === '/api/cases/case1/overview') return after;
    });
    view.state.activeCase = before;
    await view.dispatch('view-plots');
    if (status === 'discovery') await view.discoverJobs();
    else {
      await view.startJob('/api/cases/case1/actions', {action: 'plot-sync', layout_mode: 'fresh'}, 'plot-sync', true, 'case1');
      await view.pollJob('board');
    }
    assert.equal(view.state.activeCase.latest_run, 'newer', status);
    assert.equal(view.state.selectedRun, 'saved1', status);
  }
});

test('a delayed older job refresh cannot replace newer board and run details', async () => {
  const before = workflowCase();
  const after = {...before, latest_run: 'newer', runs: [{id: 'newer'}, ...before.runs],
    boards: [workflowBoard('full', 'new-board')], plots: [workflowPlot('full', 'new-layout', {input_snapshot_version: 1})]};
  let next = 0;
  const refreshes = [];
  const view = await harness((path, body) => {
    if (path.endsWith('/actions')) return activeTask(`race-${++next}`, 'case1', {action: body.action});
    if (path.startsWith('/api/jobs/')) return {id: path.split('/').at(-1), status: 'succeeded',
      result: {preview_id: path.endsWith('1') ? 'old-layout' : 'new-layout', run_id: 'saved1'}};
    if (path === '/api/cases/case1/overview') return new Promise(resolve => refreshes.push(resolve));
  });
  view.state.activeCase = before;
  await view.dispatch('view-plots');
  await view.dispatch('workflow-plot');
  await view.dispatch('workflow-plot');
  const first = view.pollJob('race-1');
  await new Promise(setImmediate);
  const second = view.pollJob('race-2');
  await new Promise(setImmediate);
  assert.equal(refreshes.length, 2);
  refreshes[1](after);
  await second;
  refreshes[0](before);
  await first;
  assert.equal(view.state.activeCase.latest_run, 'newer');
  assert.equal(view.state.activeCase.boards[0].id, 'new-board');
  assert.equal(view.state.selectedRun, 'saved1');
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'new-layout');
});

const tabCases = [
  {id: 'a1', name: 'Alpha & partners', run_defaults: defaults, latest_run: 'a-new',
    runs: [{id: 'a-new', status: 'bounded_complete'}, {id: 'a-old', status: 'bounded_complete'}]},
  {id: 'b2', name: 'Beta', run_defaults: defaults, latest_run: 'b-new',
    runs: [{id: 'b-new', status: 'bounded_complete'}]},
];
const tabResponse = path => structuredClone(path === '/api/session' ? {csrf: 'test', settings: defaults, cases: tabCases}
  : tabCases.find(item => path === `/api/cases/${item.id}/overview`));

test('investigation tabs restore views, snapshots, address edits and plot drafts independently', async () => {
  const view = await harness(tabResponse);
  await view.openCase('a1');
  view.state.selectedRun = 'a-old';
  await view.dispatch('view-plots');
  Object.assign(view.currentWorkflow(view.state.activeCase), {goal: 'pegouts', maxHops: '12', boardName: 'Alpha board'});
  view.state.addressReview.notes = 'Keep this attribution draft';
  await view.openCase('b2');
  await view.dispatch('view-history');
  Object.assign(view.currentWorkflow(view.state.activeCase), {goal: 'connections', maxHops: '3'});
  await view.openCase('a1');
  assert.equal(view.state.caseView, 'plots');
  assert.equal(view.state.selectedRun, 'a-old');
  assert.equal(view.state.addressReview.notes, 'Keep this attribution draft');
  assert.equal(view.currentWorkflow(view.state.activeCase).goal, 'pegouts');
  assert.equal(view.currentWorkflow(view.state.activeCase).boardName, 'Alpha board');
  assert.deepEqual([...view.state.openCases], ['a1', 'b2']);
  assert.match(view.app.innerHTML, /aria-label="Open investigations"/);
  assert.match(view.app.innerHTML, /Alpha &amp; partners/);
  assert.match(view.app.innerHTML, /aria-label="Close Alpha &amp; partners tab"/);
  await view.openCase('b2');
  assert.equal(view.state.caseView, 'history');
  assert.equal(view.currentWorkflow(view.state.activeCase).goal, 'connections');
  assert.equal(view.state.addressReview.notes, '');
});

test('closing investigation tabs selects a neighbor and never cancels background jobs', async () => {
  const view = await harness(tabResponse);
  await view.openCase('a1');
  view.state.jobs.set('working', {id: 'working', action: 'trace', caseId: 'a1', status: 'running', started: Date.now()});
  await view.openCase('b2');
  assert.match(view.app.innerHTML, /1 running/);
  await view.dispatch('close-investigation-tab', {dataset: {id: 'b2'}});
  assert.equal(view.state.activeCase.id, 'a1');
  assert.deepEqual([...view.state.openCases], ['a1']);
  await view.dispatch('close-investigation-tab', {dataset: {id: 'a1'}});
  assert.equal(view.state.page, 'dashboard');
  assert.deepEqual([...view.state.openCases], []);
  assert.equal(view.state.jobs.get('working').status, 'running');
  assert.equal(view.calls.some(call => call.path.includes('/cancel')), false);
  await view.dispatch('open-job', {dataset: {id: 'working'}});
  assert.deepEqual([...view.state.openCases], ['a1']);
});

test('closing a background investigation tab leaves the active view intact', async () => {
  const view = await harness(tabResponse);
  await view.openCase('a1');
  await view.openCase('b2');
  await view.dispatch('view-history');
  await view.dispatch('close-investigation-tab', {dataset: {id: 'a1'}});
  assert.equal(view.state.activeCase.id, 'b2');
  assert.equal(view.state.caseView, 'history');
});

test('browser refresh restores investigation tabs and navigation without form contents', async () => {
  const view = await harness(tabResponse);
  await view.openCase('a1');
  view.state.selectedRun = 'a-old';
  await view.dispatch('view-plots');
  view.state.addressReview.notes = 'Private unsaved notes';
  await view.openCase('b2');
  await view.dispatch('view-history');
  view.windowListeners.pagehide();
  assert.doesNotMatch(view.storage.get('liquid-tracer:investigation-tabs:v1'), /Private unsaved notes/);
  const refreshed = await harness(tabResponse, {storage: view.storage, hash: '#case/b2'});
  assert.deepEqual([...refreshed.state.openCases], ['a1', 'b2']);
  assert.equal(refreshed.state.caseView, 'history');
  await refreshed.openCase('a1');
  assert.equal(refreshed.state.selectedRun, 'a-old');
  assert.equal(refreshed.state.caseView, 'plots');
});

test('tab restoration tolerates removed cases, duplicates, corrupt storage and disabled storage', async () => {
  const key = 'liquid-tracer:investigation-tabs:v1';
  const filtered = await harness(tabResponse, {storage: new Map([[key, JSON.stringify({ids: ['gone', 'a1', 'a1'],
    views: [{id: 'a1', page: 'unknown', caseView: 'unknown', selectedRun: 'gone-run'}]})]])});
  assert.deepEqual([...filtered.state.openCases], ['a1']);
  await filtered.openCase('a1');
  assert.equal(filtered.state.caseView, 'collect');
  assert.equal(filtered.state.selectedRun, 'latest');
  for (const storage of [new Map([[key, '{broken']]), {get() {throw Error('disabled');}, set() {throw Error('disabled');}}]) {
    const view = await harness(tabResponse, {storage});
    await view.openCase('a1');
    assert.equal(view.state.activeCase.id, 'a1');
  }
});

test('a late tab opening cannot replace a more recently selected investigation', async () => {
  let release;
  const slow = new Promise(resolve => {release = resolve;});
  const view = await harness(path => path === '/api/cases/a1/overview' ? slow : tabResponse(path));
  const opening = view.openCase('a1');
  await view.openCase('b2');
  release(tabCases[0]);
  await opening;
  assert.equal(view.state.activeCase.id, 'b2');
  assert.deepEqual([...view.state.openCases], ['b2']);
});

test('returning to a tab pins its displayed snapshot when another collection finishes', async () => {
  let completed = false;
  const view = await harness(path => path === '/api/cases/a1/overview' && completed
    ? {...tabCases[0], latest_run: 'a-newer', runs: [{id: 'a-newer', status: 'bounded_complete'}, ...tabCases[0].runs]}
    : tabResponse(path));
  await view.openCase('a1');
  await view.openCase('b2');
  completed = true;
  await view.openCase('a1');
  assert.equal(view.state.activeCase.latest_run, 'a-newer');
  assert.equal(view.state.selectedRun, 'a-new');
});

test('a settings save finishing after a tab switch cannot replace the selected investigation', async () => {
  let release;
  const pending = new Promise(resolve => {release = resolve;});
  const view = await harness(path => path === '/api/cases/a1/settings' ? pending : tabResponse(path));
  await view.openCase('a1');
  view.navigate('case-settings');
  await view.submitSettings({...defaults, name: 'Alpha renamed'});
  await view.openCase('b2');
  await view.dispatch('view-history');
  release({});
  await new Promise(setImmediate);
  assert.equal(view.state.activeCase.id, 'b2');
  assert.equal(view.state.caseView, 'history');
  assert.equal(view.state.error, '');
});

test('creating an investigation in the background opens its tab without switching away from current work', async () => {
  let release;
  const pending = new Promise(resolve => {release = resolve;});
  const view = await harness(path => path === '/api/cases' ? pending : tabResponse(path));
  view.navigate('new');
  await view.submit({name: 'Alpha new', seeds: `${txid}:0`});
  await view.openCase('b2');
  release(tabCases[0]);
  await new Promise(setImmediate);
  assert.equal(view.state.activeCase.id, 'b2');
  assert.deepEqual([...view.state.openCases], ['b2', 'a1']);
  assert.equal(view.state.error, '');
});

test('switching to settings and back retains both cases unsaved settings', async () => {
  const view = await harness(tabResponse);
  await view.openCase('a1');
  view.navigate('case-settings');
  view.elements.set('#settings-form', {values: {...defaults, name: 'Unsaved Alpha', hops: 17}});
  await view.openCase('b2');
  view.elements.delete('#settings-form');
  view.navigate('case-settings');
  view.elements.set('#settings-form', {values: {...defaults, name: 'Unsaved Beta', hops: 4}});
  await view.openCase('a1');
  view.elements.delete('#settings-form');
  assert.equal(view.state.page, 'case-settings');
  assert.match(view.settingsPage(), /value="Unsaved Alpha"/);
  assert.match(view.settingsPage(), /name="hops"[^>]*value="17"/);
  await view.openCase('b2');
  assert.match(view.settingsPage(), /value="Unsaved Beta"/);
});

test('a background CSV import invalidates only its owning investigation caches on return', async () => {
  const started = Promise.withResolvers(), pending = Promise.withResolvers();
  const view = await harness(tabResponse, {importAction: async (action, context) => {
    if (action !== 'input-import-apply') return false;
    started.resolve(context.caseId);
    await pending.promise;
    await context.refresh();
    return true;
  }});
  await view.openCase('a1');
  view.state.addressReview.selected = {address: 'alpha-address'};
  view.state.addressReview.data = {rows: [{address: 'alpha-address'}], offset: 0, limit: 25, total: 1};
  const applying = view.dispatch('input-import-apply');
  assert.equal(await started.promise, 'a1');
  await view.openCase('b2');
  view.state.addressReview.selected = {address: 'beta-address'};
  view.state.addressReview.data = {rows: [{address: 'beta-address'}], offset: 0, limit: 25, total: 1};
  view.state.addressReview.notes = 'Keep unsaved Beta notes';
  pending.resolve();
  await applying;
  assert.equal(view.state.activeCase.id, 'b2');
  assert.equal(view.state.addressReview.selected.address, 'beta-address');
  assert.equal(view.state.addressReview.notes, 'Keep unsaved Beta notes');
  assert.deepEqual(view.editorResets, []);
  await view.openCase('a1');
  assert.equal(view.state.addressReview.selected, null);
  assert.equal(view.state.addressReview.data, null);
  assert.deepEqual(view.editorResets, [
    {editor: 'addresses', caseId: 'a1'}, {editor: 'colors', caseId: 'a1'}, {editor: 'change-outputs', caseId: 'a1'},
  ]);
  await view.openCase('b2');
  assert.equal(view.state.addressReview.selected.address, 'beta-address');
  assert.equal(view.state.addressReview.notes, 'Keep unsaved Beta notes');
});

test('an investigation import in progress does not block new-investigation output loading', async () => {
  const view = await harness(path => path === '/api/lookup'
    ? activeTask('independent-lookup', null, {action: 'lookup', live: true}) : tabResponse(path),
    {importPending: () => true});
  await view.openCase('a1');
  assert.equal(view.isBusy(), true);
  view.navigate('new');
  view.state.draft.txids = txid;
  assert.equal(view.isBusy(), false);
  await view.dispatch('lookup');
  assert.equal(view.calls.filter(call => call.path === '/api/lookup').length, 1);
});

const valueSummary = (lbtc, extra = {}) => ({lbtc, value_base_units: '123456789', pegout_count: 2,
  valued_lbtc_count: 2, unknown_amount_count: 0, unknown_asset_count: 0, non_lbtc_count: 0, ...extra});
const valuePlot = (preview_id, run_id, created_at, summary) => ({preview_id, run_id, created_at, goal: 'pegouts',
  min_hops: 0, max_hops: 10, pegout_lbtc_summary: summary, node_count: 4, edge_count: 4, transaction_count: 2,
  status: 'matches_found', reviewable: true, artifact: {downloads: [{name: 'endpoints.csv', url: '/files/endpoints.csv'}]}});

test('collected data shows the newest peg-out LBTC total for only the selected snapshot', async () => {
  const view = await harness(tabResponse);
  await view.openCase('a1');
  view.state.activeCase.plots = [
    valuePlot('old', 'a-new', '2026-10-01T01:00:00Z', valueSummary('9')),
    valuePlot('unrelated', 'a-old', '2026-10-01T03:00:00Z', valueSummary('88')),
    valuePlot('latest', 'a-new', '2026-10-01T02:00:00Z', valueSummary('1.23456789')),
    {...valuePlot('full', 'a-new', '2026-10-01T04:00:00Z', valueSummary('1000')), goal: 'full'},
  ];
  const metric = () => view.workspace().match(/<div class="pegout-total"[\s\S]*?<\/a><\/div>/)[0];
  assert.match(metric(), /1\.23456789 LBTC/);
  assert.match(metric(), /2 unique peg-outs/);
  assert.match(metric(), /Hops 0–10/);
  assert.match(metric(), /Endpoint CSV/);
  assert.doesNotMatch(metric(), /<strong>(?:9|88|1000) LBTC/);
  view.state.selectedRun = 'a-old';
  assert.match(metric(), /88 LBTC/);
});

test('peg-out LBTC total distinguishes partial values, undisclosed values and a genuine zero', async () => {
  const view = await harness(tabResponse);
  await view.openCase('a1');
  const plot = valuePlot('partial', 'a-new', '2026-10-01T02:00:00Z', valueSummary('1.23456789', {
    pegout_count: 4, valued_lbtc_count: 1, unknown_amount_count: 1, unknown_asset_count: 1, non_lbtc_count: 1,
  }));
  view.state.activeCase.plots = [plot];
  assert.match(view.workspace(), /Known amounts only: 1 valued in LBTC/);
  assert.match(view.workspace(), /1 hidden or unavailable amount/);
  assert.match(view.workspace(), /1 unidentified asset/);
  assert.match(view.workspace(), /1 non-LBTC output excluded/);
  plot.pegout_lbtc_summary = valueSummary('0', {pegout_count: 1, valued_lbtc_count: 0, unknown_amount_count: 1});
  assert.match(view.workspace(), /<strong>Unknown<\/strong>/);
  plot.pegout_lbtc_summary = valueSummary('0', {pegout_count: 0, valued_lbtc_count: 0});
  assert.match(view.workspace(), /<strong>0 LBTC<\/strong>/);
  assert.match(view.workspace(), /0 unique peg-outs/);
});

test('old peg-out plots request a new trace instead of showing an invented LBTC total', async () => {
  const view = await harness(tabResponse);
  await view.openCase('a1');
  assert.doesNotMatch(view.workspace(), /Peg-out LBTC total/);
  view.state.activeCase.plots = [valuePlot('legacy', 'a-new', '2026-10-01T02:00:00Z', undefined)];
  assert.match(view.workspace(), /Generate a new peg-out trace to calculate the total for this snapshot/);
  assert.doesNotMatch(view.workspace(), /<strong>0 LBTC/);
});

const combinedExport = (extra = {}) => ({filename: 'open-investigation-endpoints.csv',
  content_type: 'text/csv; charset=utf-8', csv: 'Source,Status,Investigation ID\r\nseed,Pegout,a1\r\n', endpoint_count: 1,
  included: [{case_id: 'a1', name: 'Alpha & partners', run_id: 'a-old', plot_id: 'plot-a', endpoint_count: 1}],
  skipped: [{case_id: 'b2', name: 'Beta', run_id: 'b-new', reason: 'No saved peg-out trace for this snapshot.'}], ...extra});

test('combined endpoint export captures only open tabs and each selected run while other jobs continue', async () => {
  const view = await harness(path => path === '/api/endpoint-exports' ? combinedExport() : tabResponse(path));
  await view.openCase('a1');
  view.state.selectedRun = 'a-old';
  await view.openCase('b2');
  view.state.jobs.set('working', {id: 'working', action: 'trace', caseId: 'a1', status: 'running', started: Date.now()});
  view.render();
  assert.match(view.app.innerHTML, /data-action="export-open-endpoints"/);
  assert.doesNotMatch(view.app.innerHTML, /data-action="export-open-endpoints"[^>]*disabled/);
  await view.dispatch('export-open-endpoints');
  assert.deepEqual(view.calls.find(call => call.path === '/api/endpoint-exports').body, {
    investigations: [{case_id: 'a1', run_id: 'a-old'}, {case_id: 'b2', run_id: 'b-new'}],
  });
  assert.deepEqual(view.downloads, [{url: 'blob:export-1', filename: 'open-investigation-endpoints.csv'}]);
  assert.equal(await view.downloadBlobs[0].text(), combinedExport().csv);
  assert.equal('csv' in view.state.endpointExport.result, false, 'large CSV is not retained in UI state');
  assert.match(view.app.innerHTML, /Included 1 · Skipped 1/);
  assert.match(view.app.innerHTML, /Beta: skipped/);
  assert.match(view.app.innerHTML, /Shared endpoints remain separate rows/);
  assert.equal(view.state.activeCase.id, 'b2');
  assert.equal(view.state.jobs.get('working').status, 'running');
});

test('changing open tabs during export keeps the captured scope and prevents duplicate requests', async () => {
  let release;
  const wait = new Promise(resolve => {release = resolve;});
  const view = await harness(path => path === '/api/endpoint-exports' ? wait : tabResponse(path));
  await view.openCase('a1');
  await view.openCase('b2');
  const exporting = view.dispatch('export-open-endpoints');
  await view.dispatch('export-open-endpoints');
  await view.dispatch('close-investigation-tab', {dataset: {id: 'a1'}});
  view.navigate('new');
  view.state.draft.name = 'Next investigation';
  release(combinedExport());
  await exporting;
  assert.equal(view.calls.filter(call => call.path === '/api/endpoint-exports').length, 1);
  assert.deepEqual(view.calls.find(call => call.path === '/api/endpoint-exports').body.investigations.map(item => item.case_id), ['a1', 'b2']);
  assert.deepEqual([...view.state.openCases], ['b2']);
  assert.equal(view.state.page, 'new');
  assert.equal(view.state.draft.name, 'Next investigation');
  assert.equal(view.downloads.length, 1);
  assert.equal(view.state.endpointExport.pending, false);
});

test('combined export reports missing traces without downloading an empty misleading result', async () => {
  const view = await harness(path => path === '/api/endpoint-exports' ? combinedExport({included: [], endpoint_count: 0}) : tabResponse(path));
  await view.openCase('b2');
  await view.dispatch('export-open-endpoints');
  assert.equal(view.downloads.length, 0);
  assert.match(view.app.innerHTML, /No saved endpoint traces to export/);
  assert.match(view.app.innerHTML, /Generate a Paths to endpoints plot/);
  assert.match(view.app.innerHTML, /Beta: skipped/);
});

test('a completed trace with zero endpoints downloads a header-only combined CSV', async () => {
  const product = combinedExport({endpoint_count: 0, csv: 'Source,Status,Investigation ID\r\n', skipped: [],
    included: [{case_id: 'a1', name: 'Alpha', run_id: 'a-new', plot_id: 'empty', endpoint_count: 0}]});
  const view = await harness(path => path === '/api/endpoint-exports' ? product : tabResponse(path));
  await view.openCase('a1');
  await view.dispatch('export-open-endpoints');
  assert.equal(view.downloads.length, 1);
  assert.equal(await view.downloadBlobs[0].text(), product.csv);
  assert.match(view.app.innerHTML, /Exported 0 endpoint rows from 1 investigation/);
});

test('failed combined exports retain current work and report escaped errors without a partial download', async () => {
  let fail = true;
  const view = await harness(path => path === '/api/endpoint-exports'
    ? (fail ? {error: '<unsafe> saved trace failed verification'} : combinedExport({skipped: []})) : tabResponse(path));
  await view.openCase('a1');
  view.state.error = 'Existing action error';
  await view.dispatch('export-open-endpoints');
  assert.equal(view.downloads.length, 0);
  assert.equal(view.state.error, 'Existing action error');
  assert.match(view.app.innerHTML, /&lt;unsafe&gt; saved trace failed verification/);
  assert.doesNotMatch(view.app.innerHTML, /<unsafe>/);
  fail = false;
  await view.dispatch('export-open-endpoints');
  assert.equal(view.downloads.length, 1);
  await view.dispatch('dismiss-endpoint-export');
  assert.equal(view.state.endpointExport.result, null);
  assert.equal(view.state.endpointExport.error, '');
});

test('combined export skips closed cases and does nothing when no tabs are open', async () => {
  const view = await harness(path => path === '/api/endpoint-exports' ? combinedExport() : tabResponse(path));
  await view.dispatch('export-open-endpoints');
  assert.equal(view.calls.some(call => call.path === '/api/endpoint-exports'), false);
  await view.openCase('a1');
  await view.openCase('b2');
  await view.dispatch('close-investigation-tab', {dataset: {id: 'a1'}});
  await view.dispatch('export-open-endpoints');
  assert.deepEqual(view.calls.find(call => call.path === '/api/endpoint-exports').body.investigations,
    [{case_id: 'b2', run_id: 'b-new'}]);
});

for (const terminal of ['succeeded', 'failed', 'canceled']) {
  test(`system notification for ${terminal} background task is once-only and opens the correct case`, async () => {
    const sent = [];
    let permissionRequests = 0;
    class SystemNotification {
      static permission = 'default';
      static async requestPermission() {permissionRequests++; this.permission = 'granted'; return 'granted';}
      constructor(title, options) {this.title = title; this.options = options; sent.push(this);}
      close() {this.closed = true;}
    }
    const details = {id: 'background', name: 'Background investigation', run_defaults: defaults, runs: []};
    const view = await harness(path => {
      if (path === '/api/session') return {csrf: 'test', settings: defaults, cases: [details]};
      if (path === '/api/jobs/background-job') return {id: 'background-job', status: terminal,
        message: 'Private backend error details must not be copied to the system notification.', result: {}};
      if (path === '/api/cases/background/overview') return details;
    }, {systemNotification: SystemNotification});
    assert.equal(permissionRequests, 0);
    assert.match(view.app.innerHTML, /Notifications off/);
    await view.dispatch('toggle-system-notifications');
    assert.equal(permissionRequests, 1);
    assert.match(view.app.innerHTML, /Notifications on/);
    // Toast is a native keyboard-accessible button, and clicking removes it.
    const toast = view.toastElements.at(-1);
    assert.equal(toast.tag, 'button');
    assert.equal(toast.type, 'button');
    assert.match(toast.attributes['aria-label'], /Dismiss notification/);
    toast.click();
    assert.equal(toast.removed, true);
    view.state.jobs.set('background-job', {id: 'background-job', caseId: 'background', action: 'trace',
      status: 'running', message: 'Collecting', started: Date.now(), live: true, cancellable: false});
    await view.pollJob('background-job');
    await view.pollJob('background-job');
    assert.equal(sent.length, 1);
    assert.match(sent[0].options.body, /Background investigation/);
    assert.doesNotMatch(sent[0].options.body, /Private backend/);
    sent[0].onclick();
    await new Promise(setImmediate);
    assert.equal(view.focusCount, 1);
    assert.equal(sent[0].closed, true);
    assert.equal(view.state.activeCase.id, 'background');
  });
}

test('initial completed task history never sends system notifications', async () => {
  const sent = [];
  class SystemNotification {
    static permission = 'granted';
    constructor(title) {sent.push(title);}
  }
  const history = {id: 'old-job', status: 'succeeded', action: 'plot', case_id: 'past', message: 'Done'};
  const view = await harness(path => {
    if (path === '/api/session') return {csrf: 'test', settings: defaults, cases: [], active_jobs: [history]};
    if (path === '/api/jobs') return {jobs: [history]};
  }, {systemNotification: SystemNotification, storage: new Map([['liquid-tracer:system-notifications:v1', 'on']])});
  await view.discoverJobs();
  await view.pollJob('old-job');
  assert.equal(sent.length, 0);
  assert.equal(view.state.jobs.get('old-job').status, 'succeeded');
  assert.equal(view.notifications.length, 0);
});

for (const deliveredBeforePost of [false, true]) {
  test(`fast lookup discovered before POST response ${deliveredBeforePost ? 'retains delivered notification suppression' : 'still sends its completion notification'}`, async () => {
    let resolvePost;
    const posted = new Promise(resolve => {resolvePost = resolve;});
    const sent = [];
    class SystemNotification {
      static permission = 'granted';
      constructor(title) {sent.push(title);}
    }
    const task = activeTask('fast-lookup', null, {action: 'lookup', live: true});
    const terminal = {...task, status: 'succeeded', result: {transactions: [{txid, outputs: []}]}};
    const storage = new Map([['liquid-tracer:system-notifications:v1', 'on']]);
    const view = await harness(path => {
      if (path === '/api/lookup') return posted;
      if (path === '/api/jobs') return {jobs: [deliveredBeforePost ? task : terminal]};
      if (path === '/api/jobs/fast-lookup') return terminal;
    }, {systemNotification: SystemNotification, storage});
    view.navigate('new'); view.state.draft.txids = txid;
    const starting = view.startJob('/api/lookup', {}, 'lookup', true);
    await view.discoverJobs();
    assert.equal(view.state.jobs.get(task.id).generation, undefined);
    if (deliveredBeforePost) await view.pollJob(task.id);
    assert.equal(sent.length, deliveredBeforePost ? 1 : 0);
    resolvePost(task); await starting;
    await view.pollJob(task.id);
    await view.pollJob(task.id);
    assert.equal(sent.length, 1);
    assert.equal(view.state.draft.reports.length, 1);
    assert.deepEqual(JSON.parse(storage.get('liquid-tracer:task-notifications:v1')), [task.id]);
  });
}

const sharedCollection = (extra = {}) => ({dataset_id: 'pool', name: 'Shared collection', compatible: true,
  seeds: [`${txid}:0`, `${'b'.repeat(64)}:1`], seed_count: 2,
  members: [{id: 'case1', name: 'Alpha'}, {id: 'case2', name: 'Beta'}], latest_run: 'shared-new',
  runs: [{id: 'shared-new', status: 'bounded_complete', collected_hops: 12, transaction_count: 80, max_hops: 12},
    {id: 'shared-old', status: 'bounded_complete', collected_hops: 6, transaction_count: 40, max_hops: 6}], ...extra});

test('shared collection captures open cases once and leaves private preferences unchanged', async () => {
  let release;
  const posting = new Promise(resolve => {release = resolve;});
  const detail = workflowCase({shared_collection: sharedCollection()});
  const view = await harness(path => path.endsWith('/actions') ? posting : undefined);
  view.state.activeCase = detail; view.state.page = 'case'; view.state.openCases = ['case2', 'case2'];
  await view.dispatch('shared-collect-dialog');
  assert.match(view.dialog.innerHTML, /Uses Shared evidence’s collection limits and explicit stop-tracing rules/);
  await view.submitDialog({hops: '8', hop_reference_name: 'Perp'});
  view.state.openCases = ['different'];
  assert.deepEqual(view.calls.find(call => call.path.endsWith('/actions')).body,
    {action: 'shared-trace', mode: 'collect', case_ids: ['case2', 'case1'], hops: 8, hop_reference_name: 'Perp'});
  release({id: 'shared-job', status: 'running', resource_kind: 'shared_collection', resource_key: 'global-pool'});
  await new Promise(setImmediate);
  assert.deepEqual(detail.seeds, [`${txid}:0`]);
  assert.equal(detail.run_defaults.hop_reference_name, '');
  assert.equal(view.state.jobs.get('shared-job').resource_kind, 'shared_collection');
});

test('shared collection filters mixed open tabs by network and retains the reviewed members', async () => {
  for (const chain of ['bitcoin', 'liquid']) {
    const detail = workflowCase({id: 'focus', name: 'Focused', blockchain: chain, shared_collection: sharedCollection()});
    const view = await harness(path => path.endsWith('/actions') ? {id: 'shared', status: 'running'} : undefined);
    view.state.activeCase = detail; view.state.page = 'case';
    view.state.cases = [detail, workflowCase({id: 'btc', name: 'Bitcoin member', blockchain: 'bitcoin'}),
      workflowCase({id: 'liquid', name: 'Liquid member', blockchain: 'liquid'}),
      workflowCase({id: 'legacy', name: 'Legacy member', blockchain: undefined})];
    view.state.openCases = ['btc', 'liquid', 'legacy', 'btc'];
    const expected = chain === 'bitcoin' ? ['btc', 'focus'] : ['liquid', 'legacy', 'focus'];
    assert.match(view.workspace(), new RegExp(`${expected.length} currently open ${chain === 'bitcoin' ? 'Bitcoin' : 'Liquid'} investigations`));
    await view.dispatch('shared-collect-dialog');
    assert.match(view.dialog.innerHTML, chain === 'bitcoin'
      ? /2 open investigations on other blockchains are excluded/ : /1 open investigation on another blockchain is excluded/);
    assert.match(view.dialog.innerHTML, chain === 'bitcoin' ? /Bitcoin member, Focused/ : /Liquid member, Legacy member, Focused/);
    assert.doesNotMatch(view.dialog.innerHTML, chain === 'bitcoin' ? /Liquid member|Legacy member/ : /Bitcoin member/);
    view.state.openCases = ['changed-after-review'];
    await view.submitDialog({hops: '10', hop_reference_name: ''});
    assert.deepEqual(view.calls.find(call => call.path.endsWith('/actions')).body.case_ids, expected);
  }
});

test('continuing shared data pins its displayed run without open-tab seeds or private run', async () => {
  const detail = workflowCase({shared_collection: sharedCollection()});
  const view = await harness(path => path.endsWith('/actions') ? {id: 'continue-shared', status: 'running'} : undefined);
  view.state.activeCase = detail; view.state.page = 'case'; view.state.selectedRun = 'saved1'; view.state.openCases = ['unrelated'];
  await view.dispatch('shared-continue-dialog');
  detail.shared_collection.latest_run = 'changed-after-opening';
  await view.submitDialog({hops: '0', hop_reference_name: ''});
  assert.deepEqual(view.calls.find(call => call.path.endsWith('/actions')).body,
    {action: 'shared-trace', mode: 'continue', run_id: 'shared-new', hops: 0, hop_reference_name: ''});
  assert.equal(view.state.selectedRun, 'saved1');
});

test('shared collectors block each other across cases while private collection and plotting stay independent', async () => {
  const view = await harness(); view.state.activeCase = workflowCase({shared_collection: sharedCollection()}); view.state.page = 'case';
  view.state.jobs.set('shared', {id: 'shared', action: 'shared-trace', caseId: 'different', status: 'running', resource_kind: 'shared_collection', resource_key: 'global-pool'});
  assert.equal(view.actionBusy('shared-trace'), true);
  assert.equal(view.actionBusy('trace'), false);
  assert.equal(view.actionBusy('plot', {layout_mode: 'fresh'}), false);
  assert.equal(view.actionBusy('plot-sync', {layout_mode: 'fresh'}), false);
  view.state.jobs.clear();
  view.state.jobs.set('private', {id: 'private', action: 'trace', caseId: 'case1', status: 'running', resource_kind: 'collection'});
  assert.equal(view.actionBusy('shared-trace'), false); assert.equal(view.actionBusy('trace'), true);
  assert.doesNotMatch(view.workspace(), /data-action="shared-collect-dialog"[^>]*disabled/);
});

test('shared source generates before private collection and pins the selected shared snapshot', async () => {
  const detail = workflowCase({runs: [], latest_run: undefined, shared_collection: sharedCollection()});
  const view = await harness(path => path.endsWith('/actions') ? {id: 'shared-plot', status: 'running'} : path === '/api/cases/case1/overview' ? detail : undefined);
  view.state.activeCase = detail; await view.dispatch('view-plots');
  assert.match(view.workspace(), /data-action="workflow-plot"[^>]*disabled/);
  workflowEdit(view, 'data-source', 'shared'); workflowEdit(view, 'shared-run', 'shared-old');
  assert.doesNotMatch(view.workspace(), /data-action="workflow-plot"[^>]*disabled/);
  assert.match(view.workspace(), /own selected seed outputs, attribution rules/);
  await view.dispatch('workflow-plot');
  const request = view.calls.find(call => call.path.endsWith('/actions'));
  assert.equal(request.body.data_source, 'shared'); assert.equal(request.body.dataset_id, 'pool'); assert.equal(request.body.run_id, 'shared-old');
  assert.equal(view.state.selectedRun, 'latest'); assert.equal(view.state.jobs.get('shared-plot').source_run_id, 'shared-old');
});

test('shared and private source drafts remain isolated across investigations', async () => {
  const alpha = workflowCase({shared_collection: sharedCollection()}), beta = workflowCase({id: 'case2', name: 'Beta', shared_collection: sharedCollection()});
  const view = await harness(path => path === '/api/cases/case1/overview' ? alpha : path === '/api/cases/case2/overview' ? beta : undefined);
  await view.openCase('case1'); view.state.selectedRun = 'saved1';
  workflowEdit(view, 'data-source', 'shared'); workflowEdit(view, 'shared-run', 'shared-old');
  await view.openCase('case2'); assert.equal(view.currentWorkflow(beta).dataSource, 'investigation');
  workflowEdit(view, 'data-source', 'shared'); assert.equal(view.currentWorkflow(beta).sharedRun, 'shared-new');
  await view.openCase('case1'); assert.equal(view.currentWorkflow(alpha).sharedRun, 'shared-old');
  assert.equal(view.currentWorkflow(alpha).dataSource, 'shared'); assert.equal(view.state.selectedRun, 'saved1');
  workflowEdit(view, 'data-source', 'investigation'); assert.equal(view.state.selectedRun, 'saved1');
  workflowEdit(view, 'data-source', 'shared'); assert.equal(view.currentWorkflow(alpha).sharedRun, 'shared-old');
});

test('shared completion refreshes another case pool without changing its source or private snapshot', async () => {
  const before = sharedCollection(), after = sharedCollection({latest_run: 'newest', runs: [{id: 'newest', status: 'bounded_complete'}, ...before.runs]});
  const detail = workflowCase({shared_collection: before});
  const view = await harness(path => path === '/api/jobs/shared-job' ? {id: 'shared-job', status: 'succeeded', result: {run_id: 'newest'}}
    : path === '/api/cases/other/overview' ? workflowCase({id: 'other', shared_collection: after})
    : (path === '/api/cases/case1/overview' || path.startsWith('/api/cases/case1/shared-collection')) ? {...detail, shared_collection: after} : undefined);
  view.state.activeCase = detail; view.state.page = 'case'; view.state.caseView = 'plots'; view.state.selectedRun = 'saved1';
  workflowEdit(view, 'data-source', 'shared'); workflowEdit(view, 'shared-run', 'shared-old');
  view.state.jobs.set('shared-job', {id: 'shared-job', action: 'shared-trace', caseId: 'other', status: 'running', started: Date.now(), live: false});
  await view.pollJob('shared-job');
  assert.equal(view.state.activeCase.id, 'case1'); assert.equal(view.state.activeCase.shared_collection.latest_run, 'newest');
  assert.equal(view.currentWorkflow(detail).sharedRun, 'shared-old'); assert.equal(view.state.selectedRun, 'saved1'); assert.equal(view.state.caseView, 'plots');
});

test('shared board update restores original shared provenance instead of its materialized private run', async () => {
  const plot = workflowPlot('pegouts', 'saved-shared', {run_id: 'projection-123', layout_mode: 'update', board_record_id: 'cashouts', board_id: 'miro-cashouts',
    collection_source: {kind: 'shared', dataset_id: 'pool', run_id: 'shared-old'}});
  const detail = workflowCase({shared_collection: sharedCollection(), plots: [plot], boards: [workflowBoard('pegouts', 'cashouts', {preview_id: plot.preview_id})]});
  const view = await harness(path => path.endsWith('/actions') ? {id: 'board-update', status: 'running'} : undefined);
  view.state.activeCase = detail; view.state.page = 'case'; view.state.selectedRun = 'saved1';
  await view.dispatch('workflow-board-prepare', boardControl('cashouts'));
  assert.equal(view.currentWorkflow(detail).dataSource, 'shared'); assert.equal(view.currentWorkflow(detail).sharedRun, 'shared-old');
  assert.match(view.workspace(), /Shared snapshot shared-old/);
  await view.dispatch('workflow-plot-sync');
  const request = view.calls.find(call => call.path.endsWith('/actions'));
  assert.equal(request.body.run_id, 'shared-old'); assert.equal(request.body.dataset_id, 'pool'); assert.equal(request.body.board_record_id, 'cashouts');
  assert.equal(view.state.selectedRun, 'saved1');
});

test('incompatible shared source cannot fall back silently to private data', async () => {
  const detail = workflowCase({shared_collection: sharedCollection()}); const view = await harness();
  view.state.activeCase = detail; view.state.page = 'case'; view.state.caseView = 'plots'; workflowEdit(view, 'data-source', 'shared');
  detail.shared_collection = sharedCollection({compatible: false, reason: 'Different blockchain source'});
  assert.match(view.workspace(), /Different blockchain source/); assert.match(view.workspace(), /data-action="workflow-plot"[^>]*disabled/);
  await assert.rejects(() => view.dispatch('workflow-plot'), /available saved snapshot/);
  assert.equal(view.calls.filter(call => call.path.endsWith('/actions')).length, 0);
});

test('shared endpoint totals and combined export match exact provenance rather than private runs', async () => {
  const descriptor = {kind: 'shared', dataset_id: 'pool', run_id: 'shared-old'};
  const alpha = workflowCase({shared_collection: sharedCollection(), plots: [
    valuePlot('private', 'saved1', '2026-10-01T03:00:00Z', valueSummary('99')),
    {...valuePlot('shared', 'projection', '2026-10-01T02:00:00Z', valueSummary('7')), collection_source: descriptor, seeds: [`${txid}:0`]},
    {...valuePlot('other-shared', 'another-projection', '2026-10-01T04:00:00Z', valueSummary('88')), collection_source: {...descriptor, run_id: 'shared-new'}, seeds: [`${txid}:0`]},
  ]});
  const beta = workflowCase({id: 'case2', name: 'Beta', shared_collection: sharedCollection()});
  const view = await harness(path => path === '/api/cases/case1/overview' ? alpha : path === '/api/cases/case2/overview' ? beta
    : path === '/api/endpoint-exports' ? combinedExport() : undefined);
  await view.openCase('case1'); workflowEdit(view, 'data-source', 'shared'); workflowEdit(view, 'shared-run', 'shared-old');
  assert.match(view.workspace(), /<strong>7 LBTC<\/strong>/); assert.doesNotMatch(view.workspace(), /<strong>(?:88|99) LBTC<\/strong>/);
  await view.openCase('case2'); await view.dispatch('export-open-endpoints');
  assert.deepEqual(view.calls.find(call => call.path === '/api/endpoint-exports').body, {investigations: [
    {case_id: 'case1', data_source: 'shared', dataset_id: 'pool', run_id: 'shared-old'}, {case_id: 'case2', run_id: 'saved1'},
  ]});
});

for (const fullFirst of [true, false]) {
  test(`shared summary stays current when ${fullFirst ? 'full' : 'shared'} refresh finishes late`, async () => {
    let resolveOlder, resolveNewer, reads = 0;
    const older = new Promise(resolve => {resolveOlder = resolve;});
    const newer = new Promise(resolve => {resolveNewer = resolve;});
    const detail = workflowCase({shared_collection: sharedCollection()});
    const view = await harness(path => ['/api/cases/case1/overview', '/api/cases/case1/shared-collection'].includes(path) ? (++reads === 1 ? older : newer) : undefined);
    view.state.activeCase = detail; view.state.page = 'case'; view.state.selectedRun = 'saved1';
    const first = fullFirst ? view.refreshCaseDetail('case1') : view.refreshSharedCollection('case1');
    const second = fullFirst ? view.refreshSharedCollection('case1') : view.refreshCaseDetail('case1');
    resolveNewer({...detail, shared_collection: sharedCollection({latest_run: 'fresh-revision'})});
    await second;
    resolveOlder({...detail, name: 'Updated private metadata', shared_collection: sharedCollection({latest_run: 'stale-revision'})});
    await first;
    assert.equal(view.state.activeCase.shared_collection.latest_run, 'fresh-revision');
    assert.equal(view.state.selectedRun, 'saved1');
    if (fullFirst) assert.equal(view.state.activeCase.name, 'Updated private metadata');
  });
}

test('incompatible shared collection explains and disables fresh collection as well as continuation', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({shared_collection: sharedCollection({compatible: false, reason: 'Different blockchain source'})});
  view.state.page = 'case';
  assert.match(view.workspace(), /Different blockchain source/);
  for (const action of ['shared-collect-dialog', 'shared-continue-dialog']) {
    assert.match(view.workspace(), new RegExp(`data-action="${action}"[^>]*disabled`));
    await view.dispatch(action); assert.equal(view.dialog.open, false);
  }
  assert.equal(view.calls.filter(call => call.path.endsWith('/actions')).length, 0);
});

const budgetKeys = ['max_transactions', 'max_outpoints', 'max_requests', 'max_seconds', 'max_new_items'];
const noCaps = /No transaction, output lookup, API request, time, or new Miro item cap/;

test('fresh defaults and legacy investigations have no automatic budgets despite stored finite values', async () => {
  const view = await harness();
  assert.equal(view.state.settings.budget_limits_enabled, false);
  assert.equal(view.state.draft.settings.budget_limits_enabled, false);
  view.state.page = 'settings';
  for (const key of budgetKeys) assert.doesNotMatch(view.settingsPage(), new RegExp(`name="${key}"`));
  assert.doesNotMatch(view.settingsPage(), /name="budget_limits_enabled"[^>]*checked/);
  view.state.settings.budget_limits_enabled = true;
  view.state.activeCase = workflowCase(); view.state.page = 'case-settings';
  assert.match(view.settingsPage(), /Use optional run budgets/);
  assert.match(view.settingsPage(), noCaps);
  for (const key of budgetKeys) assert.doesNotMatch(view.settingsPage(), new RegExp(`name="${key}"`));
  assert.match(view.settingsPage(), /name="hops"/);
  assert.equal(view.state.activeCase.run_defaults.max_transactions, 20);
});

for (const caseSettings of [false, true]) {
  test(`${caseSettings ? 'investigation' : 'workspace'} budgets toggle without losing numeric preferences or unrelated settings`, async () => {
    const detail = workflowCase({run_defaults: {...defaults, ...layoutDefaults, budget_limits_enabled: false, center_name: 'Treasury'}});
    const view = await harness(path => path === '/api/cases/case1/overview' ? detail : undefined);
    view.state.activeCase = detail;
    view.navigate(caseSettings ? 'case-settings' : 'settings');
    const base = {name: 'Preserved name', hops: '8', budget_limits_enabled_present: '1'};
    view.changeBudgetSettings({...base, budget_limits_enabled: 'on'});
    for (const key of budgetKeys) assert.match(view.settingsPage(), new RegExp(`name="${key}"[^>]*min="0"`));
    const edited = {max_transactions: '37', max_outpoints: '81', max_requests: '92', max_seconds: '0.5', max_new_items: '1200'};
    view.changeBudgetSettings({...base, ...edited});
    for (const key of budgetKeys) assert.doesNotMatch(view.settingsPage(), new RegExp(`name="${key}"`));
    view.changeBudgetSettings({...base, budget_limits_enabled: 'on'});
    for (const key of budgetKeys) assert.match(view.settingsPage(), new RegExp(`name="${key}"[^>]*value="${edited[key]}"`));
    view.changeBudgetSettings(base);
    await view.submitSettings(base);
    const request = view.calls.find(call => call.path === (caseSettings ? '/api/cases/case1/settings' : '/api/settings'));
    assert.equal(request.body.settings.budget_limits_enabled, false);
    assert.equal(request.body.settings.hops, 8);
    for (const key of budgetKeys) assert.equal(request.body.settings[key], Number(edited[key]));
    if (caseSettings) {
      assert.equal(request.body.name, 'Preserved name');
      assert.equal(request.body.settings.center_name, 'Treasury');
    }
  });
}

test('enabled budgets permit per-cap zero and summarize unlimited values accurately', async () => {
  const limits = {...defaults, ...Object.fromEntries(budgetKeys.map(key => [key, 0])), budget_limits_enabled: true};
  const detail = workflowCase({run_defaults: limits, miro_board: 'board'});
  const view = await harness(path => path === '/api/cases/case1/overview' ? detail : undefined);
  view.state.activeCase = detail; view.state.page = 'case-settings';
  for (const key of budgetKeys) assert.match(view.settingsPage(), new RegExp(`name="${key}"[^>]*min="0"[^>]*value="0"`));
  assert.equal((view.settingsPage().match(/0 = unlimited/g) || []).length, 5);
  await view.submitSettings({name: detail.name, budget_limits_enabled_present: '1', budget_limits_enabled: 'on',
    ...Object.fromEntries(budgetKeys.map(key => [key, '0']))});
  const saved = view.calls.find(call => call.path.endsWith('/settings')).body.settings;
  assert.equal(saved.budget_limits_enabled, true);
  for (const key of budgetKeys) assert.equal(saved[key], 0);
  view.state.page = 'case'; view.state.caseView = 'collect';
  assert.equal((view.workspace().match(/<strong>Unlimited<\/strong>/g) || []).length, 5);
  assert.doesNotMatch(view.workspace(), /Up to <strong>0/);
  await view.dispatch('miro-sync-dialog');
  assert.match(view.dialog.innerHTML, /No new Miro item cap/);
});

test('private and shared collection dialogs describe unlimited legacy budgets while retaining hop and stop boundaries', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({shared_collection: sharedCollection()}); view.state.page = 'case';
  assert.match(view.workspace(), noCaps);
  assert.match(view.pegoutsGraph(view.state.activeCase), noCaps);
  for (const action of ['trace-dialog', 'shared-collect-dialog', 'shared-continue-dialog']) {
    await view.dispatch(action);
    assert.match(view.dialog.innerHTML, noCaps);
    assert.match(view.dialog.innerHTML, /Hop limits and explicit stop rules still apply/);
    assert.match(view.dialog.innerHTML, /name="hops"/);
    for (const key of budgetKeys) assert.doesNotMatch(view.dialog.innerHTML, new RegExp(`name="${key}"`));
    view.dialog.close();
  }
});

for (const flag of [undefined, false]) {
  test(`board rebuild with ${flag === undefined ? 'legacy missing' : 'disabled'} budgets hides override and submits unlimited`, async () => {
    const view = await harness(path => path.endsWith('/actions') ? {id: 'unlimited-rebuild', status: 'running'} : undefined);
    view.state.activeCase = workflowCase({miro_board: 'existing', run_defaults: {...defaults,
      ...(flag === undefined ? {} : {budget_limits_enabled: flag})}});
    view.state.page = 'case'; view.state.caseView = 'boards';
    await view.dispatch('miro-rebuild-dialog');
    assert.match(view.dialog.innerHTML, /No new Miro item cap/);
    assert.doesNotMatch(view.dialog.innerHTML, /name="max_new_items"/);
    await view.submitDialog({board_name: 'Unlimited rebuild', max_new_items: '750'});
    assert.equal(view.calls.find(call => call.path.endsWith('/actions')).body.max_new_items, 0);
  });
}


test('Starter connection hop limit validates integers and can be increased without fetching', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'limited-starters', status: 'running'} : undefined);
  view.state.activeCase = workflowCase();
  await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'connections'}});
  for (const invalid of ['', '-1', '1.5', 'bad', '2147483648']) {
    workflowEdit(view, 'connection-max-hops', invalid);
    await assert.rejects(view.dispatch('workflow-plot'), /whole-number hop limits/);
  }
  for (const valid of ['0', '3', '15', '2147483647']) {
    workflowEdit(view, 'connection-max-hops', valid);
    await view.dispatch('workflow-plot');
    assert.equal(view.calls.at(-1).body.max_hops, Number(valid));
    assert.equal(view.calls.at(-1).body.connection_scope, 'hop_limited');
    assert.equal(view.calls.at(-1).body.min_hops, 0);
    assert.equal(view.state.job.live, false);
    view.state.jobs.clear();
  }
  assert.deepEqual(view.calls.filter(call => call.body).map(call => call.body.action), ['plot', 'plot', 'plot', 'plot']);
});

for (const scope of ['all_saved', 'shortest']) test(`${scope} connections ignores hidden hop values and restores the bounded draft when toggled`, async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'all-starters', status: 'running'} : undefined);
  view.state.activeCase = workflowCase();
  await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'connections'}});
  workflowEdit(view, 'connection-max-hops', 'bad');
  workflowEdit(view, 'connection-scope', scope);
  assert.doesNotMatch(view.workspace(), /id="workflow-connection-max-hops"/);
  assert.match(view.workspace(), new RegExp(`value="${scope}" selected`));
  if (scope === 'shortest') {
    assert.match(view.workspace(), /one shortest saved route per connected ordered pair/);
    assert.match(view.workspace(), /searches all saved evidence without a hop cutoff/);
    assert.doesNotMatch(view.workspace(), /Increase the connection hop limit or select All saved connections to search further/);
  }
  await view.dispatch('workflow-plot');
  assert.equal(view.calls.at(-1).body.connection_scope, scope);
  assert.equal(view.calls.at(-1).body.max_hops, 0);
  view.state.jobs.clear();
  workflowEdit(view, 'connection-scope', 'hop_limited');
  assert.match(view.workspace(), /id="workflow-connection-max-hops"[^>]*value="bad"/);
  await assert.rejects(view.dispatch('workflow-plot'), /whole-number hop limits/);
});

test('Starter scope and limit survive source and case switches independently of peg-out bounds', async () => {
  const alpha = workflowCase({shared_collection: sharedCollection()}), beta = workflowCase({id: 'case2'});
  const view = await harness(path => path === '/api/cases/case1/overview' ? alpha : path === '/api/cases/case2/overview' ? beta : undefined);
  await view.openCase('case1');
  await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'connections'}});
  workflowEdit(view, 'connection-max-hops', '6');
  workflowEdit(view, 'data-source', 'shared');
  workflowEdit(view, 'shared-run', 'shared-old');
  await view.openCase('case2');
  assert.equal(view.currentWorkflow(beta).connectionScope, 'hop_limited');
  assert.equal(view.currentWorkflow(beta).connectionMaxHops, '10');
  workflowEdit(view, 'connection-scope', 'all_saved');
  await view.openCase('case1');
  assert.equal(view.currentWorkflow(alpha).connectionScope, 'hop_limited');
  assert.equal(view.currentWorkflow(alpha).connectionMaxHops, '6');
  assert.equal(view.currentWorkflow(alpha).sharedRun, 'shared-old');
  workflowEdit(view, 'data-source', 'investigation');
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  assert.equal(view.currentWorkflow(alpha).maxHops, '10');
  workflowEdit(view, 'max-hops', '12');
  await view.dispatch('plot-goal', {dataset: {goal: 'connections'}});
  assert.match(view.workspace(), /id="workflow-connection-max-hops"[^>]*value="6"/);
});

test('Starter board updates restore legacy and modern scope while preserving each destination draft', async () => {
  const plots = [workflowPlot('connections', 'all', {max_hops: null, query: {connection_scope: 'all_saved'}}),
    workflowPlot('connections', 'legacy', {max_hops: 3}),
    workflowPlot('connections', 'limited', {max_hops: 7, query: {connection_scope: 'hop_limited'},
      collection_source: {kind: 'shared', dataset_id: 'pool', run_id: 'shared-old'}})];
  const detail = workflowCase({shared_collection: sharedCollection(), plots,
    boards: plots.map(plot => workflowBoard('connections', plot.preview_id, {preview_id: plot.preview_id}))});
  const view = await harness(path => path.endsWith('/actions') ? {id: 'starter-update', status: 'running'} : undefined);
  view.state.activeCase = detail; await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'connections'}});
  workflowEdit(view, 'connection-max-hops', '4');
  await view.dispatch('workflow-board-prepare', boardControl('all'));
  assert.equal(view.currentWorkflow(detail).connectionScope, 'all_saved');
  assert.doesNotMatch(view.workspace(), /id="workflow-connection-max-hops"/);
  workflowEdit(view, 'layout-board', 'legacy');
  assert.equal(view.currentWorkflow(detail).connectionScope, 'hop_limited');
  assert.equal(view.currentWorkflow(detail).connectionMaxHops, '3');
  workflowEdit(view, 'connection-max-hops', '5');
  workflowEdit(view, 'layout-board', 'limited');
  assert.equal(view.currentWorkflow(detail).connectionMaxHops, '7');
  assert.equal(view.currentWorkflow(detail).dataSource, 'shared');
  await view.dispatch('workflow-plot-sync');
  assert.equal(view.calls.at(-1).body.action, 'plot-sync');
  assert.equal(view.calls.at(-1).body.connection_scope, 'hop_limited');
  assert.equal(view.calls.at(-1).body.max_hops, 7);
  assert.equal(view.calls.at(-1).body.run_id, 'shared-old');
  assert.equal(view.calls.at(-1).body.data_source, 'shared');
  assert.equal(view.calls.at(-1).body.board_record_id, 'limited');
  view.state.jobs.clear();
  workflowEdit(view, 'layout-board', 'legacy');
  assert.equal(view.currentWorkflow(detail).connectionMaxHops, '5');
  workflowEdit(view, 'layout-board', 'all');
  assert.equal(view.currentWorkflow(detail).connectionScope, 'all_saved');
  workflowEdit(view, 'layout-mode', 'fresh');
  assert.equal(view.currentWorkflow(detail).connectionScope, 'hop_limited');
  assert.equal(view.currentWorkflow(detail).connectionMaxHops, '4');
});

test('Starter new-board generation includes selected connection bound with shared provenance', async () => {
  const detail = workflowCase({shared_collection: sharedCollection()});
  const view = await harness(path => path.endsWith('/actions') ? {id: 'starter-create', status: 'running'} : undefined);
  view.state.activeCase = detail; await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'connections'}});
  workflowEdit(view, 'data-source', 'shared'); workflowEdit(view, 'shared-run', 'shared-old');
  workflowEdit(view, 'connection-max-hops', '8');
  workflowEdit(view, 'board-name', 'Eight-hop starter connections');
  await view.dispatch('workflow-plot-sync');
  assert.deepEqual(view.calls.at(-1).body, {action: 'plot-sync', goal: 'connections', run_id: 'shared-old',
    min_hops: 0, max_hops: 8, connection_scope: 'hop_limited', layout_mode: 'fresh',
    data_source: 'shared', dataset_id: 'pool', name: 'Eight-hop starter connections', layout_settings: layoutDefaults});
});


test('empty Starter preview suggests broadening the search before collecting more data', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({plots: [workflowPlot('connections', 'empty-starters', {
    empty: true, node_count: 0, edge_count: 0, max_hops: 3, layout_mode: 'fresh',
    query: {connection_scope: 'hop_limited'},
  })]});
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /No starter connections found within this scope/);
  assert.match(view.workspace(), /Increase the connection hop limit or choose All saved connections; collect more only if evidence is missing/);
  assert.doesNotMatch(view.workspace(), /No matching paths were found in this saved data/);
});


for (const action of ['lookup', 'change-output-lookup']) {
  for (const sessionState of ['deferred', 'failed']) {
    test(`output lookup completion renders ${action} without a ${sessionState} workspace refresh`, async () => {
      let releaseSession;
      const sessionWait = new Promise(resolve => {releaseSession = resolve;});
      const session = {csrf: 'test', settings: defaults, cases: []};
      let sessionReads = 0, changeLoaded = false;
      const caseId = action === 'lookup' ? null : 'output-case';
      const task = activeTask('ready-output-job', caseId, {action, live: true});
      const transaction = {txid, outputs: [{vout: 0, selectable: true,
        address: 'ready-output-address', value_text: '125000', asset: 'asset'}]};
      const result = action === 'lookup' ? {transactions: [transaction]} : transaction;
      const view = await harness(path => {
        if (path === '/api/session') {
          if (++sessionReads === 1) return session;
          return sessionState === 'deferred' ? sessionWait : {error: 'Unrelated workspace refresh failed'};
        }
        if (path === '/api/lookup' || path === '/api/cases/output-case/actions') return task;
        if (path === '/api/jobs/ready-output-job') return {...task, status: 'succeeded', result};
      }, {changeOutputHandlers: {
        // Component behavior is covered by change-outputs.test.mjs. This marker
        // proves the real app rendered after applying the completion callback.
        changeOutputsPanel() {return changeLoaded ? '<div>ready-change-output-address</div>' : '';},
        changeOutputsLookupComplete(id, jobId, value) {
          assert.equal(id, caseId); assert.equal(jobId, task.id);
          assert.equal(value.outputs[0].address, 'ready-output-address');
          changeLoaded = true; return true;
        },
      }});
      if (action === 'lookup') {
        view.navigate('new');
        view.newForm({name: 'Keep investigation draft', txids: txid, seeds: `${txid}:2`});
      } else {
        view.state.activeCase = multiCase(caseId);
        view.state.page = 'case-settings'; view.render();
      }
      await view.startJob(action === 'lookup' ? '/api/lookup' : '/api/cases/output-case/actions',
        {action, txids: txid}, action, true, caseId ?? undefined);
      let settled = false;
      const polling = view.pollJob(task.id).then(() => {settled = true;});
      try {
        await new Promise(setImmediate);
        assert.match(view.app.innerHTML, action === 'lookup' ? /ready-output-address/ : /ready-change-output-address/,
          'Ready output rows must render before any unrelated session request resolves');
        assert.equal(settled, true, 'The completion poll must finish promptly');
        assert.equal(sessionReads, 1, 'A read-only lookup must not refresh the global workspace');
        assert.equal(view.state.jobs.get(task.id).status, 'succeeded');
        assert.equal(view.state.jobs.get(task.id).outcomeError, undefined);
        assert.equal(view.isBusy(), false);
        if (action === 'lookup') {
          assert.equal(view.state.draft.name, 'Keep investigation draft');
          assert.equal(view.state.draft.seeds, `${txid}:2`);
          assert.equal(view.state.draft.selected.size, 0);
        } else assert.equal(view.state.draft.reports.length, 0);
      } finally {
        releaseSession(session);
        await polling;
      }
    });
  }
}

const stagedSections = (overrides = {}) => ({collection: 'unloaded', shared: 'unloaded', workflow: 'unloaded', boards: 'unloaded', history: 'unloaded', ...overrides});
const stagedOverview = (overrides = {}) => ({id: 'case1', name: 'Staged investigation', run_defaults: {...defaults, include_fees: true, hub_addresses: []},
  seeds: [`${txid}:0`], latest_run: 'saved1', runs: [{id: 'saved1', status: 'Summary loading', summary_pending: true}],
  sections: stagedSections(), ...overrides});
const sectionResult = (section, data = {}, status = 'ready') => ({...data, sections: {[section]: status}});
const settled = () => new Promise(setImmediate);

test('staged opening renders the overview while only collection and shared summaries load', async () => {
  const collection = Promise.withResolvers(), shared = Promise.withResolvers();
  const view = await harness(path => {
    if (path.endsWith('/overview')) return stagedOverview();
    if (path.endsWith('/collection')) return collection.promise;
    if (path.endsWith('/shared-collection')) return shared.promise;
    if (path !== '/api/session') throw Error(`Unexpected eager request: ${path}`);
  });
  await view.openCase('case1');
  assert.equal(view.state.page, 'case');
  assert.equal(view.state.activeCase.name, 'Staged investigation');
  assert.equal(view.state.openingCase, null);
  assert.match(view.app.innerHTML, /data-loading-section="collection"/);
  assert.match(view.app.innerHTML, /data-loading-section="shared"/);
  assert.doesNotMatch(view.app.innerHTML, /No shared collection yet|Your first completed or bounded run/);
  view.loadVisibleSections(); view.loadVisibleSections();
  assert.equal(view.calls.filter(call => call.path.endsWith('/collection')).length, 1);
  assert.equal(view.calls.filter(call => call.path.endsWith('/shared-collection')).length, 1);
  assert.ok(!view.calls.some(call => /\/(?:workflow|boards|history)$/.test(call.path)));
  collection.resolve(sectionResult('collection', {runs: [{id: 'saved1', status: 'bounded_complete', transaction_count: 20}], latest_run: 'saved1'}));
  shared.resolve(sectionResult('shared', {shared_collection: sharedCollection()}));
  await settled();
  assert.match(view.app.innerHTML, /20/);
  assert.doesNotMatch(view.app.innerHTML, /data-loading-section="collection"|data-loading-section="shared"/);
  assert.ok(!view.calls.some(call => /^\/api\/cases\/[^/]+$/.test(call.path)));
});

test('plots and board records load independently and legacy history waits for its tab', async () => {
  const workflow = Promise.withResolvers(), boards = Promise.withResolvers(), history = Promise.withResolvers();
  const view = await harness(path => {
    if (path.endsWith('/overview')) return stagedOverview();
    if (path.endsWith('/collection')) return sectionResult('collection', {latest_run: 'saved1', runs: [{id: 'saved1', status: 'complete'}]});
    if (path.endsWith('/shared-collection')) return sectionResult('shared', {shared_collection: sharedCollection()});
    if (path.endsWith('/workflow')) return workflow.promise;
    if (path.endsWith('/boards')) return boards.promise;
    if (path.endsWith('/history')) return history.promise;
  });
  await view.openCase('case1'); await settled();
  await view.dispatch('view-plots');
  assert.equal(view.state.caseView, 'plots');
  assert.match(view.app.innerHTML, /data-loading-section="workflow"/);
  assert.ok(!view.calls.some(call => call.path.endsWith('/history')));
  workflow.resolve(sectionResult('workflow', {plots: []})); await settled();
  assert.match(view.app.innerHTML, /id="saved-plots-panel"/);
  assert.match(view.app.innerHTML, /data-loading-section="boards"/);
  assert.match(view.app.innerHTML, /data-action="workflow-plot-sync"[^>]*disabled/);
  assert.doesNotMatch(view.app.innerHTML, /No boards yet/);
  boards.resolve(sectionResult('boards', {boards: []})); await settled();
  assert.match(view.app.innerHTML, /No boards yet/);
  await view.dispatch('view-history');
  assert.match(view.app.innerHTML, /data-loading-section="history"/);
  assert.equal(view.calls.filter(call => call.path.endsWith('/history')).length, 1);
  history.resolve(sectionResult('history', {artifacts: {}, pegout_searches: []})); await settled();
  assert.match(view.app.innerHTML, /Run history/);
  await view.dispatch('view-plots'); await view.dispatch('view-history');
  assert.equal(view.calls.filter(call => call.path.endsWith('/history')).length, 1);
});

test('summary retries preserve remembered runs and ignore responses after changing investigations', async () => {
  const collection = Promise.withResolvers();
  let collectionReads = 0;
  const storage = new Map([['liquid-tracer:investigation-tabs:v1', JSON.stringify({ids: ['case1'], views: [{id: 'case1', selectedRun: 'older', caseView: 'collect'}]})]]);
  const view = await harness(path => {
    if (path === '/api/session') return {csrf: 'test', settings: defaults, cases: [stagedOverview()]};
    if (path === '/api/cases/case1/overview') return stagedOverview();
    if (path === '/api/cases/case1/collection/older') return ++collectionReads === 1
      ? sectionResult('collection', {runs: [{id: 'older', status: 'Summary loading', summary_pending: true}]}, 'loading') : collection.promise;
    if (path === '/api/cases/case2/overview') return stagedOverview({id: 'case2', name: 'Second investigation', sections: stagedSections({collection: 'ready', shared: 'ready'})});
    if (path.endsWith('/shared-collection')) return sectionResult('shared', {shared_collection: sharedCollection()});
  }, {storage});
  await view.openCase('case1'); await settled();
  assert.equal(view.state.selectedRun, 'older');
  const retry = [...view.timers.values()].find(timer => timer.ms === 1000);
  assert.ok(retry); retry.fn(); await settled();
  assert.equal(collectionReads, 2);
  await view.openCase('case2');
  collection.resolve(sectionResult('collection', {latest_run: 'saved1', runs: [{id: 'older', status: 'complete'}]}));
  await settled();
  assert.equal(view.state.activeCase.id, 'case2');
  assert.equal(view.state.activeCase.name, 'Second investigation');
});

test('section failure remains local and retry does not reopen the investigation', async () => {
  let fail = true;
  const view = await harness(path => {
    if (path.endsWith('/overview')) return stagedOverview();
    if (path.endsWith('/collection')) return fail ? {error: 'Summary unavailable'} : sectionResult('collection', {runs: [{id: 'saved1', status: 'complete'}]});
    if (path.endsWith('/shared-collection')) return sectionResult('shared', {shared_collection: sharedCollection()});
  });
  await view.openCase('case1'); await settled();
  assert.equal(view.state.page, 'case'); assert.equal(view.state.error, '');
  assert.match(view.app.innerHTML, /Summary unavailable/);
  assert.match(view.app.innerHTML, /data-action="retry-case-section"/);
  fail = false; await view.dispatch('retry-case-section', {dataset: {section: 'collection'}}); await settled();
  assert.doesNotMatch(view.app.innerHTML, /Summary unavailable|data-loading-section="collection"/);
  assert.equal(view.calls.filter(call => call.path.endsWith('/overview')).length, 1);
});

test('saved preview browsing and selection stay lightweight; preparing for Miro explicitly verifies only the chosen layout', async () => {
  const check = Promise.withResolvers();
  const first = {...workflowPlot('full', 'first', {layout_mode: 'fresh'}), validation_pending: true, reviewable: false,
    artifact: {downloads: [{name: 'graph.svg', url: '/files/unchecked.svg'}], preview_url: '/files/unchecked.html'}};
  const second = {...workflowPlot('full', 'second', {layout_mode: 'fresh'}), validation_pending: true, reviewable: false};
  const view = await harness(path => {
    if (path.endsWith('/overview')) return stagedOverview();
    if (path.endsWith('/collection')) return sectionResult('collection', {latest_run: 'saved1', runs: [{id: 'saved1', status: 'complete'}]});
    if (path.endsWith('/shared-collection')) return sectionResult('shared', {shared_collection: sharedCollection()});
    if (path.endsWith('/workflow')) return sectionResult('workflow', {plots: [first, second]});
    if (path.endsWith('/boards')) return sectionResult('boards', {boards: []});
    if (path.endsWith('/plots/first')) return check.promise;
    if (path.endsWith('/plots/second')) return {...second, reviewable: true, validation_pending: false};
  });
  await view.openCase('case1'); await settled(); await view.dispatch('view-plots'); await settled();
  assert.equal(view.calls.filter(call => call.path.includes('/plots/')).length, 0);
  assert.match(view.app.innerHTML, /Prepare for Miro/);
  assert.match(view.app.innerHTML, /href="\/files\/unchecked.html" target="_blank" rel="noopener noreferrer"/);
  assert.doesNotMatch(savedPreviewPanel(view), /<iframe|<img|src="\/files\//);
  assert.match(view.app.innerHTML, /data-action="workflow-board-create-sync"[^>]*disabled/);
  await assert.rejects(() => view.dispatch('workflow-board-create-sync'), /reviewed New board layout/);
  assert.ok(!view.calls.some(call => call.path.endsWith('/actions')));
  await view.dispatch('verify-saved-plot', {dataset: {preview: 'first', caseId: 'case1'}}); await settled();
  assert.match(view.app.innerHTML, /Preparing this saved preview for Miro/);
  workflowEdit(view, 'plot-picker', 'second'); await settled();
  assert.equal(view.calls.filter(call => call.path.endsWith('/plots/second')).length, 0);
  check.resolve({...first, validation_pending: false, reviewable: true}); await settled();
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'second');
  assert.match(previewWriteButton(view), /disabled/);
  await view.dispatch('verify-saved-plot', {dataset: {preview: 'second', caseId: 'case1'}}); await settled();
  assert.equal(view.calls.filter(call => call.path.endsWith('/plots/second')).length, 1);
  workflowEdit(view, 'plot-picker', 'first');
  assert.equal(view.calls.filter(call => call.path.endsWith('/plots/first')).length, 1);
});

test('partial saved layout preferences keep independently loaded sections and current selection', async () => {
  const detail = workflowCase({sections: stagedSections({collection: 'ready', shared: 'ready', workflow: 'ready', boards: 'ready', history: 'ready'}),
    plots: [workflowPlot('full', 'kept')], boards: [workflowBoard('full', 'kept-board')], shared_collection: sharedCollection()});
  detail.run_defaults = {...detail.run_defaults, include_fees: true};
  const view = await harness(path => path.endsWith('/plot-settings') ? stagedOverview({run_defaults: {...detail.run_defaults, include_fees: false}}) : undefined);
  view.state.activeCase = detail; view.state.page = 'case'; view.state.caseView = 'plots'; view.state.selectedRun = 'saved1';
  view.plotForm({include_fees_present: '1'});
  await view.dispatch('plot-settings-save');
  assert.equal(view.state.activeCase.run_defaults.include_fees, false);
  assert.equal(view.state.activeCase.plots[0].preview_id, 'kept');
  assert.equal(view.state.activeCase.boards[0].id, 'kept-board');
  assert.equal(view.state.activeCase.shared_collection.dataset_id, detail.shared_collection.dataset_id);
  assert.equal(view.state.activeCase.sections.workflow, 'ready');
  assert.equal(view.state.selectedRun, 'saved1');
});

test('a ready current run stays usable while older history summaries remain pending', async () => {
  const view = await harness(path => {
    if (path.endsWith('/overview')) return stagedOverview({runs: [{id: 'saved1', status: 'complete', transaction_count: 8, collected_hops: 3}]});
    if (path.endsWith('/collection')) return sectionResult('collection', {runs: [
      {id: 'saved1', status: 'complete', transaction_count: 8, collected_hops: 3},
      {id: 'older', status: 'Summary loading', summary_pending: true},
    ]}, 'loading');
    if (path.endsWith('/shared-collection')) return sectionResult('shared', {shared_collection: sharedCollection()});
    if (path.endsWith('/workflow')) return sectionResult('workflow', {plots: []});
    if (path.endsWith('/boards')) return sectionResult('boards', {boards: []});
  });
  await view.openCase('case1'); await settled();
  assert.match(view.app.innerHTML, /id="collection-panel"/);
  assert.doesNotMatch(view.app.innerHTML, /data-loading-section="collection"/);
  await view.dispatch('view-plots'); await settled();
  const button = view.app.innerHTML.match(/<button[^>]+data-action="workflow-plot"[^>]*>/)[0];
  assert.doesNotMatch(button, /disabled/);
});

test('choosing an older private or shared snapshot requests only that summary', async () => {
  const privateRun = Promise.withResolvers(), sharedRun = Promise.withResolvers();
  const shared = sharedCollection({runs: [{id: 'shared-new', status: 'complete'}, {id: 'shared-old', status: 'Summary loading', summary_pending: true}]});
  const view = await harness(path => {
    if (path.endsWith('/overview')) return stagedOverview();
    if (path.endsWith('/collection')) return sectionResult('collection', {runs: [{id: 'saved1', status: 'complete'}, {id: 'older', status: 'Summary loading', summary_pending: true}]});
    if (path.endsWith('/collection/older')) return privateRun.promise;
    if (path.endsWith('/shared-collection')) return sectionResult('shared', {shared_collection: shared});
    if (path.endsWith('/shared-collection/shared-old')) return sharedRun.promise;
    if (path.endsWith('/workflow')) return sectionResult('workflow', {plots: []});
    if (path.endsWith('/boards')) return sectionResult('boards', {boards: []});
  });
  await view.openCase('case1'); await settled();
  view.changeRun('older'); await settled();
  assert.equal(view.state.selectedRun, 'older');
  assert.equal(view.calls.filter(call => call.path.endsWith('/collection/older')).length, 1);
  assert.match(view.app.innerHTML, /<strong[^>]*>Loading<\/strong>/);
  privateRun.resolve(sectionResult('collection', {runs: [{id: 'saved1', status: 'complete'}, {id: 'older', status: 'complete', collected_hops: 2}]})); await settled();
  assert.equal(view.state.selectedRun, 'older');
  await view.dispatch('view-plots'); await settled();
  workflowEdit(view, 'data-source', 'shared'); workflowEdit(view, 'shared-run', 'shared-old'); await settled();
  assert.equal(view.calls.filter(call => call.path.endsWith('/shared-collection/shared-old')).length, 1);
  assert.ok(!view.calls.some(call => call.path.endsWith('/history')));
  sharedRun.resolve(sectionResult('shared', {shared_collection: {...shared, runs: [{id: 'shared-old', status: 'complete'}]}})); await settled();
  assert.equal(view.currentWorkflow(view.state.activeCase).sharedRun, 'shared-old');
});

test('completed board task renders its result before overview, board and workspace refreshes finish', async () => {
  const overview = Promise.withResolvers(), boards = Promise.withResolvers(), session = Promise.withResolvers();
  let sessionReads = 0;
  const detail = workflowCase({sections: stagedSections({collection: 'ready', workflow: 'ready', shared: 'ready', boards: 'ready'}),
    boards: [workflowBoard('full', 'existing')]});
  const view = await harness(path => {
    if (path === '/api/session') return ++sessionReads === 1 ? {csrf: 'test', settings: defaults, cases: []} : session.promise;
    if (path.endsWith('/actions')) return activeTask('staged-board', 'case1', {action: 'board-sync'});
    if (path === '/api/jobs/staged-board') return {id: 'staged-board', status: 'succeeded', message: 'Board synced', result: {record_id: 'existing', board_id: 'miro-board'}};
    if (path.endsWith('/overview')) return overview.promise;
    if (path.endsWith('/boards')) return boards.promise;
  });
  view.state.activeCase = detail; view.state.page = 'case'; view.state.caseView = 'plots';
  await view.startJob('/api/cases/case1/actions', {action: 'board-sync', record_id: 'existing'}, 'board-sync', false, 'case1');
  const polling = view.pollJob('staged-board'); await settled();
  assert.equal(view.state.jobs.get('staged-board').status, 'succeeded');
  assert.equal(view.state.results.get('case1').action, 'board-sync');
  assert.match(view.app.innerHTML, /Miro board synced|Board synced|board-sync/);
  overview.resolve(stagedOverview()); await polling;
  assert.equal(view.calls.filter(call => call.path.endsWith('/boards')).length, 1);
  assert.equal(sessionReads, 2);
  assert.ok(!view.calls.some(call => call.path.endsWith('/history')));
  boards.resolve(sectionResult('boards', {boards: detail.boards}));
  session.resolve({csrf: 'test', settings: defaults, cases: []}); await settled();
});


test('overview refresh replaces an obsolete in-flight section request', async () => {
  const older = Promise.withResolvers(), newer = Promise.withResolvers();
  let boardReads = 0;
  const view = await harness(path => {
    if (path.endsWith('/overview')) return stagedOverview();
    if (path.endsWith('/collection')) return sectionResult('collection', {runs: [{id: 'saved1', status: 'complete'}]});
    if (path.includes('/shared-collection')) return sectionResult('shared', {shared_collection: sharedCollection()});
    if (path.endsWith('/workflow')) return sectionResult('workflow', {plots: []});
    if (path.endsWith('/boards')) return ++boardReads === 1 ? older.promise : newer.promise;
  });
  await view.openCase('case1'); await settled();
  await view.dispatch('view-plots'); await settled();
  assert.equal(boardReads, 1);
  await view.refreshCaseDetail('case1', () => false, 'plot'); await settled();
  assert.equal(boardReads, 2, 'A new overview must not reuse a request whose response will be rejected');
  newer.resolve(sectionResult('boards', {boards: [workflowBoard('full', 'new-board')]})); await settled();
  assert.equal(view.state.activeCase.sections.boards, 'ready');
  older.resolve(sectionResult('boards', {boards: [workflowBoard('full', 'old-board')]})); await settled();
  assert.equal(view.state.activeCase.boards[0].id, 'new-board');
  assert.doesNotMatch(view.app.innerHTML, /data-loading-section="boards"/);
});


test('invalidated selected summary keeps its identity but stops presenting stale counts as ready', async () => {
  let invalidated = false;
  const view = await harness(path => {
    if (path.endsWith('/overview')) return stagedOverview();
    if (path.endsWith('/collection')) return invalidated
      ? sectionResult('collection', {runs: [{id: 'saved1', status: 'Summary loading', summary_pending: true}]}, 'loading')
      : sectionResult('collection', {runs: [{id: 'saved1', status: 'complete', transaction_count: 8, collected_hops: 3}]});
    if (path.includes('/shared-collection')) return sectionResult('shared', {shared_collection: sharedCollection()});
  });
  await view.openCase('case1'); await settled();
  invalidated = true;
  await view.loadCaseSection('case1', 'collection', true);
  assert.equal(view.state.activeCase.runs[0].id, 'saved1');
  assert.equal(view.state.activeCase.runs[0].summary_pending, true);
  assert.match(view.app.innerHTML, /data-loading-section="collection"/);
});

test('shared collection appears immediately as an independent task during a slow read', async () => {
  const pending = Promise.withResolvers();
  const view = await harness(path => {
    if (path.endsWith('/overview')) return stagedOverview({sections: stagedSections({collection: 'ready'})});
    if (path.endsWith('/shared-collection')) return pending.promise;
  });
  await view.openCase('case1');
  assert.match(view.app.innerHTML, /1 active task/);
  assert.match(view.jobBanner(), /Loading shared collection information/);
  assert.match(view.jobBanner(), /data-job-elapsed="shared-load-/);
  assert.match(view.jobBanner(), /aria-label="Loading shared collection"/);
  assert.doesNotMatch(view.jobBanner(), /cancel-job|Proton Pass/);
  assert.equal(view.state.jobs.size, 0);
  assert.equal(view.runningJobs().length, 0);
  assert.equal(view.isBusy(), false);
  assert.equal(view.actionBusy('shared-trace'), false);
  assert.equal(view.actionBusy('plot', {layout_mode: 'fresh'}), false);
  view.loadVisibleSections(); view.loadVisibleSections();
  assert.equal(view.calls.filter(call => call.path.endsWith('/shared-collection')).length, 1);
  pending.resolve(sectionResult('shared', {shared_collection: sharedCollection()})); await settled();
  assert.doesNotMatch(view.jobBanner(), /active task/);
  assert.match(view.jobBanner(), /Shared collection information loaded/);
  const id = view.jobBanner().match(/data-task="([^"]+)"/)[1];
  await view.dispatch('dismiss-job', {dataset: {id}});
  assert.equal(view.jobBanner(), '<div id="job-tasks"></div>');
});

test('shared preparation stays in tasks between retries and finishes after navigating away', async () => {
  let reads = 0;
  const pending = Promise.withResolvers();
  const view = await harness(path => {
    if (path.endsWith('/overview')) return stagedOverview({sections: stagedSections({collection: 'ready'})});
    if (path.endsWith('/shared-collection')) return ++reads === 1
      ? sectionResult('shared', {shared_collection: sharedCollection()}, 'loading') : pending.promise;
  });
  await view.openCase('case1'); await settled();
  const id = view.jobBanner().match(/data-task="([^"]+)"/)[1];
  assert.match(view.jobBanner(), /1 active task/);
  assert.match(view.jobBanner(), /Preparing saved shared collection summaries/);
  view.navigate('dashboard');
  const timer = [...view.timers.entries()].find(([, timer]) => timer.ms === 1000);
  assert.ok(timer); view.timers.delete(timer[0]); timer[1].fn(); await settled();
  assert.equal(reads, 2);
  assert.match(view.jobBanner(), new RegExp(`data-task="${id}"`));
  assert.match(view.jobBanner(), /1 active task/);
  pending.resolve(sectionResult('shared', {shared_collection: sharedCollection()})); await settled();
  assert.equal(view.state.page, 'dashboard');
  assert.doesNotMatch(view.jobBanner(), /active task/);
  assert.match(view.app.innerHTML, /Shared collection information loaded/);
  assert.ok(!view.calls.some(call => call.path.startsWith('/api/jobs/shared-load-')));
});

test('shared task survives switching investigation views without duplicating the read', async () => {
  const pending = Promise.withResolvers();
  const view = await harness(path => {
    if (path.endsWith('/overview')) return stagedOverview({sections: stagedSections({collection: 'ready', workflow: 'ready', boards: 'ready'})});
    if (path.endsWith('/shared-collection')) return pending.promise;
  });
  await view.openCase('case1');
  const id = view.jobBanner().match(/data-task="([^"]+)"/)[1];
  await view.dispatch('view-plots');
  assert.equal(view.calls.filter(call => call.path.endsWith('/shared-collection')).length, 1);
  assert.equal((view.jobBanner().match(/data-task=/g) || []).length, 1);
  pending.resolve(sectionResult('shared', {shared_collection: sharedCollection()})); await settled();
  assert.equal(view.state.activeCase.sections.shared, 'ready');
  assert.equal(view.state.caseView, 'plots');
  assert.match(view.jobBanner(), new RegExp(`data-task="${id}"`));
  assert.doesNotMatch(view.jobBanner(), /active task/);
});

test('shared loading failures stay local, stop the task and can be retried', async () => {
  let fail = true;
  const view = await harness(path => {
    if (path.endsWith('/overview')) return stagedOverview({sections: stagedSections({collection: 'ready'})});
    if (path.endsWith('/shared-collection')) return fail ? {error: 'Shared disk unavailable'}
      : sectionResult('shared', {shared_collection: sharedCollection({compatible: false, reason: 'Different collection source'})});
  });
  await view.openCase('case1'); await settled();
  assert.equal(view.state.activeCase.sections.shared, 'error');
  assert.equal(view.state.error, '');
  assert.match(view.jobBanner(), /task-failed/);
  assert.match(view.jobBanner(), /Shared disk unavailable/);
  assert.doesNotMatch(view.jobBanner(), /active task/);
  fail = false; await view.dispatch('retry-case-section', {dataset: {section: 'shared'}}); await settled();
  assert.equal(view.state.activeCase.sections.shared, 'ready');
  assert.match(view.jobBanner(), /Shared collection information loaded/);
  assert.doesNotMatch(view.jobBanner(), /task-failed|Shared disk unavailable/);
  assert.equal((view.jobBanner().match(/data-task=/g) || []).length, 1);
});

test('shared preparation retry exhaustion ends the task instead of spinning forever', async () => {
  let reads = 0;
  const view = await harness(path => {
    if (path.endsWith('/overview')) return stagedOverview({sections: stagedSections({collection: 'ready'})});
    if (path.endsWith('/shared-collection')) {
      reads++; return sectionResult('shared', {shared_collection: sharedCollection()}, 'loading');
    }
  });
  await view.openCase('case1'); await settled();
  for (let i = 0; i < 60; i++) {
    const next = [...view.timers.entries()].find(([, timer]) => timer.ms === (i ? 5000 : 1000));
    assert.ok(next, `retry ${i + 1} scheduled`);
    view.timers.delete(next[0]); next[1].fn(); await settled();
  }
  assert.equal(reads, 61);
  assert.equal(view.state.activeCase.sections.shared, 'error');
  assert.match(view.jobBanner(), /still being prepared/);
  assert.doesNotMatch(view.jobBanner(), /active task/);
  assert.ok(![...view.timers.values()].some(timer => timer.ms === 1000 || timer.ms === 5000));
});

test('a superseded shared snapshot read cannot finish the newer task or overwrite its data', async () => {
  const older = Promise.withResolvers(), newer = Promise.withResolvers();
  const view = await harness(path => {
    if (path.endsWith('/overview')) return stagedOverview({sections: stagedSections({collection: 'ready'})});
    if (path.endsWith('/shared-collection')) return older.promise;
    if (path.endsWith('/shared-collection/shared-old')) return newer.promise;
  });
  await view.openCase('case1');
  view.currentWorkflow(view.state.activeCase).sharedRun = 'shared-old';
  const loading = view.loadCaseSection('case1', 'shared', true);
  const id = view.jobBanner().match(/data-task="([^"]+)"/)[1];
  older.resolve(sectionResult('shared', {shared_collection: sharedCollection({name: 'Obsolete data'})})); await settled();
  assert.match(view.jobBanner(), /1 active task/);
  assert.equal((view.jobBanner().match(/data-task=/g) || []).length, 1);
  assert.match(view.jobBanner(), new RegExp(`data-task="${id}"`));
  assert.notEqual(view.state.activeCase.shared_collection?.name, 'Obsolete data');
  newer.resolve(sectionResult('shared', {shared_collection: sharedCollection({name: 'Selected data'})})); await loading;
  assert.equal(view.state.activeCase.shared_collection.name, 'Selected data');
  assert.doesNotMatch(view.jobBanner(), /active task/);
});

test('shared loading and real graph jobs coexist without changing conflicts or cancellation', async () => {
  const pending = Promise.withResolvers();
  const view = await harness(path => {
    if (path.endsWith('/overview')) return stagedOverview({sections: stagedSections({collection: 'ready'})});
    if (path.endsWith('/shared-collection')) return pending.promise;
  });
  await view.openCase('case1');
  view.state.jobs.set('board-task', {id: 'board-task', action: 'board-sync', caseId: 'case1', status: 'running',
    resource_kind: 'board', resource_key: 'miro-one', started: Date.now(), message: 'Writing graph',
    live: false, cancellable: true, cancelling: false});
  assert.equal(view.runningJobs().length, 1);
  assert.match(view.jobBanner(), /2 active tasks/);
  assert.match(view.jobBanner(), /1 loading/);
  assert.match(view.jobBanner(), /data-action="cancel-job" data-id="board-task"/);
  assert.doesNotMatch(view.jobBanner(), /data-action="cancel-job" data-id="shared-load-/);
  assert.equal(view.actionBusy('plot', {layout_mode: 'fresh'}), false);
  assert.equal(view.actionBusy('shared-trace'), false);
  assert.equal(view.actionBusy('lookup'), true, 'An existing exclusive conflict must still be enforced');
  pending.resolve(sectionResult('shared', {shared_collection: sharedCollection()})); await settled();
  assert.match(view.jobBanner(), /1 active task/);
  assert.equal(view.state.jobs.get('board-task').status, 'running');
});

test('an overview refresh replaces shared loading without letting the older result erase it', async () => {
  const older = Promise.withResolvers(), newer = Promise.withResolvers();
  let reads = 0;
  const view = await harness(path => {
    if (path.endsWith('/overview')) return stagedOverview({sections: stagedSections({collection: 'ready'})});
    if (path.endsWith('/shared-collection')) return ++reads === 1 ? older.promise : newer.promise;
  });
  await view.openCase('case1');
  const refresh = view.refreshCaseDetail('case1'); await settled();
  assert.equal(reads, 2);
  assert.equal((view.jobBanner().match(/data-task=/g) || []).length, 1);
  newer.resolve(sectionResult('shared', {shared_collection: sharedCollection({name: 'Refreshed collection'})})); await refresh;
  older.resolve(sectionResult('shared', {shared_collection: sharedCollection({name: 'Stale collection'})}, 'loading')); await settled();
  assert.equal(view.state.activeCase.shared_collection.name, 'Refreshed collection');
  assert.equal(view.state.activeCase.sections.shared, 'ready');
  assert.doesNotMatch(view.jobBanner(), /active task/);
  assert.ok(![...view.timers.values()].some(timer => timer.ms === 1000));
});

test('separate investigations retain their loading tasks and open the correct investigation', async () => {
  const one = Promise.withResolvers(), two = Promise.withResolvers();
  let firstDone = false;
  const view = await harness(path => {
    if (path === '/api/session') return {csrf: 'test', settings: defaults, cases: [
      stagedOverview({id: 'case1', name: 'First investigation'}), stagedOverview({id: 'case2', name: 'Second investigation'})]};
    if (path === '/api/cases/case1/overview') return stagedOverview({name: 'First investigation', sections: stagedSections({collection: 'ready'})});
    if (path === '/api/cases/case2/overview') return stagedOverview({id: 'case2', name: 'Second investigation', sections: stagedSections({collection: 'ready'})});
    if (path === '/api/cases/case1/shared-collection') return firstDone ? sectionResult('shared', {shared_collection: sharedCollection()}) : one.promise;
    if (path === '/api/cases/case2/shared-collection') return two.promise;
  });
  await view.openCase('case1');
  const firstId = view.jobBanner().match(/data-task="([^"]+)"/)[1];
  await view.openCase('case2');
  assert.match(view.jobBanner(), /2 active tasks/);
  assert.match(view.jobBanner(), /First investigation/);
  assert.match(view.jobBanner(), /Second investigation/);
  one.resolve(sectionResult('shared', {shared_collection: sharedCollection({name: 'Only first'})})); await settled();
  assert.equal(view.state.activeCase.id, 'case2');
  assert.notEqual(view.state.activeCase.shared_collection?.name, 'Only first');
  assert.match(view.jobBanner(), /1 active task/);
  firstDone = true;
  await view.dispatch('open-job', {dataset: {id: firstId}}); await settled();
  assert.equal(view.state.activeCase.id, 'case1');
  two.resolve(sectionResult('shared', {shared_collection: sharedCollection({name: 'Only second'})})); await settled();
  assert.notEqual(view.state.activeCase.shared_collection?.name, 'Only second');
  assert.doesNotMatch(view.jobBanner(), /active task/);
});


test('new layout generation defaults to Trace without reinterpreting saved previews', async () => {
  const legacy = {...defaults};
  delete legacy.layout_style;
  const view = await harness();
  view.state.activeCase = workflowCase({run_defaults: legacy});
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /value="trace" selected>Trace layout/);
  assert.match(view.workspace(), /<summary>Advanced layout comparison<\/summary>/);
  assert.match(view.workspace(), /<option value="standard">Standard · legacy comparison/);
  assert.equal(view.readSettings({values: {}}, {...legacy, hub_addresses: []}).layout_style, 'trace');
  assert.equal(view.readSettings({values: {}}, {...legacy, hub_addresses: [], layout_style: 'trace'}).layout_style, 'trace');
  assert.equal(view.calls.length, 1);
});

test('choosing Trace layout preserves independent grouping choices in both plot requests', async () => {
  for (const action of ['workflow-plot', 'workflow-plot-sync']) {
    const view = await harness(path => path.endsWith('/actions') ? {id: 'trace-layout-job', status: 'running'} : undefined);
    view.state.activeCase = workflowCase({run_defaults: {...defaults, include_fees: true, center_name: 'Treasury'}});
    await view.dispatch('view-plots');
    await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
    view.changeLayoutStyle('trace', {group_context_inputs_present: '1'});
    assert.doesNotMatch(contextGroupingFields(view), /name="group_context_inputs"[^>]*checked/);
    view.changeLayoutStyle('standard', {group_context_inputs_present: '1', group_context_inputs: 'on'});
    view.changeLayoutStyle('trace', {group_context_inputs_present: '1', group_context_inputs: 'on'});
    assert.match(contextGroupingFields(view), /name="group_context_inputs"[^>]*checked/);
    assert.match(view.workspace(), /name="center_name"[^>]*value="Treasury"/);
    assert.match(view.workspace(), /name="include_fees"[^>]*checked/);
    assert.equal(view.calls.length, 1, 'choosing a style does not submit work');
    view.editPlotSettings({group_context_inputs_present: '1', layout_style: 'trace'});
    view.elements.delete('#plot-layout-form');
    assert.doesNotMatch(contextGroupingFields(view), /name="group_context_inputs"[^>]*checked/);
    assert.match(view.workspace(), /value="trace" selected>Trace layout/);
    await view.dispatch(action);
    const request = view.calls.find(call => call.path.endsWith('/actions')).body;
    assert.equal(request.layout_settings.layout_style, 'trace');
    assert.equal(request.layout_settings.group_context_inputs, false);
    assert.equal(request.layout_settings.include_fees, true);
    assert.equal(request.layout_settings.center_name, 'Treasury');
    assert.equal(view.state.activeCase.run_defaults.layout_style, 'standard');
    assert.equal(view.calls.some(call => call.path.endsWith('/plot-settings')), false);
  }
});

test('Trace layout and an explicit grouping override survive save and reload', async () => {
  const detail = workflowCase();
  const respond = (path, body) => {
    if (path.endsWith('/plot-settings')) {detail.run_defaults = {...detail.run_defaults, ...body.settings}; return detail;}
    if (path === '/api/cases/case1/overview') return detail;
  };
  const view = await harness(respond);
  view.state.activeCase = detail;
  await view.dispatch('view-plots');
  view.changeLayoutStyle('trace', {group_context_inputs_present: '1'});
  await view.submitPlotSettings({layout_style: 'trace', group_context_inputs_present: '1'});
  const saved = view.calls.find(call => call.path.endsWith('/plot-settings')).body.settings;
  assert.equal(saved.layout_style, 'trace');
  assert.equal(saved.group_context_inputs, false);
  const restarted = await harness(respond);
  await restarted.openCase('case1');
  await restarted.dispatch('view-plots');
  assert.match(restarted.workspace(), /value="trace" selected>Trace layout/);
  assert.doesNotMatch(contextGroupingFields(restarted), /name="group_context_inputs"[^>]*checked/);
  restarted.editPlotSettings({center_name: 'Another group'});
  restarted.elements.delete('#plot-layout-form');
  assert.doesNotMatch(contextGroupingFields(restarted), /name="group_context_inputs"[^>]*checked/);
  assert.match(restarted.workspace(), /value="trace" selected>Trace layout/);
});

test('workspace Trace choice preserves grouping and existing investigation settings', async () => {
  const detail = workflowCase();
  const view = await harness((path, body) => path === '/api/settings' ? {settings: body.settings} : undefined);
  view.state.activeCase = detail;
  view.navigate('settings');
  const form = view.changeLayoutStyle('trace', {group_context_inputs_present: '1'}, true);
  assert.equal(form.values.group_context_inputs, undefined);
  await view.submitSettings(form.values);
  const saved = view.calls.find(call => call.path === '/api/settings').body.settings;
  assert.equal(saved.layout_style, 'trace');
  assert.equal(saved.group_context_inputs, false);
  assert.equal(detail.run_defaults.layout_style, 'standard');
});

test('legacy board style stays Standard despite Trace defaults and destination drafts retain overrides', async () => {
  const legacy = {...layoutDefaults};
  delete legacy.layout_style;
  const view = await harness();
  view.state.activeCase = workflowCase({run_defaults: {...defaults, layout_style: 'trace'},
    plots: [workflowPlot('pegouts', 'legacy', {layout_settings: legacy})],
    boards: [workflowBoard('pegouts', 'oldboard', {preview_id: 'legacy'})]});
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /value="trace" selected>Trace layout/);
  await view.dispatch('workflow-board-prepare', boardControl('oldboard'));
  assert.match(view.workspace(), /value="standard" selected>Standard · legacy comparison/);
  view.changeLayoutStyle('trace', {group_context_inputs_present: '1'});
  view.editPlotSettings({layout_style: 'trace', group_context_inputs_present: '1'});
  view.elements.delete('#plot-layout-form');
  workflowEdit(view, 'layout-mode', 'fresh');
  workflowEdit(view, 'layout-mode', 'update');
  assert.match(view.workspace(), /value="trace" selected>Trace layout/);
  assert.doesNotMatch(contextGroupingFields(view), /name="group_context_inputs"[^>]*checked/);
});

test('Trace layout labels saved workflow settings without invalidating legacy renderer previews', async () => {
  const view = await harness();
  const settings = {...defaults, ...layoutDefaults};
  const artifact = {...settings, preview_id: 'legacy-layout', downloads: [], preview_url: '/files/artifacts/graph.html', compaction: {unchanged: true}};
  delete artifact.layout_style;
  const detail = workflowCase({run_defaults: settings, miro_board: 'board', artifacts: {saved1: {compact: artifact}},
    plots: [workflowPlot('pegouts', 'traceplot', {layout_settings: {...layoutDefaults, layout_style: 'trace'}})]});
  view.state.activeCase = detail;
  settings.layout_style = 'trace';
  for (const html of [view.localGraph(artifact, true, settings), view.elkGraph(artifact, true, settings), view.compactGraph(artifact, true, detail)]) {
    assert.match(html, /<iframe/);
    assert.doesNotMatch(html, /different graph settings/);
  }
  assert.ok(view.currentCompaction());
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /<summary>Saved layout settings<\/summary><p>Trace layout/);
});


const savedPreviewPanel = view => view.workspace().match(/<section class="panel" id="saved-plots-panel"[\s\S]*?<\/section>/)[0];
const previewWriteControl = (preview = 'reviewed', caseId = 'case1') => ({dataset: {preview, caseId}});
const previewWriteButton = view => savedPreviewPanel(view).match(/<button[^>]*data-action="workflow-write-miro"[^>]*>/)?.[0];

test('Write to Miro publishes the selected saved preview without rerunning generation or saving changed settings', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'publish', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({plots: [workflowPlot('full', 'other', {layout_mode: 'fresh'}),
    workflowPlot('pegouts', 'reviewed', {layout_mode: 'fresh', input_snapshot_version: 1})]});
  await view.dispatch('view-plots');
  workflowEdit(view, 'plot-picker', 'reviewed');
  workflowEdit(view, 'preview-board-name', 'Reviewed result');
  workflowEdit(view, 'max-hops', '99');
  view.plotForm({connector_style: 'curved'});
  assert.match(savedPreviewPanel(view), /Later changes in the generation form do not change this saved preview/);
  await view.dispatch('workflow-write-miro', previewWriteControl());
  assert.deepEqual(view.calls.filter(call => call.body), [{path: '/api/cases/case1/actions',
    body: {action: 'board-create-sync', preview_id: 'reviewed', name: 'Reviewed result'}}]);
  assert.equal(view.state.job.live, true);
  assert.match(previewWriteButton(view), /disabled/);
  const count = view.calls.length;
  await view.dispatch('workflow-write-miro', previewWriteControl());
  assert.equal(view.calls.length, count, 'same publication cannot be queued twice');
});

test('Write to Miro binds an update to its saved destination, even when the form selects another board', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'publish', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({plots: [workflowPlot('pegouts', 'reviewed', {layout_mode: 'update',
    board_record_id: 'target', board_id: 'miro-target', input_snapshot_version: 1})],
    boards: [workflowBoard('pegouts', 'other'), workflowBoard('pegouts', 'target')]});
  await view.dispatch('view-plots');
  await view.dispatch('workflow-board-prepare', boardControl('other'));
  await view.dispatch('workflow-write-miro', previewWriteControl());
  assert.deepEqual(view.calls.filter(call => call.body), [{path: '/api/cases/case1/actions',
    body: {action: 'board-sync', record_id: 'target', preview_id: 'reviewed', reorganize: false}}]);
});

test('Write to Miro permits removal-only updates and preserves their exact saved preview', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'publish', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({plots: [workflowPlot('pegouts', 'reviewed', {layout_mode: 'update',
    board_record_id: 'target', board_id: 'miro-target', empty: true, node_count: 0, edge_count: 0})],
    boards: [workflowBoard('pegouts', 'target')]});
  await view.dispatch('view-plots');
  assert.doesNotMatch(previewWriteButton(view), /disabled/);
  await view.dispatch('workflow-write-miro', previewWriteControl());
  assert.equal(view.calls.at(-1).body.action, 'board-sync');
  assert.equal(view.calls.at(-1).body.preview_id, 'reviewed');
});

test('Write to Miro resumes a linked interrupted creation instead of creating a second board', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'resume', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({plots: [workflowPlot('full', 'reviewed', {layout_mode: 'fresh'})],
    boards: [workflowBoard('full', 'target', {status: 'interrupted', pending_count: 2,
      preview_id: 'reviewed', creation_preview_id: 'reviewed'})]});
  await view.dispatch('view-plots');
  assert.match(savedPreviewPanel(view), /Resume writing this exact preview/);
  await view.dispatch('workflow-write-miro', previewWriteControl());
  assert.deepEqual(view.calls.at(-1).body, {action: 'board-sync', record_id: 'target', preview_id: 'reviewed', reorganize: false});
});

test('Write to Miro retries a known rejection with the original board name', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'retry', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({plots: [workflowPlot('full', 'reviewed', {layout_mode: 'fresh'})],
    boards: [workflowBoard('full', 'target', {status: 'creation_rejected', board_id: null, can_sync: false,
      name: 'Original name', preview_id: 'reviewed', creation_preview_id: 'reviewed'})]});
  await view.dispatch('view-plots');
  workflowEdit(view, 'saved-board-name', 'Do not replace');
  await view.dispatch('workflow-write-miro', previewWriteControl());
  assert.deepEqual(view.calls.at(-1).body, {action: 'board-create-sync', preview_id: 'reviewed', name: 'Original name'});
});

for (const creationPreview of ['reviewed', 'another-preview']) {
  test(`Write to Miro blocks uncertain creation for ${creationPreview} and leaves its recovery controls available`, async () => {
    const view = await harness();
    view.state.activeCase = workflowCase({plots: [workflowPlot('full', 'reviewed', {layout_mode: 'fresh'})],
      boards: [workflowBoard('full', 'unknown', {status: 'pending_creation', board_id: null, can_sync: false,
        preview_id: creationPreview, creation_preview_id: creationPreview})]});
    await view.dispatch('view-plots');
    assert.match(previewWriteButton(view), /disabled/);
    assert.match(boardCardHtml(view, 'unknown'), /Link created board to this entry/);
    await assert.rejects(view.dispatch('workflow-write-miro', previewWriteControl()), /uncertain result|uncertain board creation/);
    assert.ok(!view.calls.some(call => call.path.endsWith('/actions')));
  });
}

for (const extra of [{validation_pending: true, reviewable: false}, {reviewable: false, reason: 'Saved source changed.'},
  {empty: true, node_count: 0}]) {
  test(`Write to Miro blocks unchecked, stale or empty previews: ${JSON.stringify(extra)}`, async () => {
    const view = await harness();
    view.state.activeCase = workflowCase({plots: [workflowPlot('full', 'reviewed', {layout_mode: 'fresh', ...extra})]});
    view.state.caseView = 'plots';
    assert.match(previewWriteButton(view), /disabled/);
    await assert.rejects(view.dispatch('workflow-write-miro', previewWriteControl()), /Check this saved|Saved source changed|valid preview/);
    assert.ok(!view.calls.some(call => call.path.endsWith('/actions')));
  });
}

test('Write to Miro waits for board records without blocking offline preview generation', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({sections: stagedSections({workflow: 'ready', collection: 'ready', boards: 'loading'}),
    plots: [workflowPlot('full', 'reviewed', {layout_mode: 'fresh'})]});
  view.state.caseView = 'plots';
  assert.match(previewWriteButton(view), /disabled/);
  assert.doesNotMatch(view.workspace().match(/<button[^>]*data-action="workflow-plot"[^>]*>/)[0], /disabled/);
  await assert.rejects(view.dispatch('workflow-write-miro', previewWriteControl()), /Waiting for Miro board records/);
  assert.ok(!view.calls.some(call => call.path.endsWith('/actions')));
});

for (const boards of [[], [workflowBoard('full', 'target', {board_id: 'changed-board'})], [workflowBoard('pegouts', 'target')]]) {
  test(`Write to Miro cannot redirect an update with a missing or mismatched destination: ${JSON.stringify(boards)}`, async () => {
    const view = await harness();
    view.state.activeCase = workflowCase({plots: [workflowPlot('full', 'reviewed', {layout_mode: 'update',
      board_record_id: 'target', board_id: 'miro-target'})], boards});
    view.state.caseView = 'plots';
    assert.match(previewWriteButton(view), /disabled/);
    await assert.rejects(view.dispatch('workflow-write-miro', previewWriteControl()), /destination captured by this preview is unavailable/);
    assert.ok(!view.calls.some(call => call.path.endsWith('/actions')));
  });
}

test('already synced preview opens its existing board and cannot create or sync it again', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({plots: [workflowPlot('full', 'reviewed', {layout_mode: 'fresh'})],
    boards: [workflowBoard('full', 'target', {status: 'synced', preview_id: 'reviewed', creation_preview_id: 'reviewed'})]});
  view.state.caseView = 'plots';
  assert.equal(previewWriteButton(view), undefined);
  assert.match(savedPreviewPanel(view), /Open synced Miro board/);
  await assert.rejects(view.dispatch('workflow-write-miro', previewWriteControl()), /already synced/);
  assert.ok(!view.calls.some(call => call.path.endsWith('/actions')));
});

test('an initial preview cannot overwrite its board after a later layout was written', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({plots: [workflowPlot('full', 'reviewed', {layout_mode: 'fresh'})],
    boards: [workflowBoard('full', 'target', {status: 'synced', preview_id: 'later', creation_preview_id: 'reviewed'})]});
  view.state.caseView = 'plots';
  assert.match(previewWriteButton(view), /disabled/);
  assert.match(savedPreviewPanel(view), /Open Miro board/);
  await assert.rejects(view.dispatch('workflow-write-miro', previewWriteControl()), /later saved layout/);
  assert.ok(!view.calls.some(call => call.path.endsWith('/actions')));
});

test('an old preview control cannot publish a newly selected layout or another investigation', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({plots: [workflowPlot('full', 'reviewed', {layout_mode: 'fresh'}),
    workflowPlot('full', 'later', {layout_mode: 'fresh'})]});
  view.state.caseView = 'plots';
  workflowEdit(view, 'plot-picker', 'later');
  await assert.rejects(view.dispatch('workflow-write-miro', previewWriteControl()), /selected preview changed/);
  await assert.rejects(view.dispatch('workflow-write-miro', previewWriteControl('later', 'other-case')), /selected preview changed/);
  assert.ok(!view.calls.some(call => call.path.endsWith('/actions')));
});

test('Write to Miro respects board resource conflicts while allowing independent boards', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'publish', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({plots: [workflowPlot('full', 'reviewed', {layout_mode: 'update',
    board_record_id: 'target', board_id: 'miro-target', input_snapshot_version: 1})],
    boards: [workflowBoard('full', 'target')]});
  view.state.caseView = 'plots';
  view.state.job = {id: 'busy', action: 'board-sync', caseId: 'case1', resource_kind: 'board', resource_key: 'miro-target'};
  assert.match(previewWriteButton(view), /disabled/);
  await view.dispatch('workflow-write-miro', previewWriteControl());
  assert.ok(!view.calls.some(call => call.path.endsWith('/actions')));
  view.state.job = {id: 'other', action: 'board-sync', caseId: 'case1', resource_kind: 'board', resource_key: 'miro-other'};
  assert.doesNotMatch(previewWriteButton(view), /disabled/);
  await view.dispatch('workflow-write-miro', previewWriteControl());
  assert.equal(view.calls.at(-1).body.action, 'board-sync');
});


test('publishing a saved new-board preview prepares subsequent previews to update that same board', async () => {
  const plot = workflowPlot('pegouts', 'reviewed', {layout_mode: 'fresh'});
  const board = workflowBoard('pegouts', 'target', {status: 'synced', preview_id: 'reviewed', creation_preview_id: 'reviewed'});
  const detail = workflowCase({plots: [plot], boards: [board]});
  const view = await harness(path => path === '/api/jobs/publish' ? {status: 'succeeded', result: {...board, record_id: board.id}}
    : path === '/api/cases/case1/overview' ? detail : undefined);
  view.state.activeCase = workflowCase({plots: [plot]});
  view.currentWorkflow(view.state.activeCase).plot = 'reviewed';
  view.state.job = {id: 'publish', action: 'board-create-sync', caseId: 'case1', live: true};
  await view.pollJob();
  const draft = view.currentWorkflow(view.state.activeCase);
  assert.equal(draft.layoutMode, 'update');
  assert.equal(draft.layoutBoard, 'target');
  assert.equal(draft.goal, 'pegouts');
  assert.equal(draft.plot, 'reviewed');
  assert.match(savedPreviewPanel(view), /Open synced Miro board/);
  assert.match(savedPreviewPanel(view), /href="\/files\/case1\/previews\/reviewed\/graph.html"[^>]*target="_blank"/);
  assert.doesNotMatch(savedPreviewPanel(view), /<iframe/);
});

test('peg-out cumulative selection is opt-in and other plotting goals never show its controls', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'normal', status: 'running'} : undefined);
  view.state.activeCase = workflowCase(); await view.dispatch('view-plots');
  assert.doesNotMatch(view.workspace(), /id="workflow-pegout-mode"|id="workflow-pegout-lbtc-limit"/);
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  assert.match(view.workspace(), /id="workflow-pegout-mode"><option value="all" selected/);
  assert.match(view.workspace(), /Stop at cumulative L-BTC amount/);
  assert.doesNotMatch(view.workspace(), /id="workflow-pegout-lbtc-limit"/);
  await view.dispatch('workflow-plot');
  assert.equal(Object.hasOwn(view.calls.at(-1).body, 'pegout_lbtc_limit'), false);
});

for (const action of ['workflow-plot', 'workflow-plot-sync']) {
  test(`cumulative peg-out ${action} sends an exact decimal string with shared provenance and endpoint options`, async () => {
    const view = await harness(path => path.endsWith('/actions') ? {id: 'amount-plot', status: 'running'} : undefined);
    view.state.activeCase = workflowCase({shared_collection: sharedCollection()});
    await view.dispatch('view-plots');
    await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
    workflowEdit(view, 'data-source', 'shared'); workflowEdit(view, 'shared-run', 'shared-old');
    workflowEdit(view, 'pegout-mode', 'cumulative'); workflowEdit(view, 'pegout-lbtc-limit', ' 60.00000001 ');
    view.workflowInput({id: 'workflow-include-unspent', checked: true});
    assert.match(view.workspace(), /id="workflow-pegout-lbtc-limit"[^>]*inputmode="decimal"/);
    assert.match(view.workspace(), /Include the endpoint that reaches or exceeds the target, report any overshoot, then stop/);
    assert.match(view.workspace(), /path reachability does not establish how much stolen value/);
    await view.dispatch(action);
    const body = view.calls.at(-1).body;
    assert.equal(body.action, action === 'workflow-plot' ? 'plot' : 'plot-sync');
    assert.equal(body.pegout_lbtc_limit, '60.00000001');
    assert.equal(typeof body.pegout_lbtc_limit, 'string');
    assert.equal(body.include_unspent, true);
    assert.equal(body.data_source, 'shared'); assert.equal(body.dataset_id, 'pool'); assert.equal(body.run_id, 'shared-old');
    assert.equal(body.min_hops, 0); assert.equal(body.max_hops, 10);
  });
}

test('switching peg-out selection off omits its retained amount and ignores invalid hidden input', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'normal', status: 'running'} : undefined);
  view.state.activeCase = workflowCase(); await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  workflowEdit(view, 'pegout-mode', 'cumulative'); workflowEdit(view, 'pegout-lbtc-limit', 'invalid');
  workflowEdit(view, 'pegout-mode', 'all');
  assert.doesNotMatch(view.workspace(), /id="workflow-pegout-lbtc-limit"/);
  await view.dispatch('workflow-plot');
  assert.equal(Object.hasOwn(view.calls.at(-1).body, 'pegout_lbtc_limit'), false);
  view.state.jobs.clear();
  workflowEdit(view, 'pegout-mode', 'cumulative');
  assert.equal(view.currentWorkflow(view.state.activeCase).pegoutLbtcLimit, 'invalid');
  assert.match(view.workspace(), /id="workflow-pegout-lbtc-limit"[^>]*value="invalid"/);
});

for (const goal of ['full', 'connections']) {
  test(`changing from cumulative peg-outs to ${goal} omits its amount but retains the peg-out draft`, async () => {
    const view = await harness(path => path.endsWith('/actions') ? {id: 'different-goal', status: 'running'} : undefined);
    view.state.activeCase = workflowCase(); await view.dispatch('view-plots');
    await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
    workflowEdit(view, 'pegout-mode', 'cumulative'); workflowEdit(view, 'pegout-lbtc-limit', '60');
    await view.dispatch('plot-goal', {dataset: {goal}});
    assert.doesNotMatch(view.workspace(), /id="workflow-pegout-mode"|id="workflow-pegout-lbtc-limit"/);
    await view.dispatch('workflow-plot');
    assert.equal(Object.hasOwn(view.calls.at(-1).body, 'pegout_lbtc_limit'), false);
    view.state.jobs.clear(); await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
    assert.match(view.workspace(), /value="cumulative" selected/);
    assert.match(view.workspace(), /id="workflow-pegout-lbtc-limit"[^>]*value="60"/);
  });
}

test('cumulative peg-out amounts reject zero, negative, imprecise, formatted and nonnumeric values before submission', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase(); await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}}); workflowEdit(view, 'pegout-mode', 'cumulative');
  for (const amount of ['', ' ', '0', '0.00000000', '-1', '1.000000001', '1e2', '1,000', '+60', '60.', '.1', 'NaN', 'Infinity']) {
    workflowEdit(view, 'pegout-lbtc-limit', amount);
    await assert.rejects(view.dispatch('workflow-plot'), /positive L-BTC target with up to 8 decimal places/, amount);
  }
  assert.ok(!view.calls.some(call => call.path.endsWith('/actions')));
});

test('cumulative peg-out validation preserves small units and integers beyond floating point precision', async () => {
  for (const amount of ['0.00000001', '60', '00060.00000000', '9007199254740993.12345678']) {
    const view = await harness(path => path.endsWith('/actions') ? {id: 'precise', status: 'running'} : undefined);
    view.state.activeCase = workflowCase(); await view.dispatch('view-plots');
    await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}}); workflowEdit(view, 'pegout-mode', 'cumulative');
    workflowEdit(view, 'pegout-lbtc-limit', amount);
    await view.dispatch('workflow-plot');
    assert.equal(view.calls.at(-1).body.pegout_lbtc_limit, amount);
  }
});

test('cumulative peg-out draft is isolated per investigation and survives returning to its tab', async () => {
  const view = await harness(), alpha = workflowCase(), beta = workflowCase({id: 'case2'});
  view.state.activeCase = alpha; await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  workflowEdit(view, 'pegout-mode', 'cumulative'); workflowEdit(view, 'pegout-lbtc-limit', '60.5');
  await view.dispatch('view-collect'); await view.dispatch('view-plots');
  assert.equal(view.currentWorkflow(alpha).pegoutLbtcLimit, '60.5');
  view.state.activeCase = beta; await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  assert.equal(view.currentWorkflow(beta).pegoutLimitEnabled, false);
  assert.equal(view.currentWorkflow(beta).pegoutLbtcLimit, '');
  workflowEdit(view, 'pegout-mode', 'cumulative'); workflowEdit(view, 'pegout-lbtc-limit', '20');
  view.state.activeCase = alpha; await view.dispatch('view-plots');
  assert.equal(view.currentWorkflow(alpha).pegoutLimitEnabled, true);
  assert.equal(view.currentWorkflow(alpha).pegoutLbtcLimit, '60.5');
});

test('board update preparation restores saved cumulative selection and keeps independent destination drafts', async () => {
  const capped = workflowPlot('pegouts', 'capped', {query: {pegout_lbtc_limit: '60', include_unspent: true}});
  const normal = workflowPlot('pegouts', 'normal');
  const detail = workflowCase({plots: [capped, normal], boards: [workflowBoard('pegouts', 'capped', {preview_id: 'capped'}),
    workflowBoard('pegouts', 'normal', {preview_id: 'normal'})]});
  const view = await harness(path => path.endsWith('/actions') ? {id: 'updated', status: 'running'} : undefined);
  view.state.activeCase = detail; await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  workflowEdit(view, 'pegout-mode', 'cumulative'); workflowEdit(view, 'pegout-lbtc-limit', '30');
  await view.dispatch('workflow-board-prepare', boardControl('capped'));
  assert.equal(view.currentWorkflow(detail).pegoutLimitEnabled, true);
  assert.equal(view.currentWorkflow(detail).pegoutLbtcLimit, '60');
  workflowEdit(view, 'pegout-lbtc-limit', '61');
  workflowEdit(view, 'layout-board', 'normal');
  assert.equal(view.currentWorkflow(detail).pegoutLimitEnabled, false);
  assert.equal(view.currentWorkflow(detail).pegoutLbtcLimit, '');
  workflowEdit(view, 'layout-board', 'capped');
  assert.equal(view.currentWorkflow(detail).pegoutLbtcLimit, '61');
  await view.dispatch('workflow-plot');
  assert.equal(view.calls.at(-1).body.pegout_lbtc_limit, '61');
  assert.equal(view.calls.at(-1).body.board_record_id, 'capped');
  view.state.jobs.clear(); workflowEdit(view, 'layout-mode', 'fresh');
  assert.equal(view.currentWorkflow(detail).pegoutLimitEnabled, true);
  assert.equal(view.currentWorkflow(detail).pegoutLbtcLimit, '30');
});

const cumulativeSummary = extra => ({schema_version: 1, target_lbtc: '60', target_base_units: '6000000000',
  total_lbtc: '62.00000001', total_base_units: '6200000001', excess_lbtc: '2.00000001', excess_base_units: '200000001',
  limit_reached: true, stop_reason: 'limit_reached', ordering: 'ordinary_seed_hops_then_txid_vout',
  cutoff_seed_hops: 3, stopping_outpoint: `${txid}:1`, counted_pegout_count: 3, unknown_amount_count: 2,
  unknown_asset_count: 1, non_lbtc_count: 4, context_outputs_counted: false, ...extra});

test('saved cumulative peg-out variants identify target and explain actual total, overshoot and excluded values', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({plots: [workflowPlot('pegouts', 'capped', {query: {pegout_lbtc_limit: '60'},
    pegout_limit_summary: cumulativeSummary()}), workflowPlot('pegouts', 'normal')]});
  for (const page of ['plots', 'history']) {
    await view.dispatch('view-' + page);
    const html = view.workspace();
    const picker = html.match(/<select id="workflow-plot-picker"[\s\S]*?<\/select>/)[0];
    assert.match(picker, /value="capped"[^>]*>[^<]*Cumulative target 60 L-BTC/);
    assert.doesNotMatch(picker.match(/<option value="normal"[^>]*>[^<]*<\/option>/)[0], /Cumulative target/);
    assert.match(html, /<strong>Target:<\/strong> 60 L-BTC/);
    assert.match(html, /<strong>Counted:<\/strong> 62\.00000001 L-BTC/);
    assert.match(html, /<strong>Overshoot:<\/strong> 2\.00000001 L-BTC/);
    assert.match(html, /Stopped after including the endpoint that reached or exceeded the target/);
    assert.match(html, /3 unique peg-outs counted/);
    assert.match(html, /2 hidden or unavailable amounts · 1 unidentified asset · 4 non-L-BTC outputs/);
    assert.match(html, /Context outputs are excluded from this total/);
  }
});

test('unreached cumulative peg-out target reports selected path exhaustion without claiming complete evidence', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({plots: [workflowPlot('pegouts', 'shortfall', {query: {pegout_lbtc_limit: '60'},
    pegout_limit_summary: cumulativeSummary({limit_reached: false, stop_reason: 'paths_exhausted',
      total_lbtc: '50', total_base_units: '5000000000', excess_lbtc: '0', excess_base_units: '0',
      cutoff_seed_hops: null, stopping_outpoint: null})})]});
  await view.dispatch('view-plots');
  assert.match(savedPreviewPanel(view), /Target not reached: selected paths were exhausted within this snapshot, hop range, and stop rules/);
  assert.doesNotMatch(savedPreviewPanel(view), /Stopped after including/);
  assert.match(savedPreviewPanel(view), /Path reachability does not establish how much stolen value reached an endpoint/);
});

test('missing cumulative summary remains explicit and saved amount text is escaped', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({plots: [workflowPlot('pegouts', 'missing-summary',
    {query: {pegout_lbtc_limit: '<script>60</script>'}})]});
  await view.dispatch('view-plots');
  assert.match(savedPreviewPanel(view), /Generate a new preview to see the counted total and stopping result/);
  assert.match(savedPreviewPanel(view), /&lt;script&gt;60&lt;\/script&gt;/);
  assert.doesNotMatch(savedPreviewPanel(view), /<script>/);
});


const pendingPreview = (id, extra = {}) => workflowPlot('pegouts', id, {layout_mode: 'fresh', validation_pending: true,
  reviewable: false, input_snapshot_version: 1, ...extra});
const librarySelect = (view, preview, caseId = 'case1') => view.dispatch('preview-select', {dataset: {preview, caseId}});

test('saved preview Miro actions keep each card pinned to its investigation and immutable preview', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({plots: [pendingPreview('older'), pendingPreview('newer')]});
  for (const tab of ['plots', 'analysis']) {
    await view.dispatch('view-' + tab);
    for (const id of ['older', 'newer']) {
      const card = view.workspace().match(new RegExp(`<article[^>]*data-preview-card="${id}"[\\s\\S]*?</article>`))?.[0];
      assert.ok(card);
      const action = card.match(/<button[^>]*data-action="preview-miro"[^>]*>/)?.[0];
      assert.ok(action, `Preview ${id} needs a Miro handoff in ${tab}`);
      assert.match(action, new RegExp(`data-preview="${id}"`));
      assert.match(action, /data-case-id="case1"/);
      assert.doesNotMatch(card, /data-action="workflow-write-miro"/);
    }
  }
  assert.equal(view.calls.some(call => call.body), false);
});

for (const tab of ['plots', 'analysis']) {
  test(`saved preview Miro handoff from ${tab} loads boards and verifies only the requested preview before explicit publication`, async () => {
    const boards = Promise.withResolvers(), check = Promise.withResolvers();
    const older = pendingPreview('older');
    const view = await harness(path => path.endsWith('/boards') ? boards.promise
      : path.endsWith('/plots/older') ? check.promise
      : path.endsWith('/actions') ? {id: 'publication', status: 'running'} : undefined);
    view.state.activeCase = workflowCase({plots: [pendingPreview('newer'), older],
      sections: stagedSections({workflow: 'ready', collection: 'ready', shared: 'ready', analyses: 'ready'})});
    view.state.page = 'case'; view.state.caseView = tab;
    view.currentWorkflow(view.state.activeCase).plot = 'newer';
    const preparing = view.dispatch('preview-miro', previewWriteControl('older'));
    await settled();
    assert.equal(view.state.caseView, 'plots');
    assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'older');
    assert.equal(view.calls.filter(call => call.path.endsWith('/boards')).length, 1);
    assert.match(previewWriteButton(view), /disabled/);
    assert.equal(view.calls.some(call => call.body), false);
    boards.resolve(sectionResult('boards', {boards: []})); await settled();
    assert.equal(view.calls.filter(call => call.path.endsWith('/plots/older')).length, 1);
    assert.equal(view.calls.some(call => call.path.endsWith('/plots/newer')), false);
    assert.match(previewWriteButton(view), /disabled/);
    check.resolve({...older, validation_pending: false, reviewable: true}); await preparing; await settled();
    assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'older');
    assert.doesNotMatch(previewWriteButton(view), /disabled/);
    assert.equal(view.methods.some(method => method !== 'GET'), false, 'Selecting Miro controls must only read saved data');
    assert.ok(view.workspace().indexOf('id="preview-miro-panel"') < view.workspace().indexOf('id="workflow-plot-picker"'));
    await view.dispatch('workflow-write-miro', previewWriteControl('older'));
    assert.deepEqual(view.calls.filter(call => call.body).map(call => call.body), [
      {action: 'board-create-sync', preview_id: 'older', name: 'Shared evidence · Paths to endpoints'},
    ]);
  });
}

test('saved preview Miro handoff reuses verified files and ignores controls from another investigation', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({plots: [workflowPlot('pegouts', 'ready', {layout_mode: 'fresh'}), pendingPreview('other')]});
  await view.dispatch('view-analysis');
  view.currentWorkflow(view.state.activeCase).plot = 'other';
  const calls = view.calls.length;
  await view.dispatch('preview-miro', previewWriteControl('ready', 'another-case'));
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'other');
  assert.equal(view.state.caseView, 'analysis');
  assert.equal(view.calls.length, calls);
  await view.dispatch('preview-miro', previewWriteControl('ready'));
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'ready');
  assert.equal(view.state.caseView, 'plots');
  assert.equal(view.calls.some(call => /\/plots\/ready(?:\/summary)?$/.test(call.path)), false);
  assert.equal(view.calls.some(call => call.body), false);
});

const previewMiroRouteResponse = (path, {missing = false} = {}) => {
  const detail = stagedOverview();
  if (path === '/api/session') return {csrf: 'test', settings: defaults, cases: [detail]};
  if (path.endsWith('/overview')) return detail;
  if (path.endsWith('/workflow')) return sectionResult('workflow', {plots: [pendingPreview('newer')], plots_next_cursor: 'older-page'});
  if (path.endsWith('/boards')) return sectionResult('boards', {boards: []});
  if (path.endsWith('/collection')) return sectionResult('collection', {runs: []});
  if (path.endsWith('/shared-collection')) return sectionResult('shared', {shared_collection: null});
  if (path.endsWith('/plots/older/summary')) return missing ? {error: 'Requested preview is unavailable'} : pendingPreview('older');
  if (path.endsWith('/plots/older')) return {...pendingPreview('older'), validation_pending: false, reviewable: true};
};

test('saved preview Miro startup link loads an older exact preview directly without paging or publishing', async () => {
  const view = await harness(path => previewMiroRouteResponse(path), {hash: '#case/case1/preview/older/miro'});
  await settled();
  assert.equal(view.state.activeCase?.id, 'case1');
  assert.equal(view.state.caseView, 'plots');
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'older');
  assert.ok(view.state.activeCase.plots.some(plot => plot.preview_id === 'older'));
  assert.equal(view.calls.filter(call => call.path.endsWith('/plots/older/summary')).length, 1);
  assert.equal(view.calls.filter(call => call.path.endsWith('/plots/older')).length, 1);
  assert.equal(view.calls.some(call => call.path.includes('/plots?') || call.path.endsWith('/plots/newer') || call.body), false);
  assert.doesNotMatch(previewWriteButton(view), /disabled/);
});

test('saved preview Miro hashchange link selects its exact preview over a remembered newer selection', async () => {
  const view = await harness(path => previewMiroRouteResponse(path));
  await view.openCase('case1'); await settled();
  await view.dispatch('view-analysis'); await settled();
  view.currentWorkflow(view.state.activeCase).plot = 'newer';
  await view.setHash('#case/case1/preview/older/miro'); await settled();
  assert.equal(view.state.caseView, 'plots');
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'older');
  assert.equal(view.calls.filter(call => call.path.endsWith('/plots/older')).length, 1);
  assert.equal(view.calls.some(call => call.body), false);
});

test('saved preview Miro hashchange during case opening honors the latest requested preview', async () => {
  const overview = Promise.withResolvers();
  const view = await harness(path => path.endsWith('/overview') ? overview.promise
    : path.endsWith('/plots/newer') ? {...pendingPreview('newer'), validation_pending: false, reviewable: true}
    : previewMiroRouteResponse(path));
  await view.setHash('#case/case1/preview/older/miro');
  assert.equal(view.state.openingCase?.id, 'case1');
  await view.setHash('#case/case1/preview/newer/miro');
  overview.resolve(stagedOverview()); await settled(); await settled();
  assert.equal(view.state.activeCase?.id, 'case1');
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'newer');
  assert.equal(view.calls.some(call => call.path.endsWith('/plots/older')), false);
  assert.equal(view.methods.some(method => method !== 'GET'), false);
});

for (const section of ['boards', 'workflow']) {
  test(`saved preview Miro handoff retries a failed ${section} load without any publication`, async () => {
    let attempts = 0;
    const plot = pendingPreview('older');
    const view = await harness(path => path.endsWith('/' + section)
      ? ++attempts === 1 ? {error: 'Saved section temporarily unavailable'} : sectionResult(section, section === 'boards' ? {boards: []} : {plots: [plot]})
      : path.endsWith('/plots/older') ? {...plot, validation_pending: false, reviewable: true} : undefined);
    view.state.activeCase = workflowCase({plots: [plot],
      sections: stagedSections({workflow: 'ready', collection: 'ready', boards: 'ready', shared: 'ready', analyses: 'ready', [section]: 'unloaded'})});
    view.state.page = 'case'; view.state.caseView = 'analysis';
    await view.dispatch('preview-miro', previewWriteControl('older')); await settled();
    assert.equal(view.state.activeCase.sections[section], 'error');
    assert.equal(view.methods.some(method => method !== 'GET'), false);
    await view.dispatch('preview-miro', previewWriteControl('older')); await settled();
    assert.equal(attempts, 2);
    assert.equal(view.state.activeCase.sections[section], 'ready');
    assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'older');
    assert.doesNotMatch(previewWriteButton(view), /disabled/);
    assert.equal(view.methods.some(method => method !== 'GET'), false);
  });
}

test('saved preview Miro handoff retries failed file verification and never enables publication before success', async () => {
  let attempts = 0;
  const plot = pendingPreview('older');
  const view = await harness(path => path.endsWith('/plots/older')
    ? ++attempts === 1 ? {error: 'Saved files temporarily unavailable'} : {...plot, validation_pending: false, reviewable: true} : undefined);
  view.state.activeCase = workflowCase({plots: [plot]});
  await view.dispatch('preview-miro', previewWriteControl('older'));
  assert.match(previewWriteButton(view), /disabled/);
  await view.dispatch('preview-miro', previewWriteControl('older'));
  assert.equal(attempts, 2);
  assert.doesNotMatch(previewWriteButton(view), /disabled/);
  assert.equal(view.methods.some(method => method !== 'GET'), false);
});

test('saved preview Miro link fails closed when its preview is missing instead of selecting or publishing the newest', async () => {
  const view = await harness(path => previewMiroRouteResponse(path, {missing: true}), {hash: '#case/case1/preview/older/miro'});
  await settled();
  assert.equal(view.state.activeCase?.id, 'case1');
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'older');
  assert.match(savedPreviewPanel(view), /Requested preview is unavailable/);
  assert.equal(previewWriteButton(view), undefined);
  assert.equal(view.calls.some(call => /\/plots\/(older|newer)$/.test(call.path) || call.body), false);
});

test('saved preview Miro verification completion does not replace a later manual preview selection', async () => {
  const check = Promise.withResolvers(), older = pendingPreview('older');
  const view = await harness(path => path.endsWith('/plots/older') ? check.promise : undefined);
  view.state.activeCase = workflowCase({plots: [older, workflowPlot('pegouts', 'newer', {layout_mode: 'fresh'})]});
  await view.dispatch('view-analysis');
  const preparing = view.dispatch('preview-miro', previewWriteControl('older')); await settled();
  assert.equal(view.calls.filter(call => call.path.endsWith('/plots/older')).length, 1);
  await librarySelect(view, 'newer');
  check.resolve({...older, validation_pending: false, reviewable: true}); await preparing; await settled();
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'newer');
  assert.match(previewWriteButton(view), /data-preview="newer"/);
  assert.equal(view.calls.some(call => call.body), false);
});

test('saved preview Miro board loading cannot move a newly opened investigation back to the old preview', async () => {
  const boards = Promise.withResolvers();
  const other = workflowCase({id: 'case2', name: 'Other investigation', plots: [pendingPreview('other')]});
  const view = await harness(path => path === '/api/cases/case1/boards' ? boards.promise
    : path === '/api/cases/case2/overview' ? other : undefined);
  view.state.activeCase = workflowCase({plots: [pendingPreview('older')],
    sections: stagedSections({workflow: 'ready', collection: 'ready', shared: 'ready', analyses: 'ready'})});
  view.state.page = 'case'; view.state.caseView = 'analysis';
  const preparing = view.dispatch('preview-miro', previewWriteControl('older')); await settled();
  await view.openCase('case2');
  boards.resolve(sectionResult('boards', {boards: []})); await preparing; await settled();
  assert.equal(view.state.activeCase.id, 'case2');
  assert.equal(view.state.caseView, 'collect');
  assert.notEqual(view.currentWorkflow(view.state.activeCase).plot, 'older');
  assert.equal(view.calls.some(call => call.path.endsWith('/plots/older') || call.body), false);
});

test('saved preview Miro controls name create, update, resume and open without changing their saved destinations', async () => {
  for (const [kind, label] of [['fresh', 'Create Miro board'], ['update', 'Update Miro board'], ['resume', 'Resume Miro sync'], ['synced', 'Open (?:synced )?Miro board']]) {
    const plot = workflowPlot('pegouts', 'ready', {layout_mode: kind === 'update' ? 'update' : 'fresh',
      ...(kind === 'update' ? {board_record_id: 'target', board_id: 'miro-target'} : {})});
    const board = workflowBoard('pegouts', 'target', {preview_id: 'ready', creation_preview_id: 'ready',
      status: kind === 'resume' ? 'interrupted' : kind === 'synced' ? 'synced' : 'linked', pending_count: kind === 'resume' ? 1 : 0});
    const view = await harness();
    view.state.activeCase = workflowCase({plots: [plot], boards: kind === 'fresh' ? [] : [board]});
    await view.dispatch('preview-miro', previewWriteControl('ready'));
    const panel = savedPreviewPanel(view);
    assert.match(panel, new RegExp(label));
    if (kind === 'synced') {
      assert.equal(previewWriteButton(view), undefined);
      assert.match(panel, /href="https:\/\/miro.com\/app\/board\/miro-target\/"/);
    } else assert.match(previewWriteButton(view), /data-preview="ready"/);
    assert.equal(view.calls.some(call => call.body), false);
  }
});

test('saved library cards display newest-first metadata, exact cumulative target and safe separate-tab preview links', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({plots: [pendingPreview('older', {created_at: '2026-01-01T00:00:00Z'}),
    pendingPreview('newer', {created_at: '2026-10-01T00:00:00Z', layout_settings: {...layoutDefaults, layout_style: 'trace'},
      query: {pegout_lbtc_limit: '60'}, collection_source: {kind: 'shared', dataset_id: 'pool', run_id: 'shared-old'}})]});
  await view.dispatch('view-plots');
  const html = savedPreviewPanel(view);
  assert.match(html, /<h2>Saved previews<\/h2>/);
  assert.ok(html.indexOf('data-preview-card="newer"') < html.indexOf('data-preview-card="older"'));
  assert.match(html, /Trace layout · 3 objects · 2 connections/);
  assert.match(html, /Cumulative target 60 L-BTC/);
  assert.match(html, /Shared snapshot shared-old/);
  assert.match(html, /Hops 0–10/);
  assert.match(html, /href="\/files\/case1\/previews\/newer\/graph.html" target="_blank" rel="noopener noreferrer"/);
  assert.doesNotMatch(html, /<iframe|<img/);
  assert.ok(!view.calls.some(call => call.path.includes('/plots/') || call.path.startsWith('/files/') || call.path.endsWith('/actions')));
  assert.ok(view.workspace().indexOf('id="saved-plots-panel"') < view.workspace().indexOf('id="plot-layouts-panel"'));
});

test('library selection prepares and publishes the exact chosen saved ID without generation', async () => {
  const older = pendingPreview('older'), newer = pendingPreview('newer');
  const view = await harness(path => path.endsWith('/plots/older') ? {...older, validation_pending: false, reviewable: true}
    : path.endsWith('/actions') ? {id: 'write', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({plots: [newer, older]}); await view.dispatch('view-plots');
  await librarySelect(view, 'older');
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'older');
  assert.ok(!view.calls.some(call => call.path.includes('/plots/')));
  await view.dispatch('verify-saved-plot', {dataset: {preview: 'older', caseId: 'case1'}}); await settled();
  await view.dispatch('workflow-write-miro', previewWriteControl('older'));
  assert.deepEqual(view.calls.filter(call => call.body).map(call => call.body), [
    {action: 'board-create-sync', preview_id: 'older', name: 'Shared evidence · Paths to endpoints'}]);
});

test('completed preview metadata is immediately selected and openable while overview refresh is still pending', async () => {
  const overview = Promise.withResolvers(), newest = pendingPreview('completed', {created_at: '2026-10-03T00:00:00Z'});
  const view = await harness(path => path === '/api/jobs/render' ? {status: 'succeeded', result: newest}
    : path.endsWith('/overview') ? overview.promise : undefined);
  view.state.activeCase = workflowCase({plots: [pendingPreview('older')]}); view.state.caseView = 'plots';
  view.currentWorkflow(view.state.activeCase).plot = 'older';
  view.state.job = {id: 'render', action: 'plot', caseId: 'case1'};
  const completion = view.pollJob(); await settled();
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'completed');
  assert.match(savedPreviewPanel(view), /data-preview-card="completed"/);
  assert.match(savedPreviewPanel(view), /href="\/files\/case1\/previews\/completed\/graph.html"/);
  assert.doesNotMatch(savedPreviewPanel(view), /<iframe/);
  assert.ok(!view.calls.some(call => call.path.includes('/plots/')));
  overview.resolve(workflowCase({plots: [newest, pendingPreview('older')]})); await completion;
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'completed');
  assert.ok(!view.calls.some(call => call.path.includes('/plots/')));
});

test('completed layout does not override a manual library selection made while its task ran', async () => {
  const newest = pendingPreview('completed');
  const detail = workflowCase({plots: [pendingPreview('older'), pendingPreview('manual')]});
  const view = await harness(path => path.endsWith('/actions') ? {id: 'render', status: 'running'}
    : path === '/api/jobs/render' ? {status: 'succeeded', result: newest}
    : path.endsWith('/overview') ? {...detail, plots: [newest, ...detail.plots]} : undefined);
  view.state.activeCase = detail; await view.dispatch('view-plots'); await view.dispatch('workflow-plot');
  await librarySelect(view, 'manual');
  await view.pollJob();
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'manual');
  assert.match(savedPreviewPanel(view), /data-preview-card="completed"/);
  assert.ok(!view.calls.some(call => call.path.includes('/plots/')));
});

test('older metadata pages merge without losing selection, handle empty pages and never verify previews', async () => {
  const page = Promise.withResolvers();
  const view = await harness(path => path.includes('/plots?cursor=page-2') ? page.promise
    : path.includes('/plots?cursor=page-3') ? {plots: [pendingPreview('oldest')], next_cursor: null} : undefined);
  view.state.activeCase = workflowCase({plots: [pendingPreview('current')], plots_next_cursor: 'page-2'});
  await view.dispatch('view-plots'); await librarySelect(view, 'current');
  await view.dispatch('preview-load-more', {dataset: {caseId: 'case1'}});
  await view.dispatch('preview-load-more', {dataset: {caseId: 'case1'}});
  assert.equal(view.calls.filter(call => call.path.includes('/plots?')).length, 1);
  page.resolve({plots: [], next_cursor: 'page-3'}); await settled();
  assert.match(savedPreviewPanel(view), /Load older previews/);
  await view.dispatch('preview-load-more', {dataset: {caseId: 'case1'}}); await settled();
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'current');
  assert.match(savedPreviewPanel(view), /data-preview-card="oldest"/);
  assert.doesNotMatch(savedPreviewPanel(view), /data-action="preview-load-more"/);
  assert.ok(!view.calls.some(call => /\/plots\//.test(call.path)));
});

test('older page responses after switching investigations cannot populate the other library', async () => {
  const page = Promise.withResolvers();
  const view = await harness(path => path.includes('/plots?') ? page.promise : undefined);
  view.state.activeCase = workflowCase({plots: [pendingPreview('first')], plots_next_cursor: 'page-2'});
  await view.dispatch('view-plots'); await view.dispatch('preview-load-more', {dataset: {caseId: 'case1'}});
  view.state.activeCase = workflowCase({id: 'case2', plots: [pendingPreview('second')]}); await view.dispatch('view-plots');
  page.resolve({plots: [pendingPreview('wrong-case')], next_cursor: null}); await settled();
  assert.equal(view.state.activeCase.id, 'case2');
  assert.doesNotMatch(savedPreviewPanel(view), /wrong-case/);
});

test('remembered older selection restores through metadata only and survives a reload', async () => {
  const storage = new Map([['liquid-tracer:investigation-tabs:v1', JSON.stringify({ids: ['case1'],
    views: [{id: 'case1', caseView: 'plots', selectedRun: 'saved1', selectedPreview: 'older'}]})]]);
  const detail = workflowCase({plots: [pendingPreview('newest')]});
  const respond = path => path === '/api/session' ? {csrf: 'test', settings: defaults, cases: [detail]}
    : path.endsWith('/overview') ? detail
    : path.endsWith('/plots/older/summary') ? pendingPreview('older') : undefined;
  const view = await harness(respond, {storage}); await view.openCase('case1'); await settled();
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'older');
  assert.match(savedPreviewPanel(view), /data-preview-card="older"/);
  assert.equal(view.calls.filter(call => call.path.endsWith('/plots/older/summary')).length, 1);
  assert.ok(!view.calls.some(call => /\/plots\/[^/]+$/.test(call.path)));
  await librarySelect(view, 'newest');
  const saved = JSON.parse(storage.get('liquid-tracer:investigation-tabs:v1'));
  assert.equal(saved.views[0].selectedPreview, 'newest');
  const reloaded = await harness(respond, {storage}); await reloaded.openCase('case1'); await settled();
  assert.equal(reloaded.currentWorkflow(reloaded.state.activeCase).plot, 'newest');
});

test('missing remembered preview reports an error without substituting a different publication target', async () => {
  const storage = new Map([['liquid-tracer:investigation-tabs:v1', JSON.stringify({ids: ['case1'],
    views: [{id: 'case1', caseView: 'plots', selectedPreview: 'missing'}]})]]);
  const detail = workflowCase({plots: [pendingPreview('newest')]});
  const view = await harness(path => path === '/api/session' ? {csrf: 'test', settings: defaults, cases: [detail]}
    : path.endsWith('/overview') ? detail
    : path.endsWith('/plots/missing/summary') ? {error: 'Saved preview is unavailable.'} : undefined, {storage});
  await view.openCase('case1'); await settled();
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'missing');
  assert.match(savedPreviewPanel(view), /Saved preview is unavailable/);
  assert.equal(previewWriteButton(view), undefined);
  await assert.rejects(view.dispatch('workflow-write-miro', previewWriteControl('missing')), /selected preview changed/);
  await librarySelect(view, 'newest');
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'newest');
});

test('explicit preparation errors remain retryable and a replaced workflow cannot be approved by a stale response', async () => {
  const check = Promise.withResolvers();
  const pending = pendingPreview('selected');
  const view = await harness(path => path.endsWith('/plots/selected') ? check.promise
    : path.endsWith('/workflow') ? sectionResult('workflow', {plots: [pending]}) : undefined);
  view.state.activeCase = workflowCase({plots: [pending], sections: stagedSections({workflow: 'ready', collection: 'ready', boards: 'ready', shared: 'ready'})});
  await view.dispatch('view-plots');
  await view.dispatch('verify-saved-plot', {dataset: {preview: 'selected', caseId: 'case1'}});
  await view.loadCaseSection('case1', 'workflow', true);
  check.resolve({...pending, validation_pending: false, reviewable: true}); await settled();
  assert.match(previewWriteButton(view), /disabled/);
  assert.match(savedPreviewPanel(view), /Saved preview information changed/);
  assert.equal(view.calls.filter(call => call.path.endsWith('/plots/selected')).length, 1, 'refresh must not silently repeat expensive verification');
});

test('explicit board recovery selection still verifies its selected saved preview', async () => {
  const plot = pendingPreview('recover', {layout_mode: 'update', board_record_id: 'target', board_id: 'miro-target'});
  const view = await harness(path => path.endsWith('/plots/recover') ? {...plot, validation_pending: false, reviewable: true} : undefined);
  view.state.activeCase = workflowCase({plots: [plot], boards: [workflowBoard('pegouts', 'target')]});
  await view.dispatch('view-plots');
  assert.ok(!view.calls.some(call => call.path.includes('/plots/')));
  boardEdit(view, 'target', 'plot', 'recover'); await settled();
  assert.equal(view.calls.filter(call => call.path.endsWith('/plots/recover')).length, 1);
});


test('loading older metadata preserves an explicitly prepared preview with the same immutable ID', async () => {
  const prepared = {...pendingPreview('prepared'), reviewable: true, validation_pending: false};
  const view = await harness(path => path.includes('/plots?') ? {plots: [pendingPreview('prepared'), pendingPreview('older')], next_cursor: null} : undefined);
  view.state.activeCase = workflowCase({plots: [prepared], plots_next_cursor: 'older'});
  await view.dispatch('view-plots'); await librarySelect(view, 'prepared');
  await view.dispatch('preview-load-more', {dataset: {caseId: 'case1'}}); await settled();
  assert.equal(view.state.activeCase.plots.find(plot => plot.preview_id === 'prepared').validation_pending, false);
  assert.equal(view.state.activeCase.plots.filter(plot => plot.preview_id === 'prepared').length, 1);
  assert.doesNotMatch(previewWriteButton(view), /disabled/);
  assert.ok(!view.calls.some(call => /\/plots\//.test(call.path)));
});


test('stable preview numbers label cards and choices independently of sorting and pagination', async () => {
  const view = await harness(path => path.includes('/plots?') ? {plots: [pendingPreview('earlier', {
    preview_number: 1, created_at: '2026-01-01T00:00:00Z'})], next_cursor: null} : undefined);
  view.state.activeCase = workflowCase({plots: [pendingPreview('recent', {preview_number: 12,
    created_at: '2026-10-03T00:00:00Z'}), pendingPreview('middle', {preview_number: 5,
    created_at: '2026-02-01T00:00:00Z'})], plots_next_cursor: 'older'});
  await view.dispatch('view-plots'); await librarySelect(view, 'middle');
  assert.match(savedPreviewPanel(view), /<h3>Preview 12 · Paths to endpoints/);
  assert.match(savedPreviewPanel(view), /<h3>Preview 5 · Selected preview/);
  assert.match(savedPreviewPanel(view), /value="middle" selected>Preview 5 ·/);
  await view.dispatch('preview-load-more', {dataset: {caseId: 'case1'}}); await settled();
  assert.match(savedPreviewPanel(view), /<h3>Preview 1 · Paths to endpoints/);
  assert.match(savedPreviewPanel(view), /<h3>Preview 5 · Selected preview/);
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'middle');
});

test('numbered preview prepares and publishes by its immutable ID and retains its number', async () => {
  const plot = pendingPreview('immutable-hash', {preview_number: 7});
  const view = await harness(path => path.endsWith('/plots/immutable-hash') ? {...plot, validation_pending: false, reviewable: true}
    : path.endsWith('/actions') ? {id: 'write', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({plots: [plot]}); await view.dispatch('view-plots');
  await librarySelect(view, plot.preview_id);
  await view.dispatch('verify-saved-plot', {dataset: {preview: plot.preview_id, caseId: 'case1'}}); await settled();
  assert.match(savedPreviewPanel(view), /Preview 7 · Selected preview/);
  await view.dispatch('workflow-write-miro', previewWriteControl(plot.preview_id));
  assert.deepEqual(view.calls.filter(call => call.body).map(call => call.body), [{action: 'board-create-sync',
    preview_id: 'immutable-hash', name: 'Shared evidence · Paths to endpoints'}]);
});

test('missing or invalid preview numbers never become page-relative labels', async () => {
  for (const number of [undefined, 0, -1, 1.5, '9', Number.MAX_SAFE_INTEGER + 1]) {
    const view = await harness();
    view.state.activeCase = workflowCase({plots: [pendingPreview('legacy', {preview_number: number,
      preview_number_notice: 'Saved preview numbering is unavailable. <retry>'})]});
    await view.dispatch('view-plots');
    assert.match(savedPreviewPanel(view), /<h3>Saved preview · Paths to endpoints/);
    assert.match(savedPreviewPanel(view), /unavailable\. &lt;retry&gt;/);
    assert.doesNotMatch(savedPreviewPanel(view), /<h3>Preview \d/);
  }
});

test('completed preview announces its assigned number without loading its graph', async () => {
  const plot = pendingPreview('newly-completed', {preview_number: 13});
  const detail = workflowCase({plots: [plot]});
  const view = await harness(path => path === '/api/jobs/render' ? {status: 'succeeded', result: plot}
    : path.endsWith('/overview') ? detail : undefined);
  view.state.activeCase = workflowCase(); view.state.caseView = 'plots';
  view.state.job = {id: 'render', action: 'plot', caseId: 'case1'};
  await view.pollJob();
  assert.match(view.workspace(), /Preview 13 saved/);
  assert.match(savedPreviewPanel(view), /Preview 13 · Selected preview/);
  assert.ok(!view.calls.some(call => call.path.includes('/plots/') || call.path.startsWith('/files/')));
});

test('saved preview cards and selection show displayed connections with legacy count fallback', async () => {
  for (const [extra, expected] of [[{display_edge_count: 1}, 1], [{}, 2], [{display_edge_count: 0}, 0]]) {
    const view = await harness();
    view.state.activeCase = workflowCase({plots: [pendingPreview('context-count', extra)]});
    await view.dispatch('view-plots');
    await librarySelect(view, 'context-count');
    const html = savedPreviewPanel(view);
    assert.match(html, new RegExp(`Standard layout · 3 objects · ${expected} connections`));
    assert.match(html, new RegExp(`3 objects · ${expected} connections\\.`));
    assert.equal(view.state.activeCase.plots[0].edge_count, 2);
    assert.ok(!view.calls.some(call => call.path.includes('/plots/')));
  }
});


test('saved preview library retains its scroll during paging, selection and investigation switches', async () => {
  const page = Promise.withResolvers();
  const view = await harness(path => path.includes('/plots?') ? page.promise : undefined);
  const first = workflowCase({plots: [pendingPreview('current'), pendingPreview('older')], plots_next_cursor: 'page-2'});
  const second = workflowCase({id: 'case2', plots: [pendingPreview('other')]});
  view.state.activeCase = first;
  // Model browser DOM replacement: each render creates a fresh scroll element.
  let html = view.app.innerHTML;
  Object.defineProperty(view.app, 'innerHTML', {
    get() {return html;},
    set(value) {
      html = value;
      const caseId = value.match(/class="preview-library" data-case-id="([^"]+)"/)?.[1];
      if (caseId) view.elements.set('.preview-library', {dataset: {caseId}, scrollTop: 0});
      else view.elements.delete('.preview-library');
    },
  });
  await view.dispatch('view-plots');
  view.elements.get('.preview-library').scrollTop = 680;
  await view.dispatch('preview-load-more', {dataset: {caseId: 'case1'}});
  assert.equal(view.elements.get('.preview-library').scrollTop, 680);
  page.resolve({plots: [pendingPreview('oldest')], next_cursor: null});
  await settled();
  assert.equal(view.elements.get('.preview-library').scrollTop, 680);
  await librarySelect(view, 'older');
  assert.equal(view.elements.get('.preview-library').scrollTop, 680);
  view.state.activeCase = second;
  await view.dispatch('view-plots');
  assert.equal(view.elements.get('.preview-library').scrollTop, 0);
  view.elements.get('.preview-library').scrollTop = 120;
  view.state.activeCase = first;
  await view.dispatch('view-plots');
  assert.equal(view.elements.get('.preview-library').scrollTop, 680);
});

test('Analysis retains saved evidence and ignores stale report metadata while Miro stays in Plots', async () => {
  const plot = workflowPlot('pegouts', 'one', {preview_number: 9, artifact: {preview_url: '/files/case1/previews/one/graph.html',
    downloads: [{name: 'transactions.csv', url: '/files/case1/previews/one/transactions.csv'},
      {name: 'endpoints.csv', url: '/files/case1/previews/one/endpoints.csv'}]}});
  const view = await harness();
  view.state.activeCase = workflowCase({plots: [plot], reports: [{id: 'old-report',
    source_preview_id: 'one', status: 'complete', figure_count: 3, panel_count: 4,
    preview_url: '/files/case1/reports/old-report/index.html',
    downloads: [{name: 'panels.json', url: '/files/case1/reports/old-report/panels.json'}]}],
    reports_notice: 'Old report figures are present'});
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /data-action="view-analysis"/);
  assert.match(view.workspace(), /Analyze this preview/);
  assert.match(view.workspace(), /data-action="workflow-write-miro"/);
  assert.doesNotMatch(view.workspace(), /id="report-figures-panel"/);
  await view.dispatch('view-analysis');
  const html = view.workspace();
  assert.match(html, /data-action="view-analysis" aria-current="page"/);
  assert.match(html, /Analysis source/);
  assert.match(html, /Preview 9/);
  assert.match(html, /All trace transactions CSV/);
  assert.match(html, /Endpoints only CSV/);
  assert.match(html, /id="scope-analysis-panel"/);
  assert.match(html, /data-action="scope-analyze"/);
  assert.match(html, /id="scope-max-hops"/);
  assert.doesNotMatch(html, /report-figures|report-plan|report-create|report-png|Report figures|PNG panel|old-report|Old report/);
  for (const action of ['report-plan', 'report-create', 'report-png-load', 'report-png-download'])
    await view.dispatch(action, {dataset: {caseId: 'case1', preview: 'one', report: 'old-report'}});
  assert.equal(view.calls.some(call => call.body), false);
  assert.equal(view.downloads.length, 0);
  assert.doesNotMatch(html, /data-action="workflow-write-miro"|id="run-picker"|<h2>Collected data|<iframe/);
  assert.equal(view.calls.filter(call => call.path !== '/api/session').length, 0);
  await view.dispatch('view-history');
  assert.doesNotMatch(view.workspace(), /id="report-figures-panel"/);
});

test('Analysis loads scope source summaries independently of deferred preview metadata', async () => {
  const workflow = Promise.withResolvers();
  const storage = new Map([['liquid-tracer:investigation-tabs:v1', JSON.stringify({ids: ['case1'],
    views: [{id: 'case1', caseView: 'analysis', selectedRun: 'saved1', selectedPreview: 'older'}]})]]);
  const detail = stagedOverview();
  const view = await harness(path => {
    if (path === '/api/session') return {csrf: 'test', settings: defaults, cases: [detail]};
    if (path.endsWith('/overview')) return detail;
    if (path.endsWith('/workflow')) return workflow.promise;
    if (path.endsWith('/plots/older/summary')) return pendingPreview('older');
    if (path.includes('/collection')) return sectionResult('collection', {runs: [{id: 'saved1', max_hops: 15}]});
    if (path.endsWith('/shared-collection')) return sectionResult('shared', {});
    if (path.endsWith('/analyses')) return {analyses: []};
    throw Error(`Unexpected eager request: ${path}`);
  }, {storage});
  await view.openCase('case1');
  assert.equal(view.state.caseView, 'analysis');
  assert.match(view.app.innerHTML, /data-loading-section="workflow"/);
  assert.doesNotMatch(view.app.innerHTML, /id="report-max-objects"|Loading saved runs/);
  assert.match(view.app.innerHTML, /id="scope-analysis-panel"/);
  workflow.resolve(sectionResult('workflow', {plots: [pendingPreview('newest')]})); await settled();
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'older');
  assert.match(view.app.innerHTML, /id="analysis-previews-panel"/);
  assert.match(view.app.innerHTML, /data-preview-card="older"/);
  assert.match(view.app.innerHTML, /Verify preview files/);
  assert.doesNotMatch(view.app.innerHTML, /Prepare for Miro/);
  assert.deepEqual(view.calls.map(call => call.path), ['/api/session', '/api/cases/case1/overview',
    '/api/cases/case1/collection/saved1', '/api/cases/case1/shared-collection', '/api/cases/case1/analyses',
    '/api/cases/case1/workflow', '/api/cases/case1/plots/older/summary']);
});

test('Analysis workflow failure is retryable and an empty library offers preview generation', async () => {
  let attempts = 0;
  const view = await harness(path => path.endsWith('/workflow')
    ? ++attempts === 1 ? {error: 'Saved summaries unavailable'} : sectionResult('workflow', {plots: []}) : undefined);
  view.state.activeCase = stagedOverview();
  await view.dispatch('view-analysis'); await settled();
  assert.match(view.app.innerHTML, /Saved summaries unavailable/);
  assert.match(view.app.innerHTML, /data-action="retry-case-section"/);
  await view.dispatch('retry-case-section', {dataset: {section: 'workflow'}}); await settled();
  assert.match(view.app.innerHTML, /Generate a preview/);
  assert.match(view.app.innerHTML, /id="scope-analysis-panel"/);
  assert.doesNotMatch(view.app.innerHTML, /id="report-max-objects"/);
  await view.dispatch('view-plots');
  assert.equal(view.state.caseView, 'plots');
});

test('Analysis preserves shared preview selection, layout drafts, and navigation on reload', async () => {
  const detail = workflowCase({plots: [workflowPlot('full', 'one'), workflowPlot('full', 'two')]});
  const respond = path => path === '/api/session' ? {csrf: 'test', settings: defaults, cases: [detail]}
    : path.endsWith('/overview') ? structuredClone(detail) : undefined;
  const view = await harness(respond);
  await view.openCase('case1'); await view.dispatch('view-plots');
  view.plotForm({layout_style: 'trace', center_name: 'Preserved backbone', layout_attempts: '4'});
  await view.dispatch('view-analysis');
  view.elements.delete('#plot-layout-form');
  await view.dispatch('preview-select', {dataset: {caseId: 'case1', preview: 'two'}});
  await view.dispatch('view-plots');
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'two');
  assert.match(view.workspace(), /value="Preserved backbone"/);
  await view.dispatch('view-analysis');
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'two');
  const saved = JSON.parse(view.storage.get('liquid-tracer:investigation-tabs:v1'));
  assert.equal(saved.views[0].caseView, 'analysis');
  assert.equal(saved.views[0].selectedPreview, 'two');
  const reloaded = await harness(respond, {storage: view.storage});
  await reloaded.openCase('case1');
  assert.equal(reloaded.state.caseView, 'analysis');
  assert.equal(reloaded.currentWorkflow(reloaded.state.activeCase).plot, 'two');
});

test('older preview loading releases the shared library after switching to Analysis', async () => {
  const page = Promise.withResolvers();
  const view = await harness(path => path.includes('/plots?') ? page.promise : undefined);
  view.state.activeCase = workflowCase({plots: [pendingPreview('one')], plots_next_cursor: 'next'});
  await view.dispatch('view-plots');
  await view.dispatch('preview-load-more', {dataset: {caseId: 'case1'}});
  await view.dispatch('view-analysis');
  assert.match(view.app.innerHTML, /Loading older previews/);
  page.resolve({plots: [pendingPreview('stale')], next_cursor: null}); await settled();
  assert.doesNotMatch(view.app.innerHTML, /Loading older previews|data-preview-card="stale"/);
  assert.match(view.app.innerHTML, /data-action="preview-load-more"(?![^>]*disabled)/);
  await view.dispatch('preview-load-more', {dataset: {caseId: 'case1'}}); await settled();
  assert.match(view.app.innerHTML, /data-preview-card="stale"/);
});

test('Analysis verifies evidence only on request and enables the saved endpoint downloads', async () => {
  const verified = workflowPlot('pegouts', 'one', {artifact: {downloads: [
    {name: 'transactions.csv', url: '/files/case1/previews/one/transactions.csv'},
    {name: 'endpoints.csv', url: '/files/case1/previews/one/endpoints.csv'}]}});
  const view = await harness(path => path.endsWith('/plots/one') ? verified : undefined);
  view.state.activeCase = workflowCase({plots: [{...verified, validation_pending: true, reviewable: false}]});
  await view.dispatch('view-analysis');
  assert.equal(view.calls.length, 1);
  assert.match(view.workspace(), /Verify preview files/);
  assert.doesNotMatch(view.workspace(), /Endpoints only CSV/);
  await view.dispatch('verify-saved-plot', {dataset: {caseId: 'case1', preview: 'one'}}); await settled();
  assert.equal(view.calls.filter(call => call.path.endsWith('/plots/one')).length, 1);
  assert.match(view.workspace(), /Endpoints only CSV/);
  assert.doesNotMatch(view.workspace(), /Verify preview files|Write to Miro/);
});

const scopeFixture = (id = 'scope1', extra = {}) => ({
  schema_version: 1, analysis_id: id, id, name: `Analysis 1 · Shared evidence · through hop 10`,
  created_at: '2026-10-03T10:00:00Z', case_id: 'case1', run_id: 'shared-old',
  source: {data_source: 'shared', dataset_id: 'pool', run_id: 'shared-old', collection_max_hops: 15},
  max_hops: 10, hop_basis: 'original_seeds',
  comparisons: [8, 9, 10].map(max_hops => ({max_hops, transaction_count: max_hops * 5,
    address_count: max_hops * 7, output_count: 90, pegout_count: 2, pegout_lbtc: '4.12500000',
    unspent_count: 3, unspendable_count: 4, frontier_count: 110, data_gap_count: 2, stopped_count: 1})),
  frontier_count: 110, frontier: Array.from({length: 100}, (_, index) => ({
    outpoint: `${txid}:${index}`, hop: 10, reason: 'hop_limit', saved_continuation: true})),
  downloads: [{name: 'frontiers.csv', url: `/files/case1/analyses/${id}/frontiers.csv`}], ...extra,
});
const scopeControl = (id = 'scope1', caseId = 'case1') => ({dataset: {analysis: id, caseId}});

test('Full trace submits its independent ceiling and explicit original-seed basis', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'plotjob', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({runs: [{id: 'saved1', max_hops: 15, hop_reference_name: 'Named group'}]});
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /Maximum analysis hops/);
  assert.match(view.workspace(), /Collection limit: 15 hops/);
  workflowEdit(view, 'max-hops', '8');
  await view.dispatch('workflow-plot');
  assert.equal(view.calls.at(-1).body.max_hops, 8);
  assert.equal(view.calls.at(-1).body.hop_basis, 'original_seeds');
  assert.equal(view.state.activeCase.runs[0].max_hops, 15);
  view.state.jobs.clear();
  workflowEdit(view, 'hop-basis', 'configured');
  await view.dispatch('workflow-plot');
  assert.equal('hop_basis' in view.calls.at(-1).body, false);
  workflowEdit(view, 'max-hops', '-1');
  view.state.jobs.clear();
  await assert.rejects(view.dispatch('workflow-plot'), /whole-number hop limits/);
});

test('scope analysis submits only the selected saved snapshot and appears as a cancellable local task', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'scopejob', status: 'running', cancellable: true,
    resource_kind: 'plot', action: 'scope-analyze', case_id: 'case1'} : undefined);
  view.state.activeCase = workflowCase({shared_collection: sharedCollection()});
  await view.dispatch('view-analysis');
  view.scopeAnalysisInput({id: 'scope-data-source', value: 'shared'});
  view.scopeAnalysisInput({id: 'scope-run', value: 'shared-old'});
  view.scopeAnalysisInput({id: 'scope-max-hops', value: '10'});
  assert.equal(view.calls.filter(call => call.body).length, 0);
  await view.dispatch('scope-analyze');
  assert.deepEqual(view.calls.at(-1).body, {action: 'scope-analyze', run_id: 'shared-old', data_source: 'shared', dataset_id: 'pool', max_hops: 10});
  assert.equal(view.state.job.live, false);
  assert.match(view.jobBanner(), /data-action="cancel-job"/);
  assert.equal(view.taskActivity(view.state.job).kind, 'computing');
  assert.equal(view.calls.some(call => /graph\.json|preview|\/boards|\/history/.test(call.path)), false);
});

test('scope source selections survive tabs and reload without drifting to the latest shared snapshot', async () => {
  const detail = workflowCase({shared_collection: sharedCollection()});
  const respond = path => path === '/api/session' ? {csrf: 'test', settings: defaults, cases: [detail]}
    : path.endsWith('/overview') ? structuredClone(detail) : undefined;
  const view = await harness(respond);
  await view.openCase('case1'); await view.dispatch('view-analysis');
  view.scopeAnalysisInput({id: 'scope-data-source', value: 'shared'});
  view.scopeAnalysisInput({id: 'scope-run', value: 'shared-old'});
  view.scopeAnalysisInput({id: 'scope-max-hops', value: '9'});
  await view.dispatch('view-plots'); await view.dispatch('view-analysis');
  assert.equal(view.currentScopeDraft(view.state.activeCase).sharedRun, 'shared-old');
  const reloaded = await harness(respond, {storage: view.storage});
  await reloaded.openCase('case1'); await reloaded.dispatch('view-analysis');
  assert.equal(reloaded.currentScopeDraft(reloaded.state.activeCase).sharedRun, 'shared-old');
  assert.equal(reloaded.currentScopeDraft(reloaded.state.activeCase).maxHops, '9');
  assert.equal(reloaded.calls.some(call => call.body), false);
});

test('saved scopes load only selected bounded details and retain complete CSV download access', async () => {
  const selected = scopeFixture('scope1', {name: '<script>scope</script>'});
  const {frontier, ...summary} = selected;
  const pending = Promise.withResolvers();
  const view = await harness(path => path.endsWith('/analyses/scope1') ? pending.promise : undefined);
  view.state.activeCase = workflowCase({analyses: [summary], shared_collection: sharedCollection()});
  await view.dispatch('view-analysis');
  assert.match(view.workspace(), /data-action="scope-use"[^>]*disabled/);
  await assert.rejects(view.dispatch('scope-use', scopeControl()), /finish verifying/);
  pending.resolve(selected); await settled();
  const html = view.workspace();
  assert.match(html, /Scope cutoff comparison/);
  assert.match(html, /Showing 100 of 110 boundaries/);
  assert.match(html, /All open branches CSV/);
  assert.match(html, /&lt;script&gt;scope&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>scope/);
  assert.equal(view.calls.filter(call => call.path.endsWith('/analyses/scope1')).length, 1);
  assert.equal(view.calls.some(call => call.body || /graph\.json|\.csv$/.test(call.path)), false);
});

test('using saved scope pins the exact old shared snapshot without automatically creating a plot', async () => {
  const analysis = scopeFixture();
  const view = await harness(path => path.endsWith('/analyses/scope1') ? analysis
    : path.endsWith('/actions') ? {id: 'plotjob', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({analyses: [analysis], shared_collection: sharedCollection()});
  await view.dispatch('view-analysis'); await settled();
  await view.dispatch('scope-use', scopeControl());
  assert.equal(view.state.caseView, 'plots');
  const draft = view.currentWorkflow(view.state.activeCase);
  assert.equal(draft.dataSource, 'shared'); assert.equal(draft.sharedRun, 'shared-old');
  assert.equal(draft.maxHops, '10'); assert.equal(draft.hopBasis, 'original_seeds');
  assert.equal(draft.goal, 'full'); assert.equal(draft.layoutMode, 'fresh');
  assert.equal(view.calls.some(call => call.body), false);
  await view.dispatch('workflow-plot');
  assert.equal(view.calls.at(-1).body.run_id, 'shared-old');
  assert.equal(view.calls.at(-1).body.max_hops, 10);
  assert.equal(view.calls.at(-1).body.dataset_id, 'pool');
  assert.equal(view.calls.at(-1).body.hop_basis, 'original_seeds');
});

test('scope transfer refuses stale controls and missing snapshots without silently selecting newer evidence', async () => {
  const analysis = scopeFixture();
  const view = await harness(path => path.endsWith('/analyses/scope1') ? analysis : undefined);
  view.state.activeCase = workflowCase({analyses: [analysis], shared_collection: sharedCollection({runs: [{id: 'shared-new'}]})});
  await view.dispatch('view-analysis'); await settled();
  await assert.rejects(view.dispatch('scope-use', scopeControl('other')), /Select a saved scope/);
  await assert.rejects(view.dispatch('scope-use', scopeControl()), /not available/);
  assert.equal(view.state.caseView, 'analysis');
  assert.equal(view.calls.some(call => call.body), false);
});

test('scope detail failure is retryable and background replies do not switch the active investigation', async () => {
  let attempts = 0;
  const late = Promise.withResolvers(), analysis = scopeFixture();
  const {frontier, ...summary} = analysis;
  const view = await harness(path => path.endsWith('/analyses/scope1') ? ++attempts === 1 ? {error: 'Source verification failed'} : late.promise : undefined);
  view.state.activeCase = workflowCase({analyses: [summary]});
  await view.dispatch('view-analysis'); await settled();
  assert.match(view.workspace(), /Source verification failed/);
  assert.match(view.workspace(), /Retry saved analysis/);
  const retry = view.dispatch('scope-details-retry');
  view.state.activeCase = workflowCase({id: 'other', name: 'Other investigation'});
  late.resolve(analysis); await retry;
  assert.equal(view.state.activeCase.id, 'other');
  assert.doesNotMatch(view.workspace(), /4\.12500000/);
  assert.equal(view.calls.some(call => call.body), false);
});

test('scope analysis can run while an unrelated preview is in progress and still respects exclusive operations', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'scopejob', status: 'running', resource_kind: 'plot'} : undefined);
  view.state.activeCase = workflowCase(); await view.dispatch('view-analysis');
  view.state.jobs.set('preview', {id: 'preview', status: 'running', caseId: 'case1', action: 'plot', resource_kind: 'plot'});
  assert.equal(view.actionBusy('scope-analyze'), false);
  await view.dispatch('scope-analyze');
  assert.equal(view.calls.filter(call => call.body).length, 1);
  view.state.jobs.clear();
  view.state.jobs.set('editing', {id: 'editing', status: 'running', caseId: 'case1', action: 'miro-rebuild', resource_kind: 'exclusive'});
  assert.equal(view.actionBusy('scope-analyze'), true);
  await view.dispatch('scope-analyze');
  assert.equal(view.calls.filter(call => call.body).length, 1);
});

test('board deletion is available for managed and legacy boards only with explicit capability', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({boards: [
    workflowBoard('full', 'managed', {can_delete: true}),
    workflowBoard('pegouts', 'legacy', {can_delete: true, can_sync: false, legacy_snapshot: true}),
    workflowBoard('full', 'unsupported'),
  ]});
  await view.dispatch('view-boards');
  for (const record of ['managed', 'legacy']) assert.match(boardCardHtml(view, record), /data-action="workflow-board-delete"/);
  assert.doesNotMatch(boardCardHtml(view, 'unsupported'), /data-action="workflow-board-delete"/);
  await assert.rejects(view.dispatch('workflow-board-delete', boardControl('unsupported')), /not available for deletion/);
  for (const control of [undefined, boardControl('missing'), boardControl('managed', 'other-case')])
    await assert.rejects(view.dispatch('workflow-board-delete', control), /board entry from this investigation/);
  assert.equal(view.calls.length, 1);
});

test('board deletion confirmation shows the exact escaped target and never submits without approval', async () => {
  const view = await harness();
  const board = workflowBoard('full', 'record', {name: 'T3 <review & notes>', board_id: 'board/with?characters', can_delete: true});
  view.state.activeCase = workflowCase({boards: [board]});
  await view.dispatch('view-boards');
  await view.dispatch('workflow-board-delete', boardControl('record'));
  assert.equal(view.dialog.open, true);
  assert.match(view.dialog.innerHTML, /T3 &lt;review &amp; notes&gt;/);
  assert.match(view.dialog.innerHTML, /board\/with\?characters/);
  assert.match(view.dialog.innerHTML, /https:\/\/miro.com\/app\/board\/board%2Fwith%3Fcharacters\//);
  assert.match(view.dialog.innerHTML, /entire board|manual note, comment/);
  assert.match(view.dialog.innerHTML, /Local saved previews, CSV exports, and collected evidence stay available/);
  assert.match(view.dialog.innerHTML, /Proton Pass/);
  await view.submitDialog();
  assert.equal(view.calls.length, 1);
  view.dialog.close();
  await view.submitDialog({confirm_delete: 'on'});
  assert.equal(view.calls.length, 1, 'closed confirmation cannot submit a retained target');
});

test('board deletion submits once with the exact confirmed target and protects local drafts', async () => {
  let release;
  const pending = new Promise(resolve => {release = resolve;});
  const view = await harness(path => path.endsWith('/actions') ? pending : undefined);
  const board = workflowBoard('full', 'record', {can_delete: true});
  const preview = workflowPlot('full', 'preview');
  view.state.activeCase = workflowCase({boards: [board], plots: [preview], miro_board: board.board_id});
  await view.dispatch('view-boards');
  view.currentWorkflow(view.state.activeCase).plot = preview.preview_id;
  await view.dispatch('workflow-board-delete', boardControl('record'));
  await view.submitDialog({confirm_delete: 'on'});
  await view.submitDialog({confirm_delete: 'on'});
  assert.equal(view.calls.filter(call => call.path.endsWith('/actions')).length, 1);
  assert.deepEqual(view.calls.at(-1).body, {action: 'board-delete', record_id: 'record', board_id: board.board_id, confirm_delete: true});
  release({id: 'delete', status: 'running'});
  await new Promise(setImmediate);
  assert.equal(view.dialog.open, false);
  assert.equal(view.state.job.live, true);
  assert.equal(view.state.job.resource_kind, 'board_delete');
  assert.equal(view.state.job.resource_key, board.board_id);
  assert.equal(view.state.activeCase.boards[0].status, 'pending_deletion');
  assert.equal(view.state.activeCase.miro_board, undefined);
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'preview');
  assert.equal(view.state.activeCase.plots[0], preview);
  assert.doesNotMatch(boardCardHtml(view, 'record'), /data-action="workflow-board-sync"|data-action="workflow-board-delete"/);
});

test('board deletion rejects stale dialogs after target changes, case switches, or new conflicts', async () => {
  for (const mutate of [
    view => {view.state.activeCase.boards[0].board_id = 'replacement';},
    view => {view.state.activeCase.boards[0].name = 'Different board name';},
    view => {view.state.activeCase.boards[0].can_delete = false;},
    view => {view.state.activeCase.boards = [];},
    view => {view.state.activeCase = workflowCase({id: 'other-case'});},
    view => {view.state.job = {id: 'conflict', action: 'plot', caseId: 'case1', resource_kind: 'plot'};},
  ]) {
    const view = await harness();
    view.state.activeCase = workflowCase({boards: [workflowBoard('full', 'record', {can_delete: true})]});
    await view.dispatch('view-boards');
    await view.dispatch('workflow-board-delete', boardControl('record'));
    mutate(view);
    await view.submitDialog({confirm_delete: 'on'});
    assert.equal(view.calls.filter(call => call.path.endsWith('/actions')).length, 0);
  }
});

test('board deletion conflicts with every same-case job and matching remote board work across cases', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({boards: [workflowBoard('full', 'record', {can_delete: true})], miro_board: 'miro-record',
    plots: [workflowPlot('full', 'preview', {input_snapshot_version: 1})]});
  await view.dispatch('view-boards');
  const body = {record_id: 'record', board_id: 'miro-record'};
  for (const resource_kind of ['collection', 'shared_collection', 'plot', 'board', 'exclusive', 'board_delete']) {
    view.state.job = {id: 'work', action: 'work', caseId: 'case1', resource_kind, resource_key: 'different'};
    assert.equal(view.actionBusy('board-delete', body), true, resource_kind);
    await view.dispatch('workflow-board-delete', boardControl('record'));
    assert.equal(view.dialog.open, false);
  }
  for (const resource_kind of ['board', 'exclusive', 'board_delete']) {
    view.state.job = {id: 'work', action: 'work', caseId: 'other', resource_kind, resource_key: 'miro-record'};
    assert.equal(view.actionBusy('board-delete', body), true, resource_kind);
    view.state.job.resource_key = 'unrelated';
    assert.equal(view.actionBusy('board-delete', body), false, resource_kind);
  }
  view.state.job = {id: 'delete', action: 'board-delete', caseId: 'other', resource_kind: 'board_delete', resource_key: 'miro-record'};
  assert.equal(view.actionBusy('board-sync', {record_id: 'record', preview_id: 'preview'}), true);
  assert.equal(view.actionBusy('miro-sync'), true);
  assert.equal(view.actionBusy('plot', {layout_mode: 'fresh'}), false);
  view.state.job.caseId = 'case1';
  assert.equal(view.actionBusy('shared-trace'), true);
  assert.equal(view.actionBusy('plot', {layout_mode: 'fresh'}), true);
});

test('board deletion failure preserves the dialog before submission and exposes uncertainty after a task failure', async () => {
  const refused = await harness(path => path.endsWith('/actions') ? {error: 'The board changed; review again.'} : undefined);
  refused.state.activeCase = workflowCase({boards: [workflowBoard('full', 'record', {can_delete: true})]});
  await refused.dispatch('view-boards');
  await refused.dispatch('workflow-board-delete', boardControl('record'));
  await refused.submitDialog({confirm_delete: 'on'});
  assert.equal(refused.dialog.open, true);
  assert.equal(refused.state.activeCase.boards[0].status, 'linked');
  assert.match(refused.state.error, /board changed/);

  const updated = workflowCase({boards: [workflowBoard('full', 'record', {can_delete: true, can_sync: false, status: 'deletion_uncertain', notice: 'Network response was lost.'})]});
  const view = await harness(path => path === '/api/jobs/delete' ? {status: 'failed', message: 'Network response was lost.'}
    : path === '/api/cases/case1/overview' ? updated : undefined);
  view.state.activeCase = workflowCase({boards: [workflowBoard('full', 'record', {can_delete: true})]});
  await view.dispatch('view-boards');
  view.state.job = {id: 'delete', action: 'board-delete', caseId: 'case1', resource_kind: 'board_delete', resource_key: 'miro-record'};
  await view.pollJob();
  assert.equal(view.state.error, 'Network response was lost.');
  assert.match(boardCardHtml(view, 'record'), /Retry delete board|result is unconfirmed/);
  assert.doesNotMatch(boardCardHtml(view, 'record'), /data-action="workflow-board-sync"|Update this board/);
  await view.dispatch('workflow-board-delete', boardControl('record'));
  assert.equal(view.dialog.open, true);
  assert.equal(view.calls.filter(call => call.path.endsWith('/actions')).length, 0);
});

test('board deletion success removes live links and preserves the saved preview through overview and board reloads', async () => {
  const preview = workflowPlot('full', 'preview', {layout_mode: 'fresh', input_snapshot_version: 1});
  const deleted = workflowBoard('full', 'record', {status: 'deleted', can_sync: false, can_delete: false, preview_id: 'preview', creation_preview_id: 'preview'});
  const view = await harness(path => path === '/api/jobs/delete' ? {status: 'succeeded', result: {board_id: 'miro-record', record_id: 'record'}}
    : path === '/api/cases/case1/overview' ? workflowCase({miro_board: null, plots: [preview], sections: {boards: 'unloaded', workflow: 'ready', collection: 'ready', shared: 'ready'}})
    : path === '/api/cases/case1/boards' ? {boards: [deleted]} : undefined);
  view.state.activeCase = workflowCase({miro_board: 'miro-record', plots: [preview], boards: [{...deleted, status: 'synced', can_delete: true, can_sync: true}]});
  await view.dispatch('view-boards');
  view.currentWorkflow(view.state.activeCase).plot = 'preview';
  view.state.job = {id: 'delete', action: 'board-delete', caseId: 'case1', resource_kind: 'board_delete', resource_key: 'miro-record'};
  await view.pollJob();
  await new Promise(setImmediate);
  assert.equal(view.state.activeCase.miro_board, null);
  assert.equal(view.state.activeCase.plots[0].preview_id, 'preview');
  assert.equal(view.currentWorkflow(view.state.activeCase).plot, 'preview');
  assert.ok(view.calls.some(call => call.path === '/api/cases/case1/overview'));
  assert.ok(view.calls.some(call => call.path === '/api/cases/case1/boards'));
  assert.match(boardCardHtml(view, 'record'), /This Miro board was deleted/);
  assert.doesNotMatch(view.workspace(), /https:\/\/miro.com\/app\/board\/miro-record/);
  assert.doesNotMatch(boardCardHtml(view, 'record'), /data-action="workflow-board-(sync|delete|prepare)"/);
  assert.match(view.workspace(), /local preview remains available/);
});

test('board deletion rejected state retains authorized sync and offers an explicit retry', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({plots: [workflowPlot('full', 'preview')], boards: [workflowBoard('full', 'record',
    {can_delete: true, can_sync: true, status: 'deletion_rejected', preview_id: 'preview'})]});
  await view.dispatch('view-boards');
  const card = boardCardHtml(view, 'record');
  assert.match(card, /Miro rejected the deletion request/);
  assert.match(card, /Retry delete board/);
  assert.match(card, /data-action="workflow-board-sync"/);
});


test('shortest Starter board update restores saved scope and preserves the bounded new-board draft', async () => {
  const plot = workflowPlot('connections', 'shortest', {max_hops: null,
    query: {connection_scope: 'shortest', transaction_io: 'complete'},
    collection_source: {kind: 'shared', dataset_id: 'pool', run_id: 'shared-old'}});
  const detail = workflowCase({shared_collection: sharedCollection(), plots: [plot],
    boards: [workflowBoard('connections', 'shortest-board', {preview_id: plot.preview_id})]});
  const view = await harness(path => path.endsWith('/actions') ? {id: 'shortest-update', status: 'running'} : undefined);
  view.state.activeCase = detail; await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'connections'}});
  workflowEdit(view, 'connection-max-hops', '4');
  await view.dispatch('workflow-board-prepare', boardControl('shortest-board'));
  assert.equal(view.currentWorkflow(detail).connectionScope, 'shortest');
  assert.doesNotMatch(view.workspace(), /id="workflow-connection-max-hops"/);
  await view.dispatch('workflow-plot-sync');
  assert.equal(view.calls.at(-1).body.connection_scope, 'shortest');
  assert.equal(view.calls.at(-1).body.max_hops, 0);
  assert.equal(view.calls.at(-1).body.data_source, 'shared');
  assert.equal(view.calls.at(-1).body.run_id, 'shared-old');
  view.state.jobs.clear();
  workflowEdit(view, 'layout-mode', 'fresh');
  assert.equal(view.currentWorkflow(detail).connectionScope, 'hop_limited');
  assert.equal(view.currentWorkflow(detail).connectionMaxHops, '4');
});

test('empty shortest Starter preview describes saved coverage without suggesting a larger hop limit', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase({plots: [workflowPlot('connections', 'empty-shortest', {
    empty: true, node_count: 0, edge_count: 0, max_hops: null, layout_mode: 'fresh',
    query: {connection_scope: 'shortest'},
  })]});
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /No starter connections found in the saved evidence/);
  assert.match(view.workspace(), /Shortest connections already searches without a hop cutoff/);
  assert.doesNotMatch(view.workspace(), /Increase the connection hop limit or choose All saved connections/);
});

test('all-starter previews describe disconnected starters and remain available to write with zero pairs', async () => {
  for (const [pairs, unconnected] of [[0, 3], [1, 1]]) {
    const view = await harness();
    view.state.activeCase = workflowCase({plots: [workflowPlot('connections', 'all-starters', {
      max_hops: null, layout_mode: 'fresh', empty: false,
      query: {connection_scope: 'shortest', transaction_io: 'complete'},
      includes_all_starters: true, starting_transaction_count: 3,
      unconnected_starting_transaction_count: unconnected, connection_count: pairs,
    })]});
    await view.dispatch('view-plots');
    const html = view.workspace();
    assert.match(html, /All 3 starting transactions are shown/);
    assert.match(html, new RegExp(`${unconnected} ${unconnected === 1 ? 'has' : 'have'} no qualifying connection to another starter`));
    assert.ok(html.includes(`${pairs} ordered starter pair(s) connected.`));
    assert.doesNotMatch(html.match(/<button[^>]*data-action="workflow-write-miro"[^>]*>/)[0], /disabled/);
    assert.match(html, /Open preview/);
    assert.doesNotMatch(html, /Nothing is plotted|No starter connections found/);
  }
});

test('zero-pair standalone previews show all-starter artifacts while legacy empty previews remain empty', async () => {
  for (const includesAll of [true, false]) {
    const view = await harness();
    view.state.activeCase = workflowCase({artifacts: {saved1: {connections: {
      downloads: [], include_fees: false, connection_scope: 'shortest', connection_count: 0,
      preview_id: 'starter-preview', preview_url: '/files/case1/previews/starter-preview/graph.html',
      ...(includesAll ? {includes_all_starters: true, starting_transaction_count: 3,
        unconnected_starting_transaction_count: 3} : {}),
    }}}});
    await view.dispatch('view-history');
    const html = view.workspace();
    if (includesAll) {
      assert.match(html, /All 3 starting transactions are shown/);
      assert.match(html, /title="Starter connection graph"/);
      assert.match(html, /Publish this reviewed snapshot to Miro/);
      assert.doesNotMatch(html, /Nothing is plotted/);
    } else {
      assert.match(html, /Nothing is plotted/);
      assert.doesNotMatch(html, /title="Starter connection graph"|Publish this reviewed snapshot to Miro|All 3 starting transactions/);
    }
  }
});

test('standalone completion preserves all-starter counts and does not announce an empty zero-pair chart', async () => {
  const result = {run_id: 'saved1', connection_count: 0, connection_scope: 'shortest',
    includes_all_starters: true, starting_transaction_count: 3,
    unconnected_starting_transaction_count: 3, include_fees: false, downloads: [],
    preview_id: 'starter-preview', preview_url: '/files/case1/previews/starter-preview/graph.html'};
  const view = await harness(path => path === '/api/jobs/starters' ? {status: 'succeeded', result}
    : path === '/api/cases/case1/overview' ? workflowCase() : undefined);
  view.state.activeCase = workflowCase();
  await view.dispatch('view-history');
  view.state.job = {id: 'starters', action: 'connections', caseId: 'case1'};
  await view.pollJob();
  assert.match(view.notifications.join(' '), /Starter connection chart is ready\. All starting transactions are shown/);
  assert.doesNotMatch(view.notifications.join(' '), /Nothing plotted/);
  assert.equal(view.state.artifacts.get('case1:saved1').connections.starting_transaction_count, 3);
  assert.equal(view.state.artifacts.get('case1:saved1').connections.unconnected_starting_transaction_count, 3);
  assert.match(view.workspace(), /All 3 starting transactions are shown/);
});


function deletionCase(id = 'case1', name = 'Delete me') {
  return workflowCase({id, name, seeds: [], runs: [], plots: [], boards: []});
}

function deletionJob(id = 'delete', caseId = 'case1', status = 'running') {
  return {id, case_id: caseId, action: 'investigation-delete', status, resource_kind: 'exclusive',
    live: false, cancellable: false, message: 'Deleting investigation…'};
}

test('investigation deletion confirms its exact escaped name and cancel sends no request', async () => {
  const view = await harness();
  const detail = deletionCase('case1', 'Theft <1> & notes');
  view.state.activeCase = detail; view.state.page = 'case';
  assert.match(view.workspace(), /data-action="investigation-delete"/);
  await view.dispatch('investigation-delete');
  assert.equal(view.dialog.open, true);
  assert.match(view.dialog.innerHTML, /Theft &lt;1&gt; &amp; notes/);
  assert.match(view.dialog.innerHTML, /cannot be undone/);
  assert.match(view.dialog.innerHTML, /local collection runs, previews, exports, annotations, and settings/);
  assert.match(view.dialog.innerHTML, /Shared collection data, other investigations, and Miro boards are retained/);
  for (const confirm_name of ['', 'theft <1> & notes', 'Theft <1> & notes ']) await view.submitDialog({confirm_name});
  assert.equal(view.calls.length, 1);
  await view.dispatch('close-dialog');
  await view.submitDialog({confirm_name: detail.name});
  assert.equal(view.calls.length, 1);
});

test('investigation deletion submits once and cannot be canceled as a calculation', async () => {
  let release;
  const pending = new Promise(resolve => {release = resolve;});
  const view = await harness(path => path.endsWith('/actions') ? pending : undefined);
  view.state.activeCase = deletionCase('case/1'); view.state.page = 'case';
  await view.dispatch('investigation-delete');
  await view.submitDialog({confirm_name: 'Delete me'});
  await view.submitDialog({confirm_name: 'Delete me'});
  const requests = view.calls.filter(call => call.path.endsWith('/actions'));
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, '/api/cases/case%2F1/actions');
  assert.deepEqual(requests[0].body, {action: 'investigation-delete', confirm_name: 'Delete me'});
  release(deletionJob('delete', 'case/1')); await new Promise(setImmediate);
  assert.equal(view.dialog.open, false);
  assert.equal(view.state.jobs.get('delete').live, false);
  // Even a stale cancellation flag must never turn deletion into a cancelable task.
  view.state.jobs.get('delete').cancellable = true;
  assert.doesNotMatch(view.jobBanner(), /data-action="cancel-job"/);
  await view.cancelJob('delete');
  assert.equal(view.calls.some(call => call.path.endsWith('/cancel')), false);
});

test('investigation deletion blocks case tasks and shared work and rechecks dialog ownership', async () => {
  for (const mutate of [
    view => {view.state.activeCase.name = 'Renamed';},
    view => {view.state.activeCase = deletionCase('other');},
    view => {view.navigate('dashboard');},
    ...['trace', 'plot', 'board-sync', 'change-output-lookup'].map(action => view => {
      view.state.jobs.set('work', {id: 'work', caseId: 'case1', action, status: 'running'});
    }),
    view => {view.state.jobs.set('work', {id: 'work', caseId: 'other', action: 'shared-trace', status: 'running'});},
  ]) {
    const view = await harness();
    view.state.activeCase = deletionCase(); view.state.page = 'case';
    await view.dispatch('investigation-delete');
    assert.equal(view.dialog.open, true);
    mutate(view);
    await view.submitDialog({confirm_name: 'Delete me'});
    assert.equal(view.calls.some(call => call.path.endsWith('/actions')), false);
  }
  const view = await harness();
  view.state.activeCase = deletionCase(); view.state.page = 'case';
  view.state.jobs.set('shared', {id: 'shared', caseId: 'other', action: 'shared-trace', status: 'running'});
  await view.dispatch('investigation-delete');
  assert.equal(view.dialog.open, false);
  view.state.jobs.clear();
  view.state.jobs.set('delete', {...deletionJob(), caseId: 'case1'});
  for (const action of ['trace', 'plot', 'board-sync', 'shared-trace']) assert.equal(view.actionBusy(action, {}, 'case1'), true);
  assert.equal(view.actionBusy('shared-trace', {}, 'other'), true);
  assert.equal(view.actionBusy('plot', {}, 'other'), false);
});

test('investigation deletion failure preserves investigation, tabs and saved data', async () => {
  for (const failOnSubmit of [true, false]) {
    const detail = deletionCase();
    const view = await harness(path => path.endsWith('/actions') ? failOnSubmit ? {error: 'Busy investigation'} : deletionJob()
      : path === '/api/jobs/delete' ? {...deletionJob(), status: 'failed', message: 'Local data could not be deleted.'}
      : path === '/api/session' ? {csrf: 'test', settings: defaults, cases: [detail]} : undefined);
    view.state.activeCase = detail; view.state.page = 'case'; view.state.openCases = ['case1'];
    await view.dispatch('investigation-delete');
    await view.submitDialog({confirm_name: 'Delete me'});
    if (failOnSubmit) assert.equal(view.dialog.open, true);
    else await view.pollJob('delete');
    assert.equal(view.state.activeCase.id, 'case1');
    assert.deepEqual([...view.state.openCases], ['case1']);
    assert.equal(view.state.cases[0].id, 'case1');
    assert.match(view.state.error, failOnSubmit ? /Busy investigation/ : /could not be deleted/);
  }
});

test('successful investigation deletion clears its tabs and caches without refreshing its deleted overview', async () => {
  const detail = deletionCase(), other = deletionCase('other', 'Keep me');
  const view = await harness(path => path === '/api/jobs/delete' ? {...deletionJob(), status: 'succeeded',
    result: {case_id: 'case1', name: detail.name, deleted: true, cleanup_pending: false}}
    : path === '/api/session' ? {csrf: 'test', settings: defaults, cases: [detail, other]} : undefined);
  view.state.activeCase = detail; view.state.page = 'case'; view.state.openCases = ['case1', 'other'];
  view.currentWorkflow(detail).plot = 'private-preview';
  view.currentWorkflow(other).plot = 'retained-preview';
  view.currentWorkflow(detail);
  view.currentScopeDraft(detail).selected = 'private-analysis';
  view.storage.set('liquid-tracer:scope-selection:v1:case1', '{"selected":"private-analysis"}');
  view.state.results.set('case1', {action: 'plot', result: {preview_id: 'private-preview'}});
  view.state.artifacts.set('case1:run1', {}); view.state.artifacts.set('other:run1', {});
  view.deleteCaches.sectionErrors.set('case1:workflow', 'private error');
  view.deleteCaches.scopeDetails.set('case1:analysis', {analysis_id: 'analysis'});
  view.state.jobs.set('delete', {...deletionJob(), caseId: 'case1', started: Date.now()});
  await view.pollJob('delete'); await new Promise(setImmediate);
  assert.equal(view.state.activeCase, null);
  assert.equal(view.state.page, 'dashboard');
  assert.deepEqual([...view.state.openCases], ['other']);
  assert.deepEqual(view.state.cases.map(item => item.id), ['other']);
  assert.equal(view.state.results.has('case1'), false);
  assert.equal(view.state.artifacts.has('case1:run1'), false);
  assert.equal(view.state.artifacts.has('other:run1'), true);
  for (const cache of Object.values(view.deleteCaches)) assert.equal([...cache.keys()].some(key => key === 'case1' || key.startsWith('case1:') || key.startsWith('case1/')), false);
  assert.equal(view.storage.has('liquid-tracer:scope-selection:v1:case1'), false);
  assert.deepEqual(JSON.parse(view.storage.get('liquid-tracer:investigation-tabs:v1')).ids, ['other']);
  assert.equal(view.currentWorkflow(other).plot, 'retained-preview');
  assert.equal(view.calls.some(call => call.path.startsWith('/api/cases/case1/')), false);
  assert.doesNotMatch(view.jobBanner(), /data-action="open-job"/);
  const before = view.calls.length;
  await view.openCase('case1');
  assert.equal(view.calls.length, before, 'old task links cannot reopen the deleted investigation');
});

test('deleting a background investigation retains the selected investigation and its drafts', async () => {
  const detail = deletionCase(), other = deletionCase('other', 'Keep me');
  const view = await harness(path => path === '/api/jobs/delete' ? {...deletionJob(), status: 'succeeded',
    result: {case_id: 'case1', name: detail.name, deleted: true, cleanup_pending: true}}
    : path === '/api/session' ? {csrf: 'test', settings: defaults, cases: [other]} : undefined);
  view.state.activeCase = other; view.state.page = 'case'; view.state.openCases = ['case1', 'other'];
  view.currentWorkflow(other).plot = 'keep-preview';
  view.state.jobs.set('delete', {...deletionJob(), caseId: 'case1', started: Date.now()});
  await view.pollJob('delete');
  assert.equal(view.state.activeCase.id, 'other');
  assert.equal(view.state.page, 'case');
  assert.equal(view.currentWorkflow(other).plot, 'keep-preview');
  assert.deepEqual([...view.state.openCases], ['other']);
  assert.match(view.notifications.at(-1), /local files still need cleanup/);
});

test('late overview and session responses cannot resurrect a deleted investigation', async () => {
  const detail = deletionCase();
  let releaseOverview, releaseSession, sessions = 0;
  const overview = new Promise(resolve => {releaseOverview = resolve;});
  const session = new Promise(resolve => {releaseSession = resolve;});
  const view = await harness(path => path === '/api/jobs/delete' ? {...deletionJob(), status: 'succeeded',
    result: {case_id: 'case1', name: detail.name, deleted: true}}
    : path.endsWith('/overview') ? overview
    : path === '/api/session' ? (++sessions === 2 ? session : {csrf: 'test', settings: defaults, cases: sessions === 1 ? [detail] : []}) : undefined);
  view.state.activeCase = detail; view.state.page = 'case'; view.state.openCases = ['case1'];
  const oldOverview = view.refreshCaseDetail('case1'), oldSession = view.refreshSession();
  view.state.jobs.set('delete', {...deletionJob(), caseId: 'case1', started: Date.now()});
  await view.pollJob('delete');
  releaseOverview(detail); releaseSession({csrf: 'old', settings: defaults, cases: [detail]});
  await Promise.all([oldOverview, oldSession]);
  assert.equal(view.state.activeCase, null);
  assert.equal(view.state.page, 'dashboard');
  assert.equal(view.state.cases.length, 0);
  assert.equal(view.state.openCases.length, 0);
});

test('discovered deletion closes its tab and a browser reload excludes removed investigations', async () => {
  const detail = deletionCase(), other = deletionCase('other', 'Keep me');
  const view = await harness(path => path === '/api/jobs' ? {jobs: [{...deletionJob(), status: 'succeeded',
    result: {case_id: 'case1', name: detail.name, deleted: true}}]}
    : path === '/api/session' ? {csrf: 'test', settings: defaults, cases: [detail, other]} : undefined);
  view.state.activeCase = detail; view.state.page = 'case'; view.state.openCases = ['case1', 'other'];
  await view.discoverJobs();
  assert.equal(view.state.activeCase, null);
  assert.deepEqual([...view.state.openCases], ['other']);
  assert.equal(view.calls.some(call => call.path.includes('/case1/overview')), false);
  view.storage.set('liquid-tracer:investigation-tabs:v1', JSON.stringify({ids: ['case1', 'other'], views: []}));
  const reloaded = await harness(path => path === '/api/session' ? {csrf: 'test', settings: defaults, cases: [other]} : undefined,
    {storage: view.storage});
  assert.deepEqual([...reloaded.state.openCases], ['other']);
});


test('a transient missing investigation listing does not erase drafts or prevent reopening', async () => {
  const detail = deletionCase(); let sessions = 0;
  const view = await harness(path => path === '/api/session' ? {csrf: 'test', settings: defaults,
    cases: ++sessions === 2 ? [] : [detail]} : path.endsWith('/overview') ? detail : undefined);
  view.state.activeCase = detail; view.state.page = 'case'; view.state.openCases = ['case1'];
  view.currentWorkflow(detail).plot = 'retain-preview';
  await view.refreshSession();
  assert.equal(view.state.cases.length, 0);
  assert.equal(view.state.activeCase.id, detail.id);
  assert.equal(view.currentWorkflow(detail).plot, 'retain-preview');
  await view.refreshSession();
  await view.openCase('case1');
  assert.equal(view.state.activeCase.id, detail.id);
  assert.equal(view.currentWorkflow(detail).plot, 'retain-preview');
  assert.equal(view.calls.filter(call => call.path.endsWith('/overview')).length, 1);
});

test('in-progress investigation deletion disables reopening through task links and tabs', async () => {
  const view = await harness();
  view.state.activeCase = deletionCase(); view.state.page = 'case'; view.state.openCases = ['case1'];
  view.state.jobs.set('delete', {...deletionJob(), caseId: 'case1', started: Date.now()});
  assert.doesNotMatch(view.jobBanner(), /data-action="open-job"/);
  await view.openCase('case1');
  assert.equal(view.calls.some(call => call.path.endsWith('/overview')), false);
  await view.dispatch('open-job', {dataset: {id: 'delete'}});
  assert.equal(view.calls.some(call => call.path.endsWith('/overview')), false);
  view.render();
  assert.match(view.app.innerHTML, /investigation-tab-open[^>]+ disabled/);
});

test('shared attribution access belongs to workspace or investigation settings and preserves unsaved settings', async () => {
  const owners = [], opened = new Set();
  const view = await harness(undefined, {sharedAttributionHandlers: {
    sharedAttributionsAction: async (action, context) => {
      if (!action.startsWith('shared-attributions-')) return false;
      owners.push(context.caseId); opened.add(context.caseId); context.render(); return true;
    },
    sharedAttributionsPanel: caseId => opened.has(caseId) ? '<section id="shared-attributions-panel">Shared library panel</section>' : '',
  }});
  view.state.activeCase = workflowCase();
  view.navigate('settings');
  assert.match(view.app.innerHTML, /data-action="shared-attributions-open"/);
  await view.dispatch('shared-attributions-open');
  assert.equal(owners[0], null, 'workspace access does not inherit the last active investigation');
  assert.ok(view.settingsPage().indexOf('id="shared-attributions-panel"') < view.settingsPage().indexOf('id="settings-form"'));
  await view.dispatch('case-settings');
  view.elements.set('#settings-form', {values: {...defaults, name: 'Unsaved investigation name', hops: '7'}});
  await view.dispatch('shared-attributions-open');
  const html = view.settingsPage();
  assert.equal(owners[1], 'case1');
  assert.match(html, /value="Unsaved investigation name"/);
  assert.match(html, /name="hops"[^>]*value="7"/);
  assert.ok(html.indexOf('id="shared-attributions-panel"') < html.indexOf('id="settings-form"'));
  view.navigate('dashboard');
  await view.dispatch('shared-attributions-use-apply');
  assert.equal(owners.length, 2, 'a stale shared control cannot act outside its settings page');
  assert.equal(view.calls.filter(call => call.body).length, 0);
});

test('new address assessments keep tracing unless the analyst explicitly enables a stop', async () => {
  const view = await harness((path, body) => path.endsWith('/services')
    ? {revision: 1, service: {...body, updated_at: '2026-10-05'}} : undefined);
  view.state.activeCase = workflowCase(); view.state.page = 'addresses';
  assert.equal(view.state.addressReview.stopTracing, false);
  const rows = [{address: 'new-address'}, {address: 'saved-stop', service: {enabled: true, stop_tracing: true}},
    {address: 'saved-label', service: {enabled: true, stop_tracing: false}}];
  view.state.addressReview.data = {rows, total: rows.length, offset: 0, limit: 25};
  for (const [address, expected] of [['saved-stop', true], ['saved-label', false], ['new-address', false]]) {
    await view.dispatch('address-select', {dataset: {address}});
    assert.equal(view.state.addressReview.stopTracing, expected);
  }
  await view.submitService({service_name: 'Example exchange', service_enabled: 'on', service_source: 'Analyst records'});
  assert.equal(view.calls.find(call => call.path.endsWith('/services')).body.stop_tracing, false);
});

test('inherited shared assessments identify their origin and explain that saving creates a local override', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase(); view.state.page = 'addresses';
  const row = {address: 'shared-address', activity: null, service: {address: 'shared-address', name: 'Shared exchange',
    confidence: 'suspected', source: 'Research', notes: '', enabled: true, stop_tracing: false,
    updated_at: '2026-10-03T00:00:00Z', attribution_origin: 'shared', shared_library_id: 'library', shared_revision: 3}};
  view.state.addressReview.selected = row;
  view.state.addressReview.data = {run_id: 'saved1', rows: [row], total: 1, offset: 0, limit: 100};
  view.render();
  assert.match(view.app.innerHTML, /<span class="badge gray">Shared<\/span>/);
  assert.match(view.app.innerHTML, /Saving or disabling this assessment creates a local override for this investigation/);
  assert.match(view.app.innerHTML, /Shared library entry updated/);
  assert.match(view.app.innerHTML, /name="service_stop"/);
  assert.match(view.app.innerHTML, /name="service_hop_limit"/);
});

test('shared request completion releases controls after leaving and returning to the same settings owner', async () => {
  for (const page of ['settings', 'case-settings']) {
    const response = Promise.withResolvers(); let pending = false;
    const view = await harness(undefined, {sharedAttributionHandlers: {
      sharedAttributionsAction: async (action, context) => {
        if (!action.startsWith('shared-attributions-')) return false;
        pending = true; context.render();
        await response.promise;
        pending = false; context.render(); return true;
      },
      sharedAttributionsPanel: () => `<button id="shared-completion-control"${pending ? ' disabled' : ''}>${pending ? 'Loading shared library' : 'Shared library ready'}</button>`,
    }});
    view.state.activeCase = workflowCase(); view.navigate(page);
    const work = view.dispatch('shared-attributions-open'); await settled();
    view.navigate('dashboard'); view.navigate(page);
    assert.match(view.app.innerHTML, /id="shared-completion-control" disabled/);
    response.resolve(); await work;
    assert.match(view.app.innerHTML, /id="shared-completion-control">Shared library ready/);
    assert.doesNotMatch(view.app.innerHTML, /Loading shared library/);
  }
});

test('shared file completion releases controls after leaving and returning to the same settings owner', async () => {
  for (const page of ['settings', 'case-settings']) {
    const response = Promise.withResolvers(); let pending = false;
    const view = await harness(undefined, {sharedAttributionHandlers: {
      sharedAttributionsFile: async (_input, render) => {
        pending = true; render(); await response.promise; pending = false; render();
      },
      sharedAttributionsPanel: () => `<button id="shared-file-control"${pending ? ' disabled' : ''}>${pending ? 'Reading file' : 'File ready'}</button>`,
    }});
    view.state.activeCase = workflowCase(); view.navigate(page);
    view.chooseSharedAttributionsFile();
    view.navigate('dashboard'); view.navigate(page);
    assert.match(view.app.innerHTML, /id="shared-file-control" disabled/);
    response.resolve(); await settled();
    assert.match(view.app.innerHTML, /id="shared-file-control">File ready/);
    assert.doesNotMatch(view.app.innerHTML, /Reading file/);
  }
});

test('shared library apply preserves remembered and in-flight local assessment drafts including opted-out cases', async () => {
  const response = Promise.withResolvers();
  const active = workflowCase({id: 'opted-out', name: 'Opted out investigation'});
  const other = workflowCase({id: 'remembered', name: 'Remembered investigation'});
  const local = {address: 'local-address', activity: null, service: {name: 'Saved local name', enabled: true,
    confidence: 'confirmed', source: 'Local records', stop_tracing: false, attribution_origin: 'local'}};
  const inherited = {address: 'shared-address', activity: null, service: {name: 'Shared name', enabled: true,
    confidence: 'suspected', source: 'Shared evidence', stop_tracing: false, attribution_origin: 'shared'}};
  const view = await harness((path, body) => path.endsWith('/services') ? {revision: 2, service: {...body, updated_at: '2026-10-03'}} : undefined,
    {sharedAttributionHandlers: {
      sharedAttributionsAction: async (action, context) => {
        if (!action.startsWith('shared-attributions-')) return false;
        await response.promise; await context.refresh('library'); context.render(); return true;
      },
    }});
  view.state.cases = [active, other]; view.state.openCases = [active.id, other.id];
  view.state.activeCase = active; view.navigate('settings');
  const remembered = {...view.state.addressReview, selected: inherited,
    data: {rows: [inherited], total: 1, offset: 0, limit: 25}, name: 'Unsaved remembered name',
    notes: 'Unsaved remembered notes', enabled: false, stopTracing: true, hopLimit: '3'};
  view.deleteCaches.investigationViews.set(other.id, {page: 'addresses', caseView: 'collect', selectedRun: 'saved1', addressReview: remembered});
  const work = view.dispatch('shared-attributions-apply'); await settled();
  view.state.addressReview = {...view.state.addressReview, selected: local,
    data: {rows: [local], total: 1, offset: 0, limit: 25}};
  view.navigate('addresses');
  const edits = {service_name: 'Unsaved active name', service_notes: 'Unsaved active notes', service_enabled: 'on',
    service_confidence: 'confirmed', service_source: 'Local draft source', service_observed: '2026-10-02', service_hop_limit: '5'};
  view.elements.set('#service-form', {values: edits});
  response.resolve(); await work;
  assert.equal(view.state.addressReview.selected.address, local.address);
  assert.equal(view.state.addressReview.selected.attributionStale, undefined, 'a local override does not inherit a changed shared baseline');
  for (const [key, value] of Object.entries({name: edits.service_name, notes: edits.service_notes, source: edits.service_source,
    observedAt: edits.service_observed, hopLimit: '5', enabled: true, stopTracing: false}))
    assert.equal(view.state.addressReview[key], value);
  assert.equal(view.state.addressReview.data, null, 'the list reloads without replacing its selected draft');
  assert.equal(remembered.selected.address, inherited.address);
  assert.equal(remembered.selected.attributionStale, true);
  assert.equal(remembered.name, 'Unsaved remembered name');
  assert.equal(remembered.notes, 'Unsaved remembered notes');
  assert.equal(remembered.enabled, false); assert.equal(remembered.stopTracing, true); assert.equal(remembered.hopLimit, '3');
  await view.submitService(edits);
  const save = view.calls.find(call => call.path === '/api/cases/opted-out/services');
  assert.ok(save, 'the selected address remains saveable after the background import completes');
  assert.equal(save.body.name, edits.service_name); assert.equal(save.body.notes, edits.service_notes);
  assert.equal(save.body.hop_limit, '5'); assert.equal(save.body.stop_tracing, false);
});

test('seed editor opens from the workspace and saves the exact case selection without rewriting saved evidence', async () => {
  const oldSeeds = [`${txid}:0`], replacement = 'b'.repeat(64);
  const snapshot = {id: 'saved1', status: 'bounded_complete', seeds: oldSeeds};
  const preview = workflowPlot('full', 'recorded-preview', {seeds: oldSeeds});
  const detail = workflowCase({id: 'case /?#', runs: [snapshot], plots: [preview]});
  const view = await harness((path, body) => path === '/api/cases/case%20%2F%3F%23/seeds'
    ? body ? {seeds: body.seeds, revision: 5} : {seeds: oldSeeds, revision: 4} : undefined);
  view.state.activeCase = detail; view.state.cases = [detail]; view.state.page = 'case';
  assert.match(view.workspace(), /data-action="seed-editor-open"/);
  await view.dispatch('seed-editor-open');
  assert.equal(view.state.page, 'case-settings');
  assert.match(view.app.innerHTML, /Change starting outputs/);
  view.editSeeds(`${replacement.toUpperCase()}:01,${replacement}:1`);
  await view.dispatch('seed-editor-save');
  const calls = view.calls.filter(call => call.path.endsWith('/seeds'));
  assert.deepEqual(calls, [{path: '/api/cases/case%20%2F%3F%23/seeds', body: undefined},
    {path: '/api/cases/case%20%2F%3F%23/seeds', body: {seeds: [`${replacement}:1`], expected_revision: 4}}]);
  assert.deepEqual([...view.state.activeCase.seeds], [`${replacement}:1`]);
  assert.deepEqual([...view.state.cases[0].seeds], [`${replacement}:1`]);
  assert.deepEqual(snapshot.seeds, oldSeeds);
  assert.deepEqual(preview.seeds, oldSeeds);
  assert.equal(view.calls.filter(call => call.body !== undefined).length, 1);
  assert.match(view.app.innerHTML, /Existing previews keep their original outputs/);
});

test('seed editor stale revision errors retain user edits and never overwrite current case state', async () => {
  const detail = workflowCase(), replacement = 'b'.repeat(64);
  const view = await harness((path, body) => path.endsWith('/seeds') ? body
    ? {error: 'Starting outputs changed. Reload saved selection before saving.'}
    : {seeds: detail.seeds, revision: 2} : undefined);
  view.state.activeCase = detail; view.state.page = 'case';
  await view.dispatch('seed-editor-open'); view.editSeeds(`${replacement}:1`);
  await view.dispatch('seed-editor-save');
  assert.deepEqual(view.state.activeCase.seeds, detail.seeds);
  assert.match(view.app.innerHTML, /Starting outputs changed. Reload saved selection before saving/);
  assert.match(view.app.innerHTML, new RegExp(`>${replacement}:1<\/textarea>`));
  assert.equal(view.isBusy(), false);
});

test('seed editor completion after navigation updates only its owning case and leaves another case untouched', async () => {
  const response = Promise.withResolvers(), replacement = 'b'.repeat(64);
  const first = workflowCase(), second = workflowCase({id: 'case2', name: 'Other investigation', seeds: [`${'c'.repeat(64)}:2`]});
  const view = await harness((path, body) => path === '/api/cases/case1/seeds'
    ? body ? response.promise : {seeds: first.seeds, revision: 3}
    : path === '/api/cases/case2/overview' ? second : undefined);
  view.state.activeCase = first; view.state.cases = [first, second]; view.state.page = 'case';
  await view.dispatch('seed-editor-open'); view.editSeeds(`${replacement}:1`);
  const work = view.dispatch('seed-editor-save'); await settled();
  assert.equal(view.isBusy(), true);
  await view.openCase('case2'); const before = view.app.innerHTML;
  response.resolve({seeds: [`${replacement}:1`], revision: 4}); await work;
  assert.equal(view.state.activeCase.id, 'case2');
  assert.deepEqual(view.state.activeCase.seeds, second.seeds);
  assert.equal(view.app.innerHTML, before);
  assert.deepEqual([...view.state.cases.find(item => item.id === 'case1').seeds], [`${replacement}:1`]);
  assert.equal(view.isBusy(), false);
});

test('seed editor pending writes block duplicate saves and retain close and reopen', async () => {
  const response = Promise.withResolvers(), replacement = 'b'.repeat(64), detail = workflowCase();
  const view = await harness((path, body) => path.endsWith('/seeds') ? body
    ? response.promise : {seeds: detail.seeds, revision: 7} : undefined);
  view.state.activeCase = detail; view.state.page = 'case';
  await view.dispatch('seed-editor-open'); view.editSeeds(`${replacement}:2`);
  const work = view.dispatch('seed-editor-save'); await settled();
  await view.dispatch('seed-editor-save'); await view.dispatch('seed-editor-reload');
  assert.equal(view.calls.filter(call => call.body !== undefined).length, 1);
  assert.equal(view.isBusy(), true);
  assert.match(view.app.innerHTML, /data-action="seed-editor-save" disabled/);
  await view.dispatch('seed-editor-close');
  assert.doesNotMatch(view.app.innerHTML, /id="seed-editor-panel"/);
  response.resolve({seeds: [`${replacement}:2`], revision: 8}); await work;
  await view.dispatch('seed-editor-open');
  assert.equal(view.calls.filter(call => call.path.endsWith('/seeds') && !call.body).length, 1);
  assert.match(view.app.innerHTML, new RegExp(`>${replacement}:2<\/textarea>`));
  assert.equal(view.isBusy(), false);
});

test('seed editor unsaved text survives closing and reopening without changing unrelated settings drafts', async () => {
  const detail = workflowCase(), replacement = 'b'.repeat(64);
  const view = await harness(path => path.endsWith('/seeds') ? {seeds: detail.seeds, revision: 1} : undefined);
  view.state.activeCase = detail; view.navigate('case-settings');
  view.elements.set('#settings-form', {values: {...defaults, name: 'Keep this unsaved name', hops: '7'}});
  await view.dispatch('seed-editor-open'); view.editSeeds(`${replacement}:0`);
  await view.dispatch('seed-editor-close'); await view.dispatch('seed-editor-open');
  assert.match(view.app.innerHTML, new RegExp(`>${replacement}:0<\/textarea>`));
  assert.match(view.app.innerHTML, /value="Keep this unsaved name"/);
  assert.match(view.app.innerHTML, /name="hops"[^>]*value="7"/);
  assert.equal(view.calls.filter(call => call.path.endsWith('/seeds')).length, 1);
  assert.deepEqual(view.state.activeCase.seeds, detail.seeds);
});

test('seed display identifies changed current selection while an older snapshot retains its captured outputs', async () => {
  const replacement = 'b'.repeat(64), view = await harness();
  view.state.activeCase = workflowCase({seeds: [`${replacement}:1`],
    runs: [{id: 'saved1', status: 'bounded_complete', seeds: [`${txid}:0`]}]});
  view.state.page = 'case';
  const panel = view.workspace().match(/<section class="panel seed-panel"[\s\S]*?<\/section>/)[0];
  assert.match(panel, /different starting outputs from the investigation’s current selection/);
  assert.match(panel, /rows below describe this snapshot/);
  assert.match(panel, new RegExp(txid));
  assert.doesNotMatch(panel, new RegExp(replacement));
});

for (const latestMatches of [true, false]) {
  test(`collection uses latest snapshot seeds, not the viewed historical snapshot, when current selection ${latestMatches ? 'matches' : 'differs'}`, async () => {
    const replacement = 'b'.repeat(64), current = [`${txid}:0`], other = [`${replacement}:1`];
    const view = await harness();
    view.state.activeCase = workflowCase({seeds: current, latest_run: 'recent', runs: [
      {id: 'recent', status: 'bounded_complete', max_hops: 10, seeds: latestMatches ? current : other},
      {id: 'historical', status: 'bounded_complete', max_hops: 3, seeds: latestMatches ? other : current},
    ]});
    view.state.page = 'case'; view.state.selectedRun = 'historical';
    const section = view.workspace().match(/<section class="panel" id="collection-panel"[\s\S]*?<\/section>/)[0];
    assert.match(section, latestMatches ? /Continue collecting data/ : /Collect current starting outputs/);
    await view.dispatch('trace-dialog');
    assert.match(view.dialog.innerHTML, latestMatches ? /Continue collecting transaction data/ : /Collect current starting outputs/);
    assert.match(view.dialog.innerHTML, latestMatches ? /<span>Hops for this run<\/span>/ : /<span>Maximum hops<\/span>/);
    assert.equal(view.calls.filter(call => call.body).length, 0);
  });
}

test('saved preview seed provenance stays visible and publication remains available after editing current seeds', async () => {
  const replacement = 'b'.repeat(64), current = [`${replacement}:1`];
  const preview = workflowPlot('full', 'old-selection', {seeds: [`${txid}:0`], layout_mode: 'fresh'});
  const view = await harness();
  view.state.activeCase = workflowCase({seeds: current, plots: [preview]});
  view.state.page = 'case'; await view.dispatch('view-plots');
  const html = view.workspace();
  assert.match(html, /Different starting outputs/);
  assert.match(html, /Publishing it to Miro uses that saved selection/);
  assert.match(html, /Preview starting outputs · 1 selected/);
  assert.match(html, new RegExp(`<li class="mono">${txid}:0<\/li>`));
  assert.doesNotMatch(html.match(/<button[^>]*data-action="workflow-write-miro"[^>]*>/)[0], /disabled/);
  assert.deepEqual(preview.seeds, [`${txid}:0`]);
});

test('shared peg-out total excludes mismatched and unknown seed selections while historical private totals remain available', async () => {
  const current = [`${txid}:0`], replacement = 'b'.repeat(64);
  const descriptor = {kind: 'shared', dataset_id: 'pool', run_id: 'shared-old'};
  const matching = {...valuePlot('matching', 'projection', '2026-10-01T01:00:00Z', valueSummary('7')), seeds: current, collection_source: descriptor};
  const mismatch = {...valuePlot('mismatch', 'projection', '2026-10-01T03:00:00Z', valueSummary('999')), seeds: [`${replacement}:1`], collection_source: descriptor};
  const unknown = {...valuePlot('unknown', 'projection', '2026-10-01T04:00:00Z', valueSummary('888')), collection_source: descriptor};
  const historical = {...valuePlot('private', 'saved1', '2026-10-01T02:00:00Z', valueSummary('9')), seeds: [`${replacement}:1`]};
  const view = await harness();
  const detail = workflowCase({shared_collection: sharedCollection(), plots: [matching, mismatch, unknown, historical]});
  view.state.activeCase = detail; view.state.page = 'case';
  workflowEdit(view, 'data-source', 'shared'); workflowEdit(view, 'shared-run', 'shared-old');
  assert.match(view.workspace(), /<strong>7 LBTC<\/strong>/);
  assert.doesNotMatch(view.workspace(), /<strong>(?:999|888|9) LBTC<\/strong>/);
  detail.plots = [mismatch, unknown, historical];
  assert.doesNotMatch(view.workspace(), /class="pegout-total"/);
  workflowEdit(view, 'data-source', 'investigation');
  assert.match(view.workspace(), /<strong>9 LBTC<\/strong>/);
});

test('saving seeds invalidates older in-flight case and session summaries so they cannot revert the new selection', async () => {
  const sessionResponse = Promise.withResolvers(), caseResponse = Promise.withResolvers();
  const replacement = 'b'.repeat(64), detail = workflowCase(); let sessions = 0;
  const view = await harness((path, body) => {
    if (path === '/api/session') return ++sessions === 1 ? {csrf: 'test', settings: defaults, cases: [detail]} : sessionResponse.promise;
    if (path === '/api/cases/case1/overview') return caseResponse.promise;
    if (path === '/api/cases/case1/seeds') return body ? {seeds: body.seeds, revision: 2} : {seeds: detail.seeds, revision: 1};
  });
  view.state.activeCase = detail; view.state.page = 'case';
  await view.dispatch('seed-editor-open'); view.editSeeds(`${replacement}:1`);
  const caseWork = view.refreshCaseDetail('case1'); const sessionWork = view.refreshSession(); await settled();
  await view.dispatch('seed-editor-save');
  caseResponse.resolve({...detail}); sessionResponse.resolve({csrf: 'test', settings: defaults, cases: [detail]});
  await Promise.all([caseWork, sessionWork]);
  assert.deepEqual([...view.state.activeCase.seeds], [`${replacement}:1`]);
  assert.deepEqual([...view.state.cases[0].seeds], [`${replacement}:1`]);
});

test('changing the new investigation network clears stale selected outputs and direct seeds', async () => {
  const view = await harness();
  view.navigate('new');
  view.state.draft.name = 'Bitcoin follow-up';
  view.state.draft.txids = txid;
  view.state.draft.seeds = `${txid}:2`;
  view.state.draft.reports = [{txid, outputs: [{vout: 0, selectable: true, value: 1000}]}];
  view.state.draft.selected.add(`${txid}:0`);
  view.changeBlockchain('bitcoin');
  assert.equal(view.state.draft.blockchain, 'bitcoin');
  assert.equal(view.state.draft.reports.length, 0);
  assert.equal(view.state.draft.selected.size, 0);
  assert.equal(view.state.draft.seeds, '');
  assert.equal(view.state.draft.txids, txid);
  assert.equal(view.state.draft.name, 'Bitcoin follow-up');
  assert.match(view.newCase(), /value="bitcoin" selected>Bitcoin Network/);
});

test('Bitcoin lookup is scoped and a late Liquid lookup cannot overwrite its output selection', async () => {
  const view = await harness(path => path === '/api/lookup' ? {id: 'lookup-btc', status: 'running', live: true}
    : path === '/api/jobs/lookup-btc' ? {id: 'lookup-btc', action: 'lookup', status: 'succeeded', live: true,
      result: {blockchain: 'bitcoin', transactions: [{txid, outputs: [{vout: 0, selectable: true, value: 123456789, value_text: '123456789'}]}]}} : undefined);
  view.navigate('new'); view.changeBlockchain('bitcoin'); view.state.draft.txids = txid;
  await view.dispatch('lookup');
  assert.equal(view.calls.find(call => call.path === '/api/lookup').body.blockchain, 'bitcoin');
  await view.pollJob('lookup-btc');
  assert.equal(view.state.draft.blockchain, 'bitcoin');
  assert.match(view.newCase(), /123456789/); assert.match(view.newCase(), />BTC<\/span>/);
  view.state.jobs.set('old-liquid', {id: 'old-liquid', action: 'lookup', live: true, status: 'succeeded',
    outcome: {result: {blockchain: 'liquid', transactions: [{txid, outputs: []}]}}});
  // Reviewing old output results explicitly restores their original network.
  await view.dispatch('load-job-outputs', {dataset: {id: 'old-liquid'}});
  assert.equal(view.state.draft.blockchain, 'liquid');
});

test('Bitcoin endpoint plots use BTC links, endpoint selections and no Liquid amount controls', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'btcplot', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({blockchain: 'bitcoin'});
  await view.dispatch('view-plots'); await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  const html = view.workspace();
  assert.match(html, /Live Bitcoin/);
  assert.match(html, new RegExp(`href="https://blockstream.info/tx/${txid}"`));
  assert.match(html, /Paths to endpoints/); assert.match(html, /attributed stops/i);
  assert.match(html, /a name alone does not stop tracing/);
  assert.doesNotMatch(html, /id="workflow-pegout-mode"|Cumulative L-BTC target|Peg-out LBTC total/);
  view.currentWorkflow(view.state.activeCase).pegoutLimitEnabled = true;
  view.currentWorkflow(view.state.activeCase).pegoutLbtcLimit = '60';
  await view.dispatch('workflow-plot');
  const body = view.calls.at(-1).body;
  assert.equal(body.goal, 'pegouts');
  assert.equal(body.include_unspent, true); assert.equal(body.include_unspendable, true);
  assert.equal(body.include_attributed_stops, true); assert.equal(body.pegout_lbtc_limit, undefined);
});

test('old saved endpoint queries preserve disabled attributed stops when preparing board updates', async () => {
  const old = workflowPlot('pegouts', 'oldplot', {query: {include_unspent: true}, layout_mode: 'update'});
  const view = await harness(path => path.endsWith('/actions') ? {id: 'update', status: 'running'} : undefined);
  view.state.activeCase = workflowCase({blockchain: 'bitcoin', plots: [old], boards: [workflowBoard('pegouts', 'old', {preview_id: 'oldplot'})]});
  await view.dispatch('workflow-board-prepare', boardControl('old'));
  assert.equal(view.currentWorkflow(view.state.activeCase).includeAttributedStops, false);
  await view.dispatch('workflow-plot');
  assert.equal(view.calls.at(-1).body.include_attributed_stops, undefined);
});

test('a completed lookup for another network does not automatically populate the current draft', async () => {
  const view = await harness(path => path === '/api/lookup' ? {id: 'lookup-liquid', status: 'running', live: true}
    : path === '/api/jobs/lookup-liquid' ? {id: 'lookup-liquid', action: 'lookup', status: 'succeeded', live: true,
      result: {blockchain: 'liquid', transactions: [{txid, outputs: [{vout: 0, selectable: true}]}]}} : undefined);
  view.navigate('new'); view.state.draft.txids = txid;
  await view.dispatch('lookup');
  // Simulate an intervening draft restoration without relying on a disabled select.
  view.state.draft.blockchain = 'bitcoin';
  await view.pollJob('lookup-liquid');
  assert.equal(view.state.draft.blockchain, 'bitcoin');
  assert.equal(view.state.draft.reports.length, 0);
  assert.match(view.notifications.at(-1), /Review outputs/);
});
