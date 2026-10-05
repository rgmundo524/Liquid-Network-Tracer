export type CollectionPerformance = {
  schema_version: number;
  tracing_seconds?: number;
  address_counts_seconds?: number;
  fetch_wait_seconds?: number;
  checkpoint_seconds?: number;
  checkpoint_count?: number;
  processing_seconds?: number;
  request_count?: number;
  worker_peak?: number;
  worker_limit?: number;
  network_seconds_total?: number;
  pacing_wait_seconds_total?: number;
  retry_wait_seconds_total?: number;
  evidence_seconds_total?: number;
  cache_hits?: number;
  rate_limit_responses?: number;
  retry_responses?: number;
  peak_in_flight?: number;
};

function metric(value: CollectionPerformance, key: keyof CollectionPerformance): number | undefined {
  const number = value[key];
  return typeof number === "number" && Number.isFinite(number) && number >= 0
    && number <= Number.MAX_SAFE_INTEGER ? number : undefined;
}

export function performanceDuration(seconds: number): string {
  if (seconds === 0) return "0 s";
  if (seconds < 1) return "<1 s";
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600), minutes = Math.floor(total % 3600 / 60), remainder = total % 60;
  return hours ? `${hours} h ${minutes} min` : minutes ? `${minutes} min ${remainder} s` : `${remainder} s`;
}

export function collectionPerformancePanel(value?: CollectionPerformance): string {
  if (!value || value.schema_version !== 1) return "";
  const item = (label: string, display: string): string => `<div><dt>${label}</dt><dd>${display}</dd></div>`;
  const time = (key: keyof CollectionPerformance, label: string): string => {
    const seconds = metric(value, key);
    return seconds === undefined ? "" : item(label, performanceDuration(seconds));
  };
  const count = (key: keyof CollectionPerformance, label: string): string => {
    const number = metric(value, key);
    return number === undefined || !Number.isSafeInteger(number) ? "" : item(label, number.toLocaleString("en-US"));
  };
  const elapsed = metric(value, "tracing_seconds"), requests = metric(value, "request_count");
  const averageRate = elapsed && requests !== undefined ? requests / elapsed : undefined;
  const requestRate = averageRate !== undefined && Number.isFinite(averageRate)
    ? item("Average trace requests/s", averageRate.toFixed(1)) : "";
  const summary = time("tracing_seconds", "Tracing") + time("address_counts_seconds", "Address counts")
    + count("request_count", "Trace API attempts") + requestRate;
  const wall = time("fetch_wait_seconds", "Waiting for fetched data") + time("checkpoint_seconds", "Saving progress")
    + time("processing_seconds", "Other trace processing") + count("checkpoint_count", "Progress checkpoints");
  const totals = time("network_seconds_total", "Network time") + time("pacing_wait_seconds_total", "API pacing waits")
    + time("retry_wait_seconds_total", "Retry waits") + time("evidence_seconds_total", "Saving response evidence");
  const counters = count("worker_peak", "Peak scheduled workers") + count("worker_limit", "Worker limit")
    + count("peak_in_flight", "Peak active requests") + count("cache_hits", "Cache hits")
    + count("rate_limit_responses", "Rate-limit responses") + count("retry_responses", "Retryable responses");
  if (!summary && !wall && !totals && !counters) return "";
  return `<section class="collection-performance" aria-label="Collection performance">${summary ? `<dl class="performance-metrics">${summary}</dl>` : ""}${wall || totals || counters ? `<details class="performance-details"><summary>Collection timing details</summary>${wall ? `<p class="small muted">Within tracing, measured as elapsed time. Address counting runs afterward.</p><dl class="performance-metrics">${wall}</dl>` : ""}${totals ? `<p class="small muted">Cumulative time across trace requests. Concurrent requests overlap, so these totals must not be added to elapsed time.</p><dl class="performance-metrics">${totals}</dl>` : ""}${counters ? `<dl class="performance-metrics">${counters}</dl>` : ""}<p class="small muted">Trace API attempts include retries and authentication requests. Average request rate includes local processing and progress saves. Timings describe this run; saved transactions can include earlier runs.</p></details>` : ""}</section>`;
}
