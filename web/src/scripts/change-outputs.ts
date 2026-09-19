/** Investigator-supplied layout annotations, independent of trace and ownership rules. */
type Output = {vout: number; selectable: boolean; address?: string; value?: number | string;
  value_text?: string; asset?: string; reason?: string; script_type?: string};
type Lookup = {txid: string; outputs: Output[]; current_vout: number | null; current_notes: string;
  revision: number; notice: string};
type Row = {txid: string; vout: number; notes: string; updated_at: string};
type Catalog = {revision: number; total: number; offset: number; limit: number; rows: Row[]; notice: string};
type Review = {valid: boolean; approval_sha256: string | null; unique_transactions: number;
  counts: Record<string, number>; duplicate_rows: number; errors: {row: number; message: string}[];
  changes: {row: number; txid: string; vout: number | null; notes: string; previous: number | null;
    previous_notes: string; action: string}[]; notice: string};
type Context = {caseId: string; busy: boolean; render: () => void;
  post: <T>(path: string, body: unknown) => Promise<T>;
  startLookup: (txid: string) => Promise<string | null>};
const MAX_BYTES = 512 * 1024;
const TEMPLATE = 'Txid,ChangeVout,Notes\n' + 'a'.repeat(64) + ',1,Investigator assessment\n';
const NOTICE = 'A designated change output aligns with its transaction, with the other outputs below. Transactions without a designation use the original ELK rules. This is your layout annotation; it does not establish ownership or change tracing.';
const REFRESH = 'Generate a new layout preview to review the result. Use Sync and reorganize to update an existing Miro board. Ordinary sync preserves existing positions.';
const esc = (value: unknown): string => String(value ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]!));
const off = (condition: boolean): string => condition ? ' disabled' : '';
const voutText = (vout: number | null): string => vout === null ? 'None (normal ELK)' : `vout ${vout}`;
function empty(caseId: string) {
  return {caseId, open: false, pending: false, message: '', query: '', offset: 0, catalog: null as Catalog | null,
    txid: '', selection: null as number | null, notes: '', lookup: null as Lookup | null,
    lookupJob: null as string | null, version: 0,
    importing: false, text: '', filename: '', format: 'auto', policy: 'keep', approved: false,
    review: null as Review | null, importOffset: 0, importVersion: 0};
}
let state = empty('');
export function resetChangeOutputs(caseId: string): void {state = empty(caseId);}
export function changeOutputsPending(): boolean {return state.pending;}

function invalidateImport(): void {
  state.importVersion += 1; state.review = null; state.approved = false; state.importOffset = 0;
  const apply = document.querySelector<HTMLButtonElement>('#change-outputs-import-apply');
  if (apply) apply.disabled = true;
  const approved = document.querySelector<HTMLInputElement>('#change-outputs-import-approved');
  if (approved) {approved.checked = false; approved.disabled = true;}
  const review = document.querySelector('#change-outputs-import-review');
  if (review) review.textContent = 'Preview again before applying.';
}
function invalidateLookup(): void {
  state.version += 1; state.lookup = null; state.lookupJob = null; state.selection = null; state.notes = '';
  const save = document.querySelector<HTMLButtonElement>('#change-outputs-save');
  if (save) save.disabled = true;
  const outputs = document.querySelector('#change-outputs-lookup');
  if (outputs) outputs.textContent = 'Load outputs for this transaction.';
}

export function changeOutputsInput(element: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement): boolean {
  if (!element?.id?.startsWith('change-outputs-') || element.id === 'change-outputs-file') return false;
  const id = element.id;
  if (id === 'change-outputs-txid') {state.txid = element.value; invalidateLookup();}
  else if (id === 'change-outputs-notes') state.notes = element.value;
  else if (id === 'change-outputs-query') state.query = element.value;
  else if (id.startsWith('change-outputs-vout-')) {
    if (!(element as HTMLInputElement).checked) return true;
    const value = element.value === '' ? null : Number(element.value);
    if (value !== null && !state.lookup?.outputs.some(row => row.selectable && row.vout === value)) return true;
    state.selection = value;
  } else if (id === 'change-outputs-import-approved') {
    state.approved = Boolean(state.review?.valid && (element as HTMLInputElement).checked);
    const apply = document.querySelector<HTMLButtonElement>('#change-outputs-import-apply');
    if (apply) apply.disabled = !state.approved || state.pending;
  } else if (id === 'change-outputs-text') {state.text = element.value; state.filename = ''; invalidateImport();}
  else if (id === 'change-outputs-format') {state.format = element.value; invalidateImport();}
  else if (id === 'change-outputs-replace') {
    state.policy = (element as HTMLInputElement).checked ? 'replace' : 'keep'; invalidateImport();
  } else return false;
  state.message = ''; return true;
}

