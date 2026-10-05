"""Exact cumulative L-BTC limits over explicitly selected peg-out requests."""
from collections import defaultdict
import math
import re

from .common import TraceError, output_kind, parse_outpoint
from .group_hops import reference_addresses, reference_name
from .hop_limits import output_budget
from .transaction_csv import _amount, _asset

ORDERING = "ordinary_seed_hops_then_txid_vout"
COUNT_FIELDS = {"counted_pegout_count", "unknown_amount_count", "unknown_asset_count", "non_lbtc_count"}


def lbtc_units(amount):
    whole, fraction = divmod(amount, 100_000_000)
    return str(whole) + (("." + f"{fraction:08d}".rstrip("0")) if fraction else "")


def normalize_pegout_lbtc_limit(value):
    """Require positive decimal text, never floating-point or exponent notation."""
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,100}(?:\.[0-9]{1,8})?", value.strip()):
        raise TraceError("Peg-out L-BTC limit must be positive decimal text with at most eight decimal places")
    whole, _, fraction = value.strip().partition(".")
    amount = int(whole) * 100_000_000 + int(fraction.ljust(8, "0") or "0")
    if amount <= 0:
        raise TraceError("Peg-out L-BTC limit must be greater than zero")
    return lbtc_units(amount)


def _base_units(value):
    whole, _, fraction = value.partition(".")
    return int(whole) * 100_000_000 + int(fraction.ljust(8, "0") or "0")


def validate_pegout_limit_summary(value, query=None):
    """Validate exact public amounts and the saved traversal cutoff."""
    fields = COUNT_FIELDS | {"schema_version", "target_lbtc", "target_base_units", "total_lbtc",
        "total_base_units", "excess_lbtc", "excess_base_units", "limit_reached", "stop_reason",
        "ordering", "cutoff_seed_hops", "stopping_outpoint", "context_outputs_counted"}
    try:
        if (not isinstance(value, dict) or set(value) != fields
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or type(value["limit_reached"]) is not bool or value["context_outputs_counted"] is not False
                or value["ordering"] != ORDERING
                or any(type(value[key]) is not int or not 0 <= value[key] <= 2 ** 53 - 1 for key in COUNT_FIELDS)):
            raise ValueError
        for name in ("target", "total", "excess"):
            units = value[name + "_base_units"]
            if not isinstance(units, str) or not re.fullmatch(r"0|[1-9][0-9]{0,199}", units):
                raise ValueError
            if value[name + "_lbtc"] != lbtc_units(int(units)):
                raise ValueError
        target, total, excess = (int(value[name + "_base_units"]) for name in ("target", "total", "excess"))
        if (normalize_pegout_lbtc_limit(value["target_lbtc"]) != value["target_lbtc"]
                or excess != max(0, total - target) or value["limit_reached"] != (total >= target)
                or (total and not value["counted_pegout_count"])):
            raise ValueError
        if query is not None and query.get("pegout_lbtc_limit") != value["target_lbtc"]:
            raise ValueError
        if value["limit_reached"]:
            txid, index = parse_outpoint(value["stopping_outpoint"])
            if (value["stopping_outpoint"] != f"{txid}:{index}" or value["stop_reason"] != "limit_reached"
                    or type(value["cutoff_seed_hops"]) is not int or not 0 <= value["cutoff_seed_hops"] <= 2 ** 53 - 1):
                raise ValueError
        elif (value["stop_reason"] != "paths_exhausted" or value["stopping_outpoint"] is not None
                or value["cutoff_seed_hops"] is not None):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError, TraceError):
        raise TraceError("Saved cumulative peg-out limit summary is inconsistent; regenerate the plot") from None
    return dict(value)


class _Total:
    def __init__(self, limit):
        self.limit = normalize_pegout_lbtc_limit(limit)
        self.target, self.total = _base_units(self.limit), 0
        self.counts = dict.fromkeys(COUNT_FIELDS, 0)
        self.stopping_outpoint = self.cutoff = None

    def include(self, key, output, distance):
        if output_kind(output) != "pegout":
            return False
        asset, amount = _asset(output), _amount(output)
        if not asset:
            self.counts["unknown_asset_count"] += 1
        elif asset != "L-BTC":
            self.counts["non_lbtc_count"] += 1
        elif amount == "":
            self.counts["unknown_amount_count"] += 1
        else:
            self.counts["counted_pegout_count"] += 1
            self.total += amount
        if self.total >= self.target:
            self.stopping_outpoint, self.cutoff = key, distance
            return True
        return False

    def summary(self):
        excess = max(0, self.total - self.target)
        return {"schema_version": 1, "target_lbtc": self.limit, "target_base_units": str(self.target),
                "total_lbtc": lbtc_units(self.total), "total_base_units": str(self.total),
                "excess_lbtc": lbtc_units(excess), "excess_base_units": str(excess),
                "limit_reached": self.stopping_outpoint is not None,
                "stop_reason": "limit_reached" if self.stopping_outpoint is not None else "paths_exhausted",
                "ordering": ORDERING, "cutoff_seed_hops": self.cutoff,
                "stopping_outpoint": self.stopping_outpoint, "context_outputs_counted": False, **self.counts}


