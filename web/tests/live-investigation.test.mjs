import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {readFileSync} from 'node:fs';
import {stripTypeScriptTypes} from 'node:module';
import {test} from 'node:test';
import vm from 'node:vm';
import * as frameRecovery from '../src/scripts/frame-recovery.ts';
import {collectionPerformancePanel} from '../src/scripts/collection-performance.ts';
import {createTaskNotifications} from '../src/scripts/task-notifications.ts';

// Execute the real application handlers with a small DOM and offline HTTP stub.
// The attribution panels are unrelated to new-investigation and lookup behavior.
const source = stripTypeScriptTypes(
  readFileSync(new URL('../src/scripts/app.ts', import.meta.url), 'utf8')
    .replace(/^import .*\n/gm, '')
    .replace(/^export \{\};\n/m, '')
    .replace('void initialize().catch(', 'globalThis.startup = initialize().catch('),
  {mode: 'transform'},
);
const script = new vm.Script(source + '\n Object.defineProperty(state, "job", {get() {return runningJobs()[0] || null;}, set(job) {state.jobs.clear(); if (job && state.activeCase && state.page === "dashboard") state.page = "case"; if (job) state.jobs.set(job.id, {status: "running", generation: pageGeneration, caseId: state.activeCase?.id, ...job});}}); globalThis.appTest = {state, dispatch, pollJob: (id) => pollJob(id || runningJobs()[0]?.id), startJob, discoverJobs, runningJobs, jobBanner, jobProgress, cancelJob, openCase, newCase, dashboard, workspace, settingsPage, localGraph, elkGraph, compactGraph, currentCompaction, openActionDialog, readSettings, budgetFields, isBusy, pegoutsGraph, pegoutInput, currentPegoutSearch, suggestCenterNames, suggestHopReferenceNames, render, navigate, workflowInput, currentWorkflow, actionBusy, taskActivity, refreshCaseDetail, refreshSharedCollection};');
const txid = 'a'.repeat(64);
const defaults = {hops: 1, hop_reference_name: '', max_transactions: 20, max_outpoints: 100, max_requests: 30,
  max_seconds: 60, max_new_items: 750, layout_attempts: 25, connector_style: 'straight'};

