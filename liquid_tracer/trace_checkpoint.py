"""Bound expensive trace snapshots without changing response-evidence writes."""

import math
import time


class TraceCheckpoint:
    """Save after a bounded amount of work or elapsed processing time.

    Call ``save`` after each completed output and force it at run boundaries.
    The callback owns both statistics refresh and serialization so skipped
    snapshots avoid scanning the growing graph. The time bound is checked
    between operations, not by interrupting a pending request or disk write.
    """

    def __init__(self, interval_seconds=2.0, max_operations=256, *, clock=time.monotonic):
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("Checkpoint interval must be finite and positive")
        if type(max_operations) is not int or max_operations < 1:
            raise ValueError("Checkpoint operation limit must be a positive integer")
        self.interval_seconds = interval_seconds
        self.max_operations = max_operations
        self.clock = clock
        self.writes = 0
        self.write_seconds = 0.0
        self.pending_operations = 0
        self._last_saved = clock()
        self._saving = False

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
                and self.clock() - self._last_saved < self.interval_seconds)):
            return False
        started = self.clock()
        self._saving = True
        try:
            callback()
        finally:
            finished = self.clock()
            self.write_seconds += max(0.0, finished - started)
            self._saving = False
        self.writes += 1
        self.pending_operations = 0
        self._last_saved = finished
        return True
