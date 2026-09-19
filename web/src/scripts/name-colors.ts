/** Case-local role and attribution-name colors; no changes to tracing evidence. */
import {invalidateNameColorImport, nameColorImportAction, nameColorImportFile, nameColorImportInput,
  nameColorImportPanel, nameColorImportPending, resetNameColorImport} from './name-color-import';
type Row = {key: string; name: string; variants: string[]; addresses: number; enabled_addresses: number; color: string | null};
type RoleRow = {role: string; name: string; color: string | null; default_color: string};
type Catalog = {revision: number; rows: Row[]; roles?: RoleRow[]; role_notice?: string; total: number; offset: number; limit: number; presets: [string, string][]; notice: string};
type Context = {caseId: string; busy: boolean; render: () => void;
  post: <T>(path: string, body: unknown) => Promise<T>};
let state = {caseId: '', open: false, query: '', offset: 0, pending: false, data: null as Catalog | null,
  drafts: new Map<string, string>(), roleDrafts: new Map<string, string>(), message: ''};
const esc = (v: unknown): string => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]!));
const safeColor = (v: unknown): v is string => typeof v === 'string' && /^#[0-9a-fA-F]{6}$/.test(v);

export function resetNameColors(caseId: string): void {
  resetNameColorImport(caseId);
  state = {caseId, open: false, query: '', offset: 0, pending: false, data: null,
    drafts: new Map(), roleDrafts: new Map(), message: ''};
}

export function nameColorsPanel(caseId: string, busy: boolean): string {
  if (state.caseId !== caseId) resetNameColors(caseId);
  if (!state.open) return '';
  const disabled = busy || state.pending || nameColorImportPending() ? ' disabled' : '';
  const data = state.data;
  return `<section class="panel" id="name-colors-panel"><div class="panel-head"><h2 tabindex="-1" id="name-colors-title">Assign colors</h2><button class="btn" data-action="name-colors-close"${disabled}>Close</button></div>
  <div class="panel-body"><h3>Graph role colors</h3><p class="address-note">${esc(data?.role_notice || 'Loading graph-role palette...')}</p>
  <div class="table-wrap"><table><thead><tr><th>Role</th><th>Color</th><th>Actions</th></tr></thead><tbody>${(data?.roles || []).map((row, i) => {
    const color = state.roleDrafts.get(row.role) ?? row.color ?? row.default_color;
    return `<tr><td>${esc(row.name)}<small class="muted"><br>Default: ${esc(row.default_color)}</small></td><td>
    <input type="color" id="name-colors-role-picker-${i}" aria-label="Choose color for ${esc(row.name)}" value="${safeColor(color) ? color : row.default_color}"${disabled}/>
    <input id="name-colors-role-hex-${i}" aria-label="Hex color for ${esc(row.name)}" placeholder="Default" maxlength="7" value="${esc(color)}"${disabled}/><br><small>${row.color ? 'Saved: ' + esc(row.color) : 'Using default'}</small></td><td>
    <button class="btn" data-action="name-colors-role-save" data-color-index="${i}"${disabled}>Save color</button>
    <button class="btn" data-action="name-colors-role-clear" data-color-index="${i}"${disabled}>Reset to default</button></td></tr>`;
  }).join('')}</tbody></table></div>
  <div class="panel-head"><h3>Imported name colors</h3><a class="btn" href="/api/cases/${esc(encodeURIComponent(caseId))}/input-exports/name-colors" download>Export saved CSV</a></div><p class="address-note">${esc(data?.notice || 'Loading saved names...')}</p>
  <p class="address-note">Export includes all saved name color assignments across every page. Unsaved edits and graph-role colors are excluded. Empty exports contain column headers; large exports download as a ZIP of CSV parts.</p>
  ${nameColorImportPanel(caseId, busy || state.pending)}
  <label class="field"><span>Search names (case-insensitive)</span><input id="name-colors-query" maxlength="256" value="${esc(state.query)}"${disabled}/></label>
  <div class="form-actions"><button class="btn" data-action="name-colors-load"${disabled}>Search / refresh</button></div>
  <div class="table-wrap"><table><thead><tr><th>Name</th><th>Addresses</th><th>Color</th><th>Actions</th></tr></thead><tbody>${(data?.rows || []).map((row, i) => {
    const color = state.drafts.get(row.key) ?? row.color ?? '';
    return `<tr><td>${esc(row.name)}<small class="muted">${row.variants.length > 1 ? '<br>' + esc(row.variants.join(', ')) : ''}</small></td><td>${row.addresses}<small class="muted"><br>${row.enabled_addresses} active</small></td><td><input type="color" id="name-colors-picker-${i}" aria-label="Choose color for ${esc(row.name)}" value="${safeColor(color) ? color : '#d1d5db'}"${disabled}/>
    <input id="name-colors-hex-${i}" aria-label="Hex color for ${esc(row.name)}" placeholder="Default" maxlength="7" value="${esc(color)}"${disabled}/><br><small>${row.color ? 'Saved: ' + esc(row.color) : 'No name color assigned'}</small></td><td>
    <button class="btn" data-action="name-colors-save" data-color-index="${i}"${disabled}>Save color</button>
    <button class="btn" data-action="name-colors-clear" data-color-index="${i}"${disabled}>Clear assignment</button></td></tr>`;
  }).join('')}</tbody></table></div>
  ${data && !data.total ? '<p>No named assessments yet. Graph-role colors can still be assigned above.</p>' : ''}
  <div class="form-actions"><span>${data?.total || 0} distinct names. Page ${Math.floor(state.offset / 100) + 1}.</span>
  <button class="btn" data-action="name-colors-prev"${disabled || state.offset === 0 ? ' disabled' : ''}>Previous</button>
  <button class="btn" data-action="name-colors-next"${disabled || !data || state.offset + 100 >= data.total ? ' disabled' : ''}>Next</button></div>
  <p class="address-note">Name assignments also apply to future imports. Selected seed addresses retain their configured seed color. Blank restores the applicable default. Borders remain unchanged.</p>
  <p role="status">${esc(state.message)}</p></div></section>`;
}

