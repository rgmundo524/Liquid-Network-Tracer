/** Review case-local name colors before saving. This never starts a trace or Miro sync. */
type Change = {row: number; name: string; key: string; color: string | null; previous: string | null;
  action: string; addresses: number};
type Review = {valid: boolean; approval_sha256: string | null; format: string; policy: string;
  counts: Record<string, number>; input_rows: number; unique_names: number; duplicate_rows: number;
  errors: {row: number; message: string}[]; changes: Change[]; notice: string};
type Context = {caseId: string; busy: boolean; render: () => void;
  post: <T>(path: string, body: unknown) => Promise<T>; refresh: () => Promise<void>};
const MAX_BYTES = 512 * 1024;
const TEMPLATE = 'Name,Color\nPerp,#f0abfc\nExample Exchange,#93c5fd\nClient wallet,#bbf7d0\n';
const esc = (value: unknown): string => String(value ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]!));
const safeColor = (value: unknown): value is string => typeof value === 'string' && /^#[0-9a-fA-F]{6}$/.test(value);
function emptyDraft(caseId: string) {
  return {caseId, open: false, text: '', filename: '', format: 'auto', policy: 'keep',
    review: null as Review | null, pending: false, message: '', offset: 0, version: 0};
}
let draft = emptyDraft('');

export function resetNameColorImport(caseId: string): void { draft = emptyDraft(caseId); }
export function nameColorImportPending(): boolean { return draft.pending; }

export function invalidateNameColorImport(): void {
  draft.version += 1; draft.review = null; draft.message = ''; draft.offset = 0;
  const apply = document.querySelector<HTMLButtonElement>('#name-color-import-apply');
  if (apply) apply.disabled = true;
  const review = document.querySelector('#name-color-import-review');
  if (review) review.textContent = 'Preview again before applying.';
}

export function nameColorImportInput(element: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement): boolean {
  if (!element?.id?.startsWith('name-color-import-') || element.id === 'name-color-import-file') return false;

  if (element.id === 'name-color-import-text') { draft.text = element.value; draft.filename = ''; }
  else if (element.id === 'name-color-import-format') draft.format = element.value;
  else if (element.id === 'name-color-import-replace') draft.policy = (element as HTMLInputElement).checked ? 'replace' : 'keep';
  else return false;
  invalidateNameColorImport(); return true;
}

export async function nameColorImportFile(input: HTMLInputElement, render: () => void): Promise<void> {
  const file = input.files?.[0];
  if (!file || draft.pending) return;
  invalidateNameColorImport();
  if (file.size > MAX_BYTES) {
    draft.message = 'Name color import exceeds 512 KiB. Split it into smaller files.';
    render(); return;
  }
  const owner = draft, version = draft.version;
  owner.pending = true; render();
  try {
    const text = await file.text();
    if (owner !== draft || version !== draft.version) return;
    if (new TextEncoder().encode(text).length > MAX_BYTES) throw new Error('Name color import exceeds 512 KiB.');
    owner.text = text; owner.filename = file.name; owner.format = 'auto';
  } catch (error) {
    if (owner === draft && version === draft.version) owner.message = error instanceof Error ? error.message : 'Could not read the selected file.';
  } finally {
    owner.pending = false;
    if (owner === draft) render();
  }
}

function swatch(color: string | null, fallback: string): string {
  return color === null ? esc(fallback) : `${safeColor(color) ? `<span aria-hidden="true" style="display:inline-block;width:1em;height:1em;border:1px solid currentColor;background-color:${color};vertical-align:middle"></span> ` : ''}${esc(color)}`;
}

