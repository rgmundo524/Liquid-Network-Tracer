"""Keep renderer ownership established before handling cancellation signals."""

import signal
import threading
from contextlib import contextmanager


@contextmanager
def defer_cancellation_during_spawn():
    """Replay callable cancellation handlers after the caller owns its child.

    Use inside the caller's process-cleanup try/finally, around both Popen and
    assignment of its result. Signals are not masked, so children do not inherit
    blocked cancellation signals. Existing default and ignored dispositions
    remain unchanged. Python only allows handler changes in the main thread.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    handlers = {}
    pending = []

    def remember(signum, frame):
        # One cancellation is sufficient. Replaying another during unwinding
        # could interrupt the cleanup this critical section is protecting.
        if not pending:
            pending.append((signum, frame))

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            handler = signal.getsignal(signum)
            if callable(handler):
                handlers[signum] = handler
                signal.signal(signum, remember)
        yield
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        if pending:
            signum, frame = pending[0]
            handlers[signum](signum, frame)
