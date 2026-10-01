"""Auditable transaction and terminal-output tables for a saved endpoint plot.

Membership follows exact qualifying seed paths, never address reuse or the
union of drawn lines. Public amounts describe outputs, not value allocation
from any listed source, and a peg-out request does not prove a Bitcoin payout.
"""
from collections import defaultdict
import csv
import json
from pathlib import Path
import re

from .common import TraceError, canonical, output_kind, parse_outpoint
from .group_hops import reference_name
from .pegout_paths import _paths, validate_query
from .transaction_csv import _amount, _asset, _block_time, _text, transaction_csv_rows


PATH_FIELDS = (
    "Transaction Hash", "Roles", "Source Transactions", "Source Seed Outpoints",
    "Source Paths", "Traced Input Outpoints", "Traced Output Outpoints", "Endpoint Outpoints",
    "Hop Counts", "Hop Reference", "Seed Depth", "Block", "Time",
    "Transaction Observation ID", "Transaction Observed At", "Explorer URL",
)
ENDPOINT_FIELDS = (
    "Transaction Hash", "Vout", "Outpoint", "Source Transactions", "Source Seed Outpoints",
    "Source Paths", "Address", "Receiving Entity", "Status", "Asset", "Value Base Units",
    "Value LBTC", "Hop Counts", "Hop Reference", "Seed Depth", "Block", "Time",
    "Transaction Observation ID", "Transaction Observed At", "Spend Observation ID",
    "Spend Observed At", "Explorer URL", "Hops from Seed", "Source Seed Hops",
)
ENDPOINT_TABLE_FIELDS = (
    "Source", "Source Value", "Deposit/Peg-out Tx", "Address/Peg-out Address",
    "Receiving Entity", "Status", "Pegout LBTC", "Hops from Seed", "Source Seed Hops",
    "Vout", "Outpoint", "Source Seed Outpoints",
    "Source Paths", "Hop Counts", "Hop Reference", "Value LBTC", "Asset", "Value Base Units",
    "Seed Depth", "Block", "Time", "Transaction Observation ID", "Transaction Observed At",
    "Spend Observation ID", "Spend Observed At", "Explorer URL",
)


def _joined(values):
    return "; ".join(str(value) for value in sorted(set(values)))


def _lbtc_units(amount):
    whole, fraction = divmod(amount, 100_000_000)
    return str(whole) + (("." + f"{fraction:08d}".rstrip("0")) if fraction else "")


def _whole_lbtc(output):
    if _asset(output) != "L-BTC" or _amount(output) == "":
        return ""
    return _lbtc_units(_amount(output))


def pegout_lbtc_summary(graph, state):
    """Sum unique matching requests, never inferred assets or source allocation.

    Matching paths are already selected by the peg-out graph. Computing this
    small report once avoids rerunning path searches when the UI opens a case.
    """
    result = {"pegout_count": 0, "valued_lbtc_count": 0, "unknown_amount_count": 0,
              "unknown_asset_count": 0, "non_lbtc_count": 0}
    total, seen = 0, set()
    try:
        for match in graph["pegouts"]["matches"]:
            key = match["outpoint"]
            txid, index = parse_outpoint(key)
            if key in seen:
                continue
            seen.add(key)
            output = state["transactions"][txid]["data"]["vout"][index]
            if output_kind(output) != "pegout":
                raise TraceError("Peg-out amount summary requires actual peg-out outputs")
            asset, amount = _asset(output), _amount(output)
            result["pegout_count"] += 1
            if not asset:
                result["unknown_asset_count"] += 1
            elif asset != "L-BTC":
                result["non_lbtc_count"] += 1
            elif amount == "":
                result["unknown_amount_count"] += 1
            else:
                result["valued_lbtc_count"] += 1
                total += amount
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as error:
        raise TraceError("Peg-out amount summary requires complete saved output evidence") from error
    return {**result, "lbtc": _lbtc_units(total), "value_base_units": str(total)}


