"""Private, same-machine Miro credit pacing shared by access-token identity.

Miro applies its allowance per user/application. Reusing the same access token
across tracer processes coordinates that allowance here, without persisting the
token. Different tokens for the same user/application cannot be associated
locally and still rely on Miro's response headers and 429 responses.
"""

import hashlib
import math
import os
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .common import TraceError


TARGET_CREDITS_PER_MINUTE = 95_000
RESERVATION_LIFETIME_SECONDS = 120


def _finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


class SharedMiroQuota:
    """Atomically reserve starts; never sleep while holding a database lock.

    Short-lived reservation records account for other processes' in-flight
    requests when applying response headers. Crashed reservations expire after
    the HTTP timeout; spent credits are deliberately never refunded.
    """

    def __init__(self, token, *, directory=None, clock=None):
        if not isinstance(token, str) or not token:
            raise TraceError("A Miro access token is required for shared API pacing")
        self._clock = clock or time.time
        if directory is None:
            cache = os.environ.get("XDG_CACHE_HOME", "")
            base = Path(cache) if cache and Path(cache).is_absolute() else Path.home() / ".cache"
            directory = base / "liquid-network-tracer" / "miro-quota"
        directory = Path(directory)
        identity = hashlib.sha256(b"liquid-tracer-miro-quota-v1\0" + token.encode()).hexdigest()
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
            with self._transaction() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS quota (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    next_start REAL NOT NULL DEFAULT 0,
                    cooldown REAL NOT NULL DEFAULT 0,
                    remaining REAL,
                    reset_at REAL NOT NULL DEFAULT 0,
                    credit_limit REAL NOT NULL DEFAULT 100000)""")
                db.execute("INSERT OR IGNORE INTO quota (id) VALUES (1)")
                db.execute("""CREATE TABLE IF NOT EXISTS reservations (
                    id TEXT PRIMARY KEY, credits INTEGER NOT NULL,
                    expires_at REAL NOT NULL)""")
        except (OSError, sqlite3.Error):
            raise TraceError("Cannot open the private Miro quota cache; check local cache permissions") from None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        # Connections are scoped to short transactions, never to the sync.
        return False

    @contextmanager
    def _transaction(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=5)
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except (OSError, sqlite3.Error):
            if db is not None:
                db.rollback()
            raise TraceError("Cannot update the private Miro quota cache; no new request was sent") from None
        except BaseException:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    def reserve(self, credits, spacing=0):
        """Return ``(reservation_id, 0)`` or ``(None, seconds_to_wait)``."""
        if type(credits) is not int or not 0 < credits <= TARGET_CREDITS_PER_MINUTE:
            raise TraceError("Miro request credits must be a positive integer within the minute allowance")
        with self._transaction() as db:
            now = self._clock()
            db.execute("DELETE FROM reservations WHERE expires_at <= ?", (now,))
            next_start, cooldown, remaining, reset_at, credit_limit = db.execute(
                "SELECT next_start, cooldown, remaining, reset_at, credit_limit FROM quota WHERE id = 1"
            ).fetchone()
            if now >= reset_at:
                remaining = None
            deadline = max(next_start, cooldown)
            if remaining is not None and remaining < credits:
                deadline = max(deadline, reset_at)
            if deadline > now:
                return None, deadline - now
            reservation = uuid.uuid4().hex
            # Keep the local target below the documented allowance, and slow
            # down further if the response advertises a smaller allowance.
            target = min(TARGET_CREDITS_PER_MINUTE, credit_limit * .95)
            gap = max(spacing, 60 * credits / target)
            db.execute("""UPDATE quota SET next_start = ?, remaining = ? WHERE id = 1""",
                       (now + gap, None if remaining is None else remaining - credits))
            db.execute("INSERT INTO reservations VALUES (?, ?, ?)",
                       (reservation, credits, now + RESERVATION_LIFETIME_SECONDS))
            return reservation, 0.

    def finish(self, reservation, headers=None, status=None):
        """Release one in-flight record and conservatively merge server limits."""
        values = {str(key).lower(): value for key, value in (headers or {}).items()}
        with self._transaction() as db:
            now = self._clock()
            # A duplicate or expired completion must not apply stale headers.
            deleted = db.execute("DELETE FROM reservations WHERE id = ?", (reservation,)).rowcount
            if not deleted:
                return
            db.execute("DELETE FROM reservations WHERE expires_at <= ?", (now,))
            cooldown, remaining, reset_at, credit_limit = db.execute(
                "SELECT cooldown, remaining, reset_at, credit_limit FROM quota WHERE id = 1"
            ).fetchone()
            reported_limit = _finite(values.get("x-ratelimit-limit"))
            if reported_limit is not None and reported_limit > 0:
                credit_limit = min(100_000., reported_limit)
            reported_remaining = _finite(values.get("x-ratelimit-remaining"))
            reported_reset = _finite(values.get("x-ratelimit-reset"))
            if reported_remaining is not None and reported_remaining >= 0 and reported_reset is not None and reported_reset > now:
                inflight = db.execute("SELECT COALESCE(SUM(credits), 0) FROM reservations").fetchone()[0]
                budget = max(0., reported_remaining - inflight)
                remaining = budget if remaining is None or now >= reset_at else min(remaining, budget)
                reset_at = max(reset_at, reported_reset)
            if status == 429:
                delay = _finite(values.get("retry-after"))
                if delay is None and reported_reset is not None:
                    delay = reported_reset - now
                cooldown = max(cooldown, now + max(1., 2. if delay is None else delay))
            db.execute("UPDATE quota SET cooldown = ?, remaining = ?, reset_at = ?, credit_limit = ? WHERE id = 1",
                       (cooldown, remaining, reset_at, credit_limit))
