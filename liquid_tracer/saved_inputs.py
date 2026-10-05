"""Resolve an input's public facts from exact, already saved funding evidence."""

from .common import TraceError, canonical, output_kind


def saved_input_output(transactions, vin, *, blockchain="liquid"):
    """Keep raw transaction payloads intact while resolving missing prevout facts.

    Only the full funding transaction ID and output index can supply facts.
    Bitcoin peg-ins, coinbase inputs and unknown funding transactions retain
    their original prevout. Present redundant evidence must agree with funding.
    This resolves display/accounting facts, not a spend observation or status.
    """
    prevout = vin.get("prevout")
    if prevout is not None and not isinstance(prevout, dict):
        raise TraceError("Saved input prevout must contain public output facts")
    prevout = prevout or {}
    parent = vin.get("txid")
    if vin.get("is_pegin") or vin.get("is_coinbase") or parent not in transactions:
        return prevout
    index = vin.get("vout")
    funding = transactions[parent]["data"]["vout"]
    if type(index) is not int or not 0 <= index < len(funding):
        raise TraceError("Input references an invalid saved funding output")
    output = funding[index]
    if output_kind(output, blockchain) != "spendable":
        raise TraceError("Input spends a non-spendable saved output")
    fields = ("scriptpubkey", "scriptpubkey_asm", "scriptpubkey_type", "scriptpubkey_address",
              "value", "valuecommitment", "asset", "assetcommitment", "noncecommitment", "pegout")
    if any(field in prevout and canonical(prevout[field]) != canonical(output.get(field))
           for field in fields):
        raise TraceError("Input disagrees with its saved funding output")
    return {**output, **prevout}


def input_evidence(state, transactions=None):
    """Combine lookup-only funding context with graph transactions, never paths."""
    transactions = state["transactions"] if transactions is None else transactions
    context = state.get("saved_transactions", {})
    if not isinstance(context, dict) or not isinstance(transactions, dict):
        raise TraceError("Saved input evidence must be a transaction mapping")
    for txid in context.keys() & transactions.keys():
        if canonical(context[txid]["data"]) != canonical(transactions[txid]["data"]):
            raise TraceError("Saved input evidence conflicts with its transaction")
    return {**context, **transactions} if context else transactions
