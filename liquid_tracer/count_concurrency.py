"""Choose count-lookup concurrency without changing the explorer request rate."""

import math
import os
import re
import time

from .common import TraceError
from .render_runtime import _available_bytes, _available_cpu_count


_SETTING = "LIQUID_COUNT_WORKERS"
_MIB = 1024 * 1024


class CountConcurrency:
    """Bound network workers by resources, measured latency, and retry pressure.

    The caller supplies cumulative metrics from a fresh count-only API client.
    Completed requests include retries, so they advance measurement windows but
    do not reduce the address backlog. The API's shared limiter remains solely
    responsible for pacing requests.
    """

    def __init__(self, initial_workers, total, rate):
        if initial_workers is None:
            initial_workers = 8
        if type(initial_workers) is not int or not 1 <= initial_workers <= 8:
            raise TraceError("Saved address count workers must be an integer from 1 to 8")
        if type(total) is not int or total < 1:
            raise TraceError("Address count backlog must be a positive integer")
        if type(rate) not in (int, float) or not math.isfinite(rate) or rate < 0:
            raise TraceError("Address count request rate must be a non-negative number")
        value = os.environ.get(_SETTING, "auto").strip().lower()
        if value == "auto":
            self.mode = "auto"
            # A previously chosen lower worker setting remains an explicit cap.
            self._manual_ceiling = initial_workers if initial_workers < 8 else 64
            starting_workers = initial_workers
        elif re.fullmatch(r"[0-9]{1,2}", value) and 1 <= int(value) <= 64:
            self.mode = "fixed"
            self._manual_ceiling = starting_workers = int(value)
        else:
            raise TraceError(f"{_SETTING} must be auto or an integer from 1 to 64; set it in devenv.nix")
        self._total = total
        self._rate = rate
        self._initial = starting_workers
        self._window = max(8, initial_workers)
        self._next_adjustment = self._window
        self._pressure_events = 0
        self._hold_growth = False
        self._sampled_at = time.monotonic()
        self._ceiling = self._resource_ceiling()
        self._target = min(starting_workers, self._ceiling)
        self._peak = self._target

    @property
    def ceiling(self):
        """The latest resource and user limit, independent of response latency."""
        return self._ceiling

    @property
    def peak(self):
        """Largest concurrency requested during this count phase."""
        return self._peak

    def _resource_ceiling(self):
        available = _available_bytes()
        # Reserve only a quarter of available RAM for worst-case 32 MiB bodies.
        memory_workers = 8 if available is None else max(1, available // (4 * 32 * _MIB))
        cpu_workers = max(1, _available_cpu_count() * 8)
        return min(64, self._manual_ceiling, self._total, memory_workers, cpu_workers)

    @staticmethod
    def _counter(metrics, key):
        value = metrics.get(key, 0)
        return value if type(value) is int and value >= 0 else 0

    def target(self, metrics):
        """Return a bounded in-flight target, changing at most once per window."""
        now = time.monotonic()
        if now - self._sampled_at >= 1:
            self._ceiling = self._resource_ceiling()
            self._sampled_at = now
        self._target = min(self._target, self._ceiling)
        completed = self._counter(metrics, "completed_requests")
        pressure = self._counter(metrics, "pressure_events")
        if pressure > self._pressure_events:
            self._pressure_events = pressure
            self._target = max(1, self._target // 2)
            self._next_adjustment = completed + self._window
            self._hold_growth = True
        elif completed >= self._next_adjustment:
            # Each callback can perform only one adjustment even when many
            # requests finished between callbacks. Repeated identical metrics
            # cannot trigger another increase.
            self._next_adjustment = completed + self._window
            latency = metrics.get("latency_seconds")
            if self.mode == "fixed" or self._rate == 0:
                desired = min(self._initial, self._ceiling)
            elif type(latency) in (int, float) and math.isfinite(latency) and latency > 0:
                # Little's law, with modest headroom for response variance.
                # Clamp before ceil to avoid overflow for extreme rate inputs.
                demand = self._rate * latency * 1.25
                desired = max(1, math.ceil(min(self._ceiling, demand)))
            else:
                desired = self._target
            if self._hold_growth:
                self._target = min(self._target, desired)
                self._hold_growth = False
            else:
                self._target = min(desired, self._target * 2)
        self._peak = max(self._peak, self._target)
        return self._target
