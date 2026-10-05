"""Quota storage keeps durable coordination efficient and process-safe."""

import multiprocessing
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.explorer_quota import SharedExplorerQuota, _wal_runtime_safe


ENDPOINT = 'https://example.test/liquid/api'


def _exit_with_cooldown(directory, ready):
    quota = SharedExplorerQuota(ENDPOINT, .001, directory=directory, adaptive=True)
    while not quota.cooldown(60.):
        time.sleep(.005)
    ready.send(time.time())
    ready.close()
    os._exit(0)


class QuotaStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.temp.cleanup()

    def quota(self, **options):
        quota = SharedExplorerQuota(ENDPOINT, .001, directory=self.temp.name,
                                    adaptive=True, **options)
        self.clients.append(quota)
        return quota

    def test_wal_version_gate_includes_documented_backports_only(self):
        for version in ((3, 51, 3), (3, 52, 0), (3, 44, 6), (3, 44, 7), (3, 50, 7), (3, 50, 8)):
            self.assertTrue(_wal_runtime_safe(version), version)
        for version in ((3, 51, 2), (3, 50, 6), (3, 49, 9), (3, 45, 9), (3, 44, 5), (3, 43, 99)):
            self.assertFalse(_wal_runtime_safe(version), version)

    def test_connection_is_reused_and_full_durability_is_retained(self):
        quota = self.quota()
        original = sqlite3.connect
        with patch('liquid_tracer.explorer_quota.sqlite3.connect', wraps=original) as connect:
            self.assertTrue(quota.reserve().admitted)
            first = quota._connection
            for _ in range(10):
                quota.cooldown(0.)
            self.assertIs(quota._connection, first)
            self.assertEqual(connect.call_count, 1)
            self.assertEqual(first.execute('PRAGMA synchronous').fetchone()[0], 2)
            self.assertEqual(first.execute('PRAGMA journal_mode').fetchone()[0],
                             'wal' if _wal_runtime_safe() else 'delete')
            if _wal_runtime_safe():
                self.assertEqual(first.execute('PRAGMA wal_autocheckpoint').fetchone()[0], 1000)
            metrics = quota.storage_metrics()
            self.assertEqual(metrics['quota_sqlite_version'], sqlite3.sqlite_version)
            self.assertEqual(metrics['quota_connection_mode'], 'persistent')
            self.assertEqual(metrics['quota_synchronous'], 'full')
        quota.close()
        self.assertIsNone(quota._connection)
        with self.assertRaises(sqlite3.ProgrammingError):
            first.execute('SELECT 1')

    def test_older_runtime_keeps_delete_full_for_new_database(self):
        with patch('liquid_tracer.explorer_quota.sqlite3.sqlite_version_info', (3, 50, 6)), \
             patch('liquid_tracer.explorer_quota.sqlite3.sqlite_version', '3.50.6'):
            quota = self.quota()
        self.assertTrue(quota.reserve().admitted)
        self.assertEqual(quota._connection.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
        self.assertEqual(quota._connection.execute('PRAGMA synchronous').fetchone()[0], 2)
        self.assertEqual(quota.storage_metrics()['quota_journal_mode_requested'], 'delete')
        self.assertEqual(quota.storage_metrics()['quota_sqlite_version'], '3.50.6')

    @unittest.skipUnless(_wal_runtime_safe(), 'Runtime intentionally retains rollback journaling')
    def test_older_runtime_converts_idle_wal_without_losing_cooldown(self):
        first = self.quota()
        first.cooldown(120.)
        first.close()
        with patch('liquid_tracer.explorer_quota.sqlite3.sqlite_version_info', (3, 51, 2)):
            old = self.quota()
        denied = old.reserve()
        self.assertFalse(denied.admitted)
        self.assertGreater(denied.wait_seconds, 119.)
        self.assertEqual(old.storage_metrics()['quota_journal_mode'], 'delete')

    @unittest.skipUnless(_wal_runtime_safe(), 'Runtime intentionally retains rollback journaling')
    def test_older_runtime_busy_wal_conversion_has_explicit_actionable_error(self):
        first = self.quota()
        first.reserve()
        with patch('liquid_tracer.explorer_quota.sqlite3.sqlite_version_info', (3, 51, 2)), \
             patch('liquid_tracer.explorer_quota.sqlite3.sqlite_version', '3.51.2'):
            old = self.quota()
        started = time.monotonic()
        with self.assertRaisesRegex(TraceError, 'SQLite 3.51.2.*Stop other Liquid Tracer'):
            old.reserve()
        self.assertLess(time.monotonic() - started, .3)
        self.assertIsNone(old._connection)
        first.close()
        old.reserve()
        self.assertEqual(old.storage_metrics()['quota_journal_mode'], 'delete')

    @unittest.skipUnless(_wal_runtime_safe(), 'Runtime intentionally retains rollback journaling')
    def test_contended_wal_migration_returns_short_cancellable_wait(self):
        quota = self.quota()
        with sqlite3.connect(quota.path) as legacy:
            legacy.execute('CREATE TABLE legacy_marker(value INTEGER)')
            legacy.execute('BEGIN IMMEDIATE')
            started = time.monotonic()
            denied = quota.reserve()
            self.assertFalse(denied.admitted)
            self.assertLess(time.monotonic() - started, .3)
            self.assertIsNone(quota._connection)
        self.assertTrue(quota.reserve().admitted)
        self.assertEqual(quota.storage_metrics()['quota_journal_mode'], 'wal')

    def test_rollback_leaves_persistent_connection_reusable(self):
        quota = self.quota()
        quota.reserve()
        connection = quota._connection
        with self.assertRaisesRegex(RuntimeError, 'rollback marker'):
            with quota._transaction() as db:
                db.execute("INSERT INTO clients VALUES ('rolled-back', .5, 10000000000)")
                raise RuntimeError('rollback marker')
        self.assertIs(connection, quota._connection)
        self.assertFalse(connection.in_transaction)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM clients WHERE id='rolled-back'").fetchone()[0], 0)
        self.assertTrue(quota.cooldown(0.))

    def test_close_during_local_transaction_is_bounded_and_eventually_closes_connection(self):
        quota = self.quota()
        quota.reserve()
        entered, release = threading.Event(), threading.Event()
        errors = []

        def transaction():
            try:
                with quota._transaction():
                    entered.set()
                    release.wait(3)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=transaction)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            started = time.monotonic()
            quota.close()
            self.assertLess(time.monotonic() - started, .3)
            with self.assertRaisesRegex(TraceError, 'closed'):
                quota.reserve()
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNone(quota._connection)

    def test_connection_cannot_be_reused_after_fork(self):
        quota = self.quota()
        quota.reserve()
        admission = quota.reserve()
        # The check precedes every mutex, including feedback/hint paths. A
        # fork can inherit a lock held by a thread which no longer exists.
        with quota._feedback_lock, \
             patch('liquid_tracer.explorer_quota.os.getpid', return_value=os.getpid() + 1):
            for operation in (quota.reserve, quota.close,
                              lambda: quota.success(admission),
                              lambda: quota.pressure(admission, 1.),
                              lambda: quota.cooldown(1.)):
                with self.assertRaisesRegex(TraceError, 'after forking'):
                    operation()
        self.assertTrue(quota.cooldown(0.))

    def test_hard_exit_preserves_committed_cooldown(self):
        context = multiprocessing.get_context('spawn')
        receiving, sending = context.Pipe(duplex=False)
        process = context.Process(target=_exit_with_cooldown, args=(self.temp.name, sending))
        process.start()
        sending.close()
        try:
            self.assertTrue(receiving.poll(10))
            published = receiving.recv()
            process.join(5)
            self.assertEqual(process.exitcode, 0)
            quota = self.quota()
            denied = quota.reserve()
            self.assertFalse(denied.admitted)
            self.assertEqual(denied.reason, 'server_cooldown')
            self.assertGreater(denied.wait_seconds, 59. - (time.time() - published))
        finally:
            receiving.close()
            if process.is_alive():
                process.terminate()
            process.join(5)

    def test_symlink_sidecars_are_rejected_before_opening_sqlite(self):
        for suffix in ('-wal', '-shm', '-journal'):
            with self.subTest(suffix=suffix):
                quota = self.quota()
                target = Path(self.temp.name) / ('outside' + suffix)
                target.write_bytes(b'unchanged')
                sidecar = Path(str(quota.path) + suffix)
                sidecar.symlink_to(target)
                with self.assertRaises(TraceError):
                    quota.reserve()
                self.assertEqual(target.read_bytes(), b'unchanged')
                self.assertIsNone(quota._connection)
                sidecar.unlink()
                quota.close()

    def test_quota_and_wal_sidecars_are_private(self):
        quota = self.quota()
        quota.reserve()
        for suffix in ('', '-wal', '-shm'):
            path = Path(str(quota.path) + suffix)
            if path.exists():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.stat().st_uid, os.getuid())
        self.assertEqual(quota.path.parent.stat().st_mode & 0o777, 0o700)


if __name__ == '__main__':
    unittest.main()
