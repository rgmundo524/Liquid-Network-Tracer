"""Current attribution boundaries projected over immutable saved evidence."""

from copy import deepcopy

from .export import _unspent_endpoints
from .hop_limits import HopScope


def project_full_scope(state):
    """Keep every saved path still reachable under the current trace controls.

    Reuse collection's path-local allowances, including independent seed paths
    and previously held evidence. Address equality and context inputs never
    introduce a traversal. The selected transaction's original inputs/outputs
    remain available as context; no observations or transaction payloads change.
    This is a display copy, not a replacement collection checkpoint.
    """
    result = deepcopy(state)
    scope = HopScope(result)
    unspent = _unspent_endpoints(result)
    links = {
        key: link for key, link in result["links"].items()
        if any(remaining > 0 for _, remaining in scope.paths.get(key, ()))
    }
    selected = {key.rpartition(":")[0] for key in result["seeds"]}
    selected.update(result["outputs"][key]["txid"] for key in scope.reachable)
    # A spending transaction may have been saved before all its output records
    # were checkpointed. Its verified arrival still belongs in the plot.
    selected.update(link["spending_txid"] for link in links.values())
    result["transactions"] = {
        key: value for key, value in result["transactions"].items() if key in selected
    }
    result["outputs"] = {
        key: value for key, value in result["outputs"].items()
        if key in scope.reachable and value["txid"] in result["transactions"]
    }
    result["links"] = {
        key: value for key, value in links.items()
        if key in result["outputs"] and value["spending_txid"] in result["transactions"]
    }
    for key, item in result["outputs"].items():
        if item.get("status") == "unspent_at_observation" and key not in unspent:
            # A pruned transaction can contain newer spend evidence than an old
            # unspent observation. Hiding that transaction must not resurrect
            # an unspent endpoint. Preserve the raw observation for inspection.
            item["status"] = "spent_in_saved_evidence"
    return result
