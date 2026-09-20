"""Deterministic controls for the local ELK candidate search."""

from .common import TraceError


DEFAULT_LAYOUT_ATTEMPTS = 25
MAX_LAYOUT_ATTEMPTS = 1000
LAYOUT_SEARCH_VERSION = 3


def normalize_layout_attempts(value=None):
    """Resolve the default without accepting booleans or lossy coercions."""
    if value is None:
        return DEFAULT_LAYOUT_ATTEMPTS
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_LAYOUT_ATTEMPTS:
        raise TraceError(f"Layout attempts must be a whole number from 1 to {MAX_LAYOUT_ATTEMPTS}")
    return value


def layout_seeds(attempts=None):
    """Return a stable prefix so a larger search retains earlier candidates.

    Preserve the original three seeds, then extend with a deterministic
    multiplicative generator in ELK's positive signed 32-bit integer range.
    """
    count = normalize_layout_attempts(attempts)
    seeds = [1, 7, 19][:count]
    seen = set(seeds)
    state = 19
    while len(seeds) < count:
        state = (state * 48271) % 2147483647
        if state not in seen:
            seeds.append(state)
            seen.add(state)
    return tuple(seeds)
