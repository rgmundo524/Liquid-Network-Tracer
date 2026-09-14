"""Native borders for independent input-merge and shared-address signals."""

DEFAULT_BORDER_COLOR = "#334155"
DEFAULT_BORDER_WIDTH = 2
CONVERGENCE_BORDER_COLOR = "#ff0000"
CONVERGENCE_BORDER_WIDTH = 12


def node_border(node):
    """Read typed detector results, never infer an interaction from a node label.

    Address and transaction fills are unchanged, especially selected seed red.
    Sender address interactions are not represented as transaction input merges.
    """
    kind = node.get("kind")
    if ((kind == "transaction" and (node.get("convergence") or node.get("address_interactions")))
            or (kind == "address" and node.get("address_convergence"))):
        return CONVERGENCE_BORDER_COLOR, CONVERGENCE_BORDER_WIDTH
    return DEFAULT_BORDER_COLOR, DEFAULT_BORDER_WIDTH
