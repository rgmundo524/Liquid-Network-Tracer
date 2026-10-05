"""Explicit chain identity shared by collection, presentation and exports.

Older investigations and immutable artifacts without a blockchain field are
Liquid. API URLs and public asset fields never silently change that identity.
"""

from urllib.parse import urlsplit


def blockchain(value=None):
    if isinstance(value, dict):
        value = value.get("blockchain", "liquid")
    elif value is None:
        value = "liquid"
    if not isinstance(value, str) or value not in ("liquid", "bitcoin"):
        from .common import TraceError
        raise TraceError("Unsupported blockchain. Choose Liquid or Bitcoin.")
    return value


def default_api(value=None):
    return "https://enterprise.blockstream.info" + ("/api" if blockchain(value) == "bitcoin" else "/liquid/api")


def explorer_root(value=None, *, source=None):
    chain = blockchain(value)
    if source is None and isinstance(value, dict):
        source = value.get("source") or value.get("namespace", {}).get("source")
    path = urlsplit(source or "").path
    if chain == "liquid":
        suffix = "/liquidtestnet" if path.startswith("/liquidtestnet/") else "/liquid"
    else:
        suffix = next(("/" + network for network in ("testnet4", "testnet", "signet")
                       if path.startswith("/" + network + "/")), "")
    return "https://blockstream.info" + suffix


def is_primary(node, graph=None):
    return node.get("details", {}).get("network", "liquid") == blockchain(graph)