export async function changeOutputsFile(input: HTMLInputElement, render: () => void, busy = false): Promise<void> {
  const file = input.files?.[0];
  if (!file || busy || state.pending) return;
  invalidateImport(); state.message = '';
  if (file.size > MAX_BYTES) {state.message = 'Change-output import exceeds 512 KiB.'; render(); return;}
  const owner = state, version = state.importVersion;
  owner.pending = true; render();
  try {
    const text = await file.text();
    if (owner !== state || version !== state.importVersion) return;
    if (new TextEncoder().encode(text).length > MAX_BYTES) throw new Error('Change-output import exceeds 512 KiB.');
    owner.text = text; owner.filename = file.name; owner.format = 'auto';
  } catch (error) {
    if (owner === state && version === state.importVersion) owner.message = error instanceof Error ? error.message : 'Could not read the selected file.';
  } finally {owner.pending = false; if (owner === state) render();}
}

/** Only the job started by the current editor may populate it. Recovered or old jobs stay read-only. */
export function changeOutputsLookupComplete(caseId: string, jobId: string, result?: unknown, error?: string): boolean {
  if (state.caseId !== caseId || state.lookupJob !== jobId) return false;
  state.lookupJob = null;
  if (error) {state.message = error; return true;}
  const report = result as Lookup;
  if (!report || report.txid !== state.txid.trim().toLowerCase() || !Array.isArray(report.outputs)) return false;
  state.lookup = report; state.selection = report.current_vout; state.notes = report.current_notes || '';
  state.message = 'Outputs loaded. Select one change output or None, then save.';
  return true;
}

function importPanel(locked: boolean): string {
  const review = state.review;
  if (!state.importing) return `<button class="btn" data-action="change-outputs-import-open"${off(locked)}>Import change outputs</button>`;
  return `<section aria-labelledby="change-outputs-import-title"><div class="panel-head"><h3 id="change-outputs-import-title" tabindex="-1">Import change outputs</h3><button class="btn" data-action="change-outputs-import-close"${off(locked)}>Close import</button></div>
  <p class="address-note">Upload or paste CSV or JSON with Txid, ChangeVout, and optional Notes. Each transaction can have one change output. Headers are case-insensitive. A blank ChangeVout clears an existing annotation only when replacement is enabled. Up to 512 KiB.</p>
  <label class="field"><span>Choose a change-output file</span><input type="file" id="change-outputs-file" accept=".csv,.json,text/csv,application/json"${off(locked)}/><small>${esc(state.filename || 'Or paste the file contents below.')}</small></label>
  <label class="field"><span>File contents</span><textarea id="change-outputs-text" rows="6"${off(locked)}>${esc(state.text)}</textarea></label>
  <label class="field"><span>Format</span><select id="change-outputs-format"${off(locked)}>${['auto', 'csv', 'json'].map(format => `<option value="${format}"${state.format === format ? ' selected' : ''}>${format.toUpperCase()}</option>`).join('')}</select></label>
  <label class="check-line"><input type="checkbox" id="change-outputs-replace"${state.policy === 'replace' ? ' checked' : ''}${off(locked)}/><span>Replace conflicting existing annotations and allow blank ChangeVout values to clear them. Otherwise, keep existing annotations.</span></label>
  <div class="form-actions"><button class="btn" data-action="change-outputs-import-template"${off(locked)}>Use CSV template</button><button class="btn primary" data-action="change-outputs-import-preview"${off(locked)}>Preview import</button></div>
  <div id="change-outputs-import-review" aria-live="polite">${review ? `<p><strong>${esc(review.unique_transactions)} unique transactions:</strong> ${esc(review.counts.add)} added, ${esc(review.counts.replace)} replaced, ${esc(review.counts.clear)} cleared, ${esc(review.counts.keep)} existing conflicts kept, ${esc(review.counts.unchanged)} unchanged. ${esc(review.duplicate_rows)} duplicate rows.</p><p class="address-note">${esc(review.notice)}</p>
  ${review.errors.length ? `<div class="alert error"><div><strong>Fix these errors before applying.</strong>${review.errors.map(error => `<p>Row ${esc(error.row)}: ${esc(error.message)}</p>`).join('')}</div></div>` : ''}
  <div class="table-wrap"><table><thead><tr><th>Row</th><th>Transaction</th><th>Existing change</th><th>Requested change</th><th>Notes</th><th>Action</th></tr></thead><tbody>${review.changes.slice(state.importOffset, state.importOffset + 100).map(row => `<tr><td>${esc(row.row)}</td><td class="mono">${esc(row.txid)}</td><td>${esc(voutText(row.previous))}</td><td>${esc(voutText(row.vout))}</td><td>${esc(row.notes)}${row.previous_notes && row.previous_notes !== row.notes ? `<small class="muted"><br>Previously: ${esc(row.previous_notes)}</small>` : ''}</td><td>${esc(row.action)}</td></tr>`).join('')}</tbody></table></div>
  <div class="form-actions"><span>Rows ${review.changes.length ? state.importOffset + 1 : 0}–${Math.min(state.importOffset + 100, review.changes.length)} of ${review.changes.length}</span><button class="btn" data-action="change-outputs-import-prev"${off(locked || state.importOffset === 0)}>Previous</button><button class="btn" data-action="change-outputs-import-next"${off(locked || state.importOffset + 100 >= review.changes.length)}>Next</button></div>` : ''}</div>
  <label class="check-line"><input type="checkbox" id="change-outputs-import-approved"${state.approved ? ' checked' : ''}${off(locked || !review?.valid)}/><span>I reviewed the change outputs, replacements, and cleared annotations.</span></label>
  <div class="form-actions"><button class="btn primary" id="change-outputs-import-apply" data-action="change-outputs-import-apply"${off(locked || !state.approved || !review?.valid)}>Apply reviewed change outputs</button></div></section>`;
}

