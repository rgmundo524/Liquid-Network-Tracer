"""Deterministic minimum-hop routes over authenticated directed spend records."""
from collections import deque

from .common import TraceError, parse_outpoint
from .progress import report_progress


def shortest_routes(seeds, incoming, *, progress=None):
    """Choose one route per reachable ordered starter pair without path enumeration.

    ``incoming(txid)`` yields ``(funding_txid, outpoint)`` pairs. The caller
    validates the DAG and exact spends before this search. Reverse breadth-first
    search visits only ancestors of each target. Equal-length choices use the
    lexicographically smallest outpoint at each step, independent of read order.
    A starter's selected output restriction applies only to its first step; the
    same transaction remains unrestricted when reached from another starter.
    """
    seeds = set(seeds)
    roots = sorted({parse_outpoint(key)[0] for key in seeds})
    if len(roots) < 2:
        raise TraceError("Choose outputs from at least two distinct starting transactions")
    routes = []
    for number, target in enumerate(roots):
        report_progress(progress, "projecting_collection", number, len(roots))
        sources = set(roots) - {target}
        distance, following, first = {target: 0}, {}, {}
        pending = deque([target])
        examined = 0
        while pending:
            # Finish the entire level before stopping so equal-length choices
            # remain deterministic even when the last source was just found.
            for _ in range(len(pending)):
                child = pending.popleft()
                depth = distance[child] + 1
                for parent, key in incoming(child):
                    examined += 1
                    if examined % 256 == 0:
                        report_progress(progress, "projecting_collection", number, len(roots))
                    if parent in sources and key in seeds:
                        candidate = (depth, key, child)
                        if parent not in first or candidate < first[parent]:
                            first[parent] = candidate
                    if parent not in distance:
                        distance[parent] = depth
                        following[parent] = (key, child)
                        pending.append(parent)
                    elif distance[parent] == depth and (key, child) < following[parent]:
                        following[parent] = (key, child)
            if sources <= first.keys():
                break
        for source in sorted(first):
            hops, key, child = first[source]
            path = [key]
            while child != target:
                key, child = following[child]
                path.append(key)
            routes.append({"source": source, "target": target,
                           "shortest_hops": hops, "outpoints": path})
        report_progress(progress, "projecting_collection", number + 1, len(roots))
    return sorted(routes, key=lambda route: (route["source"], route["target"]))
