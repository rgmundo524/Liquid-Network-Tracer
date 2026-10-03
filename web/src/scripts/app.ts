import {beginFrameRecovery, frameRecoveryStarted, frameRecoveryComplete, frameRecoveryDialog, frameRecoveryApproval, resetFrameRecovery} from "./frame-recovery";
import {nameColorsPanel, nameColorsInput, nameColorsAction, nameColorsFile, resetNameColors, selectNameColors} from "./name-colors";
import { addressImportPanel, addressImportInput, addressImportFile, addressImportAction, resetAddressImport, selectAddressImport } from "./address-import";
import {changeOutputsPanel, changeOutputsInput, changeOutputsFile, changeOutputsAction, changeOutputsLookupComplete, changeOutputsPending, resetChangeOutputs, selectChangeOutputs} from "./change-outputs";
import {inputImportPanel, inputImportInput, inputImportFiles, inputImportAction, inputImportPending, selectInputImport} from "./input-import";
import {collectionPerformancePanel, type CollectionPerformance} from "./collection-performance";
import {createTaskNotifications} from "./task-notifications";
export {};

type ConnectorStyle = "straight" | "curved" | "elbowed";
type LayoutStyle = "standard" | "trace";
type LayoutCounts = { crossings: number; node_overlaps: number; node_intersections: number; truncated?: boolean };
type LayoutMetrics = {
  before: LayoutCounts; after: LayoutCounts; estimated: boolean;
  attempt_count?: number; attempted_count?: number; successful_count?: number; failed_count?: number;
};
type CompactionSizes = { width: number; height: number; area: number; edge_length?: number; address_distance?: number };
type CompactionReport = {
  before: { main: CompactionSizes; board: CompactionSizes };
  after: { main: CompactionSizes; board: CompactionSizes };
  moved_addresses?: number; moved_components?: number; accepted_moves?: number; skipped_moves?: number;
  truncated: boolean; unchanged: boolean;
};
type RenderingMetadata = {
  layout_algorithm?: "elk_layered_v1" | "dependency_layers_v1";
  layout_attempts?: number;
  renderer?: "direct_svg";
  fallback_reason?: "size_limit" | "timeout" | "mermaid_size_limit" | "mermaid_timeout";
};

type Settings = {
  hops: number;
  hop_reference_name: string;
  budget_limits_enabled: boolean;
  max_transactions: number;
  max_outpoints: number;
  max_requests: number;
  max_seconds: number;
  max_new_items: number;
  layout_attempts: number;
  layout_style: LayoutStyle;
  include_fees: boolean;
  color_attribution_arrows: boolean;
  group_context_inputs: boolean;
  hub_addresses: string[];
  center_name: string;
  connector_style: ConnectorStyle;
};
const layoutSettingKeys = ["layout_style", "layout_attempts", "connector_style", "include_fees", "color_attribution_arrows", "group_context_inputs", "center_name", "hub_addresses"] as const;
type LayoutSettings = Pick<Settings, typeof layoutSettingKeys[number]>;
type Run = {
  summary_pending?: boolean;
  id: string;
  seeds?: string[];
  status: string;
  stop_reason?: string;
  created_at?: string;
  transaction_count?: number;
  frontier_count?: number;
  max_hops?: number;
  collected_hops?: number;
  hop_reference_name?: string;
  performance?: CollectionPerformance;
};
type SharedCollection = {
  dataset_id?: string; name: string; compatible: boolean; reason?: string;
  seeds: string[]; seed_count: number; members: {id: string; name: string}[];
  latest_run?: string; runs: Run[]; policy_case_name?: string;
};
type CollectionSource = {kind: "shared"; dataset_id: string; run_id: string};
type Download = { name: string; url: string };
type EndpointExport = {
  filename: string; content_type: string; csv: string; endpoint_count: number;
  included: {case_id: string; name: string; run_id: string; plot_id: string; endpoint_count: number; collection_source?: CollectionSource}[];
  skipped: {case_id: string; name: string; run_id: string | null; reason: string; collection_source?: CollectionSource}[];
};
type Artifact = RenderingMetadata & {
  max_hops?: number | null; connection_count?: number; connection_status?: string; connection_scope?: ConnectionScope;
  transaction_io?: "complete"; context_edge_count?: number;
  downloads: Download[];
  preview_url?: string;
  include_fees: boolean;
  color_attribution_arrows?: boolean;
  group_context_inputs?: boolean;
  hub_addresses?: string[];
  center_name?: string;
  connector_style?: ConnectorStyle;
  layout_metrics?: LayoutMetrics;
  preview_id?: string;
  compaction?: CompactionReport;
};
type RunArtifacts = { mermaid?: Artifact; csv?: Artifact; elk?: Artifact; compact?: Artifact; connections?: Artifact };
type PegoutSearch = {
  id: string; search_id?: string; txid?: string; seeds?: string[]; min_hops: number; max_hops: number;
  status: string; stop_reason?: string | null; created_at?: string; match_count?: number;
  resumable?: boolean; recoverable?: boolean; artifact?: Artifact;
};
type JobProgress = {
  phase: string;
  completed: number;
  total: number;
  message: string;
  retry_after?: number;
  elapsed_seconds?: number;
  hop_reference_name?: string;
  stage?: string;
  wait_reason?: "memory" | "cpu" | "fifo" | "memory_retry";
  running_layouts?: number;
  waiting_layouts?: number;
  reserved_heap_mb?: number;
  available_heap_mb?: number;
  shared_api_wait_reason?: string;
  shared_api_wait_seconds?: number;
};
type PlotGoal = "full" | "connections" | "pegouts";
type PlotLayoutMode = "fresh" | "update";
type ConnectionScope = "hop_limited" | "all_saved";
type PegoutLbtcSummary = {
  lbtc: string; value_base_units: string; pegout_count: number; valued_lbtc_count: number;
  unknown_amount_count: number; unknown_asset_count: number; non_lbtc_count: number;
};
type PegoutLimitSummary = {
  schema_version: 1;
  target_lbtc: string; target_base_units: string; total_lbtc: string; total_base_units: string;
  excess_lbtc: string; excess_base_units: string; limit_reached: boolean;
  stop_reason: "limit_reached" | "paths_exhausted"; ordering: "ordinary_seed_hops_then_txid_vout";
  cutoff_seed_hops: number | null; stopping_outpoint: string | null;
  counted_pegout_count: number; unknown_amount_count: number; unknown_asset_count: number; non_lbtc_count: number;
  context_outputs_counted: false;
};
type Plot = {
  preview_number?: number;
  preview_number_notice?: string;
  validation_pending?: boolean;
  collection_source?: CollectionSource;
  preview_id: string; goal: PlotGoal; run_id: string; min_hops: number; max_hops: number | null;
  hop_reference_name?: string;
  created_at: string; status: string; node_count: number; edge_count: number; display_edge_count?: number; transaction_count: number;
  match_count?: number; connection_count?: number; source_max_hops?: number; source_run_status?: string;
  query?: {include_unspent?: boolean; include_unspendable?: boolean; include_context?: boolean; transaction_io?: "complete"; attribution_hop_limits?: "ignore"; connection_scope?: ConnectionScope; pegout_lbtc_limit?: string};
  endpoint_count?: number; endpoint_counts?: {pegout: number; unspent: number; unspendable: number};
  pegout_lbtc_summary?: PegoutLbtcSummary;
  pegout_limit_summary?: PegoutLimitSummary;
  context_edge_count?: number;
  layout_mode?: PlotLayoutMode; board_record_id?: string; board_id?: string; board_name?: string;
  update_counts?: Record<string, number>;
  source_stop_reason?: string; notice?: string; coverage_notice?: string; reviewable: boolean; reason?: string; empty?: boolean; artifact?: Artifact;
  layout_settings?: LayoutSettings & {presentation_version?: number};
  input_snapshot_version?: number;
  input_snapshot_at?: string;
};
type InvestigationBoard = {
  id: string; name: string; goal: PlotGoal; board_id: string | null; board_url: string | null; status: string;
  preview_id?: string | null; creation_preview_id?: string | null; run_id?: string | null; legacy_snapshot?: boolean; can_sync: boolean; notice?: string; pending_count?: number;
};
type CaseSection = "collection" | "workflow" | "history" | "shared" | "boards";
type SectionStatus = "unloaded" | "loading" | "ready" | "error";
type Case = {
  sections?: Partial<Record<CaseSection, SectionStatus>>;
  shared_collection?: SharedCollection;
  id: string;
  name: string;
  blockchain?: string;
  latest_run?: string;
  fixture?: string | boolean;
  miro_board?: string;
  miro_recovery?: { pending_count: number; can_confirm_empty: boolean; can_recover_frame?: boolean };
  miro_rebuild?: { status: string; previous_board_id: string; board_id?: string; run_id: string; name?: string; notice?: string };
  run_defaults: Settings;
  status?: string;
  seeds?: string[];
  seed_count?: number;
  created_at?: string;
  runs?: Run[];
  artifacts?: Record<string, RunArtifacts>;
  pegout_searches?: PegoutSearch[];
  plots?: Plot[];
  plots_next_cursor?: string | null;
  boards?: InvestigationBoard[];
  plots_notice?: string;
  boards_notice?: string;
};
type Output = {
  vout: number;
  outpoint?: string;
  selectable: boolean;
  address?: string;
  value?: number | string;
  value_text?: string;
  asset?: string;
  reason?: string;
  script_type?: string;
};
type Report = { txid: string; outputs: Output[] };
type CountReport = { known: number; total: number; remaining: number; failed: number; stop_reason?: string | null };
type Result = RenderingMetadata & {
  performance?: CollectionPerformance;
  address_counts?: CountReport;
  max_hops?: number | null; connection_count?: number; connection_status?: string; connection_scope?: ConnectionScope;
  transaction_io?: "complete"; context_edge_count?: number;
  connector_style?: ConnectorStyle;
  layout_metrics?: LayoutMetrics;
  preview_id?: string;
  compaction?: CompactionReport;
  downloads?: Download[];
  include_fees?: boolean;
  color_attribution_arrows?: boolean;
  group_context_inputs?: boolean;
  hub_addresses?: string[];
  center_name?: string;
  preview_url?: string;
  transactions?: Report[];
  run_id?: string;
  status?: string;
  new_items?: number;
  new_shapes?: number;
  new_connectors?: number;
  new_frames?: number;
  created_frames?: number;
  updated_frames?: number;
  frames_to_remove?: number;
  fee_items_to_remove?: number;
  run_notes_to_remove?: number;
  conflicts_count?: number;
  board_url?: string;
  [key: string]: unknown;
};
type MiroConflictValue = { present: boolean; value?: string; type?: string; truncated?: boolean };
type MiroEditConflictReport = {
  kind: "miro_edit_conflicts";
  board_id: string;
  items: { key: string; item_id: string; kind: string; truncated?: boolean;
    changes: { field: string; saved: MiroConflictValue; current: MiroConflictValue }[] }[];
  truncated?: boolean;
};
type JobResource = {resource_kind: "collection" | "shared_collection" | "plot" | "board" | "exclusive"; resource_key?: string | null};
type Job = {
  resource_kind?: JobResource["resource_kind"];
  resource_key?: string | null;
  source_run_id?: string | null;
  execution_state?: "starting" | "credentials_wait" | "credentials" | "working";
  id: string;
  status: "running" | "cancelling" | "canceled" | "succeeded" | "failed";
  message: string;
  result?: Result;
  action?: string;
  case_id?: string | null;
  live?: boolean;
  progress?: JobProgress;
  cancellable?: boolean;
  started_at?: number;
  finished_at?: number;
  edit_conflicts?: MiroEditConflictReport;
};
type ServiceRule = { address: string; name: string; notes: string; enabled: boolean; updated_at: string;
  confidence?: string; source?: string; observed_at?: string; stop_tracing?: boolean; hop_limit?: number | null };
type AddressActivity = {
  address: string;
  confirmed_tx_count: number | null; mempool_tx_count: number | null;
  confirmed_unspent_output_count: number | null; mempool_unspent_output_delta: number | null;
  unspent_output_count: number | null; output_counts_consistent: boolean;
  observed_at: string | null; completed_at: string | null;
  history_pages: number; max_pages: number; history_transactions_seen: number;
  history_complete: boolean; history_stop_reason: string;
  first_confirmed_activity: { txid: string; date_utc: string | null } | null;
  oldest_observed_confirmed_activity: { txid: string; date_utc: string | null } | null;
  latest_confirmed_activity: { txid: string; date_utc: string | null } | null;
  warnings: string[]; observation_ids: number[];
};
type AddressRow = { address: string; run_output_count?: number; service: ServiceRule | null; activity: AddressActivity | null };
type AddressPage = { run_id: string; rows: AddressRow[]; total: number; offset: number; limit: number };
type CaseView = "collect" | "plots" | "boards" | "history";
type Page = "dashboard" | "new" | "case" | "settings" | "case-settings" | "addresses";
type ActiveJob = {
  resource_kind?: JobResource["resource_kind"];
  resource_key?: string | null;
  source_run_id?: string | null;
  execution_state?: Job["execution_state"];
  viewRevision?: number;
  id: string;
  action: string;
  caseId?: string;
  started: number;
  finishedAt?: number;
  message: string;
  live: boolean;
  progress?: JobProgress;
  cancellable: boolean;
  cancelling: boolean;
  status: Job["status"];
  generation?: number;
  lookupTxids?: string;
  outcome?: Job;
  outcomeError?: string;
};

const defaults: Settings = {
  hops: 1,
  hop_reference_name: "",
  budget_limits_enabled: false,
  max_transactions: 20,
  max_outpoints: 100,
  max_requests: 30,
  max_seconds: 60,
  max_new_items: 750,
  layout_attempts: 25,
  layout_style: "standard",
  include_fees: false,
  color_attribution_arrows: false,
  group_context_inputs: false,
  hub_addresses: [],
  center_name: "",
  connector_style: "straight",
};
const state = {
  csrf: "",
  caseView: "collect" as CaseView,
  settings: { ...defaults },
  cases: [] as Case[],
  page: "dashboard" as Page,
  activeCase: null as Case | null,
  openCases: [] as string[],
  endpointExport: {pending: false, caseCount: 0, result: null as Omit<EndpointExport, "csv"> | null, error: ""},
  openingCase: null as {id: string; name: string; generation: number} | null,
  selectedRun: "latest",
  addressReview: {
    query: "", suspectedOnly: false, data: null as AddressPage | null,
    selected: null as AddressRow | null, loading: false,
    name: "", notes: "", enabled: false, pasted: "",
    confidence: "suspected", source: "Investigator designation", observedAt: "", stopTracing: true, hopLimit: "",
  },
  jobs: new Map<string, ActiveJob>(),
  error: "",
  editConflicts: null as { caseId: string; report: MiroEditConflictReport } | null,
  results: new Map<string, { action: string; result: Result }>(),
  artifacts: new Map<string, RunArtifacts>(),
  draft: {
    name: "",
    txids: "",
    seeds: "",
    blockchain: "liquid",
    settings: { ...defaults },
    reports: [] as Report[],
    selected: new Set<string>(),
  },
};

const app = document.querySelector<HTMLDivElement>("#app")!;
const dialog = document.querySelector<HTMLDialogElement>("#action-dialog")!;
const taskNotifications = createTaskNotifications(window);
const pollTimers = new Map<string, ReturnType<typeof setTimeout>>();
const pollingJobs = new Set<string>();
const caseRefreshSequence = new Map<string, number>();
let discoveringJobs = false;
const dismissedJobs = new Set<string>();
let tasksOpen = false;
let dialogAction = "";
let sharedDialog: {caseId: string; mode: "collect" | "continue"; runId?: string} | null = null;
let dialogMergeApproval = "";
let dialogPreviewId = "";
let dialogRebuild: { caseId: string; sourceBoard: string; runId: string; budgetLimitsEnabled: boolean } | null = null;
let submitting = false;
let pageGeneration = 0;
let viewRevision = 0;
let workflowDraft = {caseId: "", dataSource: "investigation" as "investigation" | "shared", sharedRun: "", datasetId: "", goal: "full" as PlotGoal, minHops: "0", maxHops: "10", connectionScope: "hop_limited" as ConnectionScope, connectionMaxHops: "10", pegoutLimitEnabled: false, pegoutLbtcLimit: "", includeUnspent: false, includeUnspendable: false, layoutMode: "fresh" as PlotLayoutMode, layoutBoard: "", plot: "", board: "", boardPlot: "", boardGoal: "full" as PlotGoal, boardName: "", savedBoardName: "", linkedBoardName: "", boardUrl: ""};
let pegoutDraft = {caseId: "", custom: false, txid: "", minHops: "0", maxHops: "10", selected: "", board: "", approved: ""};
const pegoutDrafts = new Map<string, typeof pegoutDraft>();
type InvestigationView = {
  page: "case" | "case-settings" | "addresses"; caseView: CaseView; selectedRun: string;
  addressReview?: typeof state.addressReview; scrollY?: number; selectedPreview?: string;
};
const investigationViews = new Map<string, InvestigationView>();
const importedInputEditors = new Set<string>();
const investigationTabsKey = "liquid-tracer:investigation-tabs:v1";

function invalidateAddressReview(caseId: string): void {
  const remembered = investigationViews.get(caseId)?.addressReview;
  if (remembered) {remembered.selected = null; remembered.data = null;}
  if (state.activeCase?.id === caseId) {
    state.addressReview.selected = null; state.addressReview.data = null;
  }
}

function resetImportedInputEditors(caseId: string): void {
  resetAddressImport(caseId); resetNameColors(caseId); resetChangeOutputs(caseId);
  importedInputEditors.delete(caseId);
}

function invalidateImportedInputs(caseId: string): void {
  invalidateAddressReview(caseId);
  importedInputEditors.add(caseId);
  if (state.activeCase?.id === caseId) resetImportedInputEditors(caseId);
}

function persistInvestigationTabs(): void {
  try {
    // Only navigation is persisted. Evidence and unsaved form contents stay in memory.
    window.sessionStorage.setItem(investigationTabsKey, JSON.stringify({ids: state.openCases,
      views: state.openCases.map(id => {
        const view = viewingCase(id) ? {page: state.page, caseView: state.caseView, selectedRun: currentRun()?.id || state.selectedRun, selectedPreview: currentWorkflow(state.activeCase!).plot} : investigationViews.get(id);
        return {id, page: view?.page, caseView: view?.caseView, selectedRun: view?.selectedRun, selectedPreview: view?.selectedPreview};
      })}));
  } catch { /* Tabs still work when browser storage is unavailable. */ }
}

function restoreInvestigationTabs(): void {
  try {
    const saved = JSON.parse(window.sessionStorage.getItem(investigationTabsKey) || "null");
    if (!saved || !Array.isArray(saved.ids)) return;
    const available = new Set(state.cases.map(item => item.id));
    state.openCases = [...new Set<string>(saved.ids.filter((id: unknown): id is string => typeof id === "string" && available.has(id)))];
    if (Array.isArray(saved.views)) for (const view of saved.views) {
      if (!view || !state.openCases.includes(view.id)) continue;
      investigationViews.set(view.id, {
        page: ["case", "case-settings", "addresses"].includes(view.page) ? view.page : "case",
        caseView: ["collect", "plots", "history"].includes(view.caseView) ? view.caseView : "collect",
        selectedRun: typeof view.selectedRun === "string" ? view.selectedRun : "latest",
        selectedPreview: typeof view.selectedPreview === "string" && /^[A-Za-z0-9_-]{1,160}$/.test(view.selectedPreview) ? view.selectedPreview : undefined,
      });
    }
  } catch { /* Ignore unavailable or outdated session data. */ }
}

function addOpenInvestigation(id: string): void {
  if (!state.openCases.includes(id)) state.openCases.push(id);
  persistInvestigationTabs();
}

function rememberInvestigationView(): void {
  const id = state.activeCase?.id;
  if (!id || !viewingCase(id) || !state.openCases.includes(id)) return;
  savePlotLayoutDraft(); saveSettingsDraft(); saveAddressDraft();
  investigationViews.set(id, {page: state.page as InvestigationView["page"], caseView: state.caseView,
    selectedRun: currentRun()?.id || state.selectedRun, selectedPreview: currentWorkflow(state.activeCase!).plot,
    addressReview: {...state.addressReview, loading: false}, scrollY: window.scrollY || 0});
  persistInvestigationTabs();
}

function investigationTabs(): string {
  if (!state.openCases.length) return "";
  return `<nav class="investigation-tabs" aria-label="Open investigations"><div class="investigation-tab-list">${state.openCases.map(id => {
    const name = state.cases.find(item => item.id === id)?.name || (state.activeCase?.id === id ? state.activeCase.name : id);
    const active = viewingCase(id), count = runningJobs().filter(job => job.caseId === id).length;
    return `<div class="investigation-tab${active ? " active" : ""}"><button class="investigation-tab-open" data-case="${esc(id)}" title="${esc(name)}"${active ? ' aria-current="page"' : ""}${disabled(isCaseOpening(id))}><span class="investigation-tab-name">${esc(name)}</span><span class="investigation-tab-status"${count ? ' role="status"' : ""}>${count ? `${count} running` : ""}</span></button><button class="investigation-tab-close" data-action="close-investigation-tab" data-id="${esc(id)}" aria-label="Close ${esc(name)} tab" title="Close tab; running tasks continue">${icon("close")}</button></div>`;
  }).join("")}</div><button class="btn small investigation-export" data-action="export-open-endpoints" title="Export the latest saved endpoint trace for each open investigation’s selected snapshot"${disabled(state.endpointExport.pending)}>${icon("download")}<span>${state.endpointExport.pending ? "Exporting…" : "Export endpoints"}</span></button><button class="investigation-tab-add" data-page="new" aria-label="New investigation" title="New investigation">${icon("plus")}</button></nav>`;
}

function endpointExportNotice(): string {
  const {pending, caseCount, result, error} = state.endpointExport;
  if (!pending && !result && !error) return "";
  const title = pending ? "Preparing endpoint CSV"
    : error ? "Endpoint export could not be completed"
    : result!.included.length ? `Exported ${result!.endpoint_count} endpoint row${result!.endpoint_count === 1 ? "" : "s"} from ${result!.included.length} investigation${result!.included.length === 1 ? "" : "s"}`
    : "No saved endpoint traces to export";
  return `<div class="endpoint-export-notice${error ? " error" : ""}" role="status"><div><strong>${esc(title)}</strong><p>${esc(pending ? `Reading saved traces for ${caseCount} open investigation${caseCount === 1 ? "" : "s"}…` : error || (result!.included.length ? "Shared endpoints remain separate rows for each investigation." : "Generate a Paths to peg-outs plot for the selected snapshots, then export again."))}</p>${result ? `<details${result.skipped.length ? " open" : ""}><summary>Included ${result.included.length} · Skipped ${result.skipped.length}</summary><ul>${result.included.map(item => `<li>${esc(item.name)}: ${item.endpoint_count} endpoint row${item.endpoint_count === 1 ? "" : "s"} · ${item.collection_source?.kind === "shared" ? `shared snapshot ${esc(item.collection_source.run_id)}` : `run ${esc(item.run_id)}`}</li>`).join("")}${result.skipped.map(item => `<li>${esc(item.name)}: skipped. ${esc(item.reason)}</li>`).join("")}</ul></details>` : ""}</div>${!pending ? `<button class="dismiss" data-action="dismiss-endpoint-export" aria-label="Dismiss endpoint export status">${icon("close")}</button>` : ""}</div>`;
}

async function exportOpenEndpoints(): Promise<void> {
  if (state.endpointExport.pending || !state.openCases.length) return;
  rememberInvestigationView(); saveDraft();
  const investigations = state.openCases.map(case_id => {
    const draft = workflowDraft.caseId === case_id ? workflowDraft : workflowDrafts.get(case_id);
    if (draft?.dataSource === "shared") return {case_id, data_source: "shared", dataset_id: draft.datasetId, run_id: draft.sharedRun};
    return {case_id, run_id: viewingCase(case_id) ? currentRun()?.id || state.selectedRun
      : investigationViews.get(case_id)?.selectedRun || "latest"};
  });
  state.endpointExport = {pending: true, caseCount: investigations.length, result: null, error: ""};
  render();
  try {
    const product = await api<EndpointExport>("/api/endpoint-exports", {investigations});
    if (product.included.length) {
      const url = URL.createObjectURL(new Blob([product.csv], {type: product.content_type}));
      const link = document.createElement("a");
      link.href = url; link.download = product.filename; link.hidden = true;
      document.body.append(link);
      try {link.click();} finally {link.remove(); setTimeout(() => URL.revokeObjectURL(url), 30_000);}
    }
    const {csv: _csv, ...summary} = product;
    state.endpointExport.result = summary;
  } catch (error) {
    state.endpointExport.error = error instanceof Error ? error.message : "Could not export saved endpoints.";
  } finally {
    state.endpointExport.pending = false;
    render();
  }
}

async function closeInvestigationTab(id: string): Promise<void> {
  const index = state.openCases.indexOf(id);
  if (index < 0) return;
  const active = viewingCase(id);
  rememberInvestigationView();
  state.openCases.splice(index, 1);
  investigationViews.delete(id);
  persistInvestigationTabs();
  if (state.openingCase?.id === id) cancelCaseOpening();
  if (active) {
    const next = state.openCases[Math.min(index, state.openCases.length - 1)];
    navigate("dashboard");
    if (next) await openCase(next);
  } else render();
}

const esc = (value: unknown): string =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        char
      ]!,
  );
const short = (value: string | undefined, length = 12): string =>
  value
    ? value.length > length + 5
      ? `${value.slice(0, length)}…${value.slice(-5)}`
      : value
    : "—";
const human = (value: unknown): string =>
  String(value ?? "—").replaceAll("_", " ");
const runningJobs = (): ActiveJob[] => [...state.jobs.values()].filter(job => job.status === "running" || job.status === "cancelling");
const viewingCase = (caseId?: string): boolean => !!caseId && state.activeCase?.id === caseId && ["case", "case-settings", "addresses"].includes(state.page);
const scopeBusy = (caseId?: string): boolean => runningJobs().some(job => job.caseId === caseId);
const investigationEditorBusy = (): boolean => viewingCase(state.activeCase?.id) && (changeOutputsPending() || inputImportPending());
const isBusy = (): boolean => submitting || investigationEditorBusy() ||
  (state.page === "new" ? scopeBusy() : viewingCase(state.activeCase?.id) ? scopeBusy(state.activeCase!.id) : false);
const draftBusy = (): boolean => submitting || investigationEditorBusy();
function requestedResource(action: string, body: Record<string, unknown> = {}, caseId: string | null | undefined = state.activeCase?.id): JobResource {
  if (action === "shared-trace") return {resource_kind: "shared_collection", resource_key: "workspace-shared"};
  if (["trace", "address-counts"].includes(action)) return {resource_kind: "collection"};
  if (action === "plot" && body.layout_mode !== "update") return {resource_kind: "plot"};
  if (["plot", "plot-sync", "board-sync", "board-create", "board-create-sync"].includes(action)) {
    const recordId = String(body.board_record_id || body.record_id || "");
    const detail = state.activeCase?.id === caseId ? state.activeCase : undefined;
    let board = detail?.boards?.find(item => item.id === recordId);
    if (["board-sync", "board-create-sync"].includes(action)) {
      const preview = detail?.plots?.find(item => item.preview_id === body.preview_id) ||
        (detail && board && !body.preview_id ? chosenBoardPlot(detail, board) : undefined);
      if (preview?.input_snapshot_version !== 1) return {resource_kind: "exclusive"};
      if (!board && action === "board-create-sync") board = detail?.boards?.find(item =>
        item.creation_preview_id === preview.preview_id && item.status !== "creation_rejected");
    }
    return {resource_kind: "board", resource_key: board?.board_id || (recordId ? `record:${recordId}` : "new-board")};
  }
  return {resource_kind: "exclusive"};
}
function actionBusy(action: string, body: Record<string, unknown> = {}, caseId: string | null | undefined = state.activeCase?.id): boolean {
  if (draftBusy()) return true;
  const requested = requestedResource(action, body, caseId);
  return runningJobs().some(job => {
    const existing = job.resource_kind ? job as JobResource : requestedResource(job.action, {}, job.caseId ?? null);
    if (requested.resource_kind === "shared_collection" || existing.resource_kind === "shared_collection")
      return requested.resource_kind === "shared_collection" && existing.resource_kind === "shared_collection";
    if ((job.caseId ?? null) !== (caseId ?? null)) return requested.resource_kind === "board" && existing.resource_kind === "board"
      && requested.resource_key !== "new-board" && requested.resource_key === existing.resource_key;
    if (requested.resource_kind === "exclusive" || existing.resource_kind === "exclusive") return true;
    if (requested.resource_kind === "collection") return existing.resource_kind === "collection";
    if (requested.resource_kind === "board" && existing.resource_kind === "board") {
      // Older servers do not report a board identity. Keep their writes exclusive.
      return !job.resource_kind || !existing.resource_key || requested.resource_key === existing.resource_key;
    }
    return false;
  });
}
const workflowResourceBody = (detail: Case): Record<string, unknown> => {
  const draft = currentWorkflow(detail);
  return {layout_mode: draft.layoutMode, board_record_id: draft.layoutMode === "update" ? draft.layoutBoard : undefined};
};
const canApplyJobView = (job: ActiveJob): boolean => job.generation === pageGeneration &&
  (job.viewRevision === undefined || job.viewRevision === viewRevision) && viewingCase(job.caseId);
const disabled = (condition: boolean): string => (condition ? " disabled" : "");
const isCaseOpening = (id: string): boolean =>
  state.openingCase?.generation === pageGeneration && state.openingCase.id === id;
const outputValue = (output: Output): string =>
  typeof output.value_text === "string"
    ? output.value_text
    : typeof output.value === "number" && Number.isSafeInteger(output.value)
      ? String(output.value)
      : "??";
const liquidBitcoinAsset =
  "6f0279e9ed041c3d710a9f57d0c02928416460c4b722ae3457a11eec381c526d";
const outputAsset = (asset?: string): string =>
  asset === liquidBitcoinAsset ? "L-BTC" : asset ? short(asset, 10) : "??";
const formatDate = (value?: string): string => {
  if (!value) return "Saved locally";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? "Saved locally"
    : `${parsed.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" })} · ${parsed.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", timeZone: "UTC", hour12: false })} UTC`;
};

function icon(name: string): string {
  const paths: Record<string, string> = {
    liquid:
      '<path d="m12 2 7 12a8 8 0 1 1-14 0L12 2Z"/><path d="M8 16c0 2 1.5 3.5 3.5 3.5"/>',
    grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    folder:
      '<path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    settings:
      '<path d="M12 3v3m0 12v3M3 12h3m12 0h3M5.6 5.6l2.1 2.1m8.6 8.6 2.1 2.1M5.6 18.4l2.1-2.1m8.6-8.6 2.1-2.1"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
    arrow: '<path d="M4 12h15m-5-5 5 5-5 5"/>',
    chevron: '<path d="m9 5 7 7-7 7"/>',
    link: '<path d="m10 13 4-4m-5 7-2 2a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m2 0 2-2a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0"/>',
    graph:
      '<circle cx="5" cy="6" r="2"/><circle cx="5" cy="18" r="2"/><rect x="16" y="9" width="5" height="6" rx="1"/><path d="m7 7 9 4M7 17l9-4"/>',
    layers: '<path d="m12 3 10 5-10 5L2 8Zm-9 10 9 5 9-5M3 18l9 5 9-5"/>',
    lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V6a4 4 0 0 1 8 0v4m-4 5v2"/>',
    shield:
      '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8 12 3 3 5-6"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    play: '<path d="m8 4 12 8-12 8Z"/>',
    download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
    external:
      '<path d="M14 3h7v7m0-7L10 14M11 5H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-6"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v.1"/>',
    close: '<path d="m6 6 12 12M6 18 18 6"/>',
    table:
      '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18"/>',
    board:
      '<rect x="3" y="4" width="18" height="14" rx="2"/><path d="M12 18v4m-4 0h8M7 9h3m4 4h3m-7-4 4 4"/>',
    refresh:
      '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 7a7 7 0 0 1 12-2l2 3M4 16l2 3a7 7 0 0 0 12-2"/>',
    search: '<circle cx="10" cy="10" r="7"/><path d="m15 15 6 6"/>',
  };
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] ?? paths.graph}</svg>`;
}

function button(
  label: string,
  action: string,
  symbol = "",
  style = "",
  off = false,
  attrs = "",
): string {
  return `<button type="button" class="btn ${style}" data-action="${action}"${disabled(off)} ${attrs}>${symbol ? icon(symbol) : ""}${label}</button>`;
}

function toast(message: string, error = false): void {
  const element = document.createElement("button");
  element.type = "button";
  element.className = `toast${error ? " error" : ""}`;
  element.textContent = message;
  element.title = "Dismiss notification";
  element.setAttribute("aria-label", `Dismiss notification: ${message}`);
  element.onclick = () => element.remove();
  document.querySelector("#notifications")!.append(element);
  setTimeout(() => element.remove(), error ? 9000 : 5500);
}

function notificationControl(): string {
  const control = taskNotifications.control();
  return `<button type="button" class="notification-control" data-action="toggle-system-notifications" aria-pressed="${control.enabled}" title="${esc(control.hint)}"${disabled(control.disabled)}>${esc(control.label)}</button>`;
}

function notifyTaskResult(job: ActiveJob): void {
  if (!["succeeded", "failed", "canceled"].includes(job.status)) return;
  taskNotifications.notify({id: job.id, status: job.status as "succeeded" | "failed" | "canceled",
    investigation: taskName(job), action: human(job.action), open: () => {
      tasksOpen = true;
      if (job.caseId) void openCase(job.caseId).catch(handleError);
      else render();
    }});
}

class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

async function api<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers:
      body === undefined
        ? {}
        : { "Content-Type": "application/json", "X-Liquid-CSRF": state.csrf },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data: { error?: string; message?: string };
  try {
    data = await response.json();
  } catch {
    throw new Error(
      "The local server returned an unreadable response. Check its terminal.",
    );
  }
  if (!response.ok)
    throw new ApiError(
      data.error ||
        data.message ||
        `The local request failed (${response.status}).`,
      response.status,
    );
  return data as T;
}

function connectorOptions(style: ConnectorStyle): string {
  return (["straight", "curved", "elbowed"] as const)
    .map((value) => `<option value="${value}"${style === value ? " selected" : ""}>${value[0].toUpperCase() + value.slice(1)}</option>`)
    .join("");
}

function numericField(settings: Settings, key: keyof Settings, label: string, hint: string): string {
  return `<label class="field"><span>${label}</span><input name="${key}" type="number" min="${key === "layout_attempts" ? "1" : "0"}"${key === "layout_attempts" ? ' max="1000"' : ""} step="${key === "max_seconds" ? "any" : "1"}" required value="${esc(settings[key])}"/><small>${hint}</small></label>`;
}

function budgetFields(settings: Settings): string {
  return `${numericField(settings, "hops", "Additional hops", "Default for each run; 0 retries the current frontier.")}<input type="hidden" name="budget_limits_enabled_present" value="1"/><label class="check-line"><input id="budget-limits-enabled" name="budget_limits_enabled" type="checkbox"${settings.budget_limits_enabled ? " checked" : ""}/><span><strong>Use optional run budgets</strong><small>Limit transaction collection and new Miro items. Hop limits and explicit stop rules apply whether budgets are on or off.</small></span></label>${settings.budget_limits_enabled ? `<div class="budget-grid">${([
    ["max_transactions", "Transactions", "Maximum new transactions per run. 0 = unlimited."],
    ["max_outpoints", "Output lookups", "Maximum per run. 0 = unlimited."],
    ["max_requests", "API attempts", "Includes retries. 0 = unlimited."],
    ["max_seconds", "Time limit (seconds)", "Trace request budget. 0 = unlimited."],
  ] as [keyof Settings, string, string][]).map(([key, label, hint]) => numericField(settings, key, label, hint)).join("")}</div>` : traceSummary(settings)}`;
}

function graphFields(settings: Settings, suggest = false, goal: PlotGoal = "full"): string {
  const check = (key: "include_fees" | "color_attribution_arrows" | "group_context_inputs", label: string, hint: string): string =>
    `<input type="hidden" name="${key}_present" value="1"/><label class="check-line"><input name="${key}" type="checkbox"${settings[key] ? " checked" : ""}/><span><strong>${label}</strong><small>${hint}</small></span></label>`;
  return `<label class="field"><span>Layout style</span><select name="layout_style"><option value="standard"${settings.layout_style === "trace" ? "" : " selected"}>Standard</option><option value="trace"${settings.layout_style === "trace" ? " selected" : ""}>Trace layout</option></select><small>For new plot generations, Trace layout favors a straight central tracing path, nearby endpoints, and shorter branches. Choosing it also groups isolated context inputs; you can adjust that option below. Center named group chooses the path to emphasize. Board updates preserve existing positions.</small></label><div class="field-row"><label class="field"><span>Connector appearance</span><select name="connector_style">${connectorOptions(settings.connector_style)}</select></label>${numericField(settings, "layout_attempts", "Layout attempts", "More attempts compare more arrangements and take longer.")}</div>
    ${check("color_attribution_arrows", "Color arrows by attribution", "Use each named address's assigned color for its arrows.")}
    ${centerNameFields(settings, suggest)}
    <fieldset class="layout-fields context-grouping-fields"><legend>Context input grouping</legend>
    ${check("group_context_inputs", "Group isolated context inputs", "Combine isolated external input addresses and bundle repeated context inputs from the same address to the same transaction. Shared or named addresses stay visible, and traced inputs keep their own arrows. Original inputs remain in details and CSV exports. Changing this grouping replaces generated context objects or connectors; preserve any Miro comments on them first.")}</fieldset>
    <fieldset class="layout-fields fee-flow-fields"${disabled(goal === "connections")}><legend>Transaction fees</legend>
    ${check("include_fees", "Include transaction fee flows", "Show fees above the graph.")}</fieldset>
    <fieldset class="layout-fields full-trace-fields"${disabled(goal === "connections")}><legend>Branch hubs</legend>
    ${hubAddressFields(settings)}</fieldset>${goal !== "full" ? `<p class="small muted">${goal === "pegouts" ? "Peg-out layouts show every transaction input and non-fee output. The fee option also includes transaction fee flows." : "Starter connection layouts always show every transaction input and output, including fees."} Separate branch hubs apply to Full trace and Paths to peg-outs.</p>` : ""}`;
}

function traceSummary(settings: Settings): string {
  if (!settings.budget_limits_enabled)
    return '<p class="settings-summary">No transaction, output lookup, API request, time, or new Miro item cap. Hop limits and explicit stop rules still apply.</p>';
  const limit = (value: number, suffix = "") => value > 0 ? `${esc(value)}${suffix}` : "Unlimited";
  return `<p class="settings-summary">New transactions: <strong>${limit(settings.max_transactions)}</strong> · Output lookups: <strong>${limit(settings.max_outpoints)}</strong> · API attempts: <strong>${limit(settings.max_requests)}</strong> · Time: <strong>${limit(settings.max_seconds, "s")}</strong> · New Miro items: <strong>${limit(settings.max_new_items)}</strong>. Hop limits and explicit stop rules still apply.</p>`;
}

function hopBasis(name?: string): string {
  return name ? `Hops from named group: ${name}` : "Hops from starting transactions";
}

function hopBasisExplanation(name?: string): string {
  return name ? `Outputs to ${name} reset to hop 0; each transfer outside the group adds a hop. Branches count separately.`
    : "Starting transactions are hop 0; individual branches may stop earlier.";
}

function hopReferenceFields(settings: Settings, shared = false): string {
  return `<label class="field"><span>Count hops from named group</span><input name="hop_reference_name" maxlength="120" value="${esc(settings.hop_reference_name)}" placeholder="Blank: count from starting transactions" autocomplete="off" list="hop-reference-name-options"/><datalist id="hop-reference-name-options"></datalist><small>Optional attribution name, ignoring capitalization. Keeps the same starting outputs. Outputs to this group reset to hop 0; transfers outside it add a hop, separately for each branch. At the hop limit, the next spending transaction is checked for a return to the group. This may use more requests; explicit tracing stops and any enabled run budgets still apply. Collection ignores attribution CSV hop_limit values.</small><small>${shared ? "Captured for this shared collection; private collection preferences remain unchanged." : "Saved for this investigation when collection starts."} Changing the group recalculates saved paths and resumes eligible branches. Previous run snapshots stay unchanged. Center named group is a separate layout setting.</small></label>`;
}

let hopReferenceNameTimer: ReturnType<typeof setTimeout> | undefined;
async function suggestHopReferenceNames(): Promise<void> {
  const detail = state.activeCase;
  const selector = '#action-form input[name="hop_reference_name"]';
  const input = document.querySelector<HTMLInputElement>(selector);
  if (!detail || !dialog.open || !["trace", "shared-trace"].includes(dialogAction) || !input) return;
  const query = input.value.trim(), generation = pageGeneration;
  const result = await api<{rows?: {name: string; enabled_addresses: number}[]}>(`/api/cases/${encodeURIComponent(detail.id)}/name-colors`,
    {query, offset: 0, limit: 100});
  if (generation !== pageGeneration || state.activeCase?.id !== detail.id || !dialog.open || !["trace", "shared-trace"].includes(dialogAction)
      || input.value.trim() !== query || document.querySelector(selector) !== input) return;
  const list = document.querySelector<HTMLDataListElement>("#hop-reference-name-options");
  if (list) list.innerHTML = (result.rows || []).filter(row => row.enabled_addresses > 0)
    .map(row => `<option value="${esc(row.name)}"></option>`).join("");
}

function hubAddressFields(settings: Settings): string {
  return `<div class="settings-divider"></div><label class="field"><span>Separate branch hubs</span><textarea name="hub_addresses" class="mono" rows="4" spellcheck="false" placeholder="One full Liquid address per line">${esc(settings.hub_addresses.join("\n"))}</textarea><small>Group branches at high-activity or shared addresses. Trace layout places a hub after its first entry from the starting flow, with later returns looping back. Spending transactions line up vertically when other inputs allow; their outputs branch to the right. Each address keeps one identity and all connections. Tracing stays the same.</small></label>`;
}

function centerNameFields(settings: Settings, suggest = false): string {
  return `<div class="settings-divider"></div><label class="field"><span>Center named group</span><input name="center_name" maxlength="120" value="${esc(settings.center_name)}" placeholder="Attribution name, or blank to disable" autocomplete="off"${suggest ? ' list="center-name-options"' : ""}/>${suggest ? '<datalist id="center-name-options"></datalist>' : ""}<small>Align this group's addresses and connecting transactions near the center, with other activity branching around them. Enter an attribution name; matching ignores capitalization. Leave blank to disable. Layout only: tracing and all connections stay the same. New-board layouts apply this to the full graph. Board updates apply it to newly added objects while preserving existing positions.</small></label>`;
}

