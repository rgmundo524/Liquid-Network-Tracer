import os
import tempfile
import unittest
import urllib.error
from contextlib import contextmanager
from http.client import RemoteDisconnected
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.api import Esplora, Limits
from liquid_tracer.common import StopRun
from liquid_tracer.explorer_http import ExplorerRequestTimeout, TransientExplorerConnection, network_failure
from liquid_tracer.explorer_quota import LocalExplorerQuota
from liquid_tracer.store import Store


class ExplorerDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'case')
        self.clients = []
        self.clock = [1000.]

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.store.close()
        self.temp.cleanup()

    @contextmanager
    def client(self, transport, *, seconds=.05, requests=0, auth='none'):
        with patch.dict(os.environ, {'LIQUID_BLOCKSTREAM_ENTERPRISE_RPS': 'auto',
                                     'LIQUID_BLOCKSTREAM_API_RPS': '',
                                     'BLOCKSTREAM_CLIENT_ID': 'private-client',
                                     'BLOCKSTREAM_CLIENT_SECRET': 'private-secret'}), \
             patch('liquid_tracer.api.time.monotonic', side_effect=lambda: self.clock[0]):
            client = Esplora(self.store, 'run-' + str(len(self.clients)),
                             Limits(max_seconds=seconds, max_requests=requests),
                             auth=auth, transport=transport,
                             shared_quota=LocalExplorerQuota(1 / 49, adaptive=True, clock=lambda: self.clock[0]))
            self.clients.append(client)
            yield client

    def outcomes(self, client):
        return [row[0] for row in self.store.db.execute(
            'SELECT status FROM attempts WHERE run_id=? ORDER BY id', (client.run_id,))]

    def test_shortened_local_deadline_timeout_does_not_reduce_learned_rate(self):
        failures = [network_failure('GET', TimeoutError('private')),
                    urllib.error.URLError(TimeoutError('private')),
                    TimeoutError('private')]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                def transport(method, url, headers, body, timeout):
                    self.assertAlmostEqual(timeout, .05)
                    self.clock[0] += timeout + .001
                    raise failure

                with self.client(transport) as client, self.assertRaisesRegex(StopRun, 'time_limit'):
                    client.get('/address/one')
                metrics = client.request_metrics()
                self.assertEqual(metrics['local_deadline_timeouts'], 1)
                self.assertEqual(metrics['pressure_events'], 0)
                self.assertEqual(metrics['completed_requests'], 1)
                self.assertEqual(metrics['retry_wait_seconds_total'], 0)
                self.assertEqual(client.budget.requests, 1)
                self.assertEqual(client._shared_quota._state.generation, 0)
                self.assertAlmostEqual(client._shared_quota._state.target_rps, 49)
                self.assertEqual(self.outcomes(client), ['started', 'local_time_limit'])
                self.assertFalse(client.used)

    def test_actual_network_failure_at_expired_deadline_still_reduces_rate(self):
        failures = [network_failure('GET', RemoteDisconnected('private')),
                    TransientExplorerConnection('Network request failed: TimeoutError')]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                def transport(method, url, headers, body, timeout):
                    self.clock[0] += timeout + .001
                    raise failure

                with self.client(transport) as client, self.assertRaisesRegex(StopRun, 'time_limit'):
                    client.get('/address/one')
                self.assertEqual(client.request_metrics()['local_deadline_timeouts'], 0)
                self.assertEqual(client.request_metrics()['pressure_events'], 1)
                self.assertEqual(client._shared_quota._state.generation, 1)
                self.assertLess(client._shared_quota._state.target_rps, 49)
                self.assertEqual(self.outcomes(client), ['started', 'network_error'])

    def test_early_timeout_under_shortened_budget_remains_network_pressure(self):
        def transport(method, url, headers, body, timeout):
            self.assertAlmostEqual(timeout, 10)
            self.clock[0] += .01
            raise network_failure('GET', TimeoutError('private'))

        with self.client(transport, seconds=10, requests=1) as client, self.assertRaisesRegex(StopRun, 'request_limit'):
            client.get('/address/one')
        self.assertEqual(client.request_metrics()['local_deadline_timeouts'], 0)
        self.assertEqual(client.request_metrics()['pressure_events'], 1)
        self.assertEqual(client._shared_quota._state.generation, 1)

    def test_normal_twenty_second_timeout_is_not_excused_when_budget_also_expires(self):
        def transport(method, url, headers, body, timeout):
            self.assertEqual(timeout, 20)
            self.clock[0] += 21
            raise network_failure('GET', TimeoutError('private'))

        with self.client(transport, seconds=20, requests=1) as client, self.assertRaisesRegex(StopRun, 'time_limit'):
            client.get('/address/one')
        self.assertEqual(client.request_metrics()['local_deadline_timeouts'], 0)
        self.assertEqual(client.request_metrics()['pressure_events'], 1)
        self.assertEqual(client._shared_quota._state.generation, 1)

    def test_provider_errors_received_after_local_deadline_are_preserved_and_reduce_rate(self):
        for status in (429, 503):
            with self.subTest(status=status):
                def transport(method, url, headers, body, timeout):
                    self.clock[0] += timeout + .001
                    return status, {}, b'{"busy":true}'

                with self.client(transport) as client, self.assertRaisesRegex(StopRun, 'time_limit'):
                    client.get('/address/one')
                self.assertEqual(client.request_metrics()['local_deadline_timeouts'], 0)
                self.assertEqual(client.request_metrics()['pressure_events'], 1)
                self.assertEqual(client._shared_quota._state.generation, 1)
                self.assertEqual(self.outcomes(client), ['started', str(status)])
                rows = list(self.store.observations(client.used))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['status'], status)
                self.assertEqual(rows[0]['body'], b'{"busy":true}')

    def test_oauth_deadline_timeout_is_local_and_never_archives_credentials(self):
        def transport(method, url, headers, body, timeout):
            self.assertEqual(method, 'POST')
            self.clock[0] += timeout + .001
            error = network_failure('POST', TimeoutError('private'), token_request=True)
            self.assertIsInstance(error, ExplorerRequestTimeout)
            raise error

        with self.client(transport, auth='blockstream') as client, self.assertRaisesRegex(StopRun, 'time_limit'):
            client.get('/address/one')
        self.assertEqual(client.request_metrics()['local_deadline_timeouts'], 1)
        self.assertEqual(client.request_metrics()['pressure_events'], 0)
        self.assertEqual(client._shared_quota._state.generation, 0)
        self.assertEqual(self.outcomes(client), ['started', 'local_time_limit'])
        self.assertFalse(client.used)
        archived = '\n'.join(self.store.db.iterdump())
        self.assertNotIn('private-client', archived)
        self.assertNotIn('private-secret', archived)

    def test_storage_metadata_is_read_without_holding_the_api_metrics_lock(self):
        with self.client(lambda *_: (200, {}, b'{}')) as client:
            def storage_metrics():
                acquired = client._metrics_lock.acquire(blocking=False)
                self.assertTrue(acquired, 'Coordinator snapshot would invert the admission lock order')
                client._metrics_lock.release()
                return {'quota_journal_mode': 'wal', 'quota_connection_mode': 'persistent'}

            with patch.object(client._shared_quota, 'storage_metrics', side_effect=storage_metrics, create=True):
                metrics = client.request_metrics()
        self.assertEqual(metrics['quota_journal_mode'], 'wal')
        self.assertEqual(metrics['quota_connection_mode'], 'persistent')


if __name__ == '__main__':
    unittest.main()
