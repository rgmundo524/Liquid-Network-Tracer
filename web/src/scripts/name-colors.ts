/** Case-local name colors. Names/keys come from the server's casefold catalog. */
type Row = {key: string; name: string; variants: string[]; addresses: number; enabled_addresses: number; color: string | null};
type Catalog = {revision: number; rows: Row[]; total: number; offset: number; limit: number; presets: [string, string][]; notice: string};
type Context = {caseId: string; busy: boolean; render: () => void;
  post: <T>(path: string, body: unknown) => Promise<T>};
let state = {caseId: '', open: false, query: '', offset: 0, pending: false, data: null as Catalog | null,
  drafts: new Map<string, string>(), message: ''};
const esc = (v: unknown): string => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]!));
const safeColor = (v: unknown): v is string => typeof v === 'string' && /^#[0-9a-fA-F]{6}$/.test(v);

export function resetNameColors(caseId: string): void {
  state = {caseId, open: false, query: '', offset: 0, pending: false, data: null,
    drafts: new Map(), message: ''};
}

export function nameColorsPanel(caseId: string, busy: boolean): string {
  if (state.caseId !== caseId) resetNameColors(caseId);
  if (!state.open) return '';
  const disabled = busy || state.pending ? ' disabled' : '';
  const data = state.data;
  return `<section class="panel" id="name-colors-panel"><div class="panel-head"><h2 tabindex="-1" id="name-colors-title">Assign name colors</h2><button class="btn" data-action="name-colors-close"${disabled}>Close</button></div>
  <div class="panel-body"><p class="address-note">${esc(data?.notice || 'Loading saved names...')}</p>
  <label class="field"><span>Search names (case-insensitive)</span><input id="name-colors-query" maxlength="256" value="${esc(state.query)}"${disabled}/></label>
  <div class="form-actions"><button class="btn" data-action="name-colors-load"${disabled}>Search / refresh</button></div>
  <div class="table-wrap"><table><thead><tr><th>Name</th><th>Addresses</th><th>Color</th><th>Actions</th></tr></thead><tbody>${(data?.rows || []).map((row, i) => {
    const color = state.drafts.get(row.key) ?? row.color ?? '';
    return `<tr><td>${esc(row.name)}<small class="muted">${row.variants.length > 1 ? '<br>' + esc(row.variants.join(', ')) : ''}</small></td><td>${row.addresses}<small class="muted"><br>${row.enabled_addresses} active</small></td><td><input type="color" id="name-colors-picker-${i}" aria-label="Choose color for ${esc(row.name)}" value="${safeColor(color) ? color : '#d1d5db'}"${disabled}/>
    <input id="name-colors-hex-${i}" aria-label="Hex color for ${esc(row.name)}" placeholder="Default" maxlength="7" value="${esc(color)}"${disabled}/><br><small>${row.color ? 'Saved: ' + esc(row.color) : 'No name color assigned'}</small></td><td>
    <button class="btn" data-action="name-colors-save" data-color-index="${i}"${disabled}>Save color</button>
    <button class="btn" data-action="name-colors-clear" data-color-index="${i}"${disabled}>Clear assignment</button></td></tr>`;
  }).join('')}</tbody></table></div>
  ${data && !data.total ? '<p>No named assessments yet. Import attributions or give an address a name in Address review.</p>' : ''}
  <div class="form-actions"><span>${data?.total || 0} distinct names. Page ${Math.floor(state.offset / 100) + 1}.</span>
  <button class="btn" data-action="name-colors-prev"${disabled || state.offset === 0 ? ' disabled' : ''}>Previous</button>
  <button class="btn" data-action="name-colors-next"${disabled || !data || state.offset + 100 >= data.total ? ' disabled' : ''}>Next</button></div>
  <p class="address-note">Saving or clearing affects all addresses with that name in this investigation, including future imports. Confidence changes only the name prefix. Selected seed addresses remain red. Blank restores normal trace colors.</p>
  <p role="status">${esc(state.message)}</p></div></section>`;
}

export function nameColorsInput(element: HTMLInputElement): boolean {
  if (!element?.id?.startsWith('name-colors-')) return false;
  if (element.id === 'name-colors-query') {state.query = element.value; return true;}
  const match = /^name-colors-(picker|hex)-(\d+)$/.exec(element.id);
  if (!match) return false;
  const index = Number(match[2]), row = state.data?.rows[index];
  if (!row) return true;
  state.drafts.set(row.key, element.value);
  const companion = document.querySelector<HTMLInputElement>(`#name-colors-${match[1] === 'picker' ? 'hex' : 'picker'}-${index}`);
  if (companion && (match[1] === 'picker' || safeColor(element.value))) companion.value = element.value;
  return true;
}

export async function nameColorsAction(action: string, context: Context, element?: HTMLElement): Promise<boolean> {
  if (!action.startsWith('name-colors-')) return false;
  if (context.busy || state.pending) return true;
  if (state.caseId !== context.caseId) resetNameColors(context.caseId);
  if (action === 'name-colors-close') {state.open = false; context.render(); return true;}
  const owner = state;
  const path = `/api/cases/${encodeURIComponent(context.caseId)}/name-colors`;
  state.open = true; state.message = ''; state.pending = true; context.render();
  try {
    if (action === 'name-colors-save' || action === 'name-colors-clear') {
      const index = Number(element?.dataset.colorIndex);
      const row = Number.isInteger(index) ? owner.data?.rows[index] : undefined;
      if (!row || !owner.data) throw new Error('Refresh the name color menu and select a name.');
      const color = action === 'name-colors-clear' ? null : (owner.drafts.get(row.key) ?? row.color ?? '').trim();
      if (color && !safeColor(color)) throw new Error('Enter #RRGGBB or clear the assignment.');
      const result = await context.post<{changed: number}>(path, {updates: [{name: row.name, color}], expected_revision: owner.data.revision});
      owner.message = `Saved ${result.changed} color assignment(s). Regenerate previews or sync Miro to refresh the graph.`;
      owner.drafts.delete(row.key);
    } else if (action === 'name-colors-prev') owner.offset = Math.max(0, owner.offset - 100);
    else if (action === 'name-colors-next') owner.offset += 100;
    else owner.offset = 0;
    // An explicit refresh discards outdated unsaved color drafts.
    if (['name-colors-load', 'name-colors-open', 'name-colors-prev', 'name-colors-next'].includes(action)) owner.drafts.clear();
    const catalog = await context.post<Catalog>(path, {query: owner.query, offset: owner.offset, limit: 100});
    if (owner === state) state.data = catalog;
  } catch (error) {
    owner.message = error instanceof Error ? error.message : 'Could not save name colors. Refresh and try again.';
  } finally {
    owner.pending = false;
    if (owner === state) {context.render(); document.querySelector<HTMLElement>('#name-colors-title')?.focus();}
  }
  return true;
}