let centerNameTimer: ReturnType<typeof setTimeout> | undefined;
async function suggestCenterNames(): Promise<void> {
  const detail = state.activeCase;
  const input = document.querySelector<HTMLInputElement>('#plot-layout-form input[name="center_name"]');
  if (!detail || state.page !== "case" || state.caseView !== "plots" || !input) return;
  const query = input.value.trim(), generation = pageGeneration;
  const result = await api<{rows?: {name: string; enabled_addresses: number}[]}>(`/api/cases/${encodeURIComponent(detail.id)}/name-colors`,
    {query, offset: 0, limit: 100});
  if (generation !== pageGeneration || state.activeCase?.id !== detail.id || input.value.trim() !== query
      || document.querySelector('#plot-layout-form input[name="center_name"]') !== input) return;
  const list = document.querySelector<HTMLDataListElement>("#center-name-options");
  if (list) list.innerHTML = (result.rows || []).filter(row => row.enabled_addresses > 0)
    .map(row => `<option value="${esc(row.name)}"></option>`).join("");
}

function normalizedHubs(addresses: string[] | undefined): string[] {
  return [...new Set((addresses || []).map(address => address.trim()).filter(Boolean))].sort();
}

function readSettings(form: HTMLFormElement, previous: Settings = defaults): Settings {
  const data = new FormData(form);
  const number = (key: keyof Settings): number => data.has(key) ? Number(data.get(key)) : Number(previous[key]);
  const flag = (key: "include_fees" | "color_attribution_arrows" | "group_context_inputs" | "budget_limits_enabled"): boolean =>
    data.has(`${key}_present`) || data.has(key) ? data.has(key) : Boolean(previous[key]);
  return {
    budget_limits_enabled: flag("budget_limits_enabled"),
    hops: number("hops"), max_transactions: number("max_transactions"), max_outpoints: number("max_outpoints"),
    hop_reference_name: data.has("hop_reference_name") ? String(data.get("hop_reference_name") || "").trim() : (previous.hop_reference_name || ""),
    max_requests: number("max_requests"), max_seconds: number("max_seconds"), max_new_items: number("max_new_items"),
    layout_attempts: number("layout_attempts"), include_fees: flag("include_fees"),
    layout_style: (data.get("layout_style") || previous.layout_style || "standard") as LayoutStyle,
    color_attribution_arrows: flag("color_attribution_arrows"), group_context_inputs: flag("group_context_inputs"),
    hub_addresses: data.has("hub_addresses") ? normalizedHubs(String(data.get("hub_addresses") || "").split(/\r?\n/)) : [...previous.hub_addresses],
    center_name: data.has("center_name") ? String(data.get("center_name") || "").trim() : (previous.center_name || ""),
    connector_style: (data.get("connector_style") || previous.connector_style) as ConnectorStyle,
  };
}

function currentRun(): Run | undefined {
  const detail = state.activeCase;
  return detail?.runs?.find(
    (run) =>
      run.id ===
      (state.selectedRun === "latest" ? detail.latest_run : state.selectedRun),
  );
}

function resultKey(caseId: string, runId = state.selectedRun): string {
  return `${caseId}:${runId === "latest" ? (state.activeCase?.latest_run ?? "latest") : runId}`;
}

function sidebar(): string {
  return `<aside class="sidebar"><div class="brand"><span class="brand-mark">${icon("liquid")}</span><div><strong>Liquid Tracer</strong><small>Investigation workspace</small></div></div><div class="nav-label">Workspace</div><nav aria-label="Main navigation">${[
    ["dashboard", "Investigations", "grid"],
    ["new", "New investigation", "plus"],
    ["settings", "Workspace defaults", "settings"],
  ]
    .map(
      ([page, label, symbol]) =>
        `<button class="nav-button${state.page === page || (page === "dashboard" && ["case", "case-settings", "addresses"].includes(state.page)) ? " active" : ""}" data-page="${page}"${state.page === page ? ' aria-current="page"' : ""}>${icon(symbol)}<span>${label}</span></button>`,
    )
    .join(
      "",
    )}</nav><div class="sidebar-rule"></div><div class="sidebar-recent"><div class="nav-label">Recent investigations</div>${
    state.cases
      .slice(0, 5)
      .map(
        (item) =>
          `<button class="recent-case" data-case="${esc(item.id)}"${disabled(isCaseOpening(item.id))}><span class="case-dot"></span><span>${esc(item.name)}</span></button>`,
      )
      .join("") ||
    '<p class="small" style="padding:0 13px;color:#7293a1">Your saved cases will appear here.</p>'
  }</div><div class="sidebar-bottom"><div class="local-status"><span class="status-dot"></span><div><strong>Running on your computer</strong><p>Local files · Same tracing engine</p></div></div><div class="sidebar-foot">LIQUID NETWORK · UTXO TRACING<br/><span style="display:block;margin-top:7px;letter-spacing:0">Arrows between buttons · Enter to select</span></div></div></aside>`;
}

const collectionHopPhases: Record<string, string> = {
  collecting: "Processing",
  collection_complete: "Collection finished; last processing",
  collection_paused: "Collection paused while processing",
  collection_error: "Collection stopped while processing",
};

function collectionHopLabel(progress: JobProgress): string | undefined {
  const prefix = Object.hasOwn(collectionHopPhases, progress.phase) ? collectionHopPhases[progress.phase] : undefined;
  if (!prefix || !Number.isSafeInteger(progress.completed) || !Number.isSafeInteger(progress.total)
      || progress.completed < 0 || progress.total < 0 || progress.completed > progress.total) return;
  return `${prefix} ${progress.hop_reference_name ? "group-relative " : ""}hop ${progress.completed} of ${progress.total}`;
}

function jobProgress(job: ActiveJob): string {
  const progress = job.progress;
  if (!progress) return `<progress class="job-progress-bar" aria-label="${job.action === "load-shared-collection" ? "Loading shared collection" : "Calculation in progress"}"></progress>`;
  const hopLabel = collectionHopLabel(progress);
  if (hopLabel) {
    const zeroHop = progress.total === 0;
    const value = zeroHop ? (progress.phase === "collection_complete" ? 1 : 0) : progress.completed;
    return `<div class="job-progress-meta"><span>Hop progress</span><span>${progress.hop_reference_name ? "Group-relative hop" : "Hop"} ${progress.completed} / ${progress.total}</span></div><progress class="job-progress-bar" max="${zeroHop ? 1 : progress.total}" value="${value}" aria-label="${esc(hopLabel)}" aria-valuetext="${esc(hopLabel)}"></progress><p class="small muted">${progress.hop_reference_name ? `Counted from ${esc(progress.hop_reference_name)}; returns to the group reset to hop 0. ` : zeroHop ? "Starting transactions only (hop 0). " : ""}Hop depth, not a time estimate.</p>`;
  }
  if (progress.stage === "resource_wait") {
    const measured = [progress.running_layouts, progress.waiting_layouts].every(value => Number.isSafeInteger(value) && value! >= 0);
    return `<div class="job-progress-meta"><span>${esc(taskActivity(job).label)}</span>${measured ? `<span>${progress.running_layouts} calculating · ${progress.waiting_layouts} waiting</span>` : ""}</div><p class="small muted">This layout has not started its next calculation. ${Number.isFinite(progress.reserved_heap_mb) ? `${Number(progress.reserved_heap_mb).toLocaleString()} MiB reserved across layouts. ` : ""}A reservation is an allowance, not measured RAM use.</p>`;
  }
  const total = Number.isFinite(progress.total) ? Math.max(0, progress.total) : 0;
  const completed = Number.isFinite(progress.completed)
    ? Math.max(0, Math.min(progress.completed, total))
    : 0;
  const waiting = Number.isFinite(progress.retry_after) && progress.retry_after! > 0;
  const measured = total > 0 && progress.phase !== "optimizing" && !Object.hasOwn(collectionHopPhases, progress.phase);
  return `<div class="job-progress-meta"><span>Current stage · ${esc(human(progress.phase))}</span>${measured ? `<span>${esc(completed)} / ${esc(total)}</span>` : ""}</div><progress class="job-progress-bar"${measured ? ` max="${total}" value="${completed}"` : ""} aria-label="${esc(human(progress.phase))}"></progress>${waiting ? `<p class="job-retry">Waiting ${esc(progress.retry_after)} seconds before retrying Miro.</p>` : ""}`;
}

function taskActivity(job: ActiveJob): {kind: "computing" | "api" | "waiting" | "working" | "loading"; label: string} {
  if (job.action === "load-shared-collection") return {kind: "loading", label: "Loading saved data"};
  const progress = job.progress;
  if (job.status === "cancelling") return {kind: "working", label: "Canceling"};
  if (progress?.stage === "resource_wait") {
    const labels = {memory: "Waiting for memory allowance", cpu: "Waiting for CPU capacity",
      fifo: "Waiting for an earlier layout", memory_retry: "Waiting for a larger-memory retry"};
    return {kind: "waiting", label: labels[progress.wait_reason!] || "Waiting for layout resources"};
  }
  if (job.execution_state === "credentials_wait") return {kind: "waiting", label: "Waiting for credential prompt"};
  if (job.execution_state === "credentials") return {kind: "waiting", label: "Unlock credentials in terminal"};
  if (progress?.phase === "waiting") return {kind: "waiting", label: "Waiting for Miro API"};
  if (progress?.shared_api_wait_reason === "server_cooldown" && (progress.shared_api_wait_seconds || 0) > 0)
    return {kind: "waiting", label: "Waiting for Explorer API cooldown"};
  if (progress && ["optimizing", "compacting", "layout", "building_plan", "pegout_paths"].includes(progress.phase))
    return {kind: "computing", label: "Computing"};
  if (progress && (progress.phase === "collecting" || progress.phase.startsWith("address_counts") ||
      ["preflight", "updating", "removing", "framing", "recovery", "frame_recovery", "creating", "pegout_search"].includes(progress.phase)))
    return {kind: "api", label: ["collecting", "pegout_search"].includes(progress.phase) || progress.phase.startsWith("address_counts") ? "Explorer API work" : "Miro API work"};
  return {kind: "working", label: job.execution_state === "starting" ? "Starting" : "Working"};
}

function taskActivitySummary(jobs: ActiveJob[]): string {
  const counts = {computing: 0, api: 0, working: 0, waiting: 0, loading: 0};
  jobs.forEach(job => counts[taskActivity(job).kind]++);
  return [[counts.computing, "computing"], [counts.api, "using APIs"], [counts.working, "working"], [counts.waiting, "waiting"], [counts.loading, "loading"]]
    .filter(([count]) => count).map(([count, label]) => `${count} ${label}`).join(" · ");
}

function taskName(job: ActiveJob): string {
  return job.action === "shared-trace" ? "Shared collection" : job.caseId ? state.cases.find(item => item.id === job.caseId)?.name ||
    (state.activeCase?.id === job.caseId ? state.activeCase.name : job.caseId) : "New investigation";
}

function taskMessage(job: ActiveJob): string {
  return job.outcomeError || (job.status === "running" ? job.progress?.message || job.message : job.message) || "Working on your request…";
}

// Display-only loads never enter the server job queue or its conflict checks.
function activeTasks(): ActiveJob[] {
  return [...runningJobs(), ...[...sharedCollectionLoads.values()].map(load => load.task).filter(task => task.status === "running")];
}

function jobBanner(): string {
  const active = activeTasks();
  const recent = [...finishedJobs(), ...[...sharedCollectionLoads.values()].map(load => load.task).filter(task => task.status !== "running")]
    .sort((a, b) => (b.finishedAt ?? b.started) - (a.finishedAt ?? a.started)).slice(0, 8);
  const current = active.find(job => viewingCase(job.caseId)) || active[0];
  if (!active.length && !recent.length) return '<div id="job-tasks"></div>';
  return `<div id="job-tasks"><details class="task-list"${tasksOpen ? " open" : ""}><summary><span class="task-summary-count">${active.length ? `<span class="spinner" aria-hidden="true"></span>${active.length} active task${active.length === 1 ? "" : "s"}<small class="task-activity-summary">${esc(taskActivitySummary(active))}</small>` : "Recent tasks"}</span><div class="task-summary-message"><span role="status" aria-live="polite">${esc(current ? `${taskName(current)} · ${taskMessage(current)}` : `${recent.length} completed task${recent.length === 1 ? "" : "s"}`)}</span>${current ? jobProgress(current) : ""}</div><span class="task-summary-hint">View tasks</span></summary><div class="task-list-body">${[...active, ...recent].map(job => {
    const running = job.status === "running" || job.status === "cancelling";
    return `<article class="job-banner task-row${job.status === "failed" ? " task-failed" : ""}" data-task="${esc(job.id)}"><div class="job-details"><div class="task-heading"><strong>${esc(taskName(job))}</strong><span>${esc(job.action === "load-shared-collection" ? "Shared collection" : human(job.action))} · ${esc(running ? taskActivity(job).label : human(job.status))}</span></div><p data-job-message="${esc(job.id)}">${esc(taskMessage(job))}</p>${running ? `<div data-job-progress="${esc(job.id)}">${jobProgress(job)}</div>` : ""}${running && job.live && (!job.execution_state || job.execution_state === "credentials") ? '<p>Complete any Proton Pass prompt in the launching terminal.</p>' : ""}${job.source_run_id ? `<p class="small muted">Source run: ${esc(short(job.source_run_id, 8))}</p>` : ""}</div><div class="job-actions">${running ? `<span class="job-time" data-job-elapsed="${esc(job.id)}"></span>` : ""}${job.caseId ? `<button class="btn small" data-action="open-job" data-id="${esc(job.id)}">Open investigation</button>` : !running && job.action === "lookup" && job.outcome?.result ? `<button class="btn small" data-action="load-job-outputs" data-id="${esc(job.id)}">Review outputs</button>` : ""}${running && (job.cancellable || job.cancelling) ? `<button class="btn small" data-action="cancel-job" data-id="${esc(job.id)}"${disabled(job.cancelling)}>${job.cancelling ? "Canceling…" : "Cancel calculation"}</button>` : ""}${!running ? `<button class="btn ghost small" data-action="dismiss-job" data-id="${esc(job.id)}">Dismiss</button>` : ""}</div></article>`;
  }).join("")}</div></details></div>`;
}

function updateJobProgress(): void {
  for (const id of state.openCases) {
    const status = document.querySelector<HTMLElement>(`.investigation-tab-open[data-case="${id}"] .investigation-tab-status`);
    if (status) {
      const count = runningJobs().filter(job => job.caseId === id).length;
      status.textContent = count ? `${count} running` : "";
    }
  }
  const container = document.querySelector("#job-tasks");
  if (!container) return;
  const focused = document.activeElement as HTMLElement | null;
  const focusSummary = focused?.matches?.("#job-tasks summary");
  const scrollTop = document.querySelector(".task-list-body")?.scrollTop || 0;
  const focusId = focused?.closest("#job-tasks") ? focused.dataset.id : undefined;
  const focusAction = focusId ? focused?.dataset.action : undefined;
  container.outerHTML = jobBanner();
  const body = document.querySelector(".task-list-body");
  if (body) body.scrollTop = scrollTop;
  if (focusSummary) document.querySelector<HTMLElement>("#job-tasks summary")?.focus({preventScroll: true});
  if (focusId && focusAction) document.querySelector<HTMLElement>(`#job-tasks [data-action="${CSS.escape(focusAction)}"][data-id="${CSS.escape(focusId)}"]`)?.focus({preventScroll: true});
  updateJobClock();
}

const workspaceHeaderObserver = typeof ResizeObserver === "undefined" ? undefined : new ResizeObserver(entries => {
  const header = entries[0]?.target;
  if (header) document.documentElement.style.setProperty("--workspace-header-height", `${header.getBoundingClientRect().height}px`);
});

function caseOpeningBanner(): string {
  const opening = state.openingCase;
  if (!opening || opening.generation !== pageGeneration) return "";
  return `<div class="job-banner" id="case-opening"><span class="spinner" aria-hidden="true"></span><div class="job-details" role="status" aria-live="polite"><strong>Opening ${esc(opening.name)}</strong><p>Loading investigation overview.</p></div></div>`;
}

function cancelCaseOpening(): void {
  if (state.openingCase?.generation === pageGeneration) pageGeneration++;
  state.openingCase = null;
}

function render(focus = false): void {
  const previousPreviews = document.querySelector<HTMLElement>(".preview-library");
  if (previousPreviews?.dataset.caseId) previewLibraryState(previousPreviews.dataset.caseId).scrollTop = previousPreviews.scrollTop;
  const tabScroll = document.querySelector(".investigation-tab-list")?.scrollLeft || 0;
  const focused = document.activeElement as HTMLInputElement | HTMLTextAreaElement | null;
  const focusedId = !focus && focused?.closest?.("#main") ? focused.id : undefined;
  const selection = focusedId && typeof focused?.selectionStart === "number"
    ? [focused.selectionStart, focused.selectionEnd ?? focused.selectionStart] : undefined;
  saveSettingsDraft();
  const openImports = ['advanced-attributions', 'advanced-name-colors', 'advanced-change-outputs', 'compact-tools']
    .filter(id => document.querySelector<HTMLDetailsElement>(`#${id}`)?.open);
  const names: Record<Page, string> = {
    dashboard: "Investigations",
    new: "New investigation",
    case: state.activeCase?.name || "Investigation",
    settings: "Workspace defaults",
    "case-settings": "Investigation settings",
    addresses: "Address review",
  };
  app.innerHTML = `<div class="layout">${sidebar()}<div class="main-shell"><header class="workspace-header"><div class="topbar"><div class="breadcrumb">${icon("folder")}<span>Workspace</span>${icon("chevron")}<strong>${esc(names[state.page])}</strong></div><div class="topbar-right">${notificationControl()}<span class="local-pill">${icon("lock")} LOCAL SESSION</span><span class="avatar" aria-label="Investigation workspace">LT</span></div></div>${investigationTabs()}${endpointExportNotice()}${jobBanner()}${caseOpeningBanner()}</header><main id="main" class="content" tabindex="-1">${state.error ? `<div class="alert error" role="alert">${icon("info")}<div><strong>Unable to complete the action</strong><p>${esc(state.error)}</p></div><button class="dismiss" data-action="dismiss-error" aria-label="Dismiss error">${icon("close")}</button></div>` : ""}${miroEditConflictsPanel()}${state.page === "dashboard" ? dashboard() : state.page === "new" ? newCase() : state.page === "case" ? workspace() : state.page === "addresses" ? addressReviewPage() : settingsPage()}</main></div></div>`;
  workspaceHeaderObserver?.disconnect();
  const tabList = document.querySelector(".investigation-tab-list");
  if (tabList) tabList.scrollLeft = tabScroll;
  const previews = document.querySelector<HTMLElement>(".preview-library");
  if (previews?.dataset.caseId) previews.scrollTop = previewLibraryState(previews.dataset.caseId).scrollTop;
  if (focus) document.querySelector(".investigation-tab.active")?.scrollIntoView({block: "nearest", inline: "nearest"});
  const header = document.querySelector(".workspace-header");
  if (header) workspaceHeaderObserver?.observe(header);
  renderedSettingsKey = ["case-settings", "settings"].includes(state.page) ? settingsKey() : "";
  openImports.forEach(id => {
    const disclosure = document.querySelector<HTMLDetailsElement>(`#${id}`);
    if (disclosure) disclosure.open = true;
  });
  updateJobClock();
  if (focusedId) {
    const restored = document.querySelector<HTMLInputElement | HTMLTextAreaElement>(`#${CSS.escape(focusedId)}`);
    restored?.focus({preventScroll: true});
    if (selection && restored?.setSelectionRange) restored.setSelectionRange(selection[0], selection[1]);
  }
  if (focus)
    document
      .querySelector<HTMLHeadingElement>("#page-title")
      ?.focus({ preventScroll: true });
}

function dashboard(): string {
  const traced = state.cases.filter((item) => !!item.latest_run).length;
  const linked = state.cases.filter((item) => !!item.miro_board).length;
  return `<div class="page-heading"><div><div class="eyebrow">Your local workspace</div><h1 id="page-title" tabindex="-1">Investigations</h1><p>Trace selected outputs through bounded, documented runs.</p></div>${button("New investigation", "new", "plus", "primary")}</div><div class="stats-grid"><div class="stat"><div><div class="stat-label">Investigations</div><div class="stat-value">${state.cases.length.toString().padStart(2, "0")}</div><div class="stat-note">Saved on this computer</div></div><span class="stat-icon">${icon("folder")}</span></div><div class="stat"><div><div class="stat-label">Investigations with runs</div><div class="stat-value">${traced.toString().padStart(2, "0")}</div><div class="stat-note">Bounded, recorded traces</div></div><span class="stat-icon">${icon("layers")}</span></div><div class="stat"><div><div class="stat-label">Linked Miro boards</div><div class="stat-value">${linked.toString().padStart(2, "0")}</div><div class="stat-note">Editable graph workspaces</div></div><span class="stat-icon">${icon("board")}</span></div></div><section class="panel"><div class="panel-head"><div><h2>Saved investigations</h2><p>Pick up where you left off, with every run kept intact.</p></div>${button("Refresh", "refresh", "refresh", "ghost small")}</div>${state.cases.length ? `<div class="table-wrap"><table><thead><tr><th>Investigation</th><th>Source</th><th>Latest run</th><th>Miro board</th><th><span class="sr-only">Open</span></th></tr></thead><tbody>${state.cases.map((item) => `<tr><td><div class="case-cell"><span class="case-icon">${icon("folder")}</span><div><button class="case-title" data-case="${esc(item.id)}"${disabled(isCaseOpening(item.id))}>${esc(item.name)}</button><span class="small muted">${esc(formatDate(item.created_at))}</span></div></div></td><td><span class="badge ${item.fixture ? "purple" : ""}">${item.fixture ? "Synthetic data" : "Live Liquid"}</span></td><td>${item.latest_run ? `<span class="mono">${esc(short(item.latest_run, 8))}</span><div class="small muted">${esc(human(item.status || "saved"))}</div>` : '<span class="small muted">Ready for first run</span>'}</td><td><span class="badge ${item.miro_board ? "" : "gray"}">${item.miro_board ? "Linked" : "Not linked"}</span></td><td>${button("Open", "open-case", "arrow", "ghost small", isCaseOpening(item.id), `data-id="${esc(item.id)}" aria-label="Open ${esc(item.name)}"`)}</td></tr>`).join("")}</tbody></table></div><div class="table-footer"><span>${state.cases.length} investigation${state.cases.length === 1 ? "" : "s"}</span><span>Saved runs are shared with the terminal interface</span></div>` : `<div class="empty-state"><div class="empty-icon">${icon("folder")}</div><h2>Start with a transaction</h2><p>Create an investigation, choose the outputs to follow, and run a trace with a clear stopping point.</p>${button("Create your first investigation", "new", "plus", "primary")}</div>`}</section><div class="info-grid"><div class="info-card">${icon("graph")}<div><h3>A clear path from evidence to graph</h3><p>Select starting UTXOs, run a bounded trace, then explore locally with Mermaid or add the result to your Miro board.</p></div></div><div class="info-card secondary">${icon("shield")}<div><h3>Credentials stay behind the scenes</h3><p>Live actions use SecretSpec and Proton Pass through your terminal. No API keys are entered in this interface.</p></div></div></div>`;
}

function newCase(): string {
  const draft = state.draft;
  return `<div class="page-heading"><div><div class="eyebrow">Build a starting point</div><h1 id="page-title" tabindex="-1">New investigation</h1><p>Choose a blockchain and the exact outputs you want to follow.</p></div>${button("Back to investigations", "dashboard", "", "ghost")}</div><form id="new-case-form"><div class="form-grid"><div class="form-stack"><section class="panel"><div class="panel-head"><h2><span class="section-number">1</span> Investigation details</h2></div><div class="panel-body"><div class="field-row"><label class="field"><span>Investigation name</span><input name="name" maxlength="120" placeholder="e.g. Service withdrawal review" value="${esc(draft.name)}" required autocomplete="off"/></label><label class="field"><span>Blockchain</span><select name="blockchain" required${disabled(isBusy())}><option value="liquid" selected>Liquid Network</option></select></label></div><p class="small muted">Liquid Network is currently the only supported blockchain.</p></div></section><section class="panel"><div class="panel-head"><div><h2><span class="section-number">2</span> Starting outputs</h2><p>Multiple transactions can share one investigation.</p></div></div><div class="panel-body"><label class="field"><span>Transaction hashes</span><textarea name="txids" class="mono" rows="3" spellcheck="false" placeholder="Paste transaction hashes separated by commas">${esc(draft.txids)}</textarea><small>Paste up to 100 transaction hashes, separated by commas, spaces, or newlines.</small></label><div class="heading-actions">${button("Load outputs", "lookup", "search", "", isBusy())}</div><p class="small muted" style="margin-top:12px">This lookup uses your Blockstream credits. Watch the terminal for Proton Pass prompts.</p><div id="lookup-outputs">${draft.reports.length ? '<p class="small muted" style="margin-top:17px">Amounts are base units; ?? means unavailable.</p>' : ""}${draft.reports
    .map(
      (report, index) =>
        `<section class="output-group"><header><span>Transaction ${index + 1}</span><span class="mono" title="${esc(report.txid)}">${esc(short(report.txid, 15))}</span></header>${report.outputs
          .map((output) => {
            const outpoint = output.outpoint || `${report.txid}:${output.vout}`;
            return `<label class="output-row${output.selectable ? "" : " disabled"}"><input type="checkbox" data-outpoint="${esc(outpoint)}"${draft.selected.has(outpoint) ? " checked" : ""}${disabled(!output.selectable || isBusy())}/><span class="output-detail"><span class="output-top"><strong>vout ${esc(output.vout)}</strong><span>${esc(outputValue(output))} <span title="${esc(output.asset || "Unavailable asset")}">${esc(outputAsset(output.asset))}</span></span>${!output.selectable ? `<span class="badge gray">${esc(human(output.reason || output.script_type || "not traceable"))}</span>` : ""}</span><span class="mono">${esc(output.address || (output.selectable ? "Address unavailable" : human(output.reason || output.script_type || "Non-spendable output")))}</span></span></label>`;
          })
          .join("")}</section>`,
    )
    .join(
      "",
    )}${draft.reports.length ? `<p class="selection-count" id="selection-count">${draft.selected.size} starting output${draft.selected.size === 1 ? "" : "s"} selected</p>` : ""}</div><details class="direct-seeds"${draft.seeds ? " open" : ""}><summary>Enter exact output references directly</summary><label class="field"><span>Starting outputs</span><textarea name="seeds" class="mono" rows="2" spellcheck="false" placeholder="TRANSACTION_HASH:0, TRANSACTION_HASH:1">${esc(draft.seeds)}</textarea><small>Optional. These numeric outpoints are combined with checked outputs above.</small></label></details></div></section><div class="form-actions"><p>Creates the investigation locally. Start a trace when you are ready.</p><button class="btn primary" type="submit"${disabled(isBusy())}>${icon("plus")}Create investigation</button></div></div><aside class="form-stack"><div class="side-note"><strong>Follow specific outputs</strong>Each selected UTXO becomes a starting point. Shared descendants appear once in the cumulative graph.<ul><li>Choose relevant outputs after lookup.</li><li>Fees and unspendable outputs cannot be selected.</li><li>Hidden amounts and assets appear as ??.</li></ul></div><div class="side-note"><strong>One case, several runs</strong>Each run preserves a saved snapshot. Continue to more hops or resume unfinished branches later, including after closing this interface.</div></aside></div></form>`;
}

function boardUrl(board: string): string {
  return `https://miro.com/app/board/${encodeURIComponent(board)}/`;
}

function miroEditConflictsPanel(): string {
  const failure = state.editConflicts;
  if (!state.error || !failure || state.activeCase?.id !== failure.caseId ||
      !["case", "case-settings", "addresses"].includes(state.page)) return "";
  const report = failure.report;
  if (report?.kind !== "miro_edit_conflicts" || typeof report.board_id !== "string" ||
      !report.board_id || !Array.isArray(report.items)) return "";
  const value = (entry: MiroConflictValue): string => {
    if (!entry?.present) return '<span class="muted">Not set</span>';
    if (typeof entry.value !== "string") return '<span class="muted">Value unavailable</span>';
    const type = ["number", "boolean", "null", "json"].includes(entry.type || "") ? `<span class="small muted">${esc(entry.type)} value</span>` : "";
    return `${entry.value === "" ? '<span class="muted">Empty string</span>' : `<pre>${esc(entry.value.slice(0, 1000))}</pre>`}${type}${entry.truncated || entry.value.length > 1000 ? '<span class="small muted">Excerpt only; not the complete value</span>' : ""}`;
  };
  const objects = report.items.slice(0, 20).map((item, index) => {
    if (!item || typeof item.key !== "string" || typeof item.item_id !== "string" ||
        !item.item_id || !Array.isArray(item.changes)) return "";
    const changes = item.changes.slice(0, 20).filter(change => change && typeof change.field === "string" &&
      typeof change.saved?.present === "boolean" && typeof change.current?.present === "boolean");
    if (!changes.length) return "";
    let url = "";
    try {
      // Construct the link from IDs. Never render an API-supplied object_url.
      url = `${boardUrl(report.board_id)}?moveToWidget=${encodeURIComponent(item.item_id)}`;
    } catch { /* Invalid identifier encoding must not hide the field differences. */ }
    const kind = ["shape", "connector", "frame"].includes(item.kind) ? item.kind : "object";
    return `<article class="miro-edit-conflict"><div class="panel-head"><div><h3>Affected ${kind} ${index + 1}</h3><p>Object ID: <code>${esc(item.item_id)}</code></p><p>Graph key: <code>${esc(item.key)}</code></p></div>${url ? `<a class="btn small" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open object in Miro ${icon("external")}</a>` : ""}</div><div class="table-wrap"><table><thead><tr><th scope="col">Changed field</th><th scope="col">Last synced value</th><th scope="col">Current Miro value</th></tr></thead><tbody>${changes.map(change => `<tr><th scope="row"><code>${esc(change.field)}</code></th><td>${value(change.saved)}</td><td>${value(change.current)}</td></tr>`).join("")}</tbody></table></div>${item.truncated || item.changes.length > 20 ? '<p class="artifact-note">Additional differences for this object were omitted.</p>' : ""}</article>`;
  }).join("");
  if (!objects) return "";
  return `<section class="panel miro-edit-conflicts" aria-labelledby="miro-edit-conflicts-title"><div class="panel-head"><div><h2 id="miro-edit-conflicts-title">Locate the Miro changes</h2><p>These objects differ from their last synced values.</p></div></div><div class="panel-body"><ol><li>Open each affected object in Miro and review the differences below.</li><li>For accidental changes, restore the listed last synced values. Preserve any notes you want to keep before changing grouping.</li><li>Return to Plots &amp; Miro, regenerate the board update, and retry.</li></ol><p class="small muted">Text values include formatting markup so small formatting changes remain visible. Use excerpts to locate a change, not to replace the complete object text. This report does not change the board.</p></div>${objects}${report.truncated || report.items.length > 20 ? '<div class="panel-body"><p class="artifact-note">The report was shortened. Additional differences may appear after these are resolved.</p></div>' : ""}</section>`;
}

function rebuildAction(detail: Case, saved: boolean): string {
  const progress = detail.miro_rebuild;
  const incomplete = progress && progress.status !== "complete";
  const uncertain = progress?.status === "pending" || progress?.status === "unavailable";
  return `${progress?.notice ? `<p class="small muted">${esc(progress.notice)}</p>` : ""}${progress?.previous_board_id ? `<a class="board-link" href="${esc(boardUrl(progress.previous_board_id))}" target="_blank" rel="noopener noreferrer">Open preserved previous board ${icon("external")}</a>` : ""}${button(incomplete ? "Resume board rebuild" : "Rebuild on new board", "miro-rebuild-dialog", "plus", "wide", !saved || !detail.miro_board || uncertain || isBusy())}<p class="small muted">Recreate the selected graph with current settings on a new private board. The previous board, comments and manual edits remain available there.</p>`;
}

