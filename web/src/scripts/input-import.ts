/** One reviewed, atomic import for the investigation's three input CSV types. */
type Kind = 'auto' | 'attributions' | 'name-colors' | 'change-outputs';
type Policy = 'keep' | 'replace';
type FileDraft = {name: string; text: string; kind: Kind; policy: Policy};
type Change = Record<string, unknown> & {row: number; action: string};
type FileReview = {name: string; kind: Kind; policy: Policy; valid?: boolean;
  counts?: Record<string, number>; changes?: Change[]; duplicate_rows?: number; notice?: string;
  errors?: {row?: number; message: string}[]};
type Review = {valid: boolean; approval_sha256: string | null; base_revision: number;
  files: FileReview[]; errors: {file: string | number; row?: number; message: string}[];
  counts: Record<string, number>; notice: string};
type Context = {caseId: string; busy: boolean; render: () => void;
  post: <T>(path: string, body: unknown) => Promise<T>; refresh: () => Promise<void>};
const MAX_BYTES = 512 * 1024, MAX_FILES = 3, PAGE_SIZE = 50;
const LABELS: Record<Kind, string> = {auto: 'Detect automatically', attributions: 'Address attributions',
  'name-colors': 'Name colors', 'change-outputs': 'Change outputs'};
const esc = (value: unknown): string => String(value ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]!));
const off = (disabled: boolean): string => disabled ? ' disabled' : '';
function emptyDraft(caseId: string) {
  return {caseId, open: false, files: [] as FileDraft[], review: null as Review | null,
    pending: false, version: 0, message: '', offsets: [] as number[]};
}
let draft = emptyDraft('');

export function resetInputImport(caseId: string): void { draft = emptyDraft(caseId); }
export function inputImportPending(): boolean { return draft.pending; }

function invalidate(): void {
  draft.version += 1; draft.review = null; draft.offsets = []; draft.message = '';
  const apply = document.querySelector<HTMLButtonElement>('#input-import-apply');
  if (apply) apply.disabled = true;
  const review = document.querySelector('#input-import-review');
  if (review) review.textContent = 'Files or options changed. Preview again before applying.';
}

/** This is a display hint only. The server detects and validates the entire CSV. */
function detectedKind(text: string): Kind {
  const fields: string[] = [];
  let value = '', quoted = false;
  const source = text.replace(/^\uFEFF/, '');
  for (let i = 0; i < source.length; i += 1) {
    const c = source[i];
    if (c === '"') {
      if (quoted && source[i + 1] === '"') {value += '"'; i += 1;}
      else quoted = !quoted;
    } else if (!quoted && (c === ',' || c === '\n' || c === '\r')) {
      fields.push(value.trim().toLowerCase()); value = '';
      if (c !== ',') break;
    } else value += c;
    if (i === source.length - 1) fields.push(value.trim().toLowerCase());
  }
  const kinds: Kind[] = [];
  if (fields.includes('address') || fields.includes('value')) kinds.push('attributions');
  if (fields.includes('name') && fields.includes('color')) kinds.push('name-colors');
  if (fields.includes('txid') && fields.includes('changevout')) kinds.push('change-outputs');
  return kinds.length === 1 ? kinds[0] : 'auto';
}

export function inputImportInput(element: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement): boolean {
  if (!element?.id?.startsWith('input-import-') || element.id === 'input-import-files') return false;

  const match = /^input-import-(kind|policy)-(\d+)$/.exec(element.id);
  if (!match) return false;
  const file = draft.files[Number(match[2])];
  if (!file) return true;
  if (match[1] === 'kind' && Object.hasOwn(LABELS, element.value)) file.kind = element.value as Kind;
  else if (match[1] === 'policy' && ['keep', 'replace'].includes(element.value)) file.policy = element.value as Policy;
  else return true;
  invalidate(); return true;
}

