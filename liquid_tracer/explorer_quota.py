"""Private request pacing shared by explorer clients on the same machine.

Clients using the same endpoint hostname share one target, including clients
with different credentials. A second key is not assumed to buy another quota.
Only random client IDs, timing, and requested intervals are persisted.
"""

import hashlib
import math
import os
import sqlite3
import stat
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .common import TraceError


CLIENT_IDLE_SECONDS = 30.
LOCK_RETRY_SECONDS = .05


@dataclass(frozen=True)
class Admission:
    admitted: bool
    wait_seconds: float = 0.
    active_clients: int = 0
    effective_rps: float = 0.
    reason: str = ""


class _Busy(Exception):
    pass


class SharedExplorerQuota:
    """Reserve a start atomically, without sleeping under a database lock.

    The slowest active client's interval applies to everybody. Registrations are
    renewed on each admission attempt, including attempts waiting for permission.
    Close removes the registration; idle or crashed clients expire automatically.
    An expired client must register its own limit again before sending a request.
    HTTP requests time out within 20 seconds, below the default idle lease.
    """

    def __init__(self, endpoint, interval, *, directory=None, clock=None,
                 idle_seconds=CLIENT_IDLE_SECONDS):
        parsed = urllib.parse.urlsplit(endpoint)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host or parsed.username or parsed.query or parsed.fragment:
            raise TraceError("Shared explorer pacing requires an HTTPS endpoint without credentials")
        if not math.isfinite(interval) or interval <= 0 or not math.isfinite(idle_seconds) or idle_seconds <= 0:
            raise TraceError("Shared explorer interval and idle lifetime must be positive")
        self.interval = interval
        self._idle_seconds = idle_seconds
        self._clock = clock or time.time
        self._client_id = uuid.uuid4().hex
        self._closed = False
        if directory is None:
            cache = os.environ.get("XDG_CACHE_HOME", "")
            base = Path(cache) if cache and Path(cache).is_absolute() else Path.home() / ".cache"
            directory = base / "liquid-network-tracer" / "explorer-quota"
        directory = Path(directory)
        identity = hashlib.sha256(b"liquid-tracer-explorer-quota-v1\0" + host.encode()).hexdigest()
        self.path = directory / (identity + ".sqlite3")
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise OSError("unsafe quota directory")
            directory.chmod(0o700)
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags, 0o600)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                    raise OSError("unsafe quota file")
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
        except OSError:
            raise TraceError("Cannot open the private explorer quota cache; check local cache permissions") from None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def _transaction(self):
        db = None
        try:
            # Brief contention is returned to the caller's interruptible wait,
            # so SQLite's busy timeout cannot hide cancellation or time budgets.
            db = sqlite3.connect(self.path, timeout=LOCK_RETRY_SECONDS)
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS pacing (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_start REAL NOT NULL DEFAULT 0,
                cooldown REAL NOT NULL DEFAULT 0)""")
            db.execute("INSERT OR IGNORE INTO pacing (id) VALUES (1)")
            db.execute("""CREATE TABLE IF NOT EXISTS clients (
                id TEXT PRIMARY KEY, interval REAL NOT NULL,
                expires_at REAL NOT NULL)""")
            yield db
            db.commit()
        except (OSError, sqlite3.Error) as error:
            if db is not None:
                db.rollback()
            if isinstance(error, sqlite3.OperationalError) and getattr(error, "sqlite_errorcode", None) in (
                    sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise _Busy() from None
            raise TraceError("Cannot update the private explorer quota cache; check local cache permissions before retrying") from None
        except BaseException:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    def reserve(self):
        """Admit now, or return a delay for a cancellable caller-owned wait."""
        if self._closed:
            raise TraceError("Shared explorer pacing is closed")
        try:
            with self._transaction() as db:
                now = self._clock()
                db.execute("DELETE FROM clients WHERE expires_at <= ?", (now,))
                db.execute("INSERT OR REPLACE INTO clients VALUES (?, ?, ?)",
                           (self._client_id, self.interval, now + self._idle_seconds))
                active, interval = db.execute("SELECT COUNT(*), MAX(interval) FROM clients").fetchone()
                last_start, cooldown = db.execute("SELECT last_start, cooldown FROM pacing WHERE id = 1").fetchone()
                # Derive the next start from the *current* shared interval.
                # A faster client cannot use a previous faster reservation to
                # bypass a slower client's newly registered allowance.
                deadline = max(last_start + interval, cooldown)
                if deadline > now:
                    reason = "server_cooldown" if cooldown >= last_start + interval else "shared_rate_limit"
                    return Admission(False, deadline - now, active, 1. / interval, reason)
                db.execute("UPDATE pacing SET last_start = ? WHERE id = 1", (now,))
                return Admission(True, active_clients=active, effective_rps=1. / interval)
        except _Busy:
            return Admission(False, LOCK_RETRY_SECONDS, reason="shared_rate_limit")

    def cooldown(self, seconds):
        """Share Retry-After even when the reporting run will stop.

        Return False for brief lock contention; callers retry with cancellation
        and budget checks. Invalid infinite Retry-After still blocks other
        clients for a minute instead of allowing an immediate retry storm.
        """
        if self._closed:
            raise TraceError("Shared explorer pacing is closed")
        seconds = max(0., seconds) if math.isfinite(seconds) else 60.
        try:
            with self._transaction() as db:
                db.execute("UPDATE pacing SET cooldown = MAX(cooldown, ?) WHERE id = 1",
                           (self._clock() + seconds,))
            return True
        except _Busy:
            return False

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            with self._transaction() as db:
                db.execute("DELETE FROM clients WHERE id = ?", (self._client_id,))
        except (_Busy, TraceError):
            # A contended or crashed close has the same bounded idle lifetime.
            pass