function openRebuildDialog(detail: Case): void {
  const progress = detail.miro_rebuild;
  const resume = progress && progress.status !== "complete";
  if (!detail.miro_board || !currentRun() || ["pending", "unavailable"].includes(progress?.status || "")) return;
  dialogRebuild = {caseId: detail.id, sourceBoard: resume ? progress.previous_board_id : detail.miro_board,
    runId: resume ? progress.run_id : currentRun()!.id, budgetLimitsEnabled: Boolean(detail.run_defaults.budget_limits_enabled)};
  const settings = {...defaults, ...detail.run_defaults};
  const name = resume && progress.name ? progress.name : `${detail.fixture ? "SYNTHETIC DATA · " : ""}${detail.name}`.slice(0, 50) + " · Rebuilt";
  dialog.innerHTML = `<form id="action-form"><header class="dialog-head"><div><h2 id="dialog-title">${resume ? "Resume board rebuild" : "Rebuild on a new private board"}</h2><p>${resume ? "Continue the saved rebuild. An acknowledged replacement board is reused." : "Recreate this saved graph using current labels, grouping and layout settings, then link the new board to this investigation."}</p></div></header><div class="dialog-body"><div class="dialog-board"><span>Previous board preserved</span><a class="board-link" href="${esc(boardUrl(dialogRebuild.sourceBoard))}" target="_blank" rel="noopener noreferrer">Open previous board ${icon("external")}</a><span>Selected snapshot</span><strong class="mono">${esc(dialogRebuild.runId)}</strong></div><p>The previous board and its comments and manual edits stay there. They are not copied to the rebuilt graph. Create frames separately when the graph is finished.</p><label class="field"><span>New board name</span><input name="board_name" required maxlength="60" value="${esc(name)}"${resume ? " readonly" : ""}/></label>${settings.budget_limits_enabled ? `<label class="field"><span>New item budget for this rebuild</span><input name="max_new_items" type="number" min="0" max="9007199254740991" step="1" required value="${settings.max_new_items}"/><small>Include all graph objects and connections. 0 = unlimited. The layout and any enabled budget are checked before a board is created. This does not change investigation defaults.</small></label>` : '<p class="small muted">No new Miro item cap. Optional run budgets are off in investigation settings.</p>'}<p>Credentials are retrieved through the launching terminal. Complete any Proton Pass prompt there.</p></div><footer class="dialog-footer"><button type="button" class="btn" data-action="close-dialog">Cancel</button><button type="submit" class="btn primary">${resume ? "Resume board rebuild" : "Create board and rebuild"}</button></footer></form>`;
  dialog.showModal();
}

function safeLocalUrl(url: string | undefined): string {
  return url?.startsWith("/files/") &&
    !url.includes("\\") &&
    !url.includes("\r") &&
    !url.includes("\n")
    ? url
    : "";
}

function downloadLink(item: Download | undefined, label: string, classes = ""): string {
  if (!item) return "";
  const localExport = /^\/api\/cases\/[a-zA-Z0-9_-]+\/plot-exports\/[a-zA-Z0-9_-]+\/(?:transactions|endpoints)\.csv$/.test(item.url)
    && !/[\r\n]/.test(item.url);
  if (!safeLocalUrl(item.url) && !localExport) return "";
  return `<a class="btn small ${classes}" href="${esc(item.url)}" download="${esc(item.name)}">${icon("download")}${esc(label)}</a>`;
}

function fallbackNotice(artifact: RenderingMetadata | undefined): string {
  if (artifact?.renderer === "direct_svg")
    return artifact.fallback_reason === "timeout"
      ? "This saved preview was created with the former Mermaid time limit. Its direct SVG fallback and Mermaid source remain available. Generate a new chart to run Mermaid without that limit."
      : "This saved preview was created with the former Mermaid size limit. Its direct SVG fallback and Mermaid source remain available. Generate a new chart to run Mermaid without that limit.";
  if (artifact?.layout_algorithm === "dependency_layers_v1")
    return artifact.fallback_reason === "timeout"
      ? "This saved preview was created with the former ELK time limit. It uses a dependency layout without crossing optimization. Generate a new ELK preview to use the current engine."
      : "This saved preview was created with the former ELK size limit. It uses a dependency layout without crossing optimization. Generate a new ELK preview to use the current engine.";
  return "";
}

function localGraph(artifact: Artifact | undefined, saved: boolean, settings: Settings): string {
  const includeFees = settings.include_fees;
  const matches = artifact?.include_fees === includeFees &&
    (artifact?.color_attribution_arrows ?? false) === settings.color_attribution_arrows;
  const preview = matches ? safeLocalUrl(artifact?.preview_url) : "";
  const svg = matches ? artifact?.downloads.find((item) => item.name === "graph.svg") : undefined;
  const source = matches ? artifact?.downloads.find((item) => item.name === "graph.mmd") : undefined;
  const mismatch = artifact && !matches;
  const name = artifact?.renderer === "direct_svg" ? "Direct SVG fallback" : "Mermaid preview";
  const notice = preview ? fallbackNotice(artifact) : "";
  return `<section class="panel"><div class="panel-head graph-panel-head"><div><h2>Local graph</h2><p>${esc(name)} of the selected saved run.</p></div><div class="artifact-actions">${downloadLink(svg, "Download SVG", "primary")}${preview ? `${button("Refresh chart", "mermaid", "refresh", "small", !saved || isBusy())}<a class="btn ghost small" href="${esc(preview)}" target="_blank" rel="noopener noreferrer">${icon("external")}Open full view</a>` : '<span class="badge gray">Offline renderer</span>'}</div></div>${preview ? `${notice ? `<div class="panel-body artifact-note">${esc(notice)}</div>` : ""}<iframe loading="lazy" class="graph-preview" src="${esc(preview)}" title="${esc(name)} of the selected investigation run" sandbox="allow-scripts" referrerpolicy="no-referrer"></iframe><div class="preview-caption"><span>Generated from saved evidence · ${includeFees ? "Fee flows included" : "Fee flows hidden"}</span>${downloadLink(source, "Mermaid source (.mmd)", "ghost")}</div>` : `<div class="graph-placeholder"><div class="mini-flow" aria-hidden="true"><span class="mini-node">${icon("folder")}</span><span class="mini-connection"></span><span class="mini-node tx">${icon("layers")}</span><span class="mini-connection"></span><span class="mini-node out">${icon("folder")}</span></div><h3>${mismatch ? "Update the chart’s display" : "Your trace, in perspective"}</h3><p>${mismatch ? "The saved chart uses different graph settings. Create another chart to match this investigation’s current settings." : saved ? "Create a quick local chart from this snapshot, then download the SVG or Mermaid source." : "After your first run, create a local chart or send the flow to an editable Miro board."}</p>${button("Create Mermaid chart", "mermaid", "graph", "", !saved || isBusy())}</div>`}</section>`;
}

function connectionsGraph(artifact: Artifact | undefined, saved: boolean): string {
  const preview = safeLocalUrl(artifact?.preview_url);
  const count = artifact?.connection_count;
  const svg = artifact?.downloads.find(item => item.name === "graph.svg");
  const source = artifact?.downloads.find(item => item.name === "graph.mmd");
  const evidence = artifact?.downloads.find(item => item.name === "connections.json");
  const transactions = artifact?.downloads.find(item => item.name === "transactions.csv");
  return `<section class="panel" id="connections-panel"><div class="panel-head"><div><h2>Starter connections</h2><p>Transactions on directed paths that reach another starting transaction.</p></div></div>
  <div class="panel-body"><p>Shows all verified connections in the selected saved run, ignoring attribution hop limits, stop-tracing rules, and plotting hop cutoffs. Labels remain visible. No additional transactions are fetched, so collection limits may leave connections undiscovered.</p>
  ${button("Plot starter connections", "connections", "graph", "", !saved || isBusy())}
  ${artifact ? `<p>${count === 0 ? "No connection found in the saved searched data. Nothing is plotted." : `${count} ordered starter pair(s) connected${artifact.connection_scope === "all_saved" ? " · All saved connections" : ` within ${artifact.max_hops} hops`}.`}</p><p class="small muted">Saved layout: ${artifact.transaction_io === "complete" ? `All transaction inputs and outputs, including fees. Isolated context inputs ${artifact.group_context_inputs ? "grouped" : "separate"}.${artifact.context_edge_count === undefined ? "" : ` ${artifact.context_edge_count} context connections, excluded from starter-pair counts.`}` : "Paths only."}</p>` : ""}
  ${downloadLink(svg, "SVG", "small")}${downloadLink(source, "Mermaid source", "small")}${downloadLink(evidence, "Connection report", "small")}${downloadLink(transactions, "Transaction CSV", "small")}
  ${preview ? `<a class="btn small" href="${esc(preview)}" target="_blank" rel="noopener noreferrer">Open full view</a>` : ""}${detailPagesLink(artifact?.downloads)}
  ${artifact?.preview_id && count ? `<details><summary>Publish this reviewed snapshot to Miro</summary><p>Use a separate board. The full trace board is protected. One immutable snapshot per board; repeating this publication reuses acknowledged items.</p>
  <label class="field"><span>Separate Miro board URL or ID</span><input id="connection-board" maxlength="512"/></label>
  <label class="check-line"><input id="connection-confirm" type="checkbox"/><span>I reviewed this snapshot and authorize publishing it to the board above.</span></label>
  <p>SecretSpec and Proton Pass prompts appear in the launching terminal.</p>
  ${button("Publish connection snapshot", "miro-connections", "board", "", isBusy())}</details>` : ""}</div>
  ${preview && count ? `${layoutSearchWarning(artifact?.layout_metrics)}<iframe loading="lazy" class="graph-preview" src="${esc(preview)}#chart" title="Starter connection graph" sandbox="allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer"></iframe>` : ""}</section>`;
}

function currentPegoutSearch(detail: Case): PegoutSearch | undefined {
  if (pegoutDraft.caseId !== detail.id) {
    if (pegoutDraft.caseId) pegoutDrafts.set(pegoutDraft.caseId, pegoutDraft);
    pegoutDraft = pegoutDrafts.get(detail.id) || {caseId: detail.id, custom: false, txid: "", minHops: "0",
      maxHops: "10", selected: "", board: "", approved: ""};
  }
  return detail.pegout_searches?.find(search => search.id === pegoutDraft.selected) || detail.pegout_searches?.[0];
}

function pegoutSeedScope(seeds: string[]): string {
  const outputs = new Set(seeds), transactions = new Set(seeds.map(seed => seed.split(":")[0]));
  return `${outputs.size} selected seed UTXO${outputs.size === 1 ? "" : "s"} from ${transactions.size} starting transaction${transactions.size === 1 ? "" : "s"}`;
}

function pegoutSearchScope(search: PegoutSearch): string {
  return search.seeds ? pegoutSeedScope(search.seeds) : `All outputs of ${short(search.txid || "unknown transaction")}`;
}

function pegoutsGraph(detail: Case): string {
  const search = currentPegoutSearch(detail), artifact = search?.artifact;
  const preview = safeLocalUrl(artifact?.preview_url), busy = isBusy();
  const settings = {...defaults, ...detail.run_defaults};
  const count = search?.match_count;
  const partial = search && search.status !== "bounded_complete";
  const pending = search?.recoverable === true || search?.status === "running";
  const seeds = detail.seeds || [];
  return `<section class="panel" id="pegouts-panel"><div class="panel-head"><div><h2>Trace to peg-outs</h2><p>Find downstream peg-out requests within a transaction hop range.</p></div></div>
  <div class="panel-body"><p>Use the investigation's selected seed UTXOs. Each starting transaction is hop 0; each forward spend adds one hop. Unselected sibling outputs at the start are excluded. Both range limits are included. Only paths reaching a matching peg-out are plotted.</p>
  ${seeds.length ? `<p>Investigation seeds: ${esc(pegoutSeedScope(seeds))}.</p>` : `<p class="artifact-note">This investigation has no selected seed UTXOs. Enable “Use a different starting transaction” to run a custom search.</p>`}
  <label class="check-line"><input id="pegouts-custom" type="checkbox"${pegoutDraft.custom ? " checked" : ""}${disabled(busy)}/><span>Use a different starting transaction</span></label>
  ${pegoutDraft.custom ? `<label class="field"><span>Starting transaction ID</span><input id="pegouts-txid" class="mono" maxlength="64" value="${esc(pegoutDraft.txid)}" autocomplete="off"${disabled(busy)}/></label><p class="small muted">This custom search considers all outputs of the entered transaction, which is hop 0.</p>` : ""}
  <div class="form-grid"><label class="field"><span>Minimum hops</span><input id="pegouts-min" type="number" min="0" max="2147483647" step="1" value="${esc(pegoutDraft.minHops)}"${disabled(busy)}/></label>
  <label class="field"><span>Maximum hops</span><input id="pegouts-max" type="number" min="0" max="2147483647" step="1" value="${esc(pegoutDraft.maxHops)}"${disabled(busy)}/></label></div>
  <p class="small muted">${detail.fixture ? "Uses saved synthetic data." : "Uses your Blockstream API credits. Credentials are retrieved through the launching terminal."} Saved stop rules apply. Attribution CSV hop_limit values do not restrict new searches; the search hop range and any enabled run budgets still apply. Change optional budgets in investigation settings.</p>${traceSummary(settings)}
  ${button("Trace and plot peg-outs", "pegouts-start", "graph", "primary", busy || (!pegoutDraft.custom && !seeds.length))}
  ${search ? `<hr/><label class="field"><span>Saved peg-out search</span><select id="pegouts-history"${disabled(busy)}>${(detail.pegout_searches || []).map(item => `<option value="${esc(item.id)}"${item.id === search.id ? " selected" : ""}>${esc(pegoutSearchScope(item))} · hops ${item.min_hops}–${item.max_hops} · ${esc(human(item.status))} · ${esc(short(item.id, 8))}</option>`).join("")}</select></label>
  <p>Saved scope: ${esc(pegoutSearchScope(search))}.</p>${search.txid ? `<p class="mono">${esc(search.txid)}</p>` : ""}<p>Hop range ${search.min_hops}–${search.max_hops}. ${esc(human(search.status))}${search.stop_reason ? `: ${esc(human(search.stop_reason))}` : ""}.</p>
  ${partial ? `<p class="artifact-note">${pending ? "This search was interrupted." : "This search is incomplete."} Resume to continue from its saved progress. Additional peg-outs may remain undiscovered.</p>` : '<p class="small muted">Search reached its current boundary. Unspent outputs, confirmation requirements and stop rules can limit what is discoverable.</p>'}
  <p>${count === undefined ? "Generate a preview to see the matches found so far." : count === 0 ? "No matching peg-out found in the searched data." : `${count} matching peg-out request${count === 1 ? "" : "s"} found.`}</p>
  ${button(pending ? "Recover and resume search" : "Resume search", "pegouts-resume", "play", "", busy)}
  ${button("Refresh peg-out preview", "pegouts-preview", "refresh", "", busy || pending)}
  <div class="artifact-actions">${downloadLink(artifact?.downloads.find(item => item.name === "graph.svg"), "SVG")}${downloadLink(artifact?.downloads.find(item => item.name === "pegouts.json"), "Peg-out report")}${preview ? `<a class="btn small" href="${esc(preview)}" target="_blank" rel="noopener noreferrer">Open full view</a>` : ""}${detailPagesLink(artifact?.downloads)}</div>
  ${pegoutCsvDownloads(artifact)}
  ${artifact?.preview_id && count ? `<details><summary>Publish this reviewed snapshot to Miro</summary><p>Choose a separate Miro board for this peg-out snapshot.</p>
  <label class="field"><span>Separate board URL or ID</span><input id="pegouts-board" maxlength="512" value="${esc(pegoutDraft.board)}"${disabled(busy)}/></label>
  <label class="check-line"><input id="pegouts-confirm" type="checkbox"${pegoutDraft.approved === artifact.preview_id ? " checked" : ""}${disabled(busy)}/><span>I reviewed this snapshot and authorize publishing it to the board above.</span></label>
  ${button("Publish peg-out snapshot", "miro-pegouts", "board", "", busy)}</details>` : ""}` : ""}
  <p class="small muted">A peg-out request does not confirm the separate Bitcoin payout. Paths establish UTXO reachability, without assigning ownership or confidential amounts.</p></div>
  ${preview && count ? `${layoutSearchWarning(artifact?.layout_metrics)}<iframe loading="lazy" class="graph-preview" src="${esc(preview)}#chart" title="Peg-out paths" sandbox="allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer"></iframe>` : ""}</section>`;
}

function pegoutInput(element: HTMLInputElement | HTMLSelectElement): boolean {
  if (!element.id?.startsWith("pegouts-") || !state.activeCase || isBusy()) return false;
  const search = currentPegoutSearch(state.activeCase);
  if (element.id === "pegouts-custom") { pegoutDraft.custom = (element as HTMLInputElement).checked; render(); }
  else if (element.id === "pegouts-txid") pegoutDraft.txid = element.value;
  else if (element.id === "pegouts-min") pegoutDraft.minHops = element.value;
  else if (element.id === "pegouts-max") pegoutDraft.maxHops = element.value;
  else if (element.id === "pegouts-history") {
    pegoutDraft.selected = element.value; pegoutDraft.approved = ""; pegoutDraft.board = ""; render();
  } else if (element.id === "pegouts-board") {
    pegoutDraft.board = element.value; pegoutDraft.approved = "";
    const confirmation = document.querySelector<HTMLInputElement>("#pegouts-confirm");
    if (confirmation) confirmation.checked = false;
  } else if (element.id === "pegouts-confirm") pegoutDraft.approved = (element as HTMLInputElement).checked ? search?.artifact?.preview_id || "" : "";
  return true;
}

async function pegoutAction(action: string): Promise<boolean> {
  if (!["pegouts-start", "pegouts-resume", "pegouts-preview", "miro-pegouts"].includes(action)) return false;
  const detail = state.activeCase;
  if (!detail || isBusy()) return true;
  const search = currentPegoutSearch(detail);
  const operation = action === "pegouts-start" || action === "pegouts-resume" ? "pegouts" : action;
  const body: Record<string, unknown> = {action: operation};
  if (action === "pegouts-start") {
    if (pegoutDraft.custom) {
      const txid = pegoutDraft.txid.trim().toLowerCase();
      if (!/^[a-f0-9]{64}$/.test(txid)) throw new Error("Enter one 64-character Liquid transaction ID.");
      body.txid = txid;
    } else if (!detail.seeds?.length) {
      throw new Error("This investigation has no selected seed UTXOs. Enable ‘Use a different starting transaction’ to run a custom search.");
    }
    const values = [pegoutDraft.minHops.trim(), pegoutDraft.maxHops.trim()];
    if (values.some(value => !/^\d+$/.test(value) || Number(value) > 2147483647) || Number(values[0]) > Number(values[1])) {
      throw new Error("Enter whole-number hop limits from 0 to 2147483647, with minimum no greater than maximum.");
    }
    Object.assign(body, {min_hops: Number(values[0]), max_hops: Number(values[1])});
  } else {
    if (!search) throw new Error("Choose a saved peg-out search first.");
    if (action === "pegouts-resume") body.resume = search.id;
    else if (action === "pegouts-preview") body.search_id = search.id;
    else {
      const previewId = search.artifact?.preview_id;
      if (!previewId || !search.match_count) throw new Error("Generate and review a peg-out preview first.");
      if (pegoutDraft.approved !== previewId) throw new Error("Review the peg-out snapshot and confirm publication first.");
      if (!pegoutDraft.board.trim()) throw new Error("Enter a separate Miro board URL or ID.");
      Object.assign(body, {preview_id: previewId, board: pegoutDraft.board.trim(), confirm_pegouts: true});
    }
  }
  await startJob(`/api/cases/${encodeURIComponent(detail.id)}/actions`, body, operation,
    action === "miro-pegouts" || operation === "pegouts" && !detail.fixture, detail.id);
  return true;
}

function layoutSearchWarning(metrics: LayoutMetrics | undefined): string {
  if (!metrics) return "";
  const {attempt_count: requested, attempted_count: attempted, successful_count: successful, failed_count: failed} = metrics;
  if (typeof requested !== "number" || typeof attempted !== "number" ||
      typeof successful !== "number" || typeof failed !== "number") return "";
  if (![requested, attempted, successful, failed].every(Number.isInteger) ||
      requested < 1 || requested > 1000 || attempted < 1 || attempted > requested ||
      successful < 1 || failed < 1 || successful + failed !== attempted) return "";
  return `<div class="alert warning" role="status"><div><strong>Some layout attempts failed</strong><p>${successful} of ${requested} layout attempts succeeded; ${failed} failed. Best completed layout retained.</p></div></div>`;
}

function layoutMetrics(metrics: LayoutMetrics | undefined, label = "ELK layout"): string {
  if (!metrics?.before || !metrics.after) return "";
  const measures: ["crossings" | "node_overlaps" | "node_intersections", string][] = [
    ["crossings", "Line crossings"],
    ["node_overlaps", "Object overlaps"],
    ["node_intersections", "Lines through objects"],
  ];
  const count = (value: number, truncated?: boolean): string =>
    Number.isFinite(value) && value >= 0 ? `${truncated ? "≥ " : ""}${esc(value)}` : "??";
  return `${layoutSearchWarning(metrics)}<div class="layout-metrics"><table><caption>Estimated layout quality</caption><thead><tr><th scope="col">Measure</th><th scope="col">Baseline layout</th><th scope="col">${esc(label)}</th></tr></thead><tbody>${measures.map(([key, label]) => `<tr><th scope="row">${label}</th><td>${count(metrics.before[key], metrics.before.truncated)}</td><td>${count(metrics.after[key], metrics.after.truncated)}</td></tr>`).join("")}</tbody></table><p class="layout-metrics-note">Compared with the built-in layout of this saved run, not the live board. These counts exclude caption collisions, and Miro routes may differ.${metrics.before.truncated || metrics.after.truncated ? " The comparison limit was reached. Counts marked ≥ are lower bounds, so the total may be higher." : ""}</p></div>`;
}

function graphSettingsMatch(artifact: Artifact | undefined, settings: Settings): boolean {
  return Boolean(artifact && artifact.include_fees === settings.include_fees &&
    artifact.connector_style === settings.connector_style &&
    artifact.layout_attempts === settings.layout_attempts &&
    (artifact.color_attribution_arrows ?? false) === (settings.color_attribution_arrows ?? false) &&
    (artifact.group_context_inputs ?? false) === settings.group_context_inputs &&
    (artifact.center_name ?? "") === (settings.center_name ?? "") &&
    JSON.stringify(normalizedHubs(artifact.hub_addresses)) === JSON.stringify(normalizedHubs(settings.hub_addresses)));
}

function detailPagesLink(downloads: Download[] | undefined): string {
  const url = safeLocalUrl(downloads?.find(item => item.name === "details.html")?.url);
  return url ? `<a class="btn ghost small" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${icon("external")}Open detail pages</a>` : "";
}

function elkGraph(artifact: Artifact | undefined, saved: boolean, settings: Settings): string {
  const matches = graphSettingsMatch(artifact, settings);
  const preview = matches ? safeLocalUrl(artifact?.preview_url) : "";
  const downloads = matches ? artifact?.downloads : undefined;
  const svg = downloads?.find((item) => item.name === "graph.svg");
  const report = downloads?.find((item) => item.name === "layout-report.json");
  const fallback = artifact?.layout_algorithm === "dependency_layers_v1";
  const name = fallback ? "Dependency layout fallback" : "ELK layout preview";
  const notice = preview ? fallbackNotice(artifact) : "";
  return `<section class="panel" id="elk-layout-panel"><div class="panel-head graph-panel-head"><div><h2>${esc(name)}</h2><p>Local placement preview for the selected saved run.</p></div><div class="artifact-actions">${downloadLink(svg, fallback ? "Download SVG" : "Download ELK SVG", "primary")}${detailPagesLink(downloads)}${preview ? `<a class="btn ghost small" href="${esc(preview)}" target="_blank" rel="noopener noreferrer">${icon("external")}Open full view</a>` : '<span class="badge gray">Offline layout</span>'}</div></div>${preview ? `${notice ? `<div class="panel-body artifact-note">${esc(notice)}</div>` : ""}${layoutMetrics(artifact?.layout_metrics, fallback ? "Dependency layout" : "ELK layout")}<iframe loading="lazy" class="graph-preview" src="${esc(preview)}#chart" title="${esc(name)} of the selected investigation run" sandbox="allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer"></iframe><div class="preview-caption"><span>${esc(human(settings.connector_style))} connectors · ${settings.include_fees ? "Fee flows included" : "Fee flows hidden"}</span>${downloadLink(report, "Layout report", "ghost")}</div><div class="panel-body elk-explanation"><p>${fallback ? "The dependency layout calculates placement" : "ELK calculates placement"}; Miro draws the editable graph. This offline preview does not read your live board arrangement. Return connections can remain elbowed with the straight-line setting.</p>${button("Refresh layout preview", "layout", "refresh", "small", !saved || isBusy())}</div>` : `<div class="panel-body"><p class="artifact-note">${artifact && !matches ? "The saved layout preview uses different graph settings. Generate a new preview to match this investigation." : "Inspect the proposed left-to-right layout and estimated crossings before updating Miro. No API credentials are needed."}</p>${button("Create ELK layout preview", "layout", "layers", "", !saved || isBusy())}<p class="small muted elk-hint">Previewing leaves your board unchanged. Sync and reorganize applies a fresh arrangement and the selected run together. ELK calculations continue until completed or canceled; large graphs can take longer.</p></div>`}</section>`;
}

function currentCompaction(): Artifact | undefined {
  const detail = state.activeCase;
  const run = currentRun();
  if (!detail || !run) return undefined;
  const artifact = detail.artifacts?.[run.id]?.compact;
  return graphSettingsMatch(artifact, { ...defaults, ...detail.run_defaults }) ? artifact : undefined;
}

function compactionMetrics(report: CompactionReport | undefined): string {
  if (!report?.before || !report.after) return "";
  const measure = (value: number | undefined): string =>
    typeof value === "number" && Number.isFinite(value) && value >= 0
      ? esc(value.toLocaleString(undefined, { maximumFractionDigits: 1 })) : "??";
  const rows: [string, number | undefined, number | undefined][] = [
    ["Graph width", report.before.main.width, report.after.main.width],
    ["Graph height", report.before.main.height, report.after.main.height],
    ["Graph area", report.before.main.area, report.after.main.area],
    ["Address-to-transaction distance", report.before.main.address_distance, report.after.main.address_distance],
    ["Connection length", report.before.main.edge_length, report.after.main.edge_length],
    ["Whole-board area", report.before.board.area, report.after.board.area],
  ];
  return `<div class="layout-metrics compaction-metrics"><table><caption>ELK and compacted layout comparison</caption><thead><tr><th scope="col">Measure</th><th scope="col">ELK</th><th scope="col">Compacted</th></tr></thead><tbody>${rows.map(([name, before, after]) => `<tr><th scope="row">${name}</th><td>${measure(before)}</td><td>${measure(after)}</td></tr>`).join("")}</tbody></table><p class="layout-metrics-note">Distances use board coordinates. ${measure(report.moved_addresses)} addresses and ${measure(report.moved_components)} activity groups moved.${report.unchanged ? " No acceptable space-saving moves were found; the arrangement is unchanged." : ""}${report.truncated ? " Some candidate checks were incomplete; unverified moves were skipped." : ""} Caption clearance is estimated, and Miro may draw connectors differently.</p></div>`;
}

function compactGraph(artifact: Artifact | undefined, saved: boolean, detail: Case): string {
  const matches = graphSettingsMatch(artifact, { ...defaults, ...detail.run_defaults });
  const preview = matches ? safeLocalUrl(artifact?.preview_url) : "";
  const valid = Boolean(preview && artifact?.preview_id && artifact.compaction);
  const downloads = valid ? artifact?.downloads : undefined;
  const applyDisabled = !valid || !detail.miro_board || Boolean(detail.miro_recovery?.pending_count) || isBusy();
  return `<section class="panel" id="compact-layout-panel"><div class="panel-head graph-panel-head"><div><h2>Compact graph</h2><p>Bring addresses closer and close gaps between separate activity groups.</p></div><div class="artifact-actions">${downloadLink(downloads?.find((item) => item.name === "graph.svg"), "Download compact SVG", "primary")}${detailPagesLink(downloads)}${valid ? `<a class="btn ghost small" href="${esc(preview)}" target="_blank" rel="noopener noreferrer">${icon("external")}Open comparison</a>` : '<span class="badge gray">Local preview</span>'}</div></div>${valid ? `${layoutSearchWarning(artifact?.layout_metrics)}${compactionMetrics(artifact?.compaction)}<iframe loading="lazy" class="graph-preview compaction-preview" src="${esc(preview)}#chart" title="Compacted graph of the selected investigation run" sandbox="allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer"></iframe><div class="preview-caption"><span>${esc(human(artifact?.connector_style || "straight"))} connectors · ${artifact?.include_fees ? "Fee flows included" : "Fee flows hidden"}</span><div class="artifact-actions">${downloadLink(downloads?.find((item) => item.name === "before.svg"), "Original ELK SVG", "ghost")}${downloadLink(downloads?.find((item) => item.name === "compaction.json"), "Compaction report", "ghost")}</div></div>` : ""}<div class="panel-body elk-explanation"><p>${valid ? "This saved comparison starts from the local ELK arrangement. Manual moves on your Miro board are not included. Applying it replaces managed positions with this exact preview, including its saved graph settings." : artifact && !matches ? "The saved compact preview uses different graph settings. Create a new comparison to match this investigation before applying it." : "Compact the ELK result while preserving transaction order and minimum clearances. Review the before-and-after chart before applying the layout to Miro. This local step needs no API credentials."}</p><div class="compact-actions">${button(valid ? "Refresh compact preview" : "Compact graph", "compact", "layers", "", !saved || isBusy())}${valid ? button("Apply compact layout to Miro", "miro-compact-dialog", "board", "primary", applyDisabled) : ""}</div>${valid && !detail.miro_board ? '<p class="small muted elk-hint">Create or link a Miro board to apply this layout.</p>' : ""}${valid && detail.miro_recovery?.pending_count ? '<p class="small muted elk-hint">Recover the pending Miro items before applying this layout.</p>' : ""}</div></section>`;
}

function csvDownloads(artifact: Artifact | undefined, saved: boolean, includeFees: boolean): string {
  const matches = artifact?.include_fees === includeFees;
  const downloads = matches ? artifact.downloads.filter((item) => safeLocalUrl(item.url)) : [];
  const tables = downloads.filter((item) => item.name === "transactions.csv");
  const provenance = downloads.filter((item) => !item.name.endsWith(".csv"));
  const mismatch = artifact && !matches;
  return `<section class="panel"><div class="panel-head"><div><h2>Full-run CSV downloads</h2><p>Transaction inputs and outputs from the selected collection run, across its full trace.</p></div>${tables.length ? button("Refresh CSV export", "csv", "refresh", "small", !saved || isBusy()) : ""}</div>${tables.length ? `<div class="downloads">${tables.map((item) => downloadLink(item, "Full-run inputs and outputs CSV", "download-link")).join("")}</div><div class="export-footer"><span class="small muted">${includeFees ? "Fee flows included" : "Fee flows hidden"} · Indexes start at 0 · Amounts are exact base units · Unknown values stay blank.</span>${provenance.length ? `<details class="provenance-downloads"><summary>Export provenance</summary><div class="artifact-actions">${provenance.map((item) => downloadLink(item, item.name, "ghost")).join("")}</div></details>` : ""}</div>` : `<div class="panel-body"><p class="artifact-note">${mismatch ? "The saved export uses a different fee setting. Create a new export to match the current settings." : "Create transactions.csv for this full-trace snapshot. Use Plot downloads above for exports limited to a saved plotting goal."}</p>${button("Create CSV export", "csv", "table", "", !saved || isBusy())}</div>`}</section>`;
}

const plotGoals: {id: PlotGoal; name: string; description: string}[] = [
  {id: "full", name: "Full trace", description: "Every collected transaction and its traced branches."},
  {id: "connections", name: "Starter connections", description: "Paths connecting your starting transactions, within a hop limit or across all saved data."},
  {id: "pegouts", name: "Paths to peg-outs", description: "Paths to peg-out requests, optionally including unspent UTXOs and unspendable outputs."},
];

function goalName(goal: string): string {
  return plotGoals.find(item => item.id === goal)?.name || human(goal);
}

const workflowDrafts = new Map<string, typeof workflowDraft>();

function currentWorkflow(detail: Case): typeof workflowDraft {
  if (workflowDraft.caseId !== detail.id) {
    if (workflowDraft.caseId) workflowDrafts.set(workflowDraft.caseId, workflowDraft);
    workflowDraft = workflowDrafts.get(detail.id) || {caseId: detail.id, dataSource: "investigation", sharedRun: "", datasetId: "", goal: "full", minHops: "0", maxHops: "10", connectionScope: "hop_limited" as ConnectionScope, connectionMaxHops: "10", pegoutLimitEnabled: false, pegoutLbtcLimit: "", includeUnspent: false, includeUnspendable: false, layoutMode: "fresh" as PlotLayoutMode, layoutBoard: "", plot: investigationViews.get(detail.id)?.selectedPreview || "", board: "", boardPlot: "", boardGoal: "full", boardName: "", savedBoardName: "", linkedBoardName: "", boardUrl: ""};
  }
  return workflowDraft;
}

function currentPlotRun(detail: Case): Run | undefined {
  const draft = currentWorkflow(detail);
  if (draft.dataSource === "investigation") return currentRun()?.summary_pending ? undefined : currentRun();
  const shared = detail.shared_collection;
  if (!shared?.compatible || !shared.dataset_id || shared.dataset_id !== draft.datasetId) return undefined;
  return shared.runs.find(run => run.id === draft.sharedRun && !run.summary_pending);
}

function plotDataSourceFields(detail: Case): string {
  const draft = currentWorkflow(detail), shared = detail.shared_collection;
  const ready = Boolean(shared?.compatible && shared.dataset_id && shared.runs.length);
  const missing = draft.dataSource === "shared" && !currentPlotRun(detail);
  return `<div class="field-row"><label class="field"><span>Data source</span><select id="workflow-data-source"${disabled(draftBusy())}><option value="investigation"${draft.dataSource === "investigation" ? " selected" : ""}>This investigation</option><option value="shared"${draft.dataSource === "shared" ? " selected" : ""}${disabled(!ready)}>Shared collection${ready ? "" : " · unavailable"}</option></select></label>${draft.dataSource === "shared" ? `<label class="field"><span>Shared snapshot</span><select id="workflow-shared-run"${disabled(draftBusy() || !ready)}>${missing ? '<option value="" selected>Choose an available shared snapshot</option>' : ""}${(shared?.runs || []).map(run => `<option value="${esc(run.id)}"${!missing && run.id === draft.sharedRun ? " selected" : ""}>${esc(run.id)}${run.id === shared?.latest_run ? " · Latest" : ""} · ${esc(run.collected_hops ?? "Unknown")} hops collected</option>`).join("")}</select></label>` : ""}</div><p class="small muted">${draft.dataSource === "shared" ? "Uses shared transaction evidence with this investigation’s own selected seed outputs, attribution rules, colors, and board settings." : "Uses the investigation snapshot selected under Collected data."}</p>${draft.dataSource === "shared" && (!ready || missing) ? `<p class="artifact-note">${esc(shared?.reason || "The selected shared snapshot is unavailable. Choose an available snapshot or collect shared data.")}</p>` : ""}`;
}

async function refreshSharedCollection(caseId: string): Promise<void> {
  await loadCaseSection(caseId, "shared", true);
}

function currentPlot(detail: Case): Plot | undefined {
  const draft = currentWorkflow(detail);
  return draft.plot ? detail.plots?.find(item => item.preview_id === draft.plot) : detail.plots?.[0];
}

type BoardDraft = {plot?: string; recoveryUrl: string};
const boardDrafts = new Map<string, Map<string, BoardDraft>>();

function currentBoardDraft(detail: Case, board: InvestigationBoard): BoardDraft {
  let drafts = boardDrafts.get(detail.id);
  if (!drafts) {drafts = new Map(); boardDrafts.set(detail.id, drafts);}
  let draft = drafts.get(board.id);
  if (!draft) {draft = {recoveryUrl: ""}; drafts.set(board.id, draft);}
  return draft;
}

function boardFromControl(detail: Case, element?: HTMLElement): InvestigationBoard | undefined {
  if (element?.dataset.caseId !== detail.id) return undefined;
  return detail.boards?.find(board => board.id === element.dataset.record);
}

function interruptedBoard(board: InvestigationBoard): boolean {
  return Boolean(board.pending_count) || board.status === "interrupted";
}

function chosenBoardPlot(detail: Case, board: InvestigationBoard): Plot | undefined {
  const plots = matchingBoardPlots(detail, board), draft = currentBoardDraft(detail, board);
  // Interrupted operations always resume their saved plot, regardless of an older draft.
  const id = interruptedBoard(board) ? board.preview_id : draft.plot ?? board.preview_id;
  return id != null ? plots.find(plot => plot.preview_id === id) : plots[0];
}

const plotLayoutDrafts = new Map<string, LayoutSettings>();
type LayoutFormDraft = Pick<typeof workflowDraft, "dataSource" | "sharedRun" | "datasetId" | "goal" | "minHops" | "maxHops" | "connectionScope" | "connectionMaxHops" | "pegoutLimitEnabled" | "pegoutLbtcLimit" | "includeUnspent" | "includeUnspendable"> & {settings: LayoutSettings};
const layoutFormDrafts = new Map<string, Map<string, LayoutFormDraft>>();

