"""Bounded, evidence-backed address activity without ownership inference.

Esplora's address statistics expose output *counts* on Liquid, not hidden
amounts. Confirmed history is newest first and contains 25 transactions/page:
https://github.com/Blockstream/esplora/blob/master/API.md#addresses
"""

import re
from datetime import datetime, timezone

from .common import HEX64, StopRun, TraceError, now

HISTORY_PAGE_SIZE = 25
DEFAULT_HISTORY_PAGES = 5
_ADDRESS = re.compile(r"[A-Za-z0-9]{14,200}|SYNTHETIC-[A-Za-z0-9_-]{1,180}")
_BECH32_PREFIXES = ("ex1", "tex1", "ert1", "lq1", "tlq1", "el1")
_STOP_REASONS = {"request_limit", "time_limit", "server_retry_later", "interrupted"}


def validate_address(value):
    """Validate path-safe address text; the explorer validates its network/checksum.

    Preserve Base58 case. Known all-uppercase bech32/blech32 addresses can be
    normalized without conflating case-sensitive Base58 addresses. The explicit
    SYNTHETIC prefix supports offline fixtures, never an ownership attribution.
    """
    if not isinstance(value, str):
        raise TraceError("Enter one Liquid address")
    value = value.strip()
    if not _ADDRESS.fullmatch(value):
        raise TraceError("Enter one Liquid address without spaces, a URL, or a transaction output suffix")
    if value.isupper() and value.lower().startswith(_BECH32_PREFIXES):
        value = value.lower()
    return value


def _stats(body, address):
    if not isinstance(body, dict) or body.get("address") != address:
        raise TraceError("Explorer address statistics do not match the requested address")
    result = []
    for name in ("chain_stats", "mempool_stats"):
        stats = body.get(name)
        if not isinstance(stats, dict):
            raise TraceError("Explorer address statistics are missing confirmed or mempool counts")
        parsed = {}
        for field in ("tx_count", "funded_txo_count", "spent_txo_count"):
            value = stats.get(field)
            if type(value) is not int or value < 0:
                raise TraceError("Explorer address statistics contain an invalid count")
            parsed[field] = value
        result.append(parsed)
    return result


def _activity(transaction, observation_id):
    if not isinstance(transaction, dict):
        raise TraceError("Explorer address history contains an invalid transaction")
    txid = transaction.get("txid")
    status = transaction.get("status")
    if (not isinstance(txid, str) or not HEX64.fullmatch(txid)
            or not isinstance(status, dict) or status.get("confirmed") is not True):
        raise TraceError("Explorer confirmed address history contains invalid transaction status")
    height = status.get("block_height")
    if type(height) is not int or height < 0:
        raise TraceError("Explorer confirmed address history contains an invalid block height")
    block_hash = status.get("block_hash")
    if block_hash is not None and (not isinstance(block_hash, str) or not HEX64.fullmatch(block_hash)):
        raise TraceError("Explorer confirmed address history contains an invalid block hash")
    stamp = status.get("block_time")
    if stamp is not None and (type(stamp) is not int or stamp < 0):
        raise TraceError("Explorer confirmed address history contains an invalid block timestamp")
    try:
        date = datetime.fromtimestamp(stamp, timezone.utc).isoformat() if stamp is not None else None
    except (ValueError, OverflowError, OSError):
        raise TraceError("Explorer confirmed address history contains an invalid block timestamp") from None
    return {"txid": txid.lower(), "block_height": height,
            "block_hash": block_hash.lower() if block_hash else None,
            "block_time": stamp, "date_utc": date, "observation_id": observation_id}


