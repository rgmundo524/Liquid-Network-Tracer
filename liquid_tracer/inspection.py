"""Inspect bounded transaction batches to choose exact seed outputs."""

import re
from tempfile import TemporaryDirectory

from .api import ENTERPRISE, Esplora, Limits
from .common import HEX64, StopRun, TraceError, output_kind
from .store import Store

MAX_INSPECTION_TRANSACTIONS = 100


def parse_transaction_hashes(value):
    """Validate the complete input before lookup, preserving the first-seen order."""
    if isinstance(value, str):
        values = [part for part in re.split(r"[,\s]+", value.strip()) if part]
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise TraceError("Enter transaction hashes separated by commas")
    if not values:
        raise TraceError("Enter at least one transaction hash")
    result, seen = [], set()
    for position, txid in enumerate(values, 1):
        if not isinstance(txid, str) or not HEX64.fullmatch(txid):
            # Do not echo pasted input, which might contain an accidental secret.
            raise TraceError(f"Transaction hash {position} must contain exactly 64 hexadecimal characters; enter hashes without :vout")
        txid = txid.lower()
        if txid not in seen:
            seen.add(txid)
            result.append(txid)
    if len(result) > MAX_INSPECTION_TRANSACTIONS:
        raise TraceError(f"Inspect at most {MAX_INSPECTION_TRANSACTIONS} distinct transaction hashes at a time")
    return result


def transaction_outputs(txid, transaction):
    """Return public output fields without inferring hidden values or ownership."""
    if (not isinstance(transaction, dict)
            or not isinstance(transaction.get("txid"), str)
            or transaction["txid"].lower() != txid):
        raise TraceError("Explorer response does not match the requested transaction hash")
    outputs = transaction.get("vout")
    if not isinstance(outputs, list):
        raise TraceError("Explorer response has no valid transaction output list")
    result = []
    for index, output in enumerate(outputs):
        if not isinstance(output, dict):
            raise TraceError("Explorer response contains an invalid transaction output")
        for field in ("scriptpubkey_type", "scriptpubkey_address", "asset"):
            if output.get(field) is not None and not isinstance(output[field], str):
                raise TraceError("Explorer response contains invalid public output fields")
        # output_kind uses the script bytes to distinguish unspendable outputs.
        if not isinstance(output.get("scriptpubkey"), str):
            raise TraceError("Explorer response contains an invalid output script")
        value = output.get("value")
        if value is not None and (type(value) is not int or value < 0):
            raise TraceError("Explorer response contains an invalid explicit output value")
        kind = output_kind(output)
        result.append({
            "vout": index,
            "outpoint": f"{txid}:{index}",
            "address": output.get("scriptpubkey_address"),
            "value": value,
            "asset": output.get("asset"),
            "script_type": output.get("scriptpubkey_type"),
            "selectable": kind == "spendable",
            "reason": None if kind == "spendable" else kind,
        })
    return {"txid": txid, "outputs": result}


def inspect_transaction(txid, *, fixture=None, base_url=ENTERPRISE, auth="blockstream",
                        max_requests=5, max_seconds=30):
    """Fetch one transaction, with the same bounded authenticated client as tracing.

    Temporary observations only support the lookup. A later trace fetches and saves
    its own evidence, after the investigator has selected the seed outpoints.
    """
    if not isinstance(txid, str) or not HEX64.fullmatch(txid):
        raise TraceError("Transaction hash must contain exactly 64 hexadecimal characters; enter the hash without :vout")
    txid = txid.lower()
    limits = Limits(max_hops=0, max_transactions=1, max_requests=max_requests,
                    max_seconds=max_seconds)
    limits.validate()
    with TemporaryDirectory(prefix="liquid-tx-lookup-") as directory:
        store = Store(directory)
        try:
            with Esplora(store, "transaction-lookup", limits, base=base_url, auth=auth,
                         fixture=fixture, tx_cache_seconds=0) as api:
                try:
                    transaction, _ = api.get("/tx/" + txid)
                except StopRun as error:
                    reasons = {"request_limit": "request limit reached", "time_limit": "time limit reached",
                               "server_retry_later": "explorer requested a later retry"}
                    raise TraceError("Transaction lookup stopped: " + reasons.get(str(error), "lookup limit reached")) from None
                return transaction_outputs(txid, transaction)
        finally:
            store.close()


def inspect_transactions(txids, *, fixture=None, base_url=ENTERPRISE, auth="blockstream",
                         max_requests=None, max_seconds=None):
    """Inspect a batch with one client and a single finite request/time budget.

    Return a complete report only after every transaction is validated. The
    temporary observations never select seeds or start a trace themselves.
    """
    txids = parse_transaction_hashes(txids)
    count = len(txids)
    limits = Limits(max_hops=0, max_transactions=count,
                    max_requests=5 * count if max_requests is None else max_requests,
                    max_seconds=30 * count if max_seconds is None else max_seconds)
    limits.validate()
    with TemporaryDirectory(prefix="liquid-tx-lookup-") as directory:
        store = Store(directory)
        try:
            with Esplora(store, "transaction-lookup", limits, base=base_url, auth=auth,
                         fixture=fixture, tx_cache_seconds=0) as api:
                fetched = api.prefetch(["/tx/" + txid for txid in txids])
                transactions = []
                for txid in txids:
                    try:
                        result = fetched["/tx/" + txid]
                        if isinstance(result, (TraceError, StopRun)):
                            raise result
                        transaction, _ = result
                        transactions.append(transaction_outputs(txid, transaction))
                    except StopRun as error:
                        reasons = {"request_limit": "request limit reached", "time_limit": "time limit reached",
                                   "server_retry_later": "explorer requested a later retry"}
                        raise TraceError(f"Transaction lookup stopped for {txid}: "
                                         + reasons.get(str(error), "lookup limit reached")) from None
                    except TraceError as error:
                        # API and output validation errors contain safe diagnostics,
                        # never server bodies, credentials, or arbitrary input text.
                        raise TraceError(f"Transaction lookup failed for {txid}: {error}") from None
                return {"transactions": transactions}
        finally:
            store.close()
