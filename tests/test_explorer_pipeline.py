import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from liquid_tracer.api import Esplora, Limits, http
from liquid_tracer.common import StopRun, TraceError
from liquid_tracer.explorer_http import StaleExplorerConnection
from liquid_tracer.store import Store


class ExplorerPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'case')
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.store.close()
        self.temp.cleanup()

    def client(self, transport, **kwargs):
        options = dict(auth='none', advertised_rps=1000000, min_interval=0)
        options.update(kwargs)
        client = Esplora(self.store, options.pop('run_id', 'run'), options.pop('limits', Limits()),
                         transport=transport, **options)
        self.clients.append(client)
        return client

    def test_submit_coalesces_inflight_get_and_cached_result(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def transport(*_):
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(timeout=3))
            return 200, {}, b'{"status":{"confirmed":true}}'

        client = self.client(transport)
        first = client.submit('/tx/one')
        self.assertTrue(entered.wait(timeout=3))
        self.assertIs(first, client.submit('/tx/one'))
        with ThreadPoolExecutor(max_workers=1) as pool:
            direct = pool.submit(client.get, '/tx/one')
            release.set()
            self.assertEqual(direct.result(timeout=3), first.result(timeout=3))
        client.drain_pending()
        self.assertIs(first, client.submit('/tx/one'))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(client.used), 1)
        self.assertEqual(client.request_metrics()['coalesced_hits'], 3)

    def test_drain_cancels_queued_requests_but_finishes_active_evidence(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def transport(*_):
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(timeout=3))
            return 200, {}, b'{}'

        client = self.client(transport, workers=1)
        running = client.submit('/tx/one')
        self.assertTrue(entered.wait(timeout=3))
        queued = client.submit('/tx/two')
        with ThreadPoolExecutor(max_workers=1) as pool:
            drained = pool.submit(client.drain_pending, True)
            deadline = time.monotonic() + 3
            while not queued.cancelled() and time.monotonic() < deadline:
                time.sleep(.001)
            self.assertTrue(queued.cancelled())
            self.assertFalse(drained.done())
            release.set()
            drained.result(timeout=3)
        self.assertTrue(running.done())
        self.assertFalse(running.cancelled())
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(list(self.store.observations(client.used))), 1)
        # An ordinary drain retains a usable pool and client.
        self.assertEqual(client.submit('/tx/three').result(timeout=3)[0], {})

    def test_default_transport_is_owned_and_closed_only_after_workers_drain(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()

        def transport(*_):
            entered.set()
            self.assertTrue(release.wait(timeout=3))
            self.assertFalse(closed.is_set())
            return 200, {}, b'{}'

        owned = Mock(side_effect=transport)
        owned.close.side_effect = closed.set
        quota = Mock()
        quota.reserve.return_value = SimpleNamespace(admitted=True, active_clients=1,
                                                     effective_rps=49., wait_seconds=0., reason='')
        with patch('liquid_tracer.api.ExplorerHTTP', return_value=owned) as factory:
            client = self.client(http, shared_quota=quota)
        factory.assert_called_once()
        self.assertTrue(client._automatic_quota)
        future = client.submit('/tx/one')
        self.assertTrue(entered.wait(timeout=3))
        with ThreadPoolExecutor(max_workers=1) as pool:
            closing = pool.submit(client.close)
            self.assertFalse(closed.wait(timeout=.01))
            release.set()
            closing.result(timeout=3)
        self.assertTrue(closed.is_set())
        self.assertEqual(future.result(timeout=3)[0], {})
        self.assertEqual(len(list(self.store.observations(client.used))), 1)

    def test_injected_transport_is_never_owned_or_wrapped(self):
        transport = Mock(return_value=(200, {}, b'{}'))
        with patch('liquid_tracer.api.ExplorerHTTP') as factory:
            client = self.client(transport)
            client.get('/tx/one')
            client.close()
        factory.assert_not_called()
        transport.close.assert_not_called()
        self.assertFalse(client._automatic_quota)

    def test_stale_get_retry_is_budgeted_and_archived_without_hidden_retry(self):
        transport = Mock(side_effect=[StaleExplorerConnection('Network request failed: RemoteDisconnected'),
                                      (200, {}, b'{}')])
        client = self.client(transport, limits=Limits(max_requests=2))
        with patch.object(client._cancelled, 'wait', return_value=False):
            self.assertEqual(client.get('/tx/one')[0], {})
        self.assertEqual(client.budget.requests, 2)
        self.assertEqual(transport.call_count, 2)
        statuses = self.store.db.execute('SELECT status FROM attempts ORDER BY id').fetchall()
        self.assertEqual([row['status'] for row in statuses], ['started', 'network_error', 'started', '200'])
        self.assertEqual(len(list(self.store.observations(client.used))), 1)
        metrics = client.request_metrics()
        self.assertEqual(metrics['completed_requests'], 2)
        self.assertEqual(metrics['pressure_events'], 1)

    def test_stale_retry_respects_hard_request_and_attempt_limits(self):
        transport = Mock(side_effect=StaleExplorerConnection('Network request failed: RemoteDisconnected'))
        limited = self.client(transport, limits=Limits(max_requests=1))
        with self.assertRaisesRegex(StopRun, 'request_limit'):
            limited.get('/tx/one')
        self.assertEqual(transport.call_count, 1)
        transport.reset_mock()
        bounded = self.client(transport, run_id='bounded', limits=Limits(max_requests=20))
        with patch.object(bounded._cancelled, 'wait', return_value=False), self.assertRaises(StaleExplorerConnection):
            bounded.get('/tx/two')
        self.assertEqual(transport.call_count, 4)
        self.assertEqual(bounded.budget.requests, 4)

    def test_timing_separates_transport_evidence_and_retry_wait(self):
        client = self.client(Mock())
        clock = [time.monotonic()]
        statuses = iter([503, 200])

        def transport(*_):
            clock[0] += .25
            return next(statuses), {}, b'{}'

        def pause(seconds):
            clock[0] += seconds

        original_attempt, original_response = self.store.attempt, self.store.record_response

        def attempt(*args):
            clock[0] += .1
            return original_attempt(*args)

        def record_response(*args):
            clock[0] += .3
            return original_response(*args)

        client.transport = transport
        with patch('liquid_tracer.api.time.monotonic', side_effect=lambda: clock[0]), \
             patch.object(client._cancelled, 'wait', side_effect=pause), \
             patch.object(self.store, 'attempt', side_effect=attempt), \
             patch.object(self.store, 'record_response', side_effect=record_response):
            client.get('/tx/one')
        metrics = client.request_metrics()
        self.assertAlmostEqual(metrics['network_seconds_total'], .5)
        self.assertAlmostEqual(metrics['retry_wait_seconds_total'], 1.)
        self.assertAlmostEqual(metrics['evidence_seconds_total'], .8)
        self.assertAlmostEqual(metrics['pacing_wait_seconds_total'], 0.)
        self.assertEqual(metrics['retry_responses'], 1)
        self.assertEqual(metrics['peak_in_flight'], 1)

    def test_disk_cache_hits_distinguished_from_inrun_coalescing(self):
        transport = Mock(return_value=(200, {}, b'{"status":{"confirmed":true}}'))
        first = self.client(transport, run_id='first')
        first.get('/tx/one')
        first.close()
        second = self.client(transport, run_id='second')
        second.get('/tx/one')
        second.get('/tx/one')
        metrics = second.request_metrics()
        self.assertEqual(metrics['cache_hits'], 1)
        self.assertEqual(metrics['coalesced_hits'], 1)
        self.assertEqual(metrics['network_seconds_total'], 0.)
        self.assertEqual(metrics['peak_in_flight'], 0)
        self.assertEqual(transport.call_count, 1)

    def test_prefetch_already_elapsed_budget_returns_ordered_endpoint_errors(self):
        transport = Mock(return_value=(200, {}, b'{}'))
        client = self.client(transport, limits=Limits(max_seconds=1))
        endpoints = ['/tx/one', '/tx/two', '/tx/three']
        with patch('liquid_tracer.api.time.monotonic', return_value=client.budget.started + 2):
            results = client.prefetch(endpoints)
        self.assertEqual(list(results), endpoints)
        self.assertTrue(all(isinstance(value, StopRun) and str(value) == 'time_limit'
                            for value in results.values()))
        transport.assert_not_called()


if __name__ == '__main__':
    unittest.main()
