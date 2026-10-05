"""Small operation-local timers for graph preparation and publication."""
from contextlib import contextmanager
import time

from .progress import report_progress


class PlotTimings:
    """Measure sequential stages without retaining evidence or credentials.

    ELK stage time includes scheduling/resource waits, not just worker CPU.
    Timings are returned with the operation result and emitted to its existing
    progress reporter. Reporting failures cannot alter an operation's outcome.
    """
    def __init__(self, progress=None):
        self.progress = progress
        self.seconds = {}

    @contextmanager
    def measure(self, stage):
        report_progress(self.progress, "plot_" + stage, 0, 1)
        started = time.monotonic()
        try:
            yield
        except BaseException:
            # Keep the measurement for callers but do not report failed work
            # as completed or interfere with cancellation/error propagation.
            self.seconds[stage] = self.seconds.get(stage, 0) + max(0, time.monotonic() - started)
            raise
        else:
            elapsed = max(0, time.monotonic() - started)
            self.seconds[stage] = self.seconds.get(stage, 0) + elapsed
            report_progress(self.progress, "plot_" + stage, 1, 1, elapsed_seconds=round(elapsed, 3))

    def snapshot(self):
        return {key: round(value, 3) for key, value in self.seconds.items()}