function layoutDraftKey(detail: Case): string {
  const draft = currentWorkflow(detail);
  return draft.layoutMode === "fresh" ? "fresh" : draft.layoutBoard;
}

function saveDestinationDraft(detail: Case): void {
  savePlotLayoutDraft();
  const draft = currentWorkflow(detail), key = layoutDraftKey(detail);
  if (!key) return;
  let drafts = layoutFormDrafts.get(detail.id);
  if (!drafts) {drafts = new Map(); layoutFormDrafts.set(detail.id, drafts);}
  drafts.set(key, {dataSource: draft.dataSource, sharedRun: draft.sharedRun, datasetId: draft.datasetId, goal: draft.goal, minHops: draft.minHops, maxHops: draft.maxHops,
    connectionScope: draft.connectionScope, connectionMaxHops: draft.connectionMaxHops,
    pegoutLimitEnabled: draft.pegoutLimitEnabled, pegoutLbtcLimit: draft.pegoutLbtcLimit,
    includeUnspent: draft.includeUnspent, includeUnspendable: draft.includeUnspendable,
    settings: currentLayoutSettings(detail)});
}

function selectLayoutDestination(detail: Case, mode: PlotLayoutMode, board?: InvestigationBoard): void {
  saveDestinationDraft(detail);
  const draft = currentWorkflow(detail);
  draft.layoutMode = mode;
  draft.layoutBoard = board?.id || "";
  if (board) {draft.goal = board.goal; draft.board = board.id;}
  const remembered = layoutFormDrafts.get(detail.id)?.get(layoutDraftKey(detail));
  const plot = board && detail.plots?.find(item => item.preview_id === board.preview_id);
  const restored = remembered || (plot ? {dataSource: plot.collection_source?.kind === "shared" ? "shared" as const : "investigation" as const,
    sharedRun: plot.collection_source?.run_id || "", datasetId: plot.collection_source?.dataset_id || "", goal: board!.goal,
    minHops: String(plot.min_hops ?? 0), maxHops: String(plot.max_hops ?? 10),
    connectionScope: plot.goal === "connections" ? savedConnectionScope(plot) : draft.connectionScope,
    connectionMaxHops: plot.goal === "connections" ? String(plot.max_hops ?? 10) : draft.connectionMaxHops,
    pegoutLimitEnabled: plot.goal === "pegouts" && Boolean(plot.query?.pegout_lbtc_limit),
    pegoutLbtcLimit: plot.goal === "pegouts" ? plot.query?.pegout_lbtc_limit || "" : "",
    includeUnspent: Boolean(plot.query?.include_unspent), includeUnspendable: Boolean(plot.query?.include_unspendable),
    settings: selectLayoutSettings({...defaults, ...detail.run_defaults, ...plot.layout_settings, layout_style: plot.layout_settings?.layout_style ?? "standard"})} : undefined);
  if (restored) {
    const {settings, ...query} = restored;
    Object.assign(draft, query);
    plotLayoutDrafts.set(detail.id, settings);
  }
}


function selectLayoutSettings(settings: Settings): LayoutSettings {
  return Object.fromEntries(layoutSettingKeys.map(key => [key,
    key === "hub_addresses" ? [...settings.hub_addresses] : settings[key]])) as LayoutSettings;
}

function currentLayoutSettings(detail: Case): LayoutSettings {
  return selectLayoutSettings({...defaults, ...detail.run_defaults, ...plotLayoutDrafts.get(detail.id)});
}

function savePlotLayoutDraft(form = document.querySelector<HTMLFormElement>("#plot-layout-form")): void {
  const detail = state.activeCase;
  if (!form || !detail || (form.dataset?.caseId && form.dataset.caseId !== detail.id)) return;
  const previous = {...defaults, ...detail.run_defaults, ...plotLayoutDrafts.get(detail.id)};
  plotLayoutDrafts.set(detail.id, selectLayoutSettings(readSettings(form, previous)));
}

async function persistPlotLayoutSettings(detail: Case): Promise<boolean> {
  const form = document.querySelector<HTMLFormElement>("#plot-layout-form");
  if (form && !form.reportValidity()) return false;
  savePlotLayoutDraft(form);
  const settings = currentLayoutSettings(detail);
  if (JSON.stringify(settings) === JSON.stringify(selectLayoutSettings({...defaults, ...detail.run_defaults}))) {
    plotLayoutDrafts.delete(detail.id);
    return true;
  }
  submitting = true;
  render();
  try {
    const updated = await api<Case>(`/api/cases/${encodeURIComponent(detail.id)}/plot-settings`, {settings});
    if (state.activeCase?.id === detail.id) state.activeCase = mergeCase(state.activeCase, updated);
    state.cases = state.cases.map(item => item.id === detail.id ? {...item, ...updated} : item);
    plotLayoutDrafts.delete(detail.id);
    const generalDraft = settingsDrafts.get(detail.id);
    if (generalDraft) generalDraft.settings = {...generalDraft.settings, ...settings};
    return true;
  } finally {
    submitting = false;
    render();
  }
}

function plotEndpointScope(plot: Plot): string {
  return ["Peg-outs", ...(plot.query?.include_unspent ? ["unspent UTXOs"] : []),
    ...(plot.query?.include_unspendable ? ["unspendable outputs"] : [])].join(" + ");
}

function plotEndpointLabel(plot: Plot | undefined): string {
  if (plot?.goal === "connections") return ` · ${plotContextScope(plot)}`;
  if (plot?.goal !== "pegouts") return "";
  const count = plot.endpoint_count ?? plot.match_count;
  return ` · ${plotEndpointScope(plot)}${plot.query?.pegout_lbtc_limit ? ` · Cumulative target ${plot.query.pegout_lbtc_limit} L-BTC` : ""}${count === undefined ? "" : ` · ${count} endpoints`} · ${plotContextScope(plot)}`;
}

function plotContextScope(plot: Plot): string {
  if (plot.query?.transaction_io === "complete") {
    const outputs = plot.goal === "pegouts" && plot.layout_settings?.include_fees === false ? "non-fee outputs" : "outputs";
    return `All transaction inputs and ${outputs} · isolated inputs ${plot.layout_settings?.group_context_inputs ? "grouped" : "separate"}`;
  }
  return plot.query?.include_context
    ? `Context addresses included · isolated inputs ${plot.layout_settings?.group_context_inputs ? "grouped" : "separate"}`
    : "Paths only";
}

function plotPegoutLimitSummary(plot: Plot): string {
  if (!plot.query?.pegout_lbtc_limit) return "";
  const summary = plot.pegout_limit_summary;
  if (!summary) return `<p class="artifact-note">Cumulative peg-out target: ${esc(plot.query.pegout_lbtc_limit)} L-BTC. Generate a new preview to see the counted total and stopping result.</p>`;
  const unknown = [
    summary.unknown_amount_count ? `${summary.unknown_amount_count} hidden or unavailable amount${summary.unknown_amount_count === 1 ? "" : "s"}` : "",
    summary.unknown_asset_count ? `${summary.unknown_asset_count} unidentified asset${summary.unknown_asset_count === 1 ? "" : "s"}` : "",
    summary.non_lbtc_count ? `${summary.non_lbtc_count} non-L-BTC output${summary.non_lbtc_count === 1 ? "" : "s"}` : "",
  ].filter(Boolean);
  return `<div class="artifact-note" role="group" aria-label="Cumulative peg-out amount"><p><strong>Target:</strong> ${esc(summary.target_lbtc)} L-BTC · <strong>Counted:</strong> ${esc(summary.total_lbtc)} L-BTC · <strong>Overshoot:</strong> ${esc(summary.excess_lbtc)} L-BTC.</p><p>${summary.limit_reached ? "Stopped after including the endpoint that reached or exceeded the target." : "Target not reached: selected paths were exhausted within this snapshot, hop range, and stop rules."} ${esc(summary.counted_pegout_count)} unique peg-outs counted.</p>${unknown.length ? `<p>Not counted toward the target: ${esc(unknown.join(" · "))}.</p>` : ""}<p class="small muted">Nearest ordinary seed distance first, then transaction ID and output index for ties; named-group hop resets do not change this order. Context outputs are excluded from this total. Path reachability does not establish how much stolen value reached an endpoint.</p></div>`;
}

function plotEndpointSummary(plot: Plot | undefined): string {
  if (plot?.goal === "connections") {
    const scope = plot.query?.connection_scope === "all_saved"
      ? '<p class="small muted">All verified saved connections. Attribution hop limits, stop-tracing rules, and plotting hop cutoffs are ignored; labels remain visible.</p>'
      : plot.query?.connection_scope === "hop_limited"
        ? `<p class="small muted">Connecting paths within ${esc(plot.max_hops)} ordinary transaction steps from each selected starter to another. Attribution hop limits and stop-tracing rules are ignored; labels remain visible. Named groups affect displayed hop labels only.</p>`
        : '<p class="small muted">Legacy bounded connections. This saved layout retains the tracing rules and hop basis used when it was generated.</p>';
    const complete = plot.query?.transaction_io === "complete";
    const contextCount = complete && plot.context_edge_count !== undefined
      ? ` ${plot.context_edge_count} context connections, excluded from starter-pair counts.` : "";
    return `${scope}<p class="small muted">Saved layout: ${plotContextScope(plot)}.${contextCount}</p>${complete ? '<p class="small muted">Every included transaction retains all inputs and outputs, including fees, in the graph and transaction CSV. Context objects do not extend the selected connecting paths.</p>' : ""}`;
  }
  if (plot?.goal !== "pegouts") return "";
  const counts = plot.endpoint_counts;
  const matches = counts ? `${counts.pegout} peg-out requests · ${counts.unspent} unspent UTXOs · ${counts.unspendable} unspendable outputs`
    : plot.match_count === undefined ? "" : `${plot.match_count} peg-out requests`;
  const contextCount = (plot.query?.transaction_io === "complete" || plot.query?.include_context) && plot.context_edge_count !== undefined
    ? ` ${plot.context_edge_count} context connections, excluded from endpoint counts.` : "";
  const limits = plot.query?.attribution_hop_limits === "ignore"
    ? "Attribution hop limits ignored; explicit stop-tracing rules and the selected hop range applied."
    : "Attribution hop limits, stop-tracing rules and the selected hop range applied.";
  return `<p class="small muted">Endpoint types: ${esc(plotEndpointScope(plot))}.${matches ? ` Results: ${esc(matches)}.` : ""}</p><p class="small muted">Saved layout: ${plotContextScope(plot)}.${contextCount}</p><p class="small muted">${limits}</p>${plotPegoutLimitSummary(plot)}`;
}

const endpointCsvFiles = ["path-transactions.csv", "trace-endpoints.csv", "endpoints.csv"];

function plotDownloadLabel(item: Download): string {
  return item.name === "transactions.csv" ? "Plot inputs and outputs CSV" : item.name;
}

function pegoutCsvDownloads(artifact: Artifact | undefined): string {
  const transactions = artifact?.downloads.find(item => item.name === "transactions.csv");
  const endpoints = artifact?.downloads.find(item => item.name === "endpoints.csv");
  if (!transactions && !endpoints) return "";
  return `<div class="plot-csv-downloads"><p class="small muted"><strong>All trace transactions:</strong> full input/output accounting for this saved trace, including seeds, intermediate and ending transactions, and any displayed context. <strong>Endpoints only:</strong> an ending-output table with source, source value, deposit/peg-out transaction, address, receiving entity, status, and peg-out LBTC. Unspent outputs are labeled Dormant. Exports use this saved trace's hop range and endpoint choices.</p><div class="artifact-actions">${downloadLink(transactions, "All trace transactions CSV")}${downloadLink(endpoints, "Endpoints only CSV")}</div></div>`;
}

function endpointCsvDownloads(plot: Plot): string {
  if (plot.validation_pending) return "";
  return plot.goal === "pegouts" ? pegoutCsvDownloads(plot.artifact) : "";
}

function plotEvidenceDownloads(plot: Plot): string {
  if (plot.validation_pending) return "";
  const downloads = (plot.artifact?.downloads || []).filter(item => !endpointCsvFiles.includes(item.name)
    && (plot.goal !== "pegouts" || !item.name.endsWith(".csv")));
  return `${endpointCsvDownloads(plot)}${downloads.some(item => item.name === "transactions.csv") ? '<p class="small muted">Plot inputs and outputs CSV has one row per displayed input or output, including context when shown.</p>' : ""}<div class="artifact-actions">${downloads.map(item => downloadLink(item, plotDownloadLabel(item))).join("")}</div>`;
}

function plotHopReference(plot: Plot): string {
  return plot.hop_reference_name ?? state.activeCase?.runs?.find(run => run.id === plot.run_id)?.hop_reference_name ?? "";
}

function savedConnectionScope(plot: Plot): ConnectionScope {
  return plot.query?.connection_scope || (plot.max_hops == null ? "all_saved" : "hop_limited");
}

function plotHopSummary(plot: Plot): string {
  if (plot.goal === "connections" && plot.query?.connection_scope === "all_saved") return " · All saved connections";
  if (plot.goal === "connections" && plot.query?.connection_scope === "hop_limited") return ` · Within ${plot.max_hops} hops of each starter`;
  return plot.goal === "full" ? "" : ` · Hops ${plot.min_hops}–${plot.max_hops}`;
}

function previewLabel(number: unknown): string {
  return typeof number === "number" && Number.isSafeInteger(number) && number > 0 ? `Preview ${number}` : "Saved preview";
}

function plotChoiceLabel(plot: Plot): string {
  return `${previewLabel(plot.preview_number)} · ${plot.layout_mode === "update" ? `Update ${plot.board_name || "board"}` : plot.layout_mode === "fresh" ? "New board" : "Earlier layout"} · ${goalName(plot.goal)} · ${plot.collection_source?.kind === "shared" ? `Shared snapshot ${short(plot.collection_source.run_id, 8)}` : `Run ${short(plot.run_id, 8)}`}${plotHopSummary(plot)}${plotEndpointLabel(plot)} · ${hopBasis(plotHopReference(plot))} · ${formatDate(plot.created_at)}`;
}

function plotCanSync(plot: Plot): boolean {
  return !plot.validation_pending && plot.reviewable && (plot.layout_mode === "update" || !plot.empty && plot.node_count > 0);
}

function updateLayoutBoards(detail: Case, goal: PlotGoal): InvestigationBoard[] {
  return (detail.boards || []).filter(board => board.goal === goal && board.can_sync && board.board_id && !interruptedBoard(board));
}

function plotDestinationFields(detail: Case): string {
  const draft = currentWorkflow(detail), boards = updateLayoutBoards(detail, draft.goal);
  return `<fieldset class="layout-fields"${disabled(draftBusy())}><legend>Miro destination</legend><label class="field"><span>Create or update</span><select id="workflow-layout-mode"><option value="fresh"${draft.layoutMode === "fresh" ? " selected" : ""}>New board</option><option value="update"${draft.layoutMode === "update" ? " selected" : ""}>Update existing board</option></select></label>${draft.layoutMode === "update" ? `<label class="field"><span>Board to update · ${esc(goalName(draft.goal))}</span><select id="workflow-layout-board"><option value="">Select an investigation board</option>${boards.map(board => `<option value="${esc(board.id)}"${draft.layoutBoard === board.id ? " selected" : ""}>${esc(board.name || goalName(board.goal))}</option>`).join("")}</select></label><p class="small muted">Reads this board's current objects, connections and positions. Existing objects stay in place. ELK arranges additions in an empty area, with connections back to existing objects for you to merge manually. The update also removes managed objects excluded by your current tracing rules.</p><p class="small muted">Generate preview reads the board and lays out additions. Review the saved preview, then choose Write to Miro to apply it.</p>${!boards.length ? '<p class="artifact-note">Link a board for this goal below, or finish recovering an interrupted board before generating its update.</p>' : ""}` : `<label class="field"><span>New board name</span><input id="workflow-board-name" maxlength="60" value="${esc(draft.boardName)}" placeholder="${esc(`${detail.name} · ${goalName(draft.goal)}`.slice(0, 60))}"${disabled(draftBusy())}/></label><p class="small muted">Builds a local preview from saved transactions and current attribution, change-output and color rules. Write to Miro then creates a private board using that saved layout.</p>`}</fieldset>`;
}

function plotBoardSummary(plot: Plot | undefined): string {
  if (!plot?.layout_mode) return "";
  if (plot.layout_mode === "fresh") return '<p class="artifact-note"><strong>New board layout.</strong> A fresh arrangement generated from saved data.</p>';
  const counts = plot.update_counts;
  const labels: Record<string, string> = {new_nodes: "Objects to add", retained_nodes: "Objects kept", removed_nodes: "Objects to remove", new_connectors: "Connections to add", removed_connectors: "Connections to remove"};
  return `<p class="artifact-note"><strong>Update existing board:</strong> ${esc(plot.board_name || plot.board_record_id)}. Existing positions are preserved; additions occupy a separate empty area for manual merging.</p>${counts ? `<p class="small muted">${Object.entries(counts).filter(([, value]) => Number.isFinite(value)).map(([key, value]) => `${esc(labels[key] || human(key))}: ${esc(value)}`).join(" · ")}</p>` : ""}<p class="small muted">If you move board objects after generating this layout, generate the update again before syncing.</p>`;
}

function connectionHopFields(draft: typeof workflowDraft): string {
  if (draft.goal !== "connections") return "";
  return `<div class="field-row"><label class="field"><span>Connection search</span><select id="workflow-connection-scope"${disabled(draftBusy())}><option value="hop_limited"${draft.connectionScope === "hop_limited" ? " selected" : ""}>Within hop limit</option><option value="all_saved"${draft.connectionScope === "all_saved" ? " selected" : ""}>All saved connections</option></select></label>${draft.connectionScope === "hop_limited" ? `<label class="field"><span>Maximum connection hops</span><input id="workflow-connection-max-hops" type="number" min="0" max="2147483647" step="1" value="${esc(draft.connectionMaxHops)}"${disabled(draftBusy())}/><small>Maximum ordinary transaction steps from each selected starter to another. The starter is hop 0. Increase this limit to reveal longer connections.</small></label>` : ""}</div>`;
}

function pegoutLimitFields(draft: typeof workflowDraft): string {
  if (draft.goal !== "pegouts") return "";
  return `<fieldset class="layout-fields"${disabled(draftBusy())}><legend>Peg-out selection</legend><label class="field"><span>Selection mode</span><select id="workflow-pegout-mode"><option value="all"${!draft.pegoutLimitEnabled ? " selected" : ""}>All matching peg-outs</option><option value="cumulative"${draft.pegoutLimitEnabled ? " selected" : ""}>Stop at cumulative L-BTC amount</option></select></label>${draft.pegoutLimitEnabled ? `<label class="field"><span>Cumulative L-BTC target</span><input id="workflow-pegout-lbtc-limit" type="text" inputmode="decimal" autocomplete="off" value="${esc(draft.pegoutLbtcLimit)}" placeholder="60"/><small>Positive L-BTC amount with up to 8 decimal places.</small></label><p class="small muted">Count each unique peg-out once, nearest ordinary transaction distance from a selected seed first. Equal distances use transaction ID and output index; named-group hop resets do not change this order. Include the endpoint that reaches or exceeds the target, report any overshoot, then stop.</p><p class="small muted">Hidden or unknown amounts and unidentified assets do not count toward the target. Only known L-BTC values count. Context outputs do not count, and path reachability does not establish how much stolen value reached an endpoint.</p>` : ""}</fieldset>`;
}

function plotEndpointFields(draft: typeof workflowDraft): string {
  if (draft.goal === "connections") return '<p class="artifact-note">Every included transaction shows all its inputs and outputs, including fees. Outputs on excluded branches remain visible without continuing those branches. The transaction CSV includes this complete accounting; extra context does not add starter-pair matches.</p>';
  if (draft.goal !== "pegouts") return "";
  return `<fieldset class="layout-fields"${disabled(draftBusy())}><legend>Additional endpoints</legend>
    <label class="check-line"><input id="workflow-include-unspent" type="checkbox"${draft.includeUnspent ? " checked" : ""}/><span>Include unspent UTXOs</span></label>
    <label class="check-line"><input id="workflow-include-unspendable" type="checkbox"${draft.includeUnspendable ? " checked" : ""}/><span>Include unspendable outputs</span></label>
    <p class="small muted">${draft.pegoutLimitEnabled ? "Matching peg-out requests are included until the cumulative target ends the search." : "Peg-out requests are always included."} Unspent means observed unspent in the selected collection run. Unchecked outputs and outputs stopped only by a hop limit are not counted as unspent. These choices are saved with each generated layout.</p></fieldset>
    <p class="small muted">Peg-out paths ignore attribution CSV hop_limit values. Explicit stop-tracing rules and the selected hop range still apply. If an older collection stopped at an attribution limit, continue collecting before generating the new layout.</p>
    <p class="artifact-note">Every included transaction shows all its inputs and non-fee outputs. Fee outputs follow the Include transaction fee flows setting. Outputs on excluded branches remain visible without continuing those branches. These extra objects do not add endpoint matches or change the peg-out total.</p>`;
}

function savedLayoutSummary(plot: Plot): string {
  const settings = plot.layout_settings;
  if (!settings) return "";
  const contextGrouping = plot.goal === "full" || plot.query?.transaction_io === "complete" || plot.goal === "pegouts" && plot.query?.include_context;
  return `<details class="tool-details"><summary>Saved layout settings</summary><p>${settings.layout_style === "trace" ? "Trace layout" : "Standard layout"} · ${esc(human(settings.connector_style))} connectors · ${esc(settings.layout_attempts)} layout attempts · Attribution arrow colors ${settings.color_attribution_arrows ? "on" : "off"}.</p><p>Centered group: ${esc(settings.center_name || "None")}.${contextGrouping ? ` Isolated context inputs ${settings.group_context_inputs ? "grouped" : "separate"}.` : ""}${plot.goal === "full" || plot.goal === "pegouts" ? ` Fee flows ${settings.include_fees ? "shown" : "hidden"}.` : ""}${plot.goal === "full" || plot.goal === "pegouts" ? ` ${esc(settings.hub_addresses.length)} separate branch hubs.` : ""}</p></details>`;
}

function sharedCollectionPanel(detail: Case): string {
  if (!sectionReady(detail, "shared")) return sectionNotice(detail, "shared");
  const shared = detail.shared_collection;
  const latest = shared?.runs.find(run => run.id === shared.latest_run);
  const members = shared?.members || [];
  const openIds = [...new Set([...state.openCases, detail.id])];
  return `<section class="panel" id="shared-collection-panel"><div class="panel-head"><div><h2>Shared collection</h2><p>Collect the starting outputs from open investigations into reusable transaction evidence.</p></div><span class="badge gray">Workspace data</span></div><div class="panel-body">${shared?.dataset_id ? `<p><strong>${esc(shared.name)}</strong> · ${shared.seed_count} starting outputs · ${members.length} investigations</p><p class="small muted">${esc(members.map(member => member.name).join(", "))}</p>${latest ? `<p>Latest snapshot: <span class="mono">${esc(latest.id)}</span> · <strong>${esc(latest.collected_hops ?? "Not recorded")}</strong> hops collected · ${esc(latest.transaction_count ?? "Unknown")} transactions · ${esc(human(latest.status))}</p>` : '<p class="small muted">No shared collection snapshot yet.</p>'}${shared.policy_case_name ? `<p class="small muted">Last collection rules: ${esc(shared.policy_case_name)}.</p>` : ""}` : '<p>No shared collection yet.</p>'}${shared?.compatible === false ? `<p class="artifact-note">${esc(shared.reason || "This shared collection is incompatible with the investigation.")}</p>` : ""}<p class="small muted">Collect from open investigations starts a new shared run from the ${openIds.length} currently open investigation${openIds.length === 1 ? "" : "s"}. Continue shared data keeps the saved seed set. Both use ${esc(detail.name)}’s collection limits and explicit stop rules. Private investigation runs remain separate.</p><div class="task-actions">${button("Collect from open investigations", "shared-collect-dialog", "play", "", actionBusy("shared-trace") || shared?.compatible === false)}${shared?.latest_run ? button("Continue shared data", "shared-continue-dialog", "refresh", "", actionBusy("shared-trace") || !shared.compatible) : ""}</div></div></section>`;
}

function collectDataPanel(detail: Case, saved: boolean, settings: Settings): string {
  return `<section class="panel" id="collection-panel"><div class="panel-head"><div><h2>Collect transaction data</h2><p>Collect once, then reuse the saved evidence for every plotting goal.</p></div></div><div class="panel-body"><p>Follow your ${detail.seed_count ?? detail.seeds?.length ?? 0} selected starting outputs and save a new run. Continuing extends the latest run’s hop limit. Choose a named group in the collection form to count distance from that group. The collection hop ceiling, explicit tracing stops and any enabled run budgets apply. Attribution CSV hop_limit values are ignored, including zero. Continue an older run to collect branches previously held by those limits; 0 additional hops fills eligible gaps within its existing ceiling.</p><p class="artifact-note">Collection does not generate plots or update Miro. A paused run can be continued here. Plots &amp; Miro uses saved transaction data and current investigation rules. Updating an existing board also reads its current arrangement.</p><p class="small muted">${esc(hopBasis(settings.hop_reference_name))}. ${esc(hopBasisExplanation(settings.hop_reference_name))}</p>${traceSummary(settings)}<div class="task-actions">${button(saved ? "Continue collecting data" : "Collect transaction data", "trace-dialog", "play", "primary", actionBusy("trace"))}</div><hr/><h3>Address activity</h3><p class="small muted">Missing transaction counts are fetched during collection. Retry any unresolved counts here.</p>${button("Fetch address transaction counts", "address-counts", "refresh", "small", !saved || actionBusy("address-counts"))}</div></section>${sharedCollectionPanel(detail)}`;
}

function plotLayoutsPanel(detail: Case, saved: boolean): string {
  const draft = currentWorkflow(detail);
  const plotRun = currentPlotRun(detail);
  saved = Boolean(plotRun);
  const unavailable = !saved || plotRun?.summary_pending || !sectionReady(detail, "workflow") || actionBusy("plot", workflowResourceBody(detail)) || draft.layoutMode === "update" && !updateLayoutBoards(detail, draft.goal).some(board => board.id === draft.layoutBoard);
  const settings = {...defaults, ...detail.run_defaults, ...currentLayoutSettings(detail)};
  return `<section class="panel" id="plot-layouts-panel"><div class="panel-head"><div><h2>Generate a graph preview</h2><p>Choose the goal and layout, review the preview, then write it to Miro.</p></div><span class="badge gray">Saved transactions</span></div><div class="panel-body">${plotDataSourceFields(detail)}<div class="plot-goals" role="group" aria-label="Plotting goal">${plotGoals.map(goal => `<button class="plot-goal${draft.goal === goal.id ? " selected" : ""}" data-action="plot-goal" data-goal="${goal.id}" aria-pressed="${draft.goal === goal.id}"${disabled(draftBusy())}><strong>${goal.name}</strong><span>${goal.description}</span></button>`).join("")}</div>${draft.goal === "pegouts" ? `<div class="field-row"><label class="field"><span>Minimum hops</span><input id="workflow-min-hops" type="number" min="0" max="2147483647" step="1" value="${esc(draft.minHops)}"${disabled(draftBusy())}/></label><label class="field"><span>Maximum hops</span><input id="workflow-max-hops" type="number" min="0" max="2147483647" step="1" value="${esc(draft.maxHops)}"${disabled(draftBusy())}/></label></div>` : ""}${connectionHopFields(draft)}<p class="artifact-note">${draft.goal === "connections" ? `Starter connections searches ${draft.connectionScope === "hop_limited" ? "within the selected connection hop limit" : "all saved paths"} between selected starting transactions. Attribution hop limits and stop-tracing rules are ignored; labels remain visible. No additional transactions are fetched.` : "Plotting makes no blockchain requests. A hop filter cannot reveal data beyond your collection coverage."} ${draft.goal === "connections" ? "Increase the connection hop limit or select All saved connections to search further. Collect more data only if the required transactions are missing." : "If a path is missing, collect more data first and generate another plot."}</p><p class="small muted">${esc(hopBasis(plotRun?.hop_reference_name))}. ${esc(hopBasisExplanation(plotRun?.hop_reference_name))} ${draft.goal === "connections" ? "Named groups affect displayed hop labels only. The connection limit counts ordinary transaction steps from each selected starter." : "Uses the selected collection run’s hop basis with current attribution rules."}</p>${draft.goal === "pegouts" ? '<p class="small muted">Only selected starting UTXOs are followed. A peg-out request does not confirm the separate Bitcoin payout.</p>' : ""}${pegoutLimitFields(draft)}${plotEndpointFields(draft)}${plotDestinationFields(detail)}<form id="plot-layout-form" data-case-id="${esc(detail.id)}"><fieldset class="layout-fields"${disabled(draftBusy())}><legend>Layout settings</legend>${graphFields(settings, true, draft.goal)}</fieldset><p class="small muted">Each generation captures this selected run, investigation rules, and layout settings. Other jobs can continue independently. Use Save layout settings to set the defaults for future layouts.</p><div class="task-actions">${button("Generate preview", "workflow-plot", "graph", "primary", unavailable)}${button("Save layout settings", "plot-settings-save", "check", "", isBusy())}</div><p class="small muted">A new-board preview works offline. An update preview reads Miro to preserve its current arrangement; credentials are retrieved through the launching terminal. Previewing does not write to Miro.</p></form>${!saved ? '<p class="artifact-note">Collect transaction data before generating a plot.</p>' : ""}</div></section>`;
}

function combinedPlotPublication(detail: Case): string {
  const draft = currentWorkflow(detail), plotRun = currentPlotRun(detail), pending = pendingFreshCreation(detail);
  const unavailable = !plotRun || plotRun.summary_pending || !sectionReady(detail, "workflow") || !sectionReady(detail, "boards") ||
    actionBusy("plot-sync", workflowResourceBody(detail)) ||
    (draft.layoutMode === "update" && !updateLayoutBoards(detail, draft.goal).some(board => board.id === draft.layoutBoard)) ||
    (draft.layoutMode === "fresh" && Boolean(pending));
  return `<details class="tool-details"><summary>Advanced: generate and publish in one step</summary><p class="small muted">Generates a new layout using the settings above and writes it to Miro without pausing for preview review. To publish an existing preview without recalculating, use Write to Miro beside that preview.</p>${button(draft.layoutMode === "update" ? "Generate & update board" : "Generate & create board", "workflow-plot-sync", "graph", "", unavailable)}${draft.layoutMode === "fresh" && pending ? `<p class="artifact-note">The creation of ${esc(pending.name)} has an uncertain result. Check Miro and link the created board to its saved entry below before starting another new board.</p>` : ""}</details>`;
}

type SavedPlotWrite = {body?: Record<string, unknown>; board?: InvestigationBoard; notice: string; chooseBoard?: boolean; showName?: boolean};

function savedPlotWrite(detail: Case, plot: Plot): SavedPlotWrite {
  if (plot.validation_pending) return {notice: "Check this saved layout before writing it to Miro."};
  if (!plotCanSync(plot)) return {notice: plot.reason || "Generate a valid preview with matching activity before writing to Miro."};
  if (!sectionReady(detail, "boards")) return {notice: "Waiting for Miro board records before choosing the destination."};
  const draft = currentWorkflow(detail), boards = detail.boards || [];
  const compatible = boards.filter(board => board.can_sync && matchingBoardPlots(detail, board).some(item => item.preview_id === plot.preview_id));
  const board = plot.layout_mode === "update"
    ? boards.find(item => item.id === plot.board_record_id && item.board_id === plot.board_id && item.goal === plot.goal)
    : plot.layout_mode === "fresh" ? boards.find(item => item.creation_preview_id === plot.preview_id && item.goal === plot.goal)
    : boards.find(item => item.status === "synced" && item.preview_id === plot.preview_id) ||
      compatible.find(item => item.id === draft.board) || (compatible.length === 1 ? compatible[0] : undefined);
  if (board) {
    if (board.board_id && board.status === "synced" && board.preview_id === plot.preview_id)
      return {board, notice: "This saved preview is already synced. Open the board to view it, or generate an update preview for changes."};
    if (plot.layout_mode === "fresh" && board.preview_id && board.preview_id !== plot.preview_id)
      return {board, notice: "This board has a later saved layout. Select that layout or generate an update preview; the initial preview will not be reapplied."};
    if (!board.board_id) {
      if (board.status === "creation_rejected") return {board, body: {action: "board-create-sync", preview_id: plot.preview_id, name: board.name}, notice: "Retry the rejected creation using this saved layout and its original board name."};
      return {board, notice: "Board creation has an uncertain result. Link the created board to its saved entry below before resuming; do not create another board."};
    }
    if (!compatible.some(item => item.id === board.id)) return {board, notice: "This board cannot accept this saved layout. Recover its pending operation below or generate a new update preview."};
    return {board, body: {action: "board-sync", record_id: board.id, preview_id: plot.preview_id, reorganize: false},
      notice: interruptedBoard(board) ? `Resume writing this exact preview to ${board.name}. Pending items keep their existing recovery checks.`
        : `Write this exact preview to ${board.name}. Its saved layout will be used without rerunning ELK.`};
  }
  if (plot.layout_mode === "fresh") {
    if (pendingFreshCreation(detail)) return {notice: "Resolve the uncertain board creation below before creating another board."};
    return {body: {action: "board-create-sync", preview_id: plot.preview_id,
      name: draft.savedBoardName.trim() || draft.boardName.trim() || `${detail.name} · ${goalName(plot.goal)}`.slice(0, 60)},
      showName: true, notice: "Create a private Miro board and write this exact saved preview. ELK does not run again."};
  }
  return {notice: plot.layout_mode === "update" ? "The destination captured by this preview is unavailable. Select an available board and generate a new update preview."
    : "Choose a matching managed board below for this earlier saved layout.", chooseBoard: !plot.layout_mode};
}

function savedPlotWriteControls(detail: Case, plot: Plot): string {
  const write = savedPlotWrite(detail, plot), draft = currentWorkflow(detail);
  const busy = write.body ? actionBusy(String(write.body.action), write.body) : draftBusy();
  const synced = write.board?.status === "synced" && write.board.preview_id === plot.preview_id;
  return `${write.showName ? `<label class="field"><span>New Miro board name</span><input id="workflow-preview-board-name" maxlength="60" value="${esc(draft.savedBoardName || draft.boardName)}" placeholder="${esc(`${detail.name} · ${goalName(plot.goal)}`.slice(0, 60))}"${disabled(busy)}/></label>` : ""}<div class="task-actions">${synced ? "" : button("Write to Miro", "workflow-write-miro", "board", "primary", !write.body || busy, `data-preview="${esc(plot.preview_id)}" data-case-id="${esc(detail.id)}"`)}${write.board?.board_id ? `<a class="btn${synced ? " primary" : ""}" href="${esc(boardUrl(write.board.board_id))}" target="_blank" rel="noopener noreferrer">${synced ? "Open synced Miro board" : "Open Miro board"} ${icon("external")}</a>` : ""}${write.chooseBoard ? button("Choose Miro board", "plot-boards", "board", "", draftBusy()) : ""}</div><p class="small muted" role="status">${esc(write.notice)}${write.body ? " Miro credentials are retrieved through the launching terminal." : ""}</p>`;
}

function savedPreviews(detail: Case): Plot[] {
  return [...(detail.plots || [])].sort((left, right) => (right.created_at || "").localeCompare(left.created_at || "") || right.preview_id.localeCompare(left.preview_id));
}

function previewLibraryList(detail: Case): string {
  const selected = currentPlot(detail), library = previewLibraryState(detail.id);
  return `<div class="preview-library" data-case-id="${esc(detail.id)}" role="list" tabindex="0" aria-label="Saved previews">${savedPreviews(detail).map(plot => {
    const preview = safeLocalUrl(plot.artifact?.preview_url);
    const attrs = `data-preview="${esc(plot.preview_id)}" data-case-id="${esc(detail.id)}"`;
    const active = selected?.preview_id === plot.preview_id;
    return `<article class="preview-library-card${active ? " is-selected" : ""}" role="listitem" data-preview-card="${esc(plot.preview_id)}"><div><h3>${esc(previewLabel(plot.preview_number))} · ${esc(goalName(plot.goal))}${active ? ' <span class="badge">Selected</span>' : ""}</h3><p>${esc(plotChoiceLabel(plot))}</p><p>${plot.layout_settings?.layout_style === "trace" ? "Trace layout" : "Standard layout"} · ${esc(plot.node_count)} objects · ${esc(plot.display_edge_count ?? plot.edge_count)} connections</p><p class="mono muted">${esc(plot.preview_id)}</p></div><div class="preview-library-actions">${preview ? `<a class="btn small" href="${esc(preview)}" target="_blank" rel="noopener noreferrer">Open preview ${icon("external")}</a>` : '<span class="small muted">Preview file unavailable</span>'}${button(active ? "Selected for Miro" : "Select for Miro", "preview-select", "board", active ? "small primary" : "small", draftBusy(), `${attrs} aria-pressed="${active}"`)}</div></article>`;
  }).join("")}</div>${library.pageError ? `<p class="artifact-note" role="status">${esc(library.pageError)}</p>` : ""}${detail.plots_next_cursor ? button(library.loadingPage ? "Loading older previews…" : "Load older previews", "preview-load-more", "", "small", library.loadingPage, `data-case-id="${esc(detail.id)}"`) : ""}`;
}

