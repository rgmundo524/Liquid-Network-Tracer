import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .common import StopRun, TraceError, canonical, read_json

TOKEN_URL = "https://login.blockstream.com/realms/blockstream-public/protocol/openid-connect/token"
ENTERPRISE = "https://enterprise.blockstream.info/liquid/api"
MAX_BODY = 32 * 1024 * 1024


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

    def check(self):
        if time.monotonic() - self.started >= self.limits.max_seconds:
            raise StopRun("time_limit")

    def request(self):
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
                 fixture=None, tx_cache_seconds=86400, min_interval=.25, transport=http):
        self.store, self.run_id = store, run_id
        self.base = base.rstrip("/")
        self.budget = Budget(limits)
        self.transport = transport
        self.auth = auth
        self.tx_cache_seconds = tx_cache_seconds
        self.min_interval = min_interval
        self.last_call = 0.
        self.token = None
        self.token_until = 0.
        self.used = set()
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
        if not math.isfinite(min_interval) or not math.isfinite(tx_cache_seconds) or min_interval < 0 or tx_cache_seconds < 0:
            raise TraceError("Interval and cache duration cannot be negative")

    def call(self, method, url, kind, endpoint, headers=None, body=None):
        self.budget.pause(max(0., self.min_interval - (time.monotonic() - self.last_call)))
        self.budget.request()
        self.store.attempt(self.run_id, kind, endpoint, "started")
        self.last_call = time.monotonic()
        try:
            result = self.transport(method, url, headers, body, self.budget.timeout())
        except TraceError:
            self.store.attempt(self.run_id, kind, endpoint, "network_error")
            raise
        self.store.attempt(self.run_id, kind, endpoint, result[0])
        return result

    def bearer(self):
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
            self.token = data["access_token"]
            self.token_until = time.monotonic() + max(1., float(data.get("expires_in", 300)) - 30)
        except (ValueError, KeyError, TypeError):
            raise TraceError("Malformed authentication response") from None
        return self.token

    def get(self, endpoint):
        self.budget.check()
        # Only transaction bodies cross run boundaries. Spend status is refreshed each run.
        ttl = self.tx_cache_seconds if endpoint.count("/") == 2 and endpoint.startswith("/tx/") else 0
        cached = self.store.cached(self.base, endpoint, self.run_id, ttl)
        if cached:
            data, oid = cached
            # Unconfirmed transactions need fresh confirmation status in a new run.
            if not ttl or (isinstance(data, dict) and isinstance(data.get("status"), dict) and data["status"].get("confirmed")) or self.fixture is not None:
                self.used.add(oid)
                return data, oid
        if self.fixture is not None:
            self.budget.request()
            if endpoint not in self.fixture:
                raise TraceError("Synthetic fixture has no response for " + endpoint)
            raw = canonical(self.fixture[endpoint])
            oid = self.store.observe(self.run_id, self.base, endpoint, raw)
            self.used.add(oid)
            return json.loads(raw), oid
        for attempt in range(4):
            from . import __version__
            headers = {"Accept": "application/json", "User-Agent": "liquid-utxo-tracer/" + __version__}
            if self.auth == "blockstream":
                headers["Authorization"] = "Bearer " + self.bearer()
            status, response_headers, raw = self.call("GET", self.base + endpoint, "esplora", endpoint, headers)
            oid = self.store.observe(self.run_id, self.base, endpoint, raw, status)
            self.used.add(oid)
            if status == 200:
                try:
                    return json.loads(raw), oid
                except (ValueError, UnicodeDecodeError):
                    raise TraceError("Explorer returned invalid JSON for " + endpoint) from None
            if status == 401 and self.auth == "blockstream" and attempt == 0:
                self.token = None
                continue
            if status == 429 or status in (500, 502, 503, 504):
                if attempt == 3:
                    break
                retry = next((v for k, v in response_headers.items() if k.lower() == "retry-after"), "")
                try:
                    delay = max(2 ** attempt, float(retry))
                except ValueError:
                    delay = 2 ** attempt
                if delay > 30:
                    raise StopRun("server_retry_later")
                self.budget.pause(delay)
                continue
            raise TraceError("Explorer HTTP " + str(status) + " for " + endpoint)
        raise TraceError("Explorer retries exhausted for " + endpoint)
