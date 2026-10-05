"""Verified, immutable working indexes for sealed shared collection archives.

A cold index audits the entire archive. Warm reads authenticate only selected
records from that working snapshot, and use the source file inventory to reject
stale caches. An unchanged stat inventory is not a fresh audit of unused bytes.
The small catalog authenticates record membership, so a missing database row
cannot silently turn a recorded spend into an apparent end of a trail.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time

from .common import TraceError, canonical, digest, parse_outpoint, read_json
from .progress import report_progress

SCHEMA_VERSION = 1
_LARGE_FIELDS = {"transactions", "outputs", "links", "address_tx_counts", "observations"}


def _ordinary(path):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise TraceError("Shared snapshot index files cannot contain symbolic links")
    return path


def _stat(path):
    value = Path(path).lstat()
    if stat.S_ISLNK(value.st_mode):
        raise TraceError("Shared snapshot index files cannot contain symbolic links")
    if not stat.S_ISREG(value.st_mode):
        raise TraceError("Shared snapshot index requires ordinary files")
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


def _inventory(archive, *, require_snapshot=True):
    archive = _ordinary(archive)
    manifest = archive / "SHA256SUMS"
    before = _stat(manifest)
    raw = manifest.read_bytes()
    if _stat(manifest) != before:
        raise TraceError("Shared archive changed while reading its checksum manifest")
    files = {"SHA256SUMS": before}
    directories = {archive}
    try:
        for line in raw.decode("utf-8").splitlines():
            checksum, name = line.split("  ", 1)
            relative = Path(name)
            if (len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum)
                    or relative.is_absolute() or ".." in relative.parts or str(relative) != name
                    or name in files):
                raise ValueError
            # Check each shared parent directory once, instead of restatting
            # the same archive ancestors for every observation file.
            parent = archive
            for component in relative.parts[:-1]:
                parent = parent / component
                if parent not in directories:
                    mode = parent.lstat().st_mode
                    if stat.S_ISLNK(mode):
                        raise TraceError("Shared snapshot index files cannot contain symbolic links")
                    if not stat.S_ISDIR(mode):
                        raise TraceError("Shared archive contains an invalid evidence directory")
                    directories.add(parent)
            files[name] = _stat(archive / relative)
        required = {"trace.json", "graph.json", "miro-plan.json"}
        if require_snapshot:
            required.add("evidence-index.json")
        if not required <= files.keys():
            raise ValueError
    except (ValueError, UnicodeError) as error:
        raise TraceError("Shared snapshot index needs an intact sealed archive and observation index") from error
    return digest(raw), files


def _write_json(path, value):
    payload = canonical(value)
    with Path(path).open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _file_digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def _record(connection, kind, key, value):
    payload = canonical(value)
    checksum = digest(payload)
    connection.execute("INSERT INTO records VALUES (?, ?, ?, ?)", (kind, str(key), payload, checksum))
    return checksum


def _build(archive, target, catalog_path, identity, inventory, progress):
    from .cli import verify_export
    from .connections import _saved_connection_evidence
    from .pegout_paths import _evidence

    verify_export(archive, progress=progress)
    report_progress(progress, "loading_collection", 0, 1)
    state = read_json(archive / "trace.json")
    if (not isinstance(state, dict) or state.get("case_id") != identity["case_id"]
            or state.get("run_id") != identity["run_id"]
            or (identity["source"] is not None and state.get("source") != identity["source"])
            or ("shared_collection" in state and
                state["shared_collection"].get("dataset_id") != identity["case_id"])):
        raise TraceError("Shared run does not match the dataset identity")
    report_progress(progress, "loading_collection", 1, 1)
    state = _saved_connection_evidence(state, copy_state=False)
    # Validate every saved edge, the DAG and all peg-out metadata once, including
    # branches that a later bounded query will never visit.
    _evidence(state, respect_attribution_hops=False)
    observations = read_json(archive / "evidence-index.json")
    if (not isinstance(observations, list) or any(not isinstance(row, dict)
            or type(row.get("id")) is not int or row["id"] <= 0 for row in observations)
            or len({row["id"] for row in observations}) != len(observations)):
        raise TraceError("Shared collection has an invalid observation index")
    for row in observations:
        name = row.get("file")
        if (not isinstance(name, str) or name not in inventory or Path(name).parts[:1] != ("evidence",)
                or row.get("source") != state.get("source")):
            raise TraceError("Shared observation disagrees with its sealed source archive")
    metadata = {name: value for name, value in state.items() if name not in _LARGE_FIELDS}
    metadata["_snapshot_collected_depth"] = max((item.get("depth", 0) for item in state["outputs"].values()), default=0)
    # Membership is per transaction: most plots read only a small fraction of
    # these lists, while the catalog holds just one digest per transaction.
    members = {txid: {"transaction": None, "outputs": {}, "outgoing": {}, "incoming": []}
               for txid in state["transactions"]}
    descriptor, temporary_name = tempfile.mkstemp(prefix=".snapshot-", suffix=".sqlite", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary_catalog = temporary.with_suffix(".json")
    connection = None
    try:
        connection = sqlite3.connect(temporary)
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("CREATE TABLE records (kind TEXT NOT NULL, key TEXT NOT NULL, payload BLOB NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(kind, key)) WITHOUT ROWID")
        catalog = {"schema_version": SCHEMA_VERSION, "identity": identity, "source_inventory": inventory,
                   "metadata": metadata, "members": {}, "counts": {}, "observations": {}}
        total = sum(len(state.get(name, {})) for name in ("transactions", "outputs", "links", "address_tx_counts")) + len(observations)
        completed = 0
        report_progress(progress, "indexing_collection", completed, total)
        for txid, value in state["transactions"].items():
            members[txid]["transaction"] = _record(connection, "transaction", txid, value)
            completed += 1
            if completed % 128 == 0:
                report_progress(progress, "indexing_collection", completed, total)
        for key, value in state["outputs"].items():
            txid, index = parse_outpoint(key)
            if (txid not in members or key != f"{txid}:{index}" or value.get("txid") != txid
                    or type(value.get("vout")) is not int or value["vout"] != index
                    or value.get("outpoint", key) != key
                    or index >= len(state["transactions"][txid]["data"]["vout"])):
                raise TraceError("Shared output bookkeeping disagrees with its exact saved output")
            members[txid]["outputs"][key] = _record(connection, "output", key, value)
            completed += 1
            if completed % 128 == 0:
                report_progress(progress, "indexing_collection", completed, total)
        for key, value in state["links"].items():
            parent, _ = parse_outpoint(key)
            value = {**value, "outpoint": key}
            members[parent]["outgoing"][key] = _record(connection, "link", key, value)
            members[value["spending_txid"]]["incoming"].append(key)
            completed += 1
            if completed % 128 == 0:
                report_progress(progress, "indexing_collection", completed, total)
        for key, value in state.get("address_tx_counts", {}).items():
            catalog["counts"][key] = _record(connection, "count", key, value)
            completed += 1
        for value in observations:
            catalog["observations"][str(value["id"])] = _record(connection, "observation", value["id"], value)
            completed += 1
            if completed % 128 == 0:
                report_progress(progress, "indexing_collection", completed, total)
        for key, value in members.items():
            value["incoming"].sort()
            catalog["members"][key] = _record(connection, "member", key, value)
        connection.commit()
        connection.close()
        connection = None
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        if _inventory(archive) != (identity["manifest_sha256"], inventory):
            raise TraceError("Shared archive changed while building its snapshot index")
        if state.get("shared_collection", {}).get("dataset_id") == identity["case_id"]:
            from .run_summaries import remember_run_summary
            remember_run_summary(archive, state)
        catalog["database_sha256"] = _file_digest(temporary)
        # Publication is under the cross-process build lock. If interrupted
        # between these replaces, the next opener detects and rebuilds it.
        temporary.replace(target)
        catalog["database_stat"] = _stat(target)
        _write_json(temporary_catalog, {"payload": catalog, "sha256": digest(canonical(catalog))})
        temporary_catalog.replace(catalog_path)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        report_progress(progress, "indexing_collection", total, total)
    finally:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)
        temporary_catalog.unlink(missing_ok=True)


class SnapshotIndex:
    """Read-only access to authenticated records from one verified snapshot."""

    def __init__(self, archive, path, catalog):
        self.archive, self.path = archive, path
        self._catalog = catalog
        self.metadata = catalog["metadata"]
        self.manifest_sha256 = catalog["identity"]["manifest_sha256"]
        self.manifest_names = set(catalog["source_inventory"]) - {"SHA256SUMS"}
        self._members = {}
        self._connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            schema = self._connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='records'").fetchone()
            if schema is None:
                raise TraceError("Shared snapshot index has an invalid database schema")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _read(self, kind, key, expected):
        if _stat(self.path) != self._catalog["database_stat"]:
            raise TraceError("Shared snapshot index changed during use; retry to rebuild it")
        try:
            row = self._connection.execute("SELECT payload, sha256 FROM records WHERE kind=? AND key=?", (kind, str(key))).fetchone()
            if row is None or row[1] != expected or digest(row[0]) != expected:
                raise TraceError("Shared snapshot index record is missing or corrupt; rebuild the index")
            return json.loads(row[0])
        except (sqlite3.DatabaseError, ValueError, TypeError) as error:
            raise TraceError("Shared snapshot index record is corrupt; rebuild the index") from error

    def _member(self, txid):
        expected = self._catalog["members"].get(txid)
        if expected is None:
            return None
        if txid not in self._members:
            self._members[txid] = self._read("member", txid, expected)
        return self._members[txid]

    def transaction(self, txid):
        member = self._member(txid)
        return self._read("transaction", txid, member["transaction"]) if member is not None else None

    def output(self, key):
        member = self._member(parse_outpoint(key)[0])
        expected = member["outputs"].get(key) if member else None
        return self._read("output", key, expected) if expected is not None else None

    def link(self, key):
        member = self._member(parse_outpoint(key)[0])
        expected = member["outgoing"].get(key) if member else None
        return self._read("link", key, expected) if expected is not None else None

    def outgoing(self, txid):
        member = self._member(txid)
        return [self._read("link", key, value) for key, value in sorted(member["outgoing"].items())] if member else []

    def incoming(self, txid):
        member = self._member(txid)
        return [self.link(key) for key in member["incoming"]] if member else []

    def counts(self, addresses):
        return {key: self._read("count", key, self._catalog["counts"][key])
                for key in addresses if key in self._catalog["counts"]}

    def observation(self, identity):
        expected = self._catalog["observations"].get(str(identity))
        return self._read("observation", identity, expected) if expected is not None else None


def _cached(archive, target, catalog_path, identity, inventory):
    if not target.exists() or not catalog_path.exists():
        return None
    try:
        envelope = read_json(catalog_path)
        catalog = envelope["payload"]
        if (envelope["sha256"] != digest(canonical(catalog)) or catalog["schema_version"] != SCHEMA_VERSION
                or catalog["identity"] != identity or catalog["source_inventory"] != inventory
                or catalog["database_stat"] != _stat(target)):
            return None
        return SnapshotIndex(archive, target, catalog)
    except (KeyError, ValueError, TypeError, OSError, sqlite3.DatabaseError, TraceError):
        return None


@contextmanager
def _gate(lock_path, progress):
    """One process prepares a revision while waiters remain cancellable."""
    with lock_path.open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                report_progress(progress, "waiting_collection_index", 0, 1)
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _cache_directory(archive):
    directory = _ordinary(archive.parent.parent / "indexes")
    directory.mkdir(exist_ok=True)
    return directory


@contextmanager
def open_snapshot_index(archive, *, case_id, run_id, source=None, progress=None):
    """Open/build one cache under a cancellable cross-process publication lock."""
    archive = _ordinary(archive)
    manifest_sha256, inventory = _inventory(archive)
    identity = {"schema_version": SCHEMA_VERSION, "archive": str(archive), "case_id": case_id,
                "run_id": run_id, "source": source, "manifest_sha256": manifest_sha256}
    key = digest(canonical(identity))
    directory = _cache_directory(archive)
    target = _ordinary(directory / (key + ".sqlite"))
    catalog_path = _ordinary(directory / (key + ".json"))
    lock_path = _ordinary(directory / (key + ".lock"))
    with _gate(lock_path, progress):
        if _inventory(archive) != (manifest_sha256, inventory):
            raise TraceError("Shared archive changed while waiting for its snapshot index")
        index = _cached(archive, target, catalog_path, identity, inventory)
        if index is None:
            _build(archive, target, catalog_path, identity, inventory, progress)
            index = _cached(archive, target, catalog_path, identity, inventory)
            if index is None:
                raise TraceError("Shared snapshot index could not be published intact")
        else:
            report_progress(progress, "reusing_collection_index", 1, 1)
    try:
        yield index
    finally:
        index.close()


def verified_source_identity(archive, *, case_id, run_id, progress=None):
    """Check a private archive's source without imposing new topology rules.

    Historical private archives are valid compatibility evidence even when they
    predate observation indexes or contain no transaction graph at all. Their
    first lookup still verifies every sealed byte; reuse authenticates the small
    source identity and requires the exact same source-file stat inventory.
    """
    from .cli import verify_export

    archive = _ordinary(archive)
    manifest_sha256, inventory = _inventory(archive, require_snapshot=False)
    binding = {"schema_version": SCHEMA_VERSION, "chain_identity_version": 1, "archive": str(archive), "case_id": case_id,
               "run_id": run_id, "manifest_sha256": manifest_sha256}
    key = "source-" + digest(canonical(binding))
    directory = _cache_directory(archive)
    path = _ordinary(directory / (key + ".json"))
    lock_path = _ordinary(directory / (key + ".lock"))
    with _gate(lock_path, progress):
        if _inventory(archive, require_snapshot=False) != (manifest_sha256, inventory):
            raise TraceError("Saved archive changed while waiting to check its source")
        if path.exists():
            try:
                envelope = read_json(path)
                payload = envelope["payload"]
                if (envelope["sha256"] == digest(canonical(payload))
                        and payload["binding"] == binding and payload["source_inventory"] == inventory):
                    result = payload["identity"]
                    if (result["case_id"] == case_id and result["run_id"] == run_id
                            and isinstance(result["source"], str) and result["source"]):
                        report_progress(progress, "reusing_collection_index", 1, 1)
                        return result
            except (KeyError, ValueError, TypeError, OSError):
                pass
        verify_export(archive, progress=progress)
        report_progress(progress, "loading_collection", 0, 1)
        state = read_json(archive / "trace.json")
        if (not isinstance(state, dict) or state.get("case_id") != case_id or state.get("run_id") != run_id
                or not isinstance(state.get("source"), str) or not state["source"]):
            raise TraceError("Saved run does not match the investigation and source identity")
        from .networks import blockchain
        result = {"case_id": case_id, "run_id": run_id, "source": state["source"], "blockchain": blockchain(state)}
        del state
        report_progress(progress, "loading_collection", 1, 1)
        if _inventory(archive, require_snapshot=False) != (manifest_sha256, inventory):
            raise TraceError("Saved archive changed while checking its source identity")
        payload = {"binding": binding, "source_inventory": inventory, "identity": result}
        descriptor, temporary_name = tempfile.mkstemp(prefix=".source-", suffix=".json", dir=directory)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            _write_json(temporary, {"payload": payload, "sha256": digest(canonical(payload))})
            temporary.replace(path)
            handle = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(handle)
            finally:
                os.close(handle)
        finally:
            temporary.unlink(missing_ok=True)
        return result
