import ssl
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from http.client import RemoteDisconnected
from unittest.mock import Mock, call, patch

from liquid_tracer.api import ENTERPRISE, TOKEN_URL, Esplora, Limits, http
from liquid_tracer.common import StopRun, TraceError
from liquid_tracer.explorer_http import ExplorerHTTP, TransientExplorerConnection, network_failure
from liquid_tracer.store import Store
from tests.test_explorer_http import Connection


class ExplorerNetworkRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'case')
        self.clients = []
        self.transports = []
        proxies = patch('liquid_tracer.explorer_http.urllib.request.getproxies', return_value={})
        proxies.start()
        self.addCleanup(proxies.stop)

    def tearDown(self):
        for client in self.clients:
            client.close()
        for transport in self.transports:
            transport.close()
        self.store.close()
        self.temp.cleanup()

    def client(self, transport, **kwargs):
        client = Esplora(self.store, kwargs.pop('run_id', 'run'), kwargs.pop('limits', Limits()),
                         auth='none', transport=transport, advertised_rps=1000000, **kwargs)
        self.clients.append(client)
        return client

    def transport(self):
        transport = ExplorerHTTP(ENTERPRISE, TOKEN_URL, http)
        self.transports.append(transport)
        return transport

    def statuses(self):
        return [row['status'] for row in self.store.db.execute('SELECT status FROM attempts ORDER BY id')]

    def test_fresh_disconnect_reopens_through_quota_and_archives_each_attempt(self):
        first = Connection('enterprise.blockstream.info', 443, 20, ssl.create_default_context())
        first.failure = RemoteDisconnected('sensitive remote text')
        replacement = Connection('enterprise.blockstream.info', 443, 20, ssl.create_default_context())
        quota = Mock()
        quota.reserve.return_value = SimpleNamespace(admitted=True, active_clients=1,
                                                     effective_rps=49., wait_seconds=0., reason='')
        client = self.client(self.transport(), limits=Limits(max_requests=2), shared_quota=quota)
        with patch('liquid_tracer.explorer_http.HTTPSConnection', side_effect=[first, replacement]) as factory, \
             patch.object(client._cancelled, 'wait', return_value=False) as backoff:
            result, _ = client.get('/tx/one')
        self.assertEqual(result, {})
        self.assertEqual(factory.call_count, 2)
        self.assertTrue(first.closed)
        self.assertEqual(client.budget.requests, 2)
        self.assertEqual(quota.reserve.call_count, 2)
        backoff.assert_called_once_with(1)
        self.assertEqual(self.statuses(), ['started', 'network_error', 'started', '200'])
        self.assertEqual(len(list(self.store.observations(client.used))), 1)

    def test_reused_disconnect_reopens_through_same_bounded_retry_path(self):
        first = Connection('enterprise.blockstream.info', 443, 20, ssl.create_default_context())
        replacement = Connection('enterprise.blockstream.info', 443, 20, ssl.create_default_context())
        client = self.client(self.transport())
        with patch('liquid_tracer.explorer_http.HTTPSConnection', side_effect=[first, replacement]), \
             patch.object(client._cancelled, 'wait', return_value=False):
            client.get('/tx/warm')
            first.failure = RemoteDisconnected('private')
            self.assertEqual(client.get('/tx/one')[0], {})
        self.assertEqual(client.budget.requests, 3)
        self.assertTrue(first.closed)
        self.assertEqual(self.statuses(), ['started', '200', 'started', 'network_error', 'started', '200'])

    def test_proxy_disconnect_and_timeout_retry_through_urllib_fallback(self):
        response = Mock()
        response.status, response.headers = 200, {}
        response.read.return_value = b'{}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.side_effect = [RemoteDisconnected('private'),
                                   urllib.error.URLError(TimeoutError('private')), response]
        with patch('liquid_tracer.explorer_http.urllib.request.getproxies', return_value={'https': 'http://proxy.example:3128'}), \
             patch('liquid_tracer.explorer_http.urllib.request.proxy_bypass', return_value=False), \
             patch('liquid_tracer.api.urllib.request.build_opener', return_value=opener):
            client = self.client(self.transport())
            with patch.object(client._cancelled, 'wait', return_value=False) as backoff:
                self.assertEqual(client.get('/tx/one')[0], {})
        self.assertEqual(client.budget.requests, 3)
        self.assertEqual(backoff.call_args_list, [call(1), call(2)])
        self.assertEqual(self.statuses(), ['started', 'network_error'] * 2 + ['started', '200'])

    def test_exhaustion_is_bounded_redacted_coalesced_and_has_no_partial_evidence(self):
        failure = network_failure('GET', ConnectionResetError('private token'))
        transport = Mock(side_effect=failure)
        client = self.client(transport)
        with patch.object(client._cancelled, 'wait', return_value=False) as backoff:
            for _ in range(2):
                with self.assertRaisesRegex(TransientExplorerConnection,
                                           'network retries exhausted after 4 attempts') as caught:
                    client.get('/tx/one')
                self.assertIn('ConnectionResetError', str(caught.exception))
                self.assertNotIn('private', str(caught.exception))
        self.assertEqual(transport.call_count, 4)
        self.assertEqual(client.budget.requests, 4)
        self.assertEqual(backoff.call_args_list, [call(1), call(2), call(4)])
        self.assertEqual(self.statuses(), ['started', 'network_error'] * 4)
        self.assertEqual(len(list(self.store.observations(client.used))), 0)

    def test_http_and_transport_failures_share_one_four_attempt_ceiling(self):
        failure = network_failure('GET', TimeoutError('private'))
        transport = Mock(side_effect=[failure, (503, {}, b'{}'), failure, (500, {}, b'{}')])
        client = self.client(transport)
        with patch.object(client._cancelled, 'wait', return_value=False), \
             self.assertRaisesRegex(TraceError, 'Explorer retries exhausted'):
            client.get('/tx/one')
        self.assertEqual(transport.call_count, 4)
        self.assertEqual(len(list(self.store.observations(client.used))), 2)

    def test_request_and_deadline_budgets_stop_before_retry_sleep(self):
        for limits, reason in [(Limits(max_requests=1), 'request_limit'),
                               (Limits(max_seconds=.5), 'time_limit')]:
            with self.subTest(reason=reason):
                transport = Mock(side_effect=network_failure('GET', RemoteDisconnected()))
                client = self.client(transport, limits=limits, run_id=reason)
                with patch.object(client._cancelled, 'wait', return_value=False) as backoff, \
                     self.assertRaisesRegex(StopRun, reason):
                    client.get('/tx/' + reason)
                self.assertEqual(transport.call_count, 1)
                self.assertEqual(client.budget.requests, 1)
                backoff.assert_not_called()

    def test_close_interrupts_retry_backoff_before_another_request(self):
        waiting = threading.Event()
        transport = Mock(side_effect=network_failure('GET', RemoteDisconnected()))
        client = self.client(transport)
        original = client._cancelled.wait

        def wait(seconds):
            waiting.set()
            return original(seconds)

        with patch.object(client._cancelled, 'wait', side_effect=wait):
            result = client.submit('/tx/one')
            self.assertTrue(waiting.wait(timeout=2))
            started = time.monotonic()
            client.close()
            self.assertLess(time.monotonic() - started, .5)
            with self.assertRaisesRegex(StopRun, 'interrupted'):
                result.result(timeout=1)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(self.statuses(), ['started', 'network_error'])

    def test_token_renewal_retries_without_archiving_credentials_or_token_response(self):
        failure = network_failure('POST', RemoteDisconnected('private'), token_request=True)
        token = b'{"access_token":"private-bearer", "expires_in":300}'
        transport = Mock(side_effect=[failure, (200, {}, token), (200, {}, b'{}')])
        client = self.client(transport)
        client.auth = 'blockstream'
        client.token, client.token_until = 'expired-token', 0
        with patch.dict('os.environ', {'BLOCKSTREAM_CLIENT_ID': 'private-client',
                                       'BLOCKSTREAM_CLIENT_SECRET': 'private-secret'}), \
             patch.object(client._cancelled, 'wait', return_value=False) as backoff:
            self.assertEqual(client.get('/tx/one')[0], {})
        self.assertEqual(client.budget.requests, 3)
        self.assertEqual(transport.call_count, 3)
        self.assertEqual([item.args[0] for item in transport.call_args_list], ['POST', 'POST', 'GET'])
        self.assertEqual([item.args[1] for item in transport.call_args_list[:2]], [TOKEN_URL, TOKEN_URL])
        self.assertEqual(transport.call_args_list[-1].args[2]['Authorization'], 'Bearer private-bearer')
        backoff.assert_called_once_with(1)
        self.assertEqual(self.statuses(), ['started', 'network_error', 'started', '200', 'started', '200'])
        self.assertEqual(len(list(self.store.observations(client.used))), 1)
        archived = '\n'.join(self.store.db.iterdump())
        for secret in ('private-client', 'private-secret', 'private-bearer', 'expired-token'):
            self.assertNotIn(secret, archived)

    def test_token_transient_http_and_transport_failures_share_attempt_ceiling(self):
        failure = network_failure('POST', TimeoutError('private'), token_request=True)
        transport = Mock(side_effect=[failure, (503, {}, b'private-body'), failure, failure])
        client = self.client(transport)
        client.auth = 'blockstream'
        with patch.dict('os.environ', {'BLOCKSTREAM_CLIENT_ID': 'private-client',
                                       'BLOCKSTREAM_CLIENT_SECRET': 'private-secret'}), \
             patch.object(client._cancelled, 'wait', return_value=False) as backoff:
            for endpoint in ('/tx/one', '/tx/two'):
                with self.assertRaisesRegex(TransientExplorerConnection,
                                           'token request network retries exhausted after 4 attempts') as caught:
                    client.get(endpoint)
                self.assertNotIn('private', str(caught.exception))
        self.assertEqual(transport.call_count, 4)
        self.assertEqual(client.budget.requests, 4)
        self.assertEqual(backoff.call_args_list, [call(1), call(2), call(4)])
        self.assertEqual(len(list(self.store.observations(client.used))), 0)

    def test_token_credential_validation_errors_and_generic_posts_do_not_retry(self):
        generic = network_failure('POST', RemoteDisconnected('private'))
        self.assertNotIsInstance(generic, TransientExplorerConnection)
        for result in ((401, {}, b'private'), (400, {}, b'private'), (200, {}, b'invalid json')):
            with self.subTest(status=result[0]):
                transport = Mock(return_value=result)
                client = self.client(transport)
                client.auth = 'blockstream'
                with patch.dict('os.environ', {'BLOCKSTREAM_CLIENT_ID': 'private-client',
                                               'BLOCKSTREAM_CLIENT_SECRET': 'private-secret'}), \
                     patch.object(client._cancelled, 'wait', return_value=False) as backoff, \
                     self.assertRaises(TraceError):
                    client.get('/tx/one')
                self.assertEqual(transport.call_count, 1)
                backoff.assert_not_called()

    def test_permanent_transport_and_invalid_json_fail_without_retry(self):
        for response in (network_failure('GET', ssl.SSLCertVerificationError('private')),
                         (200, {}, b'invalid json')):
            with self.subTest(response=type(response).__name__):
                transport = Mock(side_effect=response) if isinstance(response, Exception) else Mock(return_value=response)
                client = self.client(transport)
                with patch.object(client._cancelled, 'wait', return_value=False) as backoff, \
                     self.assertRaises(TraceError):
                    client.get('/tx/one')
                self.assertEqual(transport.call_count, 1)
                backoff.assert_not_called()


if __name__ == '__main__':
    unittest.main()