export function nameColorImportPanel(caseId: string, busy: boolean): string {
  if (draft.caseId !== caseId) resetNameColorImport(caseId);
  const locked = busy || draft.pending, disabled = locked ? ' disabled' : '';
  if (!draft.open) return `<div class="form-actions"><button class="btn" data-action="name-color-import-open"${disabled}>Import name colors</button></div>`;
  const review = draft.review;
  return `<section id="name-color-import-panel" aria-labelledby="name-color-import-title"><div class="panel-head"><h4 id="name-color-import-title" tabindex="-1">Import name colors</h4><button class="btn" data-action="name-color-import-close"${disabled}>Close import</button></div>
  <p class="address-note">Choose a CSV or JSON file, or paste its contents. Use Name and Color fields with #RRGGBB colors. Names and headers are case-insensitive. CSV columns can be in any order; unrelated columns such as Duplicate count are ignored. Supported fields still require valid values, and JSON rejects unsupported fields. Import attributions first so the names already exist. Blank colors clear an assignment only when replacement is enabled. Graph-role colors are edited above. Up to 512 KiB.</p>
  <label class="field"><span>Choose a name color file</span><input type="file" id="name-color-import-file" accept=".csv,.json,text/csv,application/json"${disabled}/><small>${esc(draft.filename || 'Or paste the contents below.')}</small></label>
  <label class="field"><span>Name color file contents</span><textarea id="name-color-import-text" rows="6"${disabled}>${esc(draft.text)}</textarea></label>
  <label class="field"><span>Format</span><select id="name-color-import-format"${disabled}>${['auto', 'csv', 'json'].map(format => `<option value="${format}"${draft.format === format ? ' selected' : ''}>${format.toUpperCase()}</option>`).join('')}</select></label>
  <label class="check-line"><input type="checkbox" id="name-color-import-replace"${draft.policy === 'replace' ? ' checked' : ''}${disabled}/><span>Replace conflicting existing name colors and allow blank colors to clear assignments. Otherwise, existing colors are kept.</span></label>
  <div class="form-actions"><button class="btn" data-action="name-color-import-template"${disabled}>Use CSV template</button><button class="btn primary" data-action="name-color-import-preview"${disabled}>Preview import</button></div>
  <div id="name-color-import-review" aria-live="polite">${review ? `<p><strong>${review.unique_names} unique names:</strong> ${review.counts.add} new colors, ${review.counts.replace} replacements, ${review.counts.clear} cleared, ${review.counts.keep} existing conflicts kept, ${review.counts.unchanged} unchanged. ${review.duplicate_rows} identical duplicate rows.</p><p class="address-note">${esc(review.notice)}</p>
  ${review.errors.length ? `<div class="alert error"><div><strong>Fix these errors before applying.</strong>${review.errors.map(error => `<p>Row ${error.row}: ${esc(error.message)}</p>`).join('')}</div></div>` : ''}
  <div class="table-wrap"><table><thead><tr><th>Row</th><th>Name</th><th>Addresses</th><th>Existing color</th><th>Requested color</th><th>Action</th></tr></thead><tbody>${review.changes.slice(draft.offset, draft.offset + 100).map(change => `<tr><td>${change.row}</td><td>${esc(change.name)}</td><td>${change.addresses}</td><td>${swatch(change.previous, 'No assignment')}</td><td>${swatch(change.color, 'Clear assignment')}</td><td>${esc(change.action)}</td></tr>`).join('')}</tbody></table></div>
  <div class="form-actions"><span>Rows ${review.changes.length ? draft.offset + 1 : 0}–${Math.min(draft.offset + 100, review.changes.length)} of ${review.changes.length}</span><button class="btn" data-action="name-color-import-prev"${locked || draft.offset === 0 ? ' disabled' : ''}>Previous</button><button class="btn" data-action="name-color-import-next"${locked || draft.offset + 100 >= review.changes.length ? ' disabled' : ''}>Next</button></div>` : ''}</div>
  <div class="form-actions"><button class="btn primary" id="name-color-import-apply" data-action="name-color-import-apply"${locked || !review?.valid ? ' disabled' : ''}>Apply reviewed name colors</button></div>
  <p class="address-note">Saved locally. Regenerate a preview or sync Miro separately to update graph colors.</p>
  ${draft.message ? `<p role="status">${esc(draft.message)}</p>` : ''}</section>`;
}

export async function nameColorImportAction(action: string, context: Context): Promise<boolean> {
  if (!action.startsWith('name-color-import-')) return false;
  if (context.busy || draft.pending || draft.caseId !== context.caseId) return true;
  if (action === 'name-color-import-open' || action === 'name-color-import-close') {
    draft.open = action.endsWith('-open'); context.render();
    if (draft.open) document.querySelector<HTMLElement>('#name-color-import-title')?.focus();
    return true;
  }
  if (action === 'name-color-import-template') {
    invalidateNameColorImport(); draft.text = TEMPLATE; draft.filename = ''; draft.format = 'csv';
    draft.message = 'Replace the example names with names in this investigation before previewing.';
    context.render(); return true;
  }
  if (action === 'name-color-import-prev' || action === 'name-color-import-next') {
    const finalOffset = Math.max(0, (Math.ceil((draft.review?.changes.length || 0) / 100) - 1) * 100);
    draft.offset = Math.max(0, Math.min(finalOffset, draft.offset + (action.endsWith('-next') ? 100 : -100)));
    context.render(); return true;
  }
  if (!['name-color-import-preview', 'name-color-import-apply'].includes(action)) return true;
  const applying = action === 'name-color-import-apply';
  if (applying && (!draft.review?.valid || !draft.review.approval_sha256)) {
    draft.message = 'Preview and review the import before applying.'; context.render(); return true;
  }
  if (new TextEncoder().encode(draft.text).length > MAX_BYTES) {
    invalidateNameColorImport(); draft.message = 'Name color import exceeds 512 KiB.'; context.render(); return true;
  }
  const payload = {text: draft.text, format: draft.format, policy: draft.policy,
    ...(applying ? {approve_plan: draft.review!.approval_sha256} : {})};
  if (!applying) invalidateNameColorImport();
  const owner = draft, version = draft.version;
  owner.pending = true; owner.message = ''; context.render();
  try {
    const path = `/api/cases/${encodeURIComponent(context.caseId)}/name-color-import`;
    if (!applying) {
      const review = await context.post<Review>(path, payload);
      if (owner === draft && version === draft.version) { owner.review = review; owner.offset = 0; }
    } else {
      const result = await context.post<{changed: number; revision: number; notice: string}>(path, payload);
      if (owner === draft) {
        if (version === draft.version) {
          invalidateNameColorImport();
          owner.message = `Saved ${result.changed} name color assignment(s). Regenerate a preview or sync Miro to update graph colors. No trace or sync was started.`;
        }
        try { await context.refresh(); }
        catch (error) {
          if (owner === draft) owner.message += ` The color list could not refresh: ${error instanceof Error ? error.message : 'refresh the color menu and try again.'}`;
        }
      }
    }
  } catch (error) {
    if (owner === draft && (applying || version === draft.version)) {
      invalidateNameColorImport();
      owner.message = error instanceof Error ? error.message : 'Could not import name colors. Preview again and retry.';
    }
  } finally {
    owner.pending = false;
    if (owner === draft) context.render();
  }
  return true;
}
