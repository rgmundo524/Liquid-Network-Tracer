"""Cooperative cancellation shared by local render stages."""

from concurrent.futures import CancelledError


def check_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError()