function savedPlotsPanel(detail: Case): string {
  const plot = currentPlot(detail), artifact = plot?.artifact;
  const library = previewLibraryState(detail.id);
  const preview = safeLocalUrl(artifact?.preview_url);
  return `<section class="panel" id="saved-plots-panel"><div class="panel-head"><div><h2>Saved previews</h2><p>Browse earlier layouts, open a preview in a separate tab, and choose one to write to Miro. Browsing does not load or regenerate the graph.</p></div><span class="badge gray">${detail.plots?.length || 0} loaded</span></div><div class="panel-body">${detail.plots_notice ? `<p class="artifact-note" role="status">${esc(detail.plots_notice)}</p>` : ""}${previewLibraryList(detail)}${plot ? `<div class="selected-preview-details" tabindex="-1"><h3>${esc(previewLabel(plot.preview_number))} · Selected for Miro</h3>${plot.preview_number_notice ? `<p class="artifact-note" role="status">${esc(plot.preview_number_notice)}</p>` : ""}<label class="field"><span>Selected preview</span><select id="workflow-plot-picker">${savedPreviews(detail).map(item => `<option value="${esc(item.preview_id)}"${item.preview_id === plot.preview_id ? " selected" : ""}>${esc(plotChoiceLabel(item))}</option>`).join("")}</select></label><p>${esc(goalName(plot.goal))} · Run ${esc(short(plot.run_id, 8))}${esc(plotHopSummary(plot))} · ${plot.node_count} objects · ${plot.display_edge_count ?? plot.edge_count} connections.</p><p class="small muted">${esc(hopBasis(plotHopReference(plot)))}. ${esc(hopBasisExplanation(plotHopReference(plot)))}</p>${plotEndpointSummary(plot)}${plotBoardSummary(plot)}${plot.input_snapshot_version ? `<p class="small muted">Investigation inputs captured${plot.input_snapshot_at ? ` ${esc(formatDate(plot.input_snapshot_at))}` : ""}. Later collection and CSV edits do not change this saved layout.</p>` : ""}${plot.coverage_notice && !plot.notice?.includes(plot.coverage_notice) ? `<p class="artifact-note">${esc(plot.coverage_notice)}</p>` : ""}${plot.notice ? `<p class="artifact-note">${esc(plot.notice)}</p>` : ""}${plot.empty && plot.layout_mode !== "update" ? `<p class="artifact-note">${plot.goal === "connections" ? "No starter connections found within this scope. Increase the connection hop limit or choose All saved connections; collect more only if evidence is missing." : "No matching paths were found in this saved data. Collect more data or review the selected plotting goal and its options."}</p>` : ""}${plot.validation_pending ? plotValidationNotice(detail, plot) : !plot.reviewable ? `<p class="artifact-note">${esc(plot.reason || "No matching paths in this saved data.")}</p>` : ""}${savedLayoutSummary(plot)}<p class="small muted">The saved preview and ELK SVG remain available after publication. Later changes in the generation form do not change this saved preview; generate another preview to include them.</p>${savedPlotWriteControls(detail, plot)}<div class="task-actions">${downloadLink(artifact?.downloads.find(item => item.name === "graph.svg"), "Download ELK SVG")}${detailPagesLink(artifact?.downloads)}${preview ? `<a class="btn small" href="${esc(preview)}" target="_blank" rel="noopener noreferrer">Open preview ${icon("external")}</a>` : ""}</div>${endpointCsvDownloads(plot)}</div>` : currentWorkflow(detail).plot ? `<p class="artifact-note" role="status">${esc(library.selectionError || 'Loading the selected preview’s saved information…')}</p>${library.selectionError ? button('Retry selected preview', 'preview-selection-retry', 'refresh', 'small', library.loadingSelection, `data-case-id="${esc(detail.id)}"`) : ''}` : '<p class="artifact-note">Your generated previews will appear here. All three goals use the same saved collection data.</p>'}</div></section>`;
}

function plotDownloadsPanel(detail: Case): string {
  const plot = currentPlot(detail);
  return `<section class="panel"><div class="panel-head"><div><h2>Plot downloads</h2><p>Download the chart and evidence for any saved plotting goal.</p></div></div><div class="panel-body">${detail.plots_notice ? `<p class="artifact-note">${esc(detail.plots_notice)}</p>` : ""}${plot ? `<label class="field"><span>Saved plot</span><select id="workflow-plot-picker">${(detail.plots || []).map(item => `<option value="${esc(item.preview_id)}"${item.preview_id === plot.preview_id ? " selected" : ""}>${esc(plotChoiceLabel(item))}</option>`).join("")}</select></label><p class="small muted">${esc(hopBasis(plotHopReference(plot)))}. ${esc(hopBasisExplanation(plotHopReference(plot)))}</p>${plotEndpointSummary(plot)}${plotBoardSummary(plot)}${plotEvidenceDownloads(plot)}${plot.validation_pending ? plotValidationNotice(detail, plot) : !plot.reviewable ? `<p class="artifact-note">${esc(plot.reason || "Generate this plot again to refresh its files.")}</p>` : ""}` : `<p class="artifact-note">Generate a saved plot to download its chart and transaction evidence.</p>${button("Choose a plotting goal", "view-plots", "graph")}`}</div></section>`;
}

function fullBoardMaintenance(detail: Case, saved: boolean, artifacts: RunArtifacts, settings: Settings): string {
  return `<section class="panel"><div class="panel-head"><div><h2>Full trace board tools</h2><p>Recovery and layout tools for the original investigation board.</p></div></div><div class="panel-body">${miroRecoveryNotice(detail)}<div class="task-actions">${button("Preview changes", "miro-preview", "search", "", !saved || isBusy())}${button("Create / update Miro frames", "miro-frames-dialog", "layers", "", !saved || Boolean(detail.miro_recovery?.pending_count) || isBusy())}${detail.miro_recovery?.can_recover_frame ? button("Recover interrupted frame", "miro-frame-review", "refresh", "", isBusy()) : ""}${detail.miro_recovery?.can_confirm_empty ? button("Recover empty-board sync", "miro-recover-dialog", "refresh", "", isBusy()) : ""}</div><p class="small muted">When the graph is finished, create its frames separately. Run this again after further syncing or rearranging to update the frames.</p><details class="tool-details"><summary>Board maintenance</summary><div class="panel-body">${button("Merge duplicate addresses", "address-merge-dialog", "graph", "", isBusy() || !saved)}${rebuildAction(detail, saved)}</div></details></div></section><details class="tool-details"><summary>Full trace layout and compaction</summary>${elkGraph(artifacts.elk, saved, settings)}${compactGraph(artifacts.compact, saved, detail)}</details>`;
}

function matchingBoardPlots(detail: Case, board: InvestigationBoard | undefined): Plot[] {
  if (!board) return [];
  return (detail.plots || []).filter(plot => {
    if (plot.goal !== board.goal || !plot.validation_pending && !plotCanSync(plot)) return false;
    if (interruptedBoard(board)) return plot.preview_id === board.preview_id &&
      (plot.layout_mode !== "update" || plot.board_record_id === board.id && plot.board_id === board.board_id) &&
      (plot.layout_mode !== "fresh" || board.creation_preview_id === plot.preview_id);
    if (plot.layout_mode === "update") return plot.board_record_id === board.id && plot.board_id === board.board_id;
    if (plot.layout_mode === "fresh") return board.creation_preview_id === plot.preview_id && board.status !== "synced";
    return !plot.layout_mode; // Earlier saved layouts retain their original sync controls.
  });
}

function boardCard(detail: Case, board: InvestigationBoard, saved: boolean, artifacts: RunArtifacts, settings: Settings): string {
  const draft = currentBoardDraft(detail, board), plots = matchingBoardPlots(detail, board);
  const chosen = chosenBoardPlot(detail, board);
  const captured = detail.plots?.find(plot => plot.preview_id === board.preview_id);
  const focused = currentWorkflow(detail).board === board.id;
  const attrs = `data-record="${esc(board.id)}" data-case-id="${esc(detail.id)}"`;
  const boardBusy = actionBusy("board-sync", {record_id: board.id, preview_id: chosen?.preview_id});
  const locked = boardBusy || !board.can_sync || !chosen || !plotCanSync(chosen);
  const original = Boolean(detail.miro_board && board.board_id === detail.miro_board && board.goal === "full");
  const savedLabel = interruptedBoard(board) ? "Saved layout to resume" : "Saved layout";
  const legacy = Boolean(chosen && !chosen.layout_mode);
  const rejected = board.status === "creation_rejected";
  const creationPlot = detail.plots?.find(plot => plot.preview_id === board.creation_preview_id);
  return `<article class="board-card${focused ? " is-target" : ""}" data-board-record="${esc(board.id)}" tabindex="-1"><header class="board-card-head"><div><h3>${esc(board.name || goalName(board.goal))}</h3><p>${esc(goalName(board.goal))} · ${esc(human(board.status))}${board.legacy_snapshot ? " · Archived snapshot" : ""}</p></div>${board.board_id ? `<a class="board-link" href="${esc(boardUrl(board.board_id))}" target="_blank" rel="noopener noreferrer">Open Miro board ${icon("external")}</a>` : ""}</header><div class="board-card-body">${board.notice ? `<p class="artifact-note">${esc(board.notice)}</p>` : ""}<p class="board-saved-layout"><strong>${savedLabel}:</strong> ${captured ? esc(plotChoiceLabel(captured)) : board.preview_id ? `Saved plot ${esc(short(board.preview_id, 12))} · unavailable` : board.run_id ? `Run ${esc(short(board.run_id, 8))}` : "Not synced yet"}</p>${!board.board_id && rejected ? `<p class="artifact-note">Miro rejected the board creation. Retry with the saved layout after resolving the error.</p>${button("Retry saved board creation", "workflow-board-retry-create", "refresh", "primary", actionBusy("board-create-sync", {preview_id: creationPlot?.preview_id}) || !creationPlot || !plotCanSync(creationPlot), attrs)}` : !board.board_id ? `<label class="field"><span>Created board URL or ID</span><input id="workflow-recovery-url-${esc(board.id)}" data-board-field="recoveryUrl" ${attrs} maxlength="512" value="${esc(draft.recoveryUrl)}"${disabled(boardBusy)}/><small>If creation was interrupted, locate the board in Miro and link it to this saved entry.</small></label>${button("Link created board to this entry", "workflow-board-recover", "board", "", isBusy(), attrs)}` : ""}${board.can_sync ? `<div class="task-actions">${button("Update this board", "workflow-board-prepare", "graph", "", !(saved || detail.shared_collection?.compatible && detail.shared_collection.latest_run) || actionBusy("plot-sync", {layout_mode: "update", board_record_id: board.id}) || !board.board_id || interruptedBoard(board), attrs)}</div><details class="tool-details"${interruptedBoard(board) || focused && chosen && (board.preview_id !== chosen.preview_id || board.status !== "synced") ? " open" : ""}><summary>${interruptedBoard(board) ? "Resume saved board operation" : "Saved layout sync and recovery"}</summary><label class="field"><span>Plot layout to sync · ${esc(goalName(board.goal))}</span><select id="workflow-board-plot-${esc(board.id)}" data-board-field="plot" ${attrs}${disabled(!plots.length || interruptedBoard(board) || draftBusy())}>${!chosen ? '<option value="" selected>Choose a saved plot layout for this board</option>' : ""}${plots.length ? plots.map(plot => `<option value="${esc(plot.preview_id)}"${plot.preview_id === chosen?.preview_id ? " selected" : ""}>${esc(plotChoiceLabel(plot))}</option>`).join("") : '<option value="">Choose Update this board to generate its next preview.</option>'}</select></label>${plotEndpointSummary(chosen)}${plotBoardSummary(chosen)}${chosen?.validation_pending ? plotValidationNotice(detail, chosen) : ""}${legacy ? '<details class="tool-details"><summary>Earlier layout sync controls</summary>' : ""}<div class="task-actions">${button(interruptedBoard(board) ? "Resume board sync" : legacy ? "Sync to Miro" : chosen?.layout_mode === "fresh" ? "Finish create & sync" : "Apply saved update", "workflow-board-sync", "refresh", "primary", locked, attrs)}${legacy ? button("Sync and reorganize", "workflow-board-organize", "graph", "", locked, attrs) : ""}</div>${legacy ? '<p class="small muted">These controls retain the behavior of earlier layouts. Sync keeps existing positions. Sync and reorganize applies the earlier full layout. Prepare a board update to use the current workflow.</p></details>' : chosen?.layout_mode === "fresh" ? '<p class="small muted">Finish publishing this saved layout to its previously created board. The existing board entry is reused.</p>' : '<p class="small muted">Update board applies the reviewed additions, removals and appearance changes. Existing objects retain their positions; new objects are arranged separately for you to merge.</p>'}${interruptedBoard(board) ? '<p class="artifact-note">This board has an interrupted operation. Its saved plot is locked until the operation is recovered or completed. Retry to resume; uncertain item creation may require recovery first.</p>' : ""}</details>` : board.board_id ? '<p class="artifact-note">This saved board is available to view. Create a managed board below to sync new plots.</p>' : ""}</div>${original ? `<details class="board-original-tools"><summary>Original full trace board tools</summary>${fullBoardMaintenance(detail, saved, artifacts, settings)}</details>` : ""}</article>`;
}

function pendingFreshCreation(detail: Case): InvestigationBoard | undefined {
  return (detail.boards || []).find(board => board.status === "pending_creation");
}

function freshBoardPlots(detail: Case): Plot[] {
  return (detail.plots || []).filter(plot => plot.layout_mode === "fresh" && (plot.validation_pending || plotCanSync(plot)) &&
    !(detail.boards || []).some(board => board.creation_preview_id === plot.preview_id &&
      (board.status === "synced" || Boolean(board.preview_id && board.preview_id !== plot.preview_id))));
}

function createBoardPlot(detail: Case): Plot | undefined {
  const plots = freshBoardPlots(detail), selected = currentWorkflow(detail).boardPlot;
  return selected ? plots.find(plot => plot.preview_id === selected) : plots[0];
}

function boardsPanel(detail: Case, saved: boolean, artifacts: RunArtifacts, settings: Settings): string {
  if (!sectionReady(detail, "boards")) return sectionNotice(detail, "boards");
  const draft = currentWorkflow(detail), boards = detail.boards || [];
  const requestedPlot = detail.plots?.find(plot => plot.preview_id === draft.boardPlot);
  const target = requestedPlot && boards.find(board => board.id === draft.board && board.can_sync &&
    chosenBoardPlot(detail, board)?.preview_id === requestedPlot.preview_id);
  const hasOriginal = boards.some(board => board.board_id === detail.miro_board && board.goal === "full");
  const freshPlots = freshBoardPlots(detail), createPlot = createBoardPlot(detail);
  return `<section class="panel" id="miro-boards-panel"><div class="panel-head"><div><h2>Miro boards</h2><p>Each board keeps its own saved layout and update controls.</p></div><span class="badge gray">${boards.length} boards</span></div><div class="panel-body">${requestedPlot ? `<p class="artifact-note">Selected output: ${esc(goalName(requestedPlot.goal))} · Run ${esc(short(requestedPlot.run_id, 8))}${esc(plotHopSummary(requestedPlot))}${esc(plotEndpointLabel(requestedPlot))}. ${target ? `Ready in ${esc(target.name || goalName(target.goal))} below.` : requestedPlot.layout_mode === "fresh" ? "Ready to create and sync a new board below." : "Choose its matching board below. Existing boards keep their own layouts."}</p>` : ""}${detail.boards_notice ? `<p class="artifact-note" role="status">${esc(detail.boards_notice)}</p>` : ""}${boards.length ? `<div class="board-list" role="group" aria-label="Investigation boards">${boards.map(board => boardCard(detail, board, saved, artifacts, settings)).join("")}</div>` : '<p class="artifact-note">No boards yet. Generate a preview above and choose Write to Miro, or link an existing board below.</p>'}</div></section>
  <details class="panel" id="create-sync-board-panel"${requestedPlot?.layout_mode === "fresh" ? " open" : ""}><summary class="panel-head">Publish a saved new-board layout</summary><div class="panel-body"><p class="small muted">Use a saved preview or resume an interrupted publication without generating another layout.</p><label class="field"><span>New board layout</span><select id="workflow-create-plot"${disabled(!freshPlots.length || draftBusy())}>${!createPlot ? '<option value="" selected>Choose a reviewed new-board layout</option>' : ""}${freshPlots.map(plot => `<option value="${esc(plot.preview_id)}"${plot.preview_id === createPlot?.preview_id ? " selected" : ""}>${esc(plotChoiceLabel(plot))}</option>`).join("")}</select></label>${!freshPlots.length ? '<p class="artifact-note">No saved new-board layouts yet. Generate a graph above.</p>' : ""}${plotEndpointSummary(createPlot)}${createPlot?.validation_pending ? plotValidationNotice(detail, createPlot) : ""}<label class="field"><span>Board name</span><input id="workflow-saved-board-name" maxlength="60" value="${esc(draft.savedBoardName)}" placeholder="${esc(`${detail.name} · ${goalName(createPlot?.goal || draft.boardGoal)}`.slice(0, 60))}"${disabled(draftBusy())}/></label><div class="task-actions">${button("Publish saved layout", "workflow-board-create-sync", "plus", "", !createPlot || !plotCanSync(createPlot) || actionBusy("board-create-sync", {preview_id: createPlot?.preview_id}))}</div><p class="small muted">Creates a private board and syncs the exact saved layout. Credentials are retrieved through the launching terminal.</p></div></details><details class="panel"><summary class="panel-head">Link an existing Miro board</summary><div class="panel-body"><p class="small muted">Register an existing board, then prepare an update so its current arrangement is read before additions are placed.</p><label class="field"><span>Plotting goal</span><select id="workflow-board-goal"${disabled(isBusy())}>${plotGoals.map(goal => `<option value="${goal.id}"${draft.boardGoal === goal.id ? " selected" : ""}>${goal.name}</option>`).join("")}</select></label><label class="field"><span>Board name</span><input id="workflow-link-board-name" maxlength="60" value="${esc(draft.linkedBoardName)}" placeholder="${esc(`${detail.name} · ${goalName(draft.boardGoal)}`.slice(0, 60))}"${disabled(isBusy())}/></label><label class="field"><span>Board URL or ID</span><input id="workflow-board-url" maxlength="512" value="${esc(draft.boardUrl)}" placeholder="https://miro.com/app/board/…"${disabled(isBusy())}/></label>${button("Link existing board", "workflow-board-link", "board", "", isBusy())}</div></details>${!hasOriginal && detail.miro_board ? `<section class="panel"><div class="panel-head"><div><h2>Original full trace board</h2><p>Tools for the investigation’s original board.</p></div><a class="board-link" href="${esc(boardUrl(detail.miro_board))}" target="_blank" rel="noopener noreferrer">Open Miro board ${icon("external")}</a></div>${fullBoardMaintenance(detail, saved, artifacts, settings)}</section>` : ""}`;
}

function workflowInput(element: HTMLInputElement | HTMLSelectElement): boolean {
  if (!element.id?.startsWith("workflow-") || !state.activeCase || draftBusy()) return false;
  const detail = state.activeCase, draft = currentWorkflow(detail);
  viewRevision++;
  if (element.dataset?.boardField) {
    if (element.dataset.boardField === "recoveryUrl" && isBusy()) return false;
    const board = boardFromControl(detail, element);
    if (!board) return false;
    const boardDraft = currentBoardDraft(detail, board);
    if (element.dataset.boardField === "plot") {
      if (!board.can_sync || interruptedBoard(board)) return false;
      boardDraft.plot = element.value;
      void verifySelectedPlot(detail.id, element.value);
      draft.board = board.id;
      draft.boardPlot = "";
      render();
    } else if (element.dataset.boardField === "recoveryUrl" && !board.board_id) boardDraft.recoveryUrl = element.value;
    else return false;
    return true;
  }
  const fields: Record<string, "minHops" | "maxHops" | "connectionMaxHops" | "pegoutLbtcLimit" | "plot" | "boardName" | "savedBoardName" | "linkedBoardName" | "boardUrl"> = {"workflow-min-hops": "minHops", "workflow-max-hops": "maxHops", "workflow-connection-max-hops": "connectionMaxHops", "workflow-pegout-lbtc-limit": "pegoutLbtcLimit", "workflow-plot-picker": "plot", "workflow-board-name": "boardName", "workflow-saved-board-name": "savedBoardName", "workflow-preview-board-name": "savedBoardName", "workflow-link-board-name": "linkedBoardName", "workflow-board-url": "boardUrl"};
  if (element.id === "workflow-data-source" && ["investigation", "shared"].includes(element.value)) {
    savePlotLayoutDraft();
    draft.dataSource = element.value as "investigation" | "shared";
    if (draft.dataSource === "shared" && detail.shared_collection?.compatible) {
      const shared = detail.shared_collection;
      if (draft.datasetId !== shared.dataset_id || !shared.runs.some(run => run.id === draft.sharedRun)) {
        draft.datasetId = shared.dataset_id || ""; draft.sharedRun = shared.latest_run || "";
      }
      if (detail.sections) void loadCaseSection(detail.id, "shared");
    }
    render();
  } else if (element.id === "workflow-pegout-mode" && ["all", "cumulative"].includes(element.value)) {
    savePlotLayoutDraft();
    draft.pegoutLimitEnabled = element.value === "cumulative";
    render();
  } else if (element.id === "workflow-connection-scope" && ["hop_limited", "all_saved"].includes(element.value)) {
    savePlotLayoutDraft();
    draft.connectionScope = element.value as ConnectionScope;
    render();
  } else if (element.id === "workflow-shared-run") {
    const shared = detail.shared_collection;
    if (shared?.compatible && shared.runs.some(run => run.id === element.value)) {
      draft.sharedRun = element.value; draft.datasetId = shared.dataset_id || "";
      if (detail.sections) void loadCaseSection(detail.id, "shared", true);
    }
    render();
  } else if (element.id === "workflow-layout-mode" && ["fresh", "update"].includes(element.value)) {
    selectLayoutDestination(detail, element.value as PlotLayoutMode, element.value === "update"
      ? updateLayoutBoards(detail, draft.goal).find(board => board.id === draft.layoutBoard) : undefined);
    render();
  } else if (element.id === "workflow-layout-board") {
    selectLayoutDestination(detail, "update", updateLayoutBoards(detail, draft.goal).find(board => board.id === element.value));
    render();
  }
  else if (element.id === "workflow-create-plot") {draft.boardPlot = element.value; void verifySelectedPlot(detail.id, element.value); render();}
  else if (element.id === "workflow-board-goal" && plotGoals.some(goal => goal.id === element.value)) draft.boardGoal = element.value as PlotGoal;
  else if (element.id === "workflow-include-unspent") draft.includeUnspent = (element as HTMLInputElement).checked;
  else if (element.id === "workflow-include-unspendable") draft.includeUnspendable = (element as HTMLInputElement).checked;
  else if (fields[element.id]) draft[fields[element.id]] = element.value;
  else return false;
  if (element.id === "workflow-plot-picker") persistInvestigationTabs();
  if (["workflow-plot-picker", "workflow-board-goal"].includes(element.id)) render();
  return true;
}

async function workflowAction(action: string, element?: HTMLElement): Promise<boolean> {
  if (!["plot-goal", "workflow-plot", "workflow-plot-sync", "plot-settings-save", "plot-boards", "workflow-write-miro", "workflow-board-create-sync", "workflow-board-retry-create", "workflow-board-prepare", "workflow-board-link", "workflow-board-sync", "workflow-board-organize", "workflow-board-recover"].includes(action)) return false;
  const detail = state.activeCase;
  if (!detail || draftBusy()) return true;
  if ((action.startsWith("workflow-board-") || action === "plot-boards") && !sectionReady(detail, "boards")) throw new Error("Wait for Miro board records to finish loading.");
  if (["plot-settings-save", "workflow-board-link", "workflow-board-recover"].includes(action) && isBusy()) return true;
  viewRevision++;
  const draft = currentWorkflow(detail);
  if (action === "plot-settings-save") {
    if (await persistPlotLayoutSettings(detail)) toast("Layout settings saved for this investigation.");
    return true;
  }
  if (action === "plot-goal") {
    saveDestinationDraft(detail);
    if (plotGoals.some(goal => goal.id === element?.dataset.goal)) draft.goal = element!.dataset.goal as PlotGoal;
    if (!updateLayoutBoards(detail, draft.goal).some(board => board.id === draft.layoutBoard)) draft.layoutBoard = "";
    render(); return true;
  }
  if (action === "workflow-board-prepare") {
    const board = boardFromControl(detail, element);
    if (!board?.can_sync || !board.board_id || interruptedBoard(board)) throw new Error("Select a managed board with no interrupted operation before preparing an update.");
    selectLayoutDestination(detail, "update", board);
    state.caseView = "plots"; render();
    document.querySelector<HTMLElement>("#plot-layouts-panel")?.scrollIntoView({block: "start"});
    return true;
  }
  if (action === "plot-boards") {
    const plot = currentPlot(detail);
    if (!plot || !plotCanSync(plot)) throw new Error("Select a current saved plot with activity before syncing to Miro.");
    draft.boardPlot = plot.preview_id;
    draft.boardGoal = plot.goal;
    const compatible = (detail.boards || []).filter(board => board.can_sync &&
      matchingBoardPlots(detail, board).some(item => item.preview_id === plot.preview_id));
    const target = compatible.find(board => board.id === draft.board) ||
      compatible.find(board => board.preview_id === plot.preview_id) || compatible[0];
    draft.board = target?.id || "";
    if (target) currentBoardDraft(detail, target).plot = plot.preview_id;
    state.caseView = "plots"; render();
    document.querySelector<HTMLElement>(target ? ".board-card.is-target" : "#create-sync-board-panel")?.scrollIntoView({block: "nearest"});
    return true;
  }
  let body: Record<string, unknown>;
  if (action === "workflow-plot" || action === "workflow-plot-sync") {
    const sourceRun = currentPlotRun(detail);
    if (!sourceRun || sourceRun.summary_pending || !sectionReady(detail, "workflow")) throw new Error("Collect transaction data first, then select an available saved snapshot for this data source.");
    const min = draft.goal === "pegouts" ? draft.minHops.trim() : "0";
    const max = draft.goal === "pegouts" ? draft.maxHops.trim()
      : draft.goal === "connections" && draft.connectionScope === "hop_limited" ? draft.connectionMaxHops.trim() : "0";
    if ([min, max].some(value => !/^\d+$/.test(value) || Number(value) > 2147483647) || Number(min) > Number(max)) throw new Error("Enter whole-number hop limits from 0 to 2147483647, with minimum no greater than maximum.");
    if (action === "workflow-plot-sync" && draft.layoutMode === "fresh" && pendingFreshCreation(detail)) throw new Error("Resolve the uncertain board creation below before creating another board.");
    if (action === "workflow-plot-sync" && !sectionReady(detail, "boards")) throw new Error("Wait for Miro board records to finish loading.");
    body = {action: action === "workflow-plot-sync" ? "plot-sync" : "plot", goal: draft.goal, run_id: sourceRun.id, min_hops: Number(min), max_hops: Number(max), layout_mode: draft.layoutMode};
    if (draft.goal === "connections") body.connection_scope = draft.connectionScope;
    if (draft.dataSource === "shared") {body.data_source = "shared"; body.dataset_id = draft.datasetId;}
    if (draft.layoutMode === "update") {
      const board = updateLayoutBoards(detail, draft.goal).find(item => item.id === draft.layoutBoard);
      if (!board) throw new Error("Select an available board for this plotting goal before generating an update.");
      body.board_record_id = board.id;
    }
    if (body.action === "plot-sync" && draft.layoutMode === "fresh") body.name = draft.boardName.trim() || `${detail.name} · ${goalName(draft.goal)}`.slice(0, 60);
    if (draft.goal === "pegouts") {
      if (draft.pegoutLimitEnabled) {
        const amount = draft.pegoutLbtcLimit.trim();
        if (!/^\d+(?:\.\d{1,8})?$/.test(amount) || !/[1-9]/.test(amount))
          throw new Error("Enter a positive L-BTC target with up to 8 decimal places, without commas or scientific notation.");
        body.pegout_lbtc_limit = amount;
      }
      if (draft.includeUnspent) body.include_unspent = true;
      if (draft.includeUnspendable) body.include_unspendable = true;
    }
    const form = document.querySelector<HTMLFormElement>("#plot-layout-form");
    if (form && !form.reportValidity()) return true;
    savePlotLayoutDraft(form);
    body.layout_settings = currentLayoutSettings(detail);
  } else if (action === "workflow-write-miro") {
    const plot = currentPlot(detail);
    if (!plot || (element && (element.dataset.preview !== plot.preview_id || element.dataset.caseId !== detail.id)))
      throw new Error("The selected preview changed. Review the current preview before writing to Miro.");
    const write = savedPlotWrite(detail, plot);
    if (!write.body) throw new Error(write.notice);
    body = write.body;
  } else if (action === "workflow-board-retry-create") {
    const board = boardFromControl(detail, element);
    const plot = detail.plots?.find(item => item.preview_id === board?.creation_preview_id);
    if (!board || board.status !== "creation_rejected" || board.board_id || !plot || !plotCanSync(plot)) throw new Error("Only a rejected board creation can retry its saved layout. Link an uncertain created board to its original entry first.");
    body = {action: "board-create-sync", preview_id: plot.preview_id, name: board.name};
  } else if (action === "workflow-board-create-sync") {
    const plot = createBoardPlot(detail);
    if (!plot || !plotCanSync(plot)) throw new Error("Select a reviewed New board layout before creating and syncing a board.");
    body = {action: "board-create-sync", preview_id: plot.preview_id, name: draft.savedBoardName.trim() || `${detail.name} · ${goalName(plot.goal)}`.slice(0, 60)};
  } else if (action === "workflow-board-recover") {
    const board = boardFromControl(detail, element);
    if (!board) throw new Error("Choose a board entry from this investigation before recovering it.");
    const recoveryUrl = currentBoardDraft(detail, board).recoveryUrl.trim();
    if (board.board_id || !recoveryUrl) throw new Error("Enter the URL of the board created during the interrupted operation.");
    body = {action: "board-link", record_id: board.id, goal: board.goal, name: board.name, board: recoveryUrl};
  } else if (action === "workflow-board-link") {
    if (!draft.boardUrl.trim()) throw new Error("Enter a Miro board URL or ID to link.");
    body = {action: "board-link", goal: draft.boardGoal, name: draft.linkedBoardName.trim() || `${detail.name} · ${goalName(draft.boardGoal)}`.slice(0, 60), board: draft.boardUrl.trim()};
  } else {
    const board = boardFromControl(detail, element);
    const plot = board ? chosenBoardPlot(detail, board) : undefined;
    if (!board?.can_sync || !plot || !plotCanSync(plot)) throw new Error("Select a managed board and a matching saved plot before syncing.");
    if (action === "workflow-board-organize" && plot.layout_mode) throw new Error("Generate a New board layout to reorganize the full graph, or update this board while preserving existing positions.");
    body = {action: "board-sync", record_id: board.id, preview_id: plot.preview_id, reorganize: action === "workflow-board-organize"};
  }
  await startJob(`/api/cases/${encodeURIComponent(detail.id)}/actions`, body, String(body.action), (body.action === "plot" ? body.layout_mode === "update" : body.action !== "board-link"), detail.id);
  return true;
}

function investigationDataPanel(detail: Case): string {
  return `<section class="panel" id="settings-data"><div class="panel-head"><div><h2>Investigation data</h2><p>Manage attributions, tracing stops, and change outputs here.</p></div></div><div class="panel-body"><div class="task-actions">${button("Import CSV files", "input-import-open", "plus", "primary", isBusy())}${button("Address review", "addresses", "search", "", isBusy())}${button("Change outputs", "change-outputs-open", "graph", "", isBusy())}${button("Assign colors", "name-colors-open", "", "", isBusy())}<a class="btn" href="/api/cases/${esc(encodeURIComponent(detail.id))}/input-exports/all" download>${icon("download")}Export input CSVs</a></div><p class="small muted">Import attributions, name colors, and change outputs together. Export input CSVs downloads all saved attributions, name colors, and change outputs across every page. Unsaved edits are excluded. Imports, address assessments and color changes save separately from the settings form.</p></div></section>${inputImportPanel(detail.id, isBusy())}${changeOutputsPanel(detail.id, isBusy())}`;
}

function seedTransactionsPanel(detail: Case, run?: Run): string {
  const recorded = Array.isArray(run?.seeds);
  const seeds = recorded ? run!.seeds! : Array.isArray(detail.seeds) ? detail.seeds : [];
  const transactions = new Map<string, Set<number>>();
  for (const seed of seeds) {
    const match = typeof seed === "string" ? /^([0-9a-fA-F]{64}):([0-9]+)$/.exec(seed.trim()) : null;
    if (!match) continue;
    const vout = Number(match[2]);
    if (!Number.isInteger(vout) || vout < 0 || vout > 0xffffffff) continue;
    const txid = match[1].toLowerCase();
    if (!transactions.has(txid)) transactions.set(txid, new Set());
    transactions.get(txid)!.add(vout);
  }
  const count = [...transactions.values()].reduce((total, vouts) => total + vouts.size, 0);
  const rows = [...transactions].map(([txid, selected]) => {
    const hash = detail.fixture ? `<span class="mono seed-txid">${esc(txid)}</span>`
      : `<a class="mono seed-txid" href="https://blockstream.info/liquid/tx/${txid}" target="_blank" rel="noopener noreferrer" title="Open transaction in Blockstream Explorer">${esc(txid)}</a>`;
    const vouts = [...selected].sort((a, b) => a - b).map(vout => `<span class="seed-vout">${vout}</span>`).join(" ");
    return `<tr><td>${hash}</td><td><div class="seed-vouts">${vouts}</div></td></tr>`;
  }).join("");
  return `<section class="panel seed-panel" aria-label="Seed transactions"><div class="panel-head"><div><h2>Seed transactions</h2><p>${recorded ? "Starting outputs recorded in this snapshot." : "Selected starting outputs for this investigation."} Vout indexes start at 0.</p></div><span class="badge gray">${transactions.size} transaction${transactions.size === 1 ? "" : "s"} · ${count} selected output${count === 1 ? "" : "s"}</span></div>${rows ? `<div class="table-wrap seed-table-wrap" tabindex="0" aria-label="Seed transaction IDs and selected output indexes"><table class="seed-table"><thead><tr><th scope="col">Transaction ID</th><th scope="col">Selected vout</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<div class="panel-body small muted">No seed outputs are recorded for this ${recorded ? "snapshot" : "investigation"}.</div>`}</section>`;
}

function pegoutLbtcTotal(detail: Case, run?: Run): string {
  const draft = currentWorkflow(detail), shared = draft.dataSource === "shared";
  if (!shared && !run) return "";
  const plot = detail.plots?.filter(item => item.goal === "pegouts" && (shared
    ? item.collection_source?.kind === "shared" && item.collection_source.dataset_id === draft.datasetId && item.collection_source.run_id === draft.sharedRun
    : !item.collection_source && item.run_id === run!.id))
    .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""))[0];
  if (!plot) return "";
  const summary = plot.pegout_lbtc_summary;
  if (!summary) return '<div class="pegout-total"><div><span>Peg-out LBTC total</span><p>Generate a new peg-out trace to calculate the total for this snapshot.</p></div></div>';
  const unknown = summary.unknown_amount_count + summary.unknown_asset_count;
  const amount = unknown && !summary.valued_lbtc_count ? "Unknown" : `${summary.lbtc} LBTC`;
  const notes = [`${summary.pegout_count} unique peg-out${summary.pegout_count === 1 ? "" : "s"}`];
  if (unknown) notes.push(`Known amounts only: ${summary.valued_lbtc_count} valued in LBTC`);
  if (summary.unknown_amount_count) notes.push(`${summary.unknown_amount_count} hidden or unavailable amount${summary.unknown_amount_count === 1 ? "" : "s"}`);
  if (summary.unknown_asset_count) notes.push(`${summary.unknown_asset_count} unidentified asset${summary.unknown_asset_count === 1 ? "" : "s"}`);
  if (summary.non_lbtc_count) notes.push(`${summary.non_lbtc_count} non-LBTC output${summary.non_lbtc_count === 1 ? "" : "s"} excluded`);
  const endpoints = plot.artifact?.downloads.find(item => item.name === "endpoints.csv")
    || plot.artifact?.downloads.find(item => item.name === "trace-endpoints.csv");
  return `<div class="pegout-total" role="group" aria-label="Peg-out LBTC total"><div><span>Peg-out LBTC total</span><strong>${esc(amount)}</strong><p>${esc(notes.join(" · "))}.</p><p class="small muted">${shared ? `Latest peg-out trace from shared snapshot ${esc(draft.sharedRun)}` : "Latest peg-out trace for this snapshot"}${plot.max_hops != null ? ` · Hops ${esc(plot.min_hops)}–${esc(plot.max_hops)}` : ""}${plot.created_at ? ` · ${esc(formatDate(plot.created_at))}` : ""}. Sums full endpoint values in the saved trace.${plot.query?.pegout_lbtc_limit ? ` Cumulative target: ${esc(plot.query.pegout_lbtc_limit)} L-BTC.` : ""}</p></div>${downloadLink(endpoints, "Endpoint CSV", "small")}</div>`;
}

