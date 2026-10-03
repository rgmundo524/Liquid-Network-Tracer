"""Bounded ELK processes sharing one heap budget and ordered result delivery."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from queue import Empty, SimpleQueue
from threading import Event

from .elk_errors import ElkWorkerFailure
from .render_runtime import elk_worker_budget, renderer_heap_is_auto, renderer_heap_mb
from .shared_render_resources import SharedRenderResources


MEMORY_PRESSURE_CODES = frozenset({"heap_exhausted", "memory_exhausted", "worker_killed"})
POLL_SECONDS = 0.2


def _emit(report, event):
    try:
        report(event)
    except Exception:
        pass  # Progress remains advisory, including delivery on the main thread.


def _request(request, index):
    # A larger search preserves the previous seeds and their profile choices.
    ordered = bool(request.get("branchNodeOrder")) and not request.get("centerNodeOrder") and index % 2 == 1
    profile = ("balanced" if len(request["children"]) <= 300
               and index == (2 if request.get("branchNodeOrder") else 1)
               and not request.get("centerNodeOrder") else "flow_weighted")
    # Start with coherent local branches, including a one-attempt search.
    # Alternate with unconstrained ELK so genuine joins can choose a better
    # arrangement. The stable prefix and configured attempt budget are retained.
    return {**request, "branchProfile": profile,
            "boundaryOrdering": ordered}


def _batch(request, jobs, worker, progress_for_attempt, worker_count, total_heap_mb, heap_mb, lease):
    """Drain one bounded batch. Never invoke a user's progress sink in a thread."""
    cancel_event = Event()
    events = SimpleQueue()
    executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="liquid-elk")
    pending, outcomes = {}, {}

    def run(index, seed):
        return worker(_request(request, index), [seed],
                      progress=lambda event: events.put((index, seed, event)),
                      heap_mb=heap_mb, cancel_event=cancel_event, resource_lease_fd=lease.resource_lease_fd)

    def flush():
        while True:
            try:
                index, seed, event = events.get_nowait()
            except Empty:
                return
            _emit(progress_for_attempt(index, seed), {
                **event, "worker_count": worker_count, "active_workers": len(pending),
                "total_heap_mb": total_heap_mb, "heap_mb": heap_mb,
                "active_layouts": lease.active_layouts, "machine_heap_mb": lease.machine_heap_mb})

    try:
        for index, seed in jobs:
            pending[executor.submit(run, index, seed)] = (index, seed)
        while pending:
            finished, _ = wait(pending, timeout=POLL_SECONDS, return_when=FIRST_COMPLETED)
            # Read fatal failures before calling progress sinks. An engine
            # setup failure must promptly stop every still-running sibling.
            for future in sorted(finished, key=lambda value: pending[value][0]):
                index, _ = pending.pop(future)
                try:
                    outcomes[index] = future.result()
                except ElkWorkerFailure as exc:
                    outcomes[index] = exc
            flush()
        return outcomes
    finally:
        # Also covers cancellation while submitting a future or while a
        # worker thread is still inside Popen. It will observe this event as
        # soon as ownership of the process has been established.
        cancel_event.set()
        for future in pending:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)


def iter_attempts(request, seeds, worker, progress_for_attempt, metadata):
    """Yield each configured seed once, in order, with bounded live candidates.

    Start with a graph-sized pilot and use its measured peak RAM to size later
    batches. A process failure caused by possible memory pressure in a parallel
    batch or reduced allowance gets one retry alone with the full pool after
    other batches drain. Remaining work stays sequential. A retry replaces that
    seed's outcome and never adds a configured attempt.
    """
    if not request["children"]:
        yield from _iter_attempts(request, seeds, worker, progress_for_attempt, metadata, None)
        return
    with SharedRenderResources() as resources:
        yield from _iter_attempts(request, seeds, worker, progress_for_attempt, metadata, resources)


