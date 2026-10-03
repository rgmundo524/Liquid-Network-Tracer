"""Bounded workers for distinct, already partitioned ELK section requests.

This scheduler owns one shared-resource registration. Call it outside any other
ELK lease; the supplied worker must be the direct worker, not iter_attempts.
"""

from concurrent.futures import CancelledError, FIRST_COMPLETED, ThreadPoolExecutor, wait
from queue import Empty, SimpleQueue
from threading import Event

from .elk_errors import ELK_FATAL_FAILURE_CODES, ElkWorkerFailure
from .elk_parallel import MEMORY_PRESSURE_CODES, POLL_SECONDS
from .render_runtime import elk_worker_budget, renderer_heap_is_auto, renderer_heap_mb
from .shared_render_resources import SharedRenderResources


def _emit(progress, event):
    if progress is None:
        return
    try:
        progress(event)
    except CancelledError:
        raise
    except Exception:
        pass  # A broken progress display does not invalidate a layout.


def _run_batch(requests, indices, seed, worker, progress, lease):
    """Drain workers before releasing their lease or returning any candidates."""
    cancel = Event()
    events = SimpleQueue()
    pending, outcomes, measurements = {}, {}, {}
    executor = ThreadPoolExecutor(max_workers=lease.worker_count, thread_name_prefix="liquid-elk-section")

    def run(index):
        return worker(requests[index - 1], [seed],
                      progress=lambda event: events.put((index, event)),
                      heap_mb=lease.heap_mb, cancel_event=cancel,
                      resource_lease_fd=lease.resource_lease_fd)

    def flush():
        while True:
            try:
                index, event = events.get_nowait()
            except Empty:
                return
            peak = event.get("peak_rss_mb")
            if event.get("stage") == "memory_measured" and type(peak) is int and 1 <= peak <= 2147483647:
                measurements[index] = max(measurements.get(index, 0), peak)
            _emit(progress, {**event, "section_index": index, "section_total": len(requests),
                             "worker_count": lease.worker_count, "active_workers": len(pending),
                             "total_heap_mb": lease.total_heap_mb, "heap_mb": lease.heap_mb,
                             "active_layouts": lease.active_layouts, "machine_heap_mb": lease.machine_heap_mb})

    try:
        for index in indices:
            pending[executor.submit(run, index)] = index
        while pending:
            finished, _ = wait(pending, timeout=POLL_SECONDS, return_when=FIRST_COMPLETED)
            # Handle failures before progress so invalid worker data cancels
            # siblings immediately. The direct worker normally raises a plain
            # TraceError for fatal categories; handle typed fatal errors too.
            for future in sorted(finished, key=lambda item: pending[item]):
                index = pending.pop(future)
                try:
                    outcomes[index] = future.result()
                except ElkWorkerFailure as exc:
                    if exc.failure_code in ELK_FATAL_FAILURE_CODES:
                        raise
                    outcomes[index] = exc
            flush()
        return outcomes, measurements
    finally:
        cancel.set()
        for future in pending:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)


def iter_sections(requests, seed, worker, progress, metadata):
    """Yield ``(one_based_index, candidates_or_worker_failure)`` in input order.

    The caller orders the largest section first for a measured pilot. Remaining
    sections share bounded CPU/heap leases, using the highest measured peak and
    maximum remaining graph dimensions. Missing telemetry keeps work serial.
    Memory failures retry once with an exclusive lease after siblings drain;
    later batches stay serial. Fatal errors or cancellation drain all workers
    before propagating. Requests and their layout preferences pass unchanged to
    the direct worker, which retains responsibility for credential isolation.
    Progress is delivered only on the iterator's calling thread.
    """
    requests = tuple(requests)
    metadata.update(execution="sequential", worker_count=1, memory_retry_count=0,
                    section_count=len(requests))
    if not requests:
        return
    # Suffix maxima avoid rescanning thousands of remaining sections per batch.
    dimensions = [(0, 0)] * (len(requests) + 1)
    for offset in range(len(requests) - 1, -1, -1):
        nodes, edges = dimensions[offset + 1]
        dimensions[offset] = (max(nodes, len(requests[offset].get("children", []))),
                              max(edges, len(requests[offset].get("edges", []))))

    next_index, sequential, observed_peak = 1, False, None
    with SharedRenderResources() as resources:
        while next_index <= len(requests):
            remaining = len(requests) - next_index + 1
            if sequential:
                worker_count, total_heap = 1, renderer_heap_mb()
                worker_heap = total_heap
            else:
                nodes, edges = dimensions[next_index - 1]
                worker_count, total_heap, worker_heap = elk_worker_budget(
                    remaining, peak_rss_mb=observed_peak, node_count=nodes, edge_count=edges)

            def report_resource(event, index=next_index, count=worker_count):
                _emit(progress, {"worker_count": count, "active_workers": 0, **event,
                                 "section_index": index, "section_total": len(requests)})

            lease = resources.acquire(worker_count, total_heap,
                                      peak_rss_mb=None if sequential else observed_peak,
                                      progress=report_resource, worker_heap_mb=worker_heap)
            worker_count = lease.worker_count
            metadata["worker_count"] = max(metadata["worker_count"], worker_count)
            if worker_count > 1:
                metadata["execution"] = "parallel"
            indices = range(next_index, next_index + worker_count)
            with lease:
                outcomes, measurements = _run_batch(requests, indices, seed, worker, progress, lease)

            for index in indices:
                outcome = outcomes.pop(index)
                if isinstance(outcome, ElkWorkerFailure) and outcome.failure_code in MEMORY_PRESSURE_CODES:
                    # The batch lease is released and all sibling processes
                    # have exited. Never nest a full-pool lease inside it.
                    sequential = True
                    metadata["memory_retry_count"] += 1

                    def report_retry_resource(event, section=index):
                        _emit(progress, {"worker_count": 1, "active_workers": 0, **event,
                                         "section_index": section, "section_total": len(requests)})

                    retry = resources.acquire(1, renderer_heap_mb(), progress=report_retry_resource,
                                              exclusive=True,
                                              refresh_heap=renderer_heap_mb if renderer_heap_is_auto() else None)
                    with retry:
                        _emit(progress, {"phase": "optimizing", "stage": "retrying_memory",
                                         "message": "Retrying this ELK section alone after other layout batches finish",
                                         "completed": 0, "total": 0, "failure_code": outcome.failure_code,
                                         "section_index": index, "section_total": len(requests),
                                         "worker_count": 1, "active_workers": 0,
                                         "heap_mb": retry.heap_mb, "total_heap_mb": retry.total_heap_mb,
                                         "active_layouts": retry.active_layouts, "machine_heap_mb": retry.machine_heap_mb})
                        retried, retry_measurements = _run_batch(requests, [index], seed, worker, progress, retry)
                    outcome = retried[index]
                    measurements.pop(index, None)
                    measurements.update(retry_measurements)
                if not isinstance(outcome, ElkWorkerFailure) and index in measurements:
                    observed_peak = max(observed_peak or 0, measurements[index])
                    metadata["peak_rss_mb"] = observed_peak
                yield index, outcome
            next_index += worker_count
