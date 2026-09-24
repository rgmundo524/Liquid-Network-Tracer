import {beginFrameRecovery, frameRecoveryStarted, frameRecoveryComplete, frameRecoveryDialog, frameRecoveryApproval, resetFrameRecovery} from "./frame-recovery";
import {nameColorsPanel, nameColorsInput, nameColorsAction, nameColorsFile, resetNameColors} from "./name-colors";
import { addressImportPanel, addressImportInput, addressImportFile, addressImportAction, resetAddressImport } from "./address-import";
import {changeOutputsPanel, changeOutputsInput, changeOutputsFile, changeOutputsAction, changeOutputsLookupComplete, changeOutputsPending, resetChangeOutputs} from "./change-outputs";
import {inputImportPanel, inputImportInput, inputImportFiles, inputImportAction, inputImportPending, resetInputImport} from "./input-import";
export {};

type ConnectorStyle = "straight" | "curved" | "elbowed";
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
  max_transactions: number;
  max_outpoints: number;
  max_requests: number;
  max_seconds: number;
  max_new_items: number;
  layout_attempts: number;
  include_fees: boolean;
  color_attribution_arrows: boolean;
  group_context_inputs: boolean;
  hub_addresses: string[];
  center_name: string;
  connector_style: ConnectorStyle;
};
type Run = {
  id: string;
  status: string;
  stop_reason?: string;
  created_at?: string;
  transaction_count?: number;
  frontier_count?: number;
  max_hops?: number;
};
type Download = { name: string; url: string };
type Artifact = RenderingMetadata & {
  max_hops?: number; connection_count?: number; connection_status?: string;
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
};
type PlotGoal = "full" | "connections" | "pegouts";
type Plot = {
  preview_id: string; goal: PlotGoal; run_id: string; min_hops: number; max_hops: number | null;
  created_at: string; status: string; node_count: number; edge_count: number; transaction_count: number;
  match_count?: number; connection_count?: number; source_max_hops?: number; source_run_status?: string;
  source_stop_reason?: string; notice?: string; coverage_notice?: string; reviewable: boolean; reason?: string; empty?: boolean; artifact?: Artifact;
};
type InvestigationBoard = {
  id: string; name: string; goal: PlotGoal; board_id: string; board_url: string; status: string;
  preview_id?: string; run_id?: string; legacy_snapshot?: boolean; can_sync: boolean; notice?: string; pending_count?: number;
};
type Case = {
  id: string;
  name: string;
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
  address_counts?: CountReport;
  max_hops?: number; connection_count?: number; connection_status?: string;
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
type Job = {
  id: string;
  status: "running" | "cancelling" | "canceled" | "succeeded" | "failed";
  message: string;
  result?: Result;
  action?: string;
  case_id?: string;
  live?: boolean;
  progress?: JobProgress;
  cancellable?: boolean;
  started_at?: number;
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
  id: string;
  action: string;
  caseId?: string;
  started: number;
  message: string;
  live: boolean;
  progress?: JobProgress;
  cancellable: boolean;
  cancelling: boolean;
};

const defaults: Settings = {
  hops: 1,
  max_transactions: 20,
  max_outpoints: 100,
  max_requests: 30,
  max_seconds: 60,
  max_new_items: 750,
  layout_attempts: 25,
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
  selectedRun: "latest",
  addressReview: {
    query: "", suspectedOnly: false, data: null as AddressPage | null,
    selected: null as AddressRow | null, loading: false,
    name: "", notes: "", enabled: false, pasted: "",
    confidence: "suspected", source: "Investigator designation", observedAt: "", stopTracing: true, hopLimit: "",
  },
  job: null as ActiveJob | null,
  error: "",
  results: new Map<string, { action: string; result: Result }>(),
  artifacts: new Map<string, RunArtifacts>(),
  draft: {
    name: "",
    txids: "",
    seeds: "",
    board: "",
    settings: { ...defaults },
    reports: [] as Report[],
    selected: new Set<string>(),
  },
};

const app = document.querySelector<HTMLDivElement>("#app")!;
const dialog = document.querySelector<HTMLDialogElement>("#action-dialog")!;
let pollTimer: ReturnType<typeof setTimeout> | undefined;
let dialogAction = "";
let dialogMergeApproval = "";
let dialogPreviewId = "";
let dialogRebuild: { caseId: string; sourceBoard: string; runId: string } | null = null;
let submitting = false;
let pageGeneration = 0;
let workflowDraft = {caseId: "", goal: "full" as PlotGoal, minHops: "0", maxHops: "10", plot: "", board: "", boardPlot: "", boardGoal: "full" as PlotGoal, boardName: "", boardUrl: "", recoveryUrl: ""};
let pegoutDraft = {caseId: "", custom: false, txid: "", minHops: "0", maxHops: "10", selected: "", board: "", approved: ""};

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
const isBusy = (): boolean => !!state.job || submitting || changeOutputsPending() || inputImportPending();
const disabled = (condition: boolean): string => (condition ? " disabled" : "");
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
  const element = document.createElement("div");
  element.className = `toast${error ? " error" : ""}`;
  element.textContent = message;
  document.querySelector("#notifications")!.append(element);
  setTimeout(() => element.remove(), error ? 9000 : 5500);
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
  return `<label class="field"><span>${label}</span><input name="${key}" type="number" min="${key === "max_seconds" ? "0.01" : ["hops", "max_new_items"].includes(key) ? "0" : "1"}"${key === "layout_attempts" ? ' max="1000"' : ""} step="${key === "max_seconds" ? "any" : "1"}" required value="${esc(settings[key])}"/><small>${hint}</small></label>`;
}

function budgetFields(settings: Settings): string {
  return `<div class="budget-grid">${([
    ["hops", "Additional hops", "Default for each run; 0 retries the current frontier."],
    ["max_transactions", "Transactions", "Maximum new transactions per run."],
    ["max_outpoints", "Output lookups", "Maximum per run."],
    ["max_requests", "API attempts", "Includes retries."],
    ["max_seconds", "Time limit (seconds)", "Trace request budget."],
  ] as [keyof Settings, string, string][]).map(([key, label, hint]) => numericField(settings, key, label, hint)).join("")}</div>`;
}

function graphFields(settings: Settings, suggest = false): string {
  return `${numericField(settings, "layout_attempts", "Layout attempts", "More attempts compare more arrangements and take longer.")}
    <label class="field"><span>Connector appearance</span><select name="connector_style">${connectorOptions(settings.connector_style)}</select></label>
    ${([ ["include_fees", "Include transaction fee flows", "Show fees above the graph."],
      ["color_attribution_arrows", "Color arrows by attribution", "Use each named address's assigned color for its arrows."],
      ["group_context_inputs", "Group isolated context inputs", "Combine isolated inputs used by one transaction. Changing this grouping replaces generated context objects; preserve any Miro comments on them first."]
    ] as [keyof Settings, string, string][]).map(([key, label, hint]) => `<input type="hidden" name="${key}_present" value="1"/><label class="check-line"><input name="${key}" type="checkbox"${settings[key] ? " checked" : ""}/><span><strong>${label}</strong><small>${hint}</small></span></label>`).join("")}
    ${centerNameFields(settings, suggest)}${hubAddressFields(settings)}`;
}

function traceSummary(settings: Settings): string {
  return `<p class="settings-summary">Up to <strong>${esc(settings.max_transactions)}</strong> new transactions · <strong>${esc(settings.max_outpoints)}</strong> output lookups · <strong>${esc(settings.max_requests)}</strong> API attempts · <strong>${esc(settings.max_seconds)}s</strong></p>`;
}

function hubAddressFields(settings: Settings): string {
  return `<div class="settings-divider"></div><label class="field"><span>Separate branch hubs</span><textarea name="hub_addresses" class="mono" rows="4" spellcheck="false" placeholder="One full Liquid address per line">${esc(settings.hub_addresses.join("\n"))}</textarea><small>Use high-activity or shared addresses as new tree roots for the layout. Spending transactions line up vertically when other inputs allow; their outputs branch to the right. Each address keeps one identity and all connections. Tracing stays the same.</small></label>`;
}

function centerNameFields(settings: Settings, suggest = false): string {
  return `<div class="settings-divider"></div><label class="field"><span>Center named group</span><input name="center_name" maxlength="120" value="${esc(settings.center_name)}" placeholder="Attribution name, or blank to disable" autocomplete="off"${suggest ? ' list="center-name-options"' : ""}/>${suggest ? '<datalist id="center-name-options"></datalist>' : ""}<small>Align this group's addresses and connecting transactions near the center, with other activity branching around them. Enter an attribution name; matching ignores capitalization. Leave blank to disable. Layout only: tracing and all connections stay the same. Use Sync and reorganize to apply this to an existing board.</small></label>`;
}