export function changeOutputsPanel(caseId: string, busy: boolean): string {
  if (state.caseId !== caseId) resetChangeOutputs(caseId);
  if (!state.open) return '';
  const locked = busy || state.pending, lookup = state.lookup, catalog = state.catalog;
  return `<section class="panel" id="change-outputs-panel"><div class="panel-head"><h2 tabindex="-1" id="change-outputs-title">Change outputs</h2><button class="btn" data-action="change-outputs-close"${off(locked)}>Close</button></div><div class="panel-body">
  <p class="address-note">${esc(NOTICE)}</p><h3>Find a transaction</h3>
  <label class="field"><span>Transaction hash</span><input id="change-outputs-txid" class="mono" maxlength="64" spellcheck="false" value="${esc(state.txid)}" placeholder="64-character transaction hash"${off(locked)}/></label>
  <div class="form-actions"><button class="btn" data-action="change-outputs-lookup"${off(locked)}>Load outputs</button></div>
  <p class="address-note">Uses saved transaction data first. A missing transaction is looked up using this investigation’s data source. Live lookups may require a Proton Pass prompt in the launching terminal.</p>
  <div id="change-outputs-lookup">${lookup ? `<label class="check-line"><input type="radio" name="change-output-vout" id="change-outputs-vout-none" value=""${state.selection === null ? ' checked' : ''}${off(locked)}/><span>None. Use normal ELK rules for this transaction.</span></label>
  <div class="table-wrap"><table><thead><tr><th>Change</th><th>vout</th><th>Address / output type</th><th>Amount (base units)</th></tr></thead><tbody>${lookup.outputs.map(row => `<tr><td><input type="radio" name="change-output-vout" id="change-outputs-vout-${esc(row.vout)}" value="${esc(row.vout)}" aria-label="Use vout ${esc(row.vout)} as change"${state.selection === row.vout ? ' checked' : ''}${off(locked || !row.selectable)}/></td><td>${esc(row.vout)}</td><td class="mono">${esc(row.address || row.script_type || '??')}${!row.selectable ? `<small class="muted"><br>${esc(row.reason || 'Cannot designate this output as change.')}</small>` : ''}</td><td>${esc(row.value_text ?? (typeof row.value === 'number' && Number.isSafeInteger(row.value) ? row.value : '??'))}</td></tr>`).join('')}</tbody></table></div>
  <label class="field"><span>Notes (optional)</span><textarea id="change-outputs-notes" maxlength="4000" rows="3"${off(locked)}>${esc(state.notes)}</textarea></label><div class="form-actions"><button class="btn primary" id="change-outputs-save" data-action="change-outputs-save"${off(locked)}>Save change output</button></div>` : ''}</div>
  <div class="settings-divider"></div><h3>Saved change outputs</h3><label class="field"><span>Search saved annotations</span><input id="change-outputs-query" maxlength="256" value="${esc(state.query)}"${off(locked)}/></label><div class="form-actions"><button class="btn" data-action="change-outputs-load"${off(locked)}>Search / refresh</button></div>
  <div class="table-wrap"><table><thead><tr><th>Transaction</th><th>Change</th><th>Notes</th><th>Actions</th></tr></thead><tbody>${(catalog?.rows || []).map((row, index) => `<tr><td class="mono">${esc(row.txid)}</td><td>${esc(voutText(row.vout))}</td><td>${esc(row.notes)}</td><td><button class="btn" data-action="change-outputs-edit" data-change-index="${index}"${off(locked)}>Edit</button> <button class="btn" data-action="change-outputs-clear" data-change-index="${index}"${off(locked)}>Clear</button></td></tr>`).join('')}</tbody></table></div>
  ${catalog && !catalog.total ? '<p class="address-note">No matching change outputs saved.</p>' : ''}
  <div class="form-actions"><span>${esc(catalog?.total || 0)} saved annotations. Page ${Math.floor(state.offset / 100) + 1}.</span><button class="btn" data-action="change-outputs-prev"${off(locked || state.offset === 0)}>Previous</button><button class="btn" data-action="change-outputs-next"${off(locked || !catalog || state.offset + 100 >= catalog.total)}>Next</button></div>
  <div class="settings-divider"></div>${importPanel(locked)}<p class="address-note">${esc(REFRESH)}</p><p role="status">${esc(state.message)}</p></div></section>`;
}