async function harness(respond = () => undefined, {hash = '', storage = new Map(), importAction, importPending = () => false, systemNotification} = {}) {
  frameRecovery.resetFrameRecovery();
  const calls = [], listeners = {}, windowListeners = {}, dialogListeners = {}, notifications = [], importActions = [];
  const editorResets = [], downloads = [], downloadBlobs = [], revokedDownloads = [];
  const toastElements = [];
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
    nameColorsPanel() {return "";}, nameColorsAction() {return false;}, addressImportAction() {return false;},
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
      sessionStorage: {getItem(key) {return storage.get(key) ?? null;}, setItem(key, value) {storage.set(key, value);}}},
    history: {replaceState() {}},
    location: {hash, pathname: '/', reload() {}},
    setInterval() {}, setTimeout() {return 1;}, clearTimeout() {},
    FormData: class {
      constructor(form) {this.values = form.values;}
      get(key) {return this.values[key] ?? null;}
      has(key) {return key in this.values;}
    },
    async fetch(path, options) {
      const body = options.body === undefined ? undefined : JSON.parse(options.body);
      calls.push({path, body});
      const response = await Promise.resolve(respond(path, body, calls.length))
        ?? (path === '/api/session' ? {csrf: 'test', settings: defaults, cases: []} : {});
      return {ok: !response.error, status: response.error ? 400 : 200, async json() {return response;}};
    },
  });
  script.runInContext(context);
  await context.startup;
  return {
    ...context.appTest, app, dialog, calls, notifications, importActions, elements, storage, windowListeners, editorResets, downloads, downloadBlobs, revokedDownloads,
    toastElements, get focusCount() {return focusCount;},
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
  assert.match(view.newCase(), /<select name="blockchain" required><option value="liquid" selected>Liquid Network<\/option><\/select>/);
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
  const view = await harness(path => path === '/api/cases/savedcase' ? request.promise : undefined);
  view.state.cases = [detail];
  view.state.job = {id: 'active', action: 'trace', message: 'Collecting transactions', live: false};
  const opening = view.dispatch('open-case', {dataset: {id: detail.id}});
  await new Promise(setImmediate);
  assert.equal(view.state.page, 'dashboard');
  const header = view.app.innerHTML.match(/<header class="workspace-header">[\s\S]*?<\/header>/)[0];
  assert.match(header, /id="case-opening"[\s\S]*role="status" aria-live="polite"/);
  assert.match(header, /Opening Saved &lt;investigation&gt;/);
  assert.match(header, /Loading saved runs, plots, and board records\./);
  assert.match(header, /Collecting transactions/);
  assert.match(view.app.innerHTML, /data-case="savedcase" disabled/);
  assert.match(view.app.innerHTML, /data-action="open-case" disabled/);
  assert.doesNotMatch(view.app.innerHTML.match(/<button[^>]*data-page="new"[^>]*>/)[0], /disabled/);
  await view.dispatch('open-case', {dataset: {id: detail.id}});
  assert.equal(view.calls.filter(call => call.path === '/api/cases/savedcase').length, 1);
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
    const view = await harness(path => path === '/api/cases/slowcase' ? request.promise : undefined);
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
    const view = await harness(path => path === '/api/cases/first' ? first.promise
      : path === '/api/cases/second' ? second : undefined);
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
  const view = await harness(path => path === '/api/cases/savedcase'
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
    : path === '/api/cases/savedcase' ? {error: 'Investigation not found'} : undefined,
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
  const view = await harness(path => path === '/api/cases/savedcase' ? request.promise : undefined);
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
  const view = await harness(path => path === '/api/cases/othercase' ? request.promise : undefined);
  view.state.activeCase = {id: 'current', name: 'Current case', run_defaults: defaults, runs: []};
  view.navigate('case-settings');
  view.elements.set('#settings-form', {values: {...defaults, name: 'Unsaved case name', max_transactions: 37}});
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
    const view = await harness(path => path === '/api/cases/othercase' ? request.promise : undefined);
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
  assert.match(view.dialog.innerHTML, /First run or changed named group: maximum hops/);
  assert.match(view.dialog.innerHTML, /Same hop basis: additional hops/);
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
const layoutDefaults = {layout_attempts: 25, connector_style: 'straight', include_fees: false, color_attribution_arrows: false, group_context_inputs: false, center_name: '', hub_addresses: []};
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
  assert.deepEqual(writes[0].body, {action: 'plot', layout_mode: 'fresh', goal: 'full', run_id: 'saved1', min_hops: 0, max_hops: 0, layout_settings: {layout_attempts: 40, connector_style: 'curved', include_fees: true,
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
  const disabledFields = view.workspace().match(/<fieldset[^>]*\bdisabled[^>]*>[\s\S]*?<\/fieldset>/g) || [];
  for (const key of ['include_fees', 'hub_addresses']) {
    assert.ok(disabledFields.some(fieldset => fieldset.includes(`name="${key}"`)), key);
  }
  assert.doesNotMatch(contextGroupingFields(view), /disabled/);
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
      action: 'plot', layout_settings: layoutDefaults, layout_mode: 'fresh', goal, run_id: 'older', min_hops: goal === 'pegouts' ? 2 : 0, max_hops: goal === 'pegouts' ? 10 : 0}});
    assert.equal(view.state.job.live, false);
    assert.equal(view.calls.filter(call => call.path.endsWith('/actions')).length, 1);
  }
});

test('Starter connections hides hop controls and ignores a stale invalid range when generating', async () => {
  const view = await harness(path => path.endsWith('/actions') ? {id: 'plotjob', status: 'running'} : undefined);
  view.state.activeCase = workflowCase();
  await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  assert.match(view.workspace(), /id="workflow-max-hops"/);
  workflowEdit(view, 'min-hops', 'bad'); workflowEdit(view, 'max-hops', '-1');
  await view.dispatch('plot-goal', {dataset: {goal: 'connections'}});
  assert.doesNotMatch(view.workspace(), /id="workflow-(?:min|max)-hops"/);
  assert.match(view.workspace(), /Attribution hop limits, stop-tracing rules, and plotting hop cutoffs are ignored; labels remain visible/);
  assert.match(view.workspace(), /No additional transactions are fetched/);
  await view.dispatch('workflow-plot');
  assert.equal(view.calls.at(-1).body.goal, 'connections');
  assert.equal(view.calls.at(-1).body.min_hops, 0);
  assert.equal(view.calls.at(-1).body.max_hops, 0);
});

