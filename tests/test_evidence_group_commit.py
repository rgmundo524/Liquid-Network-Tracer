import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.api import Esplora, Limits
from liquid_tracer.common import digest
from liquid_tracer.store import Store


SOURCE = 'https://example.invalid/liquid/api'


class Call:
    """A bounded test wait that also captures failures from worker threads."""

    def __init__(self, function, *args, name=None):
        self.started = threading.Event()
        self.done = threading.Event()
        self.value = None
        self.error = None

        def run():
            self.started.set()
            try:
                self.value = function(*args)
            except BaseException as error:
                self.error = error
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run, daemon=True, name=name)
        self.thread.start()

    def result(self):
        if not self.done.wait(5):
            raise AssertionError('Evidence writer did not finish')
        self.thread.join(1)
        if self.error is not None:
            raise self.error
        return self.value


class CommitGates:
    """Pause real SQLite COMMIT statements, without weakening durability."""

    def __init__(self, store, count=2):
        self.entered = [threading.Event() for _ in range(count)]
        self.release = [threading.Event() for _ in range(count)]
        self.index = 0
        self.timeouts = []

        def trace(statement):
            if statement.strip().upper() != 'COMMIT':
                return
            index = self.index
            self.index += 1
            if index < count:
                self.entered[index].set()
                if not self.release[index].wait(8):
                    self.timeouts.append(index)

        store.db.set_trace_callback(trace)

    def open(self):
        for event in self.release:
            event.set()


class EvidenceGroupCommitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.case = Path(self.temp.name) / 'case'
        self.store = Store(self.case)
        self.gates = []

    def tearDown(self):
        for gates in self.gates:
            gates.open()
        self.store.close()
        self.temp.cleanup()

    def gate(self, count=2):
        gates = CommitGates(self.store, count)
        self.gates.append(gates)
        return gates

    def pending(self, count):
        # Waiting on the production condition proves every job was accepted
        # before the blocked COMMIT is released, without timing assumptions.
        with self.store._pending_condition:
            ready = self.store._pending_condition.wait_for(
                lambda: len(self.store._pending) == count, timeout=5)
        self.assertTrue(ready, 'Expected evidence jobs were not queued')

    def rows(self, table, columns='*'):
        with sqlite3.connect((self.case / 'evidence.sqlite').as_uri() + '?mode=ro', uri=True) as reader:
            return reader.execute(f'SELECT {columns} FROM {table} ORDER BY id').fetchall()

    def response(self, index, body=None, status=200):
        return Call(self.store.record_response, 'run', 'esplora', SOURCE,
                    '/address/' + str(index), body if body is not None else str(index).encode(), status)

    def sentinel(self, gates):
        sentinel = Call(self.store.attempt, 'run', 'esplora', '/sentinel', 'started')
        self.assertTrue(gates.entered[0].wait(5))
        return sentinel

    def test_concurrent_responses_and_attempts_share_one_durable_commit(self):
        gates = self.gate()
        sentinel = self.sentinel(gates)
        raw = [b'{"value":7}', b'\x00\xffexact bytes\n', b'{"error":"retained"}']
        statuses = [200, 200, 429]
        calls = [self.response(index, body, statuses[index]) for index, body in enumerate(raw)]
        observed = Call(self.store.observe, 'run', SOURCE, '/standalone', b'{}', 404)
        attempt = Call(self.store.attempt, 'run', 'esplora', '/pending', 'started')
        self.pending(5)
        self.assertEqual(self.rows('attempts'), [])
        self.assertEqual(self.rows('observations'), [])

        gates.release[0].set()
        self.assertTrue(gates.entered[1].wait(5))
        sentinel.result()
        self.assertTrue(all(not call.done.is_set() for call in [*calls, observed, attempt]))
        self.assertEqual(self.rows('attempts', 'endpoint, status'), [('/sentinel', 'started')])
        self.assertEqual(self.rows('observations'), [])

        gates.release[1].set()
        ids = [call.result() for call in calls]
        standalone_id = observed.result()
        self.assertIsNone(attempt.result())
        self.assertEqual(len(set([*ids, standalone_id])), 4)
        by_id = {row[0]: row[1:] for row in self.rows('observations', 'id, endpoint, status, sha256, body')}
        for index, (oid, body) in enumerate(zip(ids, raw)):
            self.assertEqual(by_id[oid], ('/address/' + str(index), statuses[index], digest(body), body))
        self.assertEqual(by_id[standalone_id], ('/standalone', 404, digest(b'{}'), b'{}'))
        self.assertCountEqual(self.rows('attempts', 'endpoint, status'),
                              [('/sentinel', 'started'), ('/pending', 'started')]
                              + [('/address/' + str(index), str(statuses[index])) for index in range(3)])
        metrics = self.store.storage_metrics()
        self.assertEqual(metrics['evidence_commits'], 2)
        self.assertEqual(metrics['evidence_batch_size_max'], 5)
        self.assertEqual(metrics['evidence_write_operations'], 6)
        self.assertEqual(gates.timeouts, [])

    def test_failed_statement_rolls_back_only_its_response_and_outcome(self):
        self.store.db.executescript("""
          CREATE TRIGGER reject_bad BEFORE INSERT ON observations
          WHEN NEW.endpoint = '/address/bad'
          BEGIN SELECT RAISE(FAIL, 'synthetic rejected response'); END;
        """)
        gates = self.gate(1)
        sentinel = self.sentinel(gates)
        good_before = self.response('first', b'first exact response')
        self.pending(1)
        bad = self.response('bad', b'do not acknowledge')
        self.pending(2)
        good_after = self.response('last', b'last exact response')
        self.pending(3)
        gates.open()
        sentinel.result()
        ids = [good_before.result(), good_after.result()]
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'synthetic rejected response'):
            bad.result()
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(self.rows('observations', 'endpoint, body'),
                         [('/address/first', b'first exact response'), ('/address/last', b'last exact response')])
        self.assertEqual(self.rows('attempts', 'endpoint, status'),
                         [('/sentinel', 'started'), ('/address/first', '200'), ('/address/last', '200')])
        self.assertEqual(self.store.storage_metrics()['evidence_commits'], 2)

    def test_transaction_rollback_fails_every_member_without_acknowledging_lost_rows(self):
        self.store.db.executescript("""
          CREATE TRIGGER abort_group BEFORE INSERT ON observations
          WHEN NEW.endpoint = '/address/bad'
          BEGIN SELECT RAISE(ROLLBACK, 'synthetic transaction rollback'); END;
        """)
        gates = self.gate(1)
        sentinel = self.sentinel(gates)
        calls = []
        for index in ('first', 'bad', 'last'):
            calls.append(self.response(index))
            self.pending(len(calls))
        gates.open()
        sentinel.result()
        for call in calls:
            with self.assertRaises(Exception):
                call.result()
        self.assertEqual(self.rows('observations'), [])
        self.assertEqual(self.rows('attempts', 'endpoint, status'), [('/sentinel', 'started')])
        self.assertFalse(self.store.db.in_transaction)

    def test_commit_failure_rolls_back_and_wakes_every_caller(self):
        commit_requests = 0

        def authorize(action, name, *_):
            nonlocal commit_requests
            if action == sqlite3.SQLITE_TRANSACTION and name == 'COMMIT':
                commit_requests += 1
                if commit_requests == 2:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.store.db.set_authorizer(authorize)
        gates = self.gate(1)
        sentinel = self.sentinel(gates)
        calls = [self.response(index) for index in range(4)]
        self.pending(4)
        gates.open()
        sentinel.result()
        for call in calls:
            with self.assertRaises(sqlite3.DatabaseError):
                call.result()
        self.assertEqual(self.rows('observations'), [])
        self.assertEqual(self.rows('attempts', 'endpoint, status'), [('/sentinel', 'started')])
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.store.storage_metrics()['evidence_commits'], 1)

    def test_close_drains_accepted_jobs_and_rejects_late_writes(self):
        gates = self.gate(1)
        sentinel = self.sentinel(gates)
        calls = [self.response(index) for index in range(4)]
        self.pending(4)
        closed = Call(self.store.close)
        with self.store._pending_condition:
            self.assertTrue(self.store._pending_condition.wait_for(
                lambda: self.store._closing, timeout=5))
        late = self.response('late')
        with self.assertRaises(sqlite3.ProgrammingError):
            late.result()
        self.assertFalse(closed.done.is_set())
        gates.open()
        sentinel.result()
        ids = [call.result() for call in calls]
        closed.result()
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(self.rows('observations')), 4)
        self.assertNotIn(('/address/late',), self.rows('observations', 'endpoint'))

    def test_interrupted_waiter_settles_its_accepted_response_before_propagating(self):
        gates = self.gate(1)
        sentinel = self.sentinel(gates)
        original_wait = self.store._pending_condition.wait
        interrupted = threading.Event()

        def wait(*args, **kwargs):
            if threading.current_thread().name == 'interrupted-evidence-waiter' and not interrupted.is_set():
                interrupted.set()
                raise KeyboardInterrupt()
            return original_wait(*args, **kwargs)

        with patch.object(self.store._pending_condition, 'wait', side_effect=wait):
            follower = Call(self.store.record_response, 'run', 'esplora', SOURCE,
                            '/address/interrupted', b'exact received bytes', 200,
                            name='interrupted-evidence-waiter')
            self.pending(1)
            self.assertTrue(interrupted.wait(5))
            self.assertFalse(follower.done.is_set())
            gates.open()
            sentinel.result()
            with self.assertRaises(KeyboardInterrupt):
                follower.result()
        self.assertEqual(self.rows('observations', 'endpoint, body'),
                         [('/address/interrupted', b'exact received bytes')])
        self.assertEqual(self.rows('attempts', 'endpoint, status'),
                         [('/sentinel', 'started'), ('/address/interrupted', '200')])

    def test_interrupted_commit_wakes_its_cohort_and_later_accepted_jobs_can_finish(self):
        connection = self.store.db

        class InterruptingConnection:
            commits = 0

            def __getattr__(self, name):
                return getattr(connection, name)

            def __enter__(self):
                return connection.__enter__()

            def __exit__(self, *args):
                self.commits += 1
                if self.commits == 2:
                    # Emulate interruption while committing before SQLite has
                    # finished cleanup. The Store must recover the connection
                    # and settle its cohort before passing leadership onward.
                    raise KeyboardInterrupt()
                return connection.__exit__(*args)

        self.store.db = InterruptingConnection()
        with patch('liquid_tracer.store.EVIDENCE_BATCH_SIZE', 2):
            gates = self.gate(1)
            sentinel = self.sentinel(gates)
            calls = []
            for index in range(4):
                calls.append(self.response(index))
                self.pending(len(calls))
            closed = Call(self.store.close)
            with self.store._pending_condition:
                self.assertTrue(self.store._pending_condition.wait_for(
                    lambda: self.store._closing, timeout=5))
            gates.open()
            sentinel.result()
            for call in calls[:2]:
                with self.assertRaises(KeyboardInterrupt):
                    call.result()
            for call in calls[2:]:
                self.assertIsInstance(call.result(), int)
            closed.result()
        self.assertEqual(self.rows('observations', 'endpoint, body'),
                         [('/address/2', b'2'), ('/address/3', b'3')])

    def check_interrupted_handoff(self, method):
        original = getattr(self.store, method)
        invocations = 0

        def interrupt(*args):
            nonlocal invocations
            invocations += 1
            if invocations == 2:
                if method == '_claim_batch':
                    original(*args)
                raise KeyboardInterrupt()
            return original(*args)

        with patch.object(self.store, method, side_effect=interrupt):
            gates = self.gate(1)
            sentinel = self.sentinel(gates)
            calls = [self.response(index) for index in range(4)]
            self.pending(4)
            gates.open()
            sentinel.result()
            interrupted_callers = 0
            for call in calls:
                try:
                    call.result()
                except KeyboardInterrupt:
                    interrupted_callers += 1
            self.assertEqual(interrupted_callers, 1)
        Call(self.store.close).result()
        self.assertCountEqual(self.rows('observations', 'endpoint, body'),
                              [('/address/' + str(index), str(index).encode()) for index in range(4)])
        self.assertCountEqual(self.rows('attempts', 'endpoint, status'), [('/sentinel', 'started')]
                              + [('/address/' + str(index), '200') for index in range(4)])

    def test_interrupt_before_batch_executor_entry_does_not_strand_claimed_jobs(self):
        self.check_interrupted_handoff('_commit_batch')

    def test_interrupt_after_batch_claim_does_not_strand_claimed_jobs(self):
        self.check_interrupted_handoff('_claim_batch')

    def replace_close_once(self, error):
        connection = self.store.db

        class FailingCloseConnection:
            calls = 0

            def __getattr__(self, name):
                return getattr(connection, name)

            def close(self):
                self.calls += 1
                if self.calls == 1:
                    raise error
                return connection.close()

        wrapper = FailingCloseConnection()
        self.store.db = wrapper
        return wrapper

    def test_failed_close_remains_retryable_and_keeps_committed_evidence(self):
        oid = self.store.record_response('run', 'esplora', SOURCE, '/address/one', b'exact bytes')
        connection = self.replace_close_once(sqlite3.OperationalError('synthetic close failure'))
        with self.assertRaisesRegex(sqlite3.OperationalError, 'synthetic close failure'):
            Call(self.store.close).result()
        self.assertFalse(self.store._closed)
        self.assertFalse(self.store._writer_active)
        Call(self.store.close).result()
        self.assertTrue(self.store._closed)
        self.assertEqual(connection.calls, 2)
        self.assertEqual(self.rows('observations', 'id, body'), [(oid, b'exact bytes')])

    def test_interrupted_close_finishes_closing_before_propagating_interrupt(self):
        oid = self.store.record_response('run', 'esplora', SOURCE, '/address/one', b'exact bytes')
        connection = self.replace_close_once(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            Call(self.store.close).result()
        self.assertTrue(self.store._closed)
        self.assertFalse(self.store._writer_active)
        self.assertEqual(connection.calls, 2)
        self.assertEqual(self.rows('observations', 'id, body'), [(oid, b'exact bytes')])

    def test_interrupt_after_connection_lock_acquisition_does_not_leak_the_lock(self):
        real_clock = time.monotonic
        operation_calls = 0

        def interrupting_clock():
            nonlocal operation_calls
            if sys._getframe(1).f_code.co_name == '_operation':
                operation_calls += 1
                if operation_calls == 2:
                    raise KeyboardInterrupt()
            return real_clock()

        with patch('liquid_tracer.store.time.monotonic', new=interrupting_clock):
            failed = Call(self.store.attempt, 'run', 'esplora', '/interrupted', 'started')
            with self.assertRaises(KeyboardInterrupt):
                failed.result()
        acquired = self.store._lock.acquire(timeout=.1)
        self.assertTrue(acquired, 'Interrupted writer retained the connection lock')
        if acquired:
            self.store._lock.release()
        Call(self.store.attempt, 'run', 'esplora', '/next', 'started').result()
        Call(self.store.close).result()
        self.assertEqual(self.rows('attempts', 'endpoint, status'), [('/next', 'started')])

    def test_permanent_claim_failure_returns_once_without_spinning_or_stranding_close(self):
        with patch.object(self.store, '_claim_batch', side_effect=RuntimeError('synthetic claim failure')) as claim:
            failed = self.response('rejected')
            with self.assertRaisesRegex(RuntimeError, 'synthetic claim failure'):
                failed.result()
        self.assertEqual(claim.call_count, 1)
        with self.store._pending_condition:
            self.assertEqual(len(self.store._pending), 0)
            self.assertFalse(self.store._writer_active)
        Call(self.store.close).result()
        self.assertEqual(self.rows('observations'), [])
        self.assertEqual(self.rows('attempts'), [])

    def test_pending_queue_and_commit_groups_remain_bounded(self):
        with patch('liquid_tracer.store.EVIDENCE_QUEUE_CAPACITY', 2), \
             patch('liquid_tracer.store.EVIDENCE_BATCH_SIZE', 2):
            gates = self.gate(1)
            sentinel = self.sentinel(gates)
            first = self.response(0)
            second = self.response(1)
            self.pending(2)
            overflow = self.response(2)
            self.assertTrue(overflow.started.wait(5))
            self.assertFalse(overflow.done.wait(.03))
            with self.store._pending_condition:
                self.assertEqual(len(self.store._pending), 2)
            gates.open()
            sentinel.result()
            self.assertEqual(len({call.result() for call in (first, second, overflow)}), 3)
        metrics = self.store.storage_metrics()
        self.assertEqual(metrics['evidence_queue_depth_peak'], 2)
        self.assertLessEqual(metrics['evidence_batch_size_max'], 2)
        self.assertEqual(len(self.rows('observations')), 3)

    def test_unstarted_cohort_reserves_capacity_when_interrupted_and_requeued(self):
        original_commit = self.store._commit_batch
        original_wait = self.store._pending_condition.wait
        entered, release, overflow_waiting = (threading.Event() for _ in range(3))
        invocations = 0

        def commit(batch):
            nonlocal invocations
            invocations += 1
            if invocations == 1:
                entered.set()
                if not release.wait(5):
                    raise AssertionError('Test handoff release timed out')
                raise KeyboardInterrupt()
            return original_commit(batch)

        def wait(*args, **kwargs):
            if threading.current_thread().name == 'capacity-overflow':
                overflow_waiting.set()
            return original_wait(*args, **kwargs)

        with patch('liquid_tracer.store.EVIDENCE_QUEUE_CAPACITY', 2), \
             patch('liquid_tracer.store.EVIDENCE_BATCH_SIZE', 2), \
             patch.object(self.store, '_commit_batch', new=commit), \
             patch.object(self.store._pending_condition, 'wait', new=wait):
            try:
                first = self.response(0)
                self.assertTrue(entered.wait(5))
                second = self.response(1)
                self.pending(1)
                third = Call(self.store.record_response, 'run', 'esplora', SOURCE,
                             '/address/2', b'2', 200, name='capacity-overflow')
                self.assertTrue(overflow_waiting.wait(5))
                with self.store._pending_condition:
                    self.assertEqual(len(self.store._active_batch), 1)
                    self.assertEqual(len(self.store._pending), 1)
                    self.assertFalse(self.store._batch_started)
                release.set()
                with self.assertRaises(KeyboardInterrupt):
                    first.result()
                self.assertIsInstance(second.result(), int)
                self.assertIsInstance(third.result(), int)
            finally:
                release.set()
        self.assertEqual(self.rows('observations', 'endpoint, body'),
                         [('/address/' + str(index), str(index).encode()) for index in range(3)])
        self.assertLessEqual(self.store.storage_metrics()['evidence_queue_depth_peak'], 2)

    def test_interrupted_leader_release_still_wakes_waiting_followers(self):
        original_notify = self.store._pending_condition.notify_all
        original_wait = self.store._pending_condition.wait
        interrupted = threading.Event()
        waiting = {'release-follower-' + str(index): threading.Event() for index in range(4)}

        def notify():
            if sys._getframe(1).f_code.co_name == '_release_leader' and not interrupted.is_set():
                interrupted.set()
                raise KeyboardInterrupt()
            return original_notify()

        def wait(*args, **kwargs):
            event = waiting.get(threading.current_thread().name)
            if event is not None:
                event.set()
            return original_wait(*args, **kwargs)

        with patch.object(self.store._pending_condition, 'notify_all', new=notify), \
             patch.object(self.store._pending_condition, 'wait', new=wait):
            gates = self.gate(1)
            sentinel = self.sentinel(gates)
            followers = [Call(self.store.record_response, 'run', 'esplora', SOURCE,
                              '/address/' + str(index), str(index).encode(), 200,
                              name='release-follower-' + str(index)) for index in range(4)]
            self.pending(4)
            self.assertTrue(all(event.wait(5) for event in waiting.values()))
            gates.open()
            with self.assertRaises(KeyboardInterrupt):
                sentinel.result()
            self.assertTrue(interrupted.is_set())
            self.assertEqual(len({follower.result() for follower in followers}), 4)
        Call(self.store.close).result()
        self.assertCountEqual(self.rows('observations', 'endpoint, body'),
                              [('/address/' + str(index), str(index).encode()) for index in range(4)])
        self.assertEqual(len(self.rows('attempts')), 5)

    def test_api_cancellation_drains_every_received_response(self):
        received = threading.Barrier(4)
        transports_ready = threading.Event()
        allow_responses = threading.Event()
        raw = b'{"chain_stats":{"tx_count":7}}'

        def transport(*_):
            received.wait(timeout=5)
            transports_ready.set()
            if not allow_responses.wait(5):
                raise AssertionError('Test response release timed out')
            return 200, {}, raw

        api = Esplora(self.store, 'run', Limits(), auth='none', min_interval=0,
                      advertised_rps=1_000_000, transport=transport, workers=4)
        try:
            futures = [api.submit('/address/' + str(index)) for index in range(4)]
            # Every started attempt must already be durable before all four
            # transports reach their shared barrier and await response release.
            self.assertTrue(transports_ready.wait(5))
            gates = self.gate(1)
            allow_responses.set()
            self.assertTrue(gates.entered[0].wait(5))
            self.pending(3)
            closed = Call(api.close)
            self.assertTrue(api._cancelled.wait(5))
            self.assertFalse(closed.done.is_set())
            gates.open()
            closed.result()
            self.assertTrue(all(future.done() for future in futures))
            rows = self.rows('observations', 'endpoint, status, sha256, body')
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(row[1:] == (200, digest(raw), raw) for row in rows))
            self.assertEqual(len(self.rows('attempts')), 8)
            self.assertEqual(len(api.used_observations()), 4)
        finally:
            allow_responses.set()
            for gates in self.gates:
                gates.open()
            api.close()

    def test_process_exit_after_group_acknowledgement_preserves_all_raw_responses(self):
        script = r'''
import json, os, sys, threading
from liquid_tracer.store import Store

store = Store(sys.argv[1])
entered, release = threading.Event(), threading.Event()
first_commit = True
def trace(statement):
    global first_commit
    if statement == 'COMMIT' and first_commit:
        first_commit = False
        entered.set()
        if not release.wait(5):
            os._exit(90)
store.db.set_trace_callback(trace)
results, errors = {}, []
def write(index):
    try:
        raw = b'\x00\xffresponse:' + str(index).encode()
        results[index] = store.record_response('crash', 'esplora',
            'https://example.invalid/liquid/api', '/address/' + str(index), raw, 200)
    except BaseException as error:
        errors.append(repr(error))
threads = [threading.Thread(target=write, args=(0,))]
threads[0].start()
if not entered.wait(5):
    os._exit(91)
for index in range(1, 16):
    thread = threading.Thread(target=write, args=(index,))
    threads.append(thread)
    thread.start()
with store._pending_condition:
    if not store._pending_condition.wait_for(lambda: len(store._pending) == 15, timeout=5):
        os._exit(92)
release.set()
for thread in threads:
    thread.join(5)
if errors or any(thread.is_alive() for thread in threads):
    print(errors, flush=True)
    os._exit(93)
print(json.dumps({'ids': results, 'metrics': store.storage_metrics()}), flush=True)
os._exit(17)
'''
        case = Path(self.temp.name) / 'crash-case'
        completed = subprocess.run([sys.executable, '-c', script, str(case)],
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 17, completed.stderr + completed.stdout)
        report = json.loads(completed.stdout)
        self.assertEqual(report['metrics']['evidence_commits'], 2)
        self.assertEqual(report['metrics']['evidence_batch_size_max'], 15)
        with sqlite3.connect((case / 'evidence.sqlite').as_uri() + '?mode=ro', uri=True) as reader:
            saved = dict((row[0], row[1:]) for row in reader.execute(
                'SELECT id, endpoint, status, sha256, body FROM observations'))
            attempts = reader.execute('SELECT endpoint, status FROM attempts').fetchall()
        self.assertEqual(len(saved), 16)
        self.assertEqual(len(attempts), 16)
        for index, oid in report['ids'].items():
            raw = b'\x00\xffresponse:' + index.encode()
            self.assertEqual(saved[oid], ('/address/' + index, 200, digest(raw), raw))


if __name__ == '__main__':
    unittest.main()
