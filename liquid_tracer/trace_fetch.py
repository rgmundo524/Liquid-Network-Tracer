"""Bounded, dependency-aware prefetch for an already selected trace frontier."""

from collections import deque
from concurrent.futures import FIRST_COMPLETED, wait

from .common import StopRun, TraceError
from .count_concurrency import CountConcurrency


class TraceConcurrency(CountConcurrency):
    """Use measured response latency without increasing the explorer's quota."""

    def __init__(self, initial_workers, total, rate):
        super().__init__(initial_workers, total, rate, setting="LIQUID_TRACE_WORKERS",
                         label="Transaction fetch")


class FrontierFetcher:
    """Run ready endpoint requests while their completed dependencies add work.

    Only the calling thread invokes consumers. Consumers may enqueue an
    outspend or child lookup after validating its funding response. Endpoint
    deduplication counts requests rather than outputs against concurrency.
    Traversal state remains unchanged until this bounded window has drained.
    """

    def __init__(self, api, target, on_idle=None):
        self.api, self.target, self.on_idle = api, target, on_idle
        self.ready = deque()
        self.callbacks = {}
        self.results = {}

    def add(self, endpoint, callback=None):
        if endpoint in self.results:
            if callback is not None:
                callback(self.results[endpoint])
            return
        if endpoint not in self.callbacks:
            self.callbacks[endpoint] = []
            self.ready.append(endpoint)
        if callback is not None:
            self.callbacks[endpoint].append(callback)

    def run(self):
        pending = {}
        stop = False
        try:
            while self.ready or pending:
                target = self.target()
                while not stop and self.ready and len(pending) < target:
                    endpoint = self.ready.popleft()
                    pending[self.api.submit(endpoint)] = endpoint
                if not pending:
                    break
                completed, _ = wait(pending, timeout=.25, return_when=FIRST_COMPLETED)
                if self.on_idle is not None:
                    self.on_idle()
                # A budget failure anywhere in a completed group prevents new
                # submissions. Ordered traversal later surfaces cached errors.
                for future in completed:
                    endpoint = pending[future]
                    try:
                        self.results[endpoint] = future.result()
                    except TraceError as error:
                        self.results[endpoint] = error
                        stop = stop or isinstance(error, StopRun)
                for future in completed:
                    endpoint = pending.pop(future)
                    for callback in self.callbacks.pop(endpoint):
                        callback(self.results[endpoint])
        except BaseException:
            self.api.drain_pending(cancel=True)
            raise
