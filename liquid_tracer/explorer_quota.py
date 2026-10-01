"""Private request pacing shared by explorer clients on the same machine.

Clients using the same endpoint hostname share one target, including clients
with different credentials. A second key is not assumed to buy another quota.
Only random client IDs, timing, aggregate feedback, and rate targets are persisted.
"""

import hashlib
import math
import os
import sqlite3
import stat
import time
import threading
import urllib.parse
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from .common import TraceError


CLIENT_IDLE_SECONDS = 30.
LOCK_RETRY_SECONDS = .05
ADAPTIVE_INITIAL_RPS = 49.
ADAPTIVE_WINDOW_SECONDS = 2.
ADAPTIVE_PROBE_SECONDS = 10.
ADAPTIVE_BACKOFF = .7


def _wal_runtime_safe(version=None):
    """Use WAL only with SQLite's 2026 WAL-reset correction.

    https://sqlite.org/wal.html#walreset names 3.51.3 and later, plus
    maintained 3.44.6 and 3.50.7 backports. Other older branches remain in
    DELETE/FULL mode rather than assuming a distributor applied that patch.
    """
    version = sqlite3.sqlite_version_info if version is None else version
    return (version >= (3, 51, 3) or
            (version[:2] == (3, 44) and version >= (3, 44, 6)) or
            (version[:2] == (3, 50) and version >= (3, 50, 7)))


@dataclass
class _AdaptiveState:
    target_rps: float
    generation: int
    window_started: float
    successes: int = 0
    last_pressure: float = 0.
    hold_until: float = 0.

    def add_successes(self, pending):
        self.successes += pending.get(self.generation, 0)

    def grow(self, now):
        elapsed = now - self.window_started
        window = ADAPTIVE_PROBE_SECONDS if self.generation else ADAPTIVE_WINDOW_SECONDS
        if elapsed < window or now < self.hold_until:
            return
        # Demand, rather than the size of the address list, justifies more
        # capacity. This also prevents a slow local disk or a fixed peer from
        # causing an ever-growing target without extra throughput.
        enough = self.successes >= max(8, self.target_rps * elapsed * .5)
        if enough:
            proposed = self.target_rps * (1.05 if self.generation else 1.5)
            if math.isfinite(proposed):
                self.target_rps = proposed
        self.window_started = now
        self.successes = 0

    def pressure(self, admission, now, seconds):
        # Generations denote pressure episodes, not increases. A slow 429 from
        # just before a healthy increase must still reduce the current target,
        # while dozens of simultaneous rejections reduce it only once.
        if admission is not None and admission.generation == self.generation:
            self.target_rps = max(1e-9, self.target_rps * ADAPTIVE_BACKOFF)
            self.generation += 1
            self.last_pressure = now
            self.hold_until = now + max(5., seconds)
            self.window_started = self.hold_until
            self.successes = 0


class _Feedback:
    def _setup_feedback(self, adaptive):
        self.adaptive = bool(adaptive)
        self._feedback_lock = threading.Lock()
        self._pending_successes = {}

    def success(self, admission):
        """Buffer a successful HTTP attempt; no extra disk transaction."""
        if not self.adaptive or admission is None or not admission.admitted:
            return
        with self._feedback_lock:
            generation = admission.generation
            self._pending_successes[generation] = self._pending_successes.get(generation, 0) + 1

    def _take_successes(self):
        with self._feedback_lock:
            pending, self._pending_successes = self._pending_successes, {}
            return pending

    def _restore_successes(self, pending):
        with self._feedback_lock:
            for generation, count in pending.items():
                self._pending_successes[generation] = self._pending_successes.get(generation, 0) + count


@dataclass(frozen=True)
class Admission:
    admitted: bool
    wait_seconds: float = 0.
    active_clients: int = 0
    effective_rps: float = 0.
    reason: str = ""
    mode: str = "fixed"
    target_rps: float = 0.
    generation: int = 0


