"""Fast workspace admission for bounded public Lightning-channel lookups."""
from .common import HEX64, TraceError
from .lightning_swap import validate_swap_id

MAX_LIGHTNING_BATCH = 100


def start_lookup(server, body):
    if not isinstance(body, dict) or body.get("action") != "lightning-lookup":
        raise TraceError("Choose Bitcoin transaction hashes for Lightning Network lookup")
    supplied = set(body)
    if "txids" in body:
        if supplied != {"action", "txids"}:
            raise TraceError("CSV Lightning checks accept only Bitcoin transaction hashes")
        values = body.get("txids")
        if (not isinstance(values, list) or not 1 <= len(values) <= MAX_LIGHTNING_BATCH
                or any(not isinstance(value, str) or not HEX64.fullmatch(value) for value in values)):
            raise TraceError("Choose 1 to 100 Bitcoin transaction hashes for Lightning Network lookup")
        txids = list(dict.fromkeys(value.lower() for value in values))
        if len(txids) != len(values):
            raise TraceError("Remove duplicate Bitcoin transaction hashes from the CSV")
        arguments = ["lightning-lookup", "--txids", ",".join(txids)]
    else:
        if not {"action", "txid"} <= supplied or supplied - {"action", "txid", "boltz_swap_id"}:
            raise TraceError("Choose one Bitcoin transaction hash for Lightning Network lookup")
        txid = body.get("txid")
        if not isinstance(txid, str) or not HEX64.fullmatch(txid):
            raise TraceError("Bitcoin transaction must be a 64-character transaction hash")
        txids = [txid.lower()]
        arguments = ["lightning-lookup", "--txid", txids[0]]
        if "boltz_swap_id" in body:
            arguments += ["--boltz-swap-id", validate_swap_id(body["boltz_swap_id"])]
    return server.start_job(arguments,
                            action="lightning-lookup", live=False, txids=txids)
