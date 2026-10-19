"""Bounded inspection of one transaction and its direct spending transactions."""
import re

from .common import HEX64, StopRun, TraceError
from .lightning_scripts import inspect_input

MAX_INPUTS = 64
MAX_OUTPUTS = 32
MAX_SPENDING_TRANSACTIONS = 8
MAX_MONEY = 21000000 * 100000000


def _uint(value, maximum, label):
    if type(value) is not int or not 0 <= value <= maximum:
        raise TraceError("Invalid " + label)
    return value


def _hash(value):
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise TraceError("Invalid Bitcoin spending reference")
    return value.lower()


def _output(output):
    if not isinstance(output, dict):
        raise TraceError("Missing Bitcoin previous output")
    value = _uint(output.get("value"), MAX_MONEY, "Bitcoin output value")
    script = output.get("scriptpubkey")
    if not isinstance(script, str) or len(script) > 20000 or not re.fullmatch(r"(?:[0-9a-fA-F]{2})*", script):
        raise TraceError("Invalid Bitcoin output script")
    return value, script.lower()


def _result(status, summary, script_type="unknown"):
    return dict(status=status, script_type=script_type, template=None, branch="unknown",
                payment_hash=None, hash_algorithm=None, summary=summary, evidence=[])


def inspect_transaction(transaction, btc_transaction, fetch_outspends, check_budget):
    """Inspect only supplied inputs and up to eight distinct direct spenders.

    Hidden/unsupported scripts are conclusive about inspection coverage, never
    negative evidence about Lightning use. Missing data yields partial coverage.
    """
    txid = transaction["txid"]
    vin, vout = transaction["vin"], transaction["vout"]
    report = dict(status="complete", findings=[], checked_inputs=0, checked_outputs=0,
                  total_inputs=len(vin), total_outputs=len(vout), reason=None)
    reasons = []

    def partial(reason):
        report["status"] = "partial"
        if reason not in reasons:
            reasons.append(reason[:500])
        report["reason"] = "; ".join(reasons)[:1000]

    for index, entry in enumerate(vin[:MAX_INPUTS]):
        try:
            check_budget()
        except StopRun as error:
            partial(str(error))
            break
        if isinstance(entry, dict) and entry.get("is_coinbase") is True:
            report["checked_inputs"] += 1
            continue
        try:
            if not isinstance(entry, dict):
                raise TraceError("Invalid Bitcoin input")
            funding_txid = _hash(entry.get("txid"))
            funding_vout = _uint(entry.get("vout"), 0xffffffff, "Bitcoin previous output index")
        except TraceError as error:
            partial(str(error))
            continue
        value = None
        try:
            value, _ = _output(entry.get("prevout"))
            detail = inspect_input(entry)
            report["checked_inputs"] += 1
        except TraceError as error:
            detail = _result("unavailable", str(error)[:500])
            partial("Some input scripts could not be checked")
        report["findings"].append(dict(role="input", index=index, funding_txid=funding_txid,
            funding_vout=funding_vout, spending_txid=txid, spending_input_index=index,
            value_sat=value, **detail))
    if len(vin) > MAX_INPUTS:
        partial("Input inspection limit reached (64 inputs)")

    try:
        check_budget()
        spends = fetch_outspends()
        if not isinstance(spends, list) or len(spends) != len(vout):
            raise TraceError("Bitcoin output spend statuses do not match the transaction outputs")
    except (TraceError, StopRun) as error:
        partial(str(error))
        return report

    spending_ids = set()
    for index, output in enumerate(vout[:MAX_OUTPUTS]):
        value, spending_id, spending_input = None, None, None
        try:
            check_budget()
            value, script = _output(output)
            spent = spends[index]
            if not isinstance(spent, dict) or type(spent.get("spent")) is not bool:
                raise TraceError("Invalid Bitcoin output spend status")
            if not spent["spent"]:
                if any(key in spent for key in ("txid", "vin", "status")):
                    raise TraceError("Unspent Bitcoin output contains a spending reference")
                detail = _result("unspent", "Output is unspent at observation time. No spending witness is available; Lightning involvement is undetermined.")
            else:
                spending_id = _hash(spent.get("txid"))
                spending_input = _uint(spent.get("vin"), 99999, "Bitcoin spending input index")
                if spending_id == txid:
                    raise TraceError("Bitcoin output cannot spend itself")
                if spending_id not in spending_ids and len(spending_ids) >= MAX_SPENDING_TRANSACTIONS:
                    raise StopRun("Direct spending transaction limit reached (8 transactions)")
                spending_ids.add(spending_id)
                spending = btc_transaction(spending_id)
                if spending_input >= len(spending["vin"]):
                    raise TraceError("Reported spending input does not exist")
                entry = spending["vin"][spending_input]
                if not isinstance(entry, dict) or entry.get("txid") != txid or entry.get("vout") != index:
                    raise TraceError("Reported spending input does not spend this exact output")
                if entry.get("prevout") is not None and _output(entry["prevout"]) != (value, script):
                    raise TraceError("Spending input disagrees with the queried output")
                detail = inspect_input({**entry, "prevout": output})
            report["checked_outputs"] += 1
        except (TraceError, StopRun) as error:
            detail = _result("unavailable", str(error)[:500])
            partial(str(error))
        report["findings"].append(dict(role="output", index=index, funding_txid=txid,
            funding_vout=index, spending_txid=spending_id, spending_input_index=spending_input,
            value_sat=value, **detail))
    if len(vout) > MAX_OUTPUTS:
        partial("Output inspection limit reached (32 outputs)")
    return report
