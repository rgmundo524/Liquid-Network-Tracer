"""Stable human-readable preview numbers, separate from immutable evidence.

Numbers belong to an investigation and are never recycled. The first migration
uses the library's report-file timestamp/ID order. This only stats legacy files;
it does not read graphs, manifests, or even additional report bodies. A number
is a display label, never a statement that a preview's evidence was verified.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat

from .common import TraceError, save_json

REGISTRY = "preview-numbers.json"
LOCK = "preview-numbers.lock"
MAX_NUMBER = 2 ** 53 - 1
MAX_BYTES = 16 * 1024 * 1024
UNAVAILABLE = ("Saved preview numbers are unavailable; restore preview-numbers.json "
               "before assigning more numbers. Saved preview files are unchanged.")
UNWRITABLE = ("New preview numbers could not be saved. Check this investigation's "
              "write access and free disk space; existing preview numbers are preserved.")
CAPACITY = ("New preview numbers could not be saved because this investigation's "
            "numbering registry reached its size limit. Existing preview numbers are preserved.")


def _read(case, identity):
    from .plots import PREVIEW_ID, _ordinary

    path = _ordinary(case / REGISTRY)
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
        raise TraceError(UNAVAILABLE)
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise TraceError(UNAVAILABLE) from None
    if (len(raw) > MAX_BYTES or not isinstance(value, dict)
            or set(value) != {"schema_version", "case_id", "next_number", "numbers"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["case_id"] != identity or not isinstance(value["numbers"], dict)
            or type(value["next_number"]) is not int
            or not 1 <= value["next_number"] <= MAX_NUMBER):
        raise TraceError(UNAVAILABLE)
    numbers = value["numbers"]
    if (any(not PREVIEW_ID.fullmatch(key) or type(number) is not int
            or not 1 <= number < value["next_number"] for key, number in numbers.items())
            or len(set(numbers.values())) != len(numbers)
            or value["next_number"] != max(numbers.values(), default=0) + 1):
        raise TraceError(UNAVAILABLE)
    return value


@contextmanager
def _lock(case):
    from .plots import _ordinary

    path = _ordinary(case / LOCK)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NONBLOCK | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise TraceError(UNAVAILABLE)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _completed(case):
    """Stat-only discovery, run only for bootstrap or an unregistered arrival."""
    from .plots import PREVIEW_ID, _ordinary

    previews = _ordinary(case / "previews")
    result = []
    for directory in previews.glob("*-plots-*"):
        try:
            if (not PREVIEW_ID.fullmatch(directory.name)
                    or not stat.S_ISDIR(directory.lstat().st_mode)):
                continue
            report = (directory / "plot.json").lstat()
            manifest = (directory / "SHA256SUMS").lstat()
            if stat.S_ISREG(report.st_mode) and stat.S_ISREG(manifest.st_mode):
                result.append((report.st_mtime_ns, directory.name))
        except OSError:
            continue
    return sorted(result)


def _lookup(case, identity, wanted):
    """Atomic ancillary registry edits use a dedicated, short filesystem lock."""
    from .plots import _ordinary

    known = {}
    try:
        registry = _read(case, identity)
        known = registry["numbers"] if registry else {}
        if wanted <= known.keys():
            return known, None
        with _lock(case):
            registry = _read(case, identity)
            registry = registry or {"schema_version": 1, "case_id": identity,
                                    "next_number": 1, "numbers": {}}
            known = dict(registry["numbers"])
            if wanted <= known.keys():
                return known, None
            for _, name in _completed(case):
                if name in registry["numbers"]:
                    continue
                if registry["next_number"] >= MAX_NUMBER:
                    raise TraceError(UNAVAILABLE)
                registry["numbers"][name] = registry["next_number"]
                registry["next_number"] += 1
            if registry["numbers"] != known:
                # Match save_json's representation so an append cannot replace a
                # readable registry with one that our own reader rejects.
                size = len((json.dumps(registry, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
                if size > MAX_BYTES:
                    return known, CAPACITY
                # save_json fsyncs a temporary file and atomically replaces the
                # registry. Never follow a user-supplied temporary-file symlink.
                temporary = _ordinary(case / (REGISTRY + ".tmp"))
                if temporary.exists() and not stat.S_ISREG(temporary.stat().st_mode):
                    raise TraceError(UNAVAILABLE)
                save_json(_ordinary(case / REGISTRY), registry)
            return registry["numbers"], None if wanted <= registry["numbers"].keys() else UNAVAILABLE
    except TraceError:
        return known, UNAVAILABLE
    except OSError:
        return known, UNWRITABLE


def numbered_previews(case, values, *, identity=None):
    """Add durable display numbers without changing any checksummed preview."""
    from .investigations import read_case
    from .plots import PREVIEW_ID, _ordinary

    if not values:
        return []
    case = _ordinary(Path(case))
    identity = identity or read_case(case)["case_id"]
    wanted = {value.get("preview_id", value.get("id")) for value in values}
    if any(not isinstance(name, str) or not PREVIEW_ID.fullmatch(name) for name in wanted):
        raise TraceError("Invalid saved preview identifier")
    numbers, notice = _lookup(case, identity, wanted)
    result = []
    for value in values:
        item = dict(value)
        item.pop("preview_number", None)
        item.pop("preview_number_notice", None)
        number = numbers.get(value.get("preview_id", value.get("id")))
        if number is not None:
            item["preview_number"] = number
        if notice:
            item["preview_number_notice"] = notice
        result.append(item)
    return result
