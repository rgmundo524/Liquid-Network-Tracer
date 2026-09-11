"""Bounded Miro I/O with one pacing gate and coordinator-owned checkpoints."""

import math
import json
import queue
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import urlsplit

from .common import TraceError, canonical
from .miro_quota import TARGET_CREDITS_PER_MINUTE
from .processes import defer_cancellation_during_spawn


class MiroRequestNotSent(TraceError):
    """The pacing gate refused a request before the transport was called."""


class _Cancelled(MiroRequestNotSent):
    """A request that never reached the transport after its batch failed."""


def _retry_delay(headers):
    values = {str(key).lower(): value for key, value in headers.items()}
    try:
        delay = (float(values["retry-after"]) if "retry-after" in values
                 else float(values["x-ratelimit-reset"]) - time.time())
    except (KeyError, TypeError, ValueError):
        delay = 2.
    if not math.isfinite(delay):
        raise TraceError("Miro returned an invalid rate-limit retry delay")
    return max(1., delay)


def request_credits(method, url, body=None):
    """Endpoint costs, including Level 2 for each bulk-created item."""
    path = urlsplit(url).path.rstrip("/")
    if method == "POST" and path.endswith("/items/bulk"):
        try:
            items = json.loads(body) if isinstance(body, (bytes, str)) else body
        except (ValueError, TypeError, UnicodeError):
            raise MiroRequestNotSent("Cannot determine the Miro bulk request credit cost") from None
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise MiroRequestNotSent("Miro bulk creation requires between 1 and 20 items")
        return 100 * len(items)
    if method == "GET":
        return 100 if path.endswith(("/items", "/connectors")) else 50
    return 100