def _sqlite_busy(error):
    return (isinstance(error, sqlite3.OperationalError) and
            getattr(error, "sqlite_errorcode", 0) & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))


class _Busy(Exception):
    pass


class SharedExplorerQuota(_Feedback):
    """Reserve a start atomically, without sleeping under a database lock.

    The slowest active client's interval applies to everybody. Registrations are
    renewed on each admission attempt, including attempts waiting for permission.
    Close removes the registration; idle or crashed clients expire automatically.
    An expired client must register its own limit again before sending a request.
    HTTP requests time out within 20 seconds, below the default idle lease.
    """

    def __init__(self, endpoint, interval, *, directory=None, clock=None,
                 idle_seconds=CLIENT_IDLE_SECONDS, adaptive=False):
        parsed = urllib.parse.urlsplit(endpoint)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host or parsed.username or parsed.query or parsed.fragment:
            raise TraceError("Shared explorer pacing requires an HTTPS endpoint without credentials")
        if not math.isfinite(interval) or interval <= 0 or not math.isfinite(idle_seconds) or idle_seconds <= 0:
            raise TraceError("Shared explorer interval and idle lifetime must be positive")
        self.interval = interval
        self._setup_feedback(adaptive)
        self._idle_seconds = idle_seconds
        self._clock = clock or time.time
        self._monotonic = clock or time.monotonic
        self._denied_hint = None
        self._client_id = uuid.uuid4().hex
        self._closed = False
        self._owner_pid = os.getpid()
        self._db_lock = threading.RLock()
        self._connection = None
        self._schema_ready = False
        self._sqlite_version = sqlite3.sqlite_version
        self._journal_requested = "wal" if _wal_runtime_safe() else "delete"
        self._journal_mode = "pending"
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

    def _check_owner(self):
        if os.getpid() != self._owner_pid:
            raise TraceError("Create a new explorer quota client after forking a process")

    def success(self, admission):
        self._check_owner()
        return super().success(admission)

    def storage_metrics(self):
        """Non-sensitive settings already observed by this process; no SQL."""
        return {"quota_sqlite_version": self._sqlite_version,
                "quota_journal_mode": self._journal_mode,
                "quota_journal_mode_requested": self._journal_requested,
                "quota_synchronous": "full",
                "quota_connection_mode": "persistent"}

    def _check_files(self):
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = Path(str(self.path) + suffix)
            try:
                info = path.lstat()
            except FileNotFoundError:
                if suffix:
                    continue
                raise OSError("missing quota file") from None
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise OSError("unsafe quota file")
            if info.st_mode & 0o777 != 0o600:
                descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
                try:
                    opened = os.fstat(descriptor)
                    if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or
                            opened.st_uid != os.getuid() or opened.st_nlink != 1):
                        raise OSError("quota file changed during permission check")
                    os.fchmod(descriptor, 0o600)
                finally:
                    os.close(descriptor)

    def _open_connection(self):
        if self._connection is not None:
            return self._connection
        self._check_files()
        db = sqlite3.connect(self.path, timeout=LOCK_RETRY_SECONDS,
                             check_same_thread=False)
        try:
            current = str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            try:
                # Mode changes occur before BEGIN. FULL is retained on every
                # connection: admissions and Retry-After survive process exits.
                desired = self._journal_requested
                actual = str(db.execute("PRAGMA journal_mode=" + desired).fetchone()[0]).lower()
            except sqlite3.OperationalError as error:
                if desired == "delete" and current == "wal" and _sqlite_busy(error):
                    raise TraceError(
                        "Explorer quota uses WAL, but SQLite " + self._sqlite_version +
                        " lacks the WAL-reset fix. Stop other Liquid Tracer instances and retry "
                        "to use DELETE/FULL, or upgrade SQLite to 3.51.3 or a patched backport.") from None
                raise
            if actual not in ("wal", "delete") or (desired == "delete" and actual != "delete"):
                raise TraceError("Cannot select a safe journal mode for the private explorer quota cache")
            # A VFS without WAL support may return DELETE. This is a supported
            # durable fallback, with its actual mode included in diagnostics.
            db.execute("PRAGMA synchronous=FULL")
            if actual == "wal":
                db.execute("PRAGMA wal_autocheckpoint=1000")
            self._check_files()
            self._connection = db
            self._journal_mode = actual
            return db
        except BaseException:
            db.close()
            raise

    @contextmanager
    def _transaction(self):
        # A SQLite connection must not be reused across fork, even when this
        # object's Python lock happens to be unlocked in the child process.
        self._check_owner()
        if not self._db_lock.acquire(timeout=LOCK_RETRY_SECONDS):
            raise _Busy()
        db = None
        initialized = False
        try:
            # Both the local mutex and SQLite writer wait remain brief, letting
            # callers check cancellation and request budgets between attempts.
            db = self._open_connection()
            db.execute("BEGIN IMMEDIATE")
            if not self._schema_ready:
                db.execute("""CREATE TABLE IF NOT EXISTS pacing (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_start REAL NOT NULL DEFAULT 0,
                    cooldown REAL NOT NULL DEFAULT 0)""")
                db.execute("INSERT OR IGNORE INTO pacing (id) VALUES (1)")
                db.execute("""CREATE TABLE IF NOT EXISTS clients (
                    id TEXT PRIMARY KEY, interval REAL NOT NULL,
                    expires_at REAL NOT NULL)""")
                if self.adaptive:
                    self._initialize_controller(db)
                initialized = True
            yield db
            db.commit()
            if initialized:
                self._schema_ready = True
        except (OSError, sqlite3.Error) as error:
            if db is not None:
                db.rollback()
            if _sqlite_busy(error):
                raise _Busy() from None
            raise TraceError("Cannot update the private explorer quota cache; check local cache permissions before retrying") from None
        except BaseException:
            if db is not None:
                db.rollback()
            raise
        finally:
            try:
                if self._closed and self._connection is not None:
                    self._connection.close()
                    self._connection = None
            finally:
                self._db_lock.release()

    def _initialize_controller(self, db):
        db.execute("""CREATE TABLE IF NOT EXISTS adaptive_clients (
            id TEXT PRIMARY KEY)""")
        db.execute("""CREATE TABLE IF NOT EXISTS adaptive_pacing (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            target_rps REAL NOT NULL, generation INTEGER NOT NULL,
            window_started REAL NOT NULL, successes INTEGER NOT NULL,
            last_pressure REAL NOT NULL, hold_until REAL NOT NULL)""")
        db.execute("INSERT OR IGNORE INTO adaptive_pacing VALUES (1, ?, 0, ?, 0, 0, 0)",
                   (1. / self.interval, self._clock()))

    def _controller(self, db, now):
        # Additive tables keep the pacing/clients schema usable by older
        # workers. Their registrations continue to act as fixed limits.
        row = db.execute("""SELECT target_rps, generation, window_started,
            successes, last_pressure, hold_until FROM adaptive_pacing WHERE id = 1""").fetchone()
        state = _AdaptiveState(*row)
        if not math.isfinite(state.target_rps) or state.target_rps <= 0:
            raise TraceError("Invalid learned explorer rate; check the private quota cache")
        # A long idle period must not dilute the next active learning window.
        if now - state.window_started > CLIENT_IDLE_SECONDS and not db.execute(
                "SELECT 1 FROM clients WHERE expires_at > ? LIMIT 1", (now,)).fetchone():
            state.window_started, state.successes = now, 0
        return state

    @staticmethod
    def _save_controller(db, state):
        db.execute("""UPDATE adaptive_pacing SET target_rps = ?, generation = ?,
            window_started = ?, successes = ?, last_pressure = ?, hold_until = ? WHERE id = 1""",
            (state.target_rps, state.generation, state.window_started, state.successes,
             state.last_pressure, state.hold_until))
        # Update every adaptive lease so an older worker's ordinary MAX(interval)
        # query sees the current shared target too. Fixed peers are untouched.
        db.execute("""UPDATE clients SET interval = ? WHERE id IN
            (SELECT id FROM adaptive_clients)""", (1. / state.target_rps,))

    def _clear_denied_hint(self):
        with self._feedback_lock:
            self._denied_hint = None

    def reserve(self):
        """Admit atomically, or reuse a recent local *denial* without writes.

        Parallel workers often ask about the same next start. Writing a renewed
        lease/controller for every waiter creates a disk-write stampede. A
        recent denial can only keep a caller waiting: each possible admission
        still goes through the shared database and rechecks peers and cooldowns.
        """
        self._check_owner()
        if self._closed:
            raise TraceError("Shared explorer pacing is closed")
        with self._feedback_lock:
            hint = self._denied_hint
            now = self._monotonic()
            if hint is not None and now < hint[1]:
                return replace(hint[2], wait_seconds=hint[0] - now)
            self._denied_hint = None
        admission, deadline, checked_at = self._reserve_uncached()
        if deadline is None:
            return admission
        now = self._monotonic()
        # The database commit happened after calculating this deadline. Do not
        # add commit latency to the caller's sleep. A positive delay is required
        # even when the deadline elapsed: this result did not grant permission.
        admission = replace(admission, wait_seconds=max(1e-6, deadline - now))
        if self.adaptive and admission.active_clients == 1 and deadline > now:
            # Multiple known clients retain immediate rechecks so a peer's
            # closing/revised fixed interval is reflected without a stale hint.
            # Long server cooldowns must still renew leases and flush feedback.
            refresh_at = min(deadline, checked_at + min(1., self._idle_seconds / 2))
            if refresh_at > now:
                with self._feedback_lock:
                    self._denied_hint = (deadline, refresh_at, admission)
        return admission

    def _reserve_uncached(self):
        pending = self._take_successes()
        try:
            with self._transaction() as db:
                now = self._clock()
                checked_at = self._monotonic()
                db.execute("DELETE FROM clients WHERE expires_at <= ?", (now,))
                state = None
                if self.adaptive:
                    state = self._controller(db, now)
                    state.add_successes(pending)
                    state.grow(now)
                    db.execute("DELETE FROM adaptive_clients WHERE id NOT IN (SELECT id FROM clients)")
                    db.execute("INSERT OR IGNORE INTO adaptive_clients VALUES (?)", (self._client_id,))
                    self._save_controller(db, state)
                own_interval = 1. / state.target_rps if state else self.interval
                db.execute("INSERT OR REPLACE INTO clients VALUES (?, ?, ?)",
                           (self._client_id, own_interval, now + self._idle_seconds))
                active, interval = db.execute("SELECT COUNT(*), MAX(interval) FROM clients").fetchone()
                last_start, cooldown = db.execute("SELECT last_start, cooldown FROM pacing WHERE id = 1").fetchone()
                # Derive the next start from the *current* shared interval.
                # A faster client cannot bypass a newly registered fixed peer.
                metadata = dict(mode="adaptive" if state else "fixed",
                                target_rps=state.target_rps if state else 1. / interval,
                                generation=state.generation if state else 0)
                deadline = max(last_start + interval, cooldown)
                if deadline > now:
                    reason = "server_cooldown" if cooldown >= last_start + interval else "shared_rate_limit"
                    return (Admission(False, deadline - now, active, 1. / interval, reason, **metadata),
                            checked_at + (deadline - now), checked_at)
                db.execute("UPDATE pacing SET last_start = ? WHERE id = 1", (now,))
                return Admission(True, active_clients=active, effective_rps=1. / interval, **metadata), None, None
        except _Busy:
            self._restore_successes(pending)
            return (Admission(False, LOCK_RETRY_SECONDS, reason="shared_rate_limit",
                              mode="adaptive" if self.adaptive else "fixed"), None, None)
        except BaseException:
            self._restore_successes(pending)
            raise

    def pressure(self, admission, seconds=0.):
        """Share rejection/backoff once per pressure generation.

        False denotes brief contention, just like cooldown(). Already completed
        response evidence is the caller's responsibility before this operation.
        """
        if not self.adaptive:
            return self.cooldown(seconds)
        self._check_owner()
        if self._closed:
            raise TraceError("Shared explorer pacing is closed")
        self._clear_denied_hint()
        seconds = max(1., seconds) if math.isfinite(seconds) else 60.
        try:
            with self._transaction() as db:
                now = self._clock()
                state = self._controller(db, now)
                state.pressure(admission, now, seconds)
                self._save_controller(db, state)
                db.execute("UPDATE pacing SET cooldown = MAX(cooldown, ?) WHERE id = 1", (now + seconds,))
            return True
        except _Busy:
            return False

    def cooldown(self, seconds):
        """Share Retry-After even when the reporting run will stop.

        Return False for brief lock contention; callers retry with cancellation
        and budget checks. Invalid infinite Retry-After still blocks other
        clients for a minute instead of allowing an immediate retry storm.
        """
        self._check_owner()
        if self._closed:
            raise TraceError("Shared explorer pacing is closed")
        self._clear_denied_hint()
        seconds = max(0., seconds) if math.isfinite(seconds) else 60.
        try:
            with self._transaction() as db:
                db.execute("UPDATE pacing SET cooldown = MAX(cooldown, ?) WHERE id = 1",
                           (self._clock() + seconds,))
            return True
        except _Busy:
            return False

    def close(self):
        self._check_owner()
        if self._closed:
            return
        self._closed = True
        try:
            with self._transaction() as db:
                db.execute("DELETE FROM clients WHERE id = ?", (self._client_id,))
        except (_Busy, TraceError):
            # A contended or crashed close has the same bounded idle lifetime.
            pass


