/** Edit future starting outputs without rewriting saved evidence. */
export type SeedSelection = {seeds: string[]; revision: number};
type Context = {
  caseId: string; busy: boolean; render: () => void;
  get: <T>(path: string) => Promise<T>;
  post: <T>(path: string, body: unknown) => Promise<T>;
  saved: (selection: SeedSelection) => void;
};
type Draft = {open: boolean; pending: boolean; selection: SeedSelection | null; text: string; message: string; error: boolean};
const drafts = new Map<string, Draft>();
const esc = (value: unknown): string => String(value ?? '').replace(/[&<>"']/g,
  c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]!));
const disabled = (value: boolean): string => value ? ' disabled' : '';
function draftFor(caseId: string): Draft {
  let draft = drafts.get(caseId);
  if (!draft) {
    draft = {open: false, pending: false, selection: null, text: '', message: '', error: false};
    drafts.set(caseId, draft);
  }
  return draft;
}
export function forgetSeedEditor(caseId: string): void { drafts.delete(caseId); }
export function seedEditorPending(caseId: string): boolean { return drafts.get(caseId)?.pending ?? false; }

export function normalizeSeeds(text: string): string[] {
  const entries = text.trim().split(/[\s,]+/).filter(Boolean);
  if (!entries.length) throw new Error('Enter at least one starting output.');
  const seeds = entries.map(value => {
    const match = /^([0-9a-fA-F]{64}):([0-9]+)$/.exec(value);
    if (!match || !Number.isSafeInteger(Number(match[2])) || Number(match[2]) > 0xffffffff)
      throw new Error('Use a full 64-character transaction ID followed by : and its numeric vout, for example TRANSACTION_ID:0.');
    return `${match[1].toLowerCase()}:${Number(match[2])}`;
  });
  return [...new Set(seeds)].sort();
}

export function sameSeeds(first?: string[], second?: string[]): boolean {
  if (!Array.isArray(first) || !Array.isArray(second)) return false;
  try {
    if (!first.length || !second.length) return first.length === second.length;
    return JSON.stringify(normalizeSeeds(first.join('\n'))) === JSON.stringify(normalizeSeeds(second.join('\n')));
  } catch { return false; }
}

export function seedEditorInput(element: HTMLInputElement | HTMLTextAreaElement): boolean {
  if (element.id !== 'seed-editor-text' || !element.dataset.caseId) return false;
  const draft = drafts.get(element.dataset.caseId);
  if (draft && !draft.pending) {draft.text = element.value; draft.message = ''; draft.error = false;}
  return true;
}

export function seedEditorPanel(caseId: string, busy: boolean): string {
  const draft = drafts.get(caseId);
  if (!draft?.open) return '';
  const locked = draft.pending || busy;
  return `<section class="panel" id="seed-editor-panel" aria-labelledby="seed-editor-title"><div class="panel-head"><div><h2 id="seed-editor-title" tabindex="-1">Change starting outputs</h2><p>Choose the transaction outputs used for future collection and shared-data plots.</p></div><button type="button" class="btn" data-action="seed-editor-close">Close</button></div><div class="panel-body">
    <p>Saved runs, previews, exports, and Miro boards keep their original starting outputs. A new shared-data plot uses this selection; a plot from an older investigation snapshot keeps that snapshot’s selection.</p>
    <label class="field"><span>Starting outputs · one transaction ID:vout per line</span><textarea class="mono" id="seed-editor-text" data-case-id="${esc(caseId)}" rows="8" spellcheck="false"${disabled(locked || !draft.selection)}>${esc(draft.text)}</textarea><small>Replace, add, or remove entries. Vout indexes start at 0. Commas and spaces are also accepted. This replaces the complete selection.</small></label>
    <p class="small muted">If these outputs are in the saved shared data, select Shared collection in Plots &amp; Miro and generate another preview. Otherwise collect data for the new selection. Saving here does not download data or generate a graph.</p>
    <div class="form-actions"><button type="button" class="btn primary" data-action="seed-editor-save"${disabled(locked || !draft.selection)}>Save starting outputs</button><button type="button" class="btn" data-action="seed-editor-reload"${disabled(locked)}>Reload saved selection</button></div>
    <p role="${draft.error ? 'alert' : 'status'}">${esc(draft.pending ? 'Saving or loading starting outputs…' : draft.message)}</p></div></section>`;
}

export async function seedEditorAction(action: string, context: Context): Promise<boolean> {
  if (!action.startsWith('seed-editor-')) return false;
  const draft = draftFor(context.caseId);
  if (action === 'seed-editor-close') {draft.open = false; context.render(); return true;}
  if (context.busy || draft.pending) return true;
  if (!['seed-editor-open', 'seed-editor-reload', 'seed-editor-save'].includes(action)) return true;
  draft.open = true;
  if (action === 'seed-editor-open' && draft.selection) {context.render(); return true;}
  const path = `/api/cases/${encodeURIComponent(context.caseId)}/seeds`;
  try {
    const seeds = action === 'seed-editor-save' ? normalizeSeeds(draft.text) : null;
    if (seeds && !draft.selection) throw new Error('Load the saved starting outputs before saving.');
    draft.pending = true; draft.message = ''; draft.error = false; context.render();
    const selection = seeds ? await context.post<SeedSelection>(path, {seeds, expected_revision: draft.selection!.revision})
      : await context.get<SeedSelection>(path);
    if (drafts.get(context.caseId) !== draft) return true;
    draft.selection = selection; draft.text = selection.seeds.join('\n');
    if (seeds) {
      context.saved(selection);
      draft.message = 'Starting outputs saved. Generate a new preview from Shared collection, or collect data for this selection. Existing previews keep their original outputs.';
    }
  } catch (error) {
    if (drafts.get(context.caseId) === draft) {
      draft.message = error instanceof Error ? error.message : 'Could not save or load starting outputs.';
      draft.error = true;
    }
  } finally {
    draft.pending = false;
    if (drafts.get(context.caseId) === draft) context.render();
  }
  return true;
}
