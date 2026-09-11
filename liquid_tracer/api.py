import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from email.utils import parsedate_to_datetime

from .common import StopRun, TraceError, canonical, read_json

TOKEN_URL = "https://login.blockstream.com/realms/blockstream-public/protocol/openid-connect/token"
ENTERPRISE = "https://enterprise.blockstream.info/liquid/api"
MAX_BODY = 32 * 1024 * 1024


def default_min_interval(base=ENTERPRISE, advertised_rps=None):
    """Use 95% of a verified allowance, otherwise retain the prior 4 RPS cap.

    Enterprise allowances depend on the account. No public Esplora deployment
    configuration is treated as a published enterprise entitlement.
    """
    if advertised_rps is None:
        advertised_rps = os.getenv("LIQUID_BLOCKSTREAM_API_RPS") or None
    if advertised_rps is None:
        return .25
    try:
        advertised_rps = float(advertised_rps)
    except (ValueError, TypeError):
        raise TraceError("Advertised Blockstream requests per second must be a positive number") from None
    if not math.isfinite(advertised_rps) or advertised_rps <= 0:
        raise TraceError("Advertised Blockstream requests per second must be a positive number")
    interval = 1. / (advertised_rps * .95)
    if not math.isfinite(interval):
        raise TraceError("Advertised Blockstream requests per second is too small")
    return interval


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TraceError("Unexpected HTTP redirect; verify the configured API endpoint")


def http(method, url, headers=None, body=None, timeout=20):
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise TraceError("API response exceeds 32 MiB")
            return response.status, dict(response.headers), raw
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read(MAX_BODY)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        # Never echo request headers, credentials, URLs with queries, or server bodies.
        raise TraceError("Network request failed: " + type(error).__name__) from None


@dataclass
class Limits:
    max_hops: int = 3
    max_transactions: int = 250
    max_outpoints: int = 2000
    max_requests: int = 600
    max_seconds: float = 300

    def validate(self):
        if self.max_hops < 0 or min(self.max_transactions, self.max_outpoints, self.max_requests) < 1:
            raise TraceError("Limits must be positive; max_hops may be zero")
        if not math.isfinite(self.max_seconds) or self.max_seconds <= 0:
            raise TraceError("max_seconds must be positive")


class Budget:
    def __init__(self, limits):
        self.limits = limits
        self.started = time.monotonic()
        self.requests = 0
        self._lock = threading.RLock()

    def check(self):
        if time.monotonic() - self.started >= self.limits.max_seconds:
            raise StopRun("time_limit")

    def request(self):
        with self._lock:
            self.check()
            if self.requests >= self.limits.max_requests:
                raise StopRun("request_limit")
            self.requests += 1

    def timeout(self):
        self.check()
        return max(.01, min(20., self.limits.max_seconds - (time.monotonic() - self.started)))

    def pause(self, seconds):
        if seconds > self.limits.max_seconds - (time.monotonic() - self.started):
            raise StopRun("time_limit")
        time.sleep(max(0., seconds))


