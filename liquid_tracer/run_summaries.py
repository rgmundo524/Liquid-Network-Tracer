"""Small rebuildable UI summaries; never authority for evidence or mutations.

Legacy archives are read by one bounded daemon worker. The request path reads
only a small cached summary and file metadata, never a large trace. Cache
bindings use file identity and timestamps, not a substitute for evidence audits.
"""
from collections import OrderedDict
from copy import deepcopy
import heapq
import itertools
import json
import os
from pathlib import Path
import queue
import re
import stat
import tempfile
import threading

from .common import TraceError, canonical, digest, parse_outpoint
from .group_hops import reference_name
from .performance import public_performance

_VERSION = 1
_MAX_CACHE_BYTES = 4 * 1024 * 1024
_QUEUE_LIMIT = 64
_RUN_ID = re.compile(r"[a-zA-Z0-9]{16}\Z")
_SPECIAL = re.compile(r'["\\\x00-\x1f]')
_SPACE = re.compile(r"[ \t\r\n]*")
_FIELDS = {"case_id", "run_id", "status", "stop_reason", "started_at", "stats", "seeds", "limits",
           "hop_reference_name", "performance", "source", "collection_source", "shared_collection"}
_queue = queue.PriorityQueue(maxsize=_QUEUE_LIMIT)
_sequence = itertools.count()
_lock = threading.Lock()
_pending = set()
_results = OrderedDict()
_worker = None


class _JSONStream:
    """Validate JSON while retaining only explicitly requested small values."""
    def __init__(self, stream):
        self.stream, self.buffer, self.position, self.captured = stream, "", 0, 0

    def char(self):
        if self.position == len(self.buffer):
            self.buffer = self.stream.read(64 * 1024)
            self.position = 0
        return self.buffer[self.position:self.position + 1]

    def space(self):
        while self.char():
            self.position = _SPACE.match(self.buffer, self.position).end()
            if self.position < len(self.buffer):
                break
        return self.char()

    def expect(self, wanted):
        if self.space() != wanted:
            raise ValueError("Malformed saved trace JSON")
        self.position += 1

    def charge(self, count):
        self.captured += count
        if self.captured > _MAX_CACHE_BYTES:
            raise ValueError("Saved summary metadata exceeds its bounded size")

    def string(self, capture):
        self.expect('"')
        parts = [] if capture else None
        while self.char():
            match = _SPECIAL.search(self.buffer, self.position)
            end = match.start() if match else len(self.buffer)
            if capture:
                self.charge(end - self.position)
                parts.append(self.buffer[self.position:end])
            self.position = end
            if match is None:
                continue
            character = self.char()
            self.position += 1
            if character == '"':
                return json.loads('"' + "".join(parts) + '"') if capture else None
            if character != "\\":
                raise ValueError("Control character in saved trace JSON")
            escaped = self.char()
            if not escaped or escaped not in '"\\/bfnrtu':
                raise ValueError("Invalid saved trace JSON escape")
            self.position += 1
            token = "\\" + escaped
            if escaped == "u":
                for _ in range(4):
                    character = self.char()
                    if not character or character not in "0123456789abcdefABCDEF":
                        raise ValueError("Invalid saved trace JSON Unicode escape")
                    self.position += 1
                    token += character
            if capture:
                self.charge(len(token))
                parts.append(token)
        raise ValueError("Unterminated saved trace JSON string")

    def value(self, capture=False, depth=0, omit_keys=frozenset()):
        if depth > 128:
            raise ValueError("Saved trace JSON is too deeply nested")
        kind = self.space()
        if kind == '"':
            return self.string(capture)
        if kind in ("{", "["):
            self.position += 1
            close = "}" if kind == "{" else "]"
            result = ({} if kind == "{" else []) if capture else None
            if self.space() == close:
                self.position += 1
                return result
            while True:
                key = None
                if kind == "{":
                    key = self.string(capture)
                    self.expect(":")
                keep = capture and (kind != "{" or key not in omit_keys)
                item = self.value(keep, depth + 1)
                if keep:
                    self.charge(32)
                    if kind == "{":
                        result[key] = item
                    else:
                        result.append(item)
                separator = self.space()
                if separator == close:
                    self.position += 1
                    return result
                self.expect(",")
        token = []
        while self.char() and self.char() not in " \t\r\n,]}":
            token.append(self.char())
            self.position += 1
            if len(token) > 128:
                raise ValueError("Saved trace JSON scalar is too long")
        value = json.loads("".join(token))
        return value if capture else None

    def transaction(self):
        if self.space() != "{":
            self.value()
            return {}
        self.position += 1
        result = {}
        if self.space() == "}":
            self.position += 1
            return result
        while True:
            key = self.string(True)
            self.expect(":")
            value = self.value(key in ("depth", "reference_hops"))
            if key in ("depth", "reference_hops"):
                result[key] = value
            if self.space() == "}":
                self.position += 1
                return result
            self.expect(",")

    def transactions(self):
        self.expect("{")
        result = {"count": 0, "depth": None, "reference_hops": None,
                  "depth_valid": True, "reference_hops_valid": True}
        if self.space() == "}":
            self.position += 1
            return result
        while True:
            self.string(False)
            self.expect(":")
            retained = self.captured
            row = self.transaction()
            self.captured = retained  # Per-record keys/depths are not retained.
            result["count"] += 1
            _add_depth(result, row)
            if self.space() == "}":
                self.position += 1
                return result
            self.expect(",")

    def summary_fields(self):
        self.expect("{")
        result = {}
        if self.space() != "}":
            while True:
                key = self.string(True)
                self.expect(":")
                if key == "transactions":
                    result["_summary_depths"] = self.transactions()
                else:
                    value = self.value(key in _FIELDS, omit_keys={"service_controls"}
                                       if key in ("collection_source", "shared_collection") else frozenset())
                    if key in _FIELDS:
                        result[key] = value
                if self.space() == "}":
                    break
                self.expect(",")
        self.position += 1
        if self.space():
            raise ValueError("Trailing content in saved trace JSON")
        return result