export async function inputImportFiles(input: HTMLInputElement, render: () => void, busy = false): Promise<void> {
  const chosen = Array.from(input.files || []);
  if (!chosen.length || busy || draft.pending) return;
  invalidate();
  const owner = draft, version = draft.version;
  owner.pending = true; render();
  try {
    if (chosen.length > MAX_FILES) throw new Error('Choose up to three CSV files, one per type.');
    const names = new Set([...owner.files.map(file => file.name), ...chosen.map(file => file.name)]);
    if (names.size > MAX_FILES) throw new Error('Three files are already queued. Remove a file before adding another.');
    for (const file of chosen) {
      if (!/\.csv$/i.test(file.name)) throw new Error(`${file.name}: choose a .csv file. Pasted text and JSON are available under Advanced imports.`);
      if (file.size > MAX_BYTES) throw new Error(`${file.name}: exceeds 512 KiB. Split it into smaller files.`);
    }
    const loaded = await Promise.all(chosen.map(async file => {
      const bytes = await file.arrayBuffer();
      if (bytes.byteLength > MAX_BYTES) throw new Error(`${file.name}: exceeds 512 KiB.`);
      let text: string;
      try { text = new TextDecoder('utf-8', {fatal: true, ignoreBOM: true}).decode(bytes); }
      catch { throw new Error(`${file.name}: must contain valid UTF-8 text.`); }
      return {name: file.name, text, kind: 'auto' as Kind, policy: 'keep' as Policy};
    }));
    if (owner !== draft || version !== draft.version) return;
    const files = new Map(owner.files.map(file => [file.name, file]));
    loaded.forEach(file => {
      const previous = files.get(file.name);
      files.set(file.name, previous ? {...file, kind: previous.kind, policy: previous.policy} : file);
    });
    owner.files = [...files.values()];
  } catch (error) {
    if (owner === draft && version === draft.version) owner.message = error instanceof Error ? error.message : 'Could not read the selected files.';
  } finally {
    owner.pending = false;
    if (owner === draft) render();
  }
}

function countsText(counts: Record<string, number> = {}): string {
  return `${counts.add || 0} new, ${counts.replace || 0} replacements, ${counts.clear || 0} cleared, ` +
    `${counts.keep || 0} existing conflicts kept, ${counts.unchanged || 0} unchanged`;
}
function color(value: unknown): string {
  return value === null || value === undefined ? 'No assignment' :
    `${typeof value === 'string' && /^#[0-9a-fA-F]{6}$/.test(value) ? `<span aria-hidden="true" style="display:inline-block;width:1em;height:1em;border-radius:50%;border:1px solid currentColor;background-color:${value};vertical-align:middle"></span> ` : ''}${esc(value)}`;
}
function assessment(value: unknown): string {
  if (!value || typeof value !== 'object') return 'No saved assessment';
  const rule = value as Record<string, unknown>;
  return `${esc(rule.name || 'Unnamed')} · ${esc(rule.confidence)} · ${rule.enabled === false ? 'Disabled' : rule.stop_tracing ? 'Stop tracing' : 'Continue tracing'} · hop_limit: ${esc(rule.hop_limit ?? 'No local cap')}`;
}
function reviewRow(file: FileReview, entry: Change): string {
  let label: unknown, incoming: string, previous: string;
  if (file.kind === 'attributions') {
    label = (entry.rule as Record<string, unknown> | undefined)?.address;
    incoming = assessment(entry.rule); previous = assessment(entry.previous);
  } else if (file.kind === 'name-colors') {
    label = entry.name; incoming = entry.color === null ? 'Clear assignment' : color(entry.color); previous = color(entry.previous);
  } else {
    label = entry.txid; incoming = entry.vout === null ? 'Clear designation' : `vout ${esc(entry.vout)}`;
    previous = entry.previous === null ? 'No designation' : `vout ${esc(entry.previous)}`;
  }
  return `<tr><td>${esc(entry.row)}</td><td class="mono">${esc(label)}</td><td>${esc(entry.action)}</td><td>${incoming}</td><td>${previous}</td><td><details><summary>All fields</summary><pre>${esc(JSON.stringify(entry, null, 2))}</pre></details></td></tr>`;
}
function reviewFile(file: FileReview, index: number, locked: boolean): string {
  const changes = file.changes || [], offset = draft.offsets[index] || 0;
  return `<details class="input-import-file-review"><summary><strong>${esc(file.name)}</strong> · ${esc(LABELS[file.kind] || file.kind)} · ${esc(countsText(file.counts))}</summary>
    <p class="address-note">${esc(file.notice)}${file.duplicate_rows ? ` ${esc(file.duplicate_rows)} identical duplicate rows.` : ''}</p>
    <div class="table-wrap"><table><thead><tr><th>Row</th><th>Address / name / transaction</th><th>Action</th><th>Requested</th><th>Existing</th><th>Details</th></tr></thead><tbody>${changes.slice(offset, offset + PAGE_SIZE).map(entry => reviewRow(file, entry)).join('')}</tbody></table></div>
    <div class="form-actions"><span>Rows ${changes.length ? offset + 1 : 0}–${Math.min(offset + PAGE_SIZE, changes.length)} of ${changes.length}</span><button class="btn" data-action="input-import-prev" data-import-index="${index}"${off(locked || offset === 0)}>Previous</button><button class="btn" data-action="input-import-next" data-import-index="${index}"${off(locked || offset + PAGE_SIZE >= changes.length)}>Next</button></div></details>`;
}

