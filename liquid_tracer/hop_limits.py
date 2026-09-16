"""Path-local attribution budgets. A hop is one verified transaction spend.

A budget is applied on arrival at an annotated output, before spending it.
Repeated annotations can shorten, but never replenish, an existing budget.
Independent seed paths retain independent allowances, including after restart.
"""
from collections import defaultdict
import heapq
import math

from .common import TraceError, match_labels, output_kind

UNSET = object()


def hop_limit_value(value):
    """CSV/UI blanks clear a limit; reject booleans, floats and negative values."""
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        value = int(value.strip())
    if type(value) is not int or not 0 <= value <= 2147483647:
        raise TraceError("hop_limit must be blank or a whole number from 0 to 2147483647")
    return value


def has_hop_limits(labels):
    return any(label.get("hop_limit") is not None for label in labels)


def output_budget(labels, key, output):
    matches = match_labels(labels, key, output)
    if any(label.get("stop") is True for label in matches):
        return 0
    limits = [hop_limit_value(label["hop_limit"]) for label in matches
              if label.get("hop_limit") is not None]
    return min((n for n in limits if n is not None), default=math.inf)


class HopScope:
    """Incremental Pareto reachability over (seed depth, remaining allowance).

    A short exhausted route must not lend its depth to a longer open route.
    State is reconstructed from seeds and saved spends, not checkpoint counters.
    No address equality or context input creates a traversal link.
    """
    active = True

    def __init__(self, state):
        self.state = state
        self.by_tx = defaultdict(list)
        self.paths = defaultdict(list)
        self.pending = defaultdict(list)
        self.reachable, self.depths = set(), {}
        self._labels = defaultdict(list)
        for label in state["labels"]:
            if label.get("hop_limit") is not None:
                hop_limit_value(label["hop_limit"])
            self._labels[label["kind"], label["value"]].append(label)
        for key, item in state["outputs"].items():
            item.pop("trace_scope_depth", None)
            self._restore(item)
            self.by_tx[item["txid"]].append(key)
        self._walk([(0, key, math.inf) for key in state["seeds"]])
        for key, item in state["outputs"].items():
            if key not in self.reachable:
                self._hold(item, "held_behind_service")

    @staticmethod
    def _restore(item):
        control = item.pop("trace_control", None)
        if control and item["status"] in {"suspected_service_stop", "held_behind_service", "attribution_hop_limit"}:
            item["status"] = control["previous_status"]

    @staticmethod
    def _hold(item, reason, address=None):
        control = {"reason": reason, "previous_status": item["status"]}
        if address:
            control["address"] = address
        item["trace_control"] = control
        if item["status"] not in {"spent", "fee", "pegout", "provably_unspendable"}:
            item["status"] = reason

    def _output(self, key):
        item = self.state["outputs"][key]
        rows = self.state["transactions"].get(item["txid"], {}).get("data", {}).get("vout", [])
        return rows[item["vout"]] if 0 <= item["vout"] < len(rows) else None

    def _matches(self, key, output):
        return [label for target in (("outpoint", key), ("script", output.get("scriptpubkey")),
                                      ("address", output.get("scriptpubkey_address")))
                if target[1] is not None for label in self._labels[target]]

    def _cap(self, key, output):
        matches = self._matches(key, output)
        if any(label.get("stop") is True for label in matches):
            return 0
        limits = [hop_limit_value(m.get("hop_limit")) for m in matches]
        return min((n for n in limits if n is not None), default=math.inf)

    def register(self, key):
        self.by_tx[self.state["outputs"][key]["txid"]].append(key)

    def depth(self, item):
        return self.depths.get(item["outpoint"], item["depth"])

    def blocked(self, key):
        return bool(self.state["outputs"][key].get("trace_control"))

    def permits(self, item, output):
        key = item["outpoint"]
        cap = self._cap(key, output)
        return cap > 0 and any(remaining > 0 for _, remaining in self.paths[key] + self.pending[key])

    def refresh(self, key):
        """Resolve an initially unfetched seed before any outspend request."""
        deferred = self.pending.pop(key, [])
        if deferred:
            self._walk([(depth, key, remaining) for depth, remaining in deferred])

    def admit(self, key, depth, parent=None):
        starts = []
        if key in self.state["seeds"]:
            starts.append((0, key, math.inf))
        if parent is not None:
            starts.extend((d + 1, key, remaining - 1) for d, remaining in self.paths[parent]
                          if remaining > 0)
        return self._walk(starts)

    def mark_stop(self, item, address):
        self._restore(item)
        self._hold(item, "suspected_service_stop", address)

    def _walk(self, starts):
        queue, admitted = list(starts), set()
        heapq.heapify(queue)
        while queue:
            depth, key, remaining = heapq.heappop(queue)
            if key not in self.state["outputs"]:
                continue
            item = self.state["outputs"][key]
            output = self._output(key)
            if output is None:
                pair = (depth, remaining)
                if pair not in self.pending[key]:
                    self.pending[key].append(pair)
                self.reachable.add(key)
                self.depths[key] = min(depth, self.depths.get(key, depth))
                continue
            remaining = min(remaining, self._cap(key, output))
            old = self.paths[key]
            if any(d <= depth and r >= remaining for d, r in old):
                continue
            self.paths[key] = [(d, r) for d, r in old if not (depth <= d and remaining >= r)] + [(depth, remaining)]
            self.reachable.add(key)
            self._restore(item)
            item["labels"] = self._matches(key, output)
            viable = [d for d, r in self.paths[key] if r > 0]
            self.depths[key] = min(viable or [d for d, _ in self.paths[key]])
            item["trace_scope_depth"] = self.depths[key]
            if not viable and output_kind(output) == "spendable":
                stopped = any(m.get("stop") is True for m in item["labels"])
                self._hold(item, "suspected_service_stop" if stopped else "attribution_hop_limit",
                           output.get("scriptpubkey_address") if stopped else None)
            elif remaining > 0:
                admitted.add(key)
            link = self.state["links"].get(key)
            if remaining > 0 and link and output_kind(output) == "spendable":
                for child in self.by_tx[link["spending_txid"]]:
                    heapq.heappush(queue, (depth + 1, child, remaining - 1))
        return sorted(admitted)


