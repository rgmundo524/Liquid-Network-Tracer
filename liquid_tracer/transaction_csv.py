"""Transaction-centric CSV: one input/output occurrence per displayed arrow.

Amounts are exact explicit base-unit integers, never floats or inferred values.
The graph selects occurrences; transaction facts come from the saved trace.
"""
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import csv

from .common import HEX64, LBTC, TraceError, canonical, output_kind
from .attribution_presentation import display_name

TRANSACTION_CSV_FIELDS = (
    "Block", "Time", "Transaction Label", "Transaction Hash", "Address Label",
    "Address Flags", "Address Hash", "Asset Value", "Asset",
    "PegOut Value", "Direction", "Number of I/O",
)
VALUE_NOTICE = (
    "Asset Value and PegOut Value are exact explicit base units (satoshis for BTC/L-BTC), "
    "not inferred whole-token amounts. Asset is L-BTC for its recognized explicit asset ID, "
    "BTC for a Bitcoin peg-in input, or the full explicit Liquid asset ID otherwise. "
    "Confidential or missing asset identities are blank, never inferred from an amount, "
    "address label or graph color. Do not sum different assets. Blank amounts are unknown, "
    "not zero. PegOut Value repeats the peg-out request output amount; it is not a second "
    "transfer or proof of a Bitcoin payout."
)


def _amount(output):
    value = output.get("value")
    if value is None:
        return ""
    if type(value) is not int or value < 0:
        raise TraceError("Transaction CSV requires explicit nonnegative integer base-unit amounts")
    return value


def _asset(output, *, pegin=False):
    """Identify this occurrence's asset without consulting labels or a registry."""
    if pegin:
        # This occurrence is the Bitcoin prevout consumed by a Liquid peg-in,
        # not an L-BTC output minted in the receiving transaction.
        return "BTC"
    asset = output.get("asset")
    if asset is None:
        return ""
    if not isinstance(asset, str) or not HEX64.fullmatch(asset):
        raise TraceError("Transaction CSV requires a full explicit hexadecimal asset ID")
    return "L-BTC" if asset.lower() == LBTC else asset


def _block_time(transaction):
    status = transaction.get("status") or {}
    if status.get("confirmed") is not True:
        return "", ""
    block, stamp = status.get("block_height"), status.get("block_time")
    block = block if type(block) is int and block >= 0 else ""
    timestamp = ""
    if type(stamp) is int and stamp >= 0:
        try:
            timestamp = datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, OverflowError, OSError):
            pass
    return block, timestamp


def _text(value):
    """Protect spreadsheet cells, including formula prefixes after whitespace."""
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def _context_endpoints(nodes, edges):
    """Resolve summary inputs to their original address evidence, never a union.

    A summary is only a display replacement. Its complete membership and input
    incidence must agree before any hidden member can be an export endpoint.
    Dimensions, text and port positions play no part in this evidence check.
    """
    summaries = {key: node for key, node in nodes.items() if node["kind"] == "context_group"}
    if not summaries:
        return {}
    incident = defaultdict(list)
    for edge in edges:
        for key in {edge["source"], edge["target"]} & summaries.keys():
            incident[key].append(edge)
    result, used = {}, set()
    for key, node in summaries.items():
        details = node["details"]
        target = details["transaction_id"]
        if (not isinstance(target, str) or not target.startswith("tx:")
                or key != "context-group:" + target[3:]
                or nodes.get(target, {}).get("kind") != "transaction"
                or not isinstance(details.get("members"), list)):
            raise TraceError("Transaction CSV requires a complete context-summary membership")
        members = {}
        for member in details["members"]:
            identity, info = member["id"], member["details"]
            if (not isinstance(identity, str) or not identity or identity in nodes
                    or identity in members or identity in used or member.get("kind") != "address"
                    or info.get("network") != "liquid"
                    or not isinstance(info.get("address"), str) or not info["address"]):
                raise TraceError("Transaction CSV context summary contains an invalid address member")
            members[identity] = member
        addresses = {member["details"]["address"] for member in members.values()}
        inputs = details.get("input_edge_ids")
        if (len(addresses) < 2 or type(details.get("address_count")) is not int
                or details["address_count"] != len(addresses)
                or not isinstance(inputs, list) or any(not isinstance(item, str) for item in inputs)
                or len(inputs) != len(set(inputs)) or not inputs
                or type(details.get("input_count")) is not int or details["input_count"] != len(inputs)
                or len(incident[key]) != len(inputs) or {edge["id"] for edge in incident[key]} != set(inputs)):
            raise TraceError("Transaction CSV context-summary input membership disagrees with its arrows")
        sources = set()
        for edge in incident[key]:
            if (edge["source"] != key or edge["target"] != target
                    or edge.get("role") != "context_input" or not edge["id"].startswith("in:" + target[3:] + ":")
                    or edge.get("original_source") not in members):
                raise TraceError("Transaction CSV context-summary arrow has no matching original address")
            sources.add(edge["original_source"])
        if sources != set(members):
            raise TraceError("Transaction CSV context-summary members disagree with its input arrows")
        result[key] = members
        used.update(members)
    return result