let centerNameTimer: ReturnType<typeof setTimeout> | undefined;
async function suggestCenterNames(): Promise<void> {
  const detail = state.activeCase;
  const input = document.querySelector<HTMLInputElement>('#settings-form input[name="center_name"]');
  if (!detail || state.page !== "case-settings" || !input) return;
  const query = input.value.trim(), generation = pageGeneration;
  const result = await api<{rows?: {name: string; enabled_addresses: number}[]}>(`/api/cases/${encodeURIComponent(detail.id)}/name-colors`,
    {query, offset: 0, limit: 100});
  if (generation !== pageGeneration || state.activeCase?.id !== detail.id || input.value.trim() !== query) return;
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
  const flag = (key: "include_fees" | "color_attribution_arrows" | "group_context_inputs"): boolean =>
    data.has(`${key}_present`) || data.has(key) ? data.has(key) : previous[key];
  return {
    hops: number("hops"), max_transactions: number("max_transactions"), max_outpoints: number("max_outpoints"),
    max_requests: number("max_requests"), max_seconds: number("max_seconds"), max_new_items: number("max_new_items"),
    layout_attempts: number("layout_attempts"), include_fees: flag("include_fees"),
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
          `<button class="recent-case" data-case="${esc(item.id)}"><span class="case-dot"></span><span>${esc(item.name)}</span></button>`,
      )
      .join("") ||
    '<p class="small" style="padding:0 13px;color:#7293a1">Your saved cases will appear here.</p>'
  }</div><div class="sidebar-bottom"><div class="local-status"><span class="status-dot"></span><div><strong>Running on your computer</strong><p>Local files · Same tracing engine</p></div></div><div class="sidebar-foot">LIQUID NETWORK · UTXO TRACING<br/><span style="display:block;margin-top:7px;letter-spacing:0">Arrows between buttons · Enter to select</span></div></div></aside>`;
}

function jobProgress(): string {
  const progress = state.job?.progress;
  if (!progress) return '<progress class="job-progress-bar" aria-label="Calculation in progress"></progress>';
  const total = Number.isFinite(progress.total) ? Math.max(0, progress.total) : 0;
  const completed = Number.isFinite(progress.completed)
    ? Math.max(0, Math.min(progress.completed, total))
    : 0;
  const waiting = Number.isFinite(progress.retry_after) && progress.retry_after! > 0;
  const measured = total > 0 && progress.phase !== "optimizing";
  return `<div class="job-progress-meta"><span>Current stage · ${esc(human(progress.phase))}</span>${measured ? `<span>${esc(completed)} / ${esc(total)}</span>` : ""}</div><progress class="job-progress-bar"${measured ? ` max="${total}" value="${completed}"` : ""} aria-label="${esc(human(progress.phase))}"></progress>${waiting ? `<p class="job-retry">Waiting ${esc(progress.retry_after)} seconds before retrying Miro.</p>` : ""}`;
}

function jobBanner(): string {
  if (!state.job) return "";
  return `<div class="job-banner" role="status" aria-live="polite"><span class="spinner" aria-hidden="true"></span><div class="job-details"><strong id="job-message">${esc(state.job.cancelling ? state.job.message : state.job.progress?.message || state.job.message || "Working on your request…")}</strong><div id="job-progress">${jobProgress()}</div><p>${state.job.live ? "Keep the launching terminal open. If Proton Pass requests login or unlocking, complete it there." : "Processing local evidence. You can browse saved investigations while this completes."}</p>${state.job.cancellable || state.job.cancelling ? `<button class="btn small" id="cancel-job" data-action="cancel-job"${disabled(state.job.cancelling)}>${state.job.cancelling ? "Canceling…" : "Cancel calculation"}</button>` : ""}</div><span class="job-time" id="job-elapsed">0s elapsed</span></div>`;
}

function updateJobProgress(): void {
  const message = document.querySelector("#job-message");
  if (message && state.job)
    message.textContent = state.job.cancelling ? state.job.message : state.job.progress?.message || state.job.message;
  const progress = document.querySelector("#job-progress");
  if (progress) progress.innerHTML = jobProgress();
  const cancel = document.querySelector<HTMLButtonElement>("#cancel-job");
  if (cancel && state.job) {
    cancel.disabled = !state.job.cancellable || state.job.cancelling;
    cancel.textContent = state.job.cancelling ? "Canceling…" : "Cancel calculation";
  }
}

function render(focus = false): void {
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
  app.innerHTML = `<div class="layout">${sidebar()}<div class="main-shell"><header class="topbar"><div class="breadcrumb">${icon("folder")}<span>Workspace</span>${icon("chevron")}<strong>${esc(names[state.page])}</strong></div><div class="topbar-right"><span class="local-pill">${icon("lock")} LOCAL SESSION</span><span class="avatar" aria-label="Investigation workspace">LT</span></div></header><main id="main" class="content" tabindex="-1">${jobBanner()}${state.error ? `<div class="alert error" role="alert">${icon("info")}<div><strong>Unable to complete the action</strong><p>${esc(state.error)}</p></div><button class="dismiss" data-action="dismiss-error" aria-label="Dismiss error">${icon("close")}</button></div>` : ""}${state.page === "dashboard" ? dashboard() : state.page === "new" ? newCase() : state.page === "case" ? workspace() : state.page === "addresses" ? addressReviewPage() : settingsPage()}</main></div></div>`;
  renderedSettingsKey = ["case-settings", "settings"].includes(state.page) ? settingsKey() : "";
  openImports.forEach(id => {
    const disclosure = document.querySelector<HTMLDetailsElement>(`#${id}`);
    if (disclosure) disclosure.open = true;
  });
  updateJobClock();
  if (focus)
    document
      .querySelector<HTMLHeadingElement>("#page-title")
      ?.focus({ preventScroll: true });
}

function dashboard(): string {
  const traced = state.cases.filter((item) => !!item.latest_run).length;
  const linked = state.cases.filter((item) => !!item.miro_board).length;
  return `<div class="page-heading"><div><div class="eyebrow">Your local workspace</div><h1 id="page-title" tabindex="-1">Investigations</h1><p>Trace selected outputs through bounded, documented runs.</p></div>${button("New investigation", "new", "plus", "primary")}</div><div class="stats-grid"><div class="stat"><div><div class="stat-label">Investigations</div><div class="stat-value">${state.cases.length.toString().padStart(2, "0")}</div><div class="stat-note">Saved on this computer</div></div><span class="stat-icon">${icon("folder")}</span></div><div class="stat"><div><div class="stat-label">Investigations with runs</div><div class="stat-value">${traced.toString().padStart(2, "0")}</div><div class="stat-note">Bounded, recorded traces</div></div><span class="stat-icon">${icon("layers")}</span></div><div class="stat"><div><div class="stat-label">Linked Miro boards</div><div class="stat-value">${linked.toString().padStart(2, "0")}</div><div class="stat-note">Editable graph workspaces</div></div><span class="stat-icon">${icon("board")}</span></div></div><section class="panel"><div class="panel-head"><div><h2>Saved investigations</h2><p>Pick up where you left off, with every run kept intact.</p></div>${button("Refresh", "refresh", "refresh", "ghost small")}</div>${state.cases.length ? `<div class="table-wrap"><table><thead><tr><th>Investigation</th><th>Source</th><th>Latest run</th><th>Miro board</th><th><span class="sr-only">Open</span></th></tr></thead><tbody>${state.cases.map((item) => `<tr><td><div class="case-cell"><span class="case-icon">${icon("folder")}</span><div><button class="case-title" data-case="${esc(item.id)}">${esc(item.name)}</button><span class="small muted">${esc(formatDate(item.created_at))}</span></div></div></td><td><span class="badge ${item.fixture ? "purple" : ""}">${item.fixture ? "Synthetic data" : "Live Liquid"}</span></td><td>${item.latest_run ? `<span class="mono">${esc(short(item.latest_run, 8))}</span><div class="small muted">${esc(human(item.status || "saved"))}</div>` : '<span class="small muted">Ready for first run</span>'}</td><td><span class="badge ${item.miro_board ? "" : "gray"}">${item.miro_board ? "Linked" : "Not linked"}</span></td><td>${button("Open", "open-case", "arrow", "ghost small", false, `data-id="${esc(item.id)}" aria-label="Open ${esc(item.name)}"`)}</td></tr>`).join("")}</tbody></table></div><div class="table-footer"><span>${state.cases.length} investigation${state.cases.length === 1 ? "" : "s"}</span><span>Saved runs are shared with the terminal interface</span></div>` : `<div class="empty-state"><div class="empty-icon">${icon("folder")}</div><h2>Start with a transaction</h2><p>Create an investigation, choose the outputs to follow, and run a trace with a clear stopping point.</p>${button("Create your first investigation", "new", "plus", "primary")}</div>`}</section><div class="info-grid"><div class="info-card">${icon("graph")}<div><h3>A clear path from evidence to graph</h3><p>Select starting UTXOs, run a bounded trace, then explore locally with Mermaid or add the result to your Miro board.</p></div></div><div class="info-card secondary">${icon("shield")}<div><h3>Credentials stay behind the scenes</h3><p>Live actions use SecretSpec and Proton Pass through your terminal. No API keys are entered in this interface.</p></div></div></div>`;
}

function newCase(): string {
  const draft = state.draft;
  return `<div class="page-heading"><div><div class="eyebrow">Build a starting point</div><h1 id="page-title" tabindex="-1">New investigation</h1><p>Choose the transactions and exact outputs you want to follow.</p></div>${button("Back to investigations", "dashboard", "", "ghost")}</div><form id="new-case-form"><div class="form-grid"><div class="form-stack"><section class="panel"><div class="panel-head"><h2><span class="section-number">1</span> Investigation details</h2></div><div class="panel-body"><label class="field"><span>Investigation name</span><input name="name" maxlength="120" placeholder="e.g. Service withdrawal review" value="${esc(draft.name)}" required autocomplete="off"/></label><label class="field"><span>Miro board URL or ID <span class="muted">(optional)</span></span><input name="board" placeholder="You can link or create a board later" value="${esc(draft.board)}" autocomplete="off"/></label></div></section><section class="panel"><div class="panel-head"><div><h2><span class="section-number">2</span> Starting outputs</h2><p>Multiple transactions can share one investigation.</p></div></div><div class="panel-body"><label class="field"><span>Transaction hashes</span><textarea name="txids" class="mono" rows="3" spellcheck="false" placeholder="Paste transaction hashes separated by commas">${esc(draft.txids)}</textarea><small>Paste up to 100 transaction hashes, separated by commas, spaces, or newlines.</small></label><div class="heading-actions">${button("Load outputs", "lookup", "search", "", isBusy())}</div><p class="small muted" style="margin-top:12px">This lookup uses your Blockstream credits. Watch the terminal for Proton Pass prompts.</p><div id="lookup-outputs">${draft.reports.length ? '<p class="small muted" style="margin-top:17px">Amounts are base units; ?? means unavailable.</p>' : ""}${draft.reports
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
    )}${draft.reports.length ? `<p class="selection-count" id="selection-count">${draft.selected.size} starting output${draft.selected.size === 1 ? "" : "s"} selected</p>` : ""}</div><details class="direct-seeds"${draft.seeds ? " open" : ""}><summary>Enter exact output references directly</summary><label class="field"><span>Starting outputs</span><textarea name="seeds" class="mono" rows="2" spellcheck="false" placeholder="TRANSACTION_HASH:0, TRANSACTION_HASH:1">${esc(draft.seeds)}</textarea><small>Optional. These numeric outpoints are combined with checked outputs above.</small></label></details></div></section><section class="panel"><div class="panel-head"><h2>Starting preferences</h2></div><div class="panel-body"><p>Uses workspace defaults: <strong>${draft.settings.hops} additional hops</strong> per run.</p>${traceSummary(draft.settings)}<p class="small muted">After creating the investigation, use Investigation settings to adjust tracing, layout, colors, and Miro.</p></div></section><div class="form-actions"><p>Creates the investigation locally. Start a trace when you are ready.</p><button class="btn primary" type="submit"${disabled(isBusy())}>${icon("plus")}Create investigation</button></div></div><aside class="form-stack"><div class="side-note"><strong>Follow specific outputs</strong>Each selected UTXO becomes a starting point. Shared descendants appear once in the cumulative graph.<ul><li>Choose relevant outputs after lookup.</li><li>Fees and unspendable outputs cannot be selected.</li><li>Hidden amounts and assets appear as ??.</li></ul></div><div class="side-note"><strong>One case, several runs</strong>Each run has its own budget. Continue unfinished branches in a later run, including after closing this interface.</div></aside></div></form>`;
}