def validate_pegout_lbtc_summary(value, match_count):
    """Keep amounts exact in JSON and reject inconsistent saved summaries."""
    counts = {"valued_lbtc_count", "unknown_amount_count", "unknown_asset_count", "non_lbtc_count"}
    if (not isinstance(value, dict) or set(value) != counts | {"pegout_count", "lbtc", "value_base_units"}
            or any(type(value[key]) is not int or not 0 <= value[key] <= 2 ** 53 - 1
                   for key in counts | {"pegout_count"})
            or type(match_count) is not int or value["pegout_count"] != match_count
            or value["pegout_count"] != sum(value[key] for key in counts)
            or not isinstance(value["value_base_units"], str)
            or not re.fullmatch(r"0|[1-9][0-9]{0,99}", value["value_base_units"])):
        raise TraceError("Saved peg-out amount summary is inconsistent; regenerate the plot")
    amount = int(value["value_base_units"])
    if value["lbtc"] != _lbtc_units(amount) or (not value["valued_lbtc_count"] and amount):
        raise TraceError("Saved peg-out amount summary is inconsistent; regenerate the plot")
    return dict(value)


def _hops_by_transaction(depths, output_depths):
    if not output_depths:
        return depths
    result = defaultdict(set)
    for key, hops in output_depths.items():
        result[parse_outpoint(key)[0]].update(hops)
    return result


def _source_fields(sources):
    """Keep source-to-distance associations without repeating monetary rows."""
    return {
        "Source Transactions": _joined(parse_outpoint(key)[0] for key in sources),
        "Source Seed Outpoints": _joined(sources),
        "Source Paths": json.dumps([
            {"seed_outpoint": key, "hops": sorted(hops)} for key, hops in sorted(sources.items())
        ], ensure_ascii=False, separators=(",", ":")),
    }


def _observed_at(observations, observation_id, state, endpoint):
    """A retrieval timestamp must belong to this exact API observation."""
    if not observations or observation_id in (None, ""):
        return ""
    record = observations.get(observation_id)
    if record is None:
        return ""
    if (record.get("id") != observation_id or record.get("source") != state["source"]
            or record.get("endpoint") != endpoint or record.get("status") != 200):
        raise TraceError("Endpoint CSV observation does not match its saved transaction or outspends request")
    stamp = record.get("fetched_at", "")
    if not isinstance(stamp, str):
        raise TraceError("Endpoint CSV observation timestamp must be text")
    return stamp


def _transaction_fields(txid, state, observations):
    record = state["transactions"][txid]
    block, stamp = _block_time(record["data"])
    oid = record.get("observation_id", "")
    source = state["source"]
    explorer = "https://blockstream.info/" + ("liquidtestnet" if "liquidtestnet" in source else "liquid")
    return {
        "Transaction Hash": txid, "Seed Depth": record.get("depth", ""), "Block": block,
        "Time": stamp, "Transaction Observation ID": oid,
        "Transaction Observed At": _observed_at(observations, oid, state, "/tx/" + txid),
        "Explorer URL": "" if source.startswith("fixture://") else explorer + "/tx/" + txid,
    }