def transaction_csv_rows(graph, state):
    """Return only displayed I/O occurrences, in UTC transaction / IN / OUT order.

    Inputs are indexed by vin in the spending transaction, NOT by the funding
    output's vout. Repeated addresses and the two sides of a spend stay distinct.
    No node styles, positions, Miro IDs, legends or synthetic ownership claims.
    """
    try:
        if (graph.get("run_id") != state.get("run_id")
                or graph.get("namespace", {}).get("case_id") != state.get("case_id")
                or graph.get("namespace", {}).get("source") != state.get("source")):
            raise TraceError("Transaction CSV graph does not match the saved run, case or source")
        nodes = {}
        for node in graph["nodes"]:
            if node["id"] in nodes:
                raise TraceError("Transaction CSV graph has duplicate nodes")
            nodes[node["id"]] = node
        transactions = {}
        for key, node in nodes.items():
            if node["kind"] != "transaction":
                continue
            transaction = node["details"]["transaction"]
            txid = transaction["txid"]
            archived = state["transactions"][txid]["data"]
            if key != "tx:" + txid or canonical(transaction) != canonical(archived):
                raise TraceError("Transaction CSV graph disagrees with saved transaction evidence")
            transactions[txid] = archived
        context_endpoints = _context_endpoints(nodes, graph["edges"])
        endpoint_nodes = {**nodes, **{key: node for members in context_endpoints.values()
                                      for key, node in members.items()}}
        labels = state.get("labels", [])
        index = defaultdict(list)
        for label in labels:
            index[(label["kind"], label["value"])].append(label)
        starts = {entry["key"]: entry["index"] for entry in
                  graph.get("activity_frames", {}).get("starting_transactions", [])}
        unspent = {key for node in nodes.values() for key in
                   node.get("details", {}).get("unspent_endpoints", [])}
        seeds = set(state.get("seeds", []))
        # Occurrence metadata already contains the exact displayed assessments,
        # including current overrides of an older archive and legacy labels.
        # Do not aggregate at the merged-address level or regenerate old rules.
        occurrence_labels = defaultdict(dict)
        endpoint_outpoints = {}
        for key, node in endpoint_nodes.items():
            if node["kind"] != "address":
                continue
            endpoint_outpoints[key] = set()
            for item in node["details"].get("occurrences", []):
                outpoint = item["outpoint"]
                endpoint_outpoints[key].add(outpoint)
                for label in item.get("labels", []):
                    occurrence_labels[key, outpoint][canonical(label)] = label
        rows, seen = [], set()
        for edge in graph["edges"]:
            direction, txid, raw_index = edge["id"].split(":")
            if direction not in ("in", "out") or not raw_index.isascii() or not raw_index.isdecimal():
                raise TraceError("Transaction CSV requires a transaction input/output arrow")
            io_index = int(raw_index)
            if raw_index != str(io_index) or (direction, txid, io_index) in seen:
                raise TraceError("Transaction CSV graph has duplicate or invalid I/O occurrences")
            seen.add((direction, txid, io_index))
            transaction = transactions[txid]
            tx_key = "tx:" + txid
            if edge["target" if direction == "in" else "source"] != tx_key:
                raise TraceError("Transaction CSV arrow direction disagrees with its transaction")
            other = nodes[edge["source" if direction == "in" else "target"]]
            grouped = other["kind"] == "context_group"
            if grouped:
                other = context_endpoints[other["id"]][edge["original_source"]]
            if other["kind"] not in ("address", "event"):
                raise TraceError("Transaction CSV arrow must join a transaction and an I/O endpoint")
            flags = []
            if direction == "in":
                vin = transaction["vin"][io_index]
                coinbase, pegin = bool(vin.get("is_coinbase")), bool(vin.get("is_pegin"))
                if grouped and (coinbase or pegin):
                    raise TraceError("Transaction CSV context summary cannot contain coinbase or peg-in inputs")
                output = {} if coinbase else (vin.get("prevout") or {})
                outpoint = f"{vin.get('txid', txid)}:{vin.get('vout', io_index)}"
                if coinbase:
                    flags.append("COINBASE")
                if pegin:
                    flags.append("PEG-IN")
            else:
                output = transaction["vout"][io_index]
                coinbase = pegin = False
                outpoint = f"{txid}:{io_index}"
            if edge.get("outpoint") != outpoint:
                raise TraceError("Transaction CSV arrow disagrees with its exact UTXO reference")
            if other["kind"] == "address":
                details = other["details"]
                if (details.get("network") != ("bitcoin" if pegin else "liquid")
                        or details.get("address") != output.get("scriptpubkey_address")
                        or outpoint not in endpoint_outpoints[other["id"]]):
                    raise TraceError("Transaction CSV address endpoint disagrees with its saved UTXO")
            elif other["id"] != (f"coinbase:{txid}:{io_index}" if coinbase else "event:" + outpoint):
                raise TraceError("Transaction CSV event endpoint disagrees with its saved I/O")
            kind = output_kind(output)
            pegout = direction == "out" and kind == "pegout"
            address = (output["pegout"].get("scriptpubkey_address") if pegout
                       else output.get("scriptpubkey_address")) or ""
            if not isinstance(address, str):
                raise TraceError("Transaction CSV address must be text")
            # Bitcoin peg-ins and coinbase inputs cannot inherit Liquid labels.
            # Peg-out destinations are Bitcoin addresses, not Liquid address keys.
            matches = []
            if not coinbase and not pegin:
                targets = [("outpoint", outpoint), ("script", output.get("scriptpubkey"))]
                if not pegout:
                    targets.append(("address", output.get("scriptpubkey_address")))
                targets = {target for target in targets if target[1] is not None}
                if other["kind"] == "address":
                    matches = list(occurrence_labels[other["id"], outpoint].values())
                    if any((label["kind"], label["value"]) not in targets for label in matches):
                        raise TraceError("Transaction CSV attribution does not match its exact UTXO")
                else:
                    matches = [label for target in targets for label in index[target]]
            names = sorted({display_name(label) for label in matches})
            if any(label.get("stop") is True for label in matches):
                flags.append("STOP TRACING")
            if not pegin and not coinbase and outpoint in seeds:
                flags.append("SELECTED SEED")
            if edge.get("role", "").startswith("context"):
                flags.append("CONTEXT")
            if outpoint in unspent and direction == "out":
                flags.append("UNSPENT AT OBSERVATION")
            if pegout:
                flags.append("PEG-OUT REQUEST")
            elif kind == "fee" and not coinbase:
                flags.append("FEE")
            elif kind == "provably_unspendable":
                flags.append("UNSPENDABLE")
            if output.get("value") is None and output.get("valuecommitment"):
                flags.append("CONFIDENTIAL VALUE")
            if not pegin and output.get("asset") is None and output.get("assetcommitment"):
                flags.append("CONFIDENTIAL ASSET")
            block, timestamp = _block_time(transaction)
            amount = _amount(output)
            row = dict(zip(TRANSACTION_CSV_FIELDS, (
                block, timestamp, f"Starting TX {starts[tx_key]}" if tx_key in starts else "", txid,
                "; ".join(names), "; ".join(flags), address,
                amount, _asset(output, pegin=pegin), amount if pegout else "", direction.upper(), io_index,
            )))
            rows.append(row)
        rows.sort(key=lambda row: (row["Time"] == "", row["Time"],
                                  row["Block"] if row["Block"] != "" else -1,
                                  row["Transaction Hash"], row["Direction"] != "IN", row["Number of I/O"]))
        return rows
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as error:
        raise TraceError("Transaction CSV requires complete, consistent saved I/O evidence") from error


def write_transaction_csv(path, graph, state):
    rows = transaction_csv_rows(graph, state)
    with Path(path).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=TRANSACTION_CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _text(value) for key, value in row.items()})
    return len(rows)