function boardUrl(board: string): string {
  return `https://miro.com/app/board/${encodeURIComponent(board)}/`;
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
    runId: resume ? progress.run_id : currentRun()!.id};
  const settings = {...defaults, ...detail.run_defaults};
  const name = resume && progress.name ? progress.name : `${detail.fixture ? "SYNTHETIC DATA · " : ""}${detail.name}`.slice(0, 50) + " · Rebuilt";
  dialog.innerHTML = `<form id="action-form"><header class="dialog-head"><div><h2 id="dialog-title">${resume ? "Resume board rebuild" : "Rebuild on a new private board"}</h2><p>${resume ? "Continue the saved rebuild. An acknowledged replacement board is reused." : "Recreate this saved graph using current labels, grouping and layout settings, then link the new board to this investigation."}</p></div></header><div class="dialog-body"><div class="dialog-board"><span>Previous board preserved</span><a class="board-link" href="${esc(boardUrl(dialogRebuild.sourceBoard))}" target="_blank" rel="noopener noreferrer">Open previous board ${icon("external")}</a><span>Selected snapshot</span><strong class="mono">${esc(dialogRebuild.runId)}</strong></div><p>The previous board and its comments and manual edits stay there. They are not copied to the rebuilt graph. Create frames separately when the graph is finished.</p><label class="field"><span>New board name</span><input name="board_name" required maxlength="60" value="${esc(name)}"${resume ? " readonly" : ""}/></label><label class="field"><span>New item budget for this rebuild</span><input name="max_new_items" type="number" min="0" max="9007199254740991" step="1" required value="${settings.max_new_items}"/><small>Include all graph objects and connections. Large graphs may need more than the usual sync budget. The layout and full budget are checked before a board is created. This does not change investigation defaults.</small></label><p>Credentials are retrieved through the launching terminal. Complete any Proton Pass prompt there.</p></div><footer class="dialog-footer"><button type="button" class="btn" data-action="close-dialog">Cancel</button><button type="submit" class="btn primary">${resume ? "Resume board rebuild" : "Create board and rebuild"}</button></footer></form>`;
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
  if (!item || !safeLocalUrl(item.url)) return "";
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
  return `<section class="panel" id="connections-panel"><div class="panel-head"><div><h2>Starter connections</h2><p>Only directed paths that reach another starting transaction.</p></div></div>
  <div class="panel-body"><p>Uses the selected saved run, not a new blockchain search. First trace to the required depth. A time, request or transaction limit may leave connections undiscovered.</p>
  <label class="field"><span>Maximum transaction hops per connecting path</span><input id="connection-hops" type="number" min="0" max="2147483647" step="1" value="${artifact?.max_hops ?? 10}"/></label>
  ${button("Plot starter connections", "connections", "graph", "", !saved || isBusy())}
  ${artifact ? `<p>${count === 0 ? "No connection found in the saved searched data. Nothing is plotted." : `${count} ordered starter pair(s) connected within ${artifact.max_hops} hops.`}</p>` : ""}
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
    pegoutDraft = {caseId: detail.id, custom: false, txid: "", minHops: "0",
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
  <p class="small muted">${detail.fixture ? "Uses saved synthetic data." : "Uses your Blockstream API credits. Credentials are retrieved through the launching terminal."} Each search or continuation allows ${settings.max_transactions} new transactions, ${settings.max_outpoints} output lookups, ${settings.max_requests} API attempts and ${settings.max_seconds} seconds. Change these in investigation settings. Saved stop rules and attribution hop limits apply.</p>
  ${button("Trace and plot peg-outs", "pegouts-start", "graph", "primary", busy || (!pegoutDraft.custom && !seeds.length))}
  ${search ? `<hr/><label class="field"><span>Saved peg-out search</span><select id="pegouts-history"${disabled(busy)}>${(detail.pegout_searches || []).map(item => `<option value="${esc(item.id)}"${item.id === search.id ? " selected" : ""}>${esc(pegoutSearchScope(item))} · hops ${item.min_hops}–${item.max_hops} · ${esc(human(item.status))} · ${esc(short(item.id, 8))}</option>`).join("")}</select></label>
  <p>Saved scope: ${esc(pegoutSearchScope(search))}.</p>${search.txid ? `<p class="mono">${esc(search.txid)}</p>` : ""}<p>Hop range ${search.min_hops}–${search.max_hops}. ${esc(human(search.status))}${search.stop_reason ? `: ${esc(human(search.stop_reason))}` : ""}.</p>
  ${partial ? `<p class="artifact-note">${pending ? "This search was interrupted." : "This search is incomplete."} Resume to continue from its saved progress. Additional peg-outs may remain undiscovered.</p>` : '<p class="small muted">Search reached its current boundary. Unspent outputs, confirmation requirements and stop rules can limit what is discoverable.</p>'}
  <p>${count === undefined ? "Generate a preview to see the matches found so far." : count === 0 ? "No matching peg-out found in the searched data." : `${count} matching peg-out request${count === 1 ? "" : "s"} found.`}</p>
  ${button(pending ? "Recover and resume search" : "Resume search", "pegouts-resume", "play", "", busy)}
  ${button("Refresh peg-out preview", "pegouts-preview", "refresh", "", busy || pending)}
  <div class="artifact-actions">${downloadLink(artifact?.downloads.find(item => item.name === "graph.svg"), "SVG")}${downloadLink(artifact?.downloads.find(item => item.name === "pegouts.json"), "Peg-out report")}${downloadLink(artifact?.downloads.find(item => item.name === "transactions.csv"), "Transaction CSV")}${preview ? `<a class="btn small" href="${esc(preview)}" target="_blank" rel="noopener noreferrer">Open full view</a>` : ""}${detailPagesLink(artifact?.downloads)}</div>
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
  return `<section class="panel"><div class="panel-head"><div><h2>CSV downloads</h2><p>${tables.length ? "One row per displayed transaction input or output." : "Export transaction I/O, not graph objects."}</p></div>${tables.length ? button("Refresh CSV export", "csv", "refresh", "small", !saved || isBusy()) : ""}</div>${tables.length ? `<div class="downloads">${tables.map((item) => downloadLink(item, `Download ${item.name}`, "download-link")).join("")}</div><div class="export-footer"><span class="small muted">${includeFees ? "Fee flows included" : "Fee flows hidden"} · Indexes start at 0 · Amounts are exact base units · Unknown values stay blank.</span>${provenance.length ? `<details class="provenance-downloads"><summary>Export provenance</summary><div class="artifact-actions">${provenance.map((item) => downloadLink(item, item.name, "ghost")).join("")}</div></details>` : ""}</div>` : `<div class="panel-body"><p class="artifact-note">${mismatch ? "The saved export uses a different fee setting. Create a new export to match the current settings." : "Create transactions.csv for this full-trace snapshot. Use the Starter connections panel for its filtered transaction CSV."}</p>${button("Create CSV export", "csv", "table", "", !saved || isBusy())}</div>`}</section>`;
}

const plotGoals: {id: PlotGoal; name: string; description: string}[] = [
  {id: "full", name: "Full trace", description: "Every collected transaction and its traced branches."},
  {id: "connections", name: "Starter connections", description: "Directed paths connecting your starting transactions."},
  {id: "pegouts", name: "Paths to peg-outs", description: "Paths from selected seed UTXOs to peg-out requests."},
];

function goalName(goal: string): string {
  return plotGoals.find(item => item.id === goal)?.name || human(goal);
}

function currentWorkflow(detail: Case): typeof workflowDraft {
  if (workflowDraft.caseId !== detail.id) workflowDraft = {caseId: detail.id, goal: "full", minHops: "0", maxHops: "10", plot: "", board: "", boardPlot: "", boardGoal: "full", boardName: "", boardUrl: "", recoveryUrl: ""};
  return workflowDraft;
}

function currentPlot(detail: Case): Plot | undefined {
  const draft = currentWorkflow(detail);
  return detail.plots?.find(item => item.preview_id === draft.plot) || detail.plots?.[0];
}

function currentBoard(detail: Case): InvestigationBoard | undefined {
  const draft = currentWorkflow(detail);
  if (draft.board) return detail.boards?.find(item => item.id === draft.board);
  if (draft.boardPlot) return detail.boards?.find(board =>
    board.can_sync && matchingBoardPlots(detail, board).some(plot => plot.preview_id === draft.boardPlot));
  return detail.boards?.[0];
}

