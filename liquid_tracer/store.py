import json
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .common import TraceError, digest, now
from .sqlite_storage import wal_runtime_safe


class Store:
    """Append-only response observations; mutable state lives in run snapshots."""

    def __init__(self, case):
        self.case = Path(case)
        self.case.mkdir(parents=True, exist_ok=True)
        # One connection is shared by the bounded fetch workers. Serialize the
        # whole transaction, not just execute(), so one worker cannot commit or
        # roll back another worker's evidence.
        self._lock = threading.RLock()
        self._metrics_lock = threading.Lock()
        self._metrics = {"evidence_" + name: 0. for name in (
            "write_lock_wait_seconds_total", "read_lock_wait_seconds_total",
            "write_seconds_total", "read_seconds_total", "commit_seconds_total")}
        self._metrics.update(evidence_commits=0, evidence_write_operations=0, evidence_read_operations=0)
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
    def _operation(self, kind):
        started = time.monotonic()
        self._lock.acquire()
        acquired = time.monotonic()
        try:
            yield
        finally:
            finished = time.monotonic()
            self._lock.release()
            with self._metrics_lock:
                self._metrics[f"evidence_{kind}_lock_wait_seconds_total"] += max(0., acquired - started)
                self._metrics[f"evidence_{kind}_seconds_total"] += max(0., finished - acquired)
                self._metrics[f"evidence_{kind}_operations"] += 1

    @contextmanager
    def _write(self):
        with self._operation("write"):
            self._prepare_write()
            commit_started = None
            committed = False
            try:
                with self.db:
                    yield
                    commit_started = time.monotonic()
                committed = True
            finally:
                if commit_started is not None:
                    with self._metrics_lock:
                        self._metrics["evidence_commit_seconds_total"] += max(0., time.monotonic() - commit_started)
                        self._metrics["evidence_commits"] += int(committed)

    def observe(self, run_id, source, endpoint, body, status=200):
        with self._write():
            cur = self.db.execute("INSERT INTO observations VALUES (NULL,?,?,?,?,?,?,?,?)",
                (run_id, source, endpoint, now(), time.time(), status, digest(body), body))
        return cur.lastrowid

    def attempt(self, run_id, kind, endpoint, status):
        with self._write():
            self.db.execute("INSERT INTO attempts VALUES (NULL,?,?,?,?,?)",
                            (run_id, kind, endpoint, now(), str(status)))

    def record_response(self, run_id, kind, source, endpoint, body, status=200):
        """Commit a received outcome and its exact response bytes together.

        A request's started attempt is already durable before transport begins.
        After receiving a response, neither its outcome nor its observation can
        be acknowledged independently of the other. The returned ID is visible
        to other SQLite readers only after this transaction commits.
        """
        timestamp, epoch, checksum = now(), time.time(), digest(body)
        with self._write():
            self.db.execute("INSERT INTO attempts VALUES (NULL,?,?,?,?,?)",
                            (run_id, kind, endpoint, timestamp, str(status)))
            cur = self.db.execute("INSERT INTO observations VALUES (NULL,?,?,?,?,?,?,?,?)",
                (run_id, source, endpoint, timestamp, epoch, status, checksum, body))
        return cur.lastrowid

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
        with self._lock:
            self.db.close()
