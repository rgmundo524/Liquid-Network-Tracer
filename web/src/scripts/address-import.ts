/** Local file contents only. The server never receives a user-selected filesystem path. */
type Change = { row: number; action: string; rule: Record<string, string | boolean>; previous: Record<string, string | boolean> | null };
type Review = { valid: boolean; approval_sha256: string | null; unique_addresses: number;
  counts: Record<string, number>; duplicate_rows: number; active_stops_to_save: number;
  changes: Change[]; errors: { row: number; message: string }[]; notice: string };
type Context = { caseId: string; busy: boolean; render: () => void;
  post: <T>(path: string, body: unknown) => Promise<T>; refresh: () => Promise<void> };
const MAX_BYTES = 512 * 1024;
const TEMPLATE = 'Address,Name,confidence,stop_tracing,source,notes\nREPLACE_WITH_LIQUID_ADDRESS_1,Example Exchange,suspected,true,Investigator research,Explain the evidence\nREPLACE_WITH_LIQUID_ADDRESS_2,Client wallet,confirmed,false,Client records,Continue tracing\n';
let draft = { caseId: '', text: '', filename: '', format: 'auto', policy: 'keep', approved: false,
  review: null as Review | null, pending: false, message: '', offset: 0 };
const esc = (value: unknown): string => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]!));

export function resetAddressImport(caseId: string): void {
  draft = { caseId, text: '', filename: '', format: 'auto', policy: 'keep', approved: false,
    review: null, pending: false, message: '', offset: 0 };
}

function invalidate(): void {
  draft.review = null; draft.approved = false; draft.message = ''; draft.offset = 0;
  const button = document.querySelector<HTMLButtonElement>('#attribution-apply');
  if (button) button.disabled = true;
  const checkbox = document.querySelector<HTMLInputElement>('#attribution-approved');
  if (checkbox) checkbox.checked = false;
  const preview = document.querySelector('#attribution-review');
  if (preview) preview.textContent = 'Input changed. Preview again before applying.';
}

export function addressImportInput(element: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement): boolean {
  if (!element.id.startsWith('attribution-') || element.id === 'attribution-file') return false;
  if (element.id === 'attribution-approved') {
    draft.approved = (element as HTMLInputElement).checked;
    const button = document.querySelector<HTMLButtonElement>('#attribution-apply');
    if (button) button.disabled = !draft.review?.valid || !draft.approved || draft.pending;
    return true;
  }
  if (element.id === 'attribution-text') { draft.text = element.value; draft.filename = ''; }
  if (element.id === 'attribution-format') draft.format = element.value;
  if (element.id === 'attribution-replace') draft.policy = (element as HTMLInputElement).checked ? 'replace' : 'keep';
  invalidate(); return true;
}

export async function addressImportFile(input: HTMLInputElement, render: () => void): Promise<void> {
  const file = input.files?.[0];
  if (!file || draft.pending) return;
  if (file.size > MAX_BYTES) throw new Error('Address import exceeds 512 KiB. Split it into smaller files.');
  const owner = draft.caseId;
  const text = await file.text();
  if (owner !== draft.caseId) return;
  invalidate(); draft.text = text; draft.filename = file.name; draft.format = 'auto'; render();
}