def limited_paths(state, query, forward, endpoints, confirmed, *, seed_distances=None,
                  selected_endpoints=None, max_seed_hops=None, limit_summary=None):
    """Walk whole breadth-first frontiers before applying the cumulative cutoff.

    Every predecessor at the stopping distance is known before its endpoints
    are considered. Later frontiers are not expanded. Per-seed CSV callers use
    the combined selection and cutoff instead of creating separate budgets.
    """
    from .pegout_paths import _endpoint_matches
    from .named_hop_paths import transaction_depths

    named = bool(reference_name(state))
    addresses = reference_addresses(state) if named else set()
    rows = {txid: record["data"]["vout"] for txid, record in state["transactions"].items()}
    links = {key: child for entries in forward.values() for child, key, _ in entries}
    starts = set(query["seeds"]) if "seeds" in query else {
        f"{query['txid']}:{index}" for index in range(len(rows.get(query["txid"], [])))}
    maximum = query["max_hops"]
    ordinary_max = len(rows) - 1 if max_seed_hops is None else max_seed_hops
    respect_hops = query.get("attribution_hop_limits") != "ignore"
    frontier, seen, predecessors = set(), set(), defaultdict(set)

    def admit(key, depth, remaining, distance, previous=None):
        txid, index = parse_outpoint(key)
        if txid not in confirmed or depth > maximum or distance > ordinary_max:
            return
        output = rows[txid][index]
        if output_kind(output) == "fee":
            return
        remaining = min(remaining, output_budget(state["labels"], key, output,
                                                 respect_attribution_hops=respect_hops))
        point = (key, depth, remaining, distance)
        if previous is not None:
            predecessors[point].add(previous)
        if point not in seen:
            seen.add(point)
            frontier.add(point)

    for key in sorted(starts):
        admit(key, 0, math.inf, 0)
    accepted, endpoint_points = set(), defaultdict(set)
    total = _Total(query["pegout_lbtc_limit"]) if selected_endpoints is None else None
    while frontier:
        current, frontier = frontier, set()
        candidates = defaultdict(set)
        for point in current:
            key, depth, _, _ = point
            txid, index = parse_outpoint(key)
            if depth >= query["min_hops"] and index in endpoints[txid]:
                candidates[key].add(point)
        stopped = False
        for key in sorted(candidates, key=parse_outpoint):
            if key in accepted:
                endpoint_points[key].update(candidates[key])
                continue
            if selected_endpoints is not None and key not in selected_endpoints:
                continue
            if stopped:
                continue
            accepted.add(key)
            endpoint_points[key].update(candidates[key])
            if total is not None:
                txid, index = parse_outpoint(key)
                stopped = total.include(key, rows[txid][index], next(iter(candidates[key]))[3])
        if stopped:
            break
        for point in current:
            key, depth, remaining, distance = point
            if remaining <= 0 or key not in links or distance >= ordinary_max:
                continue
            child = links[key]
            for index, output in enumerate(rows[child]):
                next_depth = (0 if named and output.get("scriptpubkey_address") in addresses
                              and output_kind(output) == "spendable" else depth + 1)
                admit(f"{child}:{index}", next_depth, remaining - 1, distance + 1, point)
    successful = set().union(*endpoint_points.values()) if endpoint_points else set()
    kept, marked, pending = set(), set(successful), list(successful)
    output_depths, depths, distances = defaultdict(set), defaultdict(set), defaultdict(set)
    for key, points in endpoint_points.items():
        distances[key].update(point[1] for point in points)
        if seed_distances is not None:
            seed_distances[key] = min(point[3] for point in points)
    while pending:
        point = pending.pop()
        key, depth, _, _ = point
        output_depths[key].add(depth)
        depths[parse_outpoint(key)[0]].add(depth)
        for previous in predecessors[point]:
            kept.add(previous[0])
            if previous not in marked:
                marked.add(previous)
                pending.append(previous)
    if total is not None and limit_summary is not None:
        limit_summary.update(total.summary())
    if named:
        depths = transaction_depths(output_depths)
    return kept, _endpoint_matches(state, endpoints, distances), depths, output_depths if named else {}