async function loadCatalog(owner: typeof state, context: Context): Promise<void> {
  const catalog = await context.post<Catalog>(`/api/cases/${encodeURIComponent(context.caseId)}/change-outputs`,
    {query: owner.query, offset: owner.offset, limit: 100});
  if (owner === state) owner.catalog = catalog;
}
async function lookup(context: Context): Promise<void> {
  const txid = state.txid.trim().toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(txid)) {state.message = 'Enter a 64-character hexadecimal transaction hash.'; context.render(); return;}
  invalidateLookup(); state.txid = txid; state.message = '';
  const owner = state, version = state.version;
  try {
    const jobId = await context.startLookup(txid);
    if (owner === state && version === state.version) state.lookupJob = jobId;
  } catch (error) {
    if (owner === state && version === state.version) owner.message = error instanceof Error ? error.message : 'Could not load transaction outputs.';
  }
  if (owner === state) context.render();
}

async function importAction(action: string, context: Context): Promise<void> {
  if (action === 'change-outputs-import-open' || action === 'change-outputs-import-close') {
    state.importing = action.endsWith('-open'); context.render(); return;
  }
  if (action === 'change-outputs-import-template') {
    invalidateImport(); state.text = TEMPLATE; state.filename = ''; state.format = 'csv';
    state.message = 'Replace the example transaction hash and vout with your own assessment.'; context.render(); return;
  }
  if (action.endsWith('-prev') || action.endsWith('-next')) {
    const last = Math.max(0, (Math.ceil((state.review?.changes.length || 0) / 100) - 1) * 100);
    state.importOffset = Math.max(0, Math.min(last, state.importOffset + (action.endsWith('-next') ? 100 : -100)));
    context.render(); return;
  }
  if (!['change-outputs-import-preview', 'change-outputs-import-apply'].includes(action)) return;
  const applying = action.endsWith('-apply');
  if (applying && (!state.approved || !state.review?.valid || !state.review.approval_sha256)) {
    state.message = 'Preview and review the import before applying.'; context.render(); return;
  }
  if (new TextEncoder().encode(state.text).length > MAX_BYTES) {
    invalidateImport(); state.message = 'Change-output import exceeds 512 KiB.'; context.render(); return;
  }
  const payload = {text: state.text, format: state.format, policy: state.policy,
    ...(applying ? {approve_plan: state.review!.approval_sha256} : {})};
  if (!applying) invalidateImport();
  const owner = state, version = state.importVersion;
  owner.pending = true; owner.message = ''; context.render();
  try {
    const path = `/api/cases/${encodeURIComponent(context.caseId)}/change-output-import`;
    if (!applying) {
      const review = await context.post<Review>(path, payload);
      if (owner === state && version === state.importVersion) owner.review = review;
    } else {
      const result = await context.post<{changed: number}>(path, payload);
      if (owner === state) {
        invalidateImport(); invalidateLookup();
        owner.message = `Saved ${result.changed} change-output annotation(s). ${REFRESH}`;
        try {await loadCatalog(owner, context);}
        catch (error) {if (owner === state) owner.message += ` The saved list could not refresh: ${error instanceof Error ? error.message : 'try again.'}`;}
      }
    }
  } catch (error) {
    if (owner === state && (applying || version === state.importVersion)) {
      invalidateImport(); owner.message = error instanceof Error ? error.message : 'Could not import change outputs. Preview again and retry.';
    }
  } finally {owner.pending = false; if (owner === state) context.render();}
}