function workspace(): string {
  const detail = state.activeCase;
  if (!detail)
    return '<div class="empty-state"><h2>Loading investigation…</h2></div>';
  const run = currentRun();
  const saved = !!detail.latest_run;
  const key = resultKey(detail.id);
  const cachedArtifacts = state.artifacts.get(key);
  const savedArtifacts = run ? detail.artifacts?.[run.id] : undefined;
  const artifacts = { ...cachedArtifacts, ...savedArtifacts, compact: savedArtifacts?.compact };
  const last = state.results.get(detail.id);
  const settings = { ...defaults, ...detail.run_defaults };
  const runOptions = (detail.runs || [])
    .map(
      (item) =>
        `<option value="${esc(item.id)}"${(state.selectedRun === "latest" ? detail.latest_run : state.selectedRun) === item.id ? " selected" : ""}>${esc(item.id)}${item.id === detail.latest_run ? " · Latest" : ""}</option>`,
    )
    .join("");

  const views: [CaseView, string][] = [["collect", "Collect data"], ["plots", "Plots & Miro"], ["history", "History & downloads"]];

  let content = ["plots", "boards"].includes(state.caseView) ? `${savedPlotsPanel(detail)}${plotLayoutsPanel(detail, saved)}${combinedPlotPublication(detail)}${boardsPanel(detail, saved, artifacts, settings)}`
    : state.caseView === "history" ? `<section class="panel"><div class="panel-head"><div><h2>Run history</h2><p>Every continuation preserves the preceding snapshot.</p></div><span class="badge gray">${(detail.runs || []).length} runs</span></div>${detail.runs?.length ? `<div class="table-wrap"><table class="run-list"><thead><tr><th>Run</th><th>Recorded</th><th>Status</th><th>Hop limit</th><th>Counted from</th><th>Transactions</th></tr></thead><tbody>${detail.runs.map((item) => `<tr class="${item.id === run?.id ? "selected" : ""}"><td><button data-run="${esc(item.id)}">${esc(short(item.id, 8))}</button>${item.id === detail.latest_run ? '<div class="muted">Latest</div>' : ""}</td><td><span class="muted">${esc(formatDate(item.created_at))}</span></td><td><span class="badge ${item.status === "error" ? "red" : "gray"}">${esc(human(item.status))}</span></td><td>${esc(item.max_hops ?? "Not recorded")}</td><td>${esc(item.hop_reference_name || "Starting transactions")}</td><td>${esc(item.transaction_count ?? "—")}</td></tr>`).join("")}</tbody></table></div>` : '<div class="panel-body small muted">Your first completed or bounded run will appear here.</div>'}</section>${plotDownloadsPanel(detail)}${csvDownloads(artifacts.csv, saved, settings.include_fees)}${localGraph(artifacts.mermaid, saved, settings)}${detail.pegout_searches?.length ? `<details class="tool-details"><summary>Earlier standalone peg-out searches</summary>${pegoutsGraph(detail)}</details>` : ""}${artifacts.connections ? `<details class="tool-details"><summary>Earlier starter-connection snapshot</summary>${connectionsGraph(artifacts.connections, saved)}</details>` : ""}`
    : collectDataPanel(detail, saved, settings);
  if (["plots", "boards"].includes(state.caseView) && !sectionReady(detail, "workflow")) content = sectionNotice(detail, "workflow");
  if (state.caseView === "history" && !sectionReady(detail, "history")) content = sectionNotice(detail, "history")
    + (sectionReady(detail, "workflow") ? plotDownloadsPanel(detail) : sectionNotice(detail, "workflow"));
  if (state.caseView === "collect" && !sectionReady(detail, "collection") && (!run || run.summary_pending)) content = sectionNotice(detail, "collection") + sharedCollectionPanel(detail);
  if (["plots", "boards"].includes(state.caseView) && !sectionReady(detail, "shared")) content += sectionNotice(detail, "shared");
  return `<div class="page-heading case-heading"><div><div class="eyebrow">Investigation workspace</div><h1 id="page-title" tabindex="-1">${esc(detail.name)}</h1><div class="workspace-meta"><span class="badge ${detail.fixture ? "purple" : ""}">${detail.fixture ? "Synthetic data" : "Live Liquid"}</span><span class="badge gray">${sectionReady(detail, "collection") ? `${(detail.runs || []).length} saved runs` : "Loading saved runs"}</span></div></div><div class="heading-actions">${button("Investigation settings", "case-settings", "settings", "", isBusy())}</div></div>
    <nav class="case-navigation" aria-label="Investigation tools">${views.map(([view, label]) => `<button type="button" class="case-nav${(state.caseView === view || view === "plots" && state.caseView === "boards") ? " active" : ""}" data-action="view-${view}"${state.caseView === view || view === "plots" && state.caseView === "boards" ? ' aria-current="page"' : ""}>${label}</button>`).join("")}</nav>
    ${last ? resultBanner(last.action, last.result) : ""}
    <section class="panel"><div class="panel-head"><div><h2>${saved ? "Collected data" : "Ready to collect"}</h2><p>${saved ? "Choose the collected data used for plots and downloads." : "Your starting outputs and limits are saved."}</p></div>${saved ? `<label class="run-picker">Snapshot<select class="input" id="run-picker" aria-label="Saved run snapshot">${runOptions}</select></label>` : '<span class="badge gray">No runs yet</span>'}</div>${saved ? `<div class="run-summary"><div><span>Hops collected</span><strong${run?.collected_hops === undefined ? ' class="text-value"' : ""}>${esc(run?.summary_pending ? "Loading" : run?.collected_hops ?? "Not recorded")}</strong><span>${run?.hop_reference_name ? "Deepest collected group-relative hop" : "Deepest saved transaction hop"}</span></div><div><span>Tracked transactions</span><strong>${esc(run?.summary_pending ? "Loading" : run?.transaction_count ?? "—")}</strong></div><div><span>Unfinished branches</span><strong>${esc(run?.summary_pending ? "Loading" : run?.frontier_count ?? "—")}</strong></div><div><span>Run status</span><strong class="text-value">${esc(run?.summary_pending ? "Loading" : human(run?.status || "saved"))}</strong></div></div><div class="run-note">${icon("clock")}<span>${esc(formatDate(run?.created_at))}${run?.max_hops !== undefined ? ` · Collection hop limit: ${run.max_hops}` : ""}${run?.stop_reason ? ` · ${esc(human(run.stop_reason))}` : ""} · ${esc(hopBasis(run?.hop_reference_name))}.${run?.collected_hops !== undefined ? ` ${esc(hopBasisExplanation(run?.hop_reference_name))}` : ""}</span></div>${collectionPerformancePanel(run?.performance)}` : `<div class="empty-state" style="padding:31px 24px"><div class="empty-icon">${icon("graph")}</div><h2>${detail.seed_count ?? detail.seeds?.length ?? "Your"} starting output${(detail.seed_count ?? detail.seeds?.length) === 1 ? "" : "s"} selected</h2><p>Collect data first, or choose an available shared collection in Plots &amp; Miro.</p></div>`}${sectionReady(detail, "workflow") ? pegoutLbtcTotal(detail, run) : ""}</section>
    ${seedTransactionsPanel(detail, ["plots", "boards"].includes(state.caseView) && currentWorkflow(detail).dataSource === "shared" ? undefined : run)}
    <div class="workspace-content">${content}</div>`;
}

function saveAddressDraft(): void {
  const form = document.querySelector<HTMLFormElement>("#service-form");
  if (!form) return;
  const data = new FormData(form);
  state.addressReview.name = String(data.get("service_name") || "");
  state.addressReview.notes = String(data.get("service_notes") || "");
  state.addressReview.enabled = data.get("service_enabled") === "on";
  state.addressReview.confidence = String(data.get("service_confidence") || "suspected");
  state.addressReview.source = String(data.get("service_source") || "");
  state.addressReview.observedAt = String(data.get("service_observed") || "");
  state.addressReview.stopTracing = data.get("service_stop") === "on";
  state.addressReview.hopLimit = String(data.get("service_hop_limit") ?? "");
}

function selectAddress(row: AddressRow): void {
  state.addressReview.selected = row;
  state.addressReview.name = row.service?.name || "";
  state.addressReview.notes = row.service?.notes || "";
  state.addressReview.enabled = row.service?.enabled === true;
  state.addressReview.confidence = row.service?.confidence || "suspected";
  state.addressReview.source = row.service?.source || "Investigator designation";
  state.addressReview.observedAt = row.service?.observed_at || "";
  state.addressReview.stopTracing = row.service?.stop_tracing ?? true;
  state.addressReview.hopLimit = row.service?.hop_limit == null ? "" : String(row.service.hop_limit);
}

async function loadAddresses(offset: number): Promise<void> {
  const detail = state.activeCase;
  if (!detail || isBusy()) return;
  saveSettingsDraft(); saveAddressDraft();
  cancelCaseOpening();
  const generation = ++pageGeneration;
  state.page = "addresses";
  state.error = "";
  state.addressReview.loading = true;
  history.replaceState(null, "", `#case/${encodeURIComponent(detail.id)}`);
  render();
  try {
    const result = await api<AddressPage>(`/api/cases/${encodeURIComponent(detail.id)}/addresses`, {
      run_id: state.selectedRun, query: state.addressReview.query, offset, limit: 25,
      suspected_only: state.addressReview.suspectedOnly,
    });
    if (generation !== pageGeneration) return;
    state.addressReview.data = result;
  } finally {
    if (generation === pageGeneration) {
      state.addressReview.loading = false;
      render();
    }
  }
}

async function saveService(enabled?: boolean): Promise<void> {
  const detail = state.activeCase, review = state.addressReview;
  if (!detail || !review.selected || isBusy()) return;
  saveAddressDraft();
  const address = review.selected.address;
  const generation = pageGeneration;
  submitting = true;
  render();
  try {
    const result = await api<{ service: ServiceRule; revision: number }>(`/api/cases/${encodeURIComponent(detail.id)}/services`, {
      address, name: review.name, notes: review.notes, enabled: enabled ?? review.enabled,
      confidence: review.confidence, source: review.source,
      observed_at: review.observedAt, stop_tracing: review.stopTracing, hop_limit: review.hopLimit,
    });
    if (generation !== pageGeneration || state.activeCase?.id !== detail.id) return;
    if (review.selected?.address === address) selectAddress({ ...review.selected, service: result.service });
    const row = review.data?.rows.find(item => item.address === address);
    if (row) row.service = result.service;
    toast(result.service.enabled ? (result.service.stop_tracing === false ? "Assessment and display hop limit saved. Collection and peg-out paths ignore this limit; explicit tracing stops still apply." : "Service stop saved. It applies to the first/next run.") : "Assessment disabled. Its history remains saved.");
  } finally {
    submitting = false;
    render();
  }
}