export function addressImportPanel(caseId: string, busy: boolean): string {
  if (draft.caseId !== caseId) resetAddressImport(caseId);
  const locked = busy || draft.pending;
  const disabled = locked ? ' disabled' : '';
  const review = draft.review;
  return `<section class="panel" id="address-import-panel"><div class="panel-head"><div><h2 tabindex="-1" id="address-import-title">Import address attributions</h2><p>Prepare known or suspected services before the first run, or import between runs.</p></div></div><div class="panel-body">
  <p class="address-note">CSV, JSON, or plain address lists. A plain list defaults to suspected confidence with tracing stopped. CSV/JSON accepts Address, Name, confidence, stop_tracing, source, and notes. Confidence and stopping are independent. Up to 5,000 rows / 512 KiB. This action is local and does not start a trace.</p>
  <label class="field"><span>Choose an attribution file</span><input type="file" id="attribution-file" accept=".csv,.json,.txt,text/csv,application/json,text/plain"${disabled}/><small>${esc(draft.filename || 'Or paste the contents below.')}</small></label>
  <label class="field"><span>Addresses or file contents</span><textarea id="attribution-text" rows="6"${disabled}>${esc(draft.text)}</textarea></label>
  <label class="field"><span>Format</span><select id="attribution-format"${disabled}>${['auto','csv','json','text'].map(format => `<option value="${format}"${draft.format === format ? ' selected' : ''}>${format.toUpperCase()}</option>`).join('')}</select></label>
  <label class="check-line"><input type="checkbox" id="attribution-replace"${draft.policy === 'replace' ? ' checked' : ''}${disabled}/><span>Replace conflicting existing assessments. Otherwise, existing entries are kept.</span></label>
  <div class="form-actions"><button class="btn" data-action="attribution-template"${disabled}>Use CSV template</button><button class="btn primary" data-action="attribution-preview"${disabled}>Preview import</button></div>
  <div id="attribution-review" aria-live="polite">${review ? `<p><strong>${review.unique_addresses} unique addresses:</strong> ${review.counts.add} new, ${review.counts.replace} replacements, ${review.counts.keep} existing conflicts kept, ${review.counts.unchanged} unchanged, ${review.duplicate_rows} identical duplicate rows. ${review.active_stops_to_save} active stop rules will be saved.</p><p class="address-note">${esc(review.notice)}</p>
  ${review.errors.length ? `<div class="alert error"><div><strong>Nothing can be saved until these errors are fixed.</strong>${review.errors.map(error => `<p>Row ${error.row}: ${esc(error.message)}</p>`).join('')}</div></div>` : ''}
  <div class="table-wrap"><table><thead><tr><th>Row</th><th>Address</th><th>Action</th><th>Name</th><th>Confidence</th><th>Stop</th><th>Evidence / previous entry</th></tr></thead><tbody>${review.changes.slice(draft.offset, draft.offset + 100).map(entry => `<tr><td>${entry.row}</td><td class="mono">${esc(entry.rule.address)}</td><td>${esc(entry.action)}</td><td>${esc(entry.rule.name)}</td><td>${esc(entry.rule.confidence)}</td><td>${entry.rule.enabled && entry.rule.stop_tracing ? 'Yes' : 'No'}</td><td><details><summary>Review fields</summary><pre>${esc(JSON.stringify({incoming: entry.rule, existing: entry.previous}, null, 2))}</pre></details></td></tr>`).join('')}</tbody></table></div>
  <div class="form-actions"><span>Rows ${review.changes.length ? draft.offset + 1 : 0}–${Math.min(draft.offset + 100, review.changes.length)} of ${review.changes.length}</span><button class="btn" data-action="attribution-prev"${locked || draft.offset === 0 ? ' disabled' : ''}>Previous</button><button class="btn" data-action="attribution-next"${locked || draft.offset + 100 >= review.changes.length ? ' disabled' : ''}>Next</button></div>` : ''}</div>
  <label class="check-line"><input type="checkbox" id="attribution-approved"${draft.approved ? ' checked' : ''}${locked || !review?.valid ? ' disabled' : ''}/><span>I reviewed the address attributions and tracing-stop settings.</span></label>
  <div class="form-actions"><button class="btn primary" id="attribution-apply" data-action="attribution-apply"${locked || !review?.valid || !draft.approved ? ' disabled' : ''}>Apply reviewed import</button></div>
  ${draft.message ? `<p role="status">${esc(draft.message)}</p>` : ''}</div></section>`;
}

export async function addressImportAction(action: string, context: Context): Promise<boolean> {
  if (!action.startsWith('attribution-')) return false;
  if (context.busy || draft.pending || draft.caseId !== context.caseId) return true;
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
  if (action === 'attribution-apply' && (!draft.approved || !draft.review?.valid)) throw new Error('Preview and approve the import before saving.');
  const payload = { text: draft.text, format: draft.format, policy: draft.policy,
    ...(action === 'attribution-apply' ? { approve_plan: draft.review!.approval_sha256 } : {}) };
  const owner = draft;
  draft.pending = true; draft.message = ''; context.render();
  try {
    const path = `/api/cases/${encodeURIComponent(context.caseId)}/address-import`;
    if (action === 'attribution-preview') {
      const reviewed = await context.post<Review>(path, payload);
      if (owner === draft) { draft.review = reviewed; draft.approved = false; draft.offset = 0; }
    } else {
      const result = await context.post<{ changed: number }>(path, payload);
      if (owner === draft) {
        invalidate(); draft.message = `Saved ${result.changed} assessments. The first/next run will use them. No trace was started.`;
        // Refresh the selected assessment as well as the list in the owning view.
        await context.refresh();
      }
    }
  } catch (error) {
    if (owner === draft) invalidate();
    throw error;
  } finally {
    owner.pending = false;
    if (owner === draft) context.render();
  }
  return true;
}
