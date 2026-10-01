import errno
import socket
import ssl
import io
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from http.client import IncompleteRead, RemoteDisconnected
from unittest.mock import Mock, patch

from liquid_tracer.api import ENTERPRISE, TOKEN_URL, http
from liquid_tracer.common import TraceError
from liquid_tracer.explorer_http import ExplorerHTTP, StaleExplorerConnection, TransientExplorerConnection


class Response:
    def __init__(self, status=200, raw=b'{}', will_close=False):
        self.status, self.raw, self.will_close = status, raw, will_close
        self.read_sizes = []

    def read(self, size):
        self.read_sizes.append(size)
        return self.raw[:size]

    def getheaders(self):
        return [('Content-Type', 'application/json')]


class Connection:
    def __init__(self, host, port, timeout, context):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.sock = Mock()
        self.requests = []
        self.response = Response()
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


class ExplorerHTTPTests(unittest.TestCase):
    def setUp(self):
        self.connections = []
        proxies = patch('liquid_tracer.explorer_http.urllib.request.getproxies', return_value={})
        proxies.start()
        self.addCleanup(proxies.stop)
        factory = patch('liquid_tracer.explorer_http.HTTPSConnection', side_effect=self.connection)
        self.factory = factory.start()
        self.addCleanup(factory.stop)

    def connection(self, *args, **kwargs):
        result = Connection(*args, **kwargs)
        self.connections.append(result)
        return result

    def client(self, base=ENTERPRISE, fallback=http):
        return ExplorerHTTP(base, TOKEN_URL, fallback)

    def test_verified_tls_reuse_updates_timeout_and_isolates_oauth_origin(self):
        with self.client() as transport:
            transport('GET', ENTERPRISE + '/tx/one', {'Authorization': 'Bearer synthetic'}, timeout=3)
            transport('POST', TOKEN_URL, {'Content-Type': 'application/x-www-form-urlencoded'}, b'client=synthetic', 5)
            transport('GET', ENTERPRISE + '/tx/two?test=1', timeout=7)
            self.assertEqual(len(self.connections), 2)
            api, oauth = self.connections
            self.assertEqual(api.host, 'enterprise.blockstream.info')
            self.assertEqual(oauth.host, 'login.blockstream.com')
            self.assertEqual(api.port, 443)
            self.assertEqual(api.timeout, 7)
            api.sock.settimeout.assert_called_with(7)
            self.assertEqual(api.context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(api.context.check_hostname)
            self.assertEqual(api.requests[1][2], '/liquid/api/tx/two?test=1')
            self.assertEqual(oauth.requests[0][3], b'client=synthetic')
            self.assertEqual(len(oauth.requests), 1)
        self.assertTrue(all(connection.closed for connection in self.connections))

    def test_each_worker_reuses_only_its_own_connection(self):
        barrier = threading.Barrier(2)
        with self.client() as transport:
            def work(_):
                barrier.wait(timeout=3)
                transport('GET', ENTERPRISE + '/tx/one')
                transport('GET', ENTERPRISE + '/tx/two')
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(work, range(2)))
        self.assertEqual(len(self.connections), 2)
        owners = []
        for connection in self.connections:
            self.assertEqual(len(connection.requests), 2)
            self.assertEqual(len({request[0] for request in connection.requests}), 1)
            owners.append(connection.requests[0][0])
        self.assertEqual(len(set(owners)), 2)

    def test_stale_get_is_marked_but_never_replayed_by_transport(self):
        with self.client() as transport:
            transport('GET', ENTERPRISE + '/tx/one')
            connection = self.connections[0]
            connection.failure = RemoteDisconnected('never expose synthetic-token')
            with self.assertRaisesRegex(StaleExplorerConnection, '^Network request failed: RemoteDisconnected$'):
                transport('GET', ENTERPRISE + '/tx/two')
            self.assertEqual(len(connection.requests), 2)
            self.assertEqual(len(self.connections), 1)
            self.assertTrue(connection.closed)
            transport('GET', ENTERPRISE + '/tx/three')
            self.assertEqual(len(self.connections), 2)

    def test_failed_exact_oauth_post_is_marked_but_never_replayed_by_transport(self):
        with self.client() as transport:
            transport('POST', TOKEN_URL)
            connection = self.connections[0]
            connection.failure = RemoteDisconnected('never expose synthetic-secret')
            with self.assertRaises(TraceError) as caught:
                transport('POST', TOKEN_URL)
            self.assertIsInstance(caught.exception, TransientExplorerConnection)
            self.assertNotIsInstance(caught.exception, StaleExplorerConnection)
            self.assertEqual(str(caught.exception), 'Network request failed: RemoteDisconnected')
            self.assertEqual(len(connection.requests), 2)
            self.assertEqual(len(self.connections), 1)
            self.assertTrue(connection.closed)

    def test_new_get_connection_failure_is_retryable_without_transport_replay(self):
        connection = Connection('enterprise.blockstream.info', 443, 3, ssl.create_default_context())
        connection.failure = RemoteDisconnected('sensitive failure')
        with self.client() as transport, patch('liquid_tracer.explorer_http.HTTPSConnection', return_value=connection):
            with self.assertRaises(TraceError) as caught:
                transport('GET', ENTERPRISE + '/tx/one')
            self.assertIsInstance(caught.exception, TransientExplorerConnection)
            self.assertNotIsInstance(caught.exception, StaleExplorerConnection)
            self.assertEqual(len(connection.requests), 1)
            self.assertTrue(connection.closed)

    def test_transient_get_failures_are_classified_on_fresh_connections(self):
        failures = [TimeoutError('private'), ConnectionResetError('private'),
                    BrokenPipeError('private'), ConnectionAbortedError('private'),
                    IncompleteRead(b'private', 99), ssl.SSLEOFError('private'),
                    socket.gaierror(socket.EAI_AGAIN, 'private'),
                    OSError(errno.ENETUNREACH, 'private')]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                connection = Connection('enterprise.blockstream.info', 443, 3, ssl.create_default_context())
                connection.failure = failure
                with self.client() as transport, patch('liquid_tracer.explorer_http.HTTPSConnection', return_value=connection):
                    with self.assertRaises(TransientExplorerConnection) as caught:
                        transport('GET', ENTERPRISE + '/tx/one')
                self.assertEqual(str(caught.exception), 'Network request failed: ' + type(failure).__name__)
                self.assertEqual(len(connection.requests), 1)
                self.assertTrue(connection.closed)

    def test_certificate_protocol_and_permanent_dns_errors_are_not_retryable(self):
        failures = [ssl.SSLCertVerificationError('private'), ssl.SSLError('private'),
                    socket.gaierror(socket.EAI_NONAME, 'private'),
                    PermissionError('private'), ValueError('private')]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                connection = Connection('enterprise.blockstream.info', 443, 3, ssl.create_default_context())
                connection.failure = failure
                with self.client() as transport, patch('liquid_tracer.explorer_http.HTTPSConnection', return_value=connection):
                    with self.assertRaises(TraceError) as caught:
                        transport('GET', ENTERPRISE + '/tx/one')
                self.assertNotIsInstance(caught.exception, TransientExplorerConnection)
                self.assertEqual(len(connection.requests), 1)
                self.assertNotIn('private', str(caught.exception))

    def test_proxy_urllib_failures_keep_retry_classification_and_redaction(self):
        for failure in (RemoteDisconnected('private'), urllib.error.URLError(TimeoutError('private')),
                        urllib.error.URLError(ssl.SSLCertVerificationError('private'))):
            opener = Mock()
            opener.open.side_effect = failure
            with self.subTest(failure=repr(type(failure))), \
                 patch('liquid_tracer.explorer_http.urllib.request.getproxies', return_value={'https': 'http://proxy.example:3128'}), \
                 patch('liquid_tracer.explorer_http.urllib.request.proxy_bypass', return_value=False), \
                 patch('liquid_tracer.api.urllib.request.build_opener', return_value=opener):
                with self.client() as transport, self.assertRaises(TraceError) as caught:
                    transport('GET', ENTERPRISE + '/tx/one')
                retryable = not (isinstance(failure, urllib.error.URLError)
                                 and isinstance(failure.reason, ssl.SSLCertVerificationError))
                self.assertEqual(isinstance(caught.exception, TransientExplorerConnection), retryable)
                self.assertNotIn('private', str(caught.exception))
                self.assertEqual(opener.open.call_count, 1)
        self.factory.assert_not_called()

    def test_response_limits_include_error_bodies_and_redirects_are_not_followed(self):
        with self.client() as transport, patch('liquid_tracer.explorer_http.MAX_BODY', 16):
            transport('GET', ENTERPRISE + '/tx/one')
            first = self.connections[0]
            first.response = Response(503, b'x' * 17)
            with self.assertRaisesRegex(TraceError, 'exceeds 32 MiB'):
                transport('GET', ENTERPRISE + '/tx/two')
            self.assertEqual(first.response.read_sizes, [17])
            self.assertTrue(first.closed)
            transport('GET', ENTERPRISE + '/tx/three')
            second = self.connections[1]
            second.response = Response(302)
            with self.assertRaisesRegex(TraceError, 'Unexpected HTTP redirect'):
                transport('GET', ENTERPRISE + '/tx/four')
            self.assertEqual(second.response.read_sizes, [])
            self.assertTrue(second.closed)
            self.assertEqual(len(self.connections), 2)

    def test_invalid_origins_and_nonexact_oauth_targets_fail_before_request(self):
        urls = ['http://enterprise.blockstream.info/liquid/api',
                'https://evil.example/liquid/api', ENTERPRISE + '#fragment',
                'https://secret@enterprise.blockstream.info/liquid/api',
                'https://enterprise.blockstream.info:444/liquid/api',
                'https://enterprise.blockstream.info:0/liquid/api',
                'https://enterprise.blockstream.info:bad/liquid/api',
                '\n' + ENTERPRISE, ENTERPRISE + '/\r\nrequest', TOKEN_URL + '?different=1',
                TOKEN_URL + '/other', 'https://login.blockstream.com/other']
        with self.client() as transport:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(TraceError):
                    transport('POST', url, {'Authorization': 'Bearer synthetic'})
            with self.assertRaises(TraceError):
                transport('GET', TOKEN_URL)
            with self.assertRaises(TraceError):
                transport('GET', ENTERPRISE, {'Host': 'evil.example'})
        self.factory.assert_not_called()

    def test_server_close_discards_connection_and_explicit_port_is_preserved(self):
        base = 'https://explorer.example:8443/api'
        with self.client(base) as transport:
            transport('GET', base + '/tx/one')
            first = self.connections[0]
            self.assertEqual(first.port, 8443)
            first.response = Response(will_close=True)
            transport('GET', base + '/tx/two')
            self.assertTrue(first.closed)
            transport('GET', base + '/tx/three')
            self.assertEqual(len(self.connections), 2)

    def test_proxy_choice_is_per_origin_and_respects_bypass(self):
        fallback = Mock(return_value=(200, {}, b'{}'))
        with patch('liquid_tracer.explorer_http.urllib.request.getproxies', return_value={'https': 'http://proxy.example:3128'}), \
             patch('liquid_tracer.explorer_http.urllib.request.proxy_bypass', side_effect=lambda host: host == 'enterprise.blockstream.info'):
            with self.client(fallback=fallback) as transport:
                transport('GET', ENTERPRISE + '/tx/one')
                transport('GET', ENTERPRISE + '/tx/two')
                headers = {'Content-Type': 'application/x-www-form-urlencoded'}
                transport('POST', TOKEN_URL, headers, b'client=synthetic', 9)
            fallback.assert_called_once_with('POST', TOKEN_URL, headers, b'client=synthetic', 9)
            self.assertEqual(len(self.connections), 1)

    def test_proxy_redirect_and_oversized_body_rejected(self):
        fallback = Mock(return_value=(302, {'Location': 'https://evil.example'}, b''))
        with patch('liquid_tracer.explorer_http.urllib.request.getproxies', return_value={'https': 'http://proxy.example:3128'}), \
             patch('liquid_tracer.explorer_http.urllib.request.proxy_bypass', return_value=False):
            with self.client(fallback=fallback) as transport:
                with self.assertRaisesRegex(TraceError, 'Unexpected HTTP redirect'):
                    transport('GET', ENTERPRISE)
                fallback.return_value = (503, {}, b'12345')
                with patch('liquid_tracer.explorer_http.MAX_BODY', 4), self.assertRaisesRegex(TraceError, 'exceeds 32 MiB'):
                    transport('GET', ENTERPRISE)
        self.factory.assert_not_called()

    def test_close_is_idempotent_and_never_reopens(self):
        transport = self.client()
        transport('GET', ENTERPRISE)
        transport.close()
        transport.close()
        with self.assertRaisesRegex(TraceError, 'transport is closed'):
            transport('GET', ENTERPRISE)
        with self.assertRaisesRegex(TraceError, 'transport is closed'):
            transport.__enter__()
        self.assertEqual(len(self.connections), 1)

    def test_urllib_fallback_bounds_error_reads_and_redacts_read_failures(self):
        for body in (io.BytesIO(b'12345'), Mock(closed=False)):
            error = urllib.error.HTTPError(ENTERPRISE + '?private', 503, 'unavailable', {}, body)
            opener = Mock()
            opener.open.side_effect = error
            if isinstance(body, io.BytesIO):
                expected = 'API response exceeds 32 MiB'
            else:
                body.read.side_effect = RemoteDisconnected('sensitive proxy response')
                expected = 'Network request failed: RemoteDisconnected'
            with self.subTest(expected=expected), \
                 patch('liquid_tracer.api.urllib.request.build_opener', return_value=opener), \
                 patch('liquid_tracer.api.MAX_BODY', 4):
                with self.assertRaises(TraceError) as caught:
                    http('GET', ENTERPRISE)
                self.assertEqual(str(caught.exception), expected)


if __name__ == '__main__':
    unittest.main()