function collectDataPanel(detail: Case, saved: boolean, settings: Settings): string {
  return `<section class="panel" id="collection-panel"><div class="panel-head"><div><h2>Collect transaction data</h2><p>Collect once, then reuse the saved evidence for every plotting goal.</p></div></div><div class="panel-body"><p>Follow your ${detail.seed_count ?? detail.seeds?.length ?? 0} selected starting outputs and save a new run. Continuing adds hops from the latest run. Budgets, tracing stops and address hop limits apply.</p><p class="artifact-note">Collection does not generate plots or update Miro. A paused run can be continued here. Plot views uses only the data already saved.</p>${traceSummary(settings)}<div class="task-actions">${button(saved ? "Continue collecting data" : "Collect transaction data", "trace-dialog", "play", "primary", isBusy())}${button("Collection settings", "case-settings", "settings", "", isBusy())}${button("Choose a plotting goal", "view-plots", "graph", "", !saved)}</div><hr/><h3>Address activity</h3><p class="small muted">Missing transaction counts are fetched during collection. Retry any unresolved counts here.</p>${button("Fetch address transaction counts", "address-counts", "refresh", "small", !saved || isBusy())}</div></section>`;
}

function plotViewsPanel(detail: Case, saved: boolean): string {
  const draft = currentWorkflow(detail), plot = currentPlot(detail), artifact = plot?.artifact;
  const preview = safeLocalUrl(artifact?.preview_url);
  return `<section class="panel" id="plot-views-panel"><div class="panel-head"><div><h2>Plot views</h2><p>Choose a goal and build a view from the selected saved run.</p></div><span class="badge gray">Saved data only</span></div><div class="panel-body"><div class="plot-goals" role="group" aria-label="Plotting goal">${plotGoals.map(goal => `<button class="plot-goal${draft.goal === goal.id ? " selected" : ""}" data-action="plot-goal" data-goal="${goal.id}" aria-pressed="${draft.goal === goal.id}"${disabled(isBusy())}><strong>${goal.name}</strong><span>${goal.description}</span></button>`).join("")}</div>${draft.goal !== "full" ? `<div class="field-row">${draft.goal === "pegouts" ? `<label class="field"><span>Minimum hops</span><input id="workflow-min-hops" type="number" min="0" max="2147483647" step="1" value="${esc(draft.minHops)}"${disabled(isBusy())}/></label>` : ""}<label class="field"><span>Maximum hops</span><input id="workflow-max-hops" type="number" min="0" max="2147483647" step="1" value="${esc(draft.maxHops)}"${disabled(isBusy())}/></label></div>` : ""}<p class="artifact-note">Plotting makes no blockchain requests. A hop filter cannot reveal data beyond your collection coverage. If a path is missing, collect more data first and generate another plot.</p>${draft.goal === "pegouts" ? '<p class="small muted">Starting transactions are hop 0. Only selected starting UTXOs are followed. A peg-out request does not confirm the separate Bitcoin payout.</p>' : ""}<div class="task-actions">${button(`Generate ${goalName(draft.goal).toLowerCase()} plot`, "workflow-plot", "graph", "primary", !saved || isBusy())}${button("Collect more data", "view-collect", "play")}${button("Graph settings", "case-settings", "settings", "", isBusy())}</div>${!saved ? '<p class="artifact-note">Collect transaction data before generating a plot.</p>' : ""}</div></section>
  <section class="panel" id="saved-plots-panel"><div class="panel-head"><div><h2>Saved plots</h2><p>Each plot records its collection run and tracing goal.</p></div><span class="badge gray">${detail.plots?.length || 0} plots</span></div><div class="panel-body">${detail.plots_notice ? `<p class="artifact-note" role="status">${esc(detail.plots_notice)}</p>` : ""}${plot ? `<label class="field"><span>Plot</span><select id="workflow-plot-picker">${(detail.plots || []).map(item => `<option value="${esc(item.preview_id)}"${item.preview_id === plot.preview_id ? " selected" : ""}>${esc(goalName(item.goal))} · ${esc(short(item.run_id, 8))} · ${esc(formatDate(item.created_at))}</option>`).join("")}</select></label><p>${esc(goalName(plot.goal))} · Run ${esc(short(plot.run_id, 8))}${plot.goal !== "full" ? ` · Hops ${plot.min_hops}–${plot.max_hops}` : ""} · ${plot.node_count} objects · ${plot.edge_count} connections.</p>${plot.coverage_notice && !plot.notice?.includes(plot.coverage_notice) ? `<p class="artifact-note">${esc(plot.coverage_notice)}</p>` : ""}${plot.notice ? `<p class="artifact-note">${esc(plot.notice)}</p>` : ""}${plot.empty ? '<p class="artifact-note">No matching paths were found in this saved data. Collect more data or adjust the hop range.</p>' : ""}${!plot.reviewable ? `<p class="artifact-note">${esc(plot.reason || "No matching paths in this saved data.")}</p>` : ""}<p class="small muted">Choose an output for this saved plot. The ELK SVG uses the same layout shown below.</p><div class="task-actions">${button("Sync with Miro", "plot-boards", "board", "primary", !plot.reviewable || Boolean(plot.empty) || plot.node_count === 0 || isBusy())}${downloadLink(artifact?.downloads.find(item => item.name === "graph.svg"), "Download ELK SVG")}${detailPagesLink(artifact?.downloads)}${preview ? `<a class="btn small" href="${esc(preview)}" target="_blank" rel="noopener noreferrer">Open full view</a>` : ""}</div>` : '<p class="artifact-note">Your generated plots will appear here. All three goals use the same saved collection data.</p>'}</div>${preview ? `<iframe loading="lazy" class="graph-preview" src="${esc(preview)}#chart" title="${esc(goalName(plot!.goal))} saved plot" sandbox="allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer"></iframe>` : ""}</section>`;
}

function plotDownloadsPanel(detail: Case): string {
  const plot = currentPlot(detail);
  return `<section class="panel"><div class="panel-head"><div><h2>Plot downloads</h2><p>Download the chart and evidence for any saved plotting goal.</p></div></div><div class="panel-body">${detail.plots_notice ? `<p class="artifact-note">${esc(detail.plots_notice)}</p>` : ""}${plot ? `<label class="field"><span>Saved plot</span><select id="workflow-plot-picker">${(detail.plots || []).map(item => `<option value="${esc(item.preview_id)}"${item.preview_id === plot.preview_id ? " selected" : ""}>${esc(goalName(item.goal))} · Run ${esc(short(item.run_id, 8))} · ${esc(formatDate(item.created_at))}</option>`).join("")}</select></label><div class="artifact-actions">${(plot.artifact?.downloads || []).map(item => downloadLink(item, item.name)).join("")}</div>${!plot.reviewable ? `<p class="artifact-note">${esc(plot.reason || "Generate this plot again to refresh its files.")}</p>` : ""}` : `<p class="artifact-note">Generate a saved plot to download its chart and transaction evidence.</p>${button("Choose a plotting goal", "view-plots", "graph")}`}</div></section>`;
}

function fullBoardMaintenance(detail: Case, saved: boolean, artifacts: RunArtifacts, settings: Settings): string {
  return `<section class="panel"><div class="panel-head"><div><h2>Full trace board tools</h2><p>Recovery and layout tools for the original investigation board.</p></div></div><div class="panel-body">${miroRecoveryNotice(detail)}<div class="task-actions">${button("Preview changes", "miro-preview", "search", "", !saved || isBusy())}${button("Create / update Miro frames", "miro-frames-dialog", "layers", "", !saved || Boolean(detail.miro_recovery?.pending_count) || isBusy())}${detail.miro_recovery?.can_recover_frame ? button("Recover interrupted frame", "miro-frame-review", "refresh", "", isBusy()) : ""}${detail.miro_recovery?.can_confirm_empty ? button("Recover empty-board sync", "miro-recover-dialog", "refresh", "", isBusy()) : ""}</div><p class="small muted">When the graph is finished, create its frames separately. Run this again after further syncing or rearranging to update the frames.</p><details class="tool-details"><summary>Board maintenance</summary><div class="panel-body">${button("Merge duplicate addresses", "address-merge-dialog", "graph", "", isBusy() || !saved)}${rebuildAction(detail, saved)}</div></details></div></section><details class="tool-details"><summary>Full trace layout and compaction</summary>${elkGraph(artifacts.elk, saved, settings)}${compactGraph(artifacts.compact, saved, detail)}</details>`;
}

function matchingBoardPlots(detail: Case, board: InvestigationBoard | undefined): Plot[] {
  return (detail.plots || []).filter(plot => plot.goal === board?.goal && plot.reviewable && !plot.empty && plot.node_count > 0 &&
    (!board?.pending_count || plot.preview_id === board.preview_id));
}

