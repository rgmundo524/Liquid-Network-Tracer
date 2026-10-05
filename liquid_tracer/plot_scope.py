"""Current attribution boundaries projected over immutable saved evidence."""

from copy import deepcopy

from .common import TraceError
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


def validate_max_hops(value):
    if type(value) is not int or not 0 <= value <= 2147483647:
        raise TraceError("Full trace hops must be a whole number from 0 to 2147483647")
    return value


def validate_hop_basis(value):
    if value not in ("configured", "original_seeds"):
        raise TraceError("Choose configured hop distances or original starting-output hops")
    return value


def projected_hop_basis(state, hop_basis="configured"):
    """Select a plotting distance basis without modifying source evidence."""
    validate_hop_basis(hop_basis)
    if hop_basis == "configured" or not reference_name(state):
        return state
    result = dict(state)
    result.pop("hop_reference_name", None)
    return result


def project_full_scope(state, *, max_hops=None):
    """Keep every saved path still reachable under the current trace controls.

    Reuse collection's path-local allowances, including independent seed paths
    and previously held evidence. Address equality and unrelated context inputs never
    introduce a traversal. The selected transaction's original inputs/outputs
    remain available as context; no observations or transaction payloads change.
    An explicit maximum selects a plot scope independently of the saved
    collection limit. Omitting it preserves historical collection behavior.
    Named-group distance retains its existing reset semantics. This is a
    display copy, not a replacement collection checkpoint.
    """
    if max_hops is not None:
        validate_max_hops(max_hops)
    result = deepcopy(state)
    if max_hops is not None:
        # The analysis index also recognizes exact spends proved by archived
        # transaction inputs, even when collection stopped before bookkeeping
        # the link. New scoped plots use the same evidence; legacy uncapped
        # previews retain their original link semantics.
        from .connections import _saved_connection_evidence
        from .group_hops import restore_scope_outputs
        result = _saved_connection_evidence(result, copy_state=False)
        restore_scope_outputs(result)
    saved_limits = deepcopy(result.get("limits", {}))
    if max_hops is not None:
        # HopScope uses this ceiling while discovering named-group returns.
        # Keep the source collection metadata unchanged in the projected view.
        result.setdefault("limits", {})["max_hops"] = max_hops
    scope = HopScope(result, respect_attribution_hops=True)
    unspent = _unspent_endpoints(result)
    named = bool(reference_name(result))
    from .trace import COLLECTION_POLICY
    bounded = max_hops is not None or named or result.get("collection_policy") == COLLECTION_POLICY
    maximum = max_hops if max_hops is not None else result.get("limits", {}).get("max_hops", float("inf"))
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
    if max_hops is not None:
        if "limits" in state:
            result["limits"] = saved_limits
        else:
            result.pop("limits", None)
    return result
