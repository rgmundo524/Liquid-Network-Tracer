"""Bounded Miro I/O with one pacing gate and coordinator-owned checkpoints."""

import math
import queue
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .common import TraceError, canonical


class MiroRequestNotSent(TraceError):
    """The pacing gate refused a request before the transport was called."""


class _Cancelled(Exception):
    """A request that never reached the transport after its batch failed."""


def _retry_delay(headers):
    values = {str(key).lower(): value for key, value in headers.items()}
    try:
        delay = (float(values["retry-after"]) if "retry-after" in values
                 else float(values["x-ratelimit-reset"]) - time.time())
    except (KeyError, TypeError, ValueError):
        delay = 2.
    if not math.isfinite(delay) or delay > 30:
        raise TraceError("Miro rate limited for more than 30 seconds; wait for the limit to reset and rerun sync")
    return max(1., delay)


class MiroRequests:
    """Overlap latency, while pacing request starts across all worker threads.

    Only the calling thread invokes ``accept`` and progress callbacks. On a
    failed batch, requests already sent may still succeed: those acknowledgments
    are drained before propagating the first error, so callers can save them.
    New POSTs should remain serial, with their existing uncertainty journal.
    """

    def __init__(self, transport, interval=.1, workers=4, progress=None):
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval < 0:
            raise TraceError("Miro request interval must be a finite nonnegative number")
        if type(workers) is not int or not 1 <= workers <= 4:
            raise TraceError("Miro request workers must be an integer between 1 and 4")
        self.transport = transport
        self.interval = interval
        self.workers = workers
        self.progress = progress
        self._coordinator = threading.get_ident()
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._notifications = queue.SimpleQueue()
        self._next_start = 0.
        self._cooldown_until = 0.
        self._remaining = None
        self._quota_reset = 0.
        self._inflight_credits = 0
        self._mapping = False
        self._executor = None
        self._blocked_error = None
        self._quota_announced = 0.
        self._waiting_until = 0.

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None
        self._flush_progress()

    def _notify(self, delay, message, reason="rate_limit"):
        self._notifications.put((time.monotonic() + delay, {
            "phase": "waiting", "message": message,
            "retry_after": min(30., max(0., delay)), "reason": reason}))

    def _flush_progress(self):
        if threading.get_ident() != self._coordinator:
            return
        while True:
            try:
                deadline, event = self._notifications.get_nowait()
            except queue.Empty:
                break
            if self.progress is not None and deadline > time.monotonic():
                self._waiting_until = max(self._waiting_until, deadline)
                event["retry_after"] = min(30., max(0., deadline - time.monotonic()))
                self.progress._send({**self.progress.current, **event})
        if self.progress is not None and self._waiting_until and time.monotonic() >= self._waiting_until:
            self._waiting_until = 0.
            self.progress._send(self.progress.current)

    def _wait(self, delay):
        deadline = time.monotonic() + delay
        while True:
            if self._cancel.is_set():
                raise _Cancelled()
            self._flush_progress()
            left = deadline - time.monotonic()
            if left <= 0:
                return
            self._cancel.wait(min(left, .05))

    def _start(self, method):
        cost = 50 if method == "GET" else 100
        spacing = min(self.interval, .05) if method == "GET" else self.interval
        while True:
            with self._lock:
                if self._cancel.is_set():
                    raise _Cancelled()
                if self._blocked_error is not None:
                    raise self._blocked_error
                current = time.monotonic()
                if self._remaining is not None and current >= self._quota_reset:
                    self._remaining = None
                deadline = max(self._next_start, self._cooldown_until)
                if self._remaining is not None and self._remaining < cost:
                    if self._quota_reset - current > 30:
                        raise TraceError("Miro rate limited for more than 30 seconds; wait for the limit to reset and rerun sync")
                    deadline = max(deadline, self._quota_reset)
                    if self._quota_announced < self._quota_reset:
                        self._quota_announced = self._quota_reset
                        self._notify(self._quota_reset - current, "Waiting for the Miro rate limit to reset")
                delay = deadline - current
                if delay <= 0:
                    self._next_start = current + spacing
                    if self._remaining is not None:
                        self._remaining -= cost
                    self._inflight_credits += cost
                    return cost
            self._wait(delay)

    def _observe(self, headers, cost, status):
        values = {str(key).lower(): value for key, value in headers.items()}
        with self._lock:
            self._inflight_credits -= cost
            try:
                remaining = float(values["x-ratelimit-remaining"])
                reset = float(values["x-ratelimit-reset"]) - time.time()
            except (KeyError, TypeError, ValueError):
                remaining, reset = None, None
            if remaining is not None and math.isfinite(remaining) and math.isfinite(reset) and remaining >= 0 and reset > 0:
                current = time.monotonic()
                # Responses may arrive out of order. Never increase the known
                # budget within a live window, and reserve for other in-flight
                # requests the response may not yet account for.
                budget = max(0., remaining - self._inflight_credits)
                if self._remaining is None or current >= self._quota_reset:
                    self._remaining = budget
                else:
                    self._remaining = min(self._remaining, budget)
                self._quota_reset = max(self._quota_reset, current + reset)
            if status == 429:
                try:
                    delay = _retry_delay(headers)
                except TraceError as error:
                    # A caller journaling POST must receive its known rejection
                    # before deciding whether a retry can proceed.
                    self._blocked_error = error
                else:
                    self._cooldown_until = max(self._cooldown_until, time.monotonic() + delay)

    def send(self, method, url, headers=None, body=None, timeout=30):
        """One paced transport attempt; ``body`` is already encoded bytes."""
        try:
            cost = self._start(method)
        except TraceError as error:
            raise MiroRequestNotSent(str(error)) from None
        try:
            result = self.transport(method, url, headers, body, timeout)
        except BaseException:
            with self._lock:
                self._inflight_credits -= cost
            raise
        status, response_headers, _ = result
        self._observe(response_headers, cost, status)
        self._flush_progress()
        return result

    def request(self, method, url, headers, body=None):
        """Encode a request and apply bounded, method-aware safe retries."""
        encoded = canonical(body) if body is not None else None
        for attempt in range(4):
            result = self.send(method, url, headers, encoded, 30)
            status = result[0]
            if status == 429 and attempt < 3:
                # send() installed a cooldown shared by every worker. Keep
                # notifications here so callers using send() can own retries.
                self._notify(_retry_delay(result[1]), "Waiting for the Miro rate limit to reset")
                continue
            if method == "GET" and status >= 500 and attempt < 3:
                delay = min(2 ** attempt, 4)
                self._notify(delay, "Waiting before retrying a Miro read", "server_retry")
                self._wait(delay)
                continue
            return result

    def map(self, jobs, worker, accept):
        """Run at most ``workers`` jobs; checkpoint all completed successes.

        ``worker(job)`` may call request(). ``accept(job, result)`` runs in the
        caller, never a worker. A failed acceptance also stops new work and
        drains the rest. Interrupts receive the same acknowledgment handling.
        """
        if threading.get_ident() != self._coordinator or self._mapping:
            raise TraceError("Miro request batches must run on their coordinator thread")
        iterator = iter(jobs)
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="miro")
        self._mapping = True
        self._cancel.clear()
        pending = {}
        first_error = None
        exhausted = False

        def fail(error):
            nonlocal first_error
            if first_error is None:
                first_error = error
            self._cancel.set()
            for future in pending:
                future.cancel()

        def accept_done(future):
            job = pending.pop(future)
            if future.cancelled():
                return
            try:
                result = future.result()
            except BaseException as error:
                if not isinstance(error, _Cancelled) or first_error is None:
                    fail(error)
            else:
                try:
                    accept(job, result)
                except BaseException as error:
                    fail(error)

        try:
            while pending or (not exhausted and first_error is None):
                try:
                    while len(pending) < self.workers and not exhausted and first_error is None:
                        # An error can finish after wait() took its snapshot.
                        # Drain every now-completed future before replacement.
                        for future in list(pending):
                            if future.done():
                                accept_done(future)
                        if first_error is not None:
                            break
                        try:
                            job = next(iterator)
                        except StopIteration:
                            exhausted = True
                            break
                        pending[self._executor.submit(worker, job)] = job
                    if not pending:
                        break
                    done, _ = wait(pending, timeout=.05, return_when=FIRST_COMPLETED)
                    self._flush_progress()
                    # Observe failures before considering any replacement jobs.
                    for future in done:
                        accept_done(future)
                except BaseException as error:
                    fail(error)
        finally:
            # A second interrupt must not abandon acknowledged writes. Continue
            # draining, then propagate the original failure/interrupt to caller.
            while pending:
                try:
                    done, _ = wait(pending, timeout=.05, return_when=FIRST_COMPLETED)
                    self._flush_progress()
                    for future in done:
                        accept_done(future)
                except BaseException as error:
                    fail(error)
            # Keep threads alive for the next phase, so their HTTPS connections
            # can also be reused. __exit__ closes the executor after all phases.
            self._mapping = False
            self._cancel.clear()
            self._flush_progress()
        if first_error is not None:
            raise first_error
