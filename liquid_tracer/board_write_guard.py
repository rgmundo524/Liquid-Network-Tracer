"""Protect case-backed Miro operations against concurrent board deletion."""

from contextlib import contextmanager
from pathlib import Path

from .board_deletion import board_write_lock, deletion_receipt
from .common import TraceError


def visible_board(case, target):
    """Suppress a confirmed deleted link even in another case's old metadata."""
    if target:
        from .cli import board_id
        try:
            receipt = deletion_receipt(case, board_id(target))
        except (TraceError, OSError, ValueError):
            # The board section exposes its unavailable-registry notice. Keep
            # the investigation itself openable without advertising this link.
            return None
        if receipt and receipt["status"] == "deleted":
            return None
    return target


@contextmanager
def state_board_lock(state_path, target):
    """Use the owning case's shared deletion lock after taking its mapping lock.

    Standalone publisher state files outside an investigation have no shared
    cases registry. Keep their existing mapping lock behavior.
    """
    for parent in Path(state_path).absolute().parents:
        if (parent / "case.json").is_file():
            with board_write_lock(parent, target):
                yield
            return
    yield
