"""Current attribution boundaries projected over immutable saved evidence."""

from copy import deepcopy

from .export import _unspent_endpoints
from .hop_limits import HopScope
from .group_hops import reference_name, refresh_reference_hops


def project_collected_full_scope(state):
    """Apply display caps to newly broadened collections, retaining old views.

    The collection policy identifies evidence gathered without attribution hop
    limits. Historical archives without it retain their original raw graph
    semantics on legacy preview and publication routes.
    """
    from .trace import COLLECTION_POLICY
    return project_full_scope(state) if state.get("collection_policy") == COLLECTION_POLICY else state


def project_full_scope(state):
    """Keep every saved path still reachable under the current trace controls.

    Reuse collection's path-local allowances, including independent seed paths
    and previously held evidence. Address equality and context inputs never
    introduce a traversal. The selected transaction's original inputs/outputs
    remain available as context; no observations or transaction payloads change.
    This is a display copy, not a replacement collection checkpoint.
    """
    result = deepcopy(state)
    scope = HopScope(result, respect_attribution_hops=True)
    unspent = _unspent_endpoints(result)
    named = bool(reference_name(result))
    from .trace import COLLECTION_POLICY
    bounded = named or result.get("collection_policy") == COLLECTION_POLICY
    maximum = result.get("limits", {}).get("max_hops", float("inf"))
    reachable = {key for key in scope.reachable
                 if not bounded or any(depth <= maximum for depth, _ in
                                       [*scope.paths.get(key, ()), *scope.pending.get(key, ())])}
    links = {
        key: link for key, link in result["links"].items()
        if key in reachable and any(remaining > 0 and (not bounded or
                                    (depth <= maximum if named else depth < maximum))
                                    for depth, remaining in scope.paths.get(key, ()))
    }
    selected = {key.rpartition(":")[0] for key in result["seeds"]}
    selected.update(result["outputs"][key]["txid"] for key in reachable)
    # A spending transaction may have been saved before all its output records
    # were checkpointed. Its verified arrival still belongs in the plot.
    if not named:
        selected.update(link["spending_txid"] for link in links.values())
    result["transactions"] = {
        key: value for key, value in result["transactions"].items() if key in selected
    }
    result["outputs"] = {
        key: value for key, value in result["outputs"].items()
        if key in reachable and value["txid"] in result["transactions"]
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
    if named:
        refresh_reference_hops(result, scope)
    return result