test('saved Starter layouts label all-saved scope and retain the range of older layouts', async () => {
  for (const allSaved of [false, true]) {
    const plot = workflowPlot('connections', 'connections1', allSaved
      ? {max_hops: null, query: {connection_scope: 'all_saved'}} : {max_hops: 3});
    const view = await harness();
    view.state.activeCase = workflowCase({plots: [plot], boards: [workflowBoard('connections', 'board1', {preview_id: plot.preview_id})]});
    for (const page of ['view-plots', 'view-history', 'view-boards']) {
      await view.dispatch(page);
      const html = view.workspace();
      assert.match(html, allSaved ? /All saved connections/ : /Hops 0–3/);
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
      assert.deepEqual(view.calls.at(-1).body, {action: 'plot', layout_settings: layoutDefaults, layout_mode: 'fresh', goal, run_id: 'saved1',
        min_hops: 0, max_hops: goal === 'pegouts' ? 10 : 0,
        ...(goal === 'pegouts' && includeUnspent ? {include_unspent: true} : {}),
        ...(goal === 'pegouts' && includeUnspendable ? {include_unspendable: true} : {})});
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
  for (const id of ['unspent', 'unspendable']) assert.doesNotMatch(view.workspace(), new RegExp(`id="workflow-include-${id}"[^>]*checked`));
  assert.equal(view.calls.length, 1);
});

test('peg-out layouts always describe complete I/O without an optional context toggle', async () => {
  for (const goal of ['full', 'connections', 'pegouts']) {
    const view = await harness(path => path.endsWith('/actions') ? {id: 'plotjob', status: 'running'} : undefined);
    view.state.activeCase = workflowCase();
    await view.dispatch('view-plots');
    await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
    const html = view.workspace();
    assert.match(html, /Every included transaction shows all its inputs and outputs, including fees/);
    assert.match(html, /Outputs on excluded branches remain visible without continuing those branches/);
    assert.match(html, /These extra objects do not add endpoint matches or change the peg-out total/);
    assert.doesNotMatch(html, /id="workflow-include-context"|Include context addresses/);
    assert.equal(view.workflowInput({id: 'workflow-include-context', checked: false}), false);
    view.workflowInput({id: 'workflow-include-unspent', checked: true});
    await view.dispatch('plot-goal', {dataset: {goal}});
    await view.dispatch('workflow-plot');
    assert.deepEqual(view.calls.at(-1).body, {action: 'plot', layout_settings: layoutDefaults, layout_mode: 'fresh', goal, run_id: 'saved1',
      min_hops: 0, max_hops: goal === 'pegouts' ? 10 : 0,
      ...(goal === 'pegouts' ? {include_unspent: true} : {})});
    assert.equal(view.state.job.live, false);
    assert.equal(view.calls.filter(call => call.body).length, 1, 'display choices neither update settings nor collect data');
  }
});

const contextGroupingFields = view => view.workspace().match(/<fieldset class="layout-fields context-grouping-fields"[^>]*>[\s\S]*?<\/fieldset>/)?.[0];

test('peg-out context grouping is always available and preserves edits across goals', async () => {
  const view = await harness();
  view.state.activeCase = workflowCase();
  await view.dispatch('view-plots');
  assert.doesNotMatch(contextGroupingFields(view), /disabled/);
  view.plotForm({group_context_inputs_present: '1', group_context_inputs: 'on'});
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  view.elements.delete('#plot-layout-form');
  assert.doesNotMatch(contextGroupingFields(view), /disabled/);
  assert.match(contextGroupingFields(view), /name="group_context_inputs"[^>]*checked/);
  assert.match(contextGroupingFields(view), /Shared or named addresses and context outputs stay separate/);
  view.plotForm({layout_attempts: '47', connector_style: 'curved', center_name: 'Draft treasury',
    group_context_inputs_present: '1'});
  await view.dispatch('plot-goal', {dataset: {goal: 'connections'}});
  view.elements.delete('#plot-layout-form');
  assert.match(contextGroupingFields(view), /disabled/);
  assert.doesNotMatch(contextGroupingFields(view), /name="group_context_inputs"[^>]*checked/);
  assert.match(view.workspace(), /name="layout_attempts"[^>]*value="47"/);
  assert.match(view.workspace(), /value="Draft treasury"/);
  assert.match(view.workspace(), /value="curved" selected/);
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  assert.doesNotMatch(contextGroupingFields(view), /disabled|name="group_context_inputs"[^>]*checked/);
  view.plotForm({group_context_inputs_present: '1', group_context_inputs: 'on'});
  await view.dispatch('plot-goal', {dataset: {goal: 'connections'}});
  view.elements.delete('#plot-layout-form');
  assert.match(contextGroupingFields(view), /disabled/);
  assert.match(contextGroupingFields(view), /name="group_context_inputs"[^>]*checked/);
  for (const goal of ['full', 'pegouts']) {
    await view.dispatch('plot-goal', {dataset: {goal}});
    assert.doesNotMatch(contextGroupingFields(view), /disabled/);
    assert.match(contextGroupingFields(view), /name="group_context_inputs"[^>]*checked/);
    assert.match(view.workspace(), /value="Draft treasury"/);
  }
  assert.equal(view.calls.length, 1, 'draft changes neither save nor queue work');
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
      max_hops: 10, layout_settings: {...layoutDefaults, group_context_inputs: grouped}});
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
  assert.match(view.workspace(), /The collection hop ceiling, resource budgets and explicit tracing stops apply/);
  assert.match(view.workspace(), /Attribution CSV hop_limit values are ignored, including zero/);
  assert.match(view.workspace(), /0 additional hops fills eligible gaps within its existing ceiling/);
  await view.dispatch('view-plots');
  await view.dispatch('plot-goal', {dataset: {goal: 'pegouts'}});
  assert.match(view.workspace(), /Peg-out paths ignore attribution CSV hop_limit values/);
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
    if (page === 'plots') assert.match(html.match(/<button[^>]*data-action="plot-boards"[^>]*>/)[0], /disabled/);
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
      : path === '/api/cases/case1' ? detail : undefined);
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
  const view = await harness(path => path === `/api/cases/${detail.id}` ? detail
    : path.endsWith('/actions') ? {id: 'syncjob', status: 'running'} : undefined);
  await view.dispatch('open-case', {dataset: {id: detail.id}});
  await view.dispatch('view-plots');
  assert.match(view.workspace(), /<iframe/);
  assert.match(view.workspace(), /Paths to peg-outs/);
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
    preview_id: detail.plots[0].preview_id, name: detail.name + ' · Paths to peg-outs'});
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
      const view = await harness(path => path === '/api/jobs/job' ? {status, message: 'Original operation message'} : path === '/api/cases/case1' ? updated : undefined);
      view.state.activeCase = workflowCase(); view.state.caseView = 'boards';
      view.state.job = {id: 'job', action, caseId: 'case1'};
      await view.pollJob();
      assert.ok(boardCardHtml(view, 'persisted'));
      if (status === 'failed') assert.equal(view.state.error, 'Original operation message');
    }
  }
  const failedRefresh = await harness(path => path === '/api/jobs/job' ? {status: 'failed', message: 'Original sync failure'} : path === '/api/cases/case1' ? {error: 'Refresh failed'} : undefined);
  failedRefresh.state.activeCase = workflowCase(); failedRefresh.state.job = {id: 'job', action: 'board-sync', caseId: 'case1'};
  await failedRefresh.pollJob();
  assert.equal(failedRefresh.state.error, 'Original sync failure');
  const switched = await harness(path => path === '/api/jobs/job' ? {status: 'failed', message: 'Previous case failure'} : undefined);
  switched.state.activeCase = workflowCase({id: 'another-case'}); switched.state.job = {id: 'job', action: 'board-create', caseId: 'case1'};
  await switched.pollJob();
  assert.equal(switched.state.activeCase.id, 'another-case');
  assert.equal(switched.calls.filter(call => call.path === '/api/cases/case1').length, 0);
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
    : path === '/api/cases/case1' ? workflowCase() : undefined);
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
  assert.ok(view.calls.some(call => call.path === '/api/cases/case1'));
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
    : path === '/api/cases/case2' ? workflowCase({id: 'case2'})
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
      : path === '/api/cases/case1' ? workflowCase() : undefined);
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
    : path === '/api/cases/case1' ? {...original, boards: [...original.boards, created]} : undefined);
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
  assert.deepEqual(view.calls.at(-1).body, {action: 'plot', goal: 'pegouts', run_id: 'saved1', min_hops: 0, max_hops: 10,
    layout_mode: 'update', board_record_id: 'cashouts', layout_settings: layoutDefaults});
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
    : path === '/api/cases/case1' ? detail : undefined);
  view.state.activeCase = workflowCase(); view.state.job = {id: 'job', action: 'plot', caseId: 'case1', live: true};
  await view.pollJob(); await view.dispatch('view-boards');
  assert.match(boardCardHtml(view, 'cashouts'), /value="update" selected/);
});