export function inputImportPanel(caseId: string, busy: boolean): string {
  if (draft.caseId !== caseId) resetInputImport(caseId);
  if (!draft.open) return '';
  const locked = busy || draft.pending, review = draft.review;
  return `<section class="panel" id="input-import-panel" aria-labelledby="input-import-title"><div class="panel-head"><div><h2 id="input-import-title" tabindex="-1">Import CSV files</h2><p>Add attributions, name colors, and change outputs together.</p></div><button class="btn" data-action="input-import-close"${off(locked)}>Close</button></div><div class="panel-body">
    <p>Choose one, two, or all three CSV files in the same picker. File types are detected from their columns. New attribution names can receive colors in this same import, in any file order.</p>
    <label class="field"><span>${draft.files.length ? 'Add or replace CSV files' : 'Choose CSV files'}</span><input type="file" id="input-import-files" accept=".csv,text/csv" multiple${off(locked)}/><small>Up to three files, one per type. Maximum 5,000 rows / 512 KiB each. Selecting a queued filename again updates its contents and keeps its type and conflict policy. Preview it again before saving.</small></label>
    <div class="table-wrap"><table><thead><tr><th>File</th><th>Contents</th><th>Existing entries</th><th></th></tr></thead><tbody>${draft.files.map((file, index) => {
      const detected = review?.files[index]?.kind || detectedKind(file.text);
      return `<tr><td>${esc(file.name)}<small class="muted"><br>${new TextEncoder().encode(file.text).length.toLocaleString()} bytes</small></td><td><select id="input-import-kind-${index}" aria-label="File type for ${esc(file.name)}"${off(locked)}>${(Object.keys(LABELS) as Kind[]).map(kind => `<option value="${kind}"${file.kind === kind ? ' selected' : ''}>${esc(LABELS[kind])}</option>`).join('')}</select><small class="muted"><br>${file.kind === 'auto' ? detected === 'auto' ? 'Choose a type if the columns are ambiguous.' : `Detected: ${esc(LABELS[detected] || detected)}` : `Selected: ${esc(LABELS[file.kind])}`}</small></td><td><select id="input-import-policy-${index}" aria-label="Conflict policy for ${esc(file.name)}"${off(locked)}><option value="keep"${file.policy === 'keep' ? ' selected' : ''}>Keep existing</option><option value="replace"${file.policy === 'replace' ? ' selected' : ''}>Replace conflicts</option></select></td><td><button class="btn" data-action="input-import-remove" data-import-index="${index}" aria-label="Remove ${esc(file.name)}"${off(locked)}>Remove</button></td></tr>`;
    }).join('')}</tbody></table></div>
    <p class="address-note"><strong>Updating a saved hop limit?</strong> Choose Replace conflicts for the attribution file. Keep existing preserves saved assessments. Replacement also allows blank colors and blank ChangeVout values to clear saved assignments.</p>
    <details><summary>CSV columns and other import formats</summary><p>Attributions: Address, Name, confidence, stop_tracing, hop_limit, source, notes. Name colors: Name, Color (#RRGGBB). Change outputs: Txid, ChangeVout, Notes. Unrelated CSV columns are ignored; supported fields are validated.</p><p>Use Address review for Advanced imports of pasted text or JSON. Existing saved CSVs are available from Export input CSVs.</p></details>
    <div class="form-actions"><button class="btn primary" data-action="input-import-preview"${off(locked || !draft.files.length)}>Preview all files</button><button class="btn" data-action="input-import-clear"${off(locked || !draft.files.length)}>Clear files</button></div>
    <div id="input-import-review" aria-live="polite">${review ? `<p><strong>Combined review:</strong> ${esc(countsText(review.counts))}.</p><p class="address-note">${esc(review.notice)}</p>
      ${review.errors.length ? `<div class="alert error"><div><strong>Fix these errors before applying. No files have been saved.</strong>${review.errors.map(error => `<p>${esc(error.file)}${error.row ? `, row ${esc(error.row)}` : ''}: ${esc(error.message)}</p>`).join('')}</div></div>` : ''}
      ${review.files.map((file, index) => reviewFile(file, index, locked)).join('')}` : ''}</div>
    <div class="form-actions"><button class="btn primary" id="input-import-apply" data-action="input-import-apply"${off(locked || !review?.valid)}>Apply all reviewed files</button></div>
    <p class="address-note">Apply saves the displayed changes from all files together. This does not start a trace or update Miro.</p>
    ${draft.pending ? '<p role="status">Preparing import…</p>' : ''}${draft.message ? `<p role="status">${esc(draft.message)}</p>` : ''}
  </div></section>`;
}

