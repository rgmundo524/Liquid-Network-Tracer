"""Durable, incremental Miro sync checkpoints.

The JSON file remains the portable snapshot format. While a writer is active, a
SQLite sidecar records explicit mutations, committed before the associated API
request or acknowledgement. Readers must use ``load_state`` to include those
records. The caller owns the existing cross-process sync lock; this module
serializes commits from that writer's worker threads.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import tempfile
import threading
import time
import weakref
import uuid

from .common import TraceError

_MARKER = "_sync_journal"
_VERSION = 1
_STATE_LOCKS = weakref.WeakValueDictionary()
_STATE_LOCKS_GUARD = threading.Lock()


class _StateChanged(TraceError):
    pass


def _state_lock(path):
    key = str(Path(path).resolve())
    with _STATE_LOCKS_GUARD:
        lock = _STATE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _STATE_LOCKS[key] = lock
        return lock


def journal_path(path):
    path = Path(path)
    return path.with_name(path.name + ".journal.sqlite3")


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _snapshot_text(state, token=None):
    snapshot = dict(state)
    if token is not None:
        snapshot[_MARKER] = {"version": _VERSION, "id": token}
    return json.dumps(snapshot, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_snapshot(path, text):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_snapshot(path):
    if not path.exists():
        return None, None
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        raise TraceError("Malformed Miro state snapshot; restore its last intact version") from None
    if not isinstance(value, dict):
        raise TraceError("Malformed Miro state snapshot; expected an object")
    return value, _sha(text)


def _path(value):
    if (not isinstance(value, (tuple, list)) or not value
            or any(not isinstance(part, str) or not part for part in value)
            or value[0] == _MARKER):
        raise TraceError("Invalid Miro journal mutation path")
    return tuple(value)


def _apply(state, changes):
    if not isinstance(changes, dict) or set(changes) != {"sets", "deletes"}:
        raise TraceError("Malformed Miro state journal operation")
    if not isinstance(changes["sets"], list) or not isinstance(changes["deletes"], list):
        raise TraceError("Malformed Miro state journal operation")
    for entry in changes["sets"]:
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            raise TraceError("Malformed Miro state journal set operation")
        path, value = _path(entry[0]), entry[1]
        target = state
        for part in path[:-1]:
            if part not in target:
                target[part] = {}
            target = target[part]
            if not isinstance(target, dict):
                raise TraceError("Miro journal mutation traverses a non-object field")
        target[path[-1]] = copy.deepcopy(value)
    for entry in changes["deletes"]:
        path = _path(entry)
        target = state
        for part in path[:-1]:
            if part not in target:
                target = None
                break
            target = target[part]
            if not isinstance(target, dict):
                raise TraceError("Miro journal mutation traverses a non-object field")
        if target is not None:
            target.pop(path[-1], None)


def _read_database(connection, snapshot, snapshot_hash):
    rows = connection.execute("SELECT key, value FROM metadata").fetchall()
    metadata = dict(rows)
    if (set(metadata) != {"version", "token", "base", "base_hash", "snapshot_hashes", "last_sequence"}
            or any(not isinstance(value, str) for value in metadata.values())):
        raise TraceError("Malformed Miro state journal metadata; restore the state and journal together")
    if metadata["version"] != str(_VERSION) or not metadata["token"]:
        raise TraceError("Unsupported Miro state journal version")
    if _sha(metadata["base"]) != metadata["base_hash"]:
        raise TraceError("Miro state journal snapshot checksum failed")
    try:
        accepted = json.loads(metadata["snapshot_hashes"])
        state = json.loads(metadata["base"])
        last_sequence = int(metadata["last_sequence"])
    except (ValueError, TypeError):
        raise TraceError("Malformed Miro state journal metadata") from None
    if (not isinstance(accepted, list) or not accepted or snapshot_hash not in accepted
            or not isinstance(state, dict) or _MARKER in state or last_sequence < 0):
        raise TraceError("Miro snapshot and journal do not match; restore both files together")
    marker = snapshot.get(_MARKER) if snapshot else None
    if marker is not None and marker != {"version": _VERSION, "id": metadata["token"]}:
        raise TraceError("Miro snapshot refers to a different state journal")
    expected = 1
    for sequence, payload, checksum in connection.execute(
            "SELECT sequence, payload, checksum FROM operations ORDER BY sequence"):
        if (sequence != expected or not isinstance(payload, str)
                or not isinstance(checksum, str) or _sha(payload) != checksum):
            raise TraceError("Miro state journal is incomplete or corrupt; restore its last intact version")
        try:
            changes = json.loads(payload)
        except (ValueError, TypeError):
            raise TraceError("Malformed Miro state journal operation") from None
        _apply(state, changes)
        expected += 1
    if expected - 1 != last_sequence:
        raise TraceError("Miro state journal has missing committed operations; restore its last intact version")
    return state, metadata


def _file_identity(path):
    try:
        value = path.stat()
    except FileNotFoundError:
        return None
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _recover_read_only_copy(path, snapshot, snapshot_hash):
    # SQLite cannot roll back a hot DELETE-mode journal on a read-only handle.
    # Recover a private copy instead, leaving dry runs and backups untouched.
    # Check every source file around the complete operation, including replay:
    # a racing writer must never make a torn copy look like authoritative state.
    sidecar = journal_path(path)
    rollback = sidecar.with_name(sidecar.name + "-journal")
    identities = {source: _file_identity(source) for source in (path, sidecar, rollback)}
    if identities[sidecar] is None or identities[rollback] is None:
        raise _StateChanged("Miro state journal changed while reading; retry after the active sync finishes")
    with tempfile.TemporaryDirectory(prefix="liquid-miro-recovery-") as directory:
        copied = Path(directory) / sidecar.name
        for source, destination in ((sidecar, copied), (rollback, copied.with_name(copied.name + "-journal"))):
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output)
        if any(_file_identity(source) != identity for source, identity in identities.items()):
            raise _StateChanged("Miro state journal changed while reading; retry after the active sync finishes")
        connection = sqlite3.connect(copied)
        try:
            connection.execute("BEGIN")
            state, _ = _read_database(connection, snapshot, snapshot_hash)
        finally:
            connection.close()
        if any(_file_identity(source) != identity for source, identity in identities.items()):
            raise _StateChanged("Miro state journal changed while reading; retry after the active sync finishes")
        return state


def load_state(path, default=None):
    """Read a portable snapshot plus committed operations, without writing files.

    A missing sidecar for an active snapshot or any inconsistent/corrupt journal
    fails closed. It must never silently resume from an older JSON mapping.
    """
    path = Path(path)
    sidecar = journal_path(path)
    sources = (path, sidecar, sidecar.with_name(sidecar.name + "-journal"))
    # Use the writer's lock for readers in this process. External writers still
    # use the existing sync flock; a preview may race them, so retry only when
    # source identities prove that a concurrent transition occurred.
    with _state_lock(path):
        for attempt in range(5):
            identities = tuple(_file_identity(source) for source in sources)
            try:
                return _load_state_once(path, default)
            except (_StateChanged, FileNotFoundError):
                pass
            except TraceError:
                if identities == tuple(_file_identity(source) for source in sources):
                    raise
            if attempt < 4:
                time.sleep(.01 * (attempt + 1))
    raise TraceError("Miro state journal is changing during an active sync; retry after that sync finishes")


def _load_state_once(path, default):
    snapshot, snapshot_hash = _read_snapshot(path)
    sidecar = journal_path(path)
    if not sidecar.exists():
        if snapshot is not None and _MARKER in snapshot:
            raise TraceError("Miro state journal is missing; restore the snapshot and journal together before syncing")
        if snapshot is not None:
            return snapshot
        if default is None:
            raise TraceError("Miro state snapshot does not exist")
        return copy.deepcopy(default)
    connection = None
    try:
        rollback = sidecar.with_name(sidecar.name + "-journal")
        if rollback.exists():
            # Even SQLite's read-only rollback detection can chmod a journal.
            # Copy first when present to leave original investigation files alone.
            return _recover_read_only_copy(path, snapshot, snapshot_hash)
        connection = sqlite3.connect(sidecar.resolve().as_uri() + "?mode=ro", uri=True)
        connection.execute("BEGIN")
        state, _ = _read_database(connection, snapshot, snapshot_hash)
        return state
    except sqlite3.Error as error:
        if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_READONLY_ROLLBACK:
            connection.close()
            connection = None
            try:
                return _recover_read_only_copy(path, snapshot, snapshot_hash)
            except sqlite3.Error:
                pass
        raise TraceError("Cannot read the Miro state journal safely; restore the state and journal together") from None
    finally:
        if connection is not None:
            connection.close()


class SyncState:
    """One locked writer session with O(changed-data) durable commits.

    Enter before mutating ``state``. Every change that should survive must be
    included in ``commit``. The context must outlive and drain all API workers.
    Exit materializes the committed state, including on ordinary exceptions;
    abrupt process termination leaves the sidecar available to ``load_state``.
    """

    def __init__(self, path, state):
        self.path = Path(path)
        self.sidecar = journal_path(path)
        self.state = state
        self._connection = None
        self._lock = _state_lock(path)
        self._closed = False
        self._sequence = 0
        self._token = None

    def __enter__(self):
        with self._lock:
            if self._connection is not None or self._closed:
                raise TraceError("Miro state journal session cannot be reused")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            snapshot, snapshot_hash = _read_snapshot(self.path)
            if self.sidecar.exists():
                if load_state(self.path) != self.state:
                    raise TraceError("Miro state changed before opening its journal; reload before syncing")
            elif snapshot is not None and _MARKER in snapshot:
                raise TraceError("Miro state journal is missing; restore both files before syncing")
            elif snapshot is not None and snapshot != self.state:
                raise TraceError("Miro state changed before opening its journal; reload before syncing")
            if not isinstance(self.state, dict) or _MARKER in self.state:
                raise TraceError("Invalid Miro state for incremental checkpointing")
            new_sidecar = not self.sidecar.exists()
            if new_sidecar:
                descriptor = os.open(self.sidecar, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
            try:
                os.chmod(self.sidecar, 0o600)
                self._connection = sqlite3.connect(self.sidecar, check_same_thread=False)
                self._connection.execute("PRAGMA journal_mode=DELETE")
                self._connection.execute("PRAGMA synchronous=FULL")
                self._connection.execute("PRAGMA busy_timeout=5000")
                with self._connection:
                    self._connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                    self._connection.execute("CREATE TABLE IF NOT EXISTS operations (sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL, checksum TEXT NOT NULL)")
                existing = self._connection.execute("SELECT value FROM metadata WHERE key='token'").fetchone()
                self._token = existing[0] if existing else uuid.uuid4().hex
                text = _snapshot_text(self.state, self._token)
                self._replace_base(self.state, [snapshot_hash, _sha(text)])
                _fsync_directory(self.path.parent)
                _write_snapshot(self.path, text)
                self._set_snapshot_hashes([_sha(text)])
            except BaseException:
                if self._connection is not None:
                    self._connection.close()
                    self._connection = None
                # An initialized sidecar may already be the only durable state.
                # Keep it for diagnosis/recovery, even when setup was interrupted.
                raise
            return self

    def _replace_base(self, state, snapshot_hashes):
        encoded = _encode(state)
        metadata = {"version": str(_VERSION), "token": self._token,
                    "base": encoded, "base_hash": _sha(encoded),
                    "snapshot_hashes": _encode(snapshot_hashes), "last_sequence": "0"}
        with self._connection:
            self._connection.execute("DELETE FROM operations")
            self._connection.executemany("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", metadata.items())
        self._sequence = 0

    def _set_snapshot_hashes(self, hashes):
        with self._connection:
            self._connection.execute("UPDATE metadata SET value=? WHERE key='snapshot_hashes'", (_encode(hashes),))

    def commit(self, sets=(), deletes=()):
        """Durably record explicit field mutations, then update the shared state."""
        with self._lock:
            if self._connection is None or self._closed:
                raise TraceError("Miro state journal session is closed")
            changes = {"sets": [[list(_path(path)), value] for path, value in sets],
                       "deletes": [list(_path(path)) for path in deletes]}
            if not changes["sets"] and not changes["deletes"]:
                return
            # Encode once to detach mutable arguments and enforce JSON values.
            try:
                payload = _encode(changes)
                changes = json.loads(payload)
            except (ValueError, TypeError):
                raise TraceError("Miro state journal values must be valid JSON") from None
            self._validate_changes(changes)
            sequence = self._sequence + 1
            try:
                with self._connection:
                    self._connection.execute("INSERT INTO operations(sequence, payload, checksum) VALUES (?, ?, ?)",
                                             (sequence, payload, _sha(payload)))
                    self._connection.execute("UPDATE metadata SET value=? WHERE key='last_sequence'", (str(sequence),))
            except sqlite3.Error:
                raise TraceError("Miro checkpoint could not be saved; stop syncing and preserve its journal") from None
            self._sequence = sequence
            _apply(self.state, changes)

    def _validate_changes(self, changes):
        # Copy only the ancestors that these operations traverse. A shallow copy
        # of state["items"] here would reintroduce quadratic work on large boards.
        skeleton = {}
        paths = [path for path, _ in changes["sets"]] + changes["deletes"]
        for path in paths:
            source, target = self.state, skeleton
            for part in path[:-1]:
                if part not in source:
                    break
                value = source[part]
                if not isinstance(value, dict):
                    target[part] = value
                    break
                target = target.setdefault(part, {})
                source = value
        _apply(skeleton, changes)

    def checkpoint(self):
        """Refresh the JSON snapshot at a phase boundary, keeping the journal active."""
        with self._lock:
            self._checkpoint(final=False)

    def _checkpoint(self, final):
        if self._connection is None or self._closed:
            raise TraceError("Miro state journal session is closed")
        snapshot, snapshot_hash = _read_snapshot(self.path)
        committed, _ = _read_database(self._connection, snapshot, snapshot_hash)
        text = _snapshot_text(committed, None if final else self._token)
        self._replace_base(committed, [snapshot_hash, _sha(text)])
        _write_snapshot(self.path, text)
        self._set_snapshot_hashes([_sha(text)])
        if final:
            self._connection.close()
            self._connection = None
            self.sidecar.unlink()
            _fsync_directory(self.path.parent)

    def __exit__(self, exc_type, exc, traceback):
        with self._lock:
            try:
                self._checkpoint(final=True)
            except Exception as error:
                if exc is None:
                    raise TraceError("Miro snapshot finalization failed; preserve its journal and retry reading the saved state") from error
                if hasattr(exc, "add_note"):
                    exc.add_note("Miro snapshot finalization also failed; committed operations remain in its journal.")
            finally:
                self._closed = True
                if self._connection is not None:
                    self._connection.close()
                    self._connection = None
        return False
