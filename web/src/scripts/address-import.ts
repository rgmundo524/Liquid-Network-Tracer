/** Local file contents only. The server never receives a user-selected filesystem path. */
type Change = { row: number; action: string; rule: Record<string, string | boolean | number | null>; previous: Record<string, string | boolean | number | null> | null };
type Review = { valid: boolean; approval_sha256: string | null; unique_addresses: number;
  counts: Record<string, number>; duplicate_rows: number; active_stops_to_save: number;
  changes: Change[]; errors: { row: number; message: string }[]; notice: string };
type Context = { caseId: string; busy: boolean; render: () => void;
  post: <T>(path: string, body: unknown) => Promise<T>; refresh: () => Promise<void> };
const MAX_BYTES = 512 * 1024;
const TEMPLATE = 'Address,Name,confidence,stop_tracing,hop_limit,source,notes\nREPLACE_WITH_ADDRESS_1,Example Exchange,suspected,true,,Investigator research,Explain the evidence\nREPLACE_WITH_ADDRESS_2,Client wallet,confirmed,false,,Client records,Continue tracing\nREPLACE_WITH_ADDRESS_3,Service deposit,suspected,false,1,Research,Display one consolidation hop\n';
function emptyDraft(caseId: string) {
  return {caseId, text: '', filename: '', format: 'auto', policy: 'keep',
    review: null as Review | null, pending: false, message: '', offset: 0, version: 0};
}
let draft = emptyDraft('');
const cases = new Map<string, typeof draft>();
export function selectAddressImport(caseId: string): void {
  if (!cases.has(caseId)) cases.set(caseId, emptyDraft(caseId));
  draft = cases.get(caseId)!;
}
const retained = (owner: typeof draft): boolean => cases.get(owner.caseId) === owner;
const esc = (value: unknown): string => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]!));

export function resetAddressImport(caseId: string): void {
  cases.set(caseId, emptyDraft(caseId)); selectAddressImport(caseId);
}

function invalidate(owner = draft): void {
  owner.version += 1; owner.review = null; owner.message = ''; owner.offset = 0;
  if (owner !== draft) return;
  const button = document.querySelector<HTMLButtonElement>('#attribution-apply');
  if (button) button.disabled = true;
  const preview = document.querySelector('#attribution-review');
  if (preview) preview.textContent = 'Input changed. Preview again before applying.';
}

export function addressImportInput(element: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement): boolean {
  if (!element.id.startsWith('attribution-') || element.id === 'attribution-file') return false;

  if (element.id === 'attribution-text') { draft.text = element.value; draft.filename = ''; }
  if (element.id === 'attribution-format') draft.format = element.value;
  if (element.id === 'attribution-replace') draft.policy = (element as HTMLInputElement).checked ? 'replace' : 'keep';
  invalidate(); return true;
}

export async function addressImportFile(input: HTMLInputElement, render: () => void): Promise<void> {
  const file = input.files?.[0];
  if (!file || draft.pending) return;
  if (file.size > MAX_BYTES) throw new Error('Address import exceeds 512 KiB. Split it into smaller files.');
  invalidate();
  const owner = draft, version = draft.version;
  owner.pending = true; render();
  try {
    const text = await file.text();
    if (!retained(owner) || version !== owner.version) return;
    if (new TextEncoder().encode(text).length > MAX_BYTES) throw new Error('Address import exceeds 512 KiB.');
    owner.text = text; owner.filename = file.name; owner.format = 'auto';
  } catch (error) {
    if (retained(owner) && version === owner.version) owner.message = error instanceof Error ? error.message : 'Could not read the selected file.';
  } finally {owner.pending = false; if (owner === draft) render();}
}

