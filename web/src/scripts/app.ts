export {};

type ConnectorStyle = "straight" | "curved" | "elbowed";
type LayoutCounts = { crossings: number; node_overlaps: number; node_intersections: number; truncated?: boolean };
type LayoutMetrics = { before: LayoutCounts; after: LayoutCounts; estimated: boolean };
type RenderingMetadata = {
  layout_algorithm?: "elk_layered_v1" | "dependency_layers_v1";
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
  include_fees: boolean;
  connector_style: ConnectorStyle;
};
type Run = {
  id: string;
  status: string;
  stop_reason?: string;
  created_at?: string;
  transaction_count?: number;
  frontier_count?: number;
};
type Download = { name: string; url: string };
type Artifact = RenderingMetadata & {
  downloads: Download[];
  preview_url?: string;
  include_fees: boolean;
  connector_style?: ConnectorStyle;
  layout_metrics?: LayoutMetrics;
};
type RunArtifacts = { mermaid?: Artifact; csv?: Artifact; elk?: Artifact };
type JobProgress = {
  phase: string;
  completed: number;
  total: number;
  message: string;
  retry_after?: number;
  elapsed_seconds?: number;
};
type Case = {
  id: string;
  name: string;
  latest_run?: string;
  fixture?: string | boolean;
  miro_board?: string;
  run_defaults: Settings;
  status?: string;
  seeds?: string[];
  seed_count?: number;
  created_at?: string;
  runs?: Run[];
  artifacts?: Record<string, RunArtifacts>;
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
type Result = RenderingMetadata & {
  connector_style?: ConnectorStyle;
  layout_metrics?: LayoutMetrics;
  downloads?: Download[];
  include_fees?: boolean;
  preview_url?: string;
  transactions?: Report[];
  run_id?: string;
  status?: string;
  new_items?: number;
  new_shapes?: number;
  new_connectors?: number;
  fee_items_to_remove?: number;
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
type Page = "dashboard" | "new" | "case" | "settings" | "case-settings";
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
  include_fees: false,
  connector_style: "straight",
};
const state = {
  csrf: "",
  settings: { ...defaults },
  cases: [] as Case[],
  page: "dashboard" as Page,
  activeCase: null as Case | null,
  selectedRun: "latest",
  job: null as ActiveJob | null,
  error: "",
  demoTxids: [] as string[],
  results: new Map<string, { action: string; result: Result }>(),
  artifacts: new Map<string, RunArtifacts>(),
  draft: {
    name: "",
    source: "demo",
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
let submitting = false;
let pageGeneration = 0;

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
const isBusy = (): boolean => !!state.job || submitting;
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

function budgetFields(settings: Settings, prefix = ""): string {
  const fields: [keyof Settings, string, string][] = [
    ["hops", "Additional hops", "Per bounded run"],
    ["max_transactions", "Transactions", "Maximum per run"],
    ["max_outpoints", "Output lookups", "Maximum per run"],
    ["max_requests", "API attempts", "Includes retries"],
    ["max_seconds", "Time limit (seconds)", "Trace request budget"],
    ["max_new_items", "New Miro items", "Maximum per sync"],
  ];
  return `<div class="budget-grid">${fields.map(([key, label, hint]) => `<label class="field"><span>${label}</span><input name="${prefix}${key}" type="number" min="${key === "max_seconds" ? "0.01" : ["hops", "max_new_items"].includes(key) ? "0" : "1"}" step="${key === "max_seconds" ? "any" : "1"}" required value="${esc(settings[key])}"/><small>${hint}</small></label>`).join("")}</div><div class="settings-divider"></div><label class="check-line"><input name="${prefix}include_fees" type="checkbox"${settings.include_fees ? " checked" : ""}/><span><strong>Include transaction fee flows</strong><small>Show fees in graph views. The archived evidence always retains fee data.</small></span></label><div class="settings-divider"></div><label class="field"><span>Miro connector appearance</span><select name="${prefix}connector_style">${connectorOptions(settings.connector_style)}</select><small>Straight lines are the default. Return connections may use elbows to avoid running back through a transaction. Miro draws its own routes; Mermaid has an independent layout.</small></label>`;
}

function readSettings(form: HTMLFormElement): Settings {
  const data = new FormData(form);
  return {
    hops: Number(data.get("hops")),
    max_transactions: Number(data.get("max_transactions")),
    max_outpoints: Number(data.get("max_outpoints")),
    max_requests: Number(data.get("max_requests")),
    max_seconds: Number(data.get("max_seconds")),
    max_new_items: Number(data.get("max_new_items")),
    include_fees: data.has("include_fees"),
    connector_style: (data.get("connector_style") || "straight") as ConnectorStyle,
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
    ["settings", "Settings", "settings"],
  ]
    .map(
      ([page, label, symbol]) =>
        `<button class="nav-button${state.page === page || (page === "dashboard" && ["case", "case-settings"].includes(state.page)) ? " active" : ""}" data-page="${page}"${state.page === page ? ' aria-current="page"' : ""}>${icon(symbol)}<span>${label}</span></button>`,
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
  const names: Record<Page, string> = {
    dashboard: "Investigations",
    new: "New investigation",
    case: state.activeCase?.name || "Investigation",
    settings: "Workspace settings",
    "case-settings": "Investigation settings",
  };
  app.innerHTML = `<div class="layout">${sidebar()}<div class="main-shell"><header class="topbar"><div class="breadcrumb">${icon("folder")}<span>Workspace</span>${icon("chevron")}<strong>${esc(names[state.page])}</strong></div><div class="topbar-right"><span class="local-pill">${icon("lock")} LOCAL SESSION</span><span class="avatar" aria-label="Investigation workspace">LT</span></div></header><main id="main" class="content" tabindex="-1">${jobBanner()}${state.error ? `<div class="alert error" role="alert">${icon("info")}<div><strong>Unable to complete the action</strong><p>${esc(state.error)}</p></div><button class="dismiss" data-action="dismiss-error" aria-label="Dismiss error">${icon("close")}</button></div>` : ""}${state.page === "dashboard" ? dashboard() : state.page === "new" ? newCase() : state.page === "case" ? workspace() : settingsPage()}</main></div></div>`;
  updateJobClock();
  if (focus)
    document
      .querySelector<HTMLHeadingElement>("#page-title")
      ?.focus({ preventScroll: true });
}

function dashboard(): string {
  const traced = state.cases.filter((item) => !!item.latest_run).length;
  const linked = state.cases.filter((item) => !!item.miro_board).length;
  return `<div class="page-heading"><div><div class="eyebrow">Your local workspace</div><h1 id="page-title" tabindex="-1">Investigations</h1><p>Trace selected outputs through bounded, documented runs.</p></div>${button("New investigation", "new", "plus", "primary")}</div><div class="stats-grid"><div class="stat"><div><div class="stat-label">Investigations</div><div class="stat-value">${state.cases.length.toString().padStart(2, "0")}</div><div class="stat-note">Saved on this computer</div></div><span class="stat-icon">${icon("folder")}</span></div><div class="stat"><div><div class="stat-label">Investigations with runs</div><div class="stat-value">${traced.toString().padStart(2, "0")}</div><div class="stat-note">Bounded, recorded traces</div></div><span class="stat-icon">${icon("layers")}</span></div><div class="stat"><div><div class="stat-label">Linked Miro boards</div><div class="stat-value">${linked.toString().padStart(2, "0")}</div><div class="stat-note">Editable graph workspaces</div></div><span class="stat-icon">${icon("board")}</span></div></div><section class="panel"><div class="panel-head"><div><h2>Saved investigations</h2><p>Pick up where you left off, with every run kept intact.</p></div>${button("Refresh", "refresh", "refresh", "ghost small")}</div>${state.cases.length ? `<div class="table-wrap"><table><thead><tr><th>Investigation</th><th>Source</th><th>Latest run</th><th>Miro board</th><th><span class="sr-only">Open</span></th></tr></thead><tbody>${state.cases.map((item) => `<tr><td><div class="case-cell"><span class="case-icon">${icon("folder")}</span><div><button class="case-title" data-case="${esc(item.id)}">${esc(item.name)}</button><span class="small muted">${esc(formatDate(item.created_at))}</span></div></div></td><td><span class="badge ${item.fixture ? "purple" : ""}">${item.fixture ? "Synthetic demo" : "Live Liquid"}</span></td><td>${item.latest_run ? `<span class="mono">${esc(short(item.latest_run, 8))}</span><div class="small muted">${esc(human(item.status || "saved"))}</div>` : '<span class="small muted">Ready for first run</span>'}</td><td><span class="badge ${item.miro_board ? "" : "gray"}">${item.miro_board ? "Linked" : "Not linked"}</span></td><td>${button("Open", "open-case", "arrow", "ghost small", false, `data-id="${esc(item.id)}" aria-label="Open ${esc(item.name)}"`)}</td></tr>`).join("")}</tbody></table></div><div class="table-footer"><span>${state.cases.length} investigation${state.cases.length === 1 ? "" : "s"}</span><span>Saved runs are shared with the terminal interface</span></div>` : `<div class="empty-state"><div class="empty-icon">${icon("folder")}</div><h2>Start with a transaction</h2><p>Create an investigation, choose the outputs to follow, and run a trace with a clear stopping point.</p>${button("Create your first investigation", "new", "plus", "primary")}</div>`}</section><div class="info-grid"><div class="info-card">${icon("graph")}<div><h3>A clear path from evidence to graph</h3><p>Select starting UTXOs, run a bounded trace, then explore locally with Mermaid or add the result to your Miro board.</p></div></div><div class="info-card secondary">${icon("shield")}<div><h3>Credentials stay behind the scenes</h3><p>Live actions use SecretSpec and Proton Pass through your terminal. No API keys are entered in this interface.</p></div></div></div>`;
}

function newCase(): string {
  const draft = state.draft;
  return `<div class="page-heading"><div><div class="eyebrow">Build a starting point</div><h1 id="page-title" tabindex="-1">New investigation</h1><p>Choose the transactions and exact outputs you want to follow.</p></div>${button("Back to investigations", "dashboard", "", "ghost")}</div><form id="new-case-form"><div class="form-grid"><div class="form-stack"><section class="panel"><div class="panel-head"><h2><span class="section-number">1</span> Investigation details</h2></div><div class="panel-body"><label class="field"><span>Investigation name</span><input name="name" maxlength="120" placeholder="e.g. Service withdrawal review" value="${esc(draft.name)}" required autocomplete="off"/></label><span class="field-label">Data source</span><div class="source-options"><label class="source-option"><input type="radio" name="source" value="demo"${disabled(isBusy())}${draft.source === "demo" ? " checked" : ""}/><span><strong>Synthetic demo</strong><small>Local fixture · No credentials</small></span></label><label class="source-option"><input type="radio" name="source" value="live"${disabled(isBusy())}${draft.source === "live" ? " checked" : ""}/><span><strong>Live Liquid</strong><small>Blockstream API · Proton Pass</small></span></label></div><label class="field"><span>Miro board URL or ID <span class="muted">(optional)</span></span><input name="board" placeholder="You can link or create a board later" value="${esc(draft.board)}" autocomplete="off"/></label></div></section><section class="panel"><div class="panel-head"><div><h2><span class="section-number">2</span> Starting outputs</h2><p>Multiple transactions can share one investigation.</p></div></div><div class="panel-body"><label class="field"><span>Transaction hashes</span><textarea name="txids" class="mono" rows="3" spellcheck="false" placeholder="Paste transaction hashes separated by commas">${esc(draft.txids)}</textarea><small>Paste up to 100 transaction hashes, separated by commas, spaces, or newlines.</small></label><div class="heading-actions">${button("Load outputs", "lookup", "search", "", isBusy())}${draft.source === "demo" ? button("Use demo transaction", "demo-tx", "", "ghost small", isBusy()) : ""}</div>${draft.source === "live" ? '<p class="small muted" style="margin-top:12px">This lookup uses your Blockstream credits. Watch the terminal for Proton Pass prompts.</p>' : ""}<div id="lookup-outputs">${draft.reports.length ? '<p class="small muted" style="margin-top:17px">Amounts are base units; ?? means unavailable.</p>' : ""}${draft.reports
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
    )}${draft.reports.length ? `<p class="selection-count" id="selection-count">${draft.selected.size} starting output${draft.selected.size === 1 ? "" : "s"} selected</p>` : ""}</div><details class="direct-seeds"${draft.seeds ? " open" : ""}><summary>Enter exact output references directly</summary><label class="field"><span>Starting outputs</span><textarea name="seeds" class="mono" rows="2" spellcheck="false" placeholder="TRANSACTION_HASH:0, TRANSACTION_HASH:1">${esc(draft.seeds)}</textarea><small>Optional. These numeric outpoints are combined with checked outputs above.</small></label></details></div></section><section class="panel"><div class="panel-head"><h2><span class="section-number">3</span> Run limits &amp; display</h2></div><div class="panel-body">${budgetFields(draft.settings)}</div></section><div class="form-actions"><p>Creates the investigation locally. You will review the first run before tracing.</p><button class="btn primary" type="submit"${disabled(isBusy())}>${icon("plus")}Create investigation</button></div></div><aside class="form-stack"><div class="side-note"><strong>Follow specific outputs</strong>Each selected UTXO becomes a starting point. Shared descendants appear once in the cumulative graph.<ul><li>Choose relevant outputs after lookup.</li><li>Fees and unspendable outputs cannot be selected.</li><li>Hidden amounts and assets appear as ??.</li></ul></div><div class="side-note"><strong>One case, several runs</strong>Each run has its own budget. Continue unfinished branches in a later run, including after closing this interface.</div><div class="side-note"><strong>Try the complete local workflow</strong>The synthetic demo can trace, render Mermaid, and export CSV without any API credentials.</div></aside></div></form>`;
}

function boardUrl(board: string): string {
  return `https://miro.com/app/board/${encodeURIComponent(board)}/`;
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

function localGraph(artifact: Artifact | undefined, saved: boolean, includeFees: boolean): string {
  const matches = artifact?.include_fees === includeFees;
  const preview = matches ? safeLocalUrl(artifact?.preview_url) : "";
  const svg = matches ? artifact?.downloads.find((item) => item.name === "graph.svg") : undefined;
  const source = matches ? artifact?.downloads.find((item) => item.name === "graph.mmd") : undefined;
  const mismatch = artifact && !matches;
  const name = artifact?.renderer === "direct_svg" ? "Direct SVG fallback" : "Mermaid preview";
  const notice = preview ? fallbackNotice(artifact) : "";
  return `<section class="panel"><div class="panel-head graph-panel-head"><div><h2>Local graph</h2><p>${esc(name)} of the selected saved run.</p></div><div class="artifact-actions">${downloadLink(svg, "Download SVG", "primary")}${preview ? `<a class="btn ghost small" href="${esc(preview)}" target="_blank" rel="noopener noreferrer">${icon("external")}Open full view</a>` : '<span class="badge gray">Offline renderer</span>'}</div></div>${preview ? `${notice ? `<div class="panel-body artifact-note">${esc(notice)}</div>` : ""}<iframe class="graph-preview" src="${esc(preview)}" title="${esc(name)} of the selected investigation run" sandbox="allow-scripts" referrerpolicy="no-referrer"></iframe><div class="preview-caption"><span>Generated from saved evidence · ${includeFees ? "Fee flows included" : "Fee flows hidden"}</span>${downloadLink(source, "Mermaid source (.mmd)", "ghost")}</div>` : `<div class="graph-placeholder"><div class="mini-flow" aria-hidden="true"><span class="mini-node">${icon("folder")}</span><span class="mini-connection"></span><span class="mini-node tx">${icon("layers")}</span><span class="mini-connection"></span><span class="mini-node out">${icon("folder")}</span></div><h3>${mismatch ? "Update the chart’s fee display" : "Your trace, in perspective"}</h3><p>${mismatch ? "The saved chart uses a different fee setting. Create another chart to match this investigation’s current settings." : saved ? "Create a quick local chart from this snapshot, then download the SVG or Mermaid source." : "After your first run, create a local chart or send the flow to an editable Miro board."}</p>${button("Create Mermaid chart", "mermaid", "graph", "", !saved || isBusy())}</div>`}</section>`;
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
  return `<div class="layout-metrics"><table><caption>Estimated layout quality</caption><thead><tr><th scope="col">Measure</th><th scope="col">Baseline layout</th><th scope="col">${esc(label)}</th></tr></thead><tbody>${measures.map(([key, label]) => `<tr><th scope="row">${label}</th><td>${count(metrics.before[key], metrics.before.truncated)}</td><td>${count(metrics.after[key], metrics.after.truncated)}</td></tr>`).join("")}</tbody></table><p class="layout-metrics-note">Compared with the built-in layout of this saved run, not the live board. Labels are not measured and Miro routes may differ.${metrics.before.truncated || metrics.after.truncated ? " The comparison limit was reached. Counts marked ≥ are lower bounds, so the total may be higher." : ""}</p></div>`;
}

function elkGraph(artifact: Artifact | undefined, saved: boolean, settings: Settings): string {
  const matches = artifact?.include_fees === settings.include_fees &&
    artifact?.connector_style === settings.connector_style;
  const preview = matches ? safeLocalUrl(artifact?.preview_url) : "";
  const downloads = matches ? artifact?.downloads : undefined;
  const svg = downloads?.find((item) => item.name === "graph.svg");
  const report = downloads?.find((item) => item.name === "layout-report.json");
  const fallback = artifact?.layout_algorithm === "dependency_layers_v1";
  const name = fallback ? "Dependency layout fallback" : "ELK layout preview";
  const notice = preview ? fallbackNotice(artifact) : "";
  return `<section class="panel" id="elk-layout-panel"><div class="panel-head graph-panel-head"><div><h2>${esc(name)}</h2><p>Local placement preview for the selected saved run.</p></div><div class="artifact-actions">${downloadLink(svg, fallback ? "Download SVG" : "Download ELK SVG", "primary")}${preview ? `<a class="btn ghost small" href="${esc(preview)}" target="_blank" rel="noopener noreferrer">${icon("external")}Open full view</a>` : '<span class="badge gray">Offline layout</span>'}</div></div>${preview ? `${notice ? `<div class="panel-body artifact-note">${esc(notice)}</div>` : ""}${layoutMetrics(artifact?.layout_metrics, fallback ? "Dependency layout" : "ELK layout")}<iframe class="graph-preview" src="${esc(preview)}#chart" title="${esc(name)} of the selected investigation run" sandbox="allow-scripts" referrerpolicy="no-referrer"></iframe><div class="preview-caption"><span>${esc(human(settings.connector_style))} connectors · ${settings.include_fees ? "Fee flows included" : "Fee flows hidden"}</span>${downloadLink(report, "Layout report", "ghost")}</div><div class="panel-body elk-explanation"><p>${fallback ? "The dependency layout calculates placement" : "ELK calculates placement"}; Miro draws the editable graph. This offline preview does not read your live board arrangement. Return connections can remain elbowed with the straight-line setting.</p>${button("Refresh layout preview", "layout", "refresh", "small", !saved || isBusy())}</div>` : `<div class="panel-body"><p class="artifact-note">${artifact && !matches ? "The saved layout preview uses different fee or connector settings. Generate a new preview to match this investigation." : "Inspect the proposed left-to-right layout and estimated crossings before updating Miro. No API credentials are needed."}</p>${button("Create ELK layout preview", "layout", "layers", "", !saved || isBusy())}<p class="small muted elk-hint">Previewing leaves your board unchanged. Sync and reorganize applies a fresh arrangement and the selected run together. ELK calculations continue until completed or canceled; large graphs can take longer.</p></div>`}</section>`;
}

function csvDownloads(artifact: Artifact | undefined, saved: boolean, includeFees: boolean): string {
  const matches = artifact?.include_fees === includeFees;
  const downloads = matches ? artifact.downloads.filter((item) => safeLocalUrl(item.url)) : [];
  const tables = downloads.filter((item) => item.name.endsWith(".csv"));
  const provenance = downloads.filter((item) => !item.name.endsWith(".csv"));
  const mismatch = artifact && !matches;
  return `<section class="panel"><div class="panel-head"><div><h2>CSV downloads</h2><p>${tables.length ? "Download individual tables from the selected saved run." : "Export verified tables and provenance for this snapshot."}</p></div>${tables.length ? '<span class="badge">Saved locally</span>' : ""}</div>${tables.length ? `<div class="downloads">${tables.map((item) => downloadLink(item, `Download ${item.name}`, "download-link")).join("")}</div><div class="export-footer"><span class="small muted">${includeFees ? "Fee flows included" : "Fee flows hidden"} in graph tables · Evidence tables retain all recorded outputs.</span>${provenance.length ? `<details class="provenance-downloads"><summary>Export provenance</summary><div class="artifact-actions">${provenance.map((item) => downloadLink(item, item.name, "ghost")).join("")}</div></details>` : ""}</div>` : `<div class="panel-body"><p class="artifact-note">${mismatch ? "The saved export uses a different fee setting. Create a new export to match the current settings." : "Create the CSV export once, then download the files individually whenever you reopen this investigation."}</p>${button("Create CSV export", "csv", "table", "", !saved || isBusy())}</div>`}</section>`;
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
  const artifacts = { ...cachedArtifacts, ...savedArtifacts };
  const last = state.results.get(detail.id);
  const settings = { ...defaults, ...detail.run_defaults };
  const runOptions = (detail.runs || [])
    .map(
      (item) =>
        `<option value="${esc(item.id)}"${(state.selectedRun === "latest" ? detail.latest_run : state.selectedRun) === item.id ? " selected" : ""}>${esc(item.id)}${item.id === detail.latest_run ? " · Latest" : ""}</option>`,
    )
    .join("");
  return `<div class="page-heading case-heading"><div><div class="eyebrow">Investigation workspace</div><h1 id="page-title" tabindex="-1">${esc(detail.name)}</h1><div class="workspace-meta"><span class="badge ${detail.fixture ? "purple" : ""}">${detail.fixture ? "Synthetic demo" : "Live Liquid"}</span><span class="badge gray">${(detail.runs || []).length} saved run${detail.runs?.length === 1 ? "" : "s"}</span><span class="mono">${esc(short(detail.id, 8))}</span></div></div><div class="heading-actions">${button("Settings", "case-settings", "settings", "", isBusy())}${button(saved ? "Continue investigation" : "Start first run", "trace-dialog", "play", "primary", isBusy())}</div></div>${last ? resultBanner(last.action, last.result) : ""}<div class="workspace-grid"><div class="workspace-main"><section class="panel"><div class="panel-head"><div><h2>${saved ? "Saved run" : "Ready to trace"}</h2><p>${saved ? "Select a snapshot to review, render, or export." : "Your starting outputs and limits are saved."}</p></div>${saved ? `<label class="run-picker">Snapshot<select class="input" id="run-picker" aria-label="Saved run snapshot">${runOptions}</select></label>` : '<span class="badge gray">No runs yet</span>'}</div>${saved ? `<div class="run-summary"><div><span>Tracked transactions</span><strong>${esc(run?.transaction_count ?? "—")}</strong></div><div><span>Unfinished branches</span><strong>${esc(run?.frontier_count ?? "—")}</strong></div><div><span>Run status</span><strong class="text-value">${esc(human(run?.status || "saved"))}</strong></div></div><div class="run-note">${icon("clock")}<span>${esc(formatDate(run?.created_at))}${run?.stop_reason ? ` · ${esc(human(run.stop_reason))}` : ""}</span></div>` : `<div class="empty-state" style="padding:31px 24px"><div class="empty-icon">${icon("graph")}</div><h2>${detail.seed_count ?? detail.seeds?.length ?? "Your"} starting output${(detail.seed_count ?? detail.seeds?.length) === 1 ? "" : "s"} selected</h2><p>Run the first trace to record exact output spends and build your investigation graph.</p>${button("Review run limits", "trace-dialog", "play", "primary", isBusy())}</div>`}</section>${elkGraph(artifacts.elk, saved, settings)}${localGraph(artifacts.mermaid, saved, settings.include_fees)}${csvDownloads(artifacts.csv, saved, settings.include_fees)}<section class="panel"><div class="panel-head"><div><h2>Run history</h2><p>Every continuation preserves the preceding snapshot.</p></div><span class="badge gray">${(detail.runs || []).length} runs</span></div>${detail.runs?.length ? `<div class="table-wrap"><table class="run-list"><thead><tr><th>Run</th><th>Recorded</th><th>Status</th><th>Transactions</th></tr></thead><tbody>${detail.runs.map((item) => `<tr class="${item.id === run?.id ? "selected" : ""}"><td><button data-run="${esc(item.id)}">${esc(short(item.id, 8))}</button>${item.id === detail.latest_run ? '<div class="muted">Latest</div>' : ""}</td><td><span class="muted">${esc(formatDate(item.created_at))}</span></td><td><span class="badge ${item.status === "error" ? "red" : "gray"}">${esc(human(item.status))}</span></td><td>${esc(item.transaction_count ?? "—")}</td></tr>`).join("")}</tbody></table></div>` : '<div class="panel-body small muted">Your first completed or bounded run will appear here.</div>'}</section></div><aside class="workspace-aside"><section class="panel"><div class="panel-body"><div class="action-card-head"><span class="action-icon">${icon("board")}</span><div><h3>Miro board</h3><p>Your editable investigation graph</p></div></div><p class="action-description">Preview changes locally, then sync the selected snapshot to your board.</p>${detail.miro_board ? `<a class="board-link" href="${esc(boardUrl(detail.miro_board))}" target="_blank" rel="noopener noreferrer">Open linked board ${icon("external")}</a>` : '<p class="board-empty">No board linked yet. Create one or add its URL in settings.</p>'}<div class="action-buttons">${button("Preview changes", "miro-preview", "search", "wide", !saved || !detail.miro_board || isBusy())}${button("Sync to Miro", "miro-sync-dialog", "refresh", "", !saved || !detail.miro_board || isBusy())}${button("Sync and reorganize", "miro-organize-dialog", "graph", "wide", !saved || !detail.miro_board || isBusy())}${!detail.miro_board ? button("Create Miro board", "miro-create-dialog", "plus", "wide", isBusy()) : ""}</div></div></section><section class="panel"><div class="panel-body local-tools"><div class="action-card-head"><span class="action-icon teal">${icon("download")}</span><div><h3>Local outputs</h3><p>From the selected snapshot</p></div></div>${button("ELK layout preview <span>Layout</span>", "layout", "layers", "", !saved || isBusy())}${button("Mermaid chart <span>Graph</span>", "mermaid", "graph", "", !saved || isBusy())}${button("Create CSV export <span>7 tables</span>", "csv", "table", "", !saved || isBusy())}<p class="small muted" style="margin-top:13px;font-size:10px">New exports are saved with the investigation. Archived evidence stays intact.</p></div></section><section class="panel"><div class="panel-body"><div class="action-card-head"><span class="action-icon blue">${icon("shield")}</span><div><h3>Bounded by design</h3><p>Saved defaults for this investigation</p></div></div><dl class="saved-settings"><div><dt>Additional hops</dt><dd>${settings.hops}</dd></div><div><dt>Transactions</dt><dd>${settings.max_transactions}</dd></div><div><dt>API attempts</dt><dd>${settings.max_requests}</dd></div><div><dt>Time limit</dt><dd>${settings.max_seconds}s</dd></div><div><dt>Fee flows</dt><dd>${settings.include_fees ? "Included" : "Hidden"}</dd></div><div><dt>Connectors</dt><dd>${esc(human(settings.connector_style))}</dd></div><div><dt>New Miro items</dt><dd>${settings.max_new_items}</dd></div></dl><div class="settings-divider"></div><p class="small muted" style="font-size:10px;line-height:1.75">Graph paths show UTXO reachability. They do not determine ownership or allocate a hidden value.</p></div></section></aside></div>`;
}

function resultBanner(action: string, result: Result): string {
  if (["csv", "mermaid", "layout"].includes(action)) return "";
  const titles: Record<string, string> = {
    trace: "Run saved",
    "miro-preview": "Miro change preview",
    "miro-sync": "Miro sync finished",
    "miro-organize": "Miro sync and reorganization finished",
    "miro-create": "Miro board created",
  };
  const numbers: [string, unknown][] = action.startsWith("miro-")
    ? [
        ["New items", result.new_items],
        ["New shapes", result.new_shapes],
        ["New connectors", result.new_connectors],
        ["Fee items to remove", result.fee_items_to_remove],
      ]
    : [];
  return `<div class="alert">${icon("check")}<div><strong>${titles[action] || "Action completed"}</strong><p>${action === "trace" ? `The saved run ${esc(result.run_id || "")} is ready to review and export.` : action === "miro-preview" ? "This is an offline plan. Live sync checks the board before making changes." : action === "miro-create" ? "The board is linked to this investigation. Sync a saved run to add the graph." : "The result is saved with this investigation."}</p>${
    numbers.some(([, value]) => typeof value === "number")
      ? `<div class="result-grid">${numbers
          .filter(([, value]) => typeof value === "number")
          .map(
            ([label, value]) =>
              `<div><strong>${esc(value)}</strong>${label}</div>`,
          )
          .join("")}</div>`
      : ""
  }${fallbackNotice(result) ? `<p>${esc(fallbackNotice(result))}</p>` : ""}${layoutMetrics(result.layout_metrics, result.layout_algorithm === "dependency_layers_v1" ? "Dependency layout" : "ELK layout")}${typeof result.conflicts_count === "number" && result.conflicts_count > 0 ? `<p style="margin-top:10px"><strong>${result.conflicts_count} board conflict${result.conflicts_count === 1 ? "" : "s"} preserved.</strong> Review your board and the saved Miro sync report before further changes.</p>` : ""}</div><button class="dismiss" data-action="dismiss-result" aria-label="Dismiss result">${icon("close")}</button></div>`;
}

function settingsPage(): string {
  const isCase = state.page === "case-settings";
  const detail = state.activeCase;
  const settings = {
    ...defaults,
    ...(isCase ? detail?.run_defaults : state.settings),
  };
  return `<div class="page-heading"><div><div class="eyebrow">${isCase ? "Investigation preferences" : "Workspace preferences"}</div><h1 id="page-title" tabindex="-1">${isCase ? "Investigation settings" : "Settings"}</h1><p>${isCase ? `Saved preferences for ${esc(detail?.name || "this investigation")}.` : "Defaults for newly created investigations."}</p></div>${button(isCase ? "Back to investigation" : "Back to investigations", isCase ? "back-case" : "dashboard", "", "ghost")}</div><form id="settings-form" class="settings-layout">${isCase ? `<section class="panel"><div class="panel-head"><h2>Investigation details</h2></div><div class="panel-body"><label class="field"><span>Investigation name</span><input name="name" maxlength="120" required value="${esc(detail?.name)}" autocomplete="off"/></label><label class="field"><span>Miro board URL or ID</span><input name="board" value="${esc(detail?.miro_board)}" placeholder="Link an existing board, or leave empty" autocomplete="off"/><small>Board selection is case configuration. API credentials stay in Proton Pass.</small></label></div></section>` : ""}<section class="panel"><div class="panel-head"><div><h2>Run limits &amp; graph display</h2><p>${isCase ? "Applies to future actions in this investigation." : "Existing investigations retain their own saved preferences."}</p></div></div><div class="panel-body">${budgetFields(settings)}</div></section><section class="panel"><div class="panel-body"><div class="action-card-head"><span class="action-icon teal">${icon("lock")}</span><div><h3>SecretSpec &amp; Proton Pass</h3><p>Managed by your existing local environment</p></div></div><p class="action-description" style="margin-bottom:0">Live Blockstream and Miro actions retrieve credentials through the launching terminal. If Proton Pass needs your attention, its prompt appears there. No API keys are stored in browser settings.</p></div></section><div class="form-actions"><p>${isCase ? "Saved runs and their original evidence remain unchanged." : "These defaults are also used by the terminal interface."}</p><button type="submit" class="btn primary"${disabled(isBusy())}>${icon("check")}Save settings</button></div></form>`;
}

function saveDraft(): void {
  const form = document.querySelector<HTMLFormElement>("#new-case-form");
  if (!form) return;
  const data = new FormData(form);
  state.draft.name = String(data.get("name") || "");
  state.draft.source = String(data.get("source") || state.draft.source);
  state.draft.txids = String(data.get("txids") || "");
  state.draft.seeds = String(data.get("seeds") || "");
  state.draft.board = String(data.get("board") || "");
  state.draft.settings = readSettings(form);
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
    if (recovered.action === "lookup")
      state.draft.source = recovered.live ? "live" : "demo";
    schedulePoll();
  }
}

async function openCase(id: string): Promise<void> {
  const generation = ++pageGeneration;
  const detail = await api<Case>(`/api/cases/${encodeURIComponent(id)}`);
  if (generation !== pageGeneration) return;
  saveDraft();
  state.activeCase = detail;
  state.selectedRun = "latest";
  state.page = "case";
  state.error = "";
  history.replaceState(null, "", `#case/${encodeURIComponent(id)}`);
  render(true);
}

function navigate(page: Page): void {
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
      state.error = "";
      toast(job.message || "Calculation canceled. Saved investigation runs are unchanged.");
    } else if (job.status === "failed") {
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
    } else {
      const result = job.result || {};
      if (active.action === "lookup") {
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
        }
        const key = `${active.caseId}:${result.run_id || detail.latest_run || "latest"}`;
        state.results.set(active.caseId, { action: active.action, result });
        if (["mermaid", "csv", "layout"].includes(active.action)) {
          state.artifacts.set(key, {
            ...state.artifacts.get(key),
            [active.action === "layout" ? "elk" : active.action]: {
              downloads: result.downloads || [],
              preview_url: safeLocalUrl(result.preview_url) || undefined,
              include_fees: result.include_fees ?? detail.run_defaults.include_fees,
              connector_style: result.connector_style,
              layout_metrics: result.layout_metrics,
              layout_algorithm: result.layout_algorithm,
              renderer: result.renderer,
              fallback_reason: result.fallback_reason,
            },
          });
        }
        toast(
          (
            {
              trace: "Bounded run saved.",
              mermaid: result.renderer === "direct_svg" ? "Direct SVG fallback is ready. " + fallbackNotice(result) : "Mermaid chart is ready.",
              layout: result.layout_algorithm === "dependency_layers_v1" ? "Dependency layout fallback is ready. " + fallbackNotice(result) : "ELK layout preview is ready.",
              csv: "CSV export is ready to download.",
              "miro-preview": "Miro change preview is ready.",
              "miro-sync": "Miro sync finished.",
              "miro-organize": "Miro sync and reorganization finished.",
              "miro-create": "Miro board created and linked.",
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

function openActionDialog(action: string): void {
  const detail = state.activeCase;
  if (!detail || isBusy()) return;
  dialogAction = action;
  const settings = { ...defaults, ...detail.run_defaults };
  const titles: Record<string, string> = {
    trace: detail.latest_run
      ? "Continue this investigation"
      : "Start the first run",
    "miro-sync": "Sync the graph to Miro",
    "miro-organize": "Sync and reorganize the Miro graph",
    "miro-create": "Create a private Miro board",
  };
  const descriptions: Record<string, string> = {
    trace: detail.latest_run
      ? "Continue the latest saved frontier with another bounded allowance. Earlier runs remain available."
      : "Follow the selected starting outputs within the limits below.",
    "miro-sync":
      "Add the selected saved graph to the linked board. Existing manual arrangements are preserved during normal sync.",
    "miro-organize":
      "Sync the selected run and apply a fresh layout to the managed graph in one action. ELK calculates the full graph layout before board updates begin. This replaces existing positions, including arrangements made by hand, and sets transaction inputs on the left and outputs on the right.",
    "miro-create":
      "Create an empty private board in your Miro account and link it to this investigation. Publishing the graph is a separate sync action.",
  };
  const live = action !== "trace" || !detail.fixture;
  const defaultBoardName =
    `${detail.fixture ? "SYNTHETIC DEMO · " : ""}${detail.name}`.slice(0, 60);
  dialog.innerHTML = `<form id="action-form"><header class="dialog-head"><div><h2 id="dialog-title">${titles[action]}</h2><p>${descriptions[action]}</p></div><button type="button" class="dialog-close" data-action="close-dialog" aria-label="Close dialog">${icon("close")}</button></header><div class="dialog-body">${action === "trace" ? `${budgetFields(settings)}<p class="small muted" style="margin:17px 0;line-height:1.7">These limits are saved as the investigation defaults and apply to this bounded run.</p>` : action === "miro-create" ? `<label class="field"><span>Board name</span><input name="board_name" required maxlength="60" value="${esc(defaultBoardName)}"/></label>` : `<div class="dialog-board"><span>Linked board</span><strong>${esc(detail.miro_board)}</strong><span style="margin-top:10px">Selected snapshot</span><strong class="mono">${esc(currentRun()?.id || detail.latest_run)}</strong><span style="margin-top:10px">New item budget</span><strong>${settings.max_new_items} items</strong></div>`}${live ? `<div class="alert ${action === "miro-organize" ? "warning" : ""}">${icon(action === "miro-organize" ? "info" : "lock")}<div><strong>${action === "trace" ? "Uses your Blockstream API credits" : "Changes your Miro workspace"}</strong><p>SecretSpec retrieves credentials through the launching terminal. Complete any Proton Pass prompt there.</p></div></div>` : '<div class="alert">' + icon("shield") + "<div><strong>Offline synthetic demo</strong><p>This run uses the bundled fixture and needs no API credentials.</p></div></div>"}</div><footer class="dialog-footer"><button type="button" class="btn" data-action="close-dialog">Cancel</button><button type="submit" class="btn primary">${icon(action === "trace" ? "play" : action === "miro-create" ? "plus" : "refresh")}${action === "trace" ? "Start bounded run" : action === "miro-create" ? "Create private board" : action === "miro-organize" ? "Sync and reorganize" : "Sync to Miro"}</button></footer></form>`;
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
  if (action === "case-settings") {
    navigate("case-settings");
    return;
  }
  if (action === "back-case") {
    navigate("case");
    return;
  }
  if (action === "demo-tx") {
    saveDraft();
    state.draft.txids = state.demoTxids.join(", ");
    render();
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
      { source: state.draft.source, txids: state.draft.txids },
      "lookup",
      state.draft.source === "live",
    );
    return;
  }
  if (["mermaid", "csv", "layout", "miro-preview"].includes(action))
    await caseAction(action);
}

function handleError(error: unknown): void {
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
  if (element.name === "source") {
    saveDraft();
    state.draft.reports = [];
    state.draft.selected.clear();
    state.draft.seeds = "";
    state.draft.txids =
      state.draft.source === "demo" ? state.demoTxids.join(", ") : "";
    render();
  }
});

// Keep an in-memory draft while a lookup runs so its completion does not
// discard names, notes, or limit edits typed in the meantime.
app.addEventListener("input", (event) => {
  if ((event.target as Element).closest("#new-case-form")) saveDraft();
});

app.addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target as HTMLFormElement;
  if (isBusy() || !form.reportValidity()) return;
  void (async () => {
    if (form.id === "new-case-form") {
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
          source: state.draft.source,
          board: state.draft.board,
          seeds,
          settings: state.draft.settings,
        });
        await refreshSession();
        await openCase(created.id);
        state.draft = {
          name: "",
          source: "demo",
          txids: state.demoTxids.join(", "),
          seeds: "",
          board: "",
          settings: { ...state.settings },
          reports: [],
          selected: new Set(),
        };
        toast(
          "Investigation created. Review the first run when you are ready.",
        );
      } finally {
        submitting = false;
        render();
      }
    } else if (form.id === "settings-form") {
      const settings = readSettings(form);
      const data = new FormData(form);
      if (state.page === "case-settings" && state.activeCase) {
        const caseId = state.activeCase.id;
        await api(`/api/cases/${encodeURIComponent(caseId)}/settings`, {
          name: String(data.get("name") || ""),
          board: String(data.get("board") || ""),
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
      render();
      toast("Settings saved locally.");
    }
  })().catch(handleError);
});

dialog.addEventListener("click", (event) => {
  if ((event.target as Element).closest('[data-action="close-dialog"]'))
    dialog.close();
});
dialog.addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target as HTMLFormElement;
  const detail = state.activeCase;
  if (!detail || isBusy() || !form.reportValidity()) return;
  const action = dialogAction;
  const body: Record<string, unknown> = {
    action,
    run_id: action === "trace" ? "latest" : state.selectedRun,
  };
  if (action === "trace") body.settings = readSettings(form);
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
  try {
    state.demoTxids = (await api<{ txids: string[] }>("/api/demo")).txids;
  } catch {
    /* Manual seed entry remains available. */
  }
  if (state.draft.source === "demo")
    state.draft.txids = state.demoTxids.join(", ");
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
