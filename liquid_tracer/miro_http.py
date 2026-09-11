"""Reusable HTTPS connections for a single Miro synchronization.

Each worker owns its connection. Requests are never replayed here: the caller
decides which responses can be retried and journals writes before sending them.
"""

import ssl
import threading
import urllib.parse
import urllib.request
from http.client import HTTPException, HTTPSConnection

from .api import MAX_BODY, http
from .common import TraceError


class MiroHTTP:
    """The ``api.http`` callable contract with one keep-alive connection per thread.

    Close the transport after its workers have finished. Configured HTTPS
    proxies retain urllib's behavior instead of being silently bypassed.
    """

    def __init__(self):
        self._context = ssl.create_default_context()
        self._local = threading.local()
        self._lock = threading.Lock()
        self._connections = set()
        self._closed = False
        proxies = urllib.request.getproxies()
        self._proxy = bool(proxies.get("https")) and not urllib.request.proxy_bypass("api.miro.com")

    def __enter__(self):
        with self._lock:
            if self._closed:
                raise TraceError("Miro HTTP transport is closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        with self._lock:
            self._closed = True
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            self._close(connection)

    @staticmethod
    def _close(connection):
        try:
            connection.close()
        except OSError:
            pass

    def _discard(self, connection):
        if connection is None:
            return
        with self._lock:
            self._connections.discard(connection)
        if getattr(self._local, "connection", None) is connection:
            self._local.connection = None
        self._close(connection)

    def _connection(self, timeout):
        with self._lock:
            if self._closed:
                raise TraceError("Miro HTTP transport is closed")
            connection = getattr(self._local, "connection", None)
            if connection is None:
                connection = HTTPSConnection("api.miro.com", timeout=timeout, context=self._context)
                self._connections.add(connection)
                self._local.connection = connection
        return connection

    @staticmethod
    def _target(url, headers):
        try:
            if not isinstance(url, str) or any(ord(character) < 32 or ord(character) == 127 for character in url):
                raise ValueError
            parsed = urllib.parse.urlsplit(url)
            if (parsed.scheme != "https" or parsed.hostname != "api.miro.com"
                    or parsed.username is not None or parsed.password is not None
                    or parsed.port not in (None, 443) or parsed.fragment):
                raise ValueError
            if any(str(key).lower() == "host" and str(value).lower() not in ("api.miro.com", "api.miro.com:443")
                   for key, value in (headers or {}).items()):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise TraceError("Miro requests require HTTPS to api.miro.com without credentials or a fragment") from None
        return urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))

    def __call__(self, method, url, headers=None, body=None, timeout=20):
        target = self._target(url, headers)
        with self._lock:
            if self._closed:
                raise TraceError("Miro HTTP transport is closed")
        if self._proxy:
            status, response_headers, raw = http(method, url, headers, body, timeout)
            if 300 <= status < 400:
                raise TraceError("Unexpected Miro HTTP redirect")
            if len(raw) > MAX_BODY:
                raise TraceError("API response exceeds 32 MiB")
            return status, response_headers, raw

        connection = None
        try:
            connection = self._connection(timeout)
            connection.timeout = timeout
            if connection.sock is not None:
                connection.sock.settimeout(timeout)
            connection.request(method, target, body=body, headers=headers or {})
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise TraceError("Unexpected Miro HTTP redirect")
            raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise TraceError("API response exceeds 32 MiB")
            result = response.status, dict(response.getheaders()), raw
            if response.will_close:
                self._discard(connection)
            return result
        except TraceError:
            self._discard(connection)
            raise
        except (HTTPException, OSError, ValueError, TypeError) as error:
            self._discard(connection)
            # Never include request headers, URLs, bodies or server exception text.
            raise TraceError("Network request failed: " + type(error).__name__) from None