export function addressImportPanel(caseId: string, busy: boolean): string {
  selectAddressImport(caseId);
  const locked = busy || draft.pending;
  const disabled = locked ? ' disabled' : '';
  const review = draft.review;
  return `<section class="panel" id="address-import-panel"><div class="panel-head"><div><h2 tabindex="-1" id="address-import-title">Import address attributions</h2><p>Prepare known or suspected services before the first run, or import between runs.</p></div><a class="btn" href="/api/cases/${esc(encodeURIComponent(caseId))}/input-exports/attributions" download>Export saved CSV</a></div><div class="panel-body">
  <p class="address-note">Export includes all saved attributions across every page, including disabled entries. Unsaved edits are excluded. Empty exports contain column headers; large exports download as a ZIP of CSV parts.</p>
  <p class="address-note">CSV, JSON, or plain address lists. A plain list defaults to suspected confidence with tracing continuing. Set stop_tracing=true to request a stopping boundary. CSV/JSON accepts Address, Name, confidence, stop_tracing, hop_limit, source, and notes. CSV columns can be in any order; unrelated columns such as Duplicate count are ignored. Supported fields still require valid values, and JSON rejects unsupported fields. hop_limit is a display cap for Full trace: blank means no local cap; a nonnegative number limits additional transaction hops shown. Collection, endpoint paths, and Starter connections ignore it. Use stop_tracing to prevent further collection. Confidence and stopping are independent. Up to 5,000 rows / 512 KiB. This action is local and does not start a trace.</p>
  <label class="field"><span>Choose an attribution file</span><input type="file" id="attribution-file" accept=".csv,.json,.txt,text/csv,application/json,text/plain"${disabled}/><small>${esc(draft.filename || 'Or paste the contents below.')}</small></label>
  <label class="field"><span>Addresses or file contents</span><textarea id="attribution-text" rows="6"${disabled}>${esc(draft.text)}</textarea></label>
  <label class="field"><span>Format</span><select id="attribution-format"${disabled}>${['auto','csv','json','text'].map(format => `<option value="${format}"${draft.format === format ? ' selected' : ''}>${format.toUpperCase()}</option>`).join('')}</select></label>
  <label class="check-line"><input type="checkbox" id="attribution-replace"${draft.policy === 'replace' ? ' checked' : ''}${disabled}/><span>Replace conflicting existing assessments. Otherwise, existing entries are kept.</span></label>
  <div class="form-actions"><button class="btn" data-action="attribution-template"${disabled}>Use CSV template</button><button class="btn primary" data-action="attribution-preview"${disabled}>Preview import</button></div>
  <div id="attribution-review" aria-live="polite">${review ? `<p><strong>${review.unique_addresses} unique addresses:</strong> ${review.counts.add} new, ${review.counts.replace} replacements, ${review.counts.keep} existing conflicts kept, ${review.counts.unchanged} unchanged, ${review.duplicate_rows} identical duplicate rows. ${review.active_stops_to_save} active stop rules will be saved.</p><p class="address-note">${esc(review.notice)}</p>
  ${review.errors.length ? `<div class="alert error"><div><strong>Nothing can be saved until these errors are fixed.</strong>${review.errors.map(error => `<p>Row ${error.row}: ${esc(error.message)}</p>`).join('')}</div></div>` : ''}
  <div class="table-wrap"><table><thead><tr><th>Row</th><th>Address</th><th>Action</th><th>Name</th><th>Confidence</th><th>Stop</th><th>hop_limit</th><th>Evidence / previous entry</th></tr></thead><tbody>${review.changes.slice(draft.offset, draft.offset + 100).map(entry => `<tr><td>${entry.row}</td><td class="mono">${esc(entry.rule.address)}</td><td>${esc(entry.action)}</td><td>${esc(entry.rule.name)}</td><td>${esc(entry.rule.confidence)}</td><td>${entry.rule.enabled && entry.rule.stop_tracing ? 'Yes' : 'No'}</td><td>${esc(entry.rule.hop_limit)}</td><td><details><summary>Review fields</summary><pre>${esc(JSON.stringify({incoming: entry.rule, existing: entry.previous}, null, 2))}</pre></details></td></tr>`).join('')}</tbody></table></div>
  <div class="form-actions"><span>Rows ${review.changes.length ? draft.offset + 1 : 0}–${Math.min(draft.offset + 100, review.changes.length)} of ${review.changes.length}</span><button class="btn" data-action="attribution-prev"${locked || draft.offset === 0 ? ' disabled' : ''}>Previous</button><button class="btn" data-action="attribution-next"${locked || draft.offset + 100 >= review.changes.length ? ' disabled' : ''}>Next</button></div>` : ''}</div>
  <div class="form-actions"><button class="btn primary" id="attribution-apply" data-action="attribution-apply"${locked || !review?.valid ? ' disabled' : ''}>Apply reviewed import</button></div>
  ${draft.message ? `<p role="status">${esc(draft.message)}</p>` : ''}
  <div class="form-actions"><button class="btn" data-action="name-colors-open"${disabled}>Assign name colors</button></div></div></section>`;
}

export async function addressImportAction(action: string, context: Context): Promise<boolean> {
  if (!action.startsWith('attribution-')) return false;
  selectAddressImport(context.caseId);
  if (context.busy || draft.pending) return true;
  if (action === 'attribution-template') {
    invalidate(); draft.text = TEMPLATE; draft.filename = ''; draft.format = 'csv';
    draft.message = 'Replace the placeholder addresses before previewing.'; context.render(); return true;
  }
  if (action === 'attribution-prev' || action === 'attribution-next') {
    draft.offset = Math.max(0, Math.min(Math.max(0, (Math.ceil((draft.review?.changes.length || 0) / 100) - 1) * 100),
      draft.offset + (action === 'attribution-next' ? 100 : -100)));
    context.render(); return true;
  }
  if (!['attribution-preview', 'attribution-apply'].includes(action)) return true;
  if (new TextEncoder().encode(draft.text).length > MAX_BYTES) throw new Error('Address import exceeds 512 KiB.');
  if (action === 'attribution-apply' && (!draft.review?.valid || !draft.review.approval_sha256)) throw new Error('Preview the import before applying it.');
  const payload = { text: draft.text, format: draft.format, policy: draft.policy,
    ...(action === 'attribution-apply' ? { approve_plan: draft.review!.approval_sha256 } : {}) };
  const owner = draft, version = draft.version;
  draft.pending = true; draft.message = ''; context.render();
  try {
    const path = `/api/cases/${encodeURIComponent(context.caseId)}/address-import`;
    if (action === 'attribution-preview') {
      const reviewed = await context.post<Review>(path, payload);
      if (retained(owner) && version === owner.version) {owner.review = reviewed; owner.offset = 0;}
    } else {
      const result = await context.post<{ changed: number }>(path, payload);
      if (retained(owner) && version === owner.version) {
        invalidate(owner); owner.message = `Saved ${result.changed} assessments. Choose Assign name colors below to color the imported names. No trace was started.`;
        // Refresh the selected assessment as well as the list in the owning view.
        await context.refresh();
      }
    }
  } catch (error) {
    if (retained(owner) && version === owner.version) {
      invalidate(owner); owner.message = error instanceof Error ? error.message : 'Could not import addresses.';
    }
    if (owner === draft) throw error;
  } finally {
    owner.pending = false;
    if (owner === draft) context.render();
  }
  return true;
}
