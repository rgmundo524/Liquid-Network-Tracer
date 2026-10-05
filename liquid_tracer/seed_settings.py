"""Editable investigation roots, separate from immutable collection evidence."""

from contextlib import ExitStack
import fcntl
from pathlib import Path

from .common import TraceError, parse_outpoint, save_json
from .investigations import read_case


class SeedEditConflict(TraceError):
    """A stale edit or active collector prevents a seed update."""


def normalize_seeds(value, *, allow_empty=False):
    if (not isinstance(value, list)
            or (not value and not allow_empty)
            or any(not isinstance(seed, str) for seed in value)):
        raise TraceError("Provide at least one starting output as transaction-hash:output-number.")
    try:
        return [f"{txid}:{index}" for txid, index in sorted(set(map(parse_outpoint, value)))]
    except ValueError:
        raise TraceError("Starting output numbers must fit in a 32-bit unsigned integer.") from None


def seed_settings(case, metadata=None):
    metadata = read_case(case) if metadata is None else metadata
    revision = metadata.get("seed_revision", 0)
    if type(revision) is not int or not 0 <= revision <= 2 ** 53 - 1:
        raise TraceError("The saved starting-output revision is invalid; restore case.json.")
    return {"seeds": normalize_seeds(metadata.get("seeds", []), allow_empty=True), "revision": revision}


def _safe(path):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise TraceError("Starting-output settings cannot use symbolic links.")
    return path


def save_seeds(case, seeds, *, expected_revision):
    normalized = normalize_seeds(seeds)
    if type(expected_revision) is not int or not 0 <= expected_revision <= 2 ** 53 - 1:
        raise TraceError("Saving starting outputs requires the current revision.")
    case = _safe(case)
    _safe(case / "case.json")
    # Readers and writers in other processes use the same locks. Reserve the
    # workspace first so creation of a shared collector cannot race this edit.
    with ExitStack() as stack:
        def lock_file(path, mode):
            lock = stack.enter_context(_safe(path).open("a"))
            try:
                fcntl.flock(lock, mode | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SeedEditConflict("Collection or another investigation operation is active. Save starting outputs after it finishes.") from None

        lock_file(case.parent / ".shared-collection.lock", fcntl.LOCK_SH)
        shared = _safe(case.parent / ".shared-collection")
        if shared.is_dir():
            lock_file(shared / "trace.lock", fcntl.LOCK_SH)
        lock_file(case / "trace.lock", fcntl.LOCK_EX)
        lock_file(case / "case.lock", fcntl.LOCK_EX)
        metadata = read_case(case)
        current = seed_settings(case, metadata)
        if current["revision"] != expected_revision:
            raise SeedEditConflict("Starting outputs changed in another window. Reload them before saving again.")
        if current["seeds"] == normalized:
            return current
        if current["revision"] == 2 ** 53 - 1:
            raise TraceError("The starting-output revision limit was reached.")
        metadata.update(seeds=normalized, seed_revision=current["revision"] + 1)
        save_json(case / "case.json", metadata)
    return {"seeds": normalized, "revision": metadata["seed_revision"]}