def pegout_csv_rows(graph, state, *, observations=None):
    """Return one row per qualifying transaction and one per terminal UTXO."""
    try:
        report = graph["pegouts"]
        raw_query = report["query"]
        name = reference_name(state)
        query = validate_query(raw_query.get("txid"), raw_query.get("min_hops", 0),
                               raw_query.get("max_hops", 10), seeds=raw_query.get("seeds"),
                               include_unspent=raw_query.get("include_unspent", False),
                               include_unspendable=raw_query.get("include_unspendable", False),
                               include_context=raw_query.get("include_context", False),
                               transaction_io=raw_query.get("transaction_io"),
                               hop_reference_name=name)
        if canonical(query) != canonical(raw_query) or reference_name(graph) != name:
            raise TraceError("Endpoint CSV hop reference or query disagrees with the saved graph")
        outpoints, matches, depths, output_depths = _paths(state, query)
        pegouts = [{key: value for key, value in match.items() if key != "kind"}
                   for match in matches if match["kind"] == "pegout"]
        if (report.get("outpoints") != sorted(outpoints)
                or canonical(report.get("matches")) != canonical(pegouts)
                or report.get("match_count") != len(pegouts)
                or report.get("transaction_count") != len(depths)):
            raise TraceError("Endpoint CSV path report disagrees with saved spend evidence")
        if query.get("include_unspent") or query.get("include_unspendable"):
            counts = {kind: sum(match["kind"] == kind for match in matches)
                      for kind in ("pegout", "unspent", "unspendable")}
            if (canonical(report.get("endpoint_matches")) != canonical(matches)
                    or report.get("endpoint_count") != len(matches)
                    or report.get("endpoint_counts") != counts):
                raise TraceError("Endpoint CSV terminal outputs disagree with saved spend evidence")
        endpoints = {match["outpoint"] for match in matches}
        expected_edges = {"out:" + key for key in outpoints | endpoints}
        expected_edges.update(f"in:{state['links'][key]['spending_txid']}:{state['links'][key]['vin']}"
                              for key in outpoints)
        actual_edges = {edge["id"] for edge in graph["edges"]}
        transactions = {node["id"][3:] for node in graph["nodes"] if node["kind"] == "transaction"}
        if transactions != set(depths) or not expected_edges <= actual_edges:
            raise TraceError("Endpoint CSV graph is missing qualifying transactions or path arrows")
        complete = query.get("transaction_io") == "complete"
        if complete:
            local_edges = {f"{direction}:{txid}:{index}"
                           for txid in transactions for direction, field in (("in", "vin"), ("out", "vout"))
                           for index in range(len(state["transactions"][txid]["data"][field]))}
            if actual_edges != local_edges:
                raise TraceError("Endpoint CSV graph is missing complete transaction inputs or outputs")
        if any(edge["id"] not in expected_edges and
               (not (complete or query.get("include_context")) or not edge.get("role", "").startswith("context_"))
               for edge in graph["edges"]):
            raise TraceError("Endpoint CSV graph contains an unrelated path arrow")
        # Reuse the established exact-I/O and grouped-context validation, as
        # well as attribution resolution for each output occurrence.
        io_rows = transaction_csv_rows(graph, state)
        output_rows = {(row["Transaction Hash"], row["Number of I/O"]): row
                       for row in io_rows if row["Direction"] == "OUT"}
        if "seeds" in query:
            seeds = query["seeds"]
        else:
            txid = query["txid"]
            seeds = [f"{txid}:{index}" for index, output in
                     enumerate(state["transactions"].get(txid, {}).get("data", {}).get("vout", []))
                     if output_kind(output) != "fee"]
        transaction_sources, endpoint_sources = defaultdict(dict), defaultdict(dict)
        endpoint_seed_hops = defaultdict(dict)
        for seed in seeds:
            individual = {key: value for key, value in query.items() if key not in ("seeds", "txid")}
            individual["seeds"] = [seed]
            seed_distances = {}
            _, source_matches, source_depths, source_output_depths = _paths(
                state, individual, seed_distances=seed_distances)
            for txid, hops in _hops_by_transaction(source_depths, source_output_depths).items():
                transaction_sources[txid][seed] = set(hops)
            for match in source_matches:
                endpoint_sources[match["outpoint"]][seed] = set(match["hops"])
                endpoint_seed_hops[match["outpoint"]][seed] = seed_distances[match["outpoint"]]
        if set(transaction_sources) != set(depths) or set(endpoint_sources) != endpoints:
            raise TraceError("Endpoint CSV source paths disagree with the combined plot query")
        input_keys, output_keys, endpoint_keys = defaultdict(set), defaultdict(set), defaultdict(set)
        for key in outpoints:
            input_keys[state["links"][key]["spending_txid"]].add(key)
        for key in outpoints | endpoints:
            output_keys[parse_outpoint(key)[0]].add(key)
        for key in endpoints:
            endpoint_keys[parse_outpoint(key)[0]].add(key)
        path_rows = []
        for txid, sources in sorted(transaction_sources.items()):
            roles = []
            if any(parse_outpoint(seed)[0] == txid for seed in sources):
                roles.append("Seed")
            if input_keys[txid] and (output_keys[txid] - endpoints):
                roles.append("Intermediate")
            if endpoint_keys[txid]:
                roles.append("Endpoint")
            path_rows.append({
                **_transaction_fields(txid, state, observations), **_source_fields(sources),
                "Roles": "; ".join(roles), "Traced Input Outpoints": _joined(input_keys[txid]),
                "Traced Output Outpoints": _joined(output_keys[txid]),
                "Endpoint Outpoints": _joined(endpoint_keys[txid]),
                "Hop Counts": _joined(hop for hops in sources.values() for hop in hops),
                "Hop Reference": name or "Selected seed outputs",
            })
        endpoint_rows = []
        for match in matches:
            txid, index, key = match["txid"], match["vout"], match["outpoint"]
            output = state["transactions"][txid]["data"]["vout"][index]
            io = output_rows[txid, index]
            kind = match["kind"]
            status = ("Peg-out" if kind == "pegout" else "Dormant" if kind == "unspent" else
                      "OP_RETURN" if output.get("scriptpubkey_type") == "op_return"
                      or output.get("scriptpubkey", "").startswith("6a") else "Unspendable")
            oid = match.get("spend_observation_id", "") if kind == "unspent" else ""
            endpoint_rows.append({
                **_transaction_fields(txid, state, observations),
                **_source_fields(endpoint_sources[key]), "Vout": index, "Outpoint": key,
                "Address": io["Address Hash"], "Receiving Entity": io["Address Label"],
                "Status": status, "Asset": _asset(output), "Value Base Units": _amount(output),
                "Value LBTC": _whole_lbtc(output), "Hop Counts": _joined(match["hops"]),
                "Hops from Seed": min(endpoint_seed_hops[key].values()),
                "Source Seed Hops": json.dumps(endpoint_seed_hops[key], sort_keys=True, separators=(",", ":")),
                "Hop Reference": name or "Selected seed outputs", "Spend Observation ID": oid,
                "Spend Observed At": _observed_at(observations, oid, state, "/tx/" + txid + "/outspends"),
            })
        order = lambda row: (row["Time"] == "", row["Time"], row["Transaction Hash"], row.get("Vout", -1))
        path_rows.sort(key=order)
        endpoint_rows.sort(key=order)
        return path_rows, endpoint_rows
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as error:
        raise TraceError("Endpoint CSV requires complete, consistent saved path evidence") from error


