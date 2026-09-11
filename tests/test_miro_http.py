import ssl
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.client import RemoteDisconnected
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.miro_http import MiroHTTP


URL = "https://api.miro.com/v2/boards/example/shapes"


class FakeResponse:
    def __init__(self, status=200, body=b'{}', will_close=False):
        self.status = status
        self.body = body
        self.will_close = will_close
        self.read_sizes = []

    def read(self, size):
        self.read_sizes.append(size)
        return self.body[:size]

    def getheaders(self):
        return [("Content-Type", "application/json")]


class FakeConnection:
    def __init__(self, host, timeout, context):
        self.host, self.timeout, self.context = host, timeout, context
        self.sock = None
        self.requests = []
        self.response = FakeResponse()
        self.failure = None
        self.closed = False

    def request(self, method, target, body, headers):
        self.requests.append((threading.get_ident(), method, target, body, headers))

    def getresponse(self):
        if self.failure:
            raise self.failure
        return self.response

    def close(self):
        self.closed = True


class MiroHTTPTests(unittest.TestCase):
    def setUp(self):
        self.connections = []
        self.proxy_patch = patch("liquid_tracer.miro_http.urllib.request.getproxies", return_value={})
        self.proxy_patch.start()
        self.addCleanup(self.proxy_patch.stop)
        self.factory_patch = patch("liquid_tracer.miro_http.HTTPSConnection", side_effect=self.new_connection)
        self.factory = self.factory_patch.start()
        self.addCleanup(self.factory_patch.stop)

    def new_connection(self, *args, **kwargs):
        connection = FakeConnection(*args, **kwargs)
        self.connections.append(connection)
        return connection

    def test_reuses_connection_and_verified_tls_with_full_request_contract(self):
        with MiroHTTP() as transport:
            first = transport("GET", URL + "?limit=20", {"Authorization": "Bearer synthetic"}, timeout=5)
            second = transport("PATCH", URL + "/item", {"Content-Type": "application/json"}, b'{}', 11)
            self.assertEqual(first, second)
            self.assertEqual(first, (200, {"Content-Type": "application/json"}, b'{}'))
            self.assertEqual(len(self.connections), 1)
            connection = self.connections[0]
            self.assertEqual(connection.host, "api.miro.com")
            self.assertEqual(connection.timeout, 11)
            self.assertEqual(connection.context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(connection.context.check_hostname)
            self.assertEqual(connection.requests[0][2], "/v2/boards/example/shapes?limit=20")
            self.assertEqual(connection.requests[1][1:], (
                "PATCH", "/v2/boards/example/shapes/item", b'{}', {"Content-Type": "application/json"}))
        self.assertTrue(connection.closed)

    def test_each_concurrent_worker_has_its_own_reused_connection(self):
        barrier = threading.Barrier(2)
        with MiroHTTP() as transport:
            def requests(_):
                barrier.wait(timeout=5)
                transport("GET", URL)
                transport("GET", URL)

            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(requests, range(2)))
            self.assertEqual(len(self.connections), 2)
            owners = []
            for connection in self.connections:
                self.assertEqual(len(connection.requests), 2)
                threads = {request[0] for request in connection.requests}
                self.assertEqual(len(threads), 1)
                owners.extend(threads)
            self.assertEqual(len(set(owners)), 2)
        self.assertTrue(all(connection.closed for connection in self.connections))

    def test_failed_post_is_not_replayed_and_connection_is_dropped(self):
        with MiroHTTP() as transport:
            transport("GET", URL)
            connection = self.connections[0]
            connection.failure = RemoteDisconnected("Bearer never-echo-this")
            with self.assertRaisesRegex(TraceError, "^Network request failed: RemoteDisconnected$"):
                transport("POST", URL, {"Authorization": "Bearer never-echo-this"}, b'{}')
            self.assertEqual(len(connection.requests), 2)
            self.assertTrue(connection.closed)
            transport("GET", URL)
            self.assertEqual(len(self.connections), 2)
            self.assertEqual(len(self.connections[1].requests), 1)

    def test_response_limit_applies_to_error_bodies_and_discards_connection(self):
        with MiroHTTP() as transport, patch("liquid_tracer.miro_http.MAX_BODY", 16):
            transport("GET", URL)
            connection = self.connections[0]
            connection.response = FakeResponse(status=500, body=b'x' * 17)
            with self.assertRaisesRegex(TraceError, "response exceeds"):
                transport("GET", URL)
            self.assertEqual(connection.response.read_sizes, [17])
            self.assertTrue(connection.closed)
            transport("GET", URL)
            self.assertEqual(len(self.connections), 2)

    def test_redirect_is_never_followed(self):
        with MiroHTTP() as transport:
            transport("GET", URL)
            connection = self.connections[0]
            connection.response = FakeResponse(status=302)
            with self.assertRaisesRegex(TraceError, "Unexpected Miro HTTP redirect"):
                transport("GET", URL, {"Authorization": "Bearer synthetic"})
            self.assertTrue(connection.closed)
            self.assertEqual(len(connection.requests), 2)
            self.assertEqual(connection.response.read_sizes, [])
            self.assertEqual(len(self.connections), 1)

    def test_rejects_other_origins_credentials_and_malformed_urls_before_request(self):
        urls = [
            "http://api.miro.com/v2/boards", "https://evil.example/v2/boards",
            "https://api.miro.com.evil.example/v2/boards", "https://api.miro.com:444/v2/boards",
            "https://secret@api.miro.com/v2/boards", "https://api.miro.com/v2/boards#secret",
            "https://api.miro.com:notaport/v2/boards", "https://[api.miro.com/v2/boards",
            "\nhttps://api.miro.com/v2/boards", "https://api.miro.com/v2/\r\nboards",
        ]
        with MiroHTTP() as transport:
            for url in urls:
                with self.subTest(url=url), self.assertRaisesRegex(TraceError, "require HTTPS to api.miro.com"):
                    transport("GET", url, {"Authorization": "Bearer synthetic"})
            with self.assertRaisesRegex(TraceError, "require HTTPS to api.miro.com"):
                transport("GET", URL, {"Host": "evil.example"})
        self.factory.assert_not_called()

    def test_server_connection_close_opens_a_fresh_connection_next_request(self):
        with MiroHTTP() as transport:
            transport("GET", URL)
            connection = self.connections[0]
            connection.response = FakeResponse(will_close=True)
            transport("GET", URL)
            self.assertTrue(connection.closed)
            transport("GET", URL)
            self.assertEqual(len(self.connections), 2)

    def test_https_proxy_uses_existing_transport_with_unchanged_arguments(self):
        with patch("liquid_tracer.miro_http.urllib.request.getproxies", return_value={"https": "http://proxy.example:3128"}), \
                patch("liquid_tracer.miro_http.urllib.request.proxy_bypass", return_value=False), \
                patch("liquid_tracer.miro_http.http", return_value=(200, {}, b'{}')) as fallback:
            with MiroHTTP() as transport:
                headers = {"Authorization": "Bearer synthetic"}
                self.assertEqual(transport("PATCH", URL, headers, b'{}', 7), (200, {}, b'{}'))
                fallback.assert_called_once_with("PATCH", URL, headers, b'{}', 7)
        self.factory.assert_not_called()

    def test_proxy_bypass_retains_direct_connection_reuse(self):
        with patch("liquid_tracer.miro_http.urllib.request.getproxies", return_value={"https": "http://proxy.example:3128"}), \
                patch("liquid_tracer.miro_http.urllib.request.proxy_bypass", return_value=True), \
                patch("liquid_tracer.miro_http.http") as fallback:
            with MiroHTTP() as transport:
                transport("GET", URL)
                transport("GET", URL)
            fallback.assert_not_called()
        self.assertEqual(len(self.connections), 1)

    def test_closed_transport_is_not_reopened_and_close_is_idempotent(self):
        transport = MiroHTTP()
        transport("GET", URL)
        transport.close()
        transport.close()
        with self.assertRaisesRegex(TraceError, "transport is closed"):
            transport("GET", URL)
        with self.assertRaisesRegex(TraceError, "transport is closed"):
            transport.__enter__()
        self.assertEqual(len(self.connections), 1)


if __name__ == "__main__":
    unittest.main()