export async function inputImportAction(action: string, context: Context, element?: HTMLElement): Promise<boolean> {
  if (!action.startsWith('input-import-')) return false;
  if (context.busy || draft.pending) return true;
  if (draft.caseId !== context.caseId) resetInputImport(context.caseId);
  if (action === 'input-import-open' || action === 'input-import-close') {
    draft.open = action === 'input-import-open'; context.render();
    if (draft.open) document.querySelector<HTMLElement>('#input-import-title')?.focus();
    return true;
  }
  const index = Number(element?.dataset.importIndex);
  if (action === 'input-import-clear' || action === 'input-import-remove') {
    if (action === 'input-import-clear') draft.files = [];
    else if (Number.isInteger(index) && index >= 0 && index < draft.files.length) draft.files.splice(index, 1);
    else return true;
    invalidate(); context.render(); return true;
  }
  if (action === 'input-import-prev' || action === 'input-import-next') {
    if (!Number.isInteger(index) || !draft.review?.files[index]) return true;
    const count = draft.review.files[index].changes?.length || 0;
    draft.offsets[index] = Math.max(0, Math.min(Math.max(0, Math.ceil(count / PAGE_SIZE) - 1) * PAGE_SIZE,
      (draft.offsets[index] || 0) + (action === 'input-import-next' ? PAGE_SIZE : -PAGE_SIZE)));
    context.render(); return true;
  }
  if (!['input-import-preview', 'input-import-apply'].includes(action) || !draft.files.length) return true;
  const applying = action === 'input-import-apply';
  if (applying && (!draft.review?.valid || !draft.review.approval_sha256)) return true;
  const payload = {files: draft.files.map(file => ({...file})), ...(applying ? {approve_plan: draft.review!.approval_sha256} : {})};
  if (!applying) invalidate();
  const owner = draft, version = draft.version;
  draft.pending = true; draft.message = ''; context.render();
  try {
    const path = `/api/cases/${encodeURIComponent(context.caseId)}/input-import`;
    if (applying) {
      const result = await context.post<{changed: number; notice: string}>(path, payload);
      if (owner !== draft || version !== draft.version) return true;
      invalidate();
      draft.message = `Saved ${result.changed} changes from ${payload.files.length} CSV file${payload.files.length === 1 ? '' : 's'}. Continue tracing to use revised stops or hop limits. Regenerate previews or sync Miro to refresh colors; change-output layout updates use Sync and reorganize.`;
      await context.refresh();
    } else {
      const result = await context.post<Review>(path, payload);
      if (owner === draft && version === draft.version) {draft.review = result; draft.offsets = [];}
    }
  } catch (error) {
    if (owner === draft && version === draft.version) {
      invalidate(); draft.message = error instanceof Error ? error.message : 'Could not import CSV files. Preview again and retry.';
    }
  } finally {
    owner.pending = false;
    if (owner === draft) context.render();
  }
  return true;
}
