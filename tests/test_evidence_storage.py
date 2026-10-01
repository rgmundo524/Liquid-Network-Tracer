import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import TraceError, digest
from liquid_tracer.store import Store


SOURCE = 'https://example.invalid/liquid/api'


class EvidenceStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.case = Path(self.temp.name) / 'case'
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.temp.cleanup()

    def store(self, case=None, *, wal=True):
        with patch('liquid_tracer.store.wal_runtime_safe', return_value=wal):
            store = Store(case or self.case)
        self.stores.append(store)
        return store

    def test_existing_delete_history_is_not_migrated_by_opening_or_reading(self):
        first = self.store(wal=False)
        oid = first.observe('old', SOURCE, '/address/one', b'{"value":7}')
        first.close()
        self.stores.remove(first)
        path = self.case / 'evidence.sqlite'
        original = path.read_bytes()
        reader = self.store()
        self.assertEqual(reader.db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
        self.assertEqual(reader.cached(SOURCE, '/address/one', 'old', 0)[0], {'value': 7})
        row = next(reader.observations([oid]))
        self.assertEqual(row['sha256'], digest(b'{"value":7}'))
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.case / 'evidence.sqlite-wal').exists())
        self.assertEqual(reader.storage_metrics()['evidence_commits'], 0)

    def test_first_write_migrates_to_wal_full_without_changing_prior_evidence(self):
        old = self.store(wal=False)
        oid = old.observe('old', SOURCE, '/address/one', b'{"value":7}')
        before = next(old.observations([oid]))
        old.close()
        self.stores.remove(old)
        writer = self.store()
        writer.attempt('new', 'esplora', '/address/two', 'started')
        self.assertEqual(writer.db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        self.assertEqual(writer.db.execute('PRAGMA synchronous').fetchone()[0], 2)
        self.assertEqual(writer.db.execute('PRAGMA wal_autocheckpoint').fetchone()[0], 1000)
        self.assertEqual(next(writer.observations([oid])), before)
        metric = writer.storage_metrics()
        self.assertEqual(metric['evidence_journal_mode'], 'wal')
        self.assertEqual(metric['evidence_journal_mode_requested'], 'wal')
        self.assertEqual(metric['evidence_synchronous'], 'full')

    def test_old_sqlite_runtime_keeps_durable_delete_mode(self):
        writer = self.store(wal=False)
        writer.observe('new', SOURCE, '/address/one', b'{}')
        self.assertEqual(writer.db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
        self.assertEqual(writer.db.execute('PRAGMA synchronous').fetchone()[0], 2)
        self.assertEqual(writer.storage_metrics()['evidence_journal_mode_requested'], 'delete')

    def test_legacy_runtime_can_read_wal_but_must_switch_before_writing(self):
        writer = self.store()
        oid = writer.observe('old', SOURCE, '/address/one', b'{}')
        old_runtime = self.store(wal=False)
        self.assertEqual(next(old_runtime.observations([oid]))['body'], b'{}')
        self.assertEqual(old_runtime.db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        old_runtime.db.execute('PRAGMA busy_timeout=1')
        with self.assertRaisesRegex(TraceError, 'lacks the WAL-reset fix'):
            old_runtime.attempt('new', 'esplora', '/address/two', 'started')
        self.assertEqual(old_runtime.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 0)
        writer.close()
        self.stores.remove(writer)
        old_runtime.attempt('new', 'esplora', '/address/two', 'started')
        self.assertEqual(old_runtime.db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')

    def test_read_only_sqlite_reader_sees_each_acknowledged_commit_while_writer_is_open(self):
        writer = self.store()
        writer.attempt('new', 'esplora', '/address/one', 'started')
        uri = (self.case / 'evidence.sqlite').as_uri() + '?mode=ro'
        with sqlite3.connect(uri, uri=True) as reader:
            self.assertEqual(reader.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 1)
            oid = writer.record_response('new', 'esplora', SOURCE, '/address/one', b'{"value":7}', 200)
            self.assertEqual(reader.execute('SELECT id, body FROM observations').fetchone(), (oid, b'{"value":7}'))
            self.assertEqual(reader.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 2)

    def test_process_exit_without_close_preserves_acknowledged_wal_response(self):
        script = """
import os, sys
from liquid_tracer.store import Store
from unittest.mock import patch
with patch('liquid_tracer.store.wal_runtime_safe', return_value=True):
    store = Store(sys.argv[1])
store.attempt('crash', 'esplora', '/address/one', 'started')
oid = store.record_response('crash', 'esplora', 'https://example.invalid/liquid/api', '/address/one', b'{"value":7}', 200)
print(oid, flush=True)
os._exit(17)
"""
        completed = subprocess.run([sys.executable, '-c', script, str(self.case)],
                                    capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 17, completed.stderr)
        oid = int(completed.stdout)
        self.assertTrue((self.case / 'evidence.sqlite-wal').exists())
        with sqlite3.connect((self.case / 'evidence.sqlite').as_uri() + '?mode=ro', uri=True) as reader:
            self.assertEqual(reader.execute('SELECT id, sha256, body FROM observations').fetchone(),
                             (oid, digest(b'{"value":7}'), b'{"value":7}'))
            self.assertEqual(reader.execute('SELECT status FROM attempts ORDER BY id').fetchall(),
                             [('started',), ('200',)])
        reopened = self.store()
        self.assertEqual(next(reopened.observations([oid]))['body'], b'{"value":7}')

    def test_metrics_separate_commit_time_lock_hold_and_lock_wait(self):
        store = self.store()
        statements = []
        def trace(statement):
            statements.append(statement)
            if statement == 'COMMIT':
                time.sleep(.01)
        store.db.set_trace_callback(trace)
        store.attempt('new', 'esplora', '/address/one', 'started')
        oid = store.record_response('new', 'esplora', SOURCE, '/address/one', b'{}', 200)
        self.assertIsNotNone(store.cached(SOURCE, '/address/one', 'new', 0))
        next(store.observations([oid]))
        metrics = store.storage_metrics()
        self.assertEqual(metrics['evidence_write_operations'], 2)
        self.assertEqual(metrics['evidence_commits'], 2)
        self.assertEqual(metrics['evidence_read_operations'], 2)
        self.assertGreaterEqual(metrics['evidence_commit_seconds_total'], .019)
        self.assertGreaterEqual(metrics['evidence_write_seconds_total'], metrics['evidence_commit_seconds_total'])
        before = len(statements)
        store.storage_metrics()
        self.assertEqual(len(statements), before, 'Metrics must not execute SQLite queries')

    def test_waiting_for_shared_store_lock_is_reported_separately(self):
        store = self.store()
        entered = threading.Event()
        def read():
            entered.set()
            store.cached(SOURCE, '/address/one', 'new', 0)
        with store._lock:
            worker = threading.Thread(target=read)
            worker.start()
            self.assertTrue(entered.wait(1))
            time.sleep(.02)
        worker.join(1)
        self.assertFalse(worker.is_alive())
        metrics = store.storage_metrics()
        self.assertGreaterEqual(metrics['evidence_read_lock_wait_seconds_total'], .019)
        self.assertEqual(metrics['evidence_read_operations'], 1)

    def test_evidence_sidecar_symlinks_are_rejected_before_open_or_migration(self):
        for suffix in ('', '-wal', '-shm', '-journal'):
            with self.subTest(suffix=suffix):
                case = Path(self.temp.name) / ('links-' + (suffix or 'main'))
                case.mkdir()
                target = case / 'target'
                target.write_bytes(b'do not alter')
                (case / ('evidence.sqlite' + suffix)).symlink_to(target)
                with self.assertRaisesRegex(TraceError, 'ordinary files'):
                    self.store(case)
                self.assertEqual(target.read_bytes(), b'do not alter')


if __name__ == '__main__':
    unittest.main()
