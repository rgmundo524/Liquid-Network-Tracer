"""Bounded ELK processes sharing one heap budget and ordered result delivery."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from queue import Empty, SimpleQueue
from threading import Event

from .elk_errors import ElkWorkerFailure
from .render_runtime import elk_worker_budget, renderer_heap_mb


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


def _batch(request, jobs, worker, progress_for_attempt, worker_count, total_heap_mb, heap_mb):
    """Drain one bounded batch. Never invoke a user's progress sink in a thread."""
    cancel_event = Event()
    events = SimpleQueue()
    executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="liquid-elk")
    pending, outcomes = {}, {}

    def run(index, seed):
        return worker(_request(request, index), [seed],
                      progress=lambda event: events.put((index, seed, event)),
                      heap_mb=heap_mb, cancel_event=cancel_event)

    def flush():
        while True:
            try:
                index, seed, event = events.get_nowait()
            except Empty:
                return
            _emit(progress_for_attempt(index, seed), {
                **event, "worker_count": worker_count, "active_workers": len(pending),
                "total_heap_mb": total_heap_mb, "heap_mb": heap_mb})

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

    Start with a full-budget pilot and use its measured peak RAM to size later
    batches. A process failure caused by possible memory pressure gets one retry alone
    after its parallel batch has drained. Remaining work stays sequential. A
    retry replaces that seed's outcome and never adds a configured attempt.
    """
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
            worker_count, total_heap_mb, heap_mb = elk_worker_budget(remaining, peak_rss_mb=observed_peak)
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

        if worker_count == 1:
            index, seed = jobs[0]
            report = measured_progress(index, seed)

            def serial_progress(event):
                _emit(report, {**event, "worker_count": 1, "active_workers": 1,
                               "total_heap_mb": total_heap_mb, "heap_mb": heap_mb})

            try:
                outcome = worker(_request(request, index), [seed], progress=serial_progress, heap_mb=heap_mb)
            except ElkWorkerFailure as exc:
                outcome = exc
            outcomes = {index: outcome}
            del outcome
        else:
            outcomes = _batch(request, jobs, worker, measured_progress,
                              worker_count, total_heap_mb, heap_mb)
            sequential = any(isinstance(outcome, ElkWorkerFailure)
                             and outcome.failure_code in MEMORY_PRESSURE_CODES for outcome in outcomes.values())
        for index, seed in jobs:
            outcome = outcomes.pop(index)
            if (worker_count > 1 and isinstance(outcome, ElkWorkerFailure)
                    and outcome.failure_code in MEMORY_PRESSURE_CODES):
                # No sibling is running here, so the retry can use a freshly
                # calculated full pool without multiplying the memory cap.
                retry_heap_mb = renderer_heap_mb()
                metadata["memory_retry_count"] += 1
                report = measured_progress(index, seed)
                fields = {"worker_count": 1, "active_workers": 1,
                          "total_heap_mb": retry_heap_mb, "heap_mb": retry_heap_mb}
                _emit(report, {"phase": "optimizing", "stage": "retrying_memory", "completed": 0, "total": 0,
                               "message": "Retrying this ELK attempt alone with the full shared heap budget",
                               "failure_code": outcome.failure_code, **fields})

                def retry_progress(event):
                    _emit(report, {**event, **fields})

                try:
                    outcome = worker(_request(request, index), [seed], progress=retry_progress, heap_mb=retry_heap_mb)
                except ElkWorkerFailure as exc:
                    outcome = exc
            if not isinstance(outcome, ElkWorkerFailure) and index in measurements:
                observed_peak = max(observed_peak or 0, measurements[index])
                metadata["peak_rss_mb"] = observed_peak
            yield index, seed, outcome
            del outcome
        next_index += worker_count