export async function changeOutputsAction(action: string, context: Context, element?: HTMLElement): Promise<boolean> {
  if (!action.startsWith('change-outputs-')) return false;
  if (context.busy || state.pending) return true;
  if (state.caseId !== context.caseId) resetChangeOutputs(context.caseId);
  if (action.startsWith('change-outputs-import-')) {await importAction(action, context); return true;}
  if (action === 'change-outputs-close') {state.open = false; context.render(); return true;}
  if (action === 'change-outputs-lookup' || action === 'change-outputs-edit') {
    if (action.endsWith('-edit')) {
      const index = Number(element?.dataset.changeIndex), row = Number.isInteger(index) ? state.catalog?.rows[index] : undefined;
      if (!row) return true;
      state.txid = row.txid;
    }
    await lookup(context); return true;
  }
  const owner = state;
  owner.open = true; owner.pending = true; owner.message = ''; context.render();
  try {
    if (action === 'change-outputs-save' || action === 'change-outputs-clear') {
      const clearing = action.endsWith('-clear');
      const index = Number(element?.dataset.changeIndex), row = Number.isInteger(index) ? owner.catalog?.rows[index] : undefined;
      const txid = clearing ? row?.txid : owner.lookup?.txid;
      const revision = clearing ? owner.catalog?.revision : owner.lookup?.revision;
      if (!txid || revision === undefined) throw new Error('Refresh the saved list or load transaction outputs before saving.');
      const vout = clearing ? null : owner.selection;
      if (!clearing && vout !== null && !owner.lookup?.outputs.some(output => output.selectable && output.vout === vout)) throw new Error('Select a spendable output or None.');
      invalidateImport();
      const result = await context.post<{changed: number; revision: number}>(`/api/cases/${encodeURIComponent(context.caseId)}/change-outputs`,
        {txid, vout, notes: clearing ? '' : owner.notes, expected_revision: revision});
      if (owner !== state) return true;
      if (owner.lookup) {
        owner.lookup.revision = result.revision;
        if (owner.lookup.txid === txid) {owner.lookup.current_vout = vout; owner.selection = vout; if (clearing) owner.notes = '';}
      }
      owner.message = `Saved ${result.changed} change-output annotation(s). ${REFRESH}`;
    } else if (action === 'change-outputs-prev') owner.offset = Math.max(0, owner.offset - 100);
    else if (action === 'change-outputs-next') owner.offset += 100;
    else owner.offset = 0;
    try {await loadCatalog(owner, context);}
    catch (error) {
      if (owner === state && owner.message.startsWith('Saved ')) owner.message += ` The saved list could not refresh: ${error instanceof Error ? error.message : 'try again.'}`;
      else throw error;
    }
  } catch (error) {
    if (owner === state) {
      if (action === 'change-outputs-save' || action === 'change-outputs-clear') {invalidateLookup(); invalidateImport();}
      owner.message = error instanceof Error ? error.message : 'Could not load or save change outputs.';
    }
  } finally {
    owner.pending = false;
    if (owner === state) {context.render(); if (action === 'change-outputs-open') document.querySelector<HTMLElement>('#change-outputs-title')?.focus();}
  }
  return true;
}