function boardsPanel(detail: Case, saved: boolean, artifacts: RunArtifacts, settings: Settings): string {
  const draft = currentWorkflow(detail), selected = currentBoard(detail), boards = detail.boards || [];
  const plots = matchingBoardPlots(detail, selected);
  const chosen = draft.boardPlot ? plots.find(plot => plot.preview_id === draft.boardPlot) : plots[0];
  const requestedPlot = detail.plots?.find(plot => plot.preview_id === draft.boardPlot);
  const legacyFull = selected?.board_id === detail.miro_board && selected?.goal === "full";
  const locked = isBusy() || !selected?.can_sync || !chosen;
  return `<section class="panel" id="miro-boards-panel"><div class="panel-head"><div><h2>Miro boards</h2><p>View and manage every board linked to this investigation.</p></div><span class="badge gray">${boards.length} boards</span></div><div class="panel-body">${requestedPlot ? `<p class="artifact-note">Selected output: ${esc(goalName(requestedPlot.goal))} · Run ${esc(short(requestedPlot.run_id, 8))}${requestedPlot.goal !== "full" ? ` · Hops ${requestedPlot.min_hops}–${requestedPlot.max_hops}` : ""}. Sync uses this saved plot.</p>${!selected ? '<p class="artifact-note">Create or link a board for this plotting goal below, or select an existing board.</p>' : ""}` : ""}${detail.boards_notice ? `<p class="artifact-note" role="status">${esc(detail.boards_notice)}</p>` : ""}${boards.length ? `<div class="board-list" role="group" aria-label="Investigation boards">${boards.map(board => `<button class="board-choice${board.id === selected?.id ? " selected" : ""}" data-action="workflow-board-select" data-record="${esc(board.id)}" aria-pressed="${board.id === selected?.id}"><strong>${esc(board.name || goalName(board.goal))}</strong><span>${esc(goalName(board.goal))} · ${esc(human(board.status))}${board.legacy_snapshot ? " · Archived snapshot" : ""}</span></button>`).join("")}</div>` : '<p class="artifact-note">No boards yet. Create a private board or link an existing one below.</p>'}${selected ? `<div class="board-management"><h3>${esc(selected.name || goalName(selected.goal))}</h3>${selected.board_id ? `<a class="board-link" href="${esc(boardUrl(selected.board_id))}" target="_blank" rel="noopener noreferrer">Open Miro board ${icon("external")}</a>` : ""}${selected.notice ? `<p class="artifact-note">${esc(selected.notice)}</p>` : ""}${!selected.board_id ? `<label class="field"><span>Created board URL or ID</span><input id="workflow-recovery-url" maxlength="512" value="${esc(draft.recoveryUrl)}"${disabled(isBusy())}/><small>If creation was interrupted, locate the board in Miro and link it to this saved entry.</small></label>${button("Link created board to this entry", "workflow-board-recover", "board", "", isBusy())}` : ""}${selected.can_sync ? `<label class="field"><span>Plot to sync · ${esc(goalName(selected.goal))}</span><select id="workflow-board-plot"${disabled(!plots.length || isBusy())}>${!chosen ? '<option value="" selected>Choose a saved plot for this board</option>' : ""}${plots.length ? plots.map(plot => `<option value="${esc(plot.preview_id)}"${plot.preview_id === chosen?.preview_id ? " selected" : ""}>Run ${esc(short(plot.run_id, 8))}${plot.goal !== "full" ? ` · Hops ${plot.min_hops}–${plot.max_hops}` : ""} · ${esc(formatDate(plot.created_at))}</option>`).join("") : '<option value="">No eligible saved plot. Use Plot views to generate one.</option>'}</select></label><div class="task-actions">${button("Sync to Miro", "workflow-board-sync", "refresh", "primary", locked)}${button("Sync and reorganize", "workflow-board-organize", "graph", "", locked)}</div><p class="small muted">Sync keeps existing positions. Sync and reorganize also applies the plot layout. These actions update only this board and do not fetch transaction data.</p>${selected.pending_count ? '<p class="artifact-note">This board has an interrupted operation. Retry its saved plot to resume. An uncertain item creation may require recovery before retrying.</p>' : ""}` : '<p class="artifact-note">This saved board is available to view. Create a managed board below to sync new plots.</p>'}</div>` : ""}</div></section>
  <section class="panel"><div class="panel-head"><div><h2>Add a Miro board</h2><p>Give each board a plotting goal so updates go to the right place.</p></div></div><div class="panel-body"><div class="field-row"><label class="field"><span>Plotting goal</span><select id="workflow-board-goal"${disabled(isBusy())}>${plotGoals.map(goal => `<option value="${goal.id}"${draft.boardGoal === goal.id ? " selected" : ""}>${goal.name}</option>`).join("")}</select></label><label class="field"><span>Board name</span><input id="workflow-board-name" maxlength="60" value="${esc(draft.boardName)}" placeholder="${esc(`${detail.name} · ${goalName(draft.boardGoal)}`.slice(0, 60))}"${disabled(isBusy())}/></label></div><div class="task-actions">${button("Create private Miro board", "workflow-board-create", "plus", "primary", isBusy())}</div><label class="field"><span>Or link an existing board URL or ID</span><input id="workflow-board-url" maxlength="512" value="${esc(draft.boardUrl)}" placeholder="https://miro.com/app/board/…"${disabled(isBusy())}/></label>${button("Link existing board", "workflow-board-link", "board", "", isBusy())}<p class="small muted">Create or link once, then use the same sync controls for every plotting goal. Credentials are retrieved through the launching terminal.</p></div></section>${legacyFull || !selected && !requestedPlot && detail.miro_board ? fullBoardMaintenance(detail, saved, artifacts, settings) : ""}`;
}

function workflowInput(element: HTMLInputElement | HTMLSelectElement): boolean {
  if (!element.id?.startsWith("workflow-") || !state.activeCase || isBusy()) return false;
  const draft = currentWorkflow(state.activeCase);
  const fields: Record<string, "minHops" | "maxHops" | "plot" | "boardPlot" | "boardName" | "boardUrl" | "recoveryUrl"> = {"workflow-min-hops": "minHops", "workflow-max-hops": "maxHops", "workflow-plot-picker": "plot", "workflow-board-plot": "boardPlot", "workflow-board-name": "boardName", "workflow-board-url": "boardUrl", "workflow-recovery-url": "recoveryUrl"};
  if (element.id === "workflow-board-goal" && plotGoals.some(goal => goal.id === element.value)) draft.boardGoal = element.value as PlotGoal;
  else if (fields[element.id]) draft[fields[element.id]] = element.id === "workflow-board-plot" && !element.value ? "unselected" : element.value;
  else return false;
  if (["workflow-plot-picker", "workflow-board-goal", "workflow-board-plot"].includes(element.id)) render();
  return true;
}

async function workflowAction(action: string, element?: HTMLElement): Promise<boolean> {
  if (!["plot-goal", "workflow-plot", "plot-boards", "workflow-board-select", "workflow-board-create", "workflow-board-link", "workflow-board-sync", "workflow-board-organize", "workflow-board-recover"].includes(action)) return false;
  const detail = state.activeCase;
  if (!detail || isBusy()) return true;
  const draft = currentWorkflow(detail);
  if (action === "plot-goal") {
    if (plotGoals.some(goal => goal.id === element?.dataset.goal)) draft.goal = element!.dataset.goal as PlotGoal;
    render(); return true;
  }
  if (action === "workflow-board-select") {draft.board = element?.dataset.record || ""; render(); return true;}
  if (action === "plot-boards") {
    const plot = currentPlot(detail);
    if (!plot?.reviewable || plot.empty || plot.node_count === 0) throw new Error("Select a current saved plot with activity before syncing to Miro.");
    draft.boardPlot = plot.preview_id;
    draft.boardGoal = plot.goal;
    const selectedBoard = detail.boards?.find(board => board.id === draft.board);
    draft.board = (selectedBoard?.goal === plot.goal && selectedBoard.can_sync ? selectedBoard
      : detail.boards?.find(board => board.goal === plot.goal && board.can_sync))?.id || "";
    state.caseView = "boards"; render(); return true;
  }
  let body: Record<string, unknown>;
  if (action === "workflow-plot") {
    if (!currentRun()) throw new Error("Collect transaction data first, then select a saved run to plot.");
    const min = draft.goal === "pegouts" ? draft.minHops.trim() : "0", max = draft.goal === "full" ? "0" : draft.maxHops.trim();
    if ([min, max].some(value => !/^\d+$/.test(value) || Number(value) > 2147483647) || Number(min) > Number(max)) throw new Error("Enter whole-number hop limits from 0 to 2147483647, with minimum no greater than maximum.");
    body = {action: "plot", goal: draft.goal, run_id: currentRun()!.id, min_hops: Number(min), max_hops: Number(max)};
  } else if (action === "workflow-board-recover") {
    const board = currentBoard(detail);
    if (!board || board.board_id || !draft.recoveryUrl.trim()) throw new Error("Enter the URL of the board created during the interrupted operation.");
    body = {action: "board-link", record_id: board.id, goal: board.goal, name: board.name, board: draft.recoveryUrl.trim()};
  } else if (action === "workflow-board-create" || action === "workflow-board-link") {
    body = {action: action === "workflow-board-create" ? "board-create" : "board-link", goal: draft.boardGoal, name: draft.boardName.trim() || `${detail.name} · ${goalName(draft.boardGoal)}`.slice(0, 60)};
    if (action === "workflow-board-link") {if (!draft.boardUrl.trim()) throw new Error("Enter a Miro board URL or ID to link."); body.board = draft.boardUrl.trim();}
  } else {
    const board = currentBoard(detail), plots = matchingBoardPlots(detail, board);
    const plot = draft.boardPlot ? plots.find(item => item.preview_id === draft.boardPlot) : plots[0];
    if (!board?.can_sync || !plot) throw new Error("Select a managed board and a matching saved plot before syncing.");
    body = {action: "board-sync", record_id: board.id, preview_id: plot.preview_id, reorganize: action === "workflow-board-organize"};
  }
  await startJob(`/api/cases/${encodeURIComponent(detail.id)}/actions`, body, String(body.action), body.action !== "plot" && body.action !== "board-link", detail.id);
  return true;
}

