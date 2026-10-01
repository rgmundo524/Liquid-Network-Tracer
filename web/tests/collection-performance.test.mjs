import assert from 'node:assert/strict';
import {test} from 'node:test';
import {collectionPerformancePanel, performanceDuration} from '../src/scripts/collection-performance.ts';

test('older snapshots and unknown performance schemas do not add an empty panel', () => {
  for (const value of [undefined, {}, {schema_version: 2, tracing_seconds: 10}, {schema_version: 1}]) {
    assert.equal(collectionPerformancePanel(value), '');
  }
});

test('saved tracing and address-count times stay separate from overlapping request totals', () => {
  const html = collectionPerformancePanel({schema_version: 1, tracing_seconds: 4500,
    address_counts_seconds: 510, request_count: 24991, checkpoint_seconds: 3600,
    fetch_wait_seconds: 890, processing_seconds: 10, checkpoint_count: 120,
    network_seconds_total: 8000, pacing_wait_seconds_total: 6200, retry_wait_seconds_total: 30,
    evidence_seconds_total: 100, worker_peak: 32, worker_limit: 64, peak_in_flight: 27,
    rate_limit_responses: 1, retry_responses: 2, cache_hits: 9});
  assert.match(html, /<dt>Tracing<\/dt><dd>1 h 15 min<\/dd>/);
  assert.match(html, /<dt>Address counts<\/dt><dd>8 min 30 s<\/dd>/);
  assert.match(html, /<dt>Trace API attempts<\/dt><dd>24,991<\/dd>/);
  assert.match(html, /<dt>Average trace requests\/s<\/dt><dd>5.6<\/dd>/);
  assert.match(html, /<dt>Saving progress<\/dt><dd>1 h 0 min<\/dd>/);
  assert.match(html, /<dt>Saving response evidence<\/dt><dd>1 min 40 s<\/dd>/);
  assert.match(html, /Cumulative time across trace requests/);
  assert.match(html, /must not be added to elapsed time/);
  assert.match(html, /include retries and authentication requests/);
  assert.doesNotMatch(html, /counts\/s|%/);
});

test('malformed metrics cannot become HTML and zero elapsed time does not invent a rate', () => {
  const html = collectionPerformancePanel({schema_version: 1, tracing_seconds: 0, request_count: 0,
    address_counts_seconds: '<script>private</script>', network_seconds_total: Infinity,
    checkpoint_seconds: NaN, fetch_wait_seconds: -1, checkpoint_count: 1.2,
    processing_seconds: true, private: '<script>private</script>'});
  assert.match(html, /<dt>Tracing<\/dt><dd>0 s<\/dd>/);
  assert.match(html, /<dt>Trace API attempts<\/dt><dd>0<\/dd>/);
  assert.doesNotMatch(html, /private|script|NaN|Infinity|Average trace|Saving progress|Progress checkpoints/);
});

test('duration formatting remains readable at minute and hour boundaries', () => {
  assert.equal(performanceDuration(0), '0 s');
  assert.equal(performanceDuration(.1), '<1 s');
  assert.equal(performanceDuration(59.9), '1 min 0 s');
  assert.equal(performanceDuration(3599.9), '1 h 0 min');
});
