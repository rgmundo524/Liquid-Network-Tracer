"""Bound expensive trace snapshots without changing response-evidence writes."""

import math
import time


class TraceCheckpoint:
    """Save after bounded work, allowing more time for costly snapshots.

    Call ``save`` after each completed output and force it at run boundaries.
    The callback owns both statistics refresh and serialization so skipped
    snapshots avoid scanning the growing graph. A full snapshot grows with the
    investigation; its measured cost sets the next interval to target 10% save
    overhead. Recovery bounds take priority over that target: save after at most
    120 seconds of work or 8,192 completed outputs. These bounds are checked
    between operations, not by interrupting a pending request or disk write.
    Response evidence is still archived immediately by the API independently.
    """

    def __init__(self, interval_seconds=2.0, max_operations=8192, *,
                 max_interval_seconds=120.0, target_overhead=0.1, clock=time.monotonic):
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("Checkpoint interval must be finite and positive")
        if type(max_operations) is not int or max_operations < 1:
            raise ValueError("Checkpoint operation limit must be a positive integer")
        if not math.isfinite(max_interval_seconds) or max_interval_seconds < interval_seconds:
            raise ValueError("Maximum checkpoint interval must be finite and at least the minimum interval")
        if not math.isfinite(target_overhead) or not 0 < target_overhead < 1:
            raise ValueError("Checkpoint overhead target must be between zero and one")
        self.interval_seconds = interval_seconds
        self.max_operations = max_operations
        self.max_interval_seconds = max_interval_seconds
        self.target_overhead = target_overhead
        self.clock = clock
        self.writes = 0
        self.write_seconds = 0.0
        self.pending_operations = 0
        self._last_saved = clock()
        self._estimated_cost = 0.0
        self._saving = False

    @property
    def next_interval_seconds(self):
        # cost / (work + cost) = target. Use the latest cost immediately when
        # writes slow down, but smooth decreases to avoid alternating frequent
        # and expensive writes when fsync latency varies.
        work_seconds = self._estimated_cost * (1 - self.target_overhead) / self.target_overhead
        return min(self.max_interval_seconds, max(self.interval_seconds, work_seconds))

    def save(self, callback, *, force=False, completed=True):
        """Invoke the callback only when due; return whether a save succeeded.

        Failed callbacks propagate and remain due for a later forced attempt.
        Successful-write count excludes failures; elapsed write time includes
        them. Start the next interval after the callback finishes so a slow
        write does not make the very next output trigger another full write.
        """
        if self._saving:
            raise RuntimeError("Trace checkpoint callback cannot save recursively")
        if completed:
            self.pending_operations += 1
        if not force and (not self.pending_operations or (
                self.pending_operations < self.max_operations
                and self.clock() - self._last_saved < self.next_interval_seconds)):
            return False
        started = self.clock()
        self._saving = True
        try:
            callback()
        finally:
            finished = self.clock()
            cost = max(0.0, finished - started)
            self.write_seconds += cost
            self._saving = False
        self._estimated_cost = max(cost, .75 * self._estimated_cost + .25 * cost)
        self.writes += 1
        self.pending_operations = 0
        self._last_saved = finished
        return True
