"""Deterministic, bounded layout of recorded UTXO relationships.

Trace hop is a request budget, not a topological level: two selected starting
transactions can spend one another. Layout therefore uses the recorded inputs.
No blockchain lookups or changes to evidence take place here.
"""

from collections import defaultdict
from datetime import datetime, timezone
from heapq import heappop, heappush
from statistics import median


COLUMN_GAP = 360
ROW_GAP = 240
NODE_SIZE = 160
COMPONENT_GAP = 120


def transaction_ranks(transactions):
    """Longest-path ranks, with iterative SCC handling for malformed cycles."""
    children = {key: set() for key in transactions}
    parents = {key: set() for key in transactions}
    for txid, record in transactions.items():
        for vin in record["data"].get("vin", []):
            parent = vin.get("txid")
            if not vin.get("is_pegin") and not vin.get("is_coinbase") and parent in children:
                children[parent].add(txid)
                parents[txid].add(parent)

    # Kosaraju, using explicit iterator stacks so deep traces do not exhaust
    # Python's recursion limit. Traversal order cannot depend on API response order.
    seen, finished = set(), []
    for root in sorted(children):
        if root in seen:
            continue
        seen.add(root)
        stack = [(root, iter(sorted(children[root])))]
        while stack:
            node, iterator = stack[-1]
            following = next(iterator, None)
            if following is None:
                finished.append(node)
                stack.pop()
            elif following not in seen:
                seen.add(following)
                stack.append((following, iter(sorted(children[following]))))
    components, membership = [], {}
    for root in reversed(finished):
        if root in membership:
            continue
        number = len(components)
        members, stack = [], [root]
        membership[root] = number
        while stack:
            node = stack.pop()
            members.append(node)
            for parent in sorted(parents[node], reverse=True):
                if parent not in membership:
                    membership[parent] = number
                    stack.append(parent)
        components.append(sorted(members))
    following = {i: set() for i in range(len(components))}
    indegree = {i: 0 for i in following}
    for parent, descendants in children.items():
        start = membership[parent]
        for child in descendants:
            end = membership[child]
            if start != end and end not in following[start]:
                following[start].add(end)
                indegree[end] += 1
    ready, starts, ranks = [], defaultdict(int), {}
    for number, count in indegree.items():
        if count == 0:
            heappush(ready, (components[number][0], number))
    while ready:
        _, number = heappop(ready)
        for offset, node in enumerate(components[number]):
            ranks[node] = starts[number] + offset
        next_start = starts[number] + len(components[number])
        for child in sorted(following[number]):
            starts[child] = max(starts[child], next_start)
            indegree[child] -= 1
            if indegree[child] == 0:
                heappush(ready, (components[child][0], child))
    cycles = [members for members in components
              if len(members) > 1 or members[0] in children[members[0]]]
    return ranks, sorted(cycles)


def fee_order(txid, vout, transactions, ranks=None):
    """Confirmed block order, time fallback, then deterministic unknown order."""
    status = transactions[txid]["data"].get("status", {})
    height, timestamp = status.get("block_height"), status.get("block_time")
    rank = (ranks or {}).get(txid, 0)
    # Heights provide canonical ordering even when block timestamps are equal or
    # drift. No time or block height is fabricated for unconfirmed observations.
    if isinstance(height, int) and not isinstance(height, bool):
        return (0, height, timestamp if isinstance(timestamp, int) else 0, rank, txid, vout)
    if isinstance(timestamp, int) and not isinstance(timestamp, bool):
        return (1, timestamp, 0, rank, txid, vout)
    return (2, 0, 0, rank, txid, vout)


def fee_date(transaction):
    status = transaction.get("status", {})
    timestamp = status.get("block_time")
    if isinstance(timestamp, int) and not isinstance(timestamp, bool):
        try:
            return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d UTC")
        except (ValueError, OverflowError, OSError):
            pass
    height = status.get("block_height")
    return "Block " + str(height) if isinstance(height, int) and not isinstance(height, bool) else "Date ??"


def _pack(group, desired, positions, owners):
    """Place a column in neighbor order, with fixed clearance and no overlap."""
    ordered = sorted(group, key=lambda key: (desired[key], owners[key], positions[key], key))
    packed = []
    for key in ordered:
        value = desired[key]
        if packed:
            value = max(value, packed[-1] + ROW_GAP)
        packed.append(value)
    shift = median(desired[key] - value for key, value in zip(ordered, packed))
    for key, value in zip(ordered, packed):
        positions[key] = value + shift


