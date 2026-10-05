import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from contextlib import contextmanager
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from email.utils import parsedate_to_datetime
from http.client import HTTPException

from .common import StopRun, TraceError, canonical, read_json
from .networks import blockchain as normalize_blockchain, default_api
from .explorer_quota import LocalExplorerQuota, SharedExplorerQuota
from .explorer_http import ExplorerHTTP, ExplorerRequestTimeout, TransientExplorerConnection, network_failure

TOKEN_URL = "https://login.blockstream.com/realms/blockstream-public/protocol/openid-connect/token"
ENTERPRISE = "https://enterprise.blockstream.info/liquid/api"
MAX_BODY = 32 * 1024 * 1024
ADAPTIVE_RATE_ATTEMPTS = 16


def _rate_settings(base, advertised_rps=None):
    """Return the initial interval and mode without claiming a provider quota."""
    if advertised_rps is None:
        advertised_rps = os.getenv("LIQUID_BLOCKSTREAM_API_RPS") or None
    factor = .95
    label = "Advertised Blockstream requests per second"
    mode, source = "fixed", "advertised"
    if advertised_rps is None:
        if urllib.parse.urlsplit(base).hostname != "enterprise.blockstream.info":
            return .25, "fixed", "conservative_default", None
        advertised_rps = os.getenv("LIQUID_BLOCKSTREAM_ENTERPRISE_RPS") or "auto"
        if advertised_rps.strip().lower() == "auto":
            return 1. / 49., "adaptive", "enterprise_adaptive", None
        factor = 1.
        label = "Blockstream enterprise target requests per second"
        source = "enterprise_target"
    try:
        advertised_rps = float(advertised_rps)
    except (ValueError, TypeError):
        raise TraceError(label + " must be a positive number") from None
    if not math.isfinite(advertised_rps) or advertised_rps <= 0:
        raise TraceError(label + " must be a positive number")
    effective_rps = advertised_rps * factor
    if effective_rps == 0:
        raise TraceError(label + " is too small")
    interval = 1. / effective_rps
    if not math.isfinite(interval):
        raise TraceError(label + " is too small")
    return interval, mode, source, advertised_rps if source == "advertised" else None


def default_min_interval(base=ENTERPRISE, advertised_rps=None):
    """Initial pacing interval; enterprise auto mode can subsequently grow.

    A verified allowance is enforced at 95 percent. A numeric enterprise target
    remains fixed, while the default/``auto`` starts at 49 RPS and learns from
    successful responses and server pressure. Other endpoints retain 4 RPS.
    """
    return _rate_settings(base, advertised_rps)[0]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TraceError("Unexpected HTTP redirect; verify the configured API endpoint")


def http(method, url, headers=None, body=None, timeout=20):
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    opener = urllib.request.build_opener(NoRedirect())
    try:
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise TraceError("API response exceeds 32 MiB")
                return response.status, dict(response.headers), raw
        except urllib.error.HTTPError as error:
            with error:
                raw = error.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise TraceError("API response exceeds 32 MiB")
                return error.code, dict(error.headers), raw
    except (urllib.error.URLError, TimeoutError, OSError, HTTPException) as error:
        raise network_failure(method, error, token_request=url == TOKEN_URL) from None


@dataclass
class Limits:
    max_hops: int = 3
    max_transactions: int = 0
    max_outpoints: int = 0
    max_requests: int = 0
    max_seconds: float = 0

    def validate(self):
        if any(type(value) is not int or value < 0 for value in
               (self.max_hops, self.max_transactions, self.max_outpoints, self.max_requests)):
            raise TraceError("Hop and collection limits must be non-negative whole numbers; zero resource limits are unlimited")
        if (type(self.max_seconds) not in (int, float) or not math.isfinite(self.max_seconds)
                or self.max_seconds < 0):
            raise TraceError("max_seconds must be finite and non-negative; zero is unlimited")


class Budget:
    def __init__(self, limits):
        self.limits = limits
        self.started = time.monotonic()
        self.requests = 0
        self._lock = threading.RLock()

    def check(self):
        if self.limits.max_seconds and time.monotonic() - self.started >= self.limits.max_seconds:
            raise StopRun("time_limit")

    def remaining_seconds(self, current=None):
        """None means no run deadline; individual HTTP requests stay bounded."""
        if not self.limits.max_seconds:
            return None
        current = time.monotonic() if current is None else current
        return max(0., self.limits.max_seconds - (current - self.started))

    def request(self):
        with self._lock:
            self.check_request()
            self.requests += 1

    def check_request(self):
        with self._lock:
            self.check()
            if self.limits.max_requests and self.requests >= self.limits.max_requests:
                raise StopRun("request_limit")

    def timeout(self):
        self.check()
        remaining = self.remaining_seconds()
        return 20. if remaining is None else max(.01, min(20., remaining))

    def pause(self, seconds):
        self.check()
        remaining = self.remaining_seconds()
        if remaining is not None and seconds > remaining:
            raise StopRun("time_limit")
        time.sleep(max(0., seconds))


