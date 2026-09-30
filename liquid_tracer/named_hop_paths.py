"""Named-group hop paths over already validated, immutable spend evidence."""

from collections import defaultdict, deque
import math

from .common import TraceError, output_kind, parse_outpoint
from .group_hops import reference_addresses
from .hop_limits import output_budget


def walk_outputs(state, seeds, links, limit, confirmed, *, seed_distances=None):
    """Keep path-local depth and attribution allowance for each output.

    A named output resets only its own path. A boundary output may inspect its
    direct spender, but an outside output beyond the ceiling cannot continue.
    Incoming context never supplies an independent arrival or a reset.
    Callers validate spend evidence and DAG topology before calling this helper.
    Optional seed distances count ordinary transaction edges for each exact
    path state, independently of named-group resets.
    """
    transactions = state["transactions"]
    addresses = reference_addresses(state)
    rows = {txid: record["data"]["vout"] for txid, record in transactions.items()}
    queue, seen, predecessors = deque(), set(), defaultdict(set)

    def admit(key, depth, remaining, previous=None):
        txid, index = parse_outpoint(key)
        output = rows[txid][index]
        if output_kind(output) == "fee" or depth > limit:
            return
        remaining = min(remaining, output_budget(state["labels"], key, output))
        point = (key, depth, remaining)
        if previous is not None:
            predecessors[point].add(previous)
        if point not in seen:
            seen.add(point)
            if seed_distances is not None:
                # This is a unit-edge breadth-first walk. First admission is
                # the shortest seed path to this exact depth/budget state.
                seed_distances[point] = (0 if previous is None else
                                         seed_distances[previous] + 1)
            queue.append(point)

    for key in sorted(seeds):
        txid, index = parse_outpoint(key)
        if txid in confirmed:
            if index >= len(rows[txid]):
                raise TraceError("Selected seed output does not exist in its saved transaction")
            admit(key, 0, math.inf)
    while queue:
        point = queue.popleft()
        key, depth, remaining = point
        if remaining <= 0 or key not in links:
            continue
        child = links[key]
        if child not in confirmed:
            continue
        for index, output in enumerate(rows[child]):
            following_depth = (0 if output.get("scriptpubkey_address") in addresses
                               and output_kind(output) == "spendable" else depth + 1)
            admit(f"{child}:{index}", following_depth, remaining - 1, point)
    return seen, predecessors


def retain_paths(successful, predecessors):
    """Reverse-mark exact spend keys and output depths on successful paths."""
    kept, marked, pending = set(), set(successful), list(successful)
    depths = defaultdict(set)
    while pending:
        point = pending.pop()
        depths[point[0]].add(point[1])
        for previous in predecessors[point]:
            kept.add(previous[0])
            if previous not in marked:
                marked.add(previous)
                pending.append(previous)
    return kept, depths


def transaction_depths(output_depths):
    """Summarize each output's nearest qualifying arrival without merging it."""
    values = defaultdict(set)
    for key, depths in output_depths.items():
        values[key.rpartition(":")[0]].add(min(depths))
    return values