test('Plots and Miro share one workspace with unique controls and one primary generate action', async () => {
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
  assert.match(html, /class="btn primary" data-action="workflow-plot-sync"[^>]*>[\s\S]*?Generate & create board/);
  assert.match(html, /<details class="tool-details"><summary>Preview without syncing<\/summary>/);
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
    max_hops: 0, layout_mode: 'fresh', name: 'Investigator overview', layout_settings: {...layoutDefaults, connector_style: 'curved'}});
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
    min_hops: 2, max_hops: 11, layout_mode: 'update', board_record_id: 'cashouts',
    include_unspent: true, include_unspendable: true, layout_settings: layoutDefaults});
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
    : path === '/api/cases/case1' ? detail : undefined);
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
    : path === '/api/cases/case1' ? detail : undefined);
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
    : path === '/api/cases/case1' ? detail : path.endsWith('/actions') ? {id: 'resume', status: 'running'} : undefined);
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
    : path.startsWith('/api/cases/') ? multiCase(path.split('/').at(-1)) : undefined);
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
    if (path === '/api/cases/alpha') return {...detail, plots: []};
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
    if (path === '/api/cases/beta') return multiCase('beta', {latest_run: 'new-run'});
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
      : path === '/api/cases/alpha' ? multiCase('alpha')
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
    if (path === '/api/cases/alpha') return multiCase('alpha');
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
    if (path === '/api/cases/case1') return detail;
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
    if (path === '/api/cases/case1') return {...detail, latest_run: 'newer', runs: [{id: 'newer'}, ...detail.runs]};
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
    if (path === '/api/cases/case1') return after;
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
      if (path === '/api/cases/case1') return after;
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
    if (path === '/api/cases/case1') return new Promise(resolve => refreshes.push(resolve));
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
  : tabCases.find(item => path === `/api/cases/${item.id}`));

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
  const view = await harness(path => path === '/api/cases/a1' ? slow : tabResponse(path));
  const opening = view.openCase('a1');
  await view.openCase('b2');
  release(tabCases[0]);
  await opening;
  assert.equal(view.state.activeCase.id, 'b2');
  assert.deepEqual([...view.state.openCases], ['b2']);
});

