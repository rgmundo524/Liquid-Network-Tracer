import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from liquid_tracer.api import Esplora, Limits, TOKEN_URL
from liquid_tracer.common import StopRun
from liquid_tracer.store import Store


class CountAPIScalingTests(unittest.TestCase):
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
        options = dict(auth='none', advertised_rps=1000000, workers=4)
        options.update(kwargs)
        client = Esplora(self.store, 'counts', options.pop('limits', Limits()),
                         transport=transport, **options)
        self.clients.append(client)
        return client

    def test_service_includes_lookup_archive_and_json_excluding_nested_auth_pacing_and_backoff(self):
        clock = [time.monotonic()]
        statuses = iter([503, 200])

        def transport(method, url, *_):
            clock[0] += .4 if url == TOKEN_URL else .25
            if url == TOKEN_URL:
                return 200, {}, b'{"access_token":"test","expires_in":300}'
            return next(statuses), {}, b'{}'

        client = self.client(transport, auth='blockstream')

        def cached(*_):
            clock[0] += .3

        def attempt(*_):
            clock[0] += .1

        def observe(*_):
            clock[0] += .2
            return 'observation'

        reserve = client._reserve_request

        def paced_reserve():
            clock[0] += 2.
            reserve()

        def pause(seconds):
            clock[0] += seconds

        loads = json.loads

        def decode(raw):
            clock[0] += .05
            return loads(raw)

        with patch('liquid_tracer.api.time.monotonic', side_effect=lambda: clock[0]), \
             patch.dict('os.environ', BLOCKSTREAM_CLIENT_ID='test', BLOCKSTREAM_CLIENT_SECRET='test'), \
             patch.object(client, '_reserve_request', side_effect=paced_reserve), \
             patch.object(client._cancelled, 'wait', side_effect=pause), \
             patch.object(self.store, 'cached', side_effect=cached), \
             patch.object(self.store, 'attempt', side_effect=attempt), \
             patch.object(self.store, 'observe', side_effect=observe), \
             patch('liquid_tracer.api.json.loads', side_effect=decode):
            self.assertEqual(client.get('/address/one'), ({}, 'observation'))
        metrics = client.request_metrics()
        self.assertAlmostEqual(metrics['service_latency_seconds'], 1.65)
        self.assertAlmostEqual(metrics['latency_seconds'], .25)
        self.assertAlmostEqual(metrics['network_seconds_total'], .9)
        self.assertAlmostEqual(metrics['evidence_seconds_total'], 1.)
        self.assertAlmostEqual(metrics['pacing_wait_seconds_total'], 6.)
        self.assertAlmostEqual(metrics['retry_wait_seconds_total'], 1.)
        self.assertEqual(metrics['completed_endpoints'], 1)
        self.assertEqual(metrics['completed_requests'], 2)

    def test_cached_endpoint_service_measures_lookup_without_transport_sample(self):
        clock = [time.monotonic()]
        transport = Mock()
        client = self.client(transport)

        def cached(*_):
            clock[0] += .3
            return {}, 'cached-observation'

        with patch('liquid_tracer.api.time.monotonic', side_effect=lambda: clock[0]), \
             patch.object(self.store, 'cached', side_effect=cached):
            self.assertEqual(client.get('/address/one'), ({}, 'cached-observation'))
        metrics = client.request_metrics()
        self.assertAlmostEqual(metrics['service_latency_seconds'], .3)
        self.assertEqual(metrics['completed_endpoints'], 1)
        self.assertEqual(metrics['completed_requests'], 0)
        self.assertIsNone(metrics['latency_seconds'])
        transport.assert_not_called()

    def test_endpoint_sample_is_published_after_evidence_completes(self):
        entered, release = threading.Event(), threading.Event()
        client = self.client(Mock(return_value=(200, {}, b'{}')))
        observe = self.store.observe

        def delayed_observe(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return observe(*args)

        with patch.object(self.store, 'observe', side_effect=delayed_observe):
            future = client.submit('/address/one')
            try:
                self.assertTrue(entered.wait(3))
                metrics = client.request_metrics()
                self.assertEqual(metrics['completed_requests'], 1)
                self.assertEqual(metrics['completed_endpoints'], 0)
                self.assertIsNone(metrics['service_latency_seconds'])
            finally:
                release.set()
            future.result(timeout=3)
        self.assertEqual(client.request_metrics()['completed_endpoints'], 1)

    def test_streaming_retains_only_pending_bodies_and_recovers_from_evidence(self):
        transport = Mock(return_value=(200, {}, b'{"chain_stats":{"tx_count":7}}'))
        client = self.client(transport)
        retained = client.submit('/address/retained')
        retained.result(timeout=3)
        endpoints = ['/address/' + str(index) for index in range(257)]
        delivered = []

        def consume(endpoint, value):
            self.assertLessEqual(len(client._results), client.workers + 1)
            self.assertEqual(value[0]['chain_stats']['tx_count'], 7)
            delivered.append(endpoint)

        result = client.prefetch(endpoints + endpoints[:10], on_result=consume, retain_results=False)
        self.assertEqual(result, {})
        self.assertCountEqual(delivered, endpoints)
        self.assertEqual(len(delivered), len(endpoints))
        self.assertEqual(client._results, {'/address/retained': retained})
        self.assertEqual(transport.call_count, len(endpoints) + 1)
        self.assertEqual(client.get(endpoints[0])[0]['chain_stats']['tx_count'], 7)
        self.assertEqual(transport.call_count, len(endpoints) + 1)
        self.assertEqual(client.request_metrics()['cache_hits'], 1)

    def test_streaming_interruption_drains_and_delivers_started_responses_once(self):
        slow_entered, release = threading.Event(), threading.Event()
        calls, delivered = [], []

        def transport(method, url, *_):
            endpoint = url.rsplit('/', 1)[1]
            calls.append(endpoint)
            if endpoint == 'fast':
                self.assertTrue(slow_entered.wait(3))
            else:
                slow_entered.set()
                self.assertTrue(release.wait(3))
            return 200, {}, b'{}'

        client = self.client(transport, workers=2)

        def consume(endpoint, value):
            delivered.append(endpoint)
            self.assertIsInstance(value, tuple)
            if endpoint == '/address/fast':
                release.set()
                raise KeyboardInterrupt('stop')

        with self.assertRaisesRegex(KeyboardInterrupt, 'stop'):
            client.prefetch(['/address/fast', '/address/slow', '/address/queued'],
                            on_result=consume, retain_results=False)
        self.assertEqual(delivered, ['/address/fast', '/address/slow'])
        self.assertCountEqual(calls, ['fast', 'slow'])
        self.assertEqual(client._results, {})
        self.assertEqual(len(list(self.store.observations(client.used))), 2)

    def test_streaming_respects_request_budget_and_releases_failed_futures(self):
        transport = Mock(return_value=(200, {}, b'{}'))
        client = self.client(transport, limits=Limits(max_requests=2))
        delivered = {}
        self.assertEqual(client.prefetch(['/address/' + str(index) for index in range(20)],
                                         on_result=lambda endpoint, value: delivered.update({endpoint: value}),
                                         retain_results=False), {})
        self.assertEqual(client.budget.requests, 2)
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(sum(isinstance(value, tuple) for value in delivered.values()), 2)
        self.assertTrue(any(isinstance(value, StopRun) for value in delivered.values()))
        self.assertEqual(client._results, {})


if __name__ == '__main__':
    unittest.main()
