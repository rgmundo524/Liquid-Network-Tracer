"""Verified HTTPS keep-alive connections owned by one explorer client.

Connections belong to a worker and an origin, so explorer bearer headers and
OAuth form bodies never share a connection. Configured HTTPS proxies use the
existing urllib path. This transport never replays requests: the API owns the
request budget, pacing, retries and evidence archive.
"""

import errno
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException, HTTPSConnection, IncompleteRead, RemoteDisconnected

from .common import TraceError

MAX_BODY = 32 * 1024 * 1024


class TransientExplorerConnection(TraceError):
    """A safe read/token request failed transiently; API admission owns retries."""


class StaleExplorerConnection(TransientExplorerConnection):
    """A reused GET connection disconnected; retry only through API admission."""


def network_failure(method, error, *, reused=False, token_request=False):
    """Classify known transient transport failures without exposing their text.

    urllib wraps socket/TLS failures in URLError. Retry only safe reads or an
    explicitly identified client-credentials token request, and only specific
    temporary failures. Certificate, protocol, URL and JSON validation errors
    remain fatal. A new connection can disconnect just like a reused one.
    """
    while isinstance(error, urllib.error.URLError) and isinstance(error.reason, BaseException):
        error = error.reason
    transient = isinstance(error, (RemoteDisconnected, IncompleteRead, TimeoutError,
                                   ConnectionError, ssl.SSLEOFError))
    if isinstance(error, socket.gaierror):
        transient = error.errno == socket.EAI_AGAIN
    elif isinstance(error, ssl.SSLError):
        transient = isinstance(error, ssl.SSLEOFError)
    elif isinstance(error, OSError):
        transient = transient or error.errno in {
            errno.ECONNRESET, errno.ECONNABORTED, errno.ECONNREFUSED, errno.EPIPE,
            errno.ETIMEDOUT, errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH,
        }
    exception = TraceError
    if (method == "GET" or (method == "POST" and token_request)) and transient:
        exception = StaleExplorerConnection if reused and method == "GET" else TransientExplorerConnection
    return exception("Network request failed: " + type(error).__name__)


class ExplorerHTTP:
    def __init__(self, base, token_url, fallback):
        self._origin = self._parsed(base)[0]
        self._token_url = token_url
        self._token_origin = self._parsed(token_url)[0]
        self._fallback = fallback
        self._context = ssl.create_default_context()
        self._local = threading.local()
        self._lock = threading.Lock()
        self._connections = set()
        self._closed = False
        proxies = urllib.request.getproxies()
        self._proxy_origins = {
            origin for origin in (self._origin, self._token_origin)
            if proxies.get("https") and not urllib.request.proxy_bypass(self._authority(origin))
        }

    @staticmethod
    def _authority(origin):
        host, port = origin
        host = "[" + host + "]" if ":" in host else host
        return host if port == 443 else host + ":" + str(port)

    @staticmethod
    def _parsed(url):
        try:
            if not isinstance(url, str) or any(ord(character) < 32 or ord(character) == 127 for character in url):
                raise ValueError
            parsed = urllib.parse.urlsplit(url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.fragment):
                raise ValueError
            port = 443 if parsed.port is None else parsed.port
            if port < 1:
                raise ValueError
            return (parsed.hostname.lower(), port), parsed
        except (ValueError, TypeError, AttributeError):
            raise TraceError("Explorer requests require a configured HTTPS origin without credentials or a fragment") from None

    def _target(self, method, url, headers):
        origin, parsed = self._parsed(url)
        if origin != self._origin and not (url == self._token_url and method == "POST"):
            raise TraceError("Explorer requests require the configured API origin or exact OAuth endpoint")
        allowed_hosts = {self._authority(origin)}
        if origin[1] == 443:
            allowed_hosts.add(self._authority(origin) + ":443")
        if any(str(key).lower() == "host" and str(value).lower() not in allowed_hosts
               for key, value in (headers or {}).items()):
            raise TraceError("Explorer request Host must match its HTTPS origin")
        return origin, urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))

    def __enter__(self):
        with self._lock:
            self._check_open()
        return self

    def __exit__(self, *_):
        self.close()

    def _check_open(self):
        if self._closed:
            raise TraceError("Explorer HTTP transport is closed")

    @staticmethod
    def _close(connection):
        try:
            connection.close()
        except OSError:
            pass

    def close(self):
        """Close only after callers have drained the owning client's workers."""
        with self._lock:
            self._closed = True
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            self._close(connection)

    def _discard(self, origin, connection):
        if connection is None:
            return
        with self._lock:
            self._connections.discard(connection)
        connections = getattr(self._local, "connections", {})
        if connections.get(origin) is connection:
            del connections[origin]
        self._close(connection)

    def _connection(self, origin, timeout):
        with self._lock:
            self._check_open()
            connections = getattr(self._local, "connections", None)
            if connections is None:
                connections = self._local.connections = {}
            connection = connections.get(origin)
            reused = connection is not None
            if connection is None:
                connection = HTTPSConnection(origin[0], port=origin[1], timeout=timeout, context=self._context)
                connections[origin] = connection
                self._connections.add(connection)
        return connection, reused

    def __call__(self, method, url, headers=None, body=None, timeout=20):
        origin, target = self._target(method, url, headers)
        with self._lock:
            self._check_open()
        if origin in self._proxy_origins:
            try:
                result = self._fallback(method, url, headers, body, timeout)
            except (HTTPException, OSError, ValueError, TypeError) as error:
                raise network_failure(method, error, token_request=url == self._token_url) from None
            if 300 <= result[0] < 400:
                raise TraceError("Unexpected HTTP redirect; verify the configured API endpoint")
            if len(result[2]) > MAX_BODY:
                raise TraceError("API response exceeds 32 MiB")
            return result

        connection, reused = None, False
        try:
            connection, reused = self._connection(origin, timeout)
            connection.timeout = timeout
            if connection.sock is not None:
                connection.sock.settimeout(timeout)
            connection.request(method, target, body=body, headers=headers or {})
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise TraceError("Unexpected HTTP redirect; verify the configured API endpoint")
            raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise TraceError("API response exceeds 32 MiB")
            result = response.status, dict(response.getheaders()), raw
            if response.will_close:
                self._discard(origin, connection)
            return result
        except TraceError:
            self._discard(origin, connection)
            raise
        except (HTTPException, OSError, ValueError, TypeError) as error:
            self._discard(origin, connection)
            raise network_failure(method, error, reused=reused, token_request=url == self._token_url) from None