def _add_depth(result, row):
    for field in ("depth", "reference_hops"):
        if field == "reference_hops" and field in row and row[field] is None:
            continue
        depth = row.get(field)
        if type(depth) is not int or not 0 <= depth <= 2 ** 53 - 1:
            result[field + "_valid"] = False
        else:
            result[field] = max(result[field] or 0, depth)


def _read_summary_fields(path):
    with Path(path).open("r", encoding="utf-8") as stream:
        return _JSONStream(stream).summary_fields()


def _ordinary(path):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise TraceError("Saved run summary paths cannot contain symbolic links")
    return path


def _signature(path):
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise TraceError("Saved run summaries require ordinary archive files")
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


def _binding(archive, case_id):
    archive = _ordinary(archive)
    if not isinstance(case_id, str) or not case_id or not _RUN_ID.fullmatch(archive.name) or not archive.is_dir():
        raise TraceError("Invalid saved run summary identity")
    return {"version": _VERSION, "archive": str(archive), "case_id": case_id, "run_id": archive.name,
            "manifest": _signature(_ordinary(archive / "SHA256SUMS")),
            "trace": _signature(_ordinary(archive / "trace.json"))}


def _cache_path(binding):
    identity = {key: binding[key] for key in ("archive", "case_id", "run_id")}
    return _ordinary(Path(binding["archive"]).parent.parent / "run-summaries" / (digest(canonical(identity)) + ".json"))


def _summary(state, binding):
    if (not isinstance(state, dict) or state.get("case_id") != binding["case_id"]
            or state.get("run_id") != binding["run_id"] or not isinstance(state.get("stats", {}), dict)):
        raise TraceError("Saved run summary disagrees with its archive identity")
    depths = state.get("_summary_depths")
    if depths is None:
        transactions = state.get("transactions", {})
        if not isinstance(transactions, dict):
            raise TraceError("Saved run summary has invalid transaction records")
        depths = {"count": len(transactions), "depth": None, "reference_hops": None,
                  "depth_valid": True, "reference_hops_valid": True}
        for row in transactions.values():
            _add_depth(depths, row if isinstance(row, dict) else {})
    stats = state.get("stats", {})
    count, frontier = stats.get("transactions_cumulative", depths["count"]), stats.get("frontier_count", 0)
    result = {"id": binding["run_id"],
              "status": state.get("status") if isinstance(state.get("status"), str) else None,
              "stop_reason": state.get("stop_reason") if isinstance(state.get("stop_reason"), str) else None,
              "created_at": state.get("started_at") if isinstance(state.get("started_at"), str) else None,
              "transaction_count": count if type(count) is int and count >= 0 else depths["count"],
              "frontier_count": frontier if type(frontier) is int and frontier >= 0 else 0}
    name = reference_name(state)
    field = "reference_hops" if name else "depth"
    if depths[field + "_valid"] and depths[field] is not None:
        result["collected_hops"] = depths[field]
    if name:
        result["hop_reference_name"] = name
    limits = state.get("limits", {})
    if isinstance(limits, dict) and type(limits.get("max_hops")) is int:
        result["max_hops"] = limits["max_hops"]
    seeds = state.get("seeds")
    if isinstance(seeds, list) and all(isinstance(seed, str) for seed in seeds):
        try:
            result["seeds"] = [f"{txid}:{index}" for txid, index in sorted(set(map(parse_outpoint, seeds)))]
            result["seed_count"] = len(result["seeds"])
        except (TraceError, ValueError):
            pass
    performance = public_performance(state.get("performance"))
    if performance:
        result["performance"] = performance
    if isinstance(state.get("source"), str):
        result["source"] = state["source"]
    for field in ("collection_source", "shared_collection"):
        if isinstance(state.get(field), dict):
            result[field] = {key: deepcopy(value) for key, value in state[field].items() if key != "service_controls"}
    return result