def write_pegout_csvs(destination, graph, state, *, observations=None):
    """Write both tables, including their headers when no endpoints match."""
    path_rows, endpoint_rows = pegout_csv_rows(graph, state, observations=observations)
    destination = Path(destination)
    for name, rows, fields in (("path-transactions.csv", path_rows, PATH_FIELDS),
                               ("trace-endpoints.csv", endpoint_rows, ENDPOINT_FIELDS)):
        with (destination / name).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: _text(value) for key, value in row.items()})
    return {"path_transactions": len(path_rows), "trace_endpoints": len(endpoint_rows)}


def _source_values(row, state):
    """Describe selected source UTXOs without inferring external BTC totals."""
    values = {}
    for source in json.loads(row["Source Paths"]):
        key = source["seed_outpoint"]
        txid, index = parse_outpoint(key)
        output = state["transactions"][txid]["data"]["vout"][index]
        asset, amount = _asset(output), _amount(output)
        value = ""
        if asset and amount != "":
            value = (_whole_lbtc(output) + " L-BTC" if asset == "L-BTC"
                     else str(amount) + " base units " + asset)
        values[key] = value
    if len(values) == 1:
        return next(iter(values.values()))
    # Multiple seed outputs can share a transaction or converge. Preserve the
    # exact outpoint-to-value association rather than adding amounts together.
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def endpoint_table_rows(graph, state, *, observations=None):
    """User-facing terminal-output table with the supplied sample's lead columns.

    Source values are the explicit selected Liquid UTXOs, never an external
    Bitcoin source total or an allocation to the endpoint. A caller exporting
    an older plot must supply the archived run with that plot's saved service
    controls applied, so later attribution changes cannot alter its contents.
    """
    _, endpoints = pegout_csv_rows(graph, state, observations=observations)
    rows = []
    for endpoint in endpoints:
        status = {"Peg-out": "Pegout", "OP_RETURN": "OP_Return",
                  "Dormant": "Dormant", "Unspendable": "Unspendable"}[endpoint["Status"]]
        row = {key: endpoint[key] for key in ENDPOINT_TABLE_FIELDS if key in endpoint}
        row.update({
            "Source": endpoint["Source Transactions"], "Source Value": _source_values(endpoint, state),
            "Deposit/Peg-out Tx": endpoint["Transaction Hash"],
            "Address/Peg-out Address": endpoint["Address"], "Status": status,
            "Pegout LBTC": endpoint["Value LBTC"] if status == "Pegout" else "",
        })
        rows.append(row)
    return rows


def write_endpoint_table_csv(path, graph, state, *, observations=None):
    """Write the endpoint summary, including headers for an empty saved plot."""
    rows = endpoint_table_rows(graph, state, observations=observations)
    with Path(path).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=ENDPOINT_TABLE_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _text(value) for key, value in row.items()})
    return len(rows)
