import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.api import Budget, ENTERPRISE, Esplora, Limits, default_min_interval
from liquid_tracer.common import StopRun, TraceError, canonical
from liquid_tracer.store import Store


class ApiConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'case')
        self.clients = []

    def tearDown(self):
        for api in self.clients:
            api.close()
        self.store.close()
        self.temp.cleanup()

    def client(self, transport, **kwargs):
        options = {'auth': 'none', 'min_interval': 0, 'advertised_rps': 1000000}
        options.update(kwargs)
        api = Esplora(self.store, options.pop('run_id', 'run'),
                      options.pop('limits', Limits()), transport=transport, **options)
        self.clients.append(api)
        return api

    def test_prefetch_overlaps_bounded_workers_and_preserves_order(self):
        lock, barrier = threading.Lock(), threading.Barrier(4)
        active = maximum = 0
        calls = []

        def transport(method, url, headers, body, timeout):
            nonlocal active, maximum
            endpoint = url.removeprefix(ENTERPRISE)
            with lock:
                active += 1
                maximum = max(maximum, active)
                calls.append(endpoint)
            barrier.wait(timeout=3)
            with lock:
                active -= 1
            return 200, {}, canonical({'endpoint': endpoint})

        api = self.client(transport, workers=4)
        endpoints = ['/tx/' + str(index) for index in range(8)]
        result = api.prefetch(endpoints + endpoints[:2])
        self.assertEqual(list(result), endpoints)
        self.assertEqual(maximum, 4)
        self.assertCountEqual(calls, endpoints)
        self.assertEqual(len(set(pair[1] for pair in result.values())), 8)
        self.assertEqual(len(list(self.store.observations(api.used))), 8)

    def test_parallel_gets_share_success_and_observation(self):
        calls = []
        ready = threading.Barrier(8)

        def transport(*args):
            calls.append(1)
            time.sleep(.02)
            return 200, {}, b'{"value":7}'

        api = self.client(transport)

        def fetch():
            ready.wait(timeout=3)
            return api.get('/tx/same')

        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(lambda _: fetch(), range(8)))
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(api.get('/tx/same'), results[0])
        self.assertEqual(api.budget.requests, 1)

    def test_parallel_gets_share_failure_without_new_request(self):
        calls = []
        ready = threading.Barrier(8)

        def transport(*args):
            calls.append(1)
            time.sleep(.02)
            raise TraceError('Network request failed: TimeoutError')

        api = self.client(transport)

        def fetch():
            ready.wait(timeout=3)
            try:
                api.get('/tx/same')
            except TraceError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(lambda _: fetch(), range(8)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(set(results)), 1)
        result = api.prefetch(['/tx/same', '/tx/same'])
        self.assertIsInstance(result['/tx/same'], TraceError)
        self.assertEqual(len(calls), 1)
        rows = self.store.db.execute('SELECT status FROM attempts ORDER BY id').fetchall()
        self.assertEqual([row['status'] for row in rows], ['started', 'network_error'])

    def test_request_budget_is_exact_under_concurrency(self):
        calls = []

        def transport(*args):
            calls.append(1)
            time.sleep(.01)
            return 200, {}, b'{}'

        api = self.client(transport, limits=Limits(max_requests=3), workers=8)
        result = api.prefetch(['/tx/' + str(index) for index in range(30)])
        self.assertEqual(len(calls), 3)
        self.assertEqual(api.budget.requests, 3)
        self.assertEqual(sum(isinstance(value, StopRun) for value in result.values()), 27)
        self.assertEqual(len(api.used), 3)

    def test_budget_request_atomic_independently_of_client_gate(self):
        budget = Budget(Limits(max_requests=3))

        def reserve(_):
            try:
                budget.request()
                return True
            except StopRun:
                return False

        with ThreadPoolExecutor(max_workers=8) as workers:
            successes = list(workers.map(reserve, range(100)))
        self.assertEqual(sum(successes), 3)
        self.assertEqual(budget.requests, 3)

    def test_shared_start_rate_is_95_percent_of_configured_allowance(self):
        lock, starts = threading.Lock(), []

        def transport(*args):
            with lock:
                starts.append(time.monotonic())
            time.sleep(.03)
            return 200, {}, b'{}'

        api = self.client(transport, advertised_rps=50, workers=8)
        api.prefetch(['/tx/' + str(index) for index in range(8)])
        self.assertEqual(api.advertised_rps, 50)
        self.assertEqual(api.effective_rps, 47.5)
        self.assertEqual(api.rate_limit_source, 'advertised')
        # Allow scheduling noise, but a per-worker limiter would start requests
        # together and fail by roughly the entire 21ms shared interval.
        self.assertTrue(all(b - a >= api.min_interval - .004
                            for a, b in zip(starts, starts[1:])), starts)

    def test_enterprise_operating_target_is_49_shared_across_workers(self):
        lock, starts = threading.Lock(), []

        def transport(*args):
            with lock:
                starts.append(time.monotonic())
            time.sleep(.03)
            return 200, {}, b'{}'

        with patch.dict(os.environ, {'LIQUID_BLOCKSTREAM_ENTERPRISE_RPS': '49'}, clear=True):
            api = self.client(transport, advertised_rps=None, workers=8)
            api.prefetch(['/tx/' + str(index) for index in range(8)])
        self.assertIsNone(api.advertised_rps)
        self.assertAlmostEqual(api.effective_rps, 49)
        self.assertAlmostEqual(api.min_interval, 1 / 49)
        self.assertEqual(api.rate_limit_source, 'enterprise_target')
        self.assertTrue(all(b - a >= api.min_interval - .004
                            for a, b in zip(starts, starts[1:])), starts)

    def test_single_initial_oauth_and_single_generation_refresh(self):
        lock = threading.Lock()
        expired = threading.Barrier(4)
        token_calls = 0
        gets = []

        def transport(method, url, headers, body, timeout):
            nonlocal token_calls
            if method == 'POST':
                with lock:
                    token_calls += 1
                    token = 'private-token-' + str(token_calls)
                return 200, {}, canonical({'access_token': token, 'expires_in': 300})
            with lock:
                gets.append(headers['Authorization'])
            if headers['Authorization'] == 'Bearer private-token-1':
                expired.wait(timeout=3)
                return 401, {}, b'{}'
            return 200, {}, b'{}'

        with patch.dict(os.environ, {'BLOCKSTREAM_CLIENT_ID': 'private-client',
                                     'BLOCKSTREAM_CLIENT_SECRET': 'private-secret'}):
            api = self.client(transport, auth='blockstream', workers=4)
            result = api.prefetch(['/tx/' + str(index) for index in range(4)])
        self.assertTrue(all(isinstance(value, tuple) for value in result.values()))
        self.assertEqual(token_calls, 2)
        self.assertEqual(api.budget.requests, 10)
        self.assertEqual(gets.count('Bearer private-token-1'), 4)
        self.assertEqual(gets.count('Bearer private-token-2'), 4)
        self.assertEqual(len(api.used), 8)
        archive = (self.store.case / 'evidence.sqlite').read_bytes()
        for secret in (b'private-client', b'private-secret', b'private-token'):
            self.assertNotIn(secret, archive)

    def test_oauth_failure_is_shared_across_endpoints(self):
        calls = []

        def transport(*args):
            calls.append(1)
            return 401, {}, b'{"secret":"not archived"}'

        with patch.dict(os.environ, {'BLOCKSTREAM_CLIENT_ID': 'private-client',
                                     'BLOCKSTREAM_CLIENT_SECRET': 'private-secret'}):
            api = self.client(transport, auth='blockstream')
            result = api.prefetch(['/tx/' + str(index) for index in range(8)])
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(isinstance(value, TraceError) for value in result.values()))
        self.assertEqual(len(api.used), 0)

    def test_global_429_cooldown_blocks_other_endpoints(self):
        lock, calls = threading.Lock(), []
        limited_at = None

        def transport(method, url, headers, body, timeout):
            nonlocal limited_at
            with lock:
                calls.append((url, time.monotonic()))
                if len(calls) == 1:
                    limited_at = calls[-1][1]
                    return 429, {'Retry-After': '.06'}, b'{}'
            return 200, {}, b'{}'

        api = self.client(transport, advertised_rps=200, workers=4)
        # Keep the test short while testing the production shared cooldown gate.
        with patch.object(Esplora, '_retry_delay', return_value=.06):
            result = api.prefetch(['/tx/' + str(index) for index in range(4)])
        self.assertTrue(all(isinstance(value, tuple) for value in result.values()))
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(start >= limited_at + .05 for _, start in calls[1:]), calls)
        self.assertEqual(len(api.used), 5)

    def test_long_429_cooldown_archives_response_and_stops_new_requests(self):
        calls = []

        def transport(*args):
            calls.append(1)
            return 429, {'Retry-After': '120'}, b'{"retry":true}'

        api = self.client(transport, advertised_rps=50, workers=4)
        result = api.prefetch(['/tx/' + str(index) for index in range(12)])
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(isinstance(value, StopRun) for value in result.values()))
        observations = list(self.store.observations(api.used))
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]['status'], 429)
        self.assertEqual(observations[0]['body'], b'{"retry":true}')

    def test_elapsed_limit_waiting_for_gate_does_not_start_extra_requests(self):
        calls = []

        def transport(*args):
            calls.append(1)
            time.sleep(.015)
            return 200, {}, b'{}'

        api = self.client(transport, advertised_rps=1, limits=Limits(max_seconds=.05))
        result = api.prefetch(['/tx/' + str(index) for index in range(10)])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(api.used), 1)
        self.assertEqual(sum(isinstance(value, StopRun) for value in result.values()), 9)

    def test_new_run_refreshes_outspends_and_unconfirmed_but_reuses_confirmed(self):
        calls = []

        def transport(method, url, *args):
            endpoint = url.removeprefix(ENTERPRISE)
            calls.append(endpoint)
            if endpoint.endswith('/outspends'):
                return 200, {}, b'[{"spent":false}]'
            return 200, {}, canonical({'status': {'confirmed': endpoint == '/tx/confirmed'}})

        endpoints = ['/tx/confirmed', '/tx/unconfirmed', '/tx/confirmed/outspends']
        first = self.client(transport, run_id='first')
        first.prefetch(endpoints)
        first.close()
        second = self.client(transport, run_id='second')
        second.prefetch(endpoints + endpoints)
        self.assertEqual(calls.count('/tx/confirmed'), 1)
        self.assertEqual(calls.count('/tx/unconfirmed'), 2)
        self.assertEqual(calls.count('/tx/confirmed/outspends'), 2)
        self.assertEqual(len(second.used), 3)
        self.assertEqual(second.budget.requests, 2)

    def test_failure_drains_other_responses_before_return(self):
        completed = threading.Event()

        def transport(method, url, *args):
            if url.endswith('/fail'):
                return 404, {}, b'{}'
            time.sleep(.02)
            completed.set()
            return 200, {}, b'{"complete":true}'

        api = self.client(transport, workers=2)
        result = api.prefetch(['/tx/fail', '/tx/success'])
        self.assertIsInstance(result['/tx/fail'], TraceError)
        self.assertTrue(completed.is_set())
        self.assertEqual(len(api.used), 2)
        self.assertEqual(len(list(self.store.observations(api.used))), 2)

    def test_keyboard_interrupt_drains_started_response_before_raising(self):
        started, release = threading.Event(), threading.Event()

        def transport(*args):
            started.set()
            release.wait(timeout=3)
            return 200, {}, b'{"complete":true}'

        api = self.client(transport, workers=1)

        def interrupted_wait(*args, **kwargs):
            self.assertTrue(started.wait(timeout=3))
            release.set()
            raise KeyboardInterrupt()

        with patch('liquid_tracer.api.wait', side_effect=interrupted_wait):
            with self.assertRaises(KeyboardInterrupt):
                api.prefetch(['/tx/started', '/tx/queued'])
        self.assertEqual(api.budget.requests, 1)
        self.assertEqual(len(api.used), 1)
        self.assertEqual(len(list(self.store.observations(api.used))), 1)

    def test_fixture_bypasses_network_rate_and_remains_deduplicated(self):
        path = Path(self.temp.name) / 'fixture.json'
        path.write_text(json.dumps({'/tx/one': {}, '/tx/two': {}}))
        api = self.client(lambda *args: self.fail('No network for fixtures'),
                          fixture=path, advertised_rps=.00001)
        result = api.prefetch(['/tx/one', '/tx/two', '/tx/one'])
        self.assertEqual(api.min_interval, 0)
        self.assertEqual(len(result), 2)
        self.assertEqual(api.budget.requests, 2)

    def test_rate_defaults_environment_and_validation(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(default_min_interval(), 1 / 49)
            api = Esplora(self.store, 'default', Limits(), auth='none', min_interval=0)
            self.clients.append(api)
            self.assertIsNone(api.advertised_rps)
            self.assertAlmostEqual(api.effective_rps, 49)
            self.assertEqual(api.min_interval, 1 / 49)
            self.assertEqual(api.rate_limit_source, 'enterprise_target')
            slower = self.client(lambda *args: None, advertised_rps=None, min_interval=.25)
            self.assertEqual(slower.effective_rps, 4)
        with patch.dict(os.environ, {'LIQUID_BLOCKSTREAM_ENTERPRISE_RPS': '25'}, clear=True):
            self.assertEqual(default_min_interval(), 1 / 25)
            self.assertEqual(default_min_interval('https://enterprise.blockstream.info/liquidtestnet/api'), 1 / 25)
            for base in ('https://blockstream.info/liquid/api', 'https://other.example/liquid/api'):
                self.assertEqual(default_min_interval(base), .25)
                public = self.client(lambda *args: None, base=base, advertised_rps=None)
                self.assertEqual(public.effective_rps, 4)
                self.assertEqual(public.rate_limit_source, 'conservative_default')
            self.assertAlmostEqual(default_min_interval(advertised_rps=50), 1 / 47.5)
        with patch.dict(os.environ, {'LIQUID_BLOCKSTREAM_API_RPS': '20'}):
            self.assertAlmostEqual(default_min_interval(), 1 / 19)
        for rate in (0, -1, 'invalid', float('nan'), float('inf')):
            with self.subTest(rate=rate), self.assertRaises(TraceError):
                Esplora(self.store, 'bad', Limits(), advertised_rps=rate)
            with patch.dict(os.environ, {'LIQUID_BLOCKSTREAM_ENTERPRISE_RPS': str(rate)}, clear=True):
                with self.subTest(enterprise_target=rate), self.assertRaises(TraceError):
                    self.client(lambda *args: self.fail('Invalid rate must not send a request'), advertised_rps=None)
        for workers in (0, 9, True, 1.5):
            with self.subTest(workers=workers), self.assertRaises(TraceError):
                self.client(lambda *args: None, workers=workers)

    def test_close_is_idempotent_and_prevents_new_work(self):
        api = self.client(lambda *args: (200, {}, b'{}'))
        api.prefetch(['/tx/one'])
        api.close()
        api.close()
        with self.assertRaises(TraceError):
            api.get('/tx/two')
        with self.assertRaises(TraceError):
            api.prefetch(['/tx/two'])

    def test_completed_endpoint_cache_still_checks_elapsed_budget(self):
        calls = []

        def transport(*args):
            calls.append(1)
            return 200, {}, b'{}'

        api = self.client(transport, limits=Limits(max_seconds=10))
        api.get('/tx/one')
        with patch('liquid_tracer.api.time.monotonic', return_value=api.budget.started + 11):
            with self.assertRaisesRegex(StopRun, 'time_limit'):
                api.get('/tx/one')
        self.assertEqual(len(calls), 1)

    def test_close_drains_get_started_by_external_caller(self):
        started, release, closing = threading.Event(), threading.Event(), threading.Event()

        def transport(*args):
            started.set()
            release.wait(timeout=3)
            return 200, {}, b'{"complete":true}'

        api = self.client(transport)

        def close():
            closing.set()
            api.close()

        with ThreadPoolExecutor(max_workers=2) as workers:
            fetching = workers.submit(api.get, '/tx/one')
            self.assertTrue(started.wait(timeout=3))
            closed = workers.submit(close)
            self.assertTrue(closing.wait(timeout=3))
            time.sleep(.01)
            self.assertFalse(closed.done())
            release.set()
            closed.result(timeout=3)
            self.assertEqual(fetching.result(timeout=3)[0], {'complete': True})
        self.assertEqual(len(list(self.store.observations(api.used))), 1)

    def test_retry_attempts_are_archived_once_per_endpoint_cohort(self):
        calls = []

        def transport(*args):
            calls.append(1)
            return 503, {}, b'{"retry":true}'

        api = self.client(transport)
        with patch.object(api.budget, 'pause'):
            results = api.prefetch(['/tx/one'] * 8)
            with self.assertRaisesRegex(TraceError, 'retries exhausted'):
                api.get('/tx/one')
        self.assertIsInstance(results['/tx/one'], TraceError)
        self.assertEqual(len(calls), 4)
        self.assertEqual(api.budget.requests, 4)
        self.assertEqual(len(list(self.store.observations(api.used))), 4)
        attempts = self.store.db.execute('SELECT status FROM attempts ORDER BY id').fetchall()
        self.assertEqual([row['status'] for row in attempts], ['started', '503'] * 4)


if __name__ == '__main__':
    unittest.main()