function addressActivity(summary: AddressActivity | null): string {
  if (!summary) return '<p class="address-note">No activity lookup has been saved for this address. Refresh activity to retrieve its counts and a bounded history.</p>';
  const first = summary.history_complete ? summary.first_confirmed_activity : summary.oldest_observed_confirmed_activity;
  const activityDate = (activity: AddressActivity["first_confirmed_activity"]): string =>
    activity?.date_utc ? esc(formatDate(activity.date_utc)) : "Unavailable";
  const delta = summary.mempool_unspent_output_delta;
  return `<div class="address-stats">
    <div><span>Confirmed transactions</span><strong>${esc(summary.confirmed_tx_count ?? "??")}</strong></div>
    <div><span>Mempool transactions</span><strong>${esc(summary.mempool_tx_count ?? "??")}</strong></div>
    <div><span>General unspent outputs</span><strong>${esc(summary.unspent_output_count ?? "??")}</strong></div>
    <div><span>Confirmed unspent outputs</span><strong>${esc(summary.confirmed_unspent_output_count ?? "??")}</strong></div>
    <div><span>Mempool output change</span><strong>${delta === null ? "??" : esc(delta > 0 ? `+${delta}` : delta)}</strong></div>
  </div><p class="address-note">General unspent outputs cover all indexed assets at this address, including outputs outside this trace. They are not an L-BTC balance. The total includes the mempool change and may change before confirmation. These are counts, not asset values.</p>
  <dl class="address-dates"><div><dt>${summary.history_complete ? "First confirmed activity" : "Oldest activity in reviewed history"}</dt><dd>${activityDate(first)}</dd></div>
    <div><dt>Latest confirmed activity</dt><dd>${activityDate(summary.latest_confirmed_activity)}</dd></div>
    <div><dt>Statistics observed</dt><dd>${summary.observed_at ? esc(formatDate(summary.observed_at)) : "Unavailable"}</dd></div></dl>
  <p class="address-note"><strong>${summary.history_complete ? "Confirmed history complete." : "Partial confirmed history."}</strong> Reviewed ${esc(summary.history_transactions_seen)} transactions across ${esc(summary.history_pages)} pages.${summary.history_complete ? " These dates describe confirmed on-chain activity, not address creation." : ` The oldest reviewed date is not the address's first use. Stopped: ${esc(human(summary.history_stop_reason))}.`}</p>
  <p class="address-note">Statistics and history are observed separately and may change during lookup.</p>
  ${summary.warnings.map(warning => `<p class="address-note warning">${esc(warning)}</p>`).join("")}`;
}

function addressReviewPage(): string {
  const detail = state.activeCase;
  if (!detail) return dashboard();
  const review = state.addressReview, data = review.data, selected = review.selected;
  const busy = isBusy() || review.loading;
  const options = (detail.runs || []).map(run => `<option value="${esc(run.id)}"${run.id === (state.selectedRun === "latest" ? detail.latest_run : state.selectedRun) ? " selected" : ""}>${esc(run.id)}${run.id === detail.latest_run ? " · Latest" : ""}</option>`).join("");
  return `<div class="page-heading"><div><div class="eyebrow">${esc(detail.name)}</div><h1 id="page-title" tabindex="-1">Address review</h1><p>Review activity, record your assessment, and choose where future tracing stops.</p></div><div class="heading-actions">${button("Import CSV files", "input-import-open", "", "", busy)}${button("Assign colors", "name-colors-open", "", "", busy)}${button("Back to investigation settings", "case-settings", "arrow")}</div></div>
    <details class="panel" id="advanced-attributions"><summary class="panel-body">Advanced imports: pasted address lists or JSON</summary>${addressImportPanel(detail.id, busy)}</details>
    <div class="address-review-grid"><section class="panel"><div class="panel-head"><div><h2>Saved addresses</h2><p>Selected run, saved reviews, and service assessments</p></div></div>
    <div class="panel-body"><label class="field"><span>Saved run</span><select id="address-run-picker"${disabled(busy)}>${options || '<option value="latest">No saved run yet</option>'}</select></label>
    <form id="address-search-form"><label class="field"><span>Search address or service name</span><input name="address_query" maxlength="256" value="${esc(review.query)}"${disabled(busy)}/></label>
    <label class="check-line"><input name="suspected_only" type="checkbox"${review.suspectedOnly ? " checked" : ""}${disabled(busy)}/><span>Suspected attributions only</span></label>
    <button class="btn address-search" type="submit"${disabled(busy)}>${icon("search")}Search</button></form>
    <form id="address-open-form" class="address-open"><label class="field"><span>Or paste an address</span><input name="pasted_address" required maxlength="200" value="${esc(review.pasted)}" placeholder="Liquid address"${disabled(busy)}/><small>You can review an address before it appears in a trace.</small></label><button class="btn" type="submit"${disabled(busy)}>Open address</button></form></div>
    <div class="address-list" aria-live="polite">${review.loading ? '<p class="panel-body muted">Loading saved addresses…</p>' : data?.rows.length ? data.rows.map(row => `<button class="address-row${selected?.address === row.address ? " selected" : ""}" data-action="address-select" data-address="${esc(row.address)}"${disabled(busy)}><span class="mono">${esc(row.address)}</span><small>${row.service?.enabled ? `<strong>${row.service.confidence === "confirmed" ? "" : "Suspected "}${esc(row.service.name || "Unnamed address")}</strong> (${row.service.stop_tracing === false ? "continue" : "STOP TRACING"}) · ` : ""}${esc(row.run_output_count ?? 0)} outputs in saved run${row.activity ? " · Activity saved" : ""}</small></button>`).join("") : '<p class="panel-body muted">No matching addresses in this run.</p>'}</div>
    <div class="address-pagination"><span>${data?.total ? `${data.offset + 1}–${Math.min(data.offset + data.limit, data.total)} of ${data.total}` : "0 addresses"}</span><div>${button("Previous", "address-prev", "", "small", busy || !data || data.offset === 0)}${button("Next", "address-next", "", "small", busy || !data || data.offset + data.limit >= data.total)}</div></div></section>
    <div class="address-detail">${selected ? `<section class="panel"><div class="panel-head"><div><h2 id="address-detail-title" tabindex="-1">Address activity</h2><p class="mono address-full">${esc(selected.address)}</p></div>${button("Refresh activity", "address-inspect", "refresh", "", busy)}</div><div class="panel-body">${addressActivity(selected.activity)}<p class="address-note">Each refresh checks this address only, with up to 5 history pages, 10 API attempts, and 60 seconds.${detail.fixture ? " Uses the synthetic fixture." : " Uses your Blockstream credits. Proton Pass prompts appear in the launching terminal."}</p></div></section>
    <section class="panel"><div class="panel-head"><div><h2>Investigator assessment</h2><p>Your designation applies to this investigation.</p></div></div><form id="service-form" class="panel-body"><label class="check-line"><input type="checkbox" name="service_enabled"${review.enabled ? " checked" : ""}${disabled(busy)}/><span><strong>Enable this address assessment</strong><small>This records an investigative assessment. Activity counts do not establish ownership.</small></span></label><label class="check-line"><input type="checkbox" name="service_stop"${review.stopTracing ? " checked" : ""}${disabled(busy)}/><span>Stop tracing through this address</span></label>
    <label class="field"><span>Display hop_limit (optional)</span><input name="service_hop_limit" type="number" min="0" step="1" value="${esc(review.hopLimit)}" placeholder="No local cap"${disabled(busy)}/><small>Additional hops to display after this address in Full trace. Blank: no local display cap. 0: hide downstream transactions. Collection, peg-out paths, and Starter connections ignore this value. Use Stop tracing to prevent further collection.</small></label>
    <label class="field"><span>Confidence (your assessment)</span><select name="service_confidence"${disabled(busy)}>${["suspected", "confirmed"].map(value => `<option value="${value}"${review.confidence === value ? " selected" : ""}>${value}</option>`).join("")}</select></label>
    <label class="field"><span>Source / evidence reference</span><input name="service_source" maxlength="1000" value="${esc(review.source)}"${disabled(busy)}/></label>
    <label class="field"><span>Observation date (optional ISO date / timestamp)</span><input name="service_observed" maxlength="80" value="${esc(review.observedAt)}"${disabled(busy)}/></label>
    <div class="settings-divider"></div><label class="field"><span>Address / service name (optional)</span><input name="service_name" maxlength="120" value="${esc(review.name)}"${disabled(busy)}/></label><label class="field"><span>Notes (optional)</span><textarea name="service_notes" maxlength="4000" rows="4"${disabled(busy)}>${esc(review.notes)}</textarea></label><p class="address-note">An enabled stop applies to the first run and continuations, including downstream branches reachable only through it. Confidence only controls the Suspected name prefix. Stop tracing is independent. Other independently reachable branches continue. Prior run evidence stays saved.</p><div class="form-actions"><button type="submit" class="btn primary"${disabled(busy)}>Save assessment</button>${selected.service?.enabled ? button("Disable assessment", "service-remove", "", "", busy) : ""}</div>${selected.service ? `<p class="address-note">Last saved ${esc(formatDate(selected.service.updated_at))}. Changes remain in the investigation's assessment history.</p>` : ""}</form></section>` : '<section class="panel"><div class="empty-state"><div class="empty-icon">' + icon("search") + '</div><h2>Select an address</h2><p>Choose an address from the saved run, or paste one to review it. Activity is fetched only when you select Refresh activity.</p></div></section>'}</div></div>`;
}

function miroRecoveryNotice(detail: Case): string {
  const recovery = detail.miro_recovery;
  if (!recovery?.pending_count) return "";
  return `<div class="alert warning" role="status">${icon("info")}<div><strong>Miro action needs recovery</strong><p>${recovery.pending_count} item${recovery.pending_count === 1 ? " has" : "s have"} an unconfirmed create result. Board changes are paused to prevent duplicates.</p><p>${recovery.can_recover_frame ? "The graph objects already created have saved IDs. Use Recover interrupted frame below to check whether its creation succeeded, then finish the saved graph and choose Create / update Miro frames." : recovery.can_confirm_empty ? "If the linked board is empty after the failed sync, inspect it and use empty-board recovery below." : "Inspect the linked board and use the existing per-item miro-resolve command to reconcile these items before syncing."}</p></div></div>`;
}

function addressCountWarning(result: Result): string {
  const counts = result.address_counts;
  if (!counts || counts.remaining === 0) return "";
  return `<div class="alert"><div><strong>Address transaction counts are incomplete</strong><p>${esc(counts.known)} of ${esc(counts.total)} counts are available. ${esc(counts.remaining)} lookups remain unresolved (${esc(human(counts.stop_reason || "lookup failed"))}). The chart retains ?? only where no count is available. Missing counts will be retried automatically on the next chart generation.</p></div></div>`;
}

function resultBanner(action: string, result: Result): string {
  if (["csv", "mermaid", "layout", "compact", "address-inspect"].includes(action)) return addressCountWarning(result);
  const titles: Record<string, string> = {
    trace: "Run saved",
    plot: previewLabel(result.preview_number) === "Saved preview" ? "Preview saved" : `${previewLabel(result.preview_number)} saved`,
    "plot-sync": result.published === false ? "Preview saved; no board created" : "Miro graph ready",
    "address-counts": "Address transaction counts saved",
    "miro-preview": "Miro change preview",
    "miro-sync": "Miro sync finished",
    "miro-frames": "Miro frames updated",
    "address-merge": "Address objects merged",
    "miro-compact": "Compact layout applied to Miro",
    "miro-organize": "Miro sync and reorganization finished",
    "miro-create": "Miro board created",
    "miro-rebuild": "Graph rebuilt on a new board",
    "miro-recover": "Miro sync recovered",
    "miro-frame-recover": "Interrupted frame recovered",
  };
  const numbers: [string, unknown][] = action === "miro-frames"
    ? [["New frames", result.created_frames ?? result.created], ["Updated frames", result.updated_frames], ["Removed frames", result.deleted]]
    : action.startsWith("miro-")
    ? [
        ["Recovered items", result.recovered_items ?? result.resolved_count],
        ["New items", result.new_items],
        ["New shapes", result.new_shapes],
        ["New connectors", result.new_connectors],
        ["New frames", result.new_frames],
        ["Frames to remove", result.frames_to_remove],
        ["Fee items to remove", result.fee_items_to_remove],
        ["Run summaries to remove", result.run_notes_to_remove],
      ]
    : action === "address-counts" ? [["Fetched", result.fetched], ["Known", result.known], ["Remaining", result.remaining]] : [];
  return `${addressCountWarning(result)}<div class="alert">${icon("check")}<div><strong>${titles[action] || "Action completed"}</strong><p>${action === "plot-sync" ? (result.published === false ? "No matching paths were found. Adjust the goal or collect more data, then try again. The empty preview is saved below." : "The layout and SVG are saved, and Miro has been synced. Open the board below to continue your investigation.") : action === "plot" ? "Open the saved preview from the library below. Prepare it for Miro when you are ready to publish." : action === "trace" ? `The saved run ${esc(result.run_id || "")} is ready to review and export.` : action === "miro-preview" ? "This is an offline plan. Live sync checks the board before making changes." : action === "miro-rebuild" ? "The rebuilt board is linked to this investigation. The previous board and its comments remain available. Create frames separately when the graph is finished." : action === "miro-create" ? "The board is linked to this investigation. Sync a saved run to add the graph." : action === "miro-recover" ? "The board was verified empty and the unconfirmed initial batch was cleared locally. Choose Sync to Miro to resume the recovered snapshot with individual shape requests. Your saved trace is ready to use." : action === "miro-frame-recover" ? `The interrupted frame was reconciled locally. The failed snapshot is selected. ${result.resume_action === "miro-frames" ? "Choose Create / update Miro frames to finish framing the saved graph." : "Finish Sync to Miro for this snapshot, then choose Create / update Miro frames."}` : action === "miro-frames" ? "Frames now reflect the synced graph. Run Create / update Miro frames again after further changes to the graph." : action === "address-merge" ? "Choose Sync to Miro to refresh labels, or Sync and reorganize to apply the shared-address layout. When the graph is finished, choose Create / update Miro frames." : "The result is saved with this investigation."}</p>${
    numbers.some(([, value]) => typeof value === "number")
      ? `<div class="result-grid">${numbers
          .filter(([, value]) => typeof value === "number")
          .map(
            ([label, value]) =>
              `<div><strong>${esc(value)}</strong>${label}</div>`,
          )
          .join("")}</div>`
      : ""
  }${action === "miro-rebuild" && typeof result.board_id === "string" && typeof result.previous_board_id === "string" ? `<p><a href="${esc(boardUrl(result.board_id))}" target="_blank" rel="noopener noreferrer">Open rebuilt board</a> · <a href="${esc(boardUrl(result.previous_board_id))}" target="_blank" rel="noopener noreferrer">Open previous board</a></p>` : ""}${fallbackNotice(result) ? `<p>${esc(fallbackNotice(result))}</p>` : ""}${layoutMetrics(result.layout_metrics, result.layout_algorithm === "dependency_layers_v1" ? "Dependency layout" : "ELK layout")}${typeof result.conflicts_count === "number" && result.conflicts_count > 0 ? `<p style="margin-top:10px"><strong>${result.conflicts_count} board conflict${result.conflicts_count === 1 ? "" : "s"} preserved.</strong> Review your board and the saved Miro sync report before further changes.</p>` : ""}</div><button class="dismiss" data-action="dismiss-result" aria-label="Dismiss result">${icon("close")}</button></div>`;
}

type SettingsDraft = {settings: Settings; name: string; board: string};
const settingsDrafts = new Map<string, SettingsDraft>();
let renderedSettingsKey = "";
function settingsKey(): string { return state.page === "case-settings" ? (state.activeCase?.id || "") : "workspace"; }
function saveSettingsDraft(): void {
  const form = document.querySelector<HTMLFormElement>("#settings-form");
  if (!form || !renderedSettingsKey) return;
  const data = new FormData(form);
  const previous = settingsDrafts.get(renderedSettingsKey)?.settings || {...defaults, ...(renderedSettingsKey === "workspace" ? state.settings : state.activeCase?.run_defaults)};
  settingsDrafts.set(renderedSettingsKey, {settings: {...readSettings(form, previous), ...(renderedSettingsKey === "workspace" ? {} : selectLayoutSettings(previous))}, name: String(data.get("name") || ""), board: String(data.get("board") || "")});
}

function settingsPage(): string {
  const isCase = state.page === "case-settings", detail = state.activeCase;
  const draft = settingsDrafts.get(settingsKey());
  const settings = draft?.settings || {...defaults, ...(isCase ? detail?.run_defaults : state.settings)};
  return `<div class="page-heading"><div><div class="eyebrow">${isCase ? esc(detail?.name) : "Workspace"}</div><h1 id="page-title" tabindex="-1">${isCase ? "Investigation settings" : "Workspace defaults"}</h1><p>${isCase ? "Data, hop defaults, optional run budgets, and attribution colors for this investigation. Plot appearance is saved in Plots &amp; Miro." : "Hop defaults, optional run budgets, and plot layout preferences copied into new investigations. Existing investigations keep their own settings."}</p></div>${button(isCase ? "Back to investigation" : "Back to investigations", isCase ? "back-case" : "dashboard", "", "ghost")}</div>
    <nav class="settings-navigation" aria-label="Settings sections">${isCase ? '<a href="#settings-data">Investigation data</a>' : ""}<a href="#settings-trace">Tracing</a>${!isCase ? '<a href="#settings-layout">Plot layouts</a>' : ""}<a href="#settings-miro">Miro</a>${isCase ? '<a href="#settings-colors">Colors</a>' : ""}</nav>
    ${isCase && detail ? `<div class="settings-layout">${investigationDataPanel(detail)}</div>` : ""}
    <form id="settings-form" class="settings-layout">
    ${isCase ? `<section class="panel"><div class="panel-body"><label class="field"><span>Investigation name</span><input name="name" maxlength="120" required value="${esc(draft?.name ?? detail?.name)}" autocomplete="off"/></label></div></section>` : ""}
    <section class="panel" id="settings-trace"><div class="panel-head"><div><h2>Tracing</h2><p>Hop defaults and optional collection budgets.</p></div></div><div class="panel-body">${budgetFields(settings)}</div></section>
    ${!isCase ? `<section class="panel" id="settings-layout"><div class="panel-head"><div><h2>Plot layout defaults</h2><p>Starting values for new investigations. Adjust each investigation separately in Plots &amp; Miro.</p></div></div><div class="panel-body">${graphFields(settings)}</div></section>` : ""}
    <section class="panel" id="settings-miro"><div class="panel-head"><h2>Miro</h2></div><div class="panel-body">${isCase ? `${button("Manage investigation boards", "view-boards", "board", "", isBusy())}<p class="small muted">Create, update and link boards in Plots &amp; Miro.</p>` : ""}${settings.budget_limits_enabled ? numericField(settings, "max_new_items", "New Miro items", "Maximum new objects and connections per sync. 0 = unlimited.") : '<p class="small muted">No new Miro item cap. Enable optional run budgets under Tracing to set one.</p>'}<p class="small muted">Live actions use your existing SecretSpec and Proton Pass configuration.</p></div></section>
    <div class="form-actions settings-save"><p>${isCase ? "Save once to update this investigation." : "Applies to investigations created after saving."}</p><button type="submit" class="btn primary"${disabled(isBusy())}>${icon("check")}Save settings</button></div></form>
    ${isCase && detail ? `<section class="panel" id="settings-colors"><div class="panel-head"><div><h2>Colors</h2><p>Graph roles and attribution names. Color edits save separately.</p></div>${button("Edit colors", "name-colors-open", "", "", isBusy())}</div></section>${nameColorsPanel(detail.id, isBusy())}` : ""}`;
}

function saveDraft(): void {
  const form = document.querySelector<HTMLFormElement>("#new-case-form");
  if (!form) return;
  const data = new FormData(form);
  state.draft.name = String(data.get("name") || "");
  state.draft.txids = String(data.get("txids") || "");
  state.draft.seeds = String(data.get("seeds") || "");
  state.draft.blockchain = String(data.get("blockchain") ?? state.draft.blockchain);
  state.draft.settings = readSettings(form, state.draft.settings);
}

function finishedJobs(): ActiveJob[] {
  return [...state.jobs.values()].filter(job => !["running", "cancelling"].includes(job.status))
    .sort((a, b) => (b.finishedAt ?? b.started) - (a.finishedAt ?? a.started));
}

function pruneFinishedJobs(): void {
  for (const old of finishedJobs().slice(32)) {state.jobs.delete(old.id); dismissedJobs.add(old.id);}
}

function rememberJob(job: Job, local?: {action: string; caseId?: string; live: boolean; generation: number; viewRevision: number; lookupTxids?: string; resource_kind: JobResource["resource_kind"]; resource_key?: string | null; source_run_id?: string}): ActiveJob {
  const existing = state.jobs.get(job.id);
  if (existing) {
    // Discovery can observe a fast job before its initiating POST returns.
    if (local) {
      Object.assign(existing, local);
      existing.status = "running";
      existing.outcome = undefined;
      existing.finishedAt = undefined;
    }
    return existing;
  }
  const active: ActiveJob = {
    resource_kind: job.resource_kind || local?.resource_kind, resource_key: job.resource_key ?? local?.resource_key,
    source_run_id: job.source_run_id || local?.source_run_id, execution_state: job.execution_state, viewRevision: local?.viewRevision,
    id: job.id, action: local?.action || job.action || "recovered", caseId: local?.caseId ?? job.case_id ?? undefined,
    started: Number.isFinite(job.started_at) ? job.started_at! * 1000 : Date.now(),
    finishedAt: Number.isFinite(job.finished_at) ? job.finished_at! * 1000 : undefined,
    message: job.message || "A local action is running…", live: local?.live ?? job.live ?? true,
    progress: job.progress, cancellable: job.cancellable === true, cancelling: job.status === "cancelling",
    status: job.status || "running", generation: local?.generation, lookupTxids: local?.lookupTxids,
    outcome: ["succeeded", "failed", "canceled"].includes(job.status) ? job : undefined,
  };
  state.jobs.set(job.id, active);
  // Discovered terminal history stays silent because it is never polled. Do not
  // consume its notification ID: its initiating POST may still be in flight.
  if (active.status === "running" || active.status === "cancelling") schedulePoll(active.id);
  pruneFinishedJobs();
  return active;
}

async function refreshSession(): Promise<void> {
  const session = await api<{
    csrf: string; settings: Settings; cases: Case[];
    active_job?: string | null; active_jobs?: Job[];
  }>("/api/session");
  state.csrf = session.csrf;
  state.settings = { ...defaults, ...session.settings };
  state.cases = session.cases;
  if (session.active_jobs) session.active_jobs.forEach(job => rememberJob(job));
  else if (session.active_job && !state.jobs.has(session.active_job))
    rememberJob(await api<Job>(`/api/jobs/${encodeURIComponent(session.active_job)}`));
}

const sectionPaths: Record<CaseSection, string> = {collection: "collection", workflow: "workflow", history: "history", shared: "shared-collection", boards: "boards"};
const sectionLabels: Record<CaseSection, string> = {collection: "Saved run summaries", workflow: "Saved plots and boards", history: "History and downloads", shared: "Shared collection", boards: "Miro boards"};
const sectionFields: Record<CaseSection, (keyof Case)[]> = {
  collection: ["runs", "latest_run", "status"],
  workflow: ["plots", "plots_notice", "plots_next_cursor"], boards: ["boards", "boards_notice", "miro_recovery", "miro_rebuild"],
  history: ["artifacts", "pegout_searches", "runs"], shared: ["shared_collection"],
};
const sectionRequests = new Map<string, Promise<void>>();
const sectionErrors = new Map<string, string>();
const sectionRetries = new Map<string, {timer: ReturnType<typeof setTimeout>; count: number}>();
const sectionVersions = new Map<string, number>();
const plotChecks = new Map<string, Promise<void>>();
const plotCheckErrors = new Map<string, string>();

type PreviewLibraryState = {scrollTop: number; loadingPage: boolean; pageError: string; loadingSelection: boolean;
  selectionError: string; selectionKey?: string};
const previewLibraries = new Map<string, PreviewLibraryState>();
const previewSelectionRequests = new Map<string, Promise<void>>();
function previewLibraryState(caseId: string): PreviewLibraryState {
  let library = previewLibraries.get(caseId);
  if (!library) {library = {scrollTop: 0, loadingPage: false, pageError: "", loadingSelection: false, selectionError: ""}; previewLibraries.set(caseId, library);}
  return library;
}

function mergePreviewMetadata(detail: Case, incoming: Plot[]): void {
  const plots = new Map((detail.plots || []).map(plot => [plot.preview_id, plot]));
  for (const plot of incoming) {
    const existing = plots.get(plot.preview_id);
    // Metadata pages describe immutable IDs; paging must not discard an explicit
    // preparation already completed in this view. A workflow refresh may reset it.
    if (plot.validation_pending && existing && !existing.validation_pending) continue;
    plots.set(plot.preview_id, plot);
  }
  detail.plots = [...plots.values()];
}

async function loadOlderPreviews(caseId: string): Promise<void> {
  const detail = state.activeCase, library = previewLibraryState(caseId);
  if (detail?.id !== caseId || !detail.plots_next_cursor || library.loadingPage) return;
  const generation = pageGeneration, version = sectionVersions.get(`${caseId}:workflow`), cursor = detail.plots_next_cursor;
  const applies = () => generation === pageGeneration && state.activeCase?.id === caseId && sectionVersions.get(`${caseId}:workflow`) === version;
  library.loadingPage = true; library.pageError = ""; render();
  try {
    const page = await api<{plots: Plot[]; next_cursor: string | null}>(`/api/cases/${encodeURIComponent(caseId)}/plots?cursor=${encodeURIComponent(cursor)}`);
    if (!applies()) return;
    if (!Array.isArray(page.plots) || page.next_cursor != null && typeof page.next_cursor !== "string") throw new Error("Older preview information was incomplete. Try again.");
    mergePreviewMetadata(state.activeCase!, page.plots);
    state.activeCase!.plots_next_cursor = page.next_cursor;
  } catch (error) {
    if (applies()) library.pageError = error instanceof Error ? error.message : "Older previews could not be loaded.";
  } finally {
    library.loadingPage = false;
    if (generation === pageGeneration && state.activeCase?.id === caseId) render();
  }
}

async function loadPreviewSelection(caseId: string, previewId: string, retry = false): Promise<void> {
  const detail = state.activeCase, library = previewLibraryState(caseId);
  if (detail?.id !== caseId || currentWorkflow(detail).plot !== previewId) return;
  const generation = pageGeneration, version = sectionVersions.get(`${caseId}:workflow`);
  const requestKey = `${caseId}:${previewId}:${generation}:${version}`;
  if (previewSelectionRequests.has(requestKey)) return previewSelectionRequests.get(requestKey);
  if (library.selectionKey === requestKey && library.selectionError && !retry) return;
  library.selectionKey = requestKey; library.loadingSelection = true; library.selectionError = "";
  const applies = () => generation === pageGeneration && state.activeCase?.id === caseId &&
    sectionVersions.get(`${caseId}:workflow`) === version && currentWorkflow(state.activeCase).plot === previewId;
  let request!: Promise<void>;
  request = (async () => {
    try {
      const plot = await api<Plot>(`/api/cases/${encodeURIComponent(caseId)}/plots/${encodeURIComponent(previewId)}/summary`);
      if (!applies()) return;
      if (plot.preview_id !== previewId) throw new Error("The saved preview response did not match the selection.");
      mergePreviewMetadata(state.activeCase!, [plot]);
    } catch (error) {
      if (applies()) library.selectionError = error instanceof Error ? error.message : "The selected preview could not be found. Choose another saved preview or retry.";
    } finally {
      if (previewSelectionRequests.get(requestKey) === request) previewSelectionRequests.delete(requestKey);
      if (library.selectionKey === requestKey) library.loadingSelection = false;
      if (generation === pageGeneration && state.activeCase?.id === caseId) render();
    }
  })();
  previewSelectionRequests.set(requestKey, request); render();
  return request;
}


type SharedCollectionLoad = {
  task: ActiveJob;
  selected: string;
  overviewSequence?: number;
  request: Promise<void>;
  response?: Partial<Case>;
  timer?: ReturnType<typeof setTimeout>;
  receive: (response?: Partial<Case>, error?: string) => void;
};
const sharedCollectionLoads = new Map<string, SharedCollectionLoad>();
let sharedCollectionLoadId = 0;

function loadSharedCollectionSection(caseId: string, selected: string, overviewSequence: number | undefined,
    receive: SharedCollectionLoad["receive"]): Promise<void> {
  const previous = sharedCollectionLoads.get(caseId);
  if (previous?.task.status === "running" && previous.selected === selected && previous.overviewSequence === overviewSequence) {
    // Navigation may replace the view while the same summary is still loading.
    previous.receive = receive;
    if (previous.response) receive(previous.response);
    return previous.request;
  }
  if (previous?.timer) clearTimeout(previous.timer);
  const load: SharedCollectionLoad = {
    task: {id: `shared-load-${++sharedCollectionLoadId}`, action: "load-shared-collection", caseId,
      status: "running", started: Date.now(), message: "Loading shared collection information…",
      live: false, cancellable: false, cancelling: false,
      source_run_id: selected && selected !== "latest" ? selected : undefined},
    selected, overviewSequence, receive, request: Promise.resolve(),
  };
  sharedCollectionLoads.set(caseId, load);
  updateJobProgress();
  const current = () => sharedCollectionLoads.get(caseId) === load;
  const finishError = (message: string) => {
    load.task.status = "failed"; load.task.message = message; load.task.finishedAt = Date.now();
    load.receive(undefined, message);
  };
  const poll = async (retry = 0): Promise<void> => {
    try {
      const suffix = selected && selected !== "latest" ? `/${encodeURIComponent(selected)}` : "";
      const response = await api<Partial<Case>>(`/api/cases/${encodeURIComponent(caseId)}/shared-collection${suffix}`);
      if (!current()) return;
      const status = response.sections?.shared || "ready";
      if (status === "error") {
        finishError("Shared collection information could not be loaded. Try again.");
      } else if (status === "loading" || status === "unloaded") {
        if (retry >= 60) finishError("Shared collection information is still being prepared. Try again shortly.");
        else {
          load.response = response;
          load.task.message = "Preparing saved shared collection summaries. You can keep using other investigations.";
          // Continue observing preparation when its investigation is not visible.
          load.timer = setTimeout(() => {
            load.timer = undefined;
            if (current()) load.request = poll(retry + 1);
          }, retry ? 5000 : 1000);
          load.receive(response);
        }
      } else {
        load.task.status = "succeeded"; load.task.message = "Shared collection information loaded.";
        load.task.finishedAt = Date.now(); load.receive(response);
      }
    } catch (error) {
      if (current()) finishError(error instanceof Error ? error.message : "Shared collection information could not be loaded.");
    } finally { if (current()) updateJobProgress(); }
  };
  load.request = poll();
  return load.request;
}

function sectionReady(detail: Case, section: CaseSection): boolean {
  return !detail.sections || detail.sections[section] === "ready";
}

function mergeCase(previous: Case, update: Partial<Case>): Case {
  // Overview/settings responses only contain a subset of the loaded sections.
  const merged = {...previous, ...update, sections: previous.sections || update.sections
    ? {...previous.sections, ...update.sections} : undefined};
  for (const section of Object.keys(sectionPaths) as CaseSection[]) {
    if (update.sections?.[section] === "unloaded" && previous.sections?.[section]) merged.sections![section] = previous.sections[section];
  }
  if (update.runs && previous.runs && update.sections?.collection !== "ready") {
    const runs = new Map(previous.runs.map(run => [run.id, run]));
    for (const run of update.runs) runs.set(run.id, run.summary_pending && runs.has(run.id) ? {...runs.get(run.id)!, ...run} : run);
    merged.runs = [...runs.values()];
  }
  if (update.plots && previous.plots) {
    const currentIds = new Set(update.plots.map(plot => plot.preview_id));
    merged.plots = [...update.plots, ...previous.plots.filter(plot => !currentIds.has(plot.preview_id))];
  }
  return merged;
}

function sectionNotice(detail: Case, section: CaseSection): string {
  const failed = detail.sections?.[section] === "error";
  return `<section class="panel" data-loading-section="${section}"><div class="panel-body" role="status"><strong>${sectionLabels[section]}</strong><p>${failed ? esc(sectionErrors.get(`${detail.id}:${section}`) || "This section could not be loaded.") : "Loading saved information. You can use other investigation tabs while this finishes."}</p>${failed ? button("Try again", "retry-case-section", "refresh", "small", false, `data-section="${section}"`) : ""}</div></section>`;
}

function visibleSections(): CaseSection[] {
  if (state.page !== "case") return [];
  if (state.caseView === "history") return ["collection", "workflow", "history"];
  if (["plots", "boards"].includes(state.caseView)) return ["collection", "shared", "workflow", "boards"];
  return ["collection", "shared"];
}

async function loadCaseSection(caseId: string, section: CaseSection, force = false, retry = 0): Promise<void> {
  const owner = state.activeCase;
  if (owner?.id !== caseId) return;
  const selected = section === "collection" ? state.selectedRun : section === "shared" ? currentWorkflow(owner).sharedRun : "";
  const pending = section === "collection" ? currentRun()?.summary_pending : section === "shared"
    ? owner.shared_collection?.runs.find(run => run.id === selected)?.summary_pending : false;
  if (!owner.sections && !force || !force && !pending && sectionReady(owner, section)) return;
  const suffix = selected && selected !== "latest" ? `/${encodeURIComponent(selected)}` : "";
  const overviewSequence = caseRefreshSequence.get(caseId);
  const generation = pageGeneration, key = `${caseId}:${section}`;
  const requestKey = `${key}:${generation}:${selected}:${overviewSequence ?? 0}`;
  const existing = sectionRequests.get(requestKey);
  if (existing) return existing;
  const timer = sectionRetries.get(key); if (timer) clearTimeout(timer.timer);
  sectionRetries.delete(key);
  const version = (sectionVersions.get(key) || 0) + 1;
  sectionVersions.set(key, version);
  sectionErrors.delete(key);
  if (owner.sections) owner.sections[section] = "loading";
  const applies = () => generation === pageGeneration && state.activeCase?.id === caseId && sectionVersions.get(key) === version && caseRefreshSequence.get(caseId) === overviewSequence;
  if (section === "shared") return loadSharedCollectionSection(caseId, selected, overviewSequence, (response, error) => {
    if (!applies()) return;
    if (error) {
      state.activeCase!.sections = {...state.activeCase!.sections, shared: "error"};
      sectionErrors.set(key, error);
    } else if (response) {
      state.activeCase = mergeCase(state.activeCase!, {
        ...(response.shared_collection ? {shared_collection: response.shared_collection} : {}),
        sections: {shared: response.sections?.shared || "ready"},
      });
    }
    render();
  });
  let request!: Promise<void>;
  request = (async () => {
    try {
      const response = await api<Partial<Case>>(`/api/cases/${encodeURIComponent(caseId)}/${sectionPaths[section]}${suffix}`);
      if (!applies()) return;
      const partial = Object.fromEntries(sectionFields[section].filter(field => field in response).map(field => [field, response[field]])) as Partial<Case>;
      const status = response.sections?.[section] || "ready";
      if (state.activeCase!.sections) partial.sections = {[section]: status};
      state.activeCase = mergeCase(state.activeCase!, partial);
      if (section === "collection" && status === "ready" && state.selectedRun !== "latest" && !state.activeCase.runs?.some(run => run.id === state.selectedRun)) state.selectedRun = "latest";
      if (status === "loading" || status === "unloaded") {
        if (retry >= 60) {
          state.activeCase.sections = {...state.activeCase.sections, [section]: "error"};
          sectionErrors.set(key, "Saved information is still being prepared. Try again shortly.");
        } else sectionRetries.set(key, {count: retry + 1, timer: setTimeout(() => {
          sectionRetries.delete(key);
          if (applies() && visibleSections().includes(section)) void loadCaseSection(caseId, section, true, retry + 1);
        }, retry ? 5000 : 1000)});
      }
      render();
      if (section === "workflow") ensurePreviewSelection();
    } catch (error) {
      if (!applies()) return;
      state.activeCase!.sections = {...state.activeCase!.sections, [section]: "error"};
      sectionErrors.set(key, error instanceof Error ? error.message : "This section could not be loaded.");
      render();
    } finally { if (sectionRequests.get(requestKey) === request) sectionRequests.delete(requestKey); }
  })();
  sectionRequests.set(requestKey, request);
  return request;
}

function loadVisibleSections(): void {
  const detail = state.activeCase;
  if (!detail) return;
  for (const section of visibleSections()) if (detail.sections?.[section] !== "error") void loadCaseSection(detail.id, section);
  ensurePreviewSelection();
}

function plotValidationNotice(detail: Case, plot: Plot): string {
  const key = `${detail.id}:${plot.preview_id}`, checking = plotChecks.has(`${key}:${pageGeneration}`);
  return `<p class="artifact-note" role="status">${esc(plotCheckErrors.get(key) || (checking ? "Preparing this saved preview for Miro…" : "Open the preview in a separate tab to review it. Prepare for Miro checks this saved layout before enabling publication."))}</p>${!checking ? button("Prepare for Miro", "verify-saved-plot", "check", "small", draftBusy(), `data-preview="${esc(plot.preview_id)}" data-case-id="${esc(detail.id)}"`) : ""}`;
}

async function verifySelectedPlot(caseId: string, previewId: string, retry = false): Promise<void> {
  const detail = state.activeCase, plot = detail?.plots?.find(item => item.preview_id === previewId);
  if (detail?.id !== caseId || !plot?.validation_pending) return;
  const key = `${caseId}:${previewId}`, requestKey = `${key}:${pageGeneration}`, existing = plotChecks.get(requestKey);
  if (existing) return existing;
  if (plotCheckErrors.has(key) && !retry) return;
  plotCheckErrors.delete(key);
  const generation = pageGeneration, version = sectionVersions.get(`${caseId}:workflow`);
  let request!: Promise<void>;
  request = (async () => {
    try {
      const verified = await api<Plot>(`/api/cases/${encodeURIComponent(caseId)}/plots/${encodeURIComponent(previewId)}`);
      if (generation !== pageGeneration || state.activeCase?.id !== caseId || sectionVersions.get(`${caseId}:workflow`) !== version) return;
      if (verified.preview_id !== previewId) throw new Error("The saved layout response did not match the selected layout.");
      state.activeCase.plots = state.activeCase.plots?.map(item => item.preview_id === previewId ? {...verified, validation_pending: false} : item);
    } catch (error) {
      if (generation === pageGeneration && state.activeCase?.id === caseId) plotCheckErrors.set(key, error instanceof Error ? error.message : "The saved layout could not be checked.");
    } finally {
      if (plotChecks.get(requestKey) === request) plotChecks.delete(requestKey);
      if (generation === pageGeneration && state.activeCase?.id === caseId) {
        if (sectionVersions.get(`${caseId}:workflow`) !== version) plotCheckErrors.set(key, "Saved preview information changed. Prepare this preview for Miro again.");
        render();
      }
    }
  })();
  plotChecks.set(requestKey, request);
  render();
  return request;
}

function ensurePreviewSelection(): void {
  const detail = state.activeCase;
  if (!detail || state.page !== "case" || !["plots", "boards", "history"].includes(state.caseView) || !sectionReady(detail, "workflow")) return;
  const selected = currentWorkflow(detail).plot;
  if (selected && !detail.plots?.some(plot => plot.preview_id === selected)) void loadPreviewSelection(detail.id, selected);
}

async function refreshCaseDetail(caseId: string, followLatest: () => boolean = () => false, action = ""): Promise<{detail: Case; applied: boolean}> {
  const sequence = (caseRefreshSequence.get(caseId) || 0) + 1;
  caseRefreshSequence.set(caseId, sequence);
  const generation = pageGeneration;
  const versions = new Map(sectionVersions);
  const detail = await api<Case>(`/api/cases/${encodeURIComponent(caseId)}/overview`);
  const applied = caseRefreshSequence.get(caseId) === sequence && generation === pageGeneration && state.activeCase?.id === caseId && !state.openingCase;
  if (applied) {
    const priorRun = currentRun()?.id;
    if (followLatest()) state.selectedRun = "latest";
    else if (state.selectedRun === "latest" && priorRun && detail.latest_run !== state.activeCase?.latest_run) state.selectedRun = priorRun;
    const update: Partial<Case> = {...detail};
    for (const section of Object.keys(sectionPaths) as CaseSection[]) {
      if (sectionVersions.get(`${caseId}:${section}`) !== versions.get(`${caseId}:${section}`)) {
        for (const field of sectionFields[section]) delete update[field];
        if (update.sections) update.sections = {...update.sections, [section]: state.activeCase!.sections?.[section]};
      }
    }
    state.activeCase = mergeCase(state.activeCase!, update);
    render();
    if (detail.sections) {
      const affected: CaseSection[] = action === "trace" ? ["collection"] : action === "shared-trace" ? ["shared"]
        : action === "plot" ? ["workflow"] : action === "plot-sync" ? ["workflow", "boards"]
        : action.startsWith("board-") || action.startsWith("miro-") ? ["boards"]
        : ["mermaid", "csv", "layout", "compact", "connections", "pegouts", "pegouts-preview"].includes(action) ? ["history"]
        : visibleSections();
      const quick: Promise<void>[] = [];
      for (const section of affected) {
        // Heavy optional sections must not delay reporting a completed task.
        if (section === "history" && !visibleSections().includes(section)) {
          state.activeCase!.sections = {...state.activeCase!.sections, history: "unloaded"};
        } else {
          const loading = loadCaseSection(caseId, section, true);
          if (section !== "boards" && section !== "history") quick.push(loading);
        }
      }
      await Promise.all(quick);
    }
    if (state.activeCase?.id === caseId && generation === pageGeneration) {loadVisibleSections(); render();}
  }
  const stillApplied = applied && generation === pageGeneration && state.activeCase?.id === caseId;
  return {detail: stillApplied ? state.activeCase! : detail, applied: stillApplied};
}

async function discoverJobs(): Promise<void> {
  if (discoveringJobs || !state.csrf) return;
  discoveringJobs = true;
  const wasBusy = isBusy();
  try {
    const response = await api<{jobs: Job[]}>("/api/jobs");
    const discovered = response.jobs.filter(job => !dismissedJobs.has(job.id) && !state.jobs.has(job.id));
    for (const job of discovered) rememberJob(job);
    if (state.activeCase?.id && discovered.some(job => job.action === "shared-trace" && !["running", "cancelling"].includes(job.status)))
      await refreshSharedCollection(state.activeCase.id);
    if (discovered.length) {updateJobProgress(); void refreshSession().catch(() => {});}
    const currentId = state.activeCase?.id;
    if (currentId && discovered.some(job => job.case_id === currentId && !["running", "cancelling"].includes(job.status)))
      await refreshCaseDetail(currentId);
    if (wasBusy !== isBusy() || discovered.some(job => viewingCase(job.case_id || undefined)) ||
        (state.page === "dashboard" && discovered.length)) render(); else updateJobProgress();
  } catch { /* Individual job polling retains its state while reconnecting. */ }
  finally { discoveringJobs = false; }
}

async function openCase(id: string): Promise<void> {
  if (isCaseOpening(id)) return;
  rememberInvestigationView();
  saveSettingsDraft(); saveAddressDraft(); saveDraft();
  resetFrameRecovery();
  state.editConflicts = null;
  dialog.close();
  const generation = ++pageGeneration;
  const name = state.cases.find(item => item.id === id)?.name || "investigation";
  state.openingCase = {id, name, generation};
  state.error = "";
  render();
  let detail: Case;
  try {
    detail = await api<Case>(`/api/cases/${encodeURIComponent(id)}/overview`);
  } catch (error) {
    if (generation !== pageGeneration) return;
    state.openingCase = null;
    state.error = `Could not open ${name}. ${error instanceof Error ? error.message : "Try opening it again."}`;
    toast(state.error, true);
    render();
    return;
  }
  if (generation !== pageGeneration) return;
  state.openingCase = null;
  saveDraft();
  const remembered = investigationViews.get(id);
  state.activeCase = detail;
  state.addressReview = remembered?.addressReview || { query: "", suspectedOnly: false, data: null, selected: null,
    loading: false, name: "", notes: "", enabled: false, pasted: "",
    confidence: "suspected", source: "Investigator designation", observedAt: "", stopTracing: true, hopLimit: "" };
  if (importedInputEditors.has(detail.id)) resetImportedInputEditors(detail.id);
  selectInputImport(detail.id);
  selectAddressImport(detail.id);
  selectNameColors(detail.id);
  selectChangeOutputs(detail.id);
  state.selectedRun = remembered?.selectedRun && (detail.sections || detail.runs?.some(run => run.id === remembered.selectedRun)) ? remembered.selectedRun : "latest";
  state.caseView = remembered?.caseView || "collect";
  state.page = remembered?.page || "case";
  state.error = "";
  addOpenInvestigation(id);
  history.replaceState(null, "", `#case/${encodeURIComponent(id)}`);
  render(true);
  window.scrollTo?.({top: remembered?.scrollY || 0, behavior: "instant"});
  loadVisibleSections();
  if (state.page === "addresses") void loadAddresses(state.addressReview.data?.offset || 0).catch(handleError);
}

function navigate(page: Page): void {
  rememberInvestigationView();
  saveSettingsDraft(); saveAddressDraft();
  resetFrameRecovery();
  state.editConflicts = null;
  dialog.close();
  saveDraft();
  cancelCaseOpening();
  pageGeneration++;
  state.page = page;
  state.error = "";
  history.replaceState(
    null,
    "",
    page === "dashboard" ? location.pathname : viewingCase(state.activeCase?.id) ? `#case/${encodeURIComponent(state.activeCase!.id)}` : `#${page}`,
  );
  render(true);
}

async function startJob(
  path: string,
  body: unknown,
  action: string,
  live: boolean,
  caseId?: string,
): Promise<string | null> {
  const request = typeof body === "object" && body !== null ? {...body} as Record<string, unknown> : {};
  // An unscoped seed lookup must not inherit the previously viewed case.
  if (actionBusy(action, request, caseId ?? null)) return null;
  if (request.run_id === "latest" && !["trace", "shared-trace"].includes(action) && caseId === state.activeCase?.id)
    request.run_id = currentRun()?.id || state.activeCase?.latest_run || "latest";
  const resource = requestedResource(action, request, caseId ?? null);
  if (action !== "miro-frame-review") resetFrameRecovery();
  state.editConflicts = null;
  saveDraft();
  const generation = pageGeneration;
  const revision = ++viewRevision;
  const lookupTxids = action === "lookup" ? state.draft.txids : undefined;
  submitting = true;
  render();
  try {
    const job = await api<Job>(path, request);
    if (action === "trace" && caseId && typeof body === "object" && body !== null
        && "hop_reference_name" in body && typeof body.hop_reference_name === "string") {
      const hop_reference_name = body.hop_reference_name;
      if (state.activeCase?.id === caseId) state.activeCase.run_defaults = {...state.activeCase.run_defaults, hop_reference_name};
      state.cases = state.cases.map(item => item.id === caseId
        ? {...item, run_defaults: {...item.run_defaults, hop_reference_name}} : item);
      const settingsDraft = settingsDrafts.get(caseId);
      if (settingsDraft) settingsDraft.settings.hop_reference_name = hop_reference_name;
    }
    rememberJob(job, {action, caseId, live, generation, viewRevision: revision, lookupTxids, ...resource,
      source_run_id: typeof request.run_id === "string" && request.run_id !== "latest" ? request.run_id : undefined});
    tasksOpen = true;
    if (generation === pageGeneration) {state.error = ""; dialog.close();}
    schedulePoll(job.id, 150);
    return job.id;
  } finally {
    submitting = false;
    render();
  }
}

function schedulePoll(identity: string, delay = 1200): void {
  const timer = pollTimers.get(identity);
  if (timer) clearTimeout(timer);
  pollTimers.set(identity, setTimeout(() => {pollTimers.delete(identity); void pollJob(identity);}, delay));
}

async function cancelJob(identity: string): Promise<void> {
  const active = state.jobs.get(identity);
  if (!active || !active.cancellable || active.cancelling || active.status !== "running") return;
  active.cancelling = true;
  active.message = "Canceling the calculation and stopping its renderer…";
  updateJobProgress();
  try {
    const job = await api<Job>(`/api/jobs/${encodeURIComponent(active.id)}/cancel`, {});
    if (!["running", "cancelling"].includes(active.status)) return;
    active.cancelling = job.status === "cancelling";
    active.cancellable = job.cancellable === true;
    active.message = job.message;
  } catch (error) {
    active.cancelling = false;
    toast(error instanceof Error ? error.message : "Could not request cancellation.", true);
  }
  updateJobProgress();
  schedulePoll(identity, 150);
}

async function pollJob(identity: string): Promise<void> {
  const active = state.jobs.get(identity);
  if (!active || !["running", "cancelling"].includes(active.status) || pollingJobs.has(identity)) return;
  pollingJobs.add(identity);
  const wasBusy = isBusy();
  const refreshJobView = () => {
    if (wasBusy !== isBusy() || viewingCase(active.caseId) ||
        (active.action === "lookup" && state.page === "new")) render();
    else updateJobProgress();
  };
  try {
    const job = await api<Job>(`/api/jobs/${encodeURIComponent(active.id)}`);
    if (job.status === "running" || job.status === "cancelling") {
      active.status = job.status;
      active.message = job.message || active.message;
      active.progress = job.progress;
      active.execution_state = job.execution_state || active.execution_state;
      active.resource_kind = job.resource_kind || active.resource_kind;
      active.resource_key = job.resource_key ?? active.resource_key;
      active.source_run_id = job.source_run_id || active.source_run_id;
      active.cancellable = job.cancellable === true;
      active.cancelling = job.status === "cancelling";
      updateJobProgress();
      schedulePoll(identity);
      return;
    }
    active.status = job.status;
    active.message = job.message;
    active.outcome = job;
    active.finishedAt = Number.isFinite(job.finished_at) ? job.finished_at! * 1000 : Date.now();
    notifyTaskResult(active);
    pruneFinishedJobs();
    active.cancellable = false;
    refreshJobView();
    if (job.status === "canceled") {
      if (active.action.startsWith("miro-frame-") && canApplyJobView(active)) resetFrameRecovery();
      if (active.action === "change-output-lookup" && active.caseId) changeOutputsLookupComplete(active.caseId, active.id, undefined, job.message || "Transaction lookup canceled.");
      if (canApplyJobView(active)) state.error = "";
      toast(`${taskName(active)}: ${job.message || "Calculation canceled. Saved investigation runs are unchanged."}`);
      if ((active.action.startsWith("board-") || ["plot-sync", "pegouts", "pegouts-preview", "shared-trace"].includes(active.action)) && active.caseId && state.activeCase?.id === active.caseId) {
        try {
          await refreshCaseDetail(active.caseId, () => false, active.action);
        } catch { /* Preserve the cancellation message if refresh is unavailable. */ }
      }
    } else if (job.status === "failed") {
      if (active.action.startsWith("miro-frame-") && canApplyJobView(active)) resetFrameRecovery();
      if (viewingCase(active.caseId)) state.editConflicts = active.caseId && job.edit_conflicts
        ? { caseId: active.caseId, report: job.edit_conflicts } : null;
      const progress = job.progress || active.progress;
      const hopLabel = progress && collectionHopLabel(progress);
      const lastStage = hopLabel ? ` Last reported stage: ${hopLabel}.` : progress?.phase
        ? ` Last reported stage: ${human(progress.phase)}${
            Number.isFinite(progress.completed) && Number.isFinite(progress.total) && progress.total > 0
              ? ` (${progress.completed} / ${progress.total})`
              : ""
          }.`
        : "";
      active.outcomeError = (job.message || "The action did not complete. Check the launching terminal.") + lastStage;
      if (viewingCase(active.caseId)) state.error = active.outcomeError;
      toast(`${taskName(active)}: ${active.outcomeError}`, true);
      if (active.action === "change-output-lookup" && active.caseId) changeOutputsLookupComplete(active.caseId, active.id, undefined, active.outcomeError);
      if ((active.action.startsWith("miro-") || active.action.startsWith("board-") || ["plot-sync", "pegouts", "pegouts-preview", "shared-trace"].includes(active.action)) && active.caseId && state.activeCase?.id === active.caseId) {
        try {
          const previousBoards = new Set((state.activeCase?.boards || []).map(board => board.id));
          const {detail, applied} = await refreshCaseDetail(active.caseId, () => false, active.action);
          if (applied) {
            if (active.action === "plot-sync" && canApplyJobView(active)) {
              const draft = currentWorkflow(detail);
              const board = detail.boards?.find(item => !previousBoards.has(item.id)) ||
                detail.boards?.find(item => item.id === draft.layoutBoard) ||
                detail.boards?.find(item => item.creation_preview_id && item.status !== "synced" &&
                  (!item.preview_id || item.preview_id === item.creation_preview_id));
              if (board) {
                draft.board = board.id;
                draft.plot = board.preview_id || board.creation_preview_id || draft.plot;
                if (board.board_id) selectLayoutDestination(detail, "update", board);
              }
              state.caseView = "plots";
            }
          }
        } catch { /* Keep the original action failure visible if refresh is unavailable. */ }
      }
    } else {
      const result = job.result || {};
      if (["plot", "plot-sync"].includes(active.action) && canApplyJobView(active) && typeof result.preview_id === "string") {
        const detail = state.activeCase!;
        currentWorkflow(detail).plot = result.preview_id;
        if (typeof result.goal === "string" && plotGoals.some(goal => goal.id === result.goal) &&
            typeof result.run_id === "string" && typeof result.node_count === "number" && typeof result.created_at === "string") {
          mergePreviewMetadata(detail, [{...result, validation_pending: true, reviewable: false} as unknown as Plot]);
        }
        state.caseView = "plots";
        persistInvestigationTabs(); render();
      }
      if (active.caseId && !["address-inspect", "change-output-lookup"].includes(active.action)) {
        state.results.set(active.caseId, {action: active.action, result});
        refreshJobView();
      }
      if (active.action === "miro-frame-review" && active.caseId) {
        const detail = state.activeCase;
        if (canApplyJobView(active) && detail?.id === active.caseId && detail.miro_board && frameRecoveryComplete(active.caseId, active.id, result)) {
          dialogAction = "miro-frame-recover";
          const content = frameRecoveryDialog(detail.id, boardUrl(detail.miro_board));
          if (!content) throw new Error("Reopen the investigation and review its interrupted frame again.");
          dialog.innerHTML = content;
          dialog.showModal();
          dialog.querySelector<HTMLButtonElement>('[data-action="close-dialog"]')?.focus();
          toast("Frame review ready. Inspect the linked board before confirming recovery.");
        } else {
          if (canApplyJobView(active)) resetFrameRecovery();
          toast("Frame lookup completed. Choose Recover interrupted frame again to review it in this investigation.");
        }
      } else if (active.action === "change-output-lookup" && active.caseId) {
        const applied = changeOutputsLookupComplete(active.caseId, active.id, result);
        toast(applied ? "Transaction outputs loaded. Choose the change output, then save." : "Transaction lookup completed. Open Change outputs and load the transaction again to review it.");
      } else if (active.action === "lookup") {
        if (!active.live || job.live === false) {
          throw new Error("This lookup used synthetic data. Load your transaction hashes again to start a live investigation.");
        }
        if (active.generation === pageGeneration && active.lookupTxids === state.draft.txids && state.page === "new") {
          applyLookupResult(result);
        } else toast("Transaction outputs are ready. Open Tasks and choose Review outputs to load them.");
      } else if (active.caseId) {
        const {detail, applied} = await refreshCaseDetail(active.caseId,
          () => active.action === "trace" && canApplyJobView(active), active.action);
        if (applied) {
          if (canApplyJobView(active) && ["plot", "plot-sync"].includes(active.action)) {
            const draft = currentWorkflow(detail);
            draft.plot = String(result.preview_id || "");
            persistInvestigationTabs();
            if (result.layout_mode === "update") {
              const board = detail.boards?.find(item => item.id === result.board_record_id);
              if (board) currentBoardDraft(detail, board).plot = draft.plot;
            }
            state.caseView = "plots";
            ensurePreviewSelection();
          }
          if (canApplyJobView(active) && ["board-create", "board-create-sync", "board-link", "plot-sync"].includes(active.action)) {
            const draft = currentWorkflow(detail);
            draft.board = String(result.record_id || result.id || "");
            const board = detail.boards?.find(item => item.id === draft.board);
            if (board) {
              const boardDraft = currentBoardDraft(detail, board);
              boardDraft.recoveryUrl = "";
              if (active.action === "board-create-sync" || active.action === "plot-sync" && result.published !== false) selectLayoutDestination(detail, "update", board);
              const requested = matchingBoardPlots(detail, board).find(plot => plot.preview_id === draft.boardPlot);
              if (requested && board.can_sync && !interruptedBoard(board)) boardDraft.plot = requested.preview_id;
            }
            draft.boardUrl = "";
            if (["plot-sync", "board-create-sync"].includes(active.action)) draft.boardPlot = "";
            state.caseView = "plots";
          }
          if (canApplyJobView(active) && ["pegouts", "pegouts-preview"].includes(active.action)) {
            currentPegoutSearch(detail);
            pegoutDraft.selected = String(result.search_id || result.run_id || "");
            pegoutDraft.approved = "";
          }
          if (canApplyJobView(active) && ["miro-recover", "miro-frame-recover", "miro-rebuild"].includes(active.action) && result.run_id && detail.runs?.some(run => run.id === result.run_id)) {
            state.selectedRun = result.run_id;
          }
        }
        if (active.action === "address-inspect" && state.activeCase?.id === active.caseId) {
          const address = String(result.address || "");
          const inspected = await api<AddressRow>(`/api/cases/${encodeURIComponent(active.caseId)}/address`,
            { address, run_id: state.selectedRun });
          if (viewingCase(active.caseId)) {
            const selected = state.addressReview.selected;
            if (selected?.address === address) selected.activity = inspected.activity;
            const row = state.addressReview.data?.rows.find(item => item.address === address);
            if (row) row.activity = inspected.activity;
          }
        }
        const key = `${active.caseId}:${result.run_id || detail.latest_run || "latest"}`;
        state.results.set(active.caseId, { action: active.action, result });
        if (["mermaid", "csv", "layout", "compact", "connections"].includes(active.action)) {
          state.artifacts.set(key, {
            ...state.artifacts.get(key),
            [active.action === "layout" ? "elk" : active.action]: {
              downloads: result.downloads || [],
              preview_url: safeLocalUrl(result.preview_url) || undefined,
              include_fees: result.include_fees ?? detail.run_defaults.include_fees,
              color_attribution_arrows: result.color_attribution_arrows ?? false,
              group_context_inputs: result.group_context_inputs ?? false,
              hub_addresses: result.hub_addresses ?? [],
              center_name: result.center_name ?? "",
              connector_style: result.connector_style,
              layout_metrics: result.layout_metrics,
              compaction: result.compaction,
              preview_id: result.preview_id,
              max_hops: result.max_hops, connection_count: result.connection_count, connection_status: result.connection_status, connection_scope: result.connection_scope,
              transaction_io: result.transaction_io, context_edge_count: result.context_edge_count,
              layout_algorithm: result.layout_algorithm,
              layout_attempts: result.layout_attempts,
              renderer: result.renderer,
              fallback_reason: result.fallback_reason,
            },
          });
        }
        toast(
          (
            {
              trace: "Collected data saved. Choose Plots & Miro to generate a chart.",
              "shared-trace": "Shared collection saved. Choose Shared collection as the data source in Plots & Miro.",
              plot: "Plot generated from saved collection data.",
              "plot-sync": result.published === false ? "No matching paths. The empty preview was saved; no Miro board was created." : result.layout_mode === "update" ? "Layout generated and the selected Miro board updated." : "Layout generated and synced to the new Miro board.",
              "board-create": "Miro board created. Prepare a board update to add its graph.",
              "board-create-sync": "Miro board created and synced with the reviewed layout.",
              "board-link": "Miro board linked. Prepare a board update to use its current arrangement.",
              "board-sync": "The selected Miro board was updated.",
              "address-counts": "Address transaction counts saved. Regenerate a preview or sync Miro. Run again to fetch any remaining missing counts.",
              "address-inspect": "Address activity saved. Review the history coverage before drawing conclusions.",
              mermaid: result.renderer === "direct_svg" ? "Direct SVG fallback is ready. " + fallbackNotice(result) : "Mermaid chart is ready.",
              layout: result.layout_algorithm === "dependency_layers_v1" ? "Dependency layout fallback is ready. " + fallbackNotice(result) : "ELK layout preview is ready.",
              compact: "Compaction comparison is ready. Review it before applying the layout to Miro.",
              "miro-compact": "The saved compact layout was applied to Miro.",
              pegouts: result.status === "paused" ? "Peg-out search paused. Review matches so far or resume the saved search." : result.status === "error" ? "Peg-out search saved with an error. Check the launching terminal, then resume." : "Peg-out search saved. Review its coverage and matching paths.",
              "pegouts-preview": "Peg-out preview is ready.",
              "miro-pegouts": "Peg-out snapshot publication finished.",
              connections: result.connection_count ? "Starter connection chart is ready." : "No connection found in the saved searched data. Nothing plotted.",
              "miro-connections": "Connection snapshot publication finished. Full trace board unchanged.",
              csv: "CSV export is ready to download.",
              "miro-preview": "Miro change preview is ready.",
              "miro-sync": "Miro sync finished.",
              "miro-frames": "Miro frames updated.",
              "address-merge": "Address conversion complete. Sync to Miro to refresh labels, then update frames when the graph is finished.",
              "miro-organize": "Miro sync and reorganization finished.",
              "miro-create": "Miro board created and linked.",
              "miro-rebuild": "Graph rebuilt on a new board. The previous board is preserved. Create frames when finished.",
              "miro-recover": "Recovery complete. The failed snapshot is selected. Choose Sync to Miro to resume.",
              "miro-frame-recover": result.resume_action === "miro-frames" ? "Frame recovery complete. The failed snapshot is selected. Choose Create / update Miro frames to resume." : "Frame recovery complete. Finish Sync to Miro for the selected snapshot, then create or update frames.",
            } as Record<string, string>
          )[active.action] || "Local action completed.",
        );
      } else
        toast(
          "Local action completed. Refresh the investigation to review its results.",
        );
    }
    if (active.action === "shared-trace" && state.activeCase?.id && state.activeCase.id !== active.caseId)
      await refreshSharedCollection(state.activeCase.id);
    // Output lookups only populate their editor; refreshing every investigation
    // can be expensive and must not delay displaying these completed results.
    if (!["lookup", "change-output-lookup"].includes(active.action)) void refreshSession().then(() => {if (state.page === "dashboard") render();}).catch(() => {});
    refreshJobView();
  } catch (error) {
    if (active.action.startsWith("miro-frame-") && canApplyJobView(active)) {resetFrameRecovery(); dialog.close();}
    // Keep potentially live work tracked during connection loss; a missing job after
    // restart needs investigator review instead of silently starting it again.
    if (error instanceof ApiError && error.status === 404) {
      active.status = "failed";
      active.finishedAt = Date.now();
      pruneFinishedJobs();
      active.cancellable = false;
      active.outcomeError = "The local server no longer tracks this action. Review the saved investigation before starting it again.";
      notifyTaskResult(active);
      if (viewingCase(active.caseId)) state.error = active.outcomeError;
      try { await refreshSession(); } catch { /* Retry from the next discovery poll. */ }
      refreshJobView();
    } else if (["running", "cancelling"].includes(active.status)) {
      active.message = "Waiting to reconnect to the local server…";
      active.progress = undefined;
      updateJobProgress();
      schedulePoll(identity, 3500);
    } else {
      active.outcomeError = error instanceof Error ? error.message : "Could not refresh the saved result.";
      if (viewingCase(active.caseId) || (active.action === "lookup" && active.generation === pageGeneration)) state.error = active.outcomeError;
      refreshJobView();
    }
  } finally { pollingJobs.delete(identity); }
}

function applyLookupResult(result: Result): void {
  state.draft.reports = result.transactions || [];
  state.draft.txids = state.draft.reports.map(report => report.txid).join(", ");
  state.draft.selected = new Set();
  toast(`Loaded outputs from ${state.draft.reports.length} transaction${state.draft.reports.length === 1 ? "" : "s"}. Choose the outputs to follow.`);
}

function updateJobClock(): void {
  for (const job of activeTasks()) {
    const element = document.querySelector(`[data-job-elapsed="${job.id}"]`);
    if (!element) continue;
    const elapsed = Math.max(0, Math.floor((Date.now() - job.started) / 1000),
      Number.isFinite(job.progress?.elapsed_seconds) ? Math.floor(job.progress!.elapsed_seconds!) : 0);
    element.textContent = elapsed >= 60 ? `${Math.floor(elapsed / 60)}m ${elapsed % 60}s elapsed` : `${elapsed}s elapsed`;
  }
}

async function openAddressMergeDialog(): Promise<void> {
  const detail = state.activeCase;
  if (!detail?.miro_board || isBusy()) return;
  const reviewed = await api<{
    approval_sha256: string; board_id: string; run_id: string; notice: string;
    address_objects_before: number; address_objects_after: number;
    duplicates_to_remove: number; connectors_to_redirect: number; resume: boolean;
  }>(`/api/cases/${encodeURIComponent(detail.id)}/address-merge-preview`, {});
  if (state.activeCase?.id !== detail.id || isBusy()) return;
  dialogAction = "address-merge";
  dialogMergeApproval = reviewed.approval_sha256;
  dialog.innerHTML = `<form id="action-form"><header class="dialog-head"><div><h2 id="dialog-title">${reviewed.resume ? "Resume" : "Review"} address conversion</h2><p>This preview is local. Applying changes the linked Miro graph.</p></div></header><div class="dialog-body"><div class="dialog-board"><span>Board</span><strong>${esc(reviewed.board_id)}</strong><span>Last synced run</span><strong>${esc(reviewed.run_id)}</strong><span>Address circles</span><strong>${reviewed.address_objects_before} → ${reviewed.address_objects_after}</strong><span>${reviewed.connectors_to_redirect} connectors redirected; ${reviewed.duplicates_to_remove} redundant circles removed</span></div><p>${esc(reviewed.notice)}</p><label class="check-line"><input type="checkbox" name="confirm_merge" required/><span>I reviewed this conversion and preserved any comments on redundant circles.</span></label><p>Manual edits to redundant circles or unmanaged connectors block conversion. SecretSpec and Proton Pass prompts appear in the launching terminal.</p></div><footer class="dialog-footer"><button type="button" class="btn" data-action="close-dialog">Cancel</button><button type="submit" class="btn primary">${reviewed.resume ? "Resume" : "Apply"} address conversion</button></footer></form>`;
  dialog.showModal();
  dialog.querySelector<HTMLButtonElement>('[data-action="close-dialog"]')?.focus();
}

function openSharedCollectionDialog(mode: "collect" | "continue"): void {
  const detail = state.activeCase;
  if (!detail || actionBusy("shared-trace")) return;
  const shared = detail.shared_collection;
  if (shared?.compatible === false || mode === "continue" && (!shared?.compatible || !shared.dataset_id || !shared.latest_run)) return;
  resetFrameRecovery();
  sharedDialog = {caseId: detail.id, mode, runId: mode === "continue" ? shared!.latest_run : undefined};
  dialogAction = "shared-trace";
  const settings = {...defaults, ...detail.run_defaults};
  const ids = [...new Set([...state.openCases, detail.id])];
  const names = ids.map(id => state.cases.find(item => item.id === id)?.name || (id === detail.id ? detail.name : id));
  dialog.innerHTML = `<form id="action-form"><header class="dialog-head"><div><h2>${mode === "collect" ? "Collect from open investigations" : "Continue shared data"}</h2><p>${mode === "collect" ? "Start a shared collection using the selected seed outputs of the open investigations." : "Continue the saved shared seed set and frontier. Open tabs do not change its membership."}</p></div><button type="button" class="dialog-close" data-action="close-dialog" aria-label="Close dialog">${icon("close")}</button></header><div class="dialog-body"><p>${esc(mode === "collect" ? names.join(", ") : (shared?.members || []).map(member => member.name).join(", "))}</p>${sharedDialog.runId ? `<p>Continue snapshot: <span class="mono">${esc(sharedDialog.runId)}</span></p>` : ""}<p class="artifact-note">Uses ${esc(detail.name)}’s collection limits and explicit stop-tracing rules. Attribution CSV hop limits are ignored. Each investigation later plots with its own seeds and rules.</p>${numericField(settings, "hops", mode === "collect" ? "Maximum hops" : "Additional hops", mode === "collect" ? "Maximum hop depth for this new shared collection." : "Adds to this shared snapshot’s ceiling; 0 retries eligible paths. Changing the named group sets a new maximum instead.")}${hopReferenceFields(settings, true)}${traceSummary(settings)}<p class="small muted">${detail.fixture ? "Uses the investigation’s saved synthetic source." : "Uses Blockstream API credits. Complete any credential prompt in the launching terminal."}</p></div><footer class="dialog-footer"><button type="button" class="btn" data-action="close-dialog">Cancel</button><button type="submit" class="btn primary">${mode === "collect" ? "Collect shared data" : "Continue shared data"}</button></footer></form>`;
  dialog.showModal();
  void suggestHopReferenceNames().catch(() => {});
}

function openActionDialog(action: string): void {
  resetFrameRecovery();
  const detail = state.activeCase;
  if (!detail || actionBusy(action)) return;
  if (action === "miro-recover" && (!detail.miro_board || !detail.miro_recovery?.can_confirm_empty)) return;
  if (action === "miro-frames" && (!currentRun() || !detail.miro_board || detail.miro_recovery?.pending_count)) return;
  const compact = action === "miro-compact" ? currentCompaction() : undefined;
  if (action === "miro-compact" && (!compact?.preview_id || !detail.miro_board || detail.miro_recovery?.pending_count)) return;
  dialogAction = action;
  if (action === "miro-rebuild") {openRebuildDialog(detail); return;}
  dialogPreviewId = compact?.preview_id || "";
  const settings = { ...defaults, ...detail.run_defaults };
  const titles: Record<string, string> = {
    trace: detail.latest_run
      ? "Continue collecting transaction data"
      : "Collect transaction data",
    "miro-sync": "Sync the graph to Miro",
    "miro-frames": "Create / update Miro frames",
    "miro-organize": "Sync and reorganize the Miro graph",
    "miro-compact": "Apply compact layout to Miro",
    "miro-create": "Create a private Miro board",
    "miro-recover": "Recover an empty-board sync",
  };
  const descriptions: Record<string, string> = {
    trace: detail.latest_run
      ? "Continue the latest saved frontier with another bounded allowance. Earlier runs remain available. Generate plots separately after collection."
      : "Follow the selected starting outputs within the limits below. Generate plots separately after collection.",
    "miro-sync":
      "Add the selected saved graph to the linked board. Existing manual arrangements are preserved during normal sync. Create or update frames separately when the graph is finished.",
    "miro-frames":
      "Finish syncing and arranging the graph first. This action creates or updates its frames around the current Miro positions. It does not retrace or run ELK. If you change the graph later, run this action again.",
    "miro-organize":
      "Sync the selected run and apply a fresh layout to the managed graph in one action. ELK calculates the full graph layout before board updates begin. This replaces existing positions, including arrangements made by hand, and sets transaction inputs on the left and outputs on the right.",
    "miro-compact":
      "Apply the saved compact preview to the selected run. This replaces the positions of managed graph objects, including any manual arrangements. Earlier manual moves on Miro are not copied into the local preview. Other board objects remain outside this action. Create or update frames separately when the graph is finished.",
    "miro-create":
      "Create an empty private board in your Miro account and link it to this investigation. Publishing the graph is a separate sync action.",
    "miro-recover":
      "Verify that the board is empty, clear the unconfirmed initial batch locally, and use individual shape requests for the next sync. This action does not sync or retrace the investigation.",
  };
  const live = action !== "trace" || !detail.fixture;
  const defaultBoardName =
    `${detail.fixture ? "SYNTHETIC DATA · " : ""}${detail.name}`.slice(0, 60);
  dialog.innerHTML = `<form id="action-form"><header class="dialog-head"><div><h2 id="dialog-title">${titles[action]}</h2><p>${descriptions[action]}</p></div><button type="button" class="dialog-close" data-action="close-dialog" aria-label="Close dialog">${icon("close")}</button></header><div class="dialog-body">${action === "trace" ? `${numericField(settings, "hops", "Hops for this run", "First run or changed named group: maximum hops under the new basis. Same hop basis: additional hops beyond the latest run’s limit; 0 retries eligible paths. This value does not change saved defaults.")}${hopReferenceFields(settings)}${traceSummary(settings)}<p class="small muted">Optional run budgets can be enabled in Investigation settings.</p><button type="button" class="btn small" data-action="edit-case-settings">Edit investigation settings</button>` : action === "miro-create" ? `<label class="field"><span>Board name</span><input name="board_name" required maxlength="60" value="${esc(defaultBoardName)}"/></label>` : action === "miro-recover" ? `<div class="dialog-board"><span>Linked board</span><a class="board-link" href="${esc(boardUrl(detail.miro_board!))}" target="_blank" rel="noopener noreferrer">Open linked board ${icon("external")}</a><span>${detail.miro_recovery?.pending_count} unconfirmed items</span></div><label class="check-line"><input type="checkbox" name="confirm_empty" required/><span><strong>I inspected this Miro board after the failed sync and it is empty.</strong><small>If any objects are present, cancel and reconcile the pending items individually.</small></span></label>` : `<div class="dialog-board"><span>Linked board</span><strong>${esc(detail.miro_board)}</strong><span style="margin-top:10px">Selected snapshot</span><strong class="mono">${esc(currentRun()?.id || detail.latest_run)}</strong><span style="margin-top:10px">New item budget</span><strong>${settings.budget_limits_enabled && settings.max_new_items > 0 ? `${esc(settings.max_new_items)} items` : "No new Miro item cap"}</strong>${compact ? `<span style="margin-top:10px">Saved preview</span><strong class="mono">${esc(compact.preview_id)}</strong><span>${esc(human(compact.connector_style || "straight"))} connectors · ${compact.include_fees ? "Fee flows included" : "Fee flows hidden"}</span>` : ""}</div>`}${live ? `<div class="alert ${["miro-organize", "miro-compact"].includes(action) ? "warning" : ""}">${icon(["miro-organize", "miro-compact"].includes(action) ? "info" : "lock")}<div><strong>${action === "trace" ? "Uses your Blockstream API credits" : action === "miro-recover" ? "Reads Miro and updates local recovery state" : "Changes your Miro workspace"}</strong><p>SecretSpec retrieves credentials through the launching terminal. Complete any Proton Pass prompt there.</p></div></div>` : '<div class="alert">' + icon("shield") + "<div><strong>Offline synthetic data</strong><p>This run uses the investigation’s saved fixture and needs no API credentials.</p></div></div>"}</div><footer class="dialog-footer"><button type="button" class="btn" data-action="close-dialog">Cancel</button><button type="submit" class="btn primary">${icon(action === "trace" ? "play" : action === "miro-create" ? "plus" : "refresh")}${action === "trace" ? "Collect transaction data" : action === "miro-create" ? "Create private board" : action === "miro-organize" ? "Sync and reorganize" : action === "miro-compact" ? "Apply compact layout" : action === "miro-frames" ? "Create / update frames" : action === "miro-recover" ? "Verify empty board and recover" : "Sync to Miro"}</button></footer></form>`;
  dialog.showModal();
  if (action === "trace") void suggestHopReferenceNames().catch(() => {});
}

async function caseAction(action: string): Promise<void> {
  const detail = state.activeCase;
  if (!detail) return;
  await startJob(
    `/api/cases/${encodeURIComponent(detail.id)}/actions`,
    { action, run_id: state.selectedRun },
    action,
    false,
    detail.id,
  );
}

async function dispatch(action: string, element?: HTMLElement): Promise<void> {
  if (["shared-collect-dialog", "shared-continue-dialog"].includes(action)) {
    openSharedCollectionDialog(action === "shared-collect-dialog" ? "collect" : "continue"); return;
  }
  if (action === "toggle-system-notifications") {
    const result = taskNotifications.toggle();
    render();
    toast(await result);
    render();
    return;
  }
  if (action === "export-open-endpoints") {await exportOpenEndpoints(); return;}
  if (action === "dismiss-endpoint-export") {
    if (!state.endpointExport.pending) {state.endpointExport.result = null; state.endpointExport.error = ""; render();}
    return;
  }
  if (action === "close-investigation-tab") {
    await closeInvestigationTab(element?.dataset.id || "");
    return;
  }
  if (["open-job", "dismiss-job", "load-job-outputs"].includes(action)) {
    const id = element?.dataset.id || "";
    const loading = [...sharedCollectionLoads.values()].find(load => load.task.id === id);
    const job = state.jobs.get(id) || loading?.task;
    if (!job) return;
    if (action === "dismiss-job" && !["running", "cancelling"].includes(job.status)) {
      if (loading) sharedCollectionLoads.delete(job.caseId!);
      else {state.jobs.delete(job.id); dismissedJobs.add(job.id);}
      updateJobProgress();
    } else if (action === "open-job" && job.caseId) {
      await openCase(job.caseId);
      if (!loading && viewingCase(job.caseId) && job.status === "failed") {
        state.error = job.outcomeError || job.message;
        state.editConflicts = job.outcome?.edit_conflicts ? {caseId: job.caseId, report: job.outcome.edit_conflicts} : null;
        render();
      }
    } else if (action === "load-job-outputs" && job.outcome?.result && job.live && job.status === "succeeded") {
      if (scopeBusy()) return;
      navigate("new"); applyLookupResult(job.outcome.result); render();
    }
    return;
  }
  if (action === "retry-case-section" && state.activeCase && element?.dataset.section && element.dataset.section in sectionPaths) {
    void loadCaseSection(state.activeCase.id, element.dataset.section as CaseSection, true); render(); return;
  }
  if (["preview-select", "preview-load-more", "preview-selection-retry"].includes(action) && state.activeCase) {
    const detail = state.activeCase;
    if (element?.dataset.caseId !== detail.id) return;
    if (action === "preview-load-more") {void loadOlderPreviews(detail.id); return;}
    if (action === "preview-selection-retry") {void loadPreviewSelection(detail.id, currentWorkflow(detail).plot, true); return;}
    if (draftBusy() || !detail.plots?.some(plot => plot.preview_id === element.dataset.preview)) return;
    viewRevision++;
    currentWorkflow(detail).plot = element.dataset.preview!;
    previewLibraryState(detail.id).selectionError = "";
    persistInvestigationTabs(); render();
    const selected = document.querySelector<HTMLElement>(".selected-preview-details");
    selected?.focus({preventScroll: true}); selected?.scrollIntoView({block: "nearest"}); return;
  }
  if (action === "verify-saved-plot" && state.activeCase && element?.dataset.preview) {
    if (element.dataset.caseId && element.dataset.caseId !== state.activeCase.id) return;
    void verifySelectedPlot(state.activeCase.id, element.dataset.preview, true); return;
  }
  if (action.startsWith("view-") && state.activeCase) {
    const view = action.slice(5);
    if (["collect", "plots", "boards", "history"].includes(view)) {
      saveSettingsDraft(); saveAddressDraft();
      cancelCaseOpening();
      pageGeneration++;
      state.caseView = view === "boards" ? "plots" : view as CaseView; state.page = "case"; render();
      if (view === "plots" || view === "boards") void suggestCenterNames().catch(() => {});
      loadVisibleSections();
    }
    return;
  }
  if (await workflowAction(action, element)) return;
  if (await pegoutAction(action)) return;
  if (action === "address-import-open") action = "input-import-open";
  if (action === "input-import-open" && state.activeCase && !isBusy()) {
    saveSettingsDraft(); saveAddressDraft(); cancelCaseOpening(); state.page = "case-settings";
  }
  const inputImportCaseId = state.activeCase?.id;
  if (inputImportCaseId && await inputImportAction(action, {
      caseId: inputImportCaseId, busy: isBusy(), render,
      post: (path, body) => api(path, body as Record<string, unknown>),
      refresh: async () => {invalidateImportedInputs(inputImportCaseId);}
    }, element)) return;
  if (action === "miro-frame-review") {
    const detail = state.activeCase;
    if (!detail?.miro_board || !detail.miro_recovery?.can_recover_frame || isBusy()) return;
    const version = beginFrameRecovery(detail.id);
    const identity = await startJob(`/api/cases/${encodeURIComponent(detail.id)}/actions`, {action}, action, true, detail.id);
    if (identity) frameRecoveryStarted(detail.id, version, identity);
    return;
  }
  if (action === "change-outputs-open" && state.activeCase && !isBusy()) {saveSettingsDraft(); saveAddressDraft(); cancelCaseOpening(); state.page = "case-settings"; render();}
  if (state.activeCase && await changeOutputsAction(action, {
      caseId: state.activeCase.id, busy: isBusy(), render,
      post: (path, body) => api(path, body as Record<string, unknown>),
      startLookup: async txid => {
        const detail = state.activeCase;
        if (!detail) return null;
        return await startJob(`/api/cases/${encodeURIComponent(detail.id)}/actions`,
          {action: "change-output-lookup", txid}, "change-output-lookup", !detail.fixture, detail.id);
      }
    }, element)) return;
  if (action === "name-colors-open" && state.activeCase && !isBusy()) {saveSettingsDraft(); saveAddressDraft(); cancelCaseOpening(); if (state.page !== "case-settings") navigate("case-settings");}
  if (state.activeCase && await nameColorsAction(action, {caseId: state.activeCase.id, busy: isBusy(), render,
      post: (path, body) => api(path, body as Record<string, unknown>)}, element)) return;
  const addressImportCaseId = state.activeCase?.id, addressImportGeneration = pageGeneration;
  if (addressImportCaseId && await addressImportAction(action, {
      caseId: addressImportCaseId, busy: isBusy(), render,
      post: (path, body) => api(path, body as Record<string, unknown>),
      refresh: async () => {
        invalidateAddressReview(addressImportCaseId);
        if (viewingCase(addressImportCaseId) && addressImportGeneration === pageGeneration) await loadAddresses(0);
      }
    })) return;
  if (action === "address-merge-dialog") {
    await openAddressMergeDialog();
    return;
  }
  if (action === "cancel-job") {
    if (element?.dataset.id) await cancelJob(element.dataset.id);
    return;
  }
  if (action === "dismiss-error") {
    state.error = "";
    state.editConflicts = null;
    render();
    return;
  }
  if (action === "close-dialog") {
    dialog.close();
    return;
  }
  if (action === "dismiss-result") {
    if (state.activeCase) state.results.delete(state.activeCase.id);
    render();
    return;
  }
  if (action === "new" || action === "dashboard") {
    navigate(action);
    return;
  }
  if (action === "refresh") {
    await refreshSession();
    render();
    return;
  }
  if (action === "open-case" && element?.dataset.id) {
    await openCase(element.dataset.id);
    return;
  }
  if (action === "addresses") {
    await loadAddresses(0);
    return;
  }
  if (action === "address-prev" || action === "address-next") {
    const page = state.addressReview.data;
    if (page) await loadAddresses(Math.max(0, page.offset + (action === "address-next" ? page.limit : -page.limit)));
    return;
  }
  if (action === "address-select" && element?.dataset.address) {
    const row = state.addressReview.data?.rows.find(item => item.address === element.dataset.address);
    if (row) selectAddress(row);
    render();
    document.querySelector<HTMLHeadingElement>("#address-detail-title")?.focus();
    return;
  }
  if (action === "address-inspect") {
    const detail = state.activeCase, selected = state.addressReview.selected;
    if (!detail || !selected) return;
    saveAddressDraft();
    await startJob(`/api/cases/${encodeURIComponent(detail.id)}/actions`,
      { action, address: selected.address, run_id: state.selectedRun }, action, !detail.fixture, detail.id);
    return;
  }
  if (action === "service-remove") {
    await saveService(false);
    return;
  }
  if (action === "case-settings") {
    navigate("case-settings");
    return;
  }
  if (action === "back-case") {
    navigate("case");
    return;
  }
  if (action.endsWith("-dialog")) {
    openActionDialog(action.slice(0, -7));
    return;
  }
  if (action === "lookup") {
    saveDraft();
    if (!state.draft.txids.trim())
      throw new Error(
        "Enter at least one transaction hash before loading outputs.",
      );
    await startJob(
      "/api/lookup",
      { source: "live", blockchain: state.draft.blockchain, txids: state.draft.txids },
      "lookup",
      true,
    );
    return;
  }
  if (action === "connections" || action === "miro-connections") {
    const detail = state.activeCase;
    if (!detail || isBusy()) return;
    const run = currentRun();
    const body: Record<string, unknown> = {action, run_id: run?.id || "latest"};
    if (action === "miro-connections") {
      const artifact = state.artifacts.get(`${detail.id}:${run?.id || detail.latest_run || "latest"}`)?.connections || detail.artifacts?.[run?.id || detail.latest_run || ""]?.connections;
      if (!artifact?.preview_id) throw new Error("Create and review a connection preview first.");
      if (!document.querySelector<HTMLInputElement>("#connection-confirm")?.checked) throw new Error("Review the snapshot and confirm publication first.");
      const board = document.querySelector<HTMLInputElement>("#connection-board")?.value.trim();
      if (!board) throw new Error("Enter a separate Miro board URL or ID.");
      Object.assign(body, {preview_id: artifact.preview_id, board, confirm_connections: true});
    }
    await startJob(`/api/cases/${encodeURIComponent(detail.id)}/actions`, body, action, action === "miro-connections", detail.id);
    return;
  }
  if (["mermaid", "csv", "layout", "compact", "miro-preview", "address-counts"].includes(action))
    await caseAction(action);
}

function handleError(error: unknown): void {
  resetFrameRecovery();
  state.editConflicts = null;
  if (dialogAction === "miro-frame-recover") dialog.close();
  state.error =
    error instanceof Error
      ? error.message
      : "Unable to complete the local action.";
  toast(state.error, true);
  saveDraft();
  render();
}

app.addEventListener("click", (event) => {
  const element = (event.target as Element).closest<HTMLElement>("button, a");
  if (!element || (element instanceof HTMLButtonElement && element.disabled))
    return;
  if (element.dataset.page) {
    navigate(element.dataset.page as Page);
    return;
  }
  if (element.dataset.case) {
    void openCase(element.dataset.case).catch(handleError);
    return;
  }
  if (element.dataset.run) {
    viewRevision++;
    state.selectedRun = element.dataset.run;
    render();
    return;
  }
  if (element.dataset.action)
    void dispatch(element.dataset.action, element).catch(handleError);
});

app.addEventListener("change", (event) => {
  const element = event.target as HTMLInputElement | HTMLSelectElement;
  if (element.name === "layout_style") {
    const form = element.closest<HTMLFormElement>("#plot-layout-form, #settings-form");
    if (!form || (form.id === "plot-layout-form" && draftBusy())) return;
    if (element.value === "trace") {
      const grouping = form.querySelector<HTMLInputElement>('input[name="group_context_inputs"]');
      if (grouping) grouping.checked = true;
    }
    if (form.id === "plot-layout-form") {savePlotLayoutDraft(form); viewRevision++;}
    else saveSettingsDraft();
    return;
  }
  if (element.name === "budget_limits_enabled" && element.closest("#settings-form")) {
    render();
    return;
  }
  if (element.closest("#plot-layout-form")) {
    if (!draftBusy()) {savePlotLayoutDraft(); viewRevision++;}
    return;
  }
  if (workflowInput(element)) return;
  if (pegoutInput(element)) return;
  if (element.id === "input-import-files") {
    void inputImportFiles(element as HTMLInputElement, render, isBusy()).catch(handleError);
    return;
  }
  if (inputImportInput(element)) {render(); return;}
  if (element.id === "change-outputs-file") {
    void changeOutputsFile(element as HTMLInputElement, render, isBusy()).catch(handleError);
    return;
  }
  if (changeOutputsInput(element)) return;
  if (element.id === "name-color-import-file") {
    void nameColorsFile(element as HTMLInputElement, render, isBusy()).catch(handleError);
    return;
  }
  if (element.id === "attribution-file") {
    void addressImportFile(element as HTMLInputElement, render).catch(handleError);
    return;
  }
  if (nameColorsInput(element as HTMLInputElement)) return;
  if (addressImportInput(element)) return;
  if (element.closest("#service-form")) saveAddressDraft();
  if (element.name === "suspected_only") {
    state.addressReview.suspectedOnly = (element as HTMLInputElement).checked;
    return;
  }
  if (element.id === "address-run-picker") {
    viewRevision++;
    state.selectedRun = element.value;
    state.addressReview.selected = null;
    void loadAddresses(0).catch(handleError);
    return;
  }
  if (element.id === "run-picker") {
    viewRevision++;
    state.selectedRun = element.value;
    if (state.activeCase?.sections) void loadCaseSection(state.activeCase.id, "collection", true);
    render();
    return;
  }
  if (element instanceof HTMLInputElement && element.dataset.outpoint) {
    if (element.checked) state.draft.selected.add(element.dataset.outpoint);
    else state.draft.selected.delete(element.dataset.outpoint);
    const count = document.querySelector("#selection-count");
    if (count)
      count.textContent = `${state.draft.selected.size} starting output${state.draft.selected.size === 1 ? "" : "s"} selected`;
    return;
  }
});

// Keep an in-memory draft while a lookup runs so its completion does not
// discard names, notes, or limit edits typed in the meantime.
app.addEventListener("input", (event) => {
  if ((event.target as HTMLInputElement).name === "center_name" && state.page === "case" && state.caseView === "plots") {
    clearTimeout(centerNameTimer);
    centerNameTimer = setTimeout(() => { void suggestCenterNames().catch(() => {}); }, 150);
  }
  if ((event.target as Element).closest("#plot-layout-form")) {
    if (!draftBusy()) {savePlotLayoutDraft(); viewRevision++;}
    return;
  }
  if (workflowInput(event.target as HTMLInputElement | HTMLSelectElement)) return;
  if (pegoutInput(event.target as HTMLInputElement | HTMLSelectElement)) return;
  if (inputImportInput(event.target as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement)) return;
  if (changeOutputsInput(event.target as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement)) return;
  if (nameColorsInput(event.target as HTMLInputElement)) return;
  if (addressImportInput(event.target as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement)) return;
  if ((event.target as Element).closest("#new-case-form")) saveDraft();
  if ((event.target as Element).closest("#service-form")) saveAddressDraft();
  if ((event.target as HTMLInputElement).name === "address_query") state.addressReview.query = (event.target as HTMLInputElement).value;
  if ((event.target as HTMLInputElement).name === "pasted_address") state.addressReview.pasted = (event.target as HTMLInputElement).value;
});

app.addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target as HTMLFormElement;
  if (isBusy() || !form.reportValidity()) return;
  void (async () => {
    if (form.id === "plot-layout-form") {
      if (state.activeCase && await persistPlotLayoutSettings(state.activeCase)) toast("Layout settings saved for this investigation.");
    } else if (form.id === "address-search-form") {
      const data = new FormData(form);
      state.addressReview.query = String(data.get("address_query") || "");
      state.addressReview.suspectedOnly = data.get("suspected_only") === "on";
      await loadAddresses(0);
    } else if (form.id === "address-open-form") {
      const address = String(new FormData(form).get("pasted_address") || "").trim();
      const detail = state.activeCase;
      if (!detail) return;
      const generation = pageGeneration;
      const row = await api<AddressRow>(`/api/cases/${encodeURIComponent(detail.id)}/address`, { address, run_id: state.selectedRun });
      if (generation !== pageGeneration || state.activeCase?.id !== detail.id) return;
      selectAddress(row);
      render();
    } else if (form.id === "service-form") {
      await saveService();
    } else if (form.id === "new-case-form") {
      saveDraft();
      const seeds = [
        ...new Set([
          ...state.draft.selected,
          ...state.draft.seeds.split(/[\s,]+/).filter(Boolean),
        ]),
      ];
      if (!seeds.length)
        throw new Error(
          "Select at least one starting output, or enter an exact transaction hash followed by :NUMBER.",
        );
      if (seeds.some((seed) => !/^[0-9a-fA-F]{64}:\d+$/.test(seed)))
        throw new Error(
          "Each starting output must be a 64-character transaction hash followed by a numeric output index, such as :0.",
        );
      const generation = pageGeneration;
      submitting = true;
      render();
      try {
        const created = await api<Case>("/api/cases", {
          name: state.draft.name,
          source: "live",
          blockchain: state.draft.blockchain,
          seeds,
          settings: state.draft.settings,
        });
        await refreshSession();
        addOpenInvestigation(created.id);
        if (generation === pageGeneration) {
          const opening = openCase(created.id);
          const openingGeneration = pageGeneration;
          await opening;
          if (openingGeneration === pageGeneration) {
            state.draft = {
              name: "",
              txids: "",
              seeds: "",
              blockchain: "liquid",
              settings: { ...state.settings },
              reports: [],
              selected: new Set(),
            };
          }
        }
        toast(
          "Investigation created. Import your data or start the first run.",
        );
      } catch (error) {
        if (generation === pageGeneration) throw error;
        toast(`Could not finish creating the investigation. ${error instanceof Error ? error.message : "Check the launching terminal."}`, true);
      } finally {
        submitting = false;
        render();
      }
    } else if (form.id === "settings-form") {
      const generation = pageGeneration;
      const savedKey = settingsKey();
      saveSettingsDraft();
      const submittedDraft = JSON.stringify(settingsDrafts.get(savedKey));
      const previous = settingsDrafts.get(savedKey)?.settings || {...defaults, ...(state.page === "case-settings" ? state.activeCase?.run_defaults : state.settings)};
      const settings = {...readSettings(form, previous), ...(state.page === "case-settings" ? selectLayoutSettings(previous) : {})};
      const data = new FormData(form);
      try {
        if (state.page === "case-settings" && state.activeCase) {
          const caseId = state.activeCase.id;
          const updated = await api<Case>(`/api/cases/${encodeURIComponent(caseId)}/settings`, {
            name: String(data.get("name") || ""),
            board: state.activeCase.miro_board || "",
            settings,
          });
          if (state.activeCase?.id === caseId && updated.id) state.activeCase = mergeCase(state.activeCase, updated);
          else await refreshCaseDetail(caseId);
        } else {
          await api("/api/settings", { settings });
          state.draft.settings = { ...settings };
        }
        await refreshSession();
        saveSettingsDraft();
        if (JSON.stringify(settingsDrafts.get(savedKey)) === submittedDraft) {
          settingsDrafts.delete(savedKey);
          if (renderedSettingsKey === savedKey) renderedSettingsKey = "";
        }
        render();
        toast("Settings saved locally.");
      } catch (error) {
        if (generation === pageGeneration) throw error;
        toast(`Could not finish saving settings. ${error instanceof Error ? error.message : "Check the launching terminal."}`, true);
      }
    }
  })().catch(handleError);
});

