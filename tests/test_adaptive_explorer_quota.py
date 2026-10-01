"""Adaptive throughput probes, pressure feedback, and legacy-client coexistence."""

import multiprocessing
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from liquid_tracer.explorer_quota import (
    ADAPTIVE_INITIAL_RPS, Admission, LocalExplorerQuota, SharedExplorerQuota,
)


ENDPOINT = 'https://example.test/liquid/api'


def _read_learned_target(directory, now, results):
    with SharedExplorerQuota(ENDPOINT, 1 / 49, adaptive=True, directory=directory,
                             clock=lambda: now) as quota:
        result = quota.reserve()
        results.put((result.target_rps, result.generation, result.effective_rps))


class AdaptiveQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = [100.]
        self.clients = []

    def tearDown(self):
        for quota in self.clients:
            quota.close()
        self.temp.cleanup()

    def quota(self, *, local=False, adaptive=True, rate=ADAPTIVE_INITIAL_RPS):
        options = dict(adaptive=adaptive, clock=lambda: self.clock[0])
        cls = LocalExplorerQuota if local else SharedExplorerQuota
        args = (1 / rate,) if local else (ENDPOINT, 1 / rate)
        if not local:
            options['directory'] = self.temp.name
        quota = cls(*args, **options)
        self.clients.append(quota)
        return quota

    def admit(self, quota):
        for _ in range(10):
            admission = quota.reserve()
            if admission.admitted:
                return admission
            self.clock[0] += admission.wait_seconds + 1e-8
        self.fail('Quota did not admit after its own wait')

    def pump(self, quota, seconds):
        until = self.clock[0] + seconds
        while self.clock[0] < until:
            admission = self.admit(quota)
            quota.success(admission)
        return admission

    def test_successful_demand_grows_beyond_warmup_without_fixed_49_cap(self):
        for local in (False, True):
            with self.subTest(local=local):
                quota = self.quota(local=local)
                self.assertAlmostEqual(self.admit(quota).target_rps, 49.)
                admission = self.pump(quota, 8.2)
                self.assertGreater(admission.target_rps, 200.)
                self.assertEqual(admission.mode, 'adaptive')
                self.assertEqual(admission.generation, 0)
                quota.close()

    def test_no_success_or_insufficient_demand_does_not_inflate_target(self):
        for local in (False, True):
            with self.subTest(local=local):
                quota = self.quota(local=local)
                admission = self.admit(quota)
                for _ in range(20):
                    self.clock[0] += 3.
                    quota.success(admission)
                    admission = self.admit(quota)
                self.assertAlmostEqual(admission.target_rps, 49.)
                quota.close()

    def test_pressure_from_before_growth_reduces_once_and_honors_retry_after(self):
        for local in (False, True):
            with self.subTest(local=local):
                quota = self.quota(local=local)
                earlier = self.admit(quota)
                grown = self.pump(quota, 2.2)
                self.assertGreater(grown.target_rps, earlier.target_rps)
                self.assertTrue(quota.pressure(earlier, 120.))
                denied = quota.reserve()
                self.assertFalse(denied.admitted)
                self.assertAlmostEqual(denied.wait_seconds, 120.)
                self.assertAlmostEqual(denied.target_rps, grown.target_rps * .7)
                self.assertEqual(denied.generation, earlier.generation + 1)
                for _ in range(20):
                    self.assertTrue(quota.pressure(earlier, 1.))
                again = quota.reserve()
                self.assertEqual(again.target_rps, denied.target_rps)
                self.assertAlmostEqual(again.wait_seconds, 120.)
                self.clock[0] += 120.
                self.assertTrue(quota.reserve().admitted)
                quota.close()

    def test_late_pre_pressure_successes_cannot_trigger_recovery_growth(self):
        for local in (False, True):
            with self.subTest(local=local):
                quota = self.quota(local=local)
                earlier = self.admit(quota)
                quota.pressure(earlier, 1.)
                self.clock[0] += 16.
                for _ in range(1000):
                    quota.success(earlier)
                current = self.admit(quota)
                self.assertAlmostEqual(current.target_rps, 49 * .7)
                # Productive traffic eventually probes again, but more slowly
                # than initial exploration instead of re-entering a retry storm.
                grown = self.pump(quota, 10.2)
                self.assertAlmostEqual(grown.target_rps, current.target_rps * 1.05)
                quota.close()

    def test_fixed_peer_constrains_adaptive_clients_then_close_releases(self):
        adaptive = self.quota()
        grown = self.pump(adaptive, 4.2)
        fixed = self.quota(adaptive=False, rate=10.)
        self.assertEqual(fixed.reserve().effective_rps, 10.)
        admission = adaptive.reserve()
        self.assertEqual(admission.effective_rps, 10.)
        self.assertEqual(admission.target_rps, grown.target_rps)
        fixed.close()
        self.assertAlmostEqual(adaptive.reserve().effective_rps, grown.target_rps)

    def test_legacy_schema_peer_remains_fixed_and_reads_updated_adaptive_rate(self):
        quota = self.quota()
        self.admit(quota)
        # Simulate an old binary: knows only the original clients and pacing
        # tables, including its original INSERT without named columns.
        with sqlite3.connect(quota.path) as db:
            db.execute('INSERT INTO clients VALUES (?, ?, ?)', ('old-worker', .1, 200.))
        self.assertEqual(quota.reserve().effective_rps, 10.)
        with sqlite3.connect(quota.path) as db:
            db.execute('DELETE FROM clients WHERE id = ?', ('old-worker',))
        grown = self.pump(quota, 4.2)
        with sqlite3.connect(quota.path) as db:
            legacy_interval = db.execute('SELECT MAX(interval) FROM clients').fetchone()[0]
        self.assertAlmostEqual(1 / legacy_interval, grown.target_rps)

    def test_adaptive_peers_share_target_pressure_epoch_and_lease_refresh(self):
        first, second = self.quota(), self.quota()
        old_first = self.admit(first)
        old_second = self.admit(second)
        first.pressure(old_first, 4.)
        second.pressure(old_second, 2.)
        admission = second.reserve()
        self.assertEqual(admission.generation, 1)
        self.assertAlmostEqual(admission.target_rps, 49 * .7)
        self.assertEqual(admission.wait_seconds, 4.)
        self.assertEqual(admission.active_clients, 2)
        first.close()
        self.clock[0] += 31.
        self.assertEqual(second.reserve().active_clients, 1)

    def test_learned_target_survives_close_and_is_shared_across_processes(self):
        quota = self.quota()
        grown = self.pump(quota, 4.2)
        quota.close()
        self.clock[0] += 100.
        context = multiprocessing.get_context('spawn')
        results = context.Queue()
        worker = context.Process(target=_read_learned_target,
                                 args=(self.temp.name, self.clock[0], results))
        try:
            worker.start()
            learned, generation, effective = results.get(timeout=10)
            worker.join(5)
            self.assertEqual(worker.exitcode, 0)
            self.assertEqual(learned, grown.target_rps)
            self.assertEqual(generation, grown.generation)
            self.assertAlmostEqual(effective, learned)
        finally:
            if worker.is_alive():
                worker.terminate()
            worker.join(5)
            results.close()

    def test_success_feedback_adds_no_transaction_and_flushes_with_next_reservation(self):
        quota = self.quota()
        transactions = []
        original = quota._transaction

        @contextmanager
        def tracked():
            transactions.append(1)
            with original() as db:
                yield db

        quota._transaction = tracked
        admitted = self.admit(quota)
        before = len(transactions)
        for _ in range(100):
            quota.success(admitted)
        self.assertEqual(len(transactions), before)
        self.clock[0] += 2.1
        self.admit(quota)
        self.assertEqual(len(transactions), before + 1)
        with sqlite3.connect(quota.path) as db:
            self.assertGreater(db.execute('SELECT target_rps FROM adaptive_pacing').fetchone()[0], 49.)

    def test_contended_pressure_and_reservation_preserve_feedback(self):
        quota = self.quota()
        admitted = self.admit(quota)
        for _ in range(100):
            quota.success(admitted)
        self.clock[0] += 2.1
        with sqlite3.connect(quota.path) as db:
            db.execute('BEGIN IMMEDIATE')
            self.assertFalse(quota.pressure(admitted, 1.))
            self.assertFalse(quota.reserve().admitted)
        grown = self.admit(quota)
        self.assertGreater(grown.target_rps, 49.)
        self.assertTrue(quota.pressure(admitted, 1.))
        self.assertAlmostEqual(quota.reserve().target_rps, grown.target_rps * .7)

    def test_admission_defaults_preserve_old_positional_construction(self):
        admission = Admission(True, 0., 2, 49., '')
        self.assertEqual(admission.mode, 'fixed')
        self.assertEqual(admission.generation, 0)

    def test_fixed_mode_ignores_feedback_and_preserves_explicit_rate(self):
        for local in (False, True):
            with self.subTest(local=local):
                quota = self.quota(local=local, adaptive=False, rate=123.)
                admission = self.pump(quota, 2.1)
                self.assertAlmostEqual(admission.effective_rps, 123.)
                quota.pressure(admission, 3.)
                denied = quota.reserve()
                self.assertAlmostEqual(denied.effective_rps, 123.)
                self.assertEqual(denied.wait_seconds, 3.)
                quota.close()


if __name__ == '__main__':
    unittest.main()