def _iter_attempts(request, seeds, worker, progress_for_attempt, metadata, resources):
    metadata.update(execution="sequential", worker_count=1, memory_retry_count=0)
    next_index, sequential, observed_peak = 1, False, None
    while next_index <= len(seeds):
        if not request["children"]:
            seed = seeds[next_index - 1]
            yield next_index, seed, [{"seed": seed, "nodes": [], "edges": [],
                                     "branchProfile": _request(request, next_index)["branchProfile"],
                                     "branchBoundary": False}]
            next_index += 1
            continue
        # Recalculate only after the previous processes have exited and their
        # candidate batch has been consumed. Thus 90% means the entire pool,
        # never 90% independently allocated to every Node process.
        remaining = len(seeds) - next_index + 1
        if sequential:
            total_heap_mb = renderer_heap_mb()
            worker_count, heap_mb = 1, total_heap_mb
        else:
            # The first successful measured attempt is the pilot. If an older
            # worker cannot report peak RAM, keep running alone rather than
            # guessing how many complete graphs will fit in memory.
            worker_count, total_heap_mb, heap_mb = elk_worker_budget(
                remaining, peak_rss_mb=observed_peak,
                node_count=len(request["children"]), edge_count=len(request.get("edges", [])))
        requested_heap_mb = total_heap_mb
        lease = resources.acquire(worker_count, total_heap_mb, peak_rss_mb=None if sequential else observed_peak,
                                  progress=progress_for_attempt(next_index, seeds[next_index - 1]),
                                  worker_heap_mb=heap_mb)
        worker_count, total_heap_mb, heap_mb = lease.worker_count, lease.total_heap_mb, lease.heap_mb
        metadata["worker_count"] = max(metadata["worker_count"], worker_count)
        if worker_count > 1:
            metadata["execution"] = "parallel"
        jobs = [(index, seeds[index - 1]) for index in range(next_index, next_index + worker_count)]
        measurements = {}

        def measured_progress(index, seed):
            report = progress_for_attempt(index, seed)

            def record(event):
                peak = event.get("peak_rss_mb")
                if event.get("stage") == "memory_measured" and type(peak) is int and 1 <= peak <= 2147483647:
                    measurements[index] = max(measurements.get(index, 0), peak)
                _emit(report, event)
            return record

        with lease:
            if worker_count == 1:
                index, seed = jobs[0]
                report = measured_progress(index, seed)

                def serial_progress(event):
                    _emit(report, {**event, "worker_count": 1, "active_workers": 1,
                                   "total_heap_mb": total_heap_mb, "heap_mb": heap_mb,
                                   "active_layouts": lease.active_layouts, "machine_heap_mb": lease.machine_heap_mb})

                try:
                    outcome = worker(_request(request, index), [seed], progress=serial_progress, heap_mb=heap_mb,
                                     resource_lease_fd=lease.resource_lease_fd)
                except ElkWorkerFailure as exc:
                    outcome = exc
                outcomes = {index: outcome}
                del outcome
            else:
                outcomes = _batch(request, jobs, worker, measured_progress,
                                  worker_count, total_heap_mb, heap_mb, lease)
                sequential = any(isinstance(outcome, ElkWorkerFailure)
                                 and outcome.failure_code in MEMORY_PRESSURE_CODES for outcome in outcomes.values())
        for index, seed in jobs:
            outcome = outcomes.pop(index)
            if (isinstance(outcome, ElkWorkerFailure) and outcome.failure_code in MEMORY_PRESSURE_CODES
                    and (worker_count > 1 or total_heap_mb < requested_heap_mb or lease.active_layouts > 1
                         or (not sequential and heap_mb < renderer_heap_mb()))):
                # No sibling is running here, so the retry can use a freshly
                # calculated full pool without multiplying the memory cap.
                metadata["memory_retry_count"] += 1
                report = measured_progress(index, seed)
                sequential = True
                retry_lease = resources.acquire(1, renderer_heap_mb(), progress=report, exclusive=True,
                                                refresh_heap=renderer_heap_mb if renderer_heap_is_auto() else None)
                retry_heap_mb = retry_lease.heap_mb
                fields = {"worker_count": 1, "active_workers": 1,
                          "total_heap_mb": retry_heap_mb, "heap_mb": retry_heap_mb,
                          "active_layouts": retry_lease.active_layouts, "machine_heap_mb": retry_lease.machine_heap_mb}
                _emit(report, {"phase": "optimizing", "stage": "retrying_memory", "completed": 0, "total": 0,
                               "message": "Retrying this ELK attempt alone after other layout batches finish",
                               "failure_code": outcome.failure_code, **fields})

                def retry_progress(event):
                    _emit(report, {**event, **fields})

                with retry_lease:
                    try:
                        outcome = worker(_request(request, index), [seed], progress=retry_progress, heap_mb=retry_heap_mb,
                                         resource_lease_fd=retry_lease.resource_lease_fd)
                    except ElkWorkerFailure as exc:
                        outcome = exc
            if not isinstance(outcome, ElkWorkerFailure) and index in measurements:
                observed_peak = max(observed_peak or 0, measurements[index])
                metadata["peak_rss_mb"] = observed_peak
            yield index, seed, outcome
            del outcome
        next_index += worker_count