function investigationDataPanel(detail: Case): string {
  return `<section class="panel" id="settings-data"><div class="panel-head"><div><h2>Investigation data</h2><p>Manage attributions, tracing stops, and change outputs here.</p></div></div><div class="panel-body"><div class="task-actions">${button("Import CSV files", "input-import-open", "plus", "primary", isBusy())}${button("Address review", "addresses", "search", "", isBusy())}${button("Change outputs", "change-outputs-open", "graph", "", isBusy())}${button("Assign colors", "name-colors-open", "", "", isBusy())}<a class="btn" href="/api/cases/${esc(encodeURIComponent(detail.id))}/input-exports/all" download>${icon("download")}Export input CSVs</a></div><p class="small muted">Import attributions, name colors, and change outputs together. Export input CSVs downloads all saved attributions, name colors, and change outputs across every page. Unsaved edits are excluded. Imports, address assessments and color changes save separately from the settings form.</p></div></section>${inputImportPanel(detail.id, isBusy())}${changeOutputsPanel(detail.id, isBusy())}`;
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

  const views: [CaseView, string][] = [["collect", "Collect data"], ["plots", "Plot views"], ["boards", "Miro boards"], ["history", "History & downloads"]];

  const content = state.caseView === "plots" ? plotViewsPanel(detail, saved)
    : state.caseView === "boards" ? boardsPanel(detail, saved, artifacts, settings)
    : state.caseView === "history" ? `<section class="panel"><div class="panel-head"><div><h2>Run history</h2><p>Every continuation preserves the preceding snapshot.</p></div><span class="badge gray">${(detail.runs || []).length} runs</span></div>${detail.runs?.length ? `<div class="table-wrap"><table class="run-list"><thead><tr><th>Run</th><th>Recorded</th><th>Status</th><th>Transactions</th></tr></thead><tbody>${detail.runs.map((item) => `<tr class="${item.id === run?.id ? "selected" : ""}"><td><button data-run="${esc(item.id)}">${esc(short(item.id, 8))}</button>${item.id === detail.latest_run ? '<div class="muted">Latest</div>' : ""}</td><td><span class="muted">${esc(formatDate(item.created_at))}</span></td><td><span class="badge ${item.status === "error" ? "red" : "gray"}">${esc(human(item.status))}</span></td><td>${esc(item.transaction_count ?? "—")}</td></tr>`).join("")}</tbody></table></div>` : '<div class="panel-body small muted">Your first completed or bounded run will appear here.</div>'}</section>${plotDownloadsPanel(detail)}${csvDownloads(artifacts.csv, saved, settings.include_fees)}${localGraph(artifacts.mermaid, saved, settings)}${detail.pegout_searches?.length ? `<details class="tool-details"><summary>Earlier standalone peg-out searches</summary>${pegoutsGraph(detail)}</details>` : ""}${artifacts.connections ? `<details class="tool-details"><summary>Earlier starter-connection snapshot</summary>${connectionsGraph(artifacts.connections, saved)}</details>` : ""}`
    : collectDataPanel(detail, saved, settings);
  return `<div class="page-heading case-heading"><div><div class="eyebrow">Investigation workspace</div><h1 id="page-title" tabindex="-1">${esc(detail.name)}</h1><div class="workspace-meta"><span class="badge ${detail.fixture ? "purple" : ""}">${detail.fixture ? "Synthetic data" : "Live Liquid"}</span><span class="badge gray">${(detail.runs || []).length} saved runs</span></div></div><div class="heading-actions">${button("Investigation settings", "case-settings", "settings", "", isBusy())}</div></div>
    <nav class="case-navigation" aria-label="Investigation tools">${views.map(([view, label]) => `<button type="button" class="case-nav${state.caseView === view ? " active" : ""}" data-action="view-${view}"${state.caseView === view ? ' aria-current="page"' : ""}>${label}</button>`).join("")}</nav>
    ${last ? resultBanner(last.action, last.result) : ""}
    <section class="panel"><div class="panel-head"><div><h2>${saved ? "Collected data" : "Ready to collect"}</h2><p>${saved ? "Choose the collected data used for plots and downloads." : "Your starting outputs and limits are saved."}</p></div>${saved ? `<label class="run-picker">Snapshot<select class="input" id="run-picker" aria-label="Saved run snapshot">${runOptions}</select></label>` : '<span class="badge gray">No runs yet</span>'}</div>${saved ? `<div class="run-summary"><div><span>Tracked transactions</span><strong>${esc(run?.transaction_count ?? "—")}</strong></div><div><span>Unfinished branches</span><strong>${esc(run?.frontier_count ?? "—")}</strong></div><div><span>Run status</span><strong class="text-value">${esc(human(run?.status || "saved"))}</strong></div></div><div class="run-note">${icon("clock")}<span>${esc(formatDate(run?.created_at))}${run?.max_hops !== undefined ? ` · Collection hop limit: ${run.max_hops}` : ""}${run?.stop_reason ? ` · ${esc(human(run.stop_reason))}` : ""}</span></div>` : `<div class="empty-state" style="padding:31px 24px"><div class="empty-icon">${icon("graph")}</div><h2>${detail.seed_count ?? detail.seeds?.length ?? "Your"} starting output${(detail.seed_count ?? detail.seeds?.length) === 1 ? "" : "s"} selected</h2><p>Collect data first, then choose a plotting goal and its Miro board.</p></div>`}</section>
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
    toast(result.service.enabled ? (result.service.stop_tracing === false ? "Assessment and hop limit saved. Tracing continues within the configured limits." : "Service stop saved. It applies to the first/next run.") : "Assessment disabled. Its history remains saved.");
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
    <label class="field"><span>hop_limit (optional)</span><input name="service_hop_limit" type="number" min="0" step="1" value="${esc(review.hopLimit)}" placeholder="No local cap"${disabled(busy)}/><small>Additional transaction hops after reaching this address. Blank: no local cap. 0: stop. Stop tracing overrides this value.</small></label>
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
  return `${addressCountWarning(result)}<div class="alert">${icon("check")}<div><strong>${titles[action] || "Action completed"}</strong><p>${action === "trace" ? `The saved run ${esc(result.run_id || "")} is ready to review and export.` : action === "miro-preview" ? "This is an offline plan. Live sync checks the board before making changes." : action === "miro-rebuild" ? "The rebuilt board is linked to this investigation. The previous board and its comments remain available. Create frames separately when the graph is finished." : action === "miro-create" ? "The board is linked to this investigation. Sync a saved run to add the graph." : action === "miro-recover" ? "The board was verified empty and the unconfirmed initial batch was cleared locally. Choose Sync to Miro to resume the recovered snapshot with individual shape requests. Your saved trace is ready to use." : action === "miro-frame-recover" ? `The interrupted frame was reconciled locally. The failed snapshot is selected. ${result.resume_action === "miro-frames" ? "Choose Create / update Miro frames to finish framing the saved graph." : "Finish Sync to Miro for this snapshot, then choose Create / update Miro frames."}` : action === "miro-frames" ? "Frames now reflect the synced graph. Run Create / update Miro frames again after further changes to the graph." : action === "address-merge" ? "Choose Sync to Miro to refresh labels, or Sync and reorganize to apply the shared-address layout. When the graph is finished, choose Create / update Miro frames." : "The result is saved with this investigation."}</p>${
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
  settingsDrafts.set(renderedSettingsKey, {settings: readSettings(form, previous), name: String(data.get("name") || ""), board: String(data.get("board") || "")});
}

function settingsPage(): string {
  const isCase = state.page === "case-settings", detail = state.activeCase;
  const draft = settingsDrafts.get(settingsKey());
  const settings = draft?.settings || {...defaults, ...(isCase ? detail?.run_defaults : state.settings)};
  return `<div class="page-heading"><div><div class="eyebrow">${isCase ? esc(detail?.name) : "Workspace"}</div><h1 id="page-title" tabindex="-1">${isCase ? "Investigation settings" : "Workspace defaults"}</h1><p>${isCase ? "Investigation data, tracing rules, graph layout, colors, and Miro preferences." : "Starting preferences for new investigations. Existing investigations keep their own settings."}</p></div>${button(isCase ? "Back to investigation" : "Back to investigations", isCase ? "back-case" : "dashboard", "", "ghost")}</div>
    <nav class="settings-navigation" aria-label="Settings sections">${isCase ? '<a href="#settings-data">Investigation data</a>' : ""}<a href="#settings-trace">Tracing</a><a href="#settings-layout">Graph layout</a><a href="#settings-miro">Miro</a>${isCase ? '<a href="#settings-colors">Colors</a>' : ""}</nav>
    ${isCase && detail ? `<div class="settings-layout">${investigationDataPanel(detail)}</div>` : ""}
    <form id="settings-form" class="settings-layout">
    ${isCase ? `<section class="panel"><div class="panel-body"><label class="field"><span>Investigation name</span><input name="name" maxlength="120" required value="${esc(draft?.name ?? detail?.name)}" autocomplete="off"/></label></div></section>` : ""}
    <section class="panel" id="settings-trace"><div class="panel-head"><div><h2>Tracing</h2><p>Saved defaults for each bounded run.</p></div></div><div class="panel-body">${budgetFields(settings)}</div></section>
    <section class="panel" id="settings-layout"><div class="panel-head"><div><h2>Graph layout</h2><p>Use Sync and reorganize to apply placement changes to Miro.</p></div></div><div class="panel-body">${graphFields(settings, isCase)}</div></section>
    <section class="panel" id="settings-miro"><div class="panel-head"><h2>Miro</h2></div><div class="panel-body">${isCase ? `${button("Manage investigation boards", "view-boards", "board", "", isBusy())}<p class="small muted">Create, link and sync boards in Miro boards.</p>` : ""}${numericField(settings, "max_new_items", "New Miro items", "Maximum new objects and connections per sync.")}<p class="small muted">Live actions use your existing SecretSpec and Proton Pass configuration.</p></div></section>
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
  state.draft.board = String(data.get("board") || "");
  state.draft.settings = readSettings(form, state.draft.settings);
}

async function refreshSession(): Promise<void> {
  const session = await api<{
    csrf: string;
    settings: Settings;
    cases: Case[];
    active_job: string | null;
  }>("/api/session");
  state.csrf = session.csrf;
  state.settings = { ...defaults, ...session.settings };
  state.cases = session.cases;
  if (session.active_job && !state.job) {
    const recovered = await api<Job>(
      `/api/jobs/${encodeURIComponent(session.active_job)}`,
    );
    state.job = {
      id: session.active_job,
      action: recovered.action || "recovered",
      caseId: recovered.case_id,
      started: Number.isFinite(recovered.started_at) ? recovered.started_at! * 1000 : Date.now(),
      message: recovered.message || "A local action is still running…",
      live: recovered.live ?? true,
      progress: recovered.progress,
      cancellable: recovered.cancellable === true,
      cancelling: recovered.status === "cancelling",
    };
    schedulePoll();
  }
}

async function openCase(id: string): Promise<void> {
  resetFrameRecovery();
  dialog.close();
  const generation = ++pageGeneration;
  const detail = await api<Case>(`/api/cases/${encodeURIComponent(id)}`);
  if (generation !== pageGeneration) return;
  saveDraft();
  state.activeCase = detail;
  state.addressReview = { query: "", suspectedOnly: false, data: null, selected: null,
    loading: false, name: "", notes: "", enabled: false, pasted: "",
    confidence: "suspected", source: "Investigator designation", observedAt: "", stopTracing: true, hopLimit: "" };
  resetInputImport(detail.id);
  resetAddressImport(detail.id);
  resetNameColors(detail.id);
  resetChangeOutputs(detail.id);
  state.selectedRun = "latest";
  state.caseView = "collect";
  state.page = "case";
  state.error = "";
  history.replaceState(null, "", `#case/${encodeURIComponent(id)}`);
  render(true);
}

function navigate(page: Page): void {
  saveSettingsDraft(); saveAddressDraft();
  resetFrameRecovery();
  dialog.close();
  saveDraft();
  pageGeneration++;
  state.page = page;
  state.error = "";
  history.replaceState(
    null,
    "",
    page === "dashboard" ? location.pathname : `#${page}`,
  );
  render(true);
}

async function startJob(
  path: string,
  body: unknown,
  action: string,
  live: boolean,
  caseId?: string,
): Promise<void> {
  if (isBusy()) return;
  if (action !== "miro-frame-review") resetFrameRecovery();
  saveDraft();
  submitting = true;
  render();
  try {
    const job = await api<Job>(path, body);
    state.job = {
      id: job.id,
      action,
      caseId,
      started: Number.isFinite(job.started_at) ? job.started_at! * 1000 : Date.now(),
      message: job.message,
      live,
      progress: job.progress,
      cancellable: job.cancellable === true,
      cancelling: job.status === "cancelling",
    };
    state.error = "";
    dialog.close();
    schedulePoll(150);
  } finally {
    submitting = false;
    render();
  }
}

function schedulePoll(delay = 1200): void {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = setTimeout(() => void pollJob(), delay);
}

async function cancelJob(): Promise<void> {
  const active = state.job;
  if (!active || !active.cancellable || active.cancelling) return;
  active.cancelling = true;
  active.message = "Canceling the calculation and stopping its renderer…";
  updateJobProgress();
  try {
    const job = await api<Job>(`/api/jobs/${encodeURIComponent(active.id)}/cancel`, {});
    if (state.job?.id !== active.id) return;
    active.cancelling = job.status === "cancelling";
    active.cancellable = job.cancellable === true;
    active.message = job.message;
  } catch (error) {
    if (state.job?.id !== active.id) return;
    active.cancelling = false;
    toast(error instanceof Error ? error.message : "Could not request cancellation.", true);
  }
  if (state.job?.id === active.id) {
    updateJobProgress();
    schedulePoll(150);
  }
}

async function pollJob(): Promise<void> {
  const active = state.job;
  if (!active) return;
  try {
    const job = await api<Job>(`/api/jobs/${encodeURIComponent(active.id)}`);
    if (state.job?.id !== active.id) return;
    if (job.status === "running" || job.status === "cancelling") {
      active.message = job.message || active.message;
      active.progress = job.progress;
      active.cancellable = job.cancellable === true;
      active.cancelling = job.status === "cancelling";
      updateJobProgress();
      schedulePoll();
      return;
    }
    state.job = null;
    if (job.status === "canceled") {
      if (active.action.startsWith("miro-frame-")) resetFrameRecovery();
      if (active.action === "change-output-lookup" && active.caseId) changeOutputsLookupComplete(active.caseId, active.id, undefined, job.message || "Transaction lookup canceled.");
      state.error = "";
      toast(job.message || "Calculation canceled. Saved investigation runs are unchanged.");
      if (["pegouts", "pegouts-preview"].includes(active.action) && active.caseId && state.activeCase?.id === active.caseId) {
        state.activeCase = await api<Case>(`/api/cases/${encodeURIComponent(active.caseId)}`);
      }
    } else if (job.status === "failed") {
      if (active.action.startsWith("miro-frame-")) resetFrameRecovery();
      const progress = job.progress || active.progress;
      const lastStage = progress?.phase
        ? ` Last reported stage: ${human(progress.phase)}${
            Number.isFinite(progress.completed) && Number.isFinite(progress.total) && progress.total > 0
              ? ` (${progress.completed} / ${progress.total})`
              : ""
          }.`
        : "";
      state.error =
        (job.message || "The action did not complete. Check the launching terminal.") + lastStage;
      toast(state.error, true);
      if (active.action === "change-output-lookup" && active.caseId) changeOutputsLookupComplete(active.caseId, active.id, undefined, state.error);
      if ((active.action.startsWith("miro-") || ["pegouts", "pegouts-preview"].includes(active.action)) && active.caseId && state.activeCase?.id === active.caseId) {
        state.activeCase = await api<Case>(`/api/cases/${encodeURIComponent(active.caseId)}`);
      }
    } else {
      const result = job.result || {};
      if (active.action === "miro-frame-review" && active.caseId) {
        const detail = state.activeCase;
        if (detail?.id === active.caseId && detail.miro_board && frameRecoveryComplete(active.caseId, active.id, result)) {
          dialogAction = "miro-frame-recover";
          const content = frameRecoveryDialog(detail.id, boardUrl(detail.miro_board));
          if (!content) throw new Error("Reopen the investigation and review its interrupted frame again.");
          dialog.innerHTML = content;
          dialog.showModal();
          dialog.querySelector<HTMLButtonElement>('[data-action="close-dialog"]')?.focus();
          toast("Frame review ready. Inspect the linked board before confirming recovery.");
        } else {
          resetFrameRecovery();
          toast("Frame lookup completed. Choose Recover interrupted frame again to review it in this investigation.");
        }
      } else if (active.action === "change-output-lookup" && active.caseId) {
        const applied = state.activeCase?.id === active.caseId && changeOutputsLookupComplete(active.caseId, active.id, result);
        toast(applied ? "Transaction outputs loaded. Choose the change output, then save." : "Transaction lookup completed. Open Change outputs and load the transaction again to review it.");
      } else if (active.action === "lookup") {
        if (!active.live || job.live === false) {
          throw new Error("This lookup used synthetic data. Load your transaction hashes again to start a live investigation.");
        }
        state.draft.reports = result.transactions || [];
        state.draft.txids = state.draft.reports
          .map((report) => report.txid)
          .join(", ");
        state.draft.selected = new Set();
        toast(
          `Loaded outputs from ${state.draft.reports.length} transaction${state.draft.reports.length === 1 ? "" : "s"}. Choose the outputs to follow.`,
        );
      } else if (active.caseId) {
        const detail = await api<Case>(
          `/api/cases/${encodeURIComponent(active.caseId)}`,
        );
        if (state.activeCase?.id === active.caseId) {
          state.activeCase = detail;
          if (active.action === "trace") state.selectedRun = "latest";
          if (active.action === "plot") {
            const draft = currentWorkflow(detail);
            draft.plot = String(result.preview_id || "");
            draft.boardPlot = draft.plot;
            state.caseView = "plots";
          }
          if (["board-create", "board-link"].includes(active.action)) {
            const draft = currentWorkflow(detail);
            draft.board = String(result.record_id || result.id || "");
            draft.boardUrl = ""; draft.recoveryUrl = "";
            state.caseView = "boards";
          }
          if (["pegouts", "pegouts-preview"].includes(active.action)) {
            currentPegoutSearch(detail);
            pegoutDraft.selected = String(result.search_id || result.run_id || "");
            pegoutDraft.approved = "";
          }
          if (["miro-recover", "miro-frame-recover", "miro-rebuild"].includes(active.action) && result.run_id && detail.runs?.some(run => run.id === result.run_id)) {
            state.selectedRun = result.run_id;
          }
        }
        if (active.action === "address-inspect" && state.activeCase?.id === active.caseId) {
          const address = String(result.address || "");
          const inspected = await api<AddressRow>(`/api/cases/${encodeURIComponent(active.caseId)}/address`,
            { address, run_id: state.selectedRun });
          const selected = state.addressReview.selected;
          if (selected?.address === address) selected.activity = inspected.activity;
          const row = state.addressReview.data?.rows.find(item => item.address === address);
          if (row) row.activity = inspected.activity;
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
              max_hops: result.max_hops, connection_count: result.connection_count, connection_status: result.connection_status,
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
              trace: "Collected data saved. Choose Plot views to generate a chart.",
              plot: "Plot generated from saved collection data.",
              "board-create": "Miro board created. Select a saved plot to sync.",
              "board-link": "Miro board linked. Select a saved plot to sync.",
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
    await refreshSession();
    render();
  } catch (error) {
    if (active.action.startsWith("miro-frame-")) {resetFrameRecovery(); dialog.close();}
    // Do not forget a potentially running live action when the browser loses contact.
    if (error instanceof ApiError && error.status === 404) {
      state.job = null;
      state.error =
        "The local server no longer tracks this action. Review the saved investigation before starting it again.";
      try {
        await refreshSession();
        if (state.activeCase)
          state.activeCase = await api<Case>(
            `/api/cases/${encodeURIComponent(state.activeCase.id)}`,
          );
      } catch {
        // Keep the recovery notice if the restarted server goes away again.
        // A user can reopen or refresh the page after starting liquid-web.
      }
      render();
    } else if (state.job) {
      state.job.message = "Waiting to reconnect to the local server…";
      state.job.progress = undefined;
      updateJobProgress();
      schedulePoll(3500);
    } else {
      state.error =
        error instanceof Error
          ? error.message
          : "Could not refresh the saved result.";
      render();
    }
  }
}

function updateJobClock(): void {
  const element = document.querySelector("#job-elapsed");
  if (!state.job || !element) return;
  const elapsed = Math.max(
    0,
    Math.floor((Date.now() - state.job.started) / 1000),
    Number.isFinite(state.job.progress?.elapsed_seconds) ? Math.floor(state.job.progress!.elapsed_seconds!) : 0,
  );
  element.textContent =
    elapsed >= 60
      ? `${Math.floor(elapsed / 60)}m ${elapsed % 60}s elapsed`
      : `${elapsed}s elapsed`;
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

function openActionDialog(action: string): void {
  resetFrameRecovery();
  const detail = state.activeCase;
  if (!detail || isBusy()) return;
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
  dialog.innerHTML = `<form id="action-form"><header class="dialog-head"><div><h2 id="dialog-title">${titles[action]}</h2><p>${descriptions[action]}</p></div><button type="button" class="dialog-close" data-action="close-dialog" aria-label="Close dialog">${icon("close")}</button></header><div class="dialog-body">${action === "trace" ? `${numericField(settings, "hops", "Additional hops for this run", "0 retries the current frontier. This does not change saved defaults.")}${traceSummary(settings)}<p class="small muted">Other limits and graph preferences come from Investigation settings.</p><button type="button" class="btn small" data-action="edit-case-settings">Edit investigation settings</button>` : action === "miro-create" ? `<label class="field"><span>Board name</span><input name="board_name" required maxlength="60" value="${esc(defaultBoardName)}"/></label>` : action === "miro-recover" ? `<div class="dialog-board"><span>Linked board</span><a class="board-link" href="${esc(boardUrl(detail.miro_board!))}" target="_blank" rel="noopener noreferrer">Open linked board ${icon("external")}</a><span>${detail.miro_recovery?.pending_count} unconfirmed items</span></div><label class="check-line"><input type="checkbox" name="confirm_empty" required/><span><strong>I inspected this Miro board after the failed sync and it is empty.</strong><small>If any objects are present, cancel and reconcile the pending items individually.</small></span></label>` : `<div class="dialog-board"><span>Linked board</span><strong>${esc(detail.miro_board)}</strong><span style="margin-top:10px">Selected snapshot</span><strong class="mono">${esc(currentRun()?.id || detail.latest_run)}</strong><span style="margin-top:10px">New item budget</span><strong>${settings.max_new_items} items</strong>${compact ? `<span style="margin-top:10px">Saved preview</span><strong class="mono">${esc(compact.preview_id)}</strong><span>${esc(human(compact.connector_style || "straight"))} connectors · ${compact.include_fees ? "Fee flows included" : "Fee flows hidden"}</span>` : ""}</div>`}${live ? `<div class="alert ${["miro-organize", "miro-compact"].includes(action) ? "warning" : ""}">${icon(["miro-organize", "miro-compact"].includes(action) ? "info" : "lock")}<div><strong>${action === "trace" ? "Uses your Blockstream API credits" : action === "miro-recover" ? "Reads Miro and updates local recovery state" : "Changes your Miro workspace"}</strong><p>SecretSpec retrieves credentials through the launching terminal. Complete any Proton Pass prompt there.</p></div></div>` : '<div class="alert">' + icon("shield") + "<div><strong>Offline synthetic data</strong><p>This run uses the investigation’s saved fixture and needs no API credentials.</p></div></div>"}</div><footer class="dialog-footer"><button type="button" class="btn" data-action="close-dialog">Cancel</button><button type="submit" class="btn primary">${icon(action === "trace" ? "play" : action === "miro-create" ? "plus" : "refresh")}${action === "trace" ? "Collect transaction data" : action === "miro-create" ? "Create private board" : action === "miro-organize" ? "Sync and reorganize" : action === "miro-compact" ? "Apply compact layout" : action === "miro-frames" ? "Create / update frames" : action === "miro-recover" ? "Verify empty board and recover" : "Sync to Miro"}</button></footer></form>`;
  dialog.showModal();
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
  if (action.startsWith("view-") && state.activeCase) {
    const view = action.slice(5);
    if (["collect", "plots", "boards", "history"].includes(view)) {
      saveSettingsDraft(); saveAddressDraft();
      state.caseView = view as CaseView; state.page = "case"; render();
    }
    return;
  }
  if (await workflowAction(action, element)) return;
  if (await pegoutAction(action)) return;
  if (action === "address-import-open") action = "input-import-open";
  if (action === "input-import-open" && state.activeCase && !isBusy()) {
    saveSettingsDraft(); saveAddressDraft(); state.page = "case-settings";
  }
  if (state.activeCase && await inputImportAction(action, {
      caseId: state.activeCase.id, busy: isBusy(), render,
      post: (path, body) => api(path, body as Record<string, unknown>),
      refresh: async () => {
        const detail = state.activeCase;
        if (!detail) return;
        resetAddressImport(detail.id); resetNameColors(detail.id); resetChangeOutputs(detail.id);
        state.addressReview.selected = null; state.addressReview.data = null;
      }
    }, element)) return;
  if (action === "miro-frame-review") {
    const detail = state.activeCase;
    if (!detail?.miro_board || !detail.miro_recovery?.can_recover_frame || isBusy()) return;
    const version = beginFrameRecovery(detail.id);
    await startJob(`/api/cases/${encodeURIComponent(detail.id)}/actions`, {action}, action, true, detail.id);
    if (state.job?.caseId === detail.id && state.job.action === action) frameRecoveryStarted(detail.id, version, state.job.id);
    return;
  }
  if (action === "change-outputs-open" && state.activeCase && !isBusy()) {saveSettingsDraft(); saveAddressDraft(); state.page = "case-settings"; render();}
  if (state.activeCase && await changeOutputsAction(action, {
      caseId: state.activeCase.id, busy: isBusy(), render,
      post: (path, body) => api(path, body as Record<string, unknown>),
      startLookup: async txid => {
        const detail = state.activeCase;
        if (!detail) return null;
        await startJob(`/api/cases/${encodeURIComponent(detail.id)}/actions`,
          {action: "change-output-lookup", txid}, "change-output-lookup", !detail.fixture, detail.id);
        return state.job?.id || null;
      }
    }, element)) return;
  if (action === "name-colors-open" && state.activeCase && !isBusy()) {saveSettingsDraft(); saveAddressDraft(); if (state.page !== "case-settings") navigate("case-settings");}
  if (state.activeCase && await nameColorsAction(action, {caseId: state.activeCase.id, busy: isBusy(), render,
      post: (path, body) => api(path, body as Record<string, unknown>)}, element)) return;
  if (state.activeCase && await addressImportAction(action, {
      caseId: state.activeCase.id, busy: isBusy(), render,
      post: (path, body) => api(path, body as Record<string, unknown>),
      refresh: async () => { state.addressReview.selected = null; await loadAddresses(0); }
    })) return;
  if (action === "address-merge-dialog") {
    await openAddressMergeDialog();
    return;
  }
  if (action === "cancel-job") {
    await cancelJob();
    return;
  }
  if (action === "dismiss-error") {
    state.error = "";
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
    void suggestCenterNames().catch(() => {});
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
      { source: "live", txids: state.draft.txids },
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
    if (action === "connections") {
      const raw = (document.querySelector<HTMLInputElement>("#connection-hops")?.value || "").trim();
      const hops = Number(raw);
      if (!raw || !Number.isInteger(hops) || hops < 0 || hops > 2147483647) throw new Error("Enter a nonnegative whole-number hop limit.");
      body.connection_hops = hops;
    } else {
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
    state.selectedRun = element.dataset.run;
    render();
    return;
  }
  if (element.dataset.action)
    void dispatch(element.dataset.action, element).catch(handleError);
});

app.addEventListener("change", (event) => {
  const element = event.target as HTMLInputElement | HTMLSelectElement;
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
    state.selectedRun = element.value;
    state.addressReview.selected = null;
    void loadAddresses(0).catch(handleError);
    return;
  }
  if (element.id === "run-picker") {
    state.selectedRun = element.value;
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
  if ((event.target as HTMLInputElement).name === "center_name" && state.page === "case-settings") {
    clearTimeout(centerNameTimer);
    centerNameTimer = setTimeout(() => { void suggestCenterNames().catch(() => {}); }, 150);
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
    if (form.id === "address-search-form") {
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
      submitting = true;
      render();
      try {
        const created = await api<Case>("/api/cases", {
          name: state.draft.name,
          source: "live",
          board: state.draft.board,
          seeds,
          settings: state.draft.settings,
        });
        await refreshSession();
        await openCase(created.id);
        state.draft = {
          name: "",
          txids: "",
          seeds: "",
          board: "",
          settings: { ...state.settings },
          reports: [],
          selected: new Set(),
        };
        toast(
          "Investigation created. Import your data or start the first run.",
        );
      } finally {
        submitting = false;
        render();
      }
    } else if (form.id === "settings-form") {
      const savedKey = settingsKey();
      const settings = readSettings(form, {...defaults, ...(state.page === "case-settings" ? state.activeCase?.run_defaults : state.settings)});
      const data = new FormData(form);
      if (state.page === "case-settings" && state.activeCase) {
        const caseId = state.activeCase.id;
        await api(`/api/cases/${encodeURIComponent(caseId)}/settings`, {
          name: String(data.get("name") || ""),
          board: state.activeCase.miro_board || "",
          settings,
        });
        state.activeCase = await api<Case>(
          `/api/cases/${encodeURIComponent(caseId)}`,
        );
      } else {
        await api("/api/settings", { settings });
        state.draft.settings = { ...settings };
      }
      await refreshSession();
      settingsDrafts.delete(savedKey);
      renderedSettingsKey = "";
      render();
      toast("Settings saved locally.");
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
dialog.addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target as HTMLFormElement;
  const detail = state.activeCase;
  if (!detail || isBusy() || !form.reportValidity()) return;
  const action = dialogAction;
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
    run_id: action === "trace" ? "latest" : state.selectedRun,
  };
  if (action === "miro-rebuild") {
    if (!dialogRebuild || dialogRebuild.caseId !== detail.id) return;
    const data = new FormData(form);
    body.run_id = dialogRebuild.runId;
    body.source_board = dialogRebuild.sourceBoard;
    body.name = String(data.get("board_name") || "");
    body.max_new_items = Number(data.get("max_new_items"));
  }
  if (action === "trace") body.hops = Number(new FormData(form).get("hops"));
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
  state.draft.settings = { ...state.settings };
  const match = location.hash.match(/^#case\/([a-z0-9]+)$/);
  if (match) await openCase(match[1]);
  else render();
}

setInterval(updateJobClock, 1000);
void initialize().catch((error) => {
  app.innerHTML = `<main class="startup"><h1>The local server is unavailable</h1><p>${esc(error instanceof Error ? error.message : "Could not open the workspace.")}</p><p class="muted">Launch liquid-web from your devenv terminal, then open its local URL.</p><button type="button" class="btn primary" id="retry-start">Try again</button></main>`;
  document
    .querySelector("#retry-start")
    ?.addEventListener("click", () => location.reload());
});
