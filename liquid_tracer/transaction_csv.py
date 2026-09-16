"""Transaction-centric CSV: one input/output occurrence per displayed arrow.

Amounts are exact explicit base-unit integers, never floats or inferred values.
The graph selects occurrences; transaction facts come from the saved trace.
"""
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import csv

from .common import TraceError, canonical, output_kind
from .attribution_presentation import display_name

TRANSACTION_CSV_FIELDS = (
    "Block", "Time", "Transaction Label", "Transaction Hash", "Address Label",
    "Address Flags", "Address Entities", "Address Hash", "Crypto Value",
    "USD Value", "PegOut Value", "Direction", "Number of I/O",
)
VALUE_NOTICE = (
    "Crypto Value and PegOut Value are exact explicit base units (satoshis for L-BTC), "
    "not inferred whole-token amounts. This fixed-column format has no asset column; "
    "consult the saved transaction for the asset and do not sum different assets. "
    "Blank amounts are unknown, not zero. USD Value is blank because no historical "
    "USD valuation source is recorded. PegOut Value repeats the peg-out request output "
    "amount; it is not a second transfer or proof of a Bitcoin payout."
)


def _amount(output):
    value = output.get("value")
    if value is None:
        return ""
    if type(value) is not int or value < 0:
        raise TraceError("Transaction CSV requires explicit nonnegative integer base-unit amounts")
    return value


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
        for key, node in nodes.items():
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
            if other["kind"] not in ("address", "event"):
                raise TraceError("Transaction CSV arrow must join a transaction and an I/O endpoint")
            flags = []
            if direction == "in":
                vin = transaction["vin"][io_index]
                coinbase, pegin = bool(vin.get("is_coinbase")), bool(vin.get("is_pegin"))
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
            entities = sorted({label.get("entity") or label.get("name") for label in matches
                               if label.get("entity") or label.get("name")})
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
                "; ".join(names), "; ".join(flags), "; ".join(entities), address,
                amount, "", amount if pegout else "", direction.upper(), io_index,
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