def inspect_address(api, address, max_pages=DEFAULT_HISTORY_PAGES):
    """Inspect one address using the caller's Esplora client, budget and Store.

    The caller owns the durable case Store and uses a new run ID for each
    refresh. Address responses are deduplicated within that run and never use
    the cross-run transaction cache. At most one statistics request plus
    ``max_pages`` distinct history endpoints are requested (HTTP retries and
    authentication also consume the existing client request/time budget).

    Counts describe the explorer's observed index, not an atomic chain snapshot.
    The earliest confirmed activity is only supplied after a complete history
    agrees with the statistics. Partial history retains its oldest *observed*
    transaction rather than claiming that this is the address's first use.
    """
    address = validate_address(address)
    if type(max_pages) is not int or max_pages < 1:
        raise TraceError("Address history page limit must be a positive integer")
    endpoint = "/address/" + address
    try:
        body, stats_oid = api.get(endpoint)
    except StopRun as error:
        reason = str(error) if str(error) in _STOP_REASONS else "lookup_limit"
        raise TraceError("Address statistics lookup stopped: " + reason) from None
    chain, mempool = _stats(body, address)
    observation = next(api.store.observations([stats_oid]))
    confirmed_unspent = chain["funded_txo_count"] - chain["spent_txo_count"]
    mempool_delta = mempool["funded_txo_count"] - mempool["spent_txo_count"]
    combined_unspent = confirmed_unspent + mempool_delta
    warnings = []
    counts_consistent = confirmed_unspent >= 0 and combined_unspent >= 0
    if not counts_consistent:
        warnings.append("Address statistics are inconsistent; negative derived output counts are unavailable.")
    result = {
        "schema_version": 1, "address": address, "observed_at": observation["fetched_at"],
        "source": api.base, "confirmed_tx_count": chain["tx_count"],
        "mempool_tx_count": mempool["tx_count"],
        "confirmed_unspent_output_count": confirmed_unspent if confirmed_unspent >= 0 else None,
        "mempool_unspent_output_delta": mempool_delta,
        "unspent_output_count": combined_unspent if counts_consistent else None,
        "output_counts_consistent": counts_consistent,
        "count_basis": "funded_txo_count minus spent_txo_count; combined count includes mempool delta",
        "output_scope": "All indexed outputs/assets at this address, not an L-BTC balance or only investigation UTXOs.",
        "history_pages": 0, "max_pages": max_pages, "history_transactions_seen": 0,
        "history_complete": False, "history_stop_reason": "page_limit",
        "first_confirmed_activity": None, "oldest_observed_confirmed_activity": None,
        "latest_confirmed_activity": None, "observation_ids": [stats_oid], "warnings": warnings,
        "timeframe_basis": "confirmed block order and block timestamps, not address creation time",
        "snapshot_note": "Statistics and history are separate observations and are not an atomic chain snapshot.",
    }
    # The index reports no confirmed use. Avoid a paid history scan with no
    # confirmed records to retrieve. Mempool-only use has no confirmed date.
    if chain["tx_count"] == 0:
        result.update(history_complete=True, history_stop_reason="no_confirmed_transactions",
                      completed_at=now())
        return result

    seen_txids, requested = set(), set()
    cursor = None
    consistent = True
    previous_height = None
    missing_timestamp = False
    for _ in range(max_pages):
        history_endpoint = endpoint + "/txs/chain" + ("/" + cursor if cursor else "")
        if history_endpoint in requested:
            consistent = False
            result["history_stop_reason"] = "repeated_history"
            break
        requested.add(history_endpoint)
        try:
            page, oid = api.get(history_endpoint)
        except StopRun as error:
            result["history_stop_reason"] = str(error) if str(error) in _STOP_REASONS else "lookup_limit"
            break
        if not isinstance(page, list) or len(page) > HISTORY_PAGE_SIZE:
            raise TraceError("Explorer address history has an invalid page")
        result["observation_ids"].append(oid)
        result["history_pages"] += 1
        # Validate every record before deriving this page's dates or cursor.
        activities = [_activity(transaction, oid) for transaction in page]
        additions = 0
        for activity in activities:
            txid = activity["txid"]
            if txid in seen_txids:
                consistent = False
                continue
            seen_txids.add(txid)
            additions += 1
            height = activity["block_height"]
            if previous_height is not None and height > previous_height:
                consistent = False
            previous_height = height
            missing_timestamp |= activity["block_time"] is None
            if result["latest_confirmed_activity"] is None:
                result["latest_confirmed_activity"] = activity
            oldest = result["oldest_observed_confirmed_activity"]
            if oldest is None or height < oldest["block_height"]:
                result["oldest_observed_confirmed_activity"] = activity
        result["history_transactions_seen"] = len(seen_txids)
        if len(seen_txids) > chain["tx_count"]:
            consistent = False
        if len(page) < HISTORY_PAGE_SIZE:
            consistent &= len(seen_txids) == chain["tx_count"]
            result["history_complete"] = consistent
            result["history_stop_reason"] = "complete" if consistent else "snapshot_inconsistent"
            break
        if not additions:
            consistent = False
            result["history_stop_reason"] = "repeated_history"
            break
        cursor = activities[-1]["txid"]

    if not consistent:
        warnings.append("Confirmed history and statistics may have changed during lookup; the first confirmed activity is unverified.")
        # In a malformed or changing order the first row need not be latest.
        result["latest_confirmed_activity"] = None
        result["history_complete"] = False
    if missing_timestamp:
        warnings.append("Some confirmed transactions have no block timestamp; their dates are unavailable.")
    if result["history_complete"]:
        result["first_confirmed_activity"] = result["oldest_observed_confirmed_activity"]
    result["completed_at"] = now()
    return result
