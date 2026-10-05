"""Bounded, dependency-aware requests for an already selected trace frontier."""

import heapq
from collections import deque
from concurrent.futures import CancelledError, FIRST_COMPLETED, wait

from .common import StopRun, TraceError
from .count_concurrency import CountConcurrency


class TraceConcurrency(CountConcurrency):
    """Use measured response latency without increasing the explorer's quota."""

    def __init__(self, initial_workers, total, rate):
        if type(total) is not int or total < 0:
            raise TraceError("Transaction output limit must be a non-negative whole number")
        # Zero is an unknown/unlimited trace frontier, not an empty backlog.
        # Actual in-flight work remains bounded by resources and the 64-worker
        # ceiling already enforced by the shared concurrency policy.
        super().__init__(initial_workers, total or 64, rate, setting="LIQUID_TRACE_WORKERS",
                         label="Transaction fetch")


def heap_prefix(queue, limit):
    """Return the first ``limit`` heap entries without scanning the whole heap.

    The auxiliary heap visits the children of each selected entry. This costs
    O(limit log limit), even when the investigation frontier is much larger.
    The caller's heap and the ordering of equal entries are unchanged.
    """
    if not queue or limit <= 0:
        return []
    candidates = [(queue[0], 0)]
    result = []
    while candidates and len(result) < limit:
        value, index = heapq.heappop(candidates)
        result.append(value)
        for child in (2 * index + 1, 2 * index + 2):
            if child < len(queue):
                heapq.heappush(candidates, (queue[child], child))
    return result


class FrontierFetcher:
    """Keep requests moving while ordered traversal consumes their results.

    Only the calling thread invokes consumers, which may enqueue a validated
    outspend or child dependency. ``pump`` never waits for an entire cohort;
    ``get`` keeps pumping other ready dependencies while waiting for one
    endpoint. Endpoint deduplication counts requests against concurrency.
    The caller owns traversal state and the bounded lookahead frontier.
    """

    def __init__(self, api, target, on_idle=None):
        self.api, self.target, self.on_idle = api, target, on_idle
        self.ready = deque()
        self.callbacks = {}
        self.results = {}
        self.pending = {}
        self.stop_error = None

    def _abort(self):
        # Signal admission before draining: unlimited runs could otherwise
        # wait forever on another client's shared cooldown after cancellation.
        try:
            self.api.close()
        except BaseException:
            pass  # Preserve the original cancellation or consumer failure.

    def add(self, endpoint, callback=None):
        if endpoint in self.results:
            if callback is not None:
                try:
                    callback(self.results[endpoint])
                except BaseException:
                    self._abort()
                    raise
            return
        if endpoint not in self.callbacks:
            self.callbacks[endpoint] = []
            self.ready.append(endpoint)
        if callback is not None:
            self.callbacks[endpoint].append(callback)

    def _result(self, endpoint, value):
        self.results[endpoint] = value
        if isinstance(value, StopRun) and self.stop_error is None:
            self.stop_error = value

    def _deliver(self, endpoints):
        for endpoint in endpoints:
            # Pop before invoking callbacks so a failed consumer never runs
            # twice and callbacks can safely enqueue other endpoint work.
            callbacks = self.callbacks.pop(endpoint, ())
            for callback in callbacks:
                callback(self.results[endpoint])

    def _harvest(self, completed):
        endpoints = []
        # Inspect all errors before consumers can add work. A hard budget
        # failure anywhere in this group prevents further submission.
        for future in completed:
            endpoint = self.pending.pop(future)
            endpoints.append(endpoint)
            try:
                value = future.result()
            except CancelledError:
                value = StopRun("interrupted")
            except TraceError as error:
                value = error
            self._result(endpoint, value)
        self._deliver(endpoints)
        return len(endpoints)

    def _submit(self):
        if self.stop_error is not None or not self.ready:
            return
        target = self.target()
        if type(target) is not int or target < 1:
            raise TraceError("Transaction fetch concurrency must be a positive whole number")
        while self.stop_error is None and self.ready and len(self.pending) < target:
            endpoint = self.ready.popleft()
            try:
                future = self.api.submit(endpoint)
            except TraceError as error:
                self._result(endpoint, error)
                self._deliver([endpoint])
            else:
                self.pending[future] = endpoint

    def pump(self, block=False):
        """Deliver completed requests and refill slots, optionally waiting .25s.

        All consumers and progress callbacks run on the coordinating thread.
        A StopRun stops new submissions but leaves successful results available
        for ordered traversal, which raises the error when it needs that work.
        """
        try:
            count = self._harvest([future for future in self.pending if future.done()])
            self._submit()
            if block and not count and self.pending:
                completed, _ = wait(self.pending, timeout=.25, return_when=FIRST_COMPLETED)
                count += self._harvest(completed)
                self._submit()
            if self.on_idle is not None:
                self.on_idle()
            return count
        except BaseException:
            self._abort()
            raise

    def get(self, endpoint):
        """Wait for a demanded endpoint while replenishing unrelated work.

        A demand jumps ahead of speculative ready work, but never exceeds the
        active request target. Missing work after a hard stop fails immediately
        rather than waiting for a request that can no longer be submitted.
        """
        self.add(endpoint)
        if endpoint in self.ready:
            self.ready.remove(endpoint)
            self.ready.appendleft(endpoint)
        while endpoint not in self.results:
            self.pump(block=True)
            if endpoint not in self.results and self.stop_error is not None:
                raise self.stop_error
        result = self.results[endpoint]
        if isinstance(result, TraceError):
            raise result
        return result

    def run(self):
        """Compatibility path: drain this finite cohort and its dependencies."""
        while self.pending or (self.ready and self.stop_error is None):
            self.pump(block=True)