export function nameColorsInput(element: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement): boolean {
  if (nameColorImportInput(element)) return true;
  if (!element?.id?.startsWith('name-colors-')) return false;
  if (element.id === 'name-colors-query') {state.query = element.value; return true;}
  const match = /^name-colors-(role-)?(picker|hex)-(\d+)$/.exec(element.id);
  if (!match) return false;
  const roleMode = Boolean(match[1]), index = Number(match[3]);
  const key = roleMode ? state.data?.roles?.[index]?.role : state.data?.rows[index]?.key;
  if (!key) return true;
  (roleMode ? state.roleDrafts : state.drafts).set(key, element.value);
  const prefix = roleMode ? 'name-colors-role' : 'name-colors';
  const companion = document.querySelector<HTMLInputElement>(`#${prefix}-${match[2] === 'picker' ? 'hex' : 'picker'}-${index}`);
  if (companion && (match[2] === 'picker' || safeColor(element.value))) companion.value = element.value;
  return true;
}

export async function nameColorsFile(element: HTMLInputElement, render: () => void, busy = false): Promise<void> {
  if (busy || state.pending) return;
  await nameColorImportFile(element, render);
}

export async function nameColorsAction(action: string, context: Context, element?: HTMLElement): Promise<boolean> {
  if (action.startsWith('name-color-import-')) {
    const owner = state;
    return nameColorImportAction(action, {...context, busy: context.busy || owner.pending,
      refresh: async () => {
        const catalog = await context.post<Catalog>(`/api/cases/${encodeURIComponent(context.caseId)}/name-colors`,
          {query: owner.query, offset: owner.offset, limit: 100});
        if (owner === state) {owner.data = catalog; owner.drafts.clear();}
      }});
  }
  if (!action.startsWith('name-colors-')) return false;
  if (context.busy || state.pending || nameColorImportPending()) return true;
  if (state.caseId !== context.caseId) resetNameColors(context.caseId);
  if (action === 'name-colors-close') {state.open = false; context.render(); return true;}
  const owner = state;
  const path = `/api/cases/${encodeURIComponent(context.caseId)}/name-colors`;
  state.open = true; state.message = ''; state.pending = true; context.render();
  try {
    if (['name-colors-save', 'name-colors-clear', 'name-colors-role-save', 'name-colors-role-clear'].includes(action)) {
      const index = Number(element?.dataset.colorIndex), roleMode = action.startsWith('name-colors-role-');
      const roleRow = Number.isInteger(index) ? owner.data?.roles?.[index] : undefined;
      const nameRow = Number.isInteger(index) ? owner.data?.rows[index] : undefined;
      const key = roleMode ? roleRow?.role : nameRow?.key;
      if (!key || !owner.data) throw new Error('Refresh the color menu and select a name or role.');
      const drafts = roleMode ? owner.roleDrafts : owner.drafts;
      const fallback = roleMode ? (roleRow?.color ?? roleRow?.default_color ?? '') : (nameRow?.color ?? '');
      const color = action.endsWith('-clear') ? null : (drafts.get(key) ?? fallback).trim();
      if (color && !safeColor(color)) throw new Error('Enter #RRGGBB or clear the assignment.');
      const update = roleMode ? {role: roleRow!.role, color} : {name: nameRow!.name, color};
      invalidateNameColorImport();
      const result = await context.post<{changed: number}>(path, {updates: [update], expected_revision: owner.data.revision});
      owner.message = `Saved ${result.changed} color assignment(s). Regenerate previews or sync Miro to refresh the graph.`;
      drafts.delete(key);
    } else if (action === 'name-colors-prev') owner.offset = Math.max(0, owner.offset - 100);
    else if (action === 'name-colors-next') owner.offset += 100;
    else owner.offset = 0;
    if (['name-colors-load', 'name-colors-open', 'name-colors-prev', 'name-colors-next'].includes(action)) {
      owner.drafts.clear(); owner.roleDrafts.clear();
    }
    const catalog = await context.post<Catalog>(path, {query: owner.query, offset: owner.offset, limit: 100});
    if (owner === state) state.data = catalog;
  } catch (error) {
    owner.message = error instanceof Error ? error.message : 'Could not save colors. Refresh and try again.';
  } finally {
    owner.pending = false;
    if (owner === state) {context.render(); document.querySelector<HTMLElement>('#name-colors-title')?.focus();}
  }
  return true;
}
