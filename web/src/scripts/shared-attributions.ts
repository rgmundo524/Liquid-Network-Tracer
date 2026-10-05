/** Workspace attribution names and evidence, explicitly enabled per investigation. */
type Rule = {address: string; name: string; confidence: string; source: string; notes?: string;
  observed_at?: string; enabled: boolean};
type Catalog = {library_id: string; revision: number; total: number; offset: number; limit: number; rows: Rule[]};
type Sharing = {enabled: boolean; revision: number; shared_revision: number | null; shared_count: number | null; local_overrides: number | null; shared_unavailable?: boolean};
type Review = {valid: boolean; approval_sha256: string | null; counts: Record<string, number>;
  changes: {row: number; action: string; rule: Rule; previous: Rule | null}[];
  errors?: {row?: number; message: string}[]; notice?: string; duplicate_rows?: number;
  ignored_controls?: {stop_tracing?: number; hop_limit?: number}};
type Context = {caseId: string | null; blockchain?: string; busy: boolean; render: () => void;
  post: <T>(path: string, body: unknown) => Promise<T>; get: <T>(path: string) => Promise<T>;
  readText: (path: string) => Promise<{text: string; contentType: string}>;
  refresh?: (scope: 'library' | 'case') => Promise<void>};
const MAX_BYTES = 512 * 1024, PAGE_SIZE = 50;
const TEMPLATE = 'Address,Name,confidence,source,notes,observed_at,enabled\nREPLACE_WITH_ADDRESS,Example Exchange,suspected,Investigator research,Explain the evidence,,true\n';
const BASE = '/api/shared-attributions';
const esc = (value: unknown): string => String(value ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]!));
const off = (value: boolean): string => value ? ' disabled' : '';
function empty(caseId: string | null, blockchain = "liquid") {
  return {caseId, blockchain, open: false, pending: false, query: '', offset: 0, catalog: null as Catalog | null,
    catalogVersion: 0, sharing: null as Sharing | null, enabled: false,
    sharingReview: null as (Sharing & {requested: boolean}) | null, sharingVersion: 0,
    text: '', filename: '', format: 'auto', policy: 'keep', review: null as Review | null,
    reviewOffset: 0, version: 0, message: ''};
}
let state = empty(null);
const drafts = new Map<string, typeof state>();
let workspaceBlockchain = 'liquid';
const draftKey = (caseId: string | null, blockchain: string): string => `${caseId || 'workspace'}:${blockchain}`;
export function selectSharedAttributions(caseId: string | null, blockchain?: string): void {
  const chain = blockchain === 'bitcoin' ? 'bitcoin' : blockchain === 'liquid' ? 'liquid' : caseId ? 'liquid' : workspaceBlockchain;
  const key = draftKey(caseId, chain);
  if (!drafts.has(key)) drafts.set(key, empty(caseId, chain));
  state = drafts.get(key)!;
}
export function resetSharedAttributions(caseId: string | null): void {
  for (const [key, draft] of drafts) if (draft.caseId === caseId) drafts.delete(key);
  if (!caseId) workspaceBlockchain = 'liquid';
  selectSharedAttributions(caseId);
}
export function sharedAttributionsPending(caseId: string | null = state.caseId): boolean {
  return [...drafts.values()].some(draft => draft.caseId === caseId && draft.pending);
}
const retained = (owner: typeof state): boolean => drafts.get(draftKey(owner.caseId, owner.blockchain)) === owner;
function invalidate(owner = state): void {
  owner.version++; owner.review = null; owner.reviewOffset = 0; owner.message = '';
  if (owner !== state) return;
  const apply = document.querySelector<HTMLButtonElement>('#shared-attributions-apply');
  if (apply) apply.disabled = true;
  const preview = document.querySelector<HTMLButtonElement>('#shared-attributions-preview');
  if (preview) preview.disabled = owner.pending || !owner.text.trim();
  const review = document.querySelector('#shared-attributions-review');
  if (review) review.textContent = 'Contents or options changed. Review the import again.';
}
function invalidateSharing(owner = state): void {
  owner.sharingVersion++; owner.sharingReview = null;
  if (owner !== state) return;
  const apply = document.querySelector<HTMLButtonElement>('#shared-attributions-use-apply');
  if (apply) apply.disabled = true;
  const review = document.querySelector('#shared-attributions-use-review');
  if (review) review.textContent = '';
}
export function sharedAttributionsInput(element: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement): boolean {
  if (!element?.id?.startsWith('shared-attributions-') || element.id === 'shared-attributions-file') return false;
  if (element.id === 'shared-attributions-blockchain') {
    if (state.caseId || state.pending || !['liquid', 'bitcoin'].includes(element.value)) return true;
    workspaceBlockchain = element.value; selectSharedAttributions(null); state.open = true; return true;
  }
  if (element.id === 'shared-attributions-query') {
    state.query = element.value; state.offset = 0; state.catalogVersion++; return true;
  }
  if (element.id === 'shared-attributions-enabled') {
    state.enabled = (element as HTMLInputElement).checked; invalidateSharing(); return true;
  }
  if (element.id === 'shared-attributions-text') {state.text = element.value; state.filename = '';}
  else if (element.id === 'shared-attributions-format' && ['auto', 'csv', 'json'].includes(element.value)) state.format = element.value;
  else if (element.id === 'shared-attributions-policy' && ['keep', 'replace'].includes(element.value)) state.policy = element.value;
  else return false;
  invalidate(); return true;
}
function checkText(text: string, filename = '', contentType = ''): void {
  if (/\.zip$/i.test(filename) || /\bzip\b/i.test(contentType) || /^PK[\u0003\u0005\u0007]/.test(text))
    throw new Error('This export is a ZIP of CSV parts. Extract it, then import each attribution CSV part separately.');
  if (new TextEncoder().encode(text).length > MAX_BYTES)
    throw new Error('Shared attribution import exceeds 512 KiB. Split it into smaller CSV files.');
}
export async function sharedAttributionsFile(input: HTMLInputElement, render: () => void, busy = false): Promise<void> {
  const file = input.files?.[0];
  if (!file || busy || state.pending) return;
  invalidate();
  const owner = state, version = owner.version;
  owner.pending = true; render();
  try {
    checkText('', file.name, file.type);
    if (file.size > MAX_BYTES) throw new Error('Shared attribution import exceeds 512 KiB. Split it into smaller CSV files.');
    let text: string;
    try {text = new TextDecoder('utf-8', {fatal: true}).decode(await file.arrayBuffer());}
    catch {throw new Error('Choose a UTF-8 CSV or JSON file.');}
    checkText(text, file.name, file.type);
    if (retained(owner) && owner.version === version) {owner.text = text; owner.filename = file.name; owner.format = 'auto';}
  } catch (error) {
    if (retained(owner) && owner.version === version) owner.message = error instanceof Error ? error.message : 'Could not read the selected file.';
  } finally {owner.pending = false; if (owner === state) render();}
}
function counts(value: Record<string, number>): string {
  return `${value.add || 0} new, ${value.replace || 0} replacements, ${value.keep || 0} existing conflicts kept, ${value.unchanged || 0} unchanged`;
}
function fields(rule: Rule | null): string {
  if (!rule) return 'No saved shared entry';
  return `<strong>${esc(rule.name || 'Unnamed')}</strong> · ${esc(rule.confidence)} · ${rule.enabled ? 'Enabled' : 'Disabled'}
    <br>Source: ${esc(rule.source)}<br>Date: ${esc(rule.observed_at || 'Not recorded')}<br>Notes: ${esc(rule.notes)}`;
}
function sharingPanel(locked: boolean): string {
  if (!state.caseId) return '';
  const status = state.sharing, reviewed = state.sharingReview;
  return `<section aria-labelledby="shared-attributions-use-title"><h3 id="shared-attributions-use-title">Use in this investigation</h3>
    <p>Local assessments override shared entries for the same address, including disabled local entries. Stop-tracing rules, display hop limits, and name and role colors stay local to each investigation.</p>
    ${status ? `<p>Currently ${status.enabled ? 'enabled' : 'disabled'} · ${status.shared_unavailable ? 'Shared library unavailable. You can still disable sharing.' : `${esc(status.shared_count)} shared entries · ${esc(status.local_overrides)} local overrides.`}</p>` : '<p role="status">Sharing status is unavailable. Refresh the library to load it.</p>'}
    <label class="check-line"><input type="checkbox" id="shared-attributions-enabled"${state.enabled ? ' checked' : ''}${off(locked || !status)}/><span>Use shared attributions</span></label>
    <div class="form-actions"><button type="button" class="btn" data-action="shared-attributions-use-review"${off(locked || !status)}>Review sharing change</button></div>
    <div id="shared-attributions-use-review" aria-live="polite">${reviewed ? `<div class="alert warning"><div><strong>${reviewed.requested ? 'Enable shared attributions' : 'Stop using shared attributions'} for this investigation?</strong><p>${reviewed.requested ? `${esc(reviewed.shared_count)} shared entries will be available. ${esc(reviewed.local_overrides)} local overrides take precedence.` : 'Future work will use only this investigation’s local assessments.'} Saved runs and previews keep their captured inputs. Local tracing controls and colors stay unchanged.</p></div></div>` : ''}</div>
    <div class="form-actions"><button type="button" class="btn primary" id="shared-attributions-use-apply" data-action="shared-attributions-use-apply"${off(locked || !reviewed)}>Apply reviewed sharing choice</button></div></section>`;
}
export function sharedAttributionsPanel(caseId: string | null, busy: boolean, blockchain?: string): string {
  selectSharedAttributions(caseId, blockchain);
  if (!state.open) return '';
  const locked = busy || state.pending, catalog = state.catalog, review = state.review;
  return `<section class="panel" id="shared-attributions-panel" aria-labelledby="shared-attributions-title"><div class="panel-head"><div><h2 id="shared-attributions-title" tabindex="-1">Shared attributions</h2><p>Reuse address names and assessment evidence across investigations that opt in.</p></div><button type="button" class="btn" data-action="shared-attributions-close">Close</button></div><div class="panel-body">
    <p class="address-note">Editing this workspace library affects future work in every investigation that uses it. New generations capture the current shared entries; saved runs and previews remain unchanged. Each investigation must enable sharing separately.</p>
    ${sharingPanel(locked)}
    <h3>Workspace library</h3>
    ${caseId ? `<p>${state.blockchain === 'bitcoin' ? 'Bitcoin' : 'Liquid'} attribution library</p>` : `<label class="field"><span>Blockchain</span><select id="shared-attributions-blockchain"${off(locked)}><option value="liquid"${state.blockchain === 'liquid' ? ' selected' : ''}>Liquid Network</option><option value="bitcoin"${state.blockchain === 'bitcoin' ? ' selected' : ''}>Bitcoin Network</option></select></label>`}
    <p class="small muted">Libraries are separate for each blockchain. Names do not enable tracing stops automatically.</p>
    <div class="form-actions"><a class="btn" href="${BASE}/export?blockchain=${state.blockchain}" download>Export shared CSV</a></div>
    <p class="small muted">Exports include every shared entry, including disabled entries. Large exports download as a ZIP of CSV parts.</p>
    <label class="field"><span>Search shared attributions</span><input id="shared-attributions-query" maxlength="256" value="${esc(state.query)}"${off(locked)}/></label>
    <div class="form-actions"><button type="button" class="btn" data-action="shared-attributions-load"${off(locked)}>Search / refresh</button></div>
    ${catalog ? `<div class="table-wrap"><table><caption>Saved shared attributions</caption><thead><tr><th>Address</th><th>Name and assessment evidence</th></tr></thead><tbody>${catalog.rows.map(rule => `<tr><td class="mono">${esc(rule.address)}</td><td>${fields(rule)}</td></tr>`).join('')}</tbody></table></div>${!catalog.rows.length ? '<p>No shared attributions match this search.</p>' : ''}` : `<p role="status">${state.pending ? 'Loading shared attributions…' : 'Refresh to load the workspace library.'}</p>`}
    <div class="form-actions"><span>${catalog ? `${esc(catalog.total)} matching entries · Page ${Math.floor(state.offset / PAGE_SIZE) + 1}` : 'Library count unavailable'}</span><button type="button" class="btn" data-action="shared-attributions-prev"${off(locked || state.offset === 0)}>Previous</button><button type="button" class="btn" data-action="shared-attributions-next"${off(locked || !catalog || state.offset + PAGE_SIZE >= catalog.total)}>Next</button></div>
    <h3>Import into the shared library</h3><p>Choose CSV or JSON, or paste its contents. Supported fields: Address, Name, confidence, source, notes, observed_at (ISO date or timestamp), and enabled. Review before saving. Up to 5,000 rows / 512 KiB per import.</p>
    <p class="address-note">Imports change the shared library for all opted-in investigations. Existing local assessments continue to override it. Imported stop_tracing and hop_limit values are ignored; configure tracing controls and colors inside each investigation.</p>
    <label class="field"><span>Choose a shared attribution file</span><input type="file" id="shared-attributions-file" accept=".csv,.json,text/csv,application/json"${off(locked)}/><small>${esc(state.filename || 'Or paste CSV or JSON below.')}</small></label>
    <label class="field"><span>CSV or JSON contents</span><textarea id="shared-attributions-text" rows="6"${off(locked)}>${esc(state.text)}</textarea></label>
    <div class="field-row"><label class="field"><span>Format</span><select id="shared-attributions-format"${off(locked)}>${['auto', 'csv', 'json'].map(format => `<option value="${format}"${state.format === format ? ' selected' : ''}>${format.toUpperCase()}</option>`).join('')}</select></label>
    <label class="field"><span>Existing shared entries</span><select id="shared-attributions-policy"${off(locked)}><option value="keep"${state.policy === 'keep' ? ' selected' : ''}>Keep existing</option><option value="replace"${state.policy === 'replace' ? ' selected' : ''}>Replace conflicts</option></select></label></div>
    <p class="small muted">Replace conflicts replaces the full shared assessment for matching addresses, including enabled state, evidence fields, and blank values. It does not remove addresses omitted from the import.</p>
    <div class="form-actions"><button type="button" class="btn" data-action="shared-attributions-template"${off(locked)}>Use CSV template</button>${caseId ? `<button type="button" class="btn" data-action="shared-attributions-from-case"${off(locked)}>Use this investigation’s attributions</button>` : ''}<button type="button" class="btn primary" id="shared-attributions-preview" data-action="shared-attributions-preview"${off(locked || !state.text.trim())}>Review shared import</button></div>
    <div id="shared-attributions-review" aria-live="polite">${review ? `<h3>Review shared library changes</h3><p>${esc(counts(review.counts))}${review.duplicate_rows ? ` · ${esc(review.duplicate_rows)} identical duplicate rows` : ''}.</p><p>${esc(review.notice)}</p>
    ${review.ignored_controls && ((review.ignored_controls.stop_tracing || 0) + (review.ignored_controls.hop_limit || 0)) ? `<p class="artifact-note">Ignored local tracing controls: ${esc(review.ignored_controls.stop_tracing || 0)} stop_tracing values and ${esc(review.ignored_controls.hop_limit || 0)} hop_limit values. They will not be shared.</p>` : ''}
    ${(review.errors || []).length ? `<div class="alert error"><div><strong>Fix these errors before saving.</strong>${review.errors!.map(error => `<p>${error.row ? `Row ${esc(error.row)}: ` : ''}${esc(error.message)}</p>`).join('')}</div></div>` : ''}
    <div class="table-wrap"><table><thead><tr><th>Row / address</th><th>Action</th><th>Incoming assessment</th><th>Existing shared assessment</th></tr></thead><tbody>${review.changes.slice(state.reviewOffset, state.reviewOffset + PAGE_SIZE).map(change => `<tr><td>${esc(change.row)}<br><span class="mono">${esc(change.rule.address)}</span></td><td>${esc(change.action)}</td><td>${fields(change.rule)}</td><td>${fields(change.previous)}</td></tr>`).join('')}</tbody></table></div>
    <div class="form-actions"><span>Changes ${review.changes.length ? state.reviewOffset + 1 : 0}–${Math.min(state.reviewOffset + PAGE_SIZE, review.changes.length)} of ${review.changes.length}</span><button type="button" class="btn" data-action="shared-attributions-review-prev"${off(locked || state.reviewOffset === 0)}>Previous changes</button><button type="button" class="btn" data-action="shared-attributions-review-next"${off(locked || state.reviewOffset + PAGE_SIZE >= review.changes.length)}>Next changes</button></div>` : ''}</div>
    <div class="form-actions"><button type="button" class="btn primary" id="shared-attributions-apply" data-action="shared-attributions-apply"${off(locked || !review?.valid || !review.approval_sha256)}>Apply reviewed shared import</button></div>
    <p role="status">${esc(state.message)}</p></div></section>`;
}
async function load(owner: typeof state, context: Context): Promise<void> {
  const version = owner.catalogVersion, sharingVersion = owner.sharingVersion;
  const query = owner.query, offset = owner.offset;
  const results = await Promise.allSettled([
    context.post<Catalog>(BASE, {query, offset, limit: PAGE_SIZE, blockchain: owner.blockchain}),
    owner.caseId ? context.get<Sharing>(`/api/cases/${encodeURIComponent(owner.caseId)}/shared-attributions`) : Promise.resolve(null),
  ]);
  if (!retained(owner)) return;
  const errors: string[] = [];
  const [catalog, sharing] = results;
  if (catalog.status === 'fulfilled') {
    if (version === owner.catalogVersion) {owner.catalog = catalog.value; owner.offset = catalog.value.offset;}
  } else errors.push(catalog.reason instanceof Error ? catalog.reason.message : 'Could not load the shared library.');
  if (sharing.status === 'fulfilled') {
    if (sharingVersion === owner.sharingVersion && sharing.value) {
      if (!owner.sharing || owner.enabled === owner.sharing.enabled) owner.enabled = sharing.value.enabled;
      owner.sharing = sharing.value;
    }
  } else errors.push(sharing.reason instanceof Error ? sharing.reason.message : 'Could not load investigation sharing.');
  if (errors.length) throw new Error(errors.join(' '));
}
async function refresh(context: Context, scope: 'library' | 'case', owner: typeof state): Promise<void> {
  try {await context.refresh?.(scope);}
  catch (error) {owner.message += ` View could not refresh: ${error instanceof Error ? error.message : 'Refresh the page.'}`;}
}
export async function sharedAttributionsAction(action: string, context: Context): Promise<boolean> {
  if (!action.startsWith('shared-attributions-')) return false;
  selectSharedAttributions(context.caseId, context.blockchain);
  const owner = state;
  if (action === 'shared-attributions-close') {
    owner.open = false; invalidate(owner); invalidateSharing(owner); owner.catalogVersion++; context.render(); return true;
  }
  if (context.busy || owner.pending) return true;
  if (action === 'shared-attributions-template') {
    invalidate(owner); owner.text = TEMPLATE; owner.format = 'csv'; owner.filename = '';
    owner.message = 'Replace the placeholder address and review the shared import before saving.'; context.render(); return true;
  }
  if (action === 'shared-attributions-review-prev' || action === 'shared-attributions-review-next') {
    const last = Math.max(0, (Math.ceil((owner.review?.changes.length || 0) / PAGE_SIZE) - 1) * PAGE_SIZE);
    owner.reviewOffset = Math.max(0, Math.min(last, owner.reviewOffset + (action.endsWith('-next') ? PAGE_SIZE : -PAGE_SIZE)));
    context.render(); return true;
  }
  const imports = ['shared-attributions-preview', 'shared-attributions-apply'];
  const catalogs = ['shared-attributions-open', 'shared-attributions-load', 'shared-attributions-prev', 'shared-attributions-next'];
  if (![...imports, ...catalogs, 'shared-attributions-from-case', 'shared-attributions-use-review', 'shared-attributions-use-apply'].includes(action)) return true;
  if (action === 'shared-attributions-apply' && (!owner.review?.valid || !owner.review.approval_sha256)) return true;
  if (action === 'shared-attributions-preview' && !owner.text.trim()) return true;
  if (action === 'shared-attributions-use-apply' && !owner.sharingReview) return true;
  if (['shared-attributions-from-case', 'shared-attributions-use-review', 'shared-attributions-use-apply'].includes(action) && !owner.caseId) return true;
  if (catalogs.includes(action)) {
    invalidate(owner); invalidateSharing(owner); owner.open = true;
    if (action.endsWith('-prev')) owner.offset = Math.max(0, owner.offset - PAGE_SIZE);
    else if (action.endsWith('-next')) owner.offset += PAGE_SIZE;
    else owner.offset = 0;
  }
  if (action === 'shared-attributions-from-case') invalidate(owner);
  const version = owner.version, sharingVersion = owner.sharingVersion;
  owner.pending = true; owner.message = ''; context.render();
  try {
    if (catalogs.includes(action)) await load(owner, context);
    else if (action === 'shared-attributions-from-case') {
      const result = await context.readText(`/api/cases/${encodeURIComponent(owner.caseId!)}/input-exports/attributions`);
      checkText(result.text, '', result.contentType);
      if (retained(owner) && version === owner.version) {
        owner.text = result.text; owner.filename = 'Investigation attributions'; owner.format = 'csv';
        owner.message = 'Investigation attributions copied into the draft. Review before saving to the shared library. Local tracing controls will be ignored.';
      }
    } else if (action === 'shared-attributions-use-review') {
      const requested = owner.enabled;
      const status = await context.get<Sharing>(`/api/cases/${encodeURIComponent(owner.caseId!)}/shared-attributions`);
      if (retained(owner) && sharingVersion === owner.sharingVersion) {
        owner.sharing = status;
        if (requested && status.shared_unavailable) throw new Error('The shared library is unavailable. Refresh after it is restored before enabling sharing. You can still disable sharing.');
        owner.sharingReview = {...status, requested};
      }
    } else if (action === 'shared-attributions-use-apply') {
      const reviewed = owner.sharingReview!;
      const result = await context.post<Sharing>(`/api/cases/${encodeURIComponent(owner.caseId!)}/shared-attributions`,
        {enabled: reviewed.requested, expected_revision: reviewed.revision});
      if (retained(owner)) {
        if (sharingVersion === owner.sharingVersion) {
          invalidateSharing(owner); owner.sharing = result; owner.enabled = result.enabled;
          owner.message = `Shared attributions ${result.enabled ? 'enabled' : 'disabled'} for this investigation. Saved runs and previews remain unchanged.`;
        }
        await refresh(context, 'case', owner);
      }
    } else {
      checkText(owner.text);
      const payload = {text: owner.text, format: owner.format, policy: owner.policy, blockchain: owner.blockchain,
        ...(action === 'shared-attributions-apply' ? {approve_plan: owner.review!.approval_sha256} : {})};
      if (action === 'shared-attributions-preview') {
        const review = await context.post<Review>(BASE + '/import', payload);
        if (retained(owner) && version === owner.version) {owner.review = review; owner.reviewOffset = 0;}
      } else {
        const result = await context.post<{changed: number; revision: number; notice?: string}>(BASE + '/import', payload);
        // Other views may hold a review of the same library. Keep their drafts, but require fresh approval.
        for (const draft of drafts.values()) {if (draft.blockchain !== owner.blockchain) continue; invalidate(draft); invalidateSharing(draft); draft.catalogVersion++; draft.catalog = null;}
        if (retained(owner)) {
          owner.message = `Saved ${result.changed} shared attribution changes. They apply to future work in opted-in investigations. Saved runs and previews remain unchanged.`;
          await refresh(context, 'library', owner);
          try {await load(owner, context);}
          catch (error) {owner.message += ` Library could not refresh: ${error instanceof Error ? error.message : 'Refresh to try again.'}`;}
        }
      }
    }
  } catch (error) {
    const current = imports.includes(action) || action === 'shared-attributions-from-case' ? version === owner.version
      : action.startsWith('shared-attributions-use-') ? sharingVersion === owner.sharingVersion : true;
    if (retained(owner) && current) {
      if (imports.includes(action) && version === owner.version) invalidate(owner);
      if (action.startsWith('shared-attributions-use-') && sharingVersion === owner.sharingVersion) invalidateSharing(owner);
      owner.message = error instanceof Error ? error.message : 'Could not update shared attributions. Review and try again.';
    }
  } finally {
    owner.pending = false;
    if (owner === state) {context.render(); if (action === 'shared-attributions-open') document.querySelector<HTMLElement>('#shared-attributions-title')?.focus();}
  }
  return true;
}