def arrange(nodes, edges, transactions, fee_items):
    """Mutate display positions only; fees do not influence main-flow geometry."""
    fee_keys = {key for key, item in fee_items.items() if item["endpoint"] == "shapes"}
    main = {key: node for key, node in nodes.items() if key not in fee_keys}
    neighbors = {key: set() for key in main}
    incoming, outgoing = defaultdict(set), defaultdict(set)
    for edge in edges:
        source, target = edge["source"], edge["target"]
        if source in main and target in main:
            neighbors[source].add(target)
            neighbors[target].add(source)
            incoming[target].add(source)
            outgoing[source].add(target)

    # An outpoint belongs between its creating transaction and first displayed
    # spender. Reused addresses stay separate in the normal outpoint mode. In
    # merged mode one circle can legitimately have return/backward connectors.
    owners = {}
    for key, node in main.items():
        if node["kind"] == "transaction":
            owners[key] = (key,)
            continue
        producers = sorted(parent for parent in incoming[key] if main[parent]["kind"] == "transaction")
        consumers = sorted(child for child in outgoing[key] if main[child]["kind"] == "transaction")
        before = [main[parent]["column"] for parent in producers]
        after = [main[child]["column"] for child in consumers]
        owners[key] = tuple(producers or consumers or [key])
        if before and after:
            lower, upper = max(before) + 1, min(after) - 1
            node["column"] = ((lower + upper) // 2 if lower <= upper
                              else round(median(before + after)))
        elif before:
            node["column"] = max(before) + 1
        elif after:
            node["column"] = min(after) - 1

    # Lay out each disconnected investigation branch in its own compact lane.
    # Hash ordering is only a tie-breaker; connected neighbors determine rows.
    components, unseen = [], set(main)
    for root in sorted(main):
        if root not in unseen:
            continue
        members, stack = [], [root]
        unseen.remove(root)
        while stack:
            node = stack.pop()
            members.append(node)
            for child in sorted(neighbors[node], reverse=True):
                if child in unseen:
                    unseen.remove(child)
                    stack.append(child)
        components.append(sorted(members))
    top = 240.0
    for members in components:
        columns = defaultdict(list)
        positions = {}
        for key in members:
            columns[main[key]["column"]].append(key)
        for group in columns.values():
            for index, key in enumerate(group):
                positions[key] = (index - (len(group) - 1) / 2) * ROW_GAP
        levels = sorted(columns)
        # A fixed number of two-way barycentric sweeps makes runtime finite,
        # keeps branch/join neighbors together, and reduces crossing connectors.
        for _ in range(6):
            for forward in (True, False):
                for column in (levels if forward else reversed(levels)):
                    desired = {}
                    for key in columns[column]:
                        if main[key]["kind"] != "transaction":
                            # Keep all of a transaction's output circles beside
                            # one another. Otherwise a distant shared join can
                            # pull one sibling several screens away from its
                            # creating transaction. External co-inputs belong
                            # beside their consuming transaction instead.
                            desired[key] = median(positions[owner] for owner in owners[key])
                            continue
                        adjacent = [other for other in neighbors[key]
                                    if ((main[other]["column"] < column) if forward
                                        else (main[other]["column"] > column))]
                        desired[key] = median(positions[other] for other in adjacent) if adjacent else positions[key]
                    _pack(columns[column], desired, positions, owners)
        offset = top - min(positions.values())
        for key in members:
            node = main[key]
            node.update(x=node["column"] * COLUMN_GAP + 130,
                        y=round(positions[key] + offset, 3), width=NODE_SIZE, height=NODE_SIZE)
        top = max(main[key]["y"] for key in members) + NODE_SIZE + COMPONENT_GAP

    ranks = {key[3:]: node["column"] for key, node in main.items() if node["kind"] == "transaction"}
    shown_fees = sorted(fee_keys & nodes.keys(), key=lambda key:
                        fee_order(fee_items[key]["txid"], fee_items[key]["vout"], transactions, ranks))
    for index, key in enumerate(shown_fees):
        nodes[key].update(x=130 + index * 230, y=-100, width=NODE_SIZE, height=NODE_SIZE)
    main_top = min((node["y"] - NODE_SIZE / 2 for node in main.values()), default=160)
    main_bottom = max((node["y"] + NODE_SIZE / 2 for node in main.values()), default=320)
    annotation_shift = -260 if shown_fees else 0
    return {"algorithm": "dependency_layers_v1", "direction": "left_to_right",
            "main_top": main_top, "main_bottom": main_bottom,
            "fee_row_y": -100 if shown_fees else None,
            "annotations": {"legend": {"x": 700, "y": -160 + annotation_shift},
                            "run": {"x": 700, "y": -480 + annotation_shift}}}