def _remember(key, result):
    with _lock:
        _results[key] = deepcopy(result)
        _results.move_to_end(key)
        while len(_results) > 256:
            _results.popitem(last=False)


def _save(binding, summary, status):
    path = _cache_path(binding)
    path.parent.mkdir(exist_ok=True)
    _ordinary(path)
    payload = {"binding": binding, "status": status, "summary": summary}
    raw = canonical({"payload": payload, "sha256": digest(canonical(payload))})
    if len(raw) > _MAX_CACHE_BYTES:
        raise ValueError("Saved run summary exceeds its bounded cache size")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".summary-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _cached(binding):
    path = _cache_path(binding)
    try:
        if _signature(path)[2] > _MAX_CACHE_BYTES:
            return None
        value = json.loads(path.read_bytes())
        payload = value["payload"]
        if (value["sha256"] != digest(canonical(payload)) or payload["binding"] != binding
                or payload["status"] not in ("ready", "unavailable")
                or (payload["status"] == "ready" and (not isinstance(payload["summary"], dict)
                                                       or payload["summary"].get("id") != binding["run_id"]))):
            return None
        return payload["summary"], payload["status"]
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _build(archive, case_id, binding, key):
    summary, status = None, "unavailable"
    try:
        if _binding(archive, case_id) != binding:
            return
        summary = _summary(_read_summary_fields(archive / "trace.json"), binding)
        if _binding(archive, case_id) != binding:
            return
        status = "ready"
    except OSError:
        return  # Transient read failures must remain retryable without changing the archive.
    except (TraceError, ValueError, TypeError, KeyError, RecursionError):
        pass  # Invalid content is unavailable until its source identity changes.
    except Exception:
        return  # Unexpected background failures cannot stop jobs or poison the cache.
    try:
        if _binding(archive, case_id) == binding:
            _save(binding, summary, status)
    except Exception:
        pass
    _remember(key, (summary, status))


def _work():
    while True:
        _, _, (archive, case_id, binding, key) = _queue.get()
        try:
            _build(archive, case_id, binding, key)
        finally:
            with _lock:
                _pending.discard(key)
            _queue.task_done()


def _enqueue(job, priority):
    """Called with _lock; promote bounded queued work without duplicating it."""
    key = job[3]
    if key in _pending:
        if priority:
            with _queue.mutex:
                for position, entry in enumerate(_queue.queue):
                    if entry[2][3] == key and entry[0] != 0:
                        _queue.queue[position] = (0, entry[1], entry[2])
                        heapq.heapify(_queue.queue)
                        break
        return
    entry = (0 if priority else 1, next(_sequence), job)
    try:
        _queue.put_nowait(entry)
    except queue.Full:
        if not priority:
            return
        # A selected investigation must not wait behind a full history queue.
        # Replace the newest background entry; its next explicit request can
        # enqueue it again. The queue's unfinished-task count is unchanged.
        with _queue.mutex:
            candidates = [(item[1], position) for position, item in enumerate(_queue.queue) if item[0] != 0]
            if not candidates:
                return
            _, position = max(candidates)
            displaced = _queue.queue[position]
            _pending.discard(displaced[2][3])
            _queue.queue[position] = entry
            heapq.heapify(_queue.queue)
    _pending.add(key)


def get_run_summary(archive, case_id, *, schedule=True, priority=False):
    """Return (display metadata, readiness), without reading an archive body.

    Dashboard callers can pass schedule=False to inspect existing summaries
    without eagerly queueing every investigation's historical trace. Selected
    investigations can use priority=True to precede queued history work.
    """
    global _worker
    try:
        archive = _ordinary(archive)
        binding = _binding(archive, case_id)
        key = digest(canonical(binding))
        with _lock:
            cached = deepcopy(_results.get(key))
        if cached is None:
            cached = _cached(binding)
            if cached is not None:
                _remember(key, cached)
        if cached is not None:
            if _binding(archive, case_id) != binding:
                return None, "loading"
            return cached
        if schedule:
            with _lock:
                _enqueue((archive, case_id, binding, key), priority)
                if key in _pending and (_worker is None or not _worker.is_alive()):
                    _worker = threading.Thread(target=_work, name="liquid-run-summaries", daemon=True)
                    _worker.start()
        return None, "loading"
    except (TraceError, OSError, ValueError, TypeError):
        return None, "unavailable"


def remember_run_summary(archive, state):
    """Best-effort write after sealing, using the state already held by a job."""
    try:
        archive = _ordinary(archive)
        binding = _binding(archive, state["case_id"])
        summary = _summary(state, binding)
        if _binding(archive, state["case_id"]) != binding:
            return False
        _save(binding, summary, "ready")
        _remember(digest(canonical(binding)), (summary, "ready"))
        return True
    except Exception:
        return False
