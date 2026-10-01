"""Same-user ELK CPU/heap leases, without storing graph data or credentials.

Running heaps are immutable. New searches use spare capacity immediately or
share capacity at the next batch boundary. Lease locks are inherited by Node, so a
killed Python owner cannot make a still-running child's reservation disappear.
"""

import fcntl
import json
import os
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .common import TraceError
from .render_runtime import _available_bytes, _available_cpu_count


POLL_SECONDS = .2
SCORING_GRACE_SECONDS = 5
MAX_STATE_BYTES = 1024 * 1024


def _capacity():
    available = _available_bytes()
    heap = 1024 if available is None else min(2147483647, available * 9 // (10 * 1024 * 1024))
    if heap < 1:
        raise TraceError("Too little available memory to start the local renderer; free memory and retry the saved run")
    return heap, _available_cpu_count()


def _open_private(path):
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise OSError("unsafe resource file")
        os.fchmod(descriptor, 0o600)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _fair_share(total, demands, target):
    """Water-fill capped demands with conservative integer shares."""
    pending = dict(demands)
    result = {}
    while pending:
        share = total // len(pending)
        small = [key for key, demand in pending.items() if demand <= share]
        if small:
            for key in small:
                result[key] = pending.pop(key)
                total -= result[key]
            continue
        # A zero share means there are more searches than CPU slots. FIFO
        # admission below time-shares the slots, rather than permitting zero
        # workers or oversubscribing CPUs.
        for key in pending:
            result[key] = share
        break
    return result.get(target, 0)


@dataclass
class RenderLease:
    coordinator: object
    worker_count: int
    total_heap_mb: int
    heap_mb: int
    active_layouts: int
    machine_heap_mb: int

    @property
    def resource_lease_fd(self):
        return self.coordinator.lease_fd

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.coordinator.release()
        return False


class SharedRenderResources:
    """A live layout search; short flock transactions never cover worker time.

    Only active/queued searches and a five-second scoring grace count toward fair
    shares. Paused iterators and idle web servers therefore cannot reserve capacity
    forever. Explicit heap/worker settings are upper bounds, not extra capacity.
    """

    def __init__(self, *, directory=None, capacity=None, clock=None, sleep=None):
        self._capacity = capacity or _capacity
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self.identity = uuid.uuid4().hex
        self.lease_fd = None
        if directory is None:
            cache = os.environ.get("XDG_CACHE_HOME", "")
            base = Path(cache) if cache and Path(cache).is_absolute() else Path.home() / ".cache"
            directory = base / "liquid-network-tracer" / "render-resources"
        self.directory = Path(directory)
        self.state_path = self.directory / "state.json"
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise OSError("unsafe resource directory")
            self.directory.chmod(0o700)
            self.lease_fd = _open_private(self.directory / (self.identity + ".lease"))
            fcntl.flock(self.lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self._transaction() as state:
                state["jobs"][self.identity] = {"mode": "idle", "idle_until": 0, "ticket": 0,
                                                "wanted_heap": 1, "wanted_workers": 1,
                                                "heap": 0, "workers": 0}
        except (OSError, ValueError, TypeError):
            self.close()
            raise TraceError("Cannot open shared ELK resource coordination; check local cache permissions. No layout worker was started") from None
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
        return False

    def close(self):
        if self.lease_fd is None:
            return
        # Do not LOCK_UN: inherited descriptors must keep this lock held if
        # a child survived its owner. Reaping verifies that the lock is free.
        os.close(self.lease_fd)
        self.lease_fd = None
        with self._transaction():
            pass

    def _reap(self, state):
        for identity in list(state["jobs"]):
            if identity == self.identity and self.lease_fd is not None:
                continue
            path = self.directory / (identity + ".lease")
            descriptor = _open_private(path)
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                del state["jobs"][identity]
                path.unlink()
            finally:
                os.close(descriptor)

    @contextmanager
    def _transaction(self):
        descriptor = None
        temporary = None
        try:
            descriptor = _open_private(self.directory / "coordinator.lock")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            if self.state_path.exists():
                info = self.state_path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > MAX_STATE_BYTES:
                    raise ValueError("invalid resource state")
                state = json.loads(self.state_path.read_text(encoding="ascii"))
                self._validate(state)
            else:
                state = {"version": 1, "ticket": 0, "pool_heap": 0, "jobs": {}}
            self._reap(state)
            yield state
            temporary = self.directory / (uuid.uuid4().hex + ".tmp")
            out = _open_private(temporary)
            with os.fdopen(out, "w", encoding="ascii") as stream:
                json.dump(state, stream, separators=(",", ":"), sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
        except (OSError, ValueError, TypeError, KeyError):
            raise TraceError("Cannot update shared ELK resource coordination; no new layout worker was started. Check the local cache") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _validate(state):
        if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("jobs"), dict):
            raise ValueError("invalid resource state")
        for field in ("ticket", "pool_heap"):
            if type(state.get(field)) is not int or state[field] < 0:
                raise ValueError("invalid resource state")
        for identity, job in state["jobs"].items():
            if len(identity) != 32 or any(character not in "0123456789abcdef" for character in identity):
                raise ValueError("invalid resource owner")
            if not isinstance(job, dict) or job.get("mode") not in ("idle", "waiting", "active"):
                raise ValueError("invalid resource owner")
            for field in ("ticket", "wanted_heap", "wanted_workers", "heap", "workers"):
                if type(job.get(field)) is not int or job[field] < 0:
                    raise ValueError("invalid resource allowance")
            if type(job.get("idle_until")) not in (int, float) or not 0 <= job["idle_until"] < 1e20:
                raise ValueError("invalid resource owner")

    def _try_acquire(self, workers, total_heap_mb, peak_rss_mb, exclusive=False, refresh_heap=None,
                     worker_heap_mb=None):
        with self._transaction() as state:
            now = self._clock()
            job = state["jobs"][self.identity]
            if job["mode"] == "active":
                raise TraceError("An ELK resource batch is already running")
            if job["mode"] != "waiting":
                state["ticket"] += 1
                job.update(mode="waiting", ticket=state["ticket"], wanted_heap=total_heap_mb, wanted_workers=workers)
            active = [other for other in state["jobs"].values() if other["mode"] == "active"]
            heap_used = sum(other["heap"] for other in active)
            workers_used = sum(other["workers"] for other in active)
            available_heap, cpu_count = self._capacity()
            # Re-estimate free memory at every boundary. While workers run,
            # their existing allowances are accounted for but never increased
            # merely because those heaps have not yet filled up.
            if not active:
                state["pool_heap"] = available_heap
            else:
                state["pool_heap"] = min(state["pool_heap"], available_heap + heap_used)
            contenders = {key: other for key, other in state["jobs"].items()
                          if other["mode"] != "idle" or other["idle_until"] > now}
            count = len(contenders)
            first = min((other["ticket"], key) for key, other in contenders.items() if other["mode"] == "waiting")[1]
            if exclusive and not active and first == self.identity and refresh_heap is not None:
                total_heap_mb = refresh_heap()
                if type(total_heap_mb) is not int or total_heap_mb < 1:
                    raise TraceError("ELK resource requests require a positive heap limit")
                job["wanted_heap"] = total_heap_mb
            heap = max(1, _fair_share(state["pool_heap"],
                                      {key: other["wanted_heap"] for key, other in contenders.items()}, self.identity))
            cpu = max(1, _fair_share(cpu_count,
                                     {key: other["wanted_workers"] for key, other in contenders.items()}, self.identity))
            if exclusive:
                heap, cpu = min(total_heap_mb, state["pool_heap"]), 1
            memory_workers = max(1, heap // max(1024, 2 * peak_rss_mb)) if peak_rss_mb else 1
            # A peer may still hold a larger CPU share from the preceding
            # boundary. Use the remaining slots now without shrinking it.
            workers = min(workers, cpu, memory_workers, max(1, cpu_count - workers_used))
            per_worker = heap // workers
            if worker_heap_mb is not None and not exclusive:
                # A smaller CPU share must not turn unused sibling allowances
                # into a larger reservation for the workers that did fit.
                per_worker = min(per_worker, worker_heap_mb)
            heap = per_worker * workers
            free_heap = max(0, min(available_heap, state["pool_heap"] - heap_used))
            reason = ("memory_retry" if exclusive and active else
                      "cpu" if workers > cpu_count - workers_used else
                      "memory" if heap > free_heap else
                      "fifo" if first != self.identity else None)
            self._last_status = {
                "running_layouts": len(active),
                "waiting_layouts": sum(other["mode"] == "waiting" for other in state["jobs"].values()),
                "reserved_heap_mb": heap_used, "available_heap_mb": free_heap,
                "cpu_slots": cpu_count, "reserved_workers": workers_used,
            }
            if reason:
                self._last_status["wait_reason"] = reason
                return None, count, state["pool_heap"]
            job.update(mode="active", heap=heap, workers=workers)
            self._last_status.update(running_layouts=len(active) + 1,
                                     waiting_layouts=self._last_status["waiting_layouts"] - 1,
                                     reserved_heap_mb=heap_used + heap,
                                     available_heap_mb=max(0, free_heap - heap),
                                     reserved_workers=workers_used + workers)
            return RenderLease(self, workers, heap, per_worker, count, state["pool_heap"]), count, state["pool_heap"]

    def acquire(self, workers, total_heap_mb, *, peak_rss_mb=None, progress=None, exclusive=False, refresh_heap=None,
                worker_heap_mb=None):
        if type(workers) is not int or workers < 1 or type(total_heap_mb) is not int or total_heap_mb < 1:
            raise TraceError("ELK resource requests require positive worker and heap limits")
        if worker_heap_mb is not None and (type(worker_heap_mb) is not int or worker_heap_mb < 1):
            raise TraceError("ELK resource requests require a positive per-worker heap limit")
        started = self._clock()
        next_report = started
        while True:
            lease, count, capacity = self._try_acquire(workers, total_heap_mb, peak_rss_mb, exclusive, refresh_heap,
                                                       worker_heap_mb)
            now = self._clock()
            if progress and (lease is not None or now >= next_report):
                event = {"phase": "optimizing", "stage": "resource_allocated" if lease else "resource_wait",
                         "completed": 0, "total": 0, "active_layouts": count,
                         "machine_heap_mb": capacity, "elapsed_seconds": max(0, int(now - started)),
                         **self._last_status}
                if lease:
                    event.update(worker_count=lease.worker_count, active_workers=0,
                                 total_heap_mb=lease.total_heap_mb, heap_mb=lease.heap_mb,
                                 message=f"ELK resource allowance: {lease.worker_count} workers, {lease.total_heap_mb:,} MiB across {count} active layouts")
                else:
                    event["message"] = f"Waiting for shared ELK resources; {count} active layouts"
                try:
                    progress(event)
                except Exception:
                    pass
                next_report = now + 5
            if lease is not None:
                return lease
            self._sleep(POLL_SECONDS)

    def release(self):
        with self._transaction() as state:
            state["jobs"][self.identity].update(mode="idle", heap=0, workers=0,
                                                 idle_until=self._clock() + SCORING_GRACE_SECONDS)
