"""Convergence is a border on its transaction, never an extra graph object."""

DEFAULT_BORDER_COLOR = "#334155"
DEFAULT_BORDER_WIDTH = 2
CONVERGENCE_BORDER_COLOR = "#ff0000"
CONVERGENCE_BORDER_WIDTH = 12


def node_border(node):
    """Use only the detector's transaction metadata, not address connectivity.

    Address fill colors (especially selected seed red), transaction fill colors,
    labels, evidence, and the underlying convergence criteria are unaffected.
    """
    if node.get("kind") == "transaction" and node.get("convergence"):
        return CONVERGENCE_BORDER_COLOR, CONVERGENCE_BORDER_WIDTH
    return DEFAULT_BORDER_COLOR, DEFAULT_BORDER_WIDTH
