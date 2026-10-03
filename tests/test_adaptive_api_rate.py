import os
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from liquid_tracer.api import ADAPTIVE_RATE_ATTEMPTS, ENTERPRISE, TOKEN_URL, Esplora, Limits
from liquid_tracer.common import StopRun, TraceError
from liquid_tracer.explorer_http import TransientExplorerConnection
from liquid_tracer.explorer_quota import LocalExplorerQuota, SharedExplorerQuota
from liquid_tracer.store import Store


class AdaptiveAPIRateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'case')
        self.clients = []
        self.clock = [1000.]
        self.environ = patch.dict(os.environ, {}, clear=True)
        self.environ.start()

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.environ.stop()
        self.store.close()
        self.temp.cleanup()

    def client(self, transport=None, **kwargs):
        transport = transport or Mock(return_value=(200, {}, b'{}'))
        kwargs.setdefault('auth', 'none')
        limits = kwargs.pop('limits', Limits())
        client = Esplora(self.store, 'run-' + str(len(self.clients)), limits,
                         transport=transport, **kwargs)
        self.clients.append(client)
        return client

    @contextmanager
    def simulated(self, client):
        self.assertIsInstance(client._shared_quota, LocalExplorerQuota)
        client._shared_quota = LocalExplorerQuota(client.min_interval, adaptive=True,
                                                  clock=lambda: self.clock[0])
        client.budget.started = self.clock[0]

        def advance(seconds):
            self.clock[0] += seconds + 1e-8
            return False

        with ExitStack() as stack:
            stack.enter_context(patch('liquid_tracer.api.time.monotonic', side_effect=lambda: self.clock[0]))
            stack.enter_context(patch.object(client._gate, 'wait', side_effect=advance))
            stack.enter_context(patch.object(client._cancelled, 'wait', side_effect=advance))
            yield

    def test_default_and_auto_grow_past_49_with_only_local_injected_state(self):
        for value in (None, 'auto', 'AUTO'):
            with self.subTest(value=value):
                if value is None:
                    os.environ.pop('LIQUID_BLOCKSTREAM_ENTERPRISE_RPS', None)
                else:
                    os.environ['LIQUID_BLOCKSTREAM_ENTERPRISE_RPS'] = value
                client = self.client()
                self.assertEqual(client.api_rate_mode, 'adaptive')
                self.assertAlmostEqual(client.effective_rps, 49)
                self.assertFalse(hasattr(client._shared_quota, 'path'))
                with self.simulated(client):
                    for index in range(200):
                        client.get('/address/' + str(index))
                metrics = client.request_metrics()
                self.assertGreater(metrics['api_target_rps'], 49)
                self.assertEqual(metrics['api_target_rps'], metrics['shared_api_effective_rps'])
                self.assertEqual(metrics['completed_requests'], 200)
                self.assertAlmostEqual(client.effective_rps, 49)  # Constructor sizing hint remains stable.

    def test_verified_allowance_numeric_target_and_explicit_interval_stay_fixed(self):
        options = [({'advertised_rps': 100}, {}, 95),
                   ({}, {'LIQUID_BLOCKSTREAM_API_RPS': '100'}, 95),
                   ({}, {'LIQUID_BLOCKSTREAM_ENTERPRISE_RPS': '120'}, 120),
                   ({'min_interval': .25}, {}, 4),
                   ({'min_interval': .001}, {}, 1000),
                   ({'min_interval': .001, 'advertised_rps': 100}, {}, 95),
                   ({'base': 'https://blockstream.info/liquid/api'}, {}, 4),
                   ({'base': 'https://custom.example/liquid/api'}, {}, 4)]
        for kwargs, environment, expected in options:
            with self.subTest(kwargs=kwargs, environment=environment), patch.dict(os.environ, environment, clear=True):
                client = self.client(**kwargs)
                self.assertEqual(client.api_rate_mode, 'fixed')
                self.assertAlmostEqual(client.effective_rps, expected)
                self.assertIsNone(client._shared_quota)
        zero = self.client(min_interval=0)
        self.assertEqual(zero.api_rate_mode, 'adaptive')
        self.assertFalse(zero.min_interval_explicit)
        with self.assertRaisesRegex(TraceError, 'interval is too small'):
            self.client(min_interval=5e-324)

    def test_429_reduces_rate_and_retry_after_applies_before_next_request(self):
        starts = []
        responses = iter([(429, {'Retry-After': '3.5'}, b'{"busy":true}'), (200, {}, b'{}')])

        def transport(*_):
            starts.append(self.clock[0])
            return next(responses)

        client = self.client(transport)
        with self.simulated(client):
            self.assertEqual(client.get('/address/one')[0], {})
        self.assertGreaterEqual(starts[1] - starts[0], 3.5)
        self.assertAlmostEqual(client.request_metrics()['api_target_rps'], 49 * .7)
        self.assertEqual(client.budget.requests, 2)
        self.assertEqual(sorted(row['status'] for row in self.store.observations(client.used)), [200, 429])

    def test_retryable_server_errors_and_disconnects_back_off_but_remain_bounded(self):
        for failure in ('server', 'network'):
            with self.subTest(failure=failure):
                transport = Mock(return_value=(503, {}, b'{"busy":true}'))
                if failure == 'network':
                    transport.side_effect = TransientExplorerConnection('Network request failed: RemoteDisconnected')
                client = self.client(transport)
                with self.simulated(client), self.assertRaisesRegex(TraceError, 'retries exhausted'):
                    client.get('/address/' + failure)
                self.assertEqual(transport.call_count, 4)
                self.assertEqual(client.budget.requests, 4)
                self.assertLess(client.request_metrics()['api_target_rps'], 49)
                self.assertEqual(len(client.used), 4 if failure == 'server' else 0)
                outcomes = self.store.db.execute(
                    "SELECT status FROM attempts WHERE run_id = ? AND status != 'started'", (client.run_id,)).fetchall()
                self.assertEqual(len(outcomes), 4)

    def test_ordinary_bad_request_and_auth_rejection_do_not_lower_rate_or_retry(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                transport = Mock(return_value=(status, {}, b'{}'))
                client = self.client(transport)
                with self.simulated(client), self.assertRaisesRegex(TraceError, 'HTTP ' + str(status)):
                    client.get('/address/' + str(status))
                self.assertEqual(transport.call_count, 1)
                self.assertAlmostEqual(client.request_metrics()['api_target_rps'], 49)
                self.assertEqual(client.request_metrics()['pressure_events'], 0)
                self.assertEqual(len(client.used), 1)

    def test_long_retry_after_is_honored_in_adaptive_mode(self):
        client = self.client(Mock(side_effect=[(429, {'Retry-After': '60'}, b'{"busy":true}'),
                                               (200, {}, b'{}')]))
        started = self.clock[0]
        with self.simulated(client):
            self.assertEqual(client.get('/address/one')[0], {})
        self.assertGreaterEqual(self.clock[0] - started, 60)
        self.assertEqual(client.budget.requests, 2)
        self.assertEqual(len(client.used), 2)

    def test_long_retry_after_respects_run_budget_and_retains_evidence(self):
        client = self.client(Mock(return_value=(429, {'Retry-After': '60'}, b'{"busy":true}')),
                             limits=Limits(max_seconds=10))
        with self.simulated(client), self.assertRaisesRegex(StopRun, 'time_limit'):
            client.get('/address/one')
        self.assertEqual(client.budget.requests, 1)
        self.assertEqual(len(client.used), 1)

    def test_stale_high_target_can_recover_without_four_attempt_throttling_cutoff(self):
        starts = []
        client = None

        def provider(*_):
            starts.append(self.clock[0])
            # Emulate pressure that continues until the learned request target
            # falls within the provider's reduced 50 RPS capacity.
            return (429, {}, b'{"busy":true}') if client._shared_quota._state.target_rps > 50 else (200, {}, b'{}')

        client = self.client(provider)
        with self.simulated(client):
            client._shared_quota._state.target_rps = 5000
            self.assertEqual(client.get('/address/one')[0], {})
        self.assertGreater(client.budget.requests, 4)
        self.assertLess(client.budget.requests, ADAPTIVE_RATE_ATTEMPTS)
        self.assertLessEqual(client.request_metrics()['api_target_rps'], 50)
        self.assertEqual(len(client.used), client.budget.requests)
        self.assertLess(max(right - left for left, right in zip(starts, starts[1:])), 30.001)

    def test_permanent_throttle_is_bounded_with_capped_fallback_waits(self):
        transport = Mock(return_value=(429, {}, b'{}'))
        client = self.client(transport)
        started = self.clock[0]
        with self.simulated(client), self.assertRaisesRegex(TraceError, 'rate-limit retries exhausted after 16 HTTP 429'):
            client.get('/address/one')
        self.assertEqual(transport.call_count, ADAPTIVE_RATE_ATTEMPTS)
        self.assertEqual(client.budget.requests, ADAPTIVE_RATE_ATTEMPTS)
        self.assertEqual(len(client.used), ADAPTIVE_RATE_ATTEMPTS)
        self.assertLess(self.clock[0] - started, ADAPTIVE_RATE_ATTEMPTS * 30)

    def test_throttling_does_not_consume_transient_retry_allowance(self):
        responses = [(429, {}, b'{}')] * 4 + [(503, {}, b'{}')] * 3 + [(200, {}, b'{}')]
        client = self.client(Mock(side_effect=responses))
        with self.simulated(client):
            self.assertEqual(client.get('/address/one')[0], {})
        self.assertEqual(client.budget.requests, 8)

    def test_oauth_throttle_waits_and_bodies_are_never_archived(self):
        tokens = iter([(429, {'Retry-After': '60'}, b'{"secret":"do-not-save"}'),
                       (200, {}, b'{"access_token":"do-not-save","expires_in":300}')])

        def transport(method, url, *_):
            return next(tokens) if url == TOKEN_URL else (200, {}, b'{}')

        client = self.client(transport, auth='blockstream')
        started = self.clock[0]
        with self.simulated(client), patch.dict(os.environ, BLOCKSTREAM_CLIENT_ID='test', BLOCKSTREAM_CLIENT_SECRET='test'):
            self.assertEqual(client.get('/address/one')[0], {})
        self.assertGreaterEqual(self.clock[0] - started, 60)
        self.assertEqual(client.budget.requests, 3)
        rows = list(self.store.observations(client.used))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['endpoint'], '/address/one')
        self.assertEqual(rows[0]['body'], b'{}')

    def test_cancellation_interrupts_adaptive_long_retry_after(self):
        transport = Mock(return_value=(429, {'Retry-After': '60'}, b'{}'))
        client = self.client(transport)
        with self.simulated(client):
            with patch.object(client._gate, 'wait', side_effect=lambda _: client._cancelled.set()):
                with self.assertRaisesRegex(StopRun, 'interrupted'):
                    client.get('/address/one')
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(len(client.used), 1)

    def test_real_transport_lazily_creates_shared_adaptive_quota(self):
        # Exercise selection without sending a network request or opening the
        # user's private cache. The supplied object is a temporary test quota.
        quota = SharedExplorerQuota(ENTERPRISE, 1 / 49, adaptive=True,
                                    directory=Path(self.temp.name) / 'quota')
        with patch('liquid_tracer.explorer_http.urllib.request.getproxies', return_value={}):
            client = Esplora(self.store, 'owned', Limits(max_requests=1), auth='none')
        self.clients.append(client)
        self.assertIsNone(client._shared_quota)
        with patch('liquid_tracer.api.SharedExplorerQuota', return_value=quota) as factory:
            client._reserve_request()
        factory.assert_called_once_with(ENTERPRISE, 1 / 49, adaptive=True)
        self.assertEqual(client.request_metrics()['api_rate_mode'], 'adaptive')


if __name__ == '__main__':
    unittest.main()
