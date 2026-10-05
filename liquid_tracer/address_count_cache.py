"""Durable, bounded address-count checkpoints between legacy JSON compactions.

The collector owns address-counts.lock. Readers take a SQLite snapshot *before*
opening the JSON cache, so compacting JSON then clearing the journal cannot make
a concurrent reader miss committed observations. WAL and FULL synchronization
retain committed batches across interruption without rewriting all prior counts.
"""
import contextlib
import fcntl
import json
import os
import sqlite3
import stat
import tempfile
from pathlib import Path

from .common import TraceError, canonical, digest


def _path(case):
    path = (Path(case) / "address-counts.sqlite3").absolute()
    for suffix in ("", "-wal", "-shm", "-journal"):
        if path.with_name(path.name + suffix).is_symlink():
            raise TraceError("Address count storage must not be a symbolic link")
    return path


def _header(case_id):
    return {"schema_version": 1, "case_id": case_id}


def _verify_header(connection, case_id):
    row = connection.execute("SELECT payload, sha256 FROM metadata WHERE singleton = 1").fetchone()
    if row is None or row[0] != canonical(_header(case_id)) or digest(row[0]) != row[1]:
        raise TraceError("Invalid address count cache; restore its last intact version")


def _checksum(case_id, source, address, payload):
    # Bind each observation to both its owning investigation and indexed keys.
    return digest(canonical([1, case_id, source, address]) + b"\n" + payload)


def _initialize(path, case_id):
    # Publish a complete empty database atomically. An offline reader racing the
    # first lookup must never see a newly created file without its schema/header.
    fd, name = tempfile.mkstemp(prefix=".address-counts-", suffix=".sqlite3", dir=path.parent)
    os.close(fd)
    temporary = Path(name)
    connection = None
    try:
        connection = sqlite3.connect(temporary)
        # The published file must already use WAL. Switching journal mode after
        # publication can block on an offline reader that has opened a snapshot.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        with connection:
            connection.execute("CREATE TABLE metadata (singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
                               "payload BLOB NOT NULL, sha256 TEXT NOT NULL)")
            connection.execute("CREATE TABLE observations (source TEXT NOT NULL, address TEXT NOT NULL, "
                "payload BLOB NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY (source, address)) WITHOUT ROWID")
            payload = canonical(_header(case_id))
            connection.execute("INSERT INTO metadata VALUES (1, ?, ?)", (payload, digest(payload)))
        connection.close()
        connection = None
        temporary.replace(path)
    finally:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def _snapshot_uri(case, path):
    """Avoid WAL sidecar writes only while the collector cannot change the DB.

    SQLite mode=ro may create WAL/SHM files even on a checkpointed database.
    Immutable mode avoids that, but must never ignore a live or stranded WAL.
    The collector holds address-counts.lock across all cache writes, including
    its final JSON replacement and journal close. Keep a shared lock throughout
    the read; active writers and older caches without that lock use normal WAL
    snapshots instead of treating mutable evidence as immutable.
    """
    uri = path.as_uri() + "?mode=ro"
    try:
        descriptor = os.open(Path(case) / "address-counts.lock",
                             os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        yield uri
        return
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise TraceError("Address count lock must be an ordinary file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            yield uri
            return
        if not any(path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-journal")):
            uri += "&immutable=1"
        yield uri
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def read_snapshot(case, case_id, source, valid):
    """Yield verified rows, holding a read snapshot while the caller reads JSON."""
    path = _path(case)
    if not path.exists():
        yield iter(())
        return
    try:
        with _snapshot_uri(case, path) as uri:
            with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
                connection.execute("BEGIN")
                # This read establishes the snapshot before the caller opens JSON.
                _verify_header(connection, case_id)

                def rows():
                    for address, payload, checksum in connection.execute(
                            "SELECT address, payload, sha256 FROM observations WHERE source = ?", (source,)):
                        if _checksum(case_id, source, address, payload) != checksum:
                            raise TraceError("Invalid address count cache; restore its last intact version")
                        record = json.loads(payload)
                        if not valid(record, source, address):
                            raise TraceError("Invalid address transaction count observation")
                        yield address, record

                yield rows()
    except (sqlite3.Error, ValueError, TypeError) as error:
        raise TraceError("Invalid address count cache; restore its last intact version") from error


class CountCacheJournal:
    """Single-writer incremental journal; each write touches only its new batch."""

    def __init__(self, case, case_id, source):
        path = _path(case)
        self.case_id, self.source = case_id, source
        self.connection = None
        try:
            if not path.exists():
                _initialize(path, case_id)
            self.connection = sqlite3.connect(path, timeout=5)
            _verify_header(self.connection, case_id)
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
        except (sqlite3.Error, ValueError, TypeError, TraceError) as error:
            self.close()
            if isinstance(error, TraceError):
                raise
            raise TraceError("Unable to open address count cache") from error

    def write(self, records):
        rows = []
        for address, record in records.items():
            payload = canonical(record)
            rows.append((self.source, address, payload, _checksum(self.case_id, self.source, address, payload)))
        try:
            with self.connection:
                self.connection.executemany("INSERT INTO observations VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (source, address) DO UPDATE SET payload=excluded.payload, sha256=excluded.sha256", rows)
        except sqlite3.Error as error:
            raise TraceError("Unable to save address count checkpoint") from error

    def compacted(self):
        """Call only after atomic replacement of the complete JSON cache."""
        try:
            with self.connection:
                self.connection.execute("DELETE FROM observations WHERE source = ?", (self.source,))
        except sqlite3.Error as error:
            raise TraceError("Unable to finish address count cache compaction") from error

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