class LocalExplorerQuota(_Feedback):
    """Equivalent adaptive policy for injected/offline transports without files."""

    def __init__(self, interval, *, adaptive=False, clock=None):
        if not math.isfinite(interval) or interval <= 0:
            raise TraceError("Explorer interval must be positive")
        self.interval = interval
        self._setup_feedback(adaptive)
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._closed = False
        self._last_start = 0.
        self._cooldown = 0.
        self._state = _AdaptiveState(1. / interval, 0, self._clock())

    def reserve(self):
        with self._lock:
            if self._closed:
                raise TraceError("Shared explorer pacing is closed")
            now = self._clock()
            state = self._state
            if self.adaptive:
                state.add_successes(self._take_successes())
                state.grow(now)
            interval = 1. / state.target_rps if self.adaptive else self.interval
            deadline = max(self._last_start + interval, self._cooldown)
            metadata = dict(active_clients=1, effective_rps=1. / interval,
                            mode="adaptive" if self.adaptive else "fixed",
                            target_rps=state.target_rps, generation=state.generation)
            if deadline > now:
                reason = "server_cooldown" if self._cooldown >= self._last_start + interval else "shared_rate_limit"
                return Admission(False, deadline - now, reason=reason, **metadata)
            self._last_start = now
            return Admission(True, **metadata)

    def pressure(self, admission, seconds=0.):
        if not self.adaptive:
            return self.cooldown(seconds)
        with self._lock:
            if self._closed:
                raise TraceError("Shared explorer pacing is closed")
            now = self._clock()
            seconds = max(1., seconds) if math.isfinite(seconds) else 60.
            self._state.pressure(admission, now, seconds)
            self._cooldown = max(self._cooldown, now + seconds)
            return True

    def cooldown(self, seconds):
        with self._lock:
            if self._closed:
                raise TraceError("Shared explorer pacing is closed")
            seconds = max(0., seconds) if math.isfinite(seconds) else 60.
            self._cooldown = max(self._cooldown, self._clock() + seconds)
            return True

    def close(self):
        with self._lock:
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