test('returning to a tab pins its displayed snapshot when another collection finishes', async () => {
  let completed = false;
  const view = await harness(path => path === '/api/cases/a1' && completed
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
  assert.match(view.app.innerHTML, /Generate a Paths to peg-outs plot/);
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
      if (path === '/api/cases/background') return details;
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
  const view = await harness(path => path.endsWith('/actions') ? {id: 'shared-plot', status: 'running'} : path === '/api/cases/case1' ? detail : undefined);
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
  const view = await harness(path => path === '/api/cases/case1' ? alpha : path === '/api/cases/case2' ? beta : undefined);
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
    : path === '/api/cases/other' ? workflowCase({id: 'other', shared_collection: after})
    : path === '/api/cases/case1' ? {...detail, shared_collection: after} : undefined);
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
    {...valuePlot('shared', 'projection', '2026-10-01T02:00:00Z', valueSummary('7')), collection_source: descriptor},
    {...valuePlot('other-shared', 'another-projection', '2026-10-01T04:00:00Z', valueSummary('88')), collection_source: {...descriptor, run_id: 'shared-new'}},
  ]});
  const beta = workflowCase({id: 'case2', name: 'Beta', shared_collection: sharedCollection()});
  const view = await harness(path => path === '/api/cases/case1' ? alpha : path === '/api/cases/case2' ? beta
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
    const view = await harness(path => path === '/api/cases/case1' ? (++reads === 1 ? older : newer) : undefined);
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
