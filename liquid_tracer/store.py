import json
import sqlite3
import threading
import time
from pathlib import Path

from .common import TraceError, digest, now


class Store:
    """Append-only response observations; mutable state lives in run snapshots."""

    def __init__(self, case):
        self.case = Path(case)
        self.case.mkdir(parents=True, exist_ok=True)
        # One connection is shared by the bounded fetch workers. Serialize the
        # whole transaction, not just execute(), so one worker cannot commit or
        # roll back another worker's evidence.
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.case / "evidence.sqlite", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
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

    def observe(self, run_id, source, endpoint, body, status=200):
        with self._lock, self.db:
            cur = self.db.execute("INSERT INTO observations VALUES (NULL,?,?,?,?,?,?,?,?)",
                (run_id, source, endpoint, now(), time.time(), status, digest(body), body))
        return cur.lastrowid

    def attempt(self, run_id, kind, endpoint, status):
        with self._lock, self.db:
            self.db.execute("INSERT INTO attempts VALUES (NULL,?,?,?,?,?)",
                            (run_id, kind, endpoint, now(), str(status)))

    def cached(self, source, endpoint, run_id, ttl):
        with self._lock:
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
            with self._lock:
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