dialog.addEventListener("close", () => {
  if (dialogAction === "miro-frame-recover") resetFrameRecovery();
});
dialog.addEventListener("cancel", () => {
  if (dialogAction === "miro-frame-recover") resetFrameRecovery();
});
dialog.addEventListener("click", (event) => {
  if ((event.target as Element).closest('[data-action="edit-case-settings"]')) {
    dialog.close(); void dispatch("case-settings").catch(handleError); return;
  }
  if ((event.target as Element).closest('[data-action="close-dialog"]'))
    dialog.close();
});
dialog.addEventListener("input", (event) => {
  if ((event.target as HTMLInputElement).name !== "hop_reference_name") return;
  clearTimeout(hopReferenceNameTimer);
  hopReferenceNameTimer = setTimeout(() => { void suggestHopReferenceNames().catch(() => {}); }, 150);
});

dialog.addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target as HTMLFormElement;
  const detail = state.activeCase;
  if (!detail || actionBusy(dialogAction) || !form.reportValidity()) return;
  const action = dialogAction;
  if (action === "shared-trace") {
    if (!sharedDialog || sharedDialog.caseId !== detail.id) return;
    const data = new FormData(form), captured = {...sharedDialog};
    const body: Record<string, unknown> = {action, mode: captured.mode, hops: Number(data.get("hops")),
      hop_reference_name: String(data.get("hop_reference_name") || "").trim()};
    if (captured.mode === "collect") body.case_ids = [...new Set([...state.openCases, detail.id])];
    else body.run_id = captured.runId;
    void startJob(`/api/cases/${encodeURIComponent(detail.id)}/actions`, body, action, !detail.fixture, detail.id).catch(handleError);
    return;
  }
  if (action === "miro-frames" && (!currentRun() || !detail.miro_board || detail.miro_recovery?.pending_count)) return;
  if (action === "miro-frame-recover") {
    try {
      if (!detail.miro_recovery?.can_recover_frame || !detail.miro_board) throw new Error("Review the interrupted frame again before recovering.");
      const data = new FormData(form);
      const approval = frameRecoveryApproval(detail.id, String(data.get("frame_item_id") || ""), data.get("confirm_frame_absent") === "on");
      void startJob(`/api/cases/${encodeURIComponent(detail.id)}/actions`, {action, ...approval}, action, true, detail.id).catch(handleError);
    } catch (error) {handleError(error);}
    return;
  }
  const body: Record<string, unknown> = {
    action,
    run_id: action === "trace" ? "latest" : currentRun()?.id || state.selectedRun,
  };
  if (action === "miro-rebuild") {
    if (!dialogRebuild || dialogRebuild.caseId !== detail.id) return;
    const data = new FormData(form);
    body.run_id = dialogRebuild.runId;
    body.source_board = dialogRebuild.sourceBoard;
    body.name = String(data.get("board_name") || "");
    body.max_new_items = dialogRebuild.budgetLimitsEnabled ? Number(data.get("max_new_items")) : 0;
  }
  if (action === "trace") {
    const data = new FormData(form);
    body.hops = Number(data.get("hops"));
    body.hop_reference_name = data.has("hop_reference_name")
      ? String(data.get("hop_reference_name") || "").trim() : (detail.run_defaults.hop_reference_name || "");
  }
  if (action === "miro-compact") body.preview_id = dialogPreviewId;
  if (action === "address-merge") {
    body.approval_sha256 = dialogMergeApproval;
    body.confirm_merge = new FormData(form).get("confirm_merge") === "on";
  }
  if (action === "miro-recover") body.confirm_empty = new FormData(form).get("confirm_empty") === "on";
  if (action === "miro-create")
    body.name = String(new FormData(form).get("board_name") || "");
  const live = action !== "trace" || !detail.fixture;
  void startJob(
    `/api/cases/${encodeURIComponent(detail.id)}/actions`,
    body,
    action,
    live,
    detail.id,
  ).catch((error) => {
    dialog.close();
    handleError(error);
  });
});

// Arrow keys move between buttons and download links; other controls keep
// their browser-native behavior. Tab and Shift+Tab still visit every control.
document.addEventListener("keydown", (event) => {
  if (
    !["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key) ||
    event.altKey ||
    event.ctrlKey ||
    event.metaKey
  )
    return;
  const current = document.activeElement;
  if (!(current instanceof HTMLButtonElement) &&
      !(current instanceof HTMLAnchorElement && current.matches(".btn[href]"))) return;
  const scope = dialog.open ? dialog : app;
  const buttons = [
    ...scope.querySelectorAll<HTMLButtonElement | HTMLAnchorElement>(
      "button:not(:disabled), a.btn[href]",
    ),
  ].filter(
    (item) =>
      item !== current &&
      item.getClientRects().length &&
      getComputedStyle(item).visibility !== "hidden",
  );
  const rect = current.getBoundingClientRect();
  const x = rect.x + rect.width / 2,
    y = rect.y + rect.height / 2;
  const horizontal = ["ArrowLeft", "ArrowRight"].includes(event.key);
  const forward = ["ArrowDown", "ArrowRight"].includes(event.key);
  const choices = buttons
    .map((item) => {
      const target = item.getBoundingClientRect();
      const dx = target.x + target.width / 2 - x,
        dy = target.y + target.height / 2 - y;
      const primary = horizontal ? dx : dy,
        secondary = horizontal ? dy : dx;
      return {
        item,
        primary,
        score: Math.abs(primary) + Math.abs(secondary) * 2,
      };
    })
    .filter((item) => (forward ? item.primary > 3 : item.primary < -3))
    .sort((a, b) => a.score - b.score);
  if (choices[0]) {
    event.preventDefault();
    choices[0].item.focus();
  }
});

window.addEventListener("hashchange", () => {
  const match = location.hash.match(/^#case\/([a-z0-9]+)$/);
  if (match) void openCase(match[1]).catch(handleError);
});

async function initialize(): Promise<void> {
  await refreshSession();
  restoreInvestigationTabs();
  state.draft.settings = { ...state.settings };
  const match = location.hash.match(/^#case\/([a-z0-9]+)$/);
  if (match) await openCase(match[1]);
  else render();
}

window.addEventListener("pagehide", rememberInvestigationView);

app.addEventListener("toggle", event => {
  const target = event.target as HTMLDetailsElement;
  if (target.classList?.contains("task-list")) tasksOpen = target.open;
}, true);
setInterval(updateJobClock, 1000);
setInterval(() => void discoverJobs(), 3500);
void initialize().catch((error) => {
  app.innerHTML = `<main class="startup"><h1>The local server is unavailable</h1><p>${esc(error instanceof Error ? error.message : "Could not open the workspace.")}</p><p class="muted">Launch liquid-web from your devenv terminal, then open its local URL.</p><button type="button" class="btn primary" id="retry-start">Try again</button></main>`;
  document
    .querySelector("#retry-start")
    ?.addEventListener("click", () => location.reload());
});