class MiroRequests:
    """Overlap latency, while pacing request starts across all worker threads.

    Only the calling thread invokes ``accept`` and progress callbacks. On a
    failed batch, requests already sent may still succeed: those acknowledgments
    are drained before propagating the first error, so callers can save them.
    New POSTs require callers to journal intentions before dispatch and accept
    acknowledgments on the coordinator, just as existing-item edits do.
    """

    def __init__(self, transport, interval=.02, workers=4, progress=None, quota=None):
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval < 0:
            raise TraceError("Miro request interval must be a finite nonnegative number")
        if type(workers) is not int or not 1 <= workers <= 4:
            raise TraceError("Miro request workers must be an integer between 1 and 4")
        self.transport = transport
        self.interval = interval
        self.workers = workers
        self.progress = progress
        self.quota = quota
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
            "retry_after": max(0., delay), "reason": reason}))

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
                event["retry_after"] = max(0., deadline - time.monotonic())
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

    def _start(self, cost):
        spacing = self.interval
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
                    deadline = max(deadline, self._quota_reset)
                    if self._quota_announced < self._quota_reset:
                        self._quota_announced = self._quota_reset
                        self._notify(self._quota_reset - current, "Waiting for the Miro rate limit to reset")
                delay = deadline - current
                if delay <= 0:
                    reservation = None
                    if self.quota is not None:
                        reservation, delay = self.quota.reserve(cost, spacing)
                        if delay > 0:
                            if delay >= .25 and self._quota_announced < current + delay - .01:
                                self._quota_announced = current + delay
                                self._notify(delay, "Waiting for the shared Miro API credit allowance")
                            # No reservation was debited. Release the thread
                            # lock before waiting or trying another process.
                    if delay <= 0:
                        self._next_start = current + spacing
                        if self._remaining is not None:
                            self._remaining -= cost
                        self._inflight_credits += cost
                        return reservation
            self._wait(delay)

    def _finish_quota(self, reservation, headers=None, status=None):
        if self.quota is None or reservation is None:
            return
        try:
            self.quota.finish(reservation, headers, status)
        except TraceError:
            # An acknowledged response must reach the checkpoint coordinator
            # even if its local pacing cache cannot be updated afterwards.
            with self._lock:
                self._blocked_error = TraceError("Cannot update the private Miro quota cache; check local cache permissions before syncing again")

    def _observe(self, headers, cost, status, reservation=None):
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
        self._finish_quota(reservation, headers, status)

    def send(self, method, url, headers=None, body=None, timeout=30, *, credits=None):
        """One paced transport attempt; ``body`` is already encoded bytes."""
        try:
            expected = request_credits(method, url, body)
            cost = expected if credits is None else credits
            if type(cost) is not int or not expected <= cost <= TARGET_CREDITS_PER_MINUTE:
                raise MiroRequestNotSent("Miro request credits must be a positive integer within the minute allowance")
            reservation = self._start(cost)
        except TraceError as error:
            raise MiroRequestNotSent(str(error)) from None
        try:
            if self._cancel.is_set():
                raise MiroRequestNotSent("Miro request canceled before transport")
            result = self.transport(method, url, headers, body, timeout)
        except BaseException:
            with self._lock:
                self._inflight_credits -= cost
            self._finish_quota(reservation)
            raise
        status, response_headers, _ = result
        self._observe(response_headers, cost, status, reservation)
        self._flush_progress()
        return result

    def pause_for_rate_limit(self, headers):
        """Report a known 429; the next send observes its shared cooldown."""
        delay = _retry_delay(headers)
        self._notify(delay, "Waiting for the Miro rate limit to reset")
        return delay

    def request(self, method, url, headers, body=None, *, credits=None):
        """Encode a request and apply bounded, method-aware safe retries."""
        encoded = canonical(body) if body is not None else None
        for attempt in range(4):
            result = self.send(method, url, headers, encoded, 30, credits=credits)
            status = result[0]
            if status == 429 and attempt < 3:
                # send() installed a cooldown shared by every worker. Keep
                # notifications here so callers using send() can own retries.
                self.pause_for_rate_limit(result[1])
                continue
            if method == "GET" and status >= 500 and attempt < 3:
                delay = min(2 ** attempt, 4)
                self._notify(delay, "Waiting before retrying a Miro read", "server_retry")
                self._wait(delay)
                continue
            return result

    def map(self, jobs, worker, accept, reject=None):
        """Run at most ``workers`` jobs; checkpoint all completed successes.

        ``worker(job)`` may call request(). ``accept(job, result)`` runs in the
        caller, never a worker. A failed acceptance also stops new work and
        drains the rest. Interrupts receive the same acknowledgment handling.
        Optional ``reject(job, error)`` also runs on the coordinator and receives
        every failed or canceled dispatched job. Only MiroRequestNotSent proves
        that a journaled POST never reached the transport.
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
                if reject is not None:
                    try:
                        reject(job, MiroRequestNotSent("Miro request canceled before dispatch"))
                    except BaseException as error:
                        fail(error)
                return
            try:
                result = future.result()
            except BaseException as error:
                # Stop waiting POSTs before a durable rejection checkpoint can
                # spend time on disk. Already-sent acknowledgments still drain.
                if not isinstance(error, _Cancelled) or first_error is None:
                    fail(error)
                if reject is not None:
                    try:
                        reject(job, error)
                    except BaseException as rejection_error:
                        fail(rejection_error)
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
                        future = None
                        try:
                            # Do not lose a queued POST between executor.submit
                            # and registering the future for acknowledgment.
                            with defer_cancellation_during_spawn():
                                future = self._executor.submit(worker, job)
                                pending[future] = job
                        except BaseException as submission_error:
                            if future is None and reject is not None:
                                # Unexpected executor failures are ambiguous;
                                # only its shutdown rejection proves no enqueue.
                                error = (MiroRequestNotSent("Miro request canceled before dispatch")
                                         if isinstance(submission_error, RuntimeError)
                                         and "cannot schedule new futures" in str(submission_error)
                                         else submission_error)
                                reject(job, error)
                            raise
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