class Esplora:
    def __init__(self, store, run_id, limits, base=ENTERPRISE, auth="blockstream",
                 fixture=None, tx_cache_seconds=86400, min_interval=None, transport=http,
                 workers=8, advertised_rps=None):
        self.store, self.run_id = store, run_id
        self.base = base.rstrip("/")
        self.budget = Budget(limits)
        self.transport = transport
        self.auth = auth
        self.tx_cache_seconds = tx_cache_seconds
        configured_rps = advertised_rps
        if configured_rps is None:
            configured_rps = os.getenv("LIQUID_BLOCKSTREAM_API_RPS") or None
        floor = default_min_interval(self.base, configured_rps)
        self.advertised_rps = float(configured_rps) if configured_rps is not None else None
        self.effective_rps = 1. / floor
        self.rate_limit_source = "advertised" if self.advertised_rps is not None else "conservative_default"
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
            if parsed.hostname in ("enterprise.blockstream.info", "blockstream.info") and parsed.path not in ("/liquid/api", "/liquidtestnet/api"):
                raise TraceError("Use a Liquid or Liquid testnet API path, not the Bitcoin API")
        if not math.isfinite(self.min_interval) or not math.isfinite(tx_cache_seconds) or self.min_interval < 0 or tx_cache_seconds < 0:
            raise TraceError("Interval and cache duration cannot be negative")
        self.min_interval = 0. if self.fixture is not None else max(floor, self.min_interval)
        if self.fixture is None:
            self.effective_rps = 1. / self.min_interval
        if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 8:
            raise TraceError("Explorer workers must be an integer from 1 to 8")
        self._transport_slots = threading.BoundedSemaphore(workers)

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
        self._cancelled.set()
        with self._gate:
            self._gate.notify_all()
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        interrupted = self._drain(results)
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
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

    def _admit(self, kind=None, endpoint=None):
        # Reserve starts under one shared gate. Increasing workers never
        # multiplies the configured request rate or the run's hard budget.
        with self._gate:
            while True:
                self.budget.check()
                if self._cancelled.is_set():
                    raise StopRun("interrupted")
                if self._stop_reason is not None:
                    raise StopRun(self._stop_reason)
                current = time.monotonic()
                delay = max(self.last_call + self.min_interval, self._cooldown_until) - current
                if delay <= 0:
                    break
                remaining = self.budget.limits.max_seconds - (current - self.budget.started)
                if delay >= remaining:
                    raise StopRun("time_limit")
                self._gate.wait(min(delay, .25))
            self.budget.request()
            if kind is not None:
                self.store.attempt(self.run_id, kind, endpoint, "started")
            timeout = self.budget.timeout()
            self.last_call = time.monotonic()
            return timeout

    def _cooldown(self, seconds):
        with self._gate:
            if not math.isfinite(seconds) or seconds > 30:
                self._stop_reason = "server_retry_later"
                self._gate.notify_all()
                raise StopRun(self._stop_reason)
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + max(0., seconds))
            self._gate.notify_all()

    @staticmethod
    def _retry_delay(headers, attempt=0):
        retry = next((v for k, v in headers.items() if k.lower() == "retry-after"), "")
        try:
            delay = float(retry)
        except (ValueError, TypeError):
            try:
                delay = parsedate_to_datetime(retry).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                delay = 0.
        return max(2 ** attempt, delay) if math.isfinite(delay) else float("inf")

    def call(self, method, url, kind, endpoint, headers=None, body=None):
        with self._transport_slots:
            return self._call(method, url, kind, endpoint, headers, body)

    def _call(self, method, url, kind, endpoint, headers=None, body=None):
        timeout = self._admit(kind, endpoint)
        try:
            result = self.transport(method, url, headers, body, timeout)
        except TraceError:
            self.store.attempt(self.run_id, kind, endpoint, "network_error")
            raise
        if result[0] == 429:
            # Close the shared gate as soon as the response arrives. The caller
            # still archives the response before propagating a long cooldown.
            try:
                self._cooldown(self._retry_delay(result[1]))
            except StopRun:
                pass
        self.store.attempt(self.run_id, kind, endpoint, result[0])
        return result

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
        status, _, raw = self.call("POST", TOKEN_URL, "oauth", "/token",
                                  {"Content-Type": "application/x-www-form-urlencoded"}, payload)
        if status != 200:
            raise TraceError("Blockstream authentication failed (HTTP " + str(status) + ")")
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

    def get(self, endpoint):
        """Fetch an endpoint once per run, coalescing success and failure alike."""
        with self._results_lock:
            if self._closed:
                raise TraceError("Explorer client is closed")
            self.budget.check()
            future = self._results.get(endpoint)
            owner = future is None
            if owner:
                future = self._results[endpoint] = Future()
        if owner:
            try:
                future.set_result(self._get(endpoint))
            except BaseException as error:
                future.set_exception(error)
        return future.result()

    def prefetch(self, endpoints):
        """Return ordered unique endpoints mapped to results or TraceError.

        At most `workers` jobs are submitted at once. The caller receives all
        results only after started requests have saved their evidence, including
        when one request reaches a hard run limit.
        """
        endpoints = list(dict.fromkeys(endpoints))
        if not endpoints:
            return {}
        with self._results_lock:
            if self._closed:
                raise TraceError("Explorer client is closed")
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="esplora")
            pool = self._pool
        pending, results = {}, {}
        remaining = iter(endpoints)
        stop = None

        def submit():
            for endpoint in remaining:
                pending[pool.submit(self.get, endpoint)] = endpoint
                if len(pending) >= self.workers:
                    break

        try:
            submit()
            while pending:
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    endpoint = pending.pop(future)
                    try:
                        results[endpoint] = future.result()
                    except TraceError as error:
                        results[endpoint] = error
                        if isinstance(error, StopRun):
                            stop = error
                if stop is None:
                    submit()
            for endpoint in remaining:
                results[endpoint] = StopRun(str(stop))
        except BaseException:
            # Interruptions must not let a response race the run snapshot or
            # Store.close(). close() also catches a job interrupted between
            # pool.submit() and recording its future in the local pending map.
            self.close()
            raise
        return {endpoint: results[endpoint] for endpoint in endpoints}

    def _get(self, endpoint):
        self.budget.check()
        # Only transaction bodies cross run boundaries. Spend status is refreshed each run.
        ttl = self.tx_cache_seconds if endpoint.count("/") == 2 and endpoint.startswith("/tx/") else 0
        cached = self.store.cached(self.base, endpoint, self.run_id, ttl)
        if cached:
            data, oid = cached
            # Unconfirmed transactions need fresh confirmation status in a new run.
            if not ttl or (isinstance(data, dict) and isinstance(data.get("status"), dict) and data["status"].get("confirmed")) or self.fixture is not None:
                self._remember(oid)
                return data, oid
        if self.fixture is not None:
            self._admit()
            if endpoint not in self.fixture:
                raise TraceError("Synthetic fixture has no response for " + endpoint)
            raw = canonical(self.fixture[endpoint])
            oid = self.store.observe(self.run_id, self.base, endpoint, raw)
            self._remember(oid)
            return json.loads(raw), oid
        auth_retried = False
        for attempt in range(4):
            from . import __version__
            headers = {"Accept": "application/json", "User-Agent": "liquid-utxo-tracer/" + __version__}
            if self.auth == "blockstream":
                with self._token_lock:
                    headers["Authorization"] = "Bearer " + self.bearer()
                    token_generation = self._token_generation
            status, response_headers, raw = self.call("GET", self.base + endpoint, "esplora", endpoint, headers)
            oid = self.store.observe(self.run_id, self.base, endpoint, raw, status)
            self._remember(oid)
            if status == 200:
                try:
                    return json.loads(raw), oid
                except (ValueError, UnicodeDecodeError):
                    raise TraceError("Explorer returned invalid JSON for " + endpoint) from None
            if status == 401 and self.auth == "blockstream" and not auth_retried:
                with self._token_lock:
                    if token_generation == self._token_generation:
                        self.token = None
                auth_retried = True
                continue
            if status == 429 or status in (500, 502, 503, 504):
                delay = self._retry_delay(response_headers, attempt)
                if status == 429:
                    # The service's cooldown applies to every worker, including
                    # unrelated endpoints that have not started their request.
                    self._cooldown(delay)
                if attempt == 3:
                    break
                if not math.isfinite(delay) or delay > 30:
                    raise StopRun("server_retry_later")
                if status != 429:
                    self.budget.pause(delay)
                continue
            raise TraceError("Explorer HTTP " + str(status) + " for " + endpoint)
        raise TraceError("Explorer retries exhausted for " + endpoint)