class _RetryWindow:
    """Keep adaptive throttling separate from a broken endpoint's retries."""

    def __init__(self, adaptive):
        self.adaptive = adaptive
        self.attempts = self.rate_failures = self.transient_failures = 0

    def feedback(self, status=None):
        if status == 429:
            self.rate_failures += 1
            failures, limit = self.rate_failures, ADAPTIVE_RATE_ATTEMPTS
        else:
            self.transient_failures += 1
            failures, limit = self.transient_failures, 4
        if self.adaptive:
            return failures - 1, failures >= limit
        return self.attempts - 1, self.attempts >= 4


class Esplora:
    def __init__(self, store, run_id, limits, base=None, auth="blockstream",
                 fixture=None, tx_cache_seconds=86400, min_interval=None, transport=http,
                 workers=8, advertised_rps=None, adaptive_workers=False, shared_quota=None, *, blockchain="liquid"):
        self.store, self.run_id = store, run_id
        self.blockchain = normalize_blockchain(blockchain)
        self.base = (default_api(self.blockchain) if base is None else base).rstrip("/")
        self.budget = Budget(limits)
        self.transport = transport
        self.auth = auth
        self.tx_cache_seconds = tx_cache_seconds
        _, self.api_rate_mode, self.rate_limit_source, self.advertised_rps = _rate_settings(
            self.base, advertised_rps)
        # Keep the interval helper as the single injectable pacing boundary
        # used by offline transports and deterministic pipeline tests.
        floor = default_min_interval(self.base, advertised_rps)
        self.effective_rps = 1. / floor
        self.min_interval_explicit = min_interval is not None and min_interval > 0
        self.min_interval = floor if min_interval is None else min_interval
        self.last_call = 0.
        self.token = None
        self.token_until = 0.
        self.used = set()
        self.workers = workers
        self._gate = threading.Condition(threading.RLock())
        self._cooldown_until = 0.
        self._stop_reason = None
        self._cancelled = threading.Event()
        self._token_lock = threading.RLock()
        self._token_generation = 0
        self._auth_error = None
        self._results_lock = threading.RLock()
        self._results = {}
        self._metrics_lock = threading.Lock()
        self._latency_seconds = None
        self._service_latency_seconds = None
        self._completed_endpoints = 0
        self._service_local = threading.local()
        self._admission_local = threading.local()
        self._completed_requests = 0
        self._pressure_events = 0
        self._totals = {"network_seconds_total": 0., "pacing_wait_seconds_total": 0.,
                        "retry_wait_seconds_total": 0., "evidence_seconds_total": 0.,
                        "cache_hits": 0, "coalesced_hits": 0,
                        "quota_reserve_calls": 0, "quota_reserve_seconds": 0.,
                        "quota_admitted": 0, "quota_denied": 0,
                        "local_deadline_timeouts": 0,
                        "rate_limit_responses": 0, "retry_responses": 0,
                        "peak_in_flight": 0}
        self._in_flight = 0
        self._pool = None
        self._closed = False
        self.fixture = read_json(fixture) if fixture else None
        if self.fixture is not None:
            self.base = "fixture://" + __import__("hashlib").sha256(canonical(self.fixture)).hexdigest()
            self.auth = "none"
        else:
            parsed = urllib.parse.urlsplit(self.base)
            if parsed.scheme != "https" or parsed.username or parsed.query or parsed.fragment:
                raise TraceError("API base must be HTTPS without credentials, query, or fragment")
            if auth == "blockstream" and parsed.hostname != "enterprise.blockstream.info":
                raise TraceError("Blockstream credentials can only be sent to enterprise.blockstream.info")
            paths = (("/liquid/api", "/liquidtestnet/api") if self.blockchain == "liquid" else
                     ("/api", "/testnet/api", "/testnet4/api", "/signet/api"))
            if parsed.hostname in ("enterprise.blockstream.info", "blockstream.info") and parsed.path not in paths:
                raise TraceError("API path must match the investigation blockchain: " + self.blockchain)
        if not math.isfinite(self.min_interval) or not math.isfinite(tx_cache_seconds) or self.min_interval < 0 or tx_cache_seconds < 0:
            raise TraceError("Interval and cache duration cannot be negative")
        if self.min_interval_explicit and self.api_rate_mode == "adaptive":
            # Auto's 49 RPS is only a warmup. A user-supplied interval replaces
            # it; verified allowances and numeric fixed targets remain floors.
            floor = 0.
        self.min_interval = 0. if self.fixture is not None else max(floor, self.min_interval)
        if self.min_interval_explicit or self.fixture is not None:
            # An explicitly requested spacing is a fixed ceiling, not a
            # suggestion that an adaptive controller can eventually exceed.
            self.api_rate_mode = "fixed"
        if self.fixture is None:
            self.effective_rps = 1. / self.min_interval
            if not math.isfinite(self.effective_rps):
                raise TraceError("Explorer interval is too small to represent a finite request rate")
        self.api_target_rps = 0. if self.fixture is not None else self.effective_rps
        if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 8:
            raise TraceError("Explorer workers must be an integer from 1 to 8")
        if not isinstance(adaptive_workers, bool):
            raise TraceError("Adaptive explorer workers must be a boolean")
        self.worker_ceiling = 64 if adaptive_workers else workers
        self._transport_slots = threading.BoundedSemaphore(self.worker_ceiling)
        # Real HTTP clients automatically coordinate by endpoint host. Fixture
        # and explicitly injected transports remain isolated unless a test or
        # embedding application supplies its own coordinator.
        self._shared_quota = shared_quota if self.fixture is None else None
        self._automatic_quota = self.fixture is None and transport is http
        if self.fixture is None and self.api_rate_mode == "adaptive" and not self._automatic_quota and self._shared_quota is None:
            self._shared_quota = LocalExplorerQuota(self.min_interval, adaptive=True)
        self._owned_transport = ExplorerHTTP(self.base, TOKEN_URL, http) if self._automatic_quota else None
        if self._owned_transport is not None:
            self.transport = self._owned_transport
        self._shared_metrics = None
        self._pacing_error = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        """Stop queued work and drain started requests before closing evidence."""
        with self._results_lock:
            self._closed = True
            pool = self._pool
            results = list(self._results.values())
            for future in results:
                future.cancel()
        self._cancelled.set()
        with self._gate:
            self._gate.notify_all()
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        interrupted = self._drain(results)
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
        if self._owned_transport is not None:
            self._owned_transport.close()
        if self._shared_quota is not None:
            self._shared_quota.close()
        if interrupted:
            raise KeyboardInterrupt()

    @staticmethod
    def _drain(futures):
        interrupted = False
        for future in futures:
            while not future.done():
                try:
                    future.result()
                except KeyboardInterrupt:
                    # A second Ctrl-C must not close SQLite under a worker that
                    # has received a response but is still archiving it.
                    interrupted = True
                except BaseException:
                    break
        return interrupted

    def _remember(self, oid):
        with self._results_lock:
            self.used.add(oid)

    def used_observations(self):
        """Copy evidence IDs while response workers may still be archiving."""
        with self._results_lock:
            return set(self.used)

    def request_metrics(self):
        """Snapshot transport feedback and current shared admission status.

        Shared wait seconds is the latest estimated delay, not cumulative
        threaded waiting time. Network latency and completed_requests retain
        their Esplora-only semantics. service_latency_seconds measures the
        complete endpoint operation, including cache lookup and evidence/JSON
        processing, but excluding admission, authentication and retry waits.
        completed_endpoints advances only after that operation finishes. Cumulative
        timings include OAuth and are overlapping worker seconds, never
        additive phase wall time. Retry waits exclude 429 admission cooldowns,
        which count as pacing waits. cache_hits counts accepted SQLite cached
        responses; coalesced_hits counts reuse of an in-run endpoint Future.
        retry_responses counts HTTP 429/500/502/503/504, whether retried or not.
        """
        with self._metrics_lock:
            metrics = {"latency_seconds": self._latency_seconds,
                       "api_rate_mode": self.api_rate_mode,
                       "api_target_rps": self.api_target_rps,
                       "service_latency_seconds": self._service_latency_seconds,
                       "completed_endpoints": self._completed_endpoints,
                       "completed_requests": self._completed_requests,
                       "pressure_events": self._pressure_events,
                       "in_flight": self._in_flight}
            metrics.update(self._totals)
            if self._shared_metrics is not None:
                metrics.update(self._shared_metrics)
        # Storage metadata is a cheap coordinator snapshot. Never call a
        # coordinator method while holding the metrics lock: admission takes
        # those locks in the opposite order.
        storage_metrics = getattr(self._shared_quota, "storage_metrics", None)
        if callable(storage_metrics):
            storage = storage_metrics()
            if isinstance(storage, dict):
                metrics.update(storage)
        evidence_metrics = getattr(self.store, "storage_metrics", None)
        if callable(evidence_metrics):
            evidence = evidence_metrics()
            if isinstance(evidence, dict):
                metrics.update(evidence)
        return metrics

    @contextmanager
    def _exclude_service(self):
        """Exclude nested waits from this worker's endpoint service sample.

        Admission and authentication can contain further waits. Count the
        outer interval once so a token refresh cannot subtract nested pacing
        twice, or include another thread's overlapping wait time.
        """
        sample = getattr(self._service_local, "sample", None)
        if sample is None:
            yield
            return
        outer = sample["depth"] == 0
        started = time.monotonic() if outer else None
        sample["depth"] += 1
        try:
            yield
        finally:
            sample["depth"] -= 1
            if outer:
                sample["excluded"] += max(0., time.monotonic() - started)

    def _measure_request(self, started, kind, status=None, failed=False):
        elapsed = max(0., time.monotonic() - started)
        with self._metrics_lock:
            self._in_flight -= 1
            self._totals["network_seconds_total"] += elapsed
            self._totals["rate_limit_responses"] += int(status == 429)
            pressure = status in (429, 500, 502, 503, 504)
            self._totals["retry_responses"] += int(pressure)
            if kind == "esplora":
                self._latency_seconds = (elapsed if self._latency_seconds is None else
                                         .2 * elapsed + .8 * self._latency_seconds)
                self._completed_requests += 1
                self._pressure_events += int(pressure or failed)

    def _add_seconds(self, metric, started):
        elapsed = max(0., time.monotonic() - started)
        with self._metrics_lock:
            self._totals[metric] += elapsed

    def _evidence(self, method, *args):
        started = time.monotonic()
        try:
            return method(*args)
        finally:
            self._add_seconds("evidence_seconds_total", started)

    def _admit(self, kind=None, endpoint=None):
        started = time.monotonic()
        with self._exclude_service():
            self._gate.acquire()
            try:
                try:
                    self._reserve_request()
                finally:
                    self._add_seconds("pacing_wait_seconds_total", started)
            except BaseException:
                self._gate.release()
                raise
        try:
            if kind is not None:
                self._evidence(self.store.attempt, self.run_id, kind, endpoint, "started")
            self.last_call = time.monotonic()
            return self.budget.timeout()
        finally:
            self._gate.release()

    def _reserve_request(self):
        # Reserve starts under one shared gate. Increasing workers never
        # multiplies the configured request rate or the run's hard budget.
        with self._gate:
            if self._automatic_quota and self._shared_quota is None:
                self._shared_quota = SharedExplorerQuota(self.base, self.min_interval,
                                                        adaptive=self.api_rate_mode == "adaptive")
            self._admission_local.admission = None
            while True:
                self.budget.check_request()
                if self._cancelled.is_set():
                    raise StopRun("interrupted")
                if self._stop_reason is not None:
                    raise StopRun(self._stop_reason)
                if self._pacing_error is not None:
                    raise self._pacing_error
                current = time.monotonic()
                if self._shared_quota is not None:
                    quota_started = time.monotonic()
                    try:
                        admission = self._shared_quota.reserve()
                    finally:
                        with self._metrics_lock:
                            self._totals["quota_reserve_calls"] += 1
                            self._totals["quota_reserve_seconds"] += max(0., time.monotonic() - quota_started)
                    with self._metrics_lock:
                        self._totals["quota_admitted" if admission.admitted else "quota_denied"] += 1
                        if getattr(admission, "target_rps", 0):
                            self.api_target_rps = admission.target_rps
                        if self._shared_metrics is None:
                            self._shared_metrics = {"shared_api_active_clients": 1,
                                                    "shared_api_effective_rps": self.effective_rps}
                        if admission.active_clients:
                            self._shared_metrics.update(shared_api_active_clients=admission.active_clients,
                                                        shared_api_effective_rps=admission.effective_rps)
                        self._shared_metrics.update(shared_api_wait_seconds=admission.wait_seconds,
                                                    shared_api_wait_reason=admission.reason)
                    delay = 0. if admission.admitted else admission.wait_seconds
                    if admission.admitted:
                        self._admission_local.admission = admission
                else:
                    delay = max(self.last_call + self.min_interval, self._cooldown_until) - current
                if delay <= 0:
                    break
                remaining = self.budget.remaining_seconds(current)
                if remaining is not None and delay >= remaining:
                    raise StopRun("time_limit")
                self._gate.wait(min(delay, .25))
            self.budget.request()

    def _cooldown(self, seconds):
        with self._exclude_service():
            return self._set_cooldown(seconds)

    def _set_cooldown(self, seconds):
        with self._gate:
            if self._shared_quota is not None:
                # Publish the cooldown before deciding whether this run can
                # wait for it. Otherwise another instance could retry at once
                # after a long Retry-After stops the reporting run.
                while not self._shared_quota.cooldown(seconds):
                    self.budget.check()
                    if self._cancelled.is_set():
                        raise StopRun("interrupted")
                    self._gate.wait(.05)
            if not math.isfinite(seconds) or (seconds > 30 and self.api_rate_mode != "adaptive"):
                self._stop_reason = "server_retry_later"
                self._gate.notify_all()
                raise StopRun(self._stop_reason)
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + max(0., seconds))
            self._gate.notify_all()

    @staticmethod
    def _retry_delay(headers, attempt=0, *, backoff_cap=None):
        retry = next((v for k, v in headers.items() if k.lower() == "retry-after"), "")
        try:
            delay = float(retry)
        except (ValueError, TypeError):
            try:
                delay = parsedate_to_datetime(retry).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                delay = 0.
        fallback = 2 ** attempt
        if backoff_cap is not None:
            fallback = min(backoff_cap, fallback)
        return max(fallback, delay) if math.isfinite(delay) else float("inf")

    def _pause_retry(self, seconds):
        """Wait between retries without hiding attempts or delaying cancellation."""
        with self._exclude_service():
            return self._wait_retry(seconds)

    def _wait_retry(self, seconds):
        started = time.monotonic()
        try:
            self.budget.check_request()
            remaining = self.budget.remaining_seconds()
            if remaining is not None and seconds >= remaining:
                raise StopRun("time_limit")
            if self._cancelled.wait(max(0., seconds)):
                raise StopRun("interrupted")
            self.budget.check_request()
        finally:
            self._add_seconds("retry_wait_seconds_total", started)

    def call(self, method, url, kind, endpoint, headers=None, body=None, *, archive_response=False):
        started = time.monotonic()
        with self._exclude_service():
            self._transport_slots.acquire()
            self._add_seconds("pacing_wait_seconds_total", started)
        try:
            return self._call(method, url, kind, endpoint, headers, body, archive_response=archive_response)
        finally:
            self._transport_slots.release()

    def _call(self, method, url, kind, endpoint, headers=None, body=None, *, archive_response=False):
        if archive_response and (kind != "esplora" or method != "GET"):
            raise TraceError("Only explorer GET response bodies may be archived")
        timeout = self._admit(kind, endpoint)
        admission = getattr(self._admission_local, "admission", None)
        started = time.monotonic()
        result = None
        local_deadline_timeout = False
        with self._metrics_lock:
            self._in_flight += 1
            self._totals["peak_in_flight"] = max(self._totals["peak_in_flight"], self._in_flight)
        try:
            try:
                result = self.transport(method, url, headers, body, timeout)
            except BaseException as error:
                local_deadline_timeout = self._is_local_deadline_timeout(error, timeout)
                raise
            finally:
                self._measure_request(started, kind, result[0] if result is not None else None,
                                      failed=result is None and not local_deadline_timeout)
        except (TraceError, OSError, urllib.error.URLError) as error:
            if local_deadline_timeout:
                with self._metrics_lock:
                    self._totals["local_deadline_timeouts"] += 1
                self._evidence(self.store.attempt, self.run_id, kind, endpoint, "local_time_limit")
                raise StopRun("time_limit") from None
            self._evidence(self.store.attempt, self.run_id, kind, endpoint, "network_error")
            if isinstance(error, (TransientExplorerConnection, OSError, urllib.error.URLError)):
                self._rate_feedback(admission, failed=True)
            raise
        if kind == "esplora" or result[0] in (429, 500, 502, 503, 504):
            self._rate_feedback(admission, status=result[0], headers=result[1])
        if result[0] == 429:
            # Close the shared gate as soon as the response arrives. The caller
            # still archives the response before propagating a long cooldown.
            try:
                self._cooldown(self._retry_delay(result[1]))
            except StopRun:
                pass
            except TraceError as error:
                # Preserve the received response as evidence before stopping
                # for a broken coordinator. The next admission stays blocked.
                with self._gate:
                    self._pacing_error = error
                    self._gate.notify_all()
        if archive_response:
            oid = self._evidence(self.store.record_response, self.run_id, kind, self.base,
                                 endpoint, result[2], result[0])
            self._remember(oid)
            return (*result, oid)
        self._evidence(self.store.attempt, self.run_id, kind, endpoint, result[0])
        return result

    def _is_local_deadline_timeout(self, error, timeout):
        """Do not learn provider pressure from our shortened final socket wait.

        A bounded run can give its last request milliseconds of the normal
        twenty-second timeout. Only a known transport timeout after that local
        deadline qualifies. Resets, disconnects and actual HTTP errors retain
        their ordinary pressure behavior even if the budget has also expired.
        """
        while isinstance(error, urllib.error.URLError) and isinstance(error.reason, BaseException):
            error = error.reason
        if timeout >= 20 or not isinstance(error, (ExplorerRequestTimeout, TimeoutError)):
            return False
        remaining = self.budget.remaining_seconds()
        return remaining is not None and remaining <= 0

    def _rate_feedback(self, admission, *, status=None, failed=False, headers=None):
        """React to an individual admission, preserving response archival.

        The admission's pressure generation prevents many simultaneous failures
        from repeatedly reducing the shared rate. Ordinary client errors neither
        increase the target nor masquerade as provider throttling.
        """
        if self.api_rate_mode != "adaptive" or admission is None or self._shared_quota is None:
            return
        try:
            if status is not None and 200 <= status < 300:
                self._shared_quota.success(admission)
            elif failed or status in (429, 500, 502, 503, 504):
                delay = self._retry_delay(headers or {})
                with self._exclude_service(), self._gate:
                    while not self._shared_quota.pressure(admission, seconds=delay):
                        self.budget.check()
                        if self._cancelled.is_set():
                            raise StopRun("interrupted")
                        self._gate.wait(.05)
        except StopRun as error:
            # The caller still records a received response before the next
            # admission fails, including cancellation during quota contention.
            with self._gate:
                self._stop_reason = str(error)
                self._gate.notify_all()
        except TraceError as error:
            with self._gate:
                self._pacing_error = error
                self._gate.notify_all()

    def bearer(self):
        with self._token_lock:
            if self._auth_error is not None:
                raise self._auth_error
            try:
                return self._bearer()
            except TraceError as error:
                self._auth_error = error
                raise

    def _bearer(self):
        if self.token and time.monotonic() < self.token_until:
            return self.token
        client = os.getenv("BLOCKSTREAM_CLIENT_ID", "")
        secret = os.getenv("BLOCKSTREAM_CLIENT_SECRET", "")
        if not client or not secret:
            raise TraceError("Set BLOCKSTREAM_CLIENT_ID and BLOCKSTREAM_CLIENT_SECRET locally")
        payload = urllib.parse.urlencode({"client_id": client, "client_secret": secret,
                       "grant_type": "client_credentials", "scope": "openid"}).encode()
        # This exact client-credentials acquisition may be repeated safely.
        # No other POST is replayed, and token bodies are never archived.
        retries = _RetryWindow(self.api_rate_mode == "adaptive")
        while True:
            retries.attempts += 1
            try:
                status, response_headers, raw = self.call(
                    "POST", TOKEN_URL, "oauth", "/token",
                    {"Content-Type": "application/x-www-form-urlencoded"}, payload)
            except TransientExplorerConnection as error:
                attempt, exhausted = retries.feedback()
                if exhausted:
                    raise type(error)("Blockstream token request network retries exhausted after "
                                      + str(retries.attempts) + " attempts: "
                                      + str(error)) from None
                self._pause_retry(2 ** attempt)
                continue
            if status == 200:
                break
            if status not in (429, 500, 502, 503, 504):
                raise TraceError("Blockstream authentication failed (HTTP " + str(status) + ")")
            if self._pacing_error is not None:
                raise self._pacing_error
            attempt, exhausted = retries.feedback(status)
            delay = self._retry_delay(response_headers, attempt,
                                      backoff_cap=30 if retries.adaptive and status == 429 else None)
            if status == 429:
                self._cooldown(delay)
            if exhausted:
                if retries.adaptive and status == 429:
                    raise TraceError("Blockstream token request rate-limit retries exhausted after "
                                     + str(retries.rate_failures) + " HTTP 429 responses")
                raise TraceError("Blockstream token request retries exhausted after "
                                 + str(retries.attempts) + " attempts (HTTP " + str(status) + ")")
            if not math.isfinite(delay) or (delay > 30 and self.api_rate_mode != "adaptive"):
                raise StopRun("server_retry_later")
            if status != 429:
                self._pause_retry(delay)
        try:
            data = json.loads(raw)
            token = data["access_token"]
            expires = float(data.get("expires_in", 300))
            if not isinstance(token, str) or not token or not math.isfinite(expires):
                raise ValueError("Invalid token")
            self.token = token
            self.token_until = time.monotonic() + max(1., expires - 30)
            self._token_generation += 1
        except (ValueError, KeyError, TypeError):
            raise TraceError("Malformed authentication response") from None
        return self.token

    def _future(self, endpoint):
        with self._results_lock:
            if self._closed:
                raise TraceError("Explorer client is closed")
            self.budget.check()
            future = self._results.get(endpoint)
            owner = future is None
            if owner:
                future = self._results[endpoint] = Future()
            else:
                with self._metrics_lock:
                    self._totals["coalesced_hits"] += 1
            return future, owner

    def _resolve(self, endpoint, future):
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(self._get(endpoint))
        except BaseException as error:
            future.set_exception(error)

    def get(self, endpoint):
        """Fetch an endpoint once per run, coalescing success and failure alike."""
        future, owner = self._future(endpoint)
        if owner:
            self._resolve(endpoint, future)
        return future.result()

    def submit(self, endpoint):
        """Schedule one endpoint and return its coalesced result Future.

        The coordinating caller bounds its rolling window. Workers resolve
        endpoints directly, never waiting on another task in their own pool.
        Call drain_pending before committing a final run snapshot.
        """
        with self._results_lock:
            future, owner = self._future(endpoint)
            if owner:
                try:
                    if self._pool is None:
                        self._pool = ThreadPoolExecutor(max_workers=self.worker_ceiling,
                                                        thread_name_prefix="esplora")
                    self._pool.submit(self._resolve, endpoint, future)
                except BaseException as error:
                    future.set_exception(error)
                    raise
            return future

    def drain_pending(self, cancel=False):
        """Drain existing endpoint work; optionally cancel queued work only."""
        with self._results_lock:
            futures = list(self._results.values())
            if cancel:
                for future in futures:
                    future.cancel()
        if self._drain(futures):
            raise KeyboardInterrupt()

    def prefetch(self, endpoints, *, on_result=None, concurrency=None, on_idle=None,
                 retain_results=True):
        """Return ordered unique endpoints mapped to results or TraceError.

        Stream completed results on the calling thread before replenishing the
        in-flight jobs. A false callback result stops new jobs; already started
        jobs still finish and deliver their results. An optional target callback
        can vary concurrency up to worker_ceiling without changing rate limits.
        The optional advisory idle callback runs on the calling thread so UI
        progress can report a shared cooldown while no response is completing.
        With retain_results=False the callback consumes results as they finish;
        completed endpoint Futures and response bodies are released, and the
        returned mapping is empty. In-flight duplicate requests still coalesce,
        and later lookups can recover successful responses from saved evidence.
        """
        endpoints = list(dict.fromkeys(endpoints))
        if not endpoints:
            return {}
        pending, results = {}, {}
        remaining = iter(endpoints)
        stop = None
        delivered = set()
        callback_stopped = False

        def submit():
            target = concurrency() if concurrency is not None else self.workers
            if isinstance(target, bool) or not isinstance(target, int) or not 1 <= target <= self.worker_ceiling:
                target = self.workers
            while len(pending) < target:
                endpoint = next(remaining, None)
                if endpoint is None:
                    break
                try:
                    future = self.submit(endpoint)
                except StopRun as error:
                    # Match a worker-discovered hard limit: return this error
                    # with the endpoint cohort rather than aborting prefetch.
                    future = Future()
                    future.set_exception(error)
                    pending[future] = endpoint
                    break
                pending[future] = endpoint

        def deliver(endpoint):
            nonlocal stop, callback_stopped
            if on_result is None or endpoint in delivered:
                return
            # Mark first so interruption or an exception inside a consumer can
            # never cause its partially completed write to run a second time.
            delivered.add(endpoint)
            if on_result(endpoint, results[endpoint]) is False:
                callback_stopped = True
                if stop is None:
                    stop = StopRun("prefetch_stopped")

        def release(endpoint, future):
            if not retain_results:
                # Evict only this completed Future. Existing callers can still
                # hold/use it, and a replacement or unrelated request survives.
                with self._results_lock:
                    if future.done() and self._results.get(endpoint) is future:
                        self._results.pop(endpoint)
                results.pop(endpoint, None)
                delivered.discard(endpoint)

        try:
            submit()
            while pending:
                completed, _ = wait(pending, timeout=.25, return_when=FIRST_COMPLETED)
                if not completed and on_idle is not None:
                    try:
                        on_idle()
                    except Exception:
                        # Progress reporting must not abort evidence fetching.
                        pass
                for future in completed:
                    endpoint = pending[future]
                    try:
                        results[endpoint] = future.result()
                    except TraceError as error:
                        results[endpoint] = error
                        if isinstance(error, StopRun):
                            stop = error
                # Inspect every completed error before invoking consumers or
                # replenishing, so a hard limit never permits another batch.
                for future in completed:
                    endpoint = pending[future]
                    deliver(endpoint)
                    pending.pop(future)
                    release(endpoint, future)
                if stop is None:
                    submit()
            if retain_results:
                for endpoint in remaining:
                    results[endpoint] = StopRun("prefetch_stopped" if callback_stopped else str(stop))
        except BaseException as error:
            # Interruptions must not let a response race the run snapshot or
            # Store.close(). close() also catches a job interrupted between
            # pool.submit() and recording its future in the local pending map.
            try:
                self.close()
            except BaseException:
                # Preserve the original failure, including a consumer error.
                pass
            if isinstance(error, KeyboardInterrupt):
                for future, endpoint in pending.items():
                    if future.cancelled() or not future.done():
                        continue
                    try:
                        results[endpoint] = future.result()
                    except BaseException:
                        continue
                    try:
                        deliver(endpoint)
                    except BaseException:
                        break
            for future, endpoint in pending.items():
                release(endpoint, future)
            raise
        if not retain_results:
            return {}
        return {endpoint: results[endpoint] for endpoint in endpoints}

    def _get(self, endpoint):
        started = time.monotonic()
        sample = {"excluded": 0., "depth": 0}
        previous = getattr(self._service_local, "sample", None)
        self._service_local.sample = sample
        try:
            return self._get_response(endpoint)
        finally:
            self._service_local.sample = previous
            elapsed = max(0., time.monotonic() - started - sample["excluded"])
            with self._metrics_lock:
                self._service_latency_seconds = (elapsed if self._service_latency_seconds is None else
                                                  .2 * elapsed + .8 * self._service_latency_seconds)
                self._completed_endpoints += 1

    def _get_response(self, endpoint):
        self.budget.check()
        # Only transaction bodies cross run boundaries. Spend status is refreshed each run.
        ttl = self.tx_cache_seconds if endpoint.count("/") == 2 and endpoint.startswith("/tx/") else 0
        cached = self.store.cached(self.base, endpoint, self.run_id, ttl)
        if cached:
            data, oid = cached
            # Unconfirmed transactions need fresh confirmation status in a new run.
            if not ttl or (isinstance(data, dict) and isinstance(data.get("status"), dict) and data["status"].get("confirmed")) or self.fixture is not None:
                self._remember(oid)
                with self._metrics_lock:
                    self._totals["cache_hits"] += 1
                return data, oid
        if self.fixture is not None:
            self._admit()
            if endpoint not in self.fixture:
                raise TraceError("Synthetic fixture has no response for " + endpoint)
            raw = canonical(self.fixture[endpoint])
            oid = self._evidence(self.store.observe, self.run_id, self.base, endpoint, raw)
            self._remember(oid)
            return json.loads(raw), oid
        auth_retried = False
        retries = _RetryWindow(self.api_rate_mode == "adaptive")
        while True:
            retries.attempts += 1
            from . import __version__
            headers = {"Accept": "application/json", "User-Agent": "liquid-utxo-tracer/" + __version__}
            if self.auth == "blockstream":
                with self._exclude_service():
                    with self._token_lock:
                        headers["Authorization"] = "Bearer " + self.bearer()
                        token_generation = self._token_generation
            try:
                status, response_headers, raw, oid = self.call("GET", self.base + endpoint, "esplora", endpoint,
                                                               headers, archive_response=True)
            except TransientExplorerConnection as error:
                # Fresh, reused and proxy GET failures all consume the original
                # request budget and network-error evidence. Their replacements
                # use the same admission gate and four-failure ceiling as HTTP
                # server errors, with interruptible 1/2/4 second backoff.
                attempt, exhausted = retries.feedback()
                if exhausted:
                    raise type(error)("Explorer network retries exhausted after " + str(retries.attempts) + " attempts for "
                                      + endpoint + ": " + str(error)) from None
                self._pause_retry(2 ** attempt)
                continue
            if status == 200:
                try:
                    return json.loads(raw), oid
                except (ValueError, UnicodeDecodeError):
                    raise TraceError("Explorer returned invalid JSON for " + endpoint) from None
            if status == 401 and self.auth == "blockstream" and not auth_retried:
                with self._exclude_service():
                    with self._token_lock:
                        if token_generation == self._token_generation:
                            self.token = None
                auth_retried = True
                if not retries.adaptive and retries.attempts >= 4:
                    break
                continue
            if status == 429 or status in (500, 502, 503, 504):
                if self._pacing_error is not None:
                    raise self._pacing_error
                attempt, exhausted = retries.feedback(status)
                delay = self._retry_delay(response_headers, attempt,
                                          backoff_cap=30 if retries.adaptive and status == 429 else None)
                if status == 429:
                    # The service's cooldown applies to every worker, including
                    # unrelated endpoints that have not started their request.
                    self._cooldown(delay)
                if exhausted:
                    if retries.adaptive and status == 429:
                        raise TraceError("Explorer rate-limit retries exhausted after "
                                         + str(retries.rate_failures) + " HTTP 429 responses for " + endpoint)
                    break
                if not math.isfinite(delay) or (delay > 30 and self.api_rate_mode != "adaptive"):
                    raise StopRun("server_retry_later")
                if status != 429:
                    self._pause_retry(delay)
                continue
            raise TraceError("Explorer HTTP " + str(status) + " for " + endpoint)
        raise TraceError("Explorer retries exhausted for " + endpoint)