def bounded_connections(state, roots, seeds, eligible, limit):
    """Reachability in the product of transaction, used hops, and allowance.

    Reverse-mark successful states to retain every qualifying route without
    enumerating complete paths. Path budgets cannot leak between source roots.
    The no-attribution-limit path retains the existing linear BFS algorithm.
    """
    forward = defaultdict(list)
    for parent, child, key, _ in eligible:
        output = state["transactions"][parent]["data"]["vout"][int(key.rpartition(":")[2])]
        cap = output_budget(state["labels"], key, output)
        forward[parent].append((child, key, cap))
    limit = min(limit, max(0, len(state["transactions"]) - 1))  # Verified DAG.
    kept, pairs = set(), []
    from collections import deque
    for root in roots:
        start = (root, 0, limit)
        queue = deque([start])
        seen = {start}
        predecessors = defaultdict(list)
        success, targets = [], {}
        while queue:
            point = queue.popleft()
            parent, depth, remaining = point
            if depth >= limit or remaining <= 0:
                continue
            for child, key, cap in forward[parent]:
                if parent == root and key not in seeds:
                    continue
                budget = min(remaining, cap)
                if budget <= 0:
                    continue
                next_point = (child, depth + 1, budget - 1)
                predecessors[next_point].append((point, key))
                if next_point not in seen:
                    seen.add(next_point)
                    queue.append(next_point)
                    if child in roots and child != root:
                        success.append(next_point)
                        targets[child] = min(depth + 1, targets.get(child, depth + 1))
        marked = set(success)
        while success:
            point = success.pop()
            for previous, key in predecessors[point]:
                kept.add(key)
                if previous not in marked:
                    marked.add(previous)
                    success.append(previous)
        pairs.extend({"source": root, "target": target, "shortest_hops": distance}
                     for target, distance in sorted(targets.items()))
    return kept, pairs
