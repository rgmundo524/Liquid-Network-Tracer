import json
import sqlite3
import stat
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from .common import TraceError, digest, now
from .sqlite_storage import wal_runtime_safe


EVIDENCE_BATCH_SIZE = 64
EVIDENCE_QUEUE_CAPACITY = 128


@dataclass(eq=False, slots=True)
class _WriteJob:
    statements: tuple
    returning_id: bool
    submitted_at: float
    result: object = None
    error: BaseException | None = None
    done: bool = False


class Store:
    """Append-only response observations; mutable state lives in run snapshots."""

    def __init__(self, case):
        self.case = Path(case)
        self.case.mkdir(parents=True, exist_ok=True)
        # One connection is shared by the bounded fetch workers. Serialize the
        # whole transaction, not just execute(), so one worker cannot commit or
        # roll back another worker's evidence.
        self._lock = threading.RLock()
        # Callers waiting for durable writes do not hold the connection lock.
        # One caller commits the currently available FIFO cohort; there is no
        # background writer, timer, or delay to fill a batch.
        self._pending_condition = threading.Condition()
        self._pending = deque()
        self._writer_active = False
        self._writer_owner = None
        self._active_batch = ()
        self._batch_started = False
        self._closing = False
        self._closed = False
        self._metrics_lock = threading.Lock()
        self._metrics = {"evidence_" + name: 0. for name in (
            "write_lock_wait_seconds_total", "read_lock_wait_seconds_total",
            "write_seconds_total", "read_seconds_total", "commit_seconds_total",
            "queue_wait_seconds_total")}
        self._metrics.update(evidence_commits=0, evidence_write_operations=0, evidence_read_operations=0,
                             evidence_write_batches=0, evidence_batch_size_max=0, evidence_queue_depth_peak=0)
        self._journal_requested = "wal" if wal_runtime_safe() else "delete"
        self._journal_mode = "pending"
        self._write_ready = False
        self._check_files()
        self.db = sqlite3.connect(self.case / "evidence.sqlite", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # FULL is a per-connection durability setting. Do not migrate the
        # database's persistent journal mode merely to read old evidence.
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, source TEXT NOT NULL,
            endpoint TEXT NOT NULL, fetched_at TEXT NOT NULL, epoch REAL NOT NULL,
            status INTEGER NOT NULL, sha256 TEXT NOT NULL, body BLOB NOT NULL);
          CREATE INDEX IF NOT EXISTS observation_lookup
            ON observations(source, endpoint, id DESC);
          CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL,
            endpoint TEXT NOT NULL, started_at TEXT NOT NULL, status TEXT NOT NULL);
        """)
        self._journal_mode = str(self.db.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def _check_files(self):
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = self.case / ("evidence.sqlite" + suffix)
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise TraceError("Evidence database and sidecars must be ordinary files, not links")

    def _prepare_write(self):
        """Select a supported durable journal before this Store's first write."""
        if self._write_ready:
            return
        self._check_files()
        current = str(self.db.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        desired = self._journal_requested
        actual = current
        if current != desired:
            try:
                actual = str(self.db.execute("PRAGMA journal_mode=" + desired).fetchone()[0]).lower()
            except sqlite3.OperationalError as error:
                busy = getattr(error, "sqlite_errorcode", 0) & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                if busy and desired == "wal" and current == "delete":
                    # An existing reader may temporarily prevent migration.
                    # Continue using the equally durable legacy mode.
                    actual = current
                elif busy and desired == "delete" and current == "wal":
                    raise TraceError("Evidence uses WAL, but this SQLite runtime lacks the WAL-reset fix. "
                                     "Stop other Liquid Tracer processes before writing, or upgrade SQLite.") from error
                else:
                    raise
        if actual not in ("wal", "delete") or (desired == "delete" and actual != "delete"):
            raise TraceError("Unable to select a supported durable journal for evidence")
        if actual == "wal":
            self.db.execute("PRAGMA wal_autocheckpoint=1000")
        if self.db.execute("PRAGMA synchronous").fetchone()[0] != 2:
            raise TraceError("Evidence writes require SQLite synchronous=FULL")
        with self._metrics_lock:
            self._journal_mode = actual
        self._write_ready = True

    def storage_metrics(self):
        """A bounded snapshot of this Store's operations, without querying SQL."""
        with self._metrics_lock:
            return {**self._metrics, "evidence_journal_mode": self._journal_mode,
                    "evidence_journal_mode_requested": self._journal_requested,
                    "evidence_synchronous": "full", "evidence_sqlite_version": sqlite3.sqlite_version}

    @contextmanager
    def _operation(self, kind, operations=1):
        started = time.monotonic()
        acquired = finished = None
        try:
            # Keep timestamping inside the lock context too. An interrupt just
            # after acquire must not leak an RLock acquisition and strand the
            # next writer or close().
            with self._lock:
                acquired = time.monotonic()
                try:
                    yield
                finally:
                    finished = time.monotonic()
        finally:
            if acquired is not None and finished is not None:
                with self._metrics_lock:
                    self._metrics[f"evidence_{kind}_lock_wait_seconds_total"] += max(0., acquired - started)
                    self._metrics[f"evidence_{kind}_seconds_total"] += max(0., finished - acquired)
                    self._metrics[f"evidence_{kind}_operations"] += operations

    def _claim_batch(self):
        """Called with the queue condition held, never the connection lock."""
        # Prepare both collections before changing ownership. Retain the whole
        # cohort on the Store so an interrupt during the handoff cannot strand
        # jobs or leave followers waiting for a vanished leader.
        batch = tuple(islice(self._pending, EVIDENCE_BATCH_SIZE))
        remaining = deque(islice(self._pending, len(batch), None))
        self._active_batch = batch
        self._batch_started = False
        self._writer_owner = threading.get_ident()
        self._writer_active = True
        self._pending = remaining
        claimed_at = time.monotonic()
        with self._metrics_lock:
            self._metrics["evidence_queue_wait_seconds_total"] += sum(
                max(0., claimed_at - job.submitted_at) for job in batch)
            self._metrics["evidence_write_batches"] += 1
            self._metrics["evidence_batch_size_max"] = max(
                self._metrics["evidence_batch_size_max"], len(batch))
        self._pending_condition.notify_all()
        return batch

    def _release_leader(self):
        """Called only while holding the queue condition."""
        self._writer_active = False
        self._writer_owner = None
        self._active_batch = ()
        self._batch_started = False
        self._pending_condition.notify_all()

    def _recover_leader(self, error, *, requeue=False):
        """Clean up an interrupted handoff without replaying an executed job."""
        with self._pending_condition:
            if self._writer_owner != threading.get_ident():
                # Release may have cleared ownership before an interrupt
                # prevented its notification. Wake followers even when this
                # caller no longer owns the batch; never alter a new leader.
                self._pending_condition.notify_all()
                return
            batch = self._active_batch
            if not self._batch_started:
                # No SQL has run. A claim interrupted before replacing pending
                # may still leave these same jobs there; retain exactly one copy.
                remainder = [job for job in self._pending if job not in batch]
                if requeue:
                    for job in batch:
                        job.submitted_at = time.monotonic()
                    self._pending = deque((*batch, *remainder))
                    with self._metrics_lock:
                        self._metrics["evidence_queue_depth_peak"] = max(
                            self._metrics["evidence_queue_depth_peak"], len(self._pending))
                else:
                    # Ordinary failures are not transient cancellation. Fail
                    # explicitly instead of endlessly retrying a broken claim.
                    self._pending = deque(remainder)
                    for job in batch:
                        job.error, job.done = error, True
                self._release_leader()
                return
        # A failure escaping transaction cleanup has an uncertain commit result.
        # Settle it as failed, and never put executed jobs back in the queue.
        try:
            with self._lock:
                if self.db.in_transaction:
                    self.db.rollback()
        except BaseException as rollback_error:
            error = rollback_error
        finally:
            with self._pending_condition:
                for job in batch:
                    if not job.done:
                        job.error, job.done = error, True
                self._release_leader()

    def _commit_batch(self, batch):
        """Settle every job, including failures, before passing leadership on."""
        failure = None
        try:
            with self._pending_condition:
                self._batch_started = True
                # An executing cohort is never requeued, so its reserved queue
                # capacity can now be used by other received responses.
                self._pending_condition.notify_all()
            with self._operation("write", len(batch)):
                self._prepare_write()
                commit_started = None
                committed = False
                try:
                    with self.db:
                        # SAVEPOINT without an outer transaction would commit
                        # each job when released and defeat durable batching.
                        self.db.execute("BEGIN")
                        for job in batch:
                            self.db.execute("SAVEPOINT evidence_write")
                            try:
                                for statement, values in job.statements:
                                    cursor = self.db.execute(statement, values)
                                if job.returning_id:
                                    job.result = cursor.lastrowid
                            except (sqlite3.IntegrityError, sqlite3.ProgrammingError,
                                    sqlite3.DataError, OverflowError) as error:
                                # A malformed operation must not discard valid
                                # responses in its cohort. RAISE(ROLLBACK), I/O
                                # failures and failed savepoint recovery abort
                                # the whole transaction instead.
                                self.db.execute("ROLLBACK TO evidence_write")
                                self.db.execute("RELEASE evidence_write")
                                job.error = error
                            else:
                                self.db.execute("RELEASE evidence_write")
                        commit_started = time.monotonic()
                    committed = True
                except BaseException:
                    # SQLite's connection context normally rolls back itself.
                    # Also cover interruptions in a custom connection's exit
                    # or immediately before that cleanup starts, while this
                    # caller still owns the connection lock.
                    if self.db.in_transaction:
                        self.db.rollback()
                    raise
                finally:
                    if commit_started is not None:
                        with self._metrics_lock:
                            self._metrics["evidence_commit_seconds_total"] += max(
                                0., time.monotonic() - commit_started)
                            self._metrics["evidence_commits"] += int(committed)
        except BaseException as error:
            # A commit can fail with an uncertain result. Never acknowledge or
            # replay any member of that cohort, and always wake its followers.
            failure = error
        finally:
            # Publish only after releasing the DB lock. In particular, neither
            # followers nor close() wait for that lock while holding this one.
            with self._pending_condition:
                for job in batch:
                    if failure is not None:
                        job.error = failure
                    job.done = True
                self._release_leader()

    def _submit(self, statements, *, returning_id=False):
        job = _WriteJob(statements, returning_id, time.monotonic())
        interrupted = None
        try:
            with self._pending_condition:
                while (len(self._pending) + (0 if self._batch_started else len(self._active_batch))
                       >= EVIDENCE_QUEUE_CAPACITY and not self._closing):
                    self._pending_condition.wait()
                if self._closing:
                    raise sqlite3.ProgrammingError("Cannot write to a closed evidence store")
                self._pending.append(job)
                with self._metrics_lock:
                    self._metrics["evidence_queue_depth_peak"] = max(
                        self._metrics["evidence_queue_depth_peak"], len(self._pending))
                self._pending_condition.notify_all()
        except BaseException as error:
            # An interrupt can arrive after append, before bookkeeping/notify.
            # Once another leader could see this job it must still be settled.
            with self._pending_condition:
                if not (job.done or job in self._pending or job in self._active_batch):
                    raise
                self._pending_condition.notify_all()
            interrupted = error
        while True:
            try:
                with self._pending_condition:
                    if job.done:
                        break
                    if not self._writer_active and self._pending:
                        batch = self._claim_batch()
                    else:
                        self._pending_condition.wait()
                        continue
                self._commit_batch(batch)
            except BaseException as error:
                # Once accepted, do not abandon a response or leave a queue head
                # without a caller. Also guard claim -> commit, not just wait().
                cancellation = isinstance(error, (KeyboardInterrupt, SystemExit))
                self._recover_leader(error, requeue=cancellation)
                if not cancellation:
                    with self._pending_condition:
                        if job in self._pending:
                            self._pending.remove(job)
                            job.error, job.done = error, True
                            self._pending_condition.notify_all()
                interrupted = error
        if job.error is not None:
            raise job.error
        if interrupted is not None:
            raise interrupted
        return job.result

    def observe(self, run_id, source, endpoint, body, status=200):
        return self._submit((("INSERT INTO observations VALUES (NULL,?,?,?,?,?,?,?,?)",
            (run_id, source, endpoint, now(), time.time(), status, digest(body), body)),), returning_id=True)

    def attempt(self, run_id, kind, endpoint, status):
        self._submit((("INSERT INTO attempts VALUES (NULL,?,?,?,?,?)",
                       (run_id, kind, endpoint, now(), str(status))),))

    def record_response(self, run_id, kind, source, endpoint, body, status=200):
        """Commit a received outcome and its exact response bytes together.

        A request's started attempt is already durable before transport begins.
        After receiving a response, neither its outcome nor its observation can
        be acknowledged independently of the other. The returned ID is visible
        to other SQLite readers only after this transaction commits.
        """
        timestamp, epoch, checksum = now(), time.time(), digest(body)
        return self._submit((
            ("INSERT INTO attempts VALUES (NULL,?,?,?,?,?)", (run_id, kind, endpoint, timestamp, str(status))),
            ("INSERT INTO observations VALUES (NULL,?,?,?,?,?,?,?,?)",
             (run_id, source, endpoint, timestamp, epoch, status, checksum, body))), returning_id=True)

    def cached(self, source, endpoint, run_id, ttl):
        with self._operation("read"):
            row = self.db.execute("SELECT * FROM observations WHERE source=? AND endpoint=? "
                                  "AND status=200 ORDER BY id DESC LIMIT 1", (source, endpoint)).fetchone()
        if row and (row["run_id"] == run_id or (ttl > 0 and time.time() - row["epoch"] <= ttl)):
            if digest(row["body"]) != row["sha256"]:
                raise TraceError("Cached evidence checksum failed")
            try:
                return json.loads(row["body"]), row["id"]
            except (ValueError, UnicodeDecodeError):
                return None
        return None

    def observations(self, ids):
        for oid in sorted(set(ids)):
            with self._operation("read"):
                row = self.db.execute("SELECT * FROM observations WHERE id=?", (oid,)).fetchone()
                if row is None or digest(row["body"]) != row["sha256"]:
                    raise TraceError("Missing or altered evidence observation " + str(oid))
                snapshot = dict(row)
            # Rows are append-only. Snapshot one response at a time so exports
            # stay streaming without holding a lock while the consumer runs.
            yield snapshot

    def close(self):
        interrupted = None
        while True:
            try:
                with self._pending_condition:
                    if self._closed:
                        break
                    self._closing = True
                    self._pending_condition.notify_all()
                    if not self._writer_active:
                        if self._pending:
                            batch = self._claim_batch()
                        else:
                            # Reserve leadership so concurrent closers wait until
                            # the connection has actually finished closing.
                            self._active_batch = ()
                            self._batch_started = False
                            self._writer_owner = threading.get_ident()
                            self._writer_active = True
                            batch = None
                    else:
                        self._pending_condition.wait()
                        continue
                if batch is not None:
                    self._commit_batch(batch)
                    continue
                with self._lock:
                    self.db.close()
                with self._pending_condition:
                    self._closed = True
                    self._release_leader()
                break
            except BaseException as error:
                cancellation = isinstance(error, (KeyboardInterrupt, SystemExit))
                self._recover_leader(error, requeue=cancellation)
                if not cancellation:
                    # Keep a failed close retryable. In particular, do not mark
                    # the Store closed if its connection never actually closed.
                    raise
                interrupted = error
        if interrupted is not None:
            raise interrupted
