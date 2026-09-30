"""Shared pacing is exercised across real processes without network access."""

import multiprocessing
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.api import ENTERPRISE, Esplora, Limits
from liquid_tracer.common import StopRun, TraceError
from liquid_tracer.explorer_quota import SharedExplorerQuota
from liquid_tracer.store import Store


def _pace_process(directory, start, results, interval, count):
    with SharedExplorerQuota(ENTERPRISE, interval, directory=directory) as quota:
        start.wait(5)
        for _ in range(count):
            while True:
                admission = quota.reserve()
                if admission.admitted:
                    results.put(time.time())
                    break
                time.sleep(min(admission.wait_seconds, .01))


def _cooldown_process(directory, results):
    with SharedExplorerQuota(ENTERPRISE, .001, directory=directory) as quota:
        while not quota.cooldown(.3):
            time.sleep(.005)
        results.put(time.time())


def _abandoned_process(directory, results):
    quota = SharedExplorerQuota(ENTERPRISE, .08, directory=directory, idle_seconds=.3)
    while not quota.reserve().admitted:
        time.sleep(.005)
    results.put(time.time())
    # Deliberately do not close. This models a client killed after admission.


class ExplorerQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clients = []

    def tearDown(self):
        for quota in self.clients:
            quota.close()
        self.temp.cleanup()

    def quota(self, interval=.02, **kwargs):
        quota = SharedExplorerQuota(kwargs.pop('endpoint', ENTERPRISE), interval,
                                    directory=self.temp.name, **kwargs)
        self.clients.append(quota)
        return quota

    def test_host_identity_ignores_network_path_and_never_stores_urls(self):
        first = self.quota(endpoint='https://EXAMPLE.test/liquid/api')
        second = self.quota(endpoint='https://example.test/liquidtestnet/api')
        other = self.quota(endpoint='https://other.test/liquid/api')
        self.assertEqual(first.path, second.path)
        self.assertNotEqual(first.path, other.path)
        self.assertTrue(first.reserve().admitted)
        self.assertFalse(second.reserve().admitted)
        raw = first.path.read_bytes()
        self.assertNotIn(b'example.test', raw)
        self.assertNotIn(b'/liquid/api', raw)
        self.assertEqual(first.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(first.path.parent.stat().st_mode & 0o777, 0o700)
        for endpoint in ('https://user:secret@example.test/api', 'https://example.test/api?key=secret'):
            with self.assertRaises(TraceError):
                self.quota(endpoint=endpoint)

    def test_slowest_active_target_applies_to_faster_client_and_close_releases(self):
        clock = [100.]
        fast = self.quota(.01, clock=lambda: clock[0])
        slow = self.quota(.25, clock=lambda: clock[0])
        self.assertTrue(fast.reserve().admitted)
        clock[0] += .01
        joined = slow.reserve()
        self.assertFalse(joined.admitted)
        self.assertAlmostEqual(joined.wait_seconds, .24)
        self.assertEqual(joined.active_clients, 2)
        self.assertEqual(joined.effective_rps, 4)
        self.assertAlmostEqual(fast.reserve().wait_seconds, .24)
        slow.close()
        self.assertTrue(fast.reserve().admitted)
        self.assertEqual(fast.reserve().active_clients, 1)
        slow.close()

    def test_waiters_renew_lease_and_idle_clients_expire(self):
        clock = [100.]
        slow = self.quota(2., idle_seconds=1., clock=lambda: clock[0])
        fast = self.quota(.01, idle_seconds=1., clock=lambda: clock[0])
        self.assertTrue(slow.reserve().admitted)
        for _ in range(5):
            clock[0] += .3
            self.assertFalse(slow.reserve().admitted)
            self.assertEqual(fast.reserve().effective_rps, .5)
        clock[0] += 1.01
        admission = fast.reserve()
        self.assertTrue(admission.admitted)
        self.assertEqual(admission.effective_rps, 100)
        self.assertEqual(admission.active_clients, 1)
        # An expired client cannot send until its own slower policy is restored.
        self.assertFalse(slow.reserve().admitted)
        self.assertEqual(fast.reserve().effective_rps, .5)

    def test_cooldown_survives_client_close_and_merges_conservatively(self):
        clock = [100.]
        first = self.quota(clock=lambda: clock[0])
        second = self.quota(clock=lambda: clock[0])
        self.assertTrue(first.cooldown(120.))
        self.assertTrue(second.cooldown(1.))
        first.close()
        admission = second.reserve()
        self.assertFalse(admission.admitted)
        self.assertEqual(admission.wait_seconds, 120.)
        self.assertEqual(admission.reason, 'server_cooldown')
        clock[0] += 120.
        self.assertTrue(second.reserve().admitted)

    def test_contended_database_returns_short_wait_without_admitting(self):
        quota = self.quota()
        self.assertTrue(quota.reserve().admitted)
        with sqlite3.connect(quota.path) as db:
            db.execute('BEGIN IMMEDIATE')
            started = time.monotonic()
            admission = quota.reserve()
            self.assertFalse(admission.admitted)
            self.assertLess(time.monotonic() - started, .3)
            self.assertLessEqual(admission.wait_seconds, .05)
            self.assertFalse(quota.cooldown(1.))
        self.assertTrue(quota.cooldown(1.))

    def test_processes_share_combined_request_spacing(self):
        ctx = multiprocessing.get_context('spawn')
        start, results = ctx.Event(), ctx.Queue()
        processes = [ctx.Process(target=_pace_process,
                                 args=(self.temp.name, start, results, .045, 4)) for _ in range(3)]
        try:
            for process in processes:
                process.start()
            start.set()
            starts = sorted(results.get(timeout=8) for _ in range(12))
            for process in processes:
                process.join(5)
                self.assertEqual(process.exitcode, 0)
            self.assertTrue(all(b - a >= .035 for a, b in zip(starts, starts[1:])), starts)
            self.assertGreaterEqual(starts[-1] - starts[0], .045 * 10)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(5)
            results.close()

    def test_process_cooldown_and_abandoned_lease_recovery(self):
        ctx = multiprocessing.get_context('spawn')
        results = ctx.Queue()
        quota = self.quota(.001)
        process = ctx.Process(target=_cooldown_process, args=(self.temp.name, results))
        process.start()
        published = results.get(timeout=5)
        process.join(5)
        self.assertEqual(process.exitcode, 0)
        admission = quota.reserve()
        self.assertFalse(admission.admitted)
        self.assertEqual(admission.reason, 'server_cooldown')
        while not quota.reserve().admitted:
            time.sleep(.01)
        self.assertGreaterEqual(time.time() - published, .28)
        process = ctx.Process(target=_abandoned_process, args=(self.temp.name, results))
        process.start()
        abandoned = results.get(timeout=5)
        process.join(5)
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(quota.reserve().effective_rps, 12.5)
        while time.time() - abandoned < .32:
            time.sleep(.01)
        admission = quota.reserve()
        self.assertEqual(admission.active_clients, 1)
        self.assertEqual(admission.effective_rps, 1000)
        results.close()


class SharedExplorerApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'case')
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.store.close()
        self.temp.cleanup()

    def client(self, transport=lambda *args: (200, {}, b'{}'), **options):
        quota = SharedExplorerQuota(ENTERPRISE, .001, directory=Path(self.temp.name) / 'quota')
        api = Esplora(self.store, 'run-' + str(len(self.clients)), options.pop('limits', Limits()),
                      auth='none', advertised_rps=1000000, transport=transport,
                      shared_quota=quota, **options)
        self.clients.append(api)
        return api

    def test_injected_quota_updates_safe_metrics_and_enforces_budget_while_waiting(self):
        api = self.client(limits=Limits(max_seconds=.2))
        api._shared_quota.cooldown(.4)
        with self.assertRaisesRegex(StopRun, 'time_limit'):
            api.get('/one')
        self.assertEqual(api.budget.requests, 0)
        metrics = api.request_metrics()
        self.assertEqual(metrics['shared_api_active_clients'], 1)
        self.assertEqual(metrics['shared_api_effective_rps'], 1000)
        self.assertEqual(metrics['shared_api_wait_reason'], 'server_cooldown')
        self.assertGreater(metrics['shared_api_wait_seconds'], .2)

    def test_close_cancels_shared_wait_and_drains_before_unregistering(self):
        api = self.client()
        api._shared_quota.cooldown(10.)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(api.prefetch, ['/one'])
            deadline = time.monotonic() + 3
            while 'shared_api_wait_reason' not in api.request_metrics() and time.monotonic() < deadline:
                time.sleep(.005)
            started = time.monotonic()
            api.close()
            result = pending.result(timeout=2)
        self.assertLess(time.monotonic() - started, 1.)
        self.assertIsInstance(result['/one'], StopRun)
        self.assertEqual(api.budget.requests, 0)
        with sqlite3.connect(api._shared_quota.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM clients').fetchone()[0], 0)

    def test_long_retry_after_is_shared_before_originating_run_stops(self):
        first = self.client(lambda *args: (429, {'Retry-After': '120'}, b'{}'))
        second = self.client()
        with self.assertRaisesRegex(StopRun, 'server_retry_later'):
            first.get('/one')
        self.assertEqual(len(first.used), 1)
        admission = second._shared_quota.reserve()
        self.assertFalse(admission.admitted)
        self.assertGreater(admission.wait_seconds, 119.)
        self.assertEqual(admission.reason, 'server_cooldown')

    def test_pacing_write_failure_keeps_received_response_and_blocks_new_requests(self):
        requests = []

        def transport(*args):
            requests.append(1)
            return 429, {}, b'{"rate_limited":true}'

        api = self.client(transport)
        with patch.object(api._shared_quota, 'cooldown', side_effect=TraceError('quota unavailable')):
            with self.assertRaisesRegex(TraceError, 'quota unavailable'):
                api.get('/one')
        self.assertEqual(len(api.used), 1)
        self.assertEqual(len(list(self.store.observations(api.used))), 1)
        with self.assertRaisesRegex(TraceError, 'quota unavailable'):
            api.get('/two')
        self.assertEqual(requests, [1])

    def test_exhausted_request_budget_does_not_wait_for_shared_cooldown(self):
        api = self.client(limits=Limits(max_requests=1))
        api.get('/one')
        api._shared_quota.cooldown(10.)
        started = time.monotonic()
        with self.assertRaisesRegex(StopRun, 'request_limit'):
            api.get('/two')
        self.assertLess(time.monotonic() - started, .1)

    def test_default_http_is_coordinated_lazily_but_fake_transport_is_isolated(self):
        with patch('liquid_tracer.api.SharedExplorerQuota') as factory:
            fake = Esplora(self.store, 'fake', Limits(), auth='none', transport=lambda *args: (200, {}, b'{}'))
            self.clients.append(fake)
            fake.get('/fake')
            factory.assert_not_called()
            real = Esplora(self.store, 'real', Limits(), auth='none')
            self.clients.append(real)
            factory.assert_not_called()
            real.transport = lambda *args: (200, {}, b'{}')
            actual = SharedExplorerQuota(ENTERPRISE, .001, directory=Path(self.temp.name) / 'real-quota')
            factory.return_value = actual
            real.get('/real')
            factory.assert_called_once_with(ENTERPRISE, 1. / 49)

    def test_prefetch_idle_callback_reports_shared_wait_on_calling_thread(self):
        api = self.client()
        api._shared_quota.cooldown(.35)
        observations = []
        caller = threading.get_ident()

        def idle():
            observations.append((threading.get_ident(), api.request_metrics()))
            raise RuntimeError('Advisory progress cannot abort the request')

        result = api.prefetch(['/one'], on_idle=idle)
        self.assertIsInstance(result['/one'], tuple)
        self.assertTrue(observations)
        self.assertTrue(all(thread == caller for thread, _ in observations))
        self.assertEqual(observations[0][1]['shared_api_wait_reason'], 'server_cooldown')
        self.assertEqual(api.request_metrics()['shared_api_wait_seconds'], 0.)


if __name__ == '__main__':
    unittest.main()
