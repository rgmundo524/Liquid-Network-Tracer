"""Typed worker-process failures that can be isolated to one ELK seed."""

from .common import TraceError
from .render_runtime import RENDERER_FAILURE_CODES


ELK_FATAL_FAILURE_CODES = frozenset({
    "elk_worker_setup", "elk_invalid_request", "elk_invalid_output", "elk_input_order",
    "elk_unsupported_graph", "elk_unsupported_configuration", "graph_parse", "browser_launch",
})


class ElkWorkerFailure(TraceError):
    """A running worker exited unsuccessfully, without a valid candidate.

    Setup errors, malformed responses and invalid geometry use plain TraceError
    instead. Only fixed failure categories are suitable for search metadata.
    """

    def __init__(self, message, *, failure_code="unknown_exit", returncode=None):
        super().__init__(message)
        self.failure_code = (failure_code if isinstance(failure_code, str)
                             and failure_code in RENDERER_FAILURE_CODES else "unknown_exit")
        self.returncode = returncode if type(returncode) is int else None
