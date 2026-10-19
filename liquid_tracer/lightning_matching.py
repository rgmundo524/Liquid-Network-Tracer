"""Bounded lookup of indexed public Lightning channel funding and closure.

Mempool's TXID endpoint returns one ``{inputs, outputs}`` object per queried
transaction. Its input-map keys are placeholders, not Bitcoin input indexes;
every role is established again using the exact on-chain funding outpoint.
Separate bounded witness inspection reports contract candidates without channel attribution.
"""
from datetime import datetime
import math
import re
from tempfile import TemporaryDirectory

from .api import Esplora, Limits
from .common import HEX64, StopRun, TraceError
from .progress import report_progress
from .store import Store
from .lightning_inspection import inspect_transaction
from .lightning_swap import BOLTZ_API, validate_swap_id, verify_boltz_swap, unavailable
from .lightning_scripts import TEMPLATES

MEMPOOL_API = "https://mempool.space/api/v1"
BITCOIN_API = "https://blockstream.info/api"
MAX_CHANNELS = 100
INDEX_SAMPLE_COUNT = 3
MAX_TX_ITEMS = 100000
MAX_MONEY = 21000000 * 100000000
UINT64_MAX = (1 << 64) - 1
DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,19})\Z")
SHORT_ID = re.compile(r"(0|[1-9][0-9]{0,7})x(0|[1-9][0-9]{0,7})x(0|[1-9][0-9]{0,4})\Z")
PUBKEY = re.compile(r"0[23][0-9a-fA-F]{64}\Z")
SCRIPT = re.compile(r"(?:0020|5120)[0-9a-fA-F]{64}\Z")
NOTICE = (
    "This lookup checks Mempool's indexed public Lightning channels and verifies the reported "
    "funding outpoints against Bitcoin transaction data. No indexed channel found does not prove "
    "that a transaction is unrelated to Lightning: private, unannounced, newly opened, or unindexed "
    "channels may be absent. The TXID endpoint may return only one closed channel when a transaction "
    "closes several channels. Funding spends may be closures or splices; this check does not distinguish them. "
    "Revealed contract templates are candidates, not proof of a Lightning payment, service identity or ownership. "
    "Hidden Taproot scripts and unspent outputs remain inconclusive. Node identities are supplied by the index; "
    "this lookup does not independently verify Lightning gossip signatures."
)
CLASSIFICATIONS = {"funding": "indexed_channel_funding", "closure": "indexed_channel_closure"}


def _hash(value, label="Bitcoin transaction"):
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise TraceError(label + " must be a 64-character transaction hash")
    return value.lower()


def _integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise TraceError("Invalid " + label)
    return value


def _bounded_text(value, maximum, label):
    if (not isinstance(value, str) or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise TraceError("Invalid " + label)
    return value


def _timestamp(value):
    _bounded_text(value, 64, "observation time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise TraceError("Invalid observation time") from None
    if parsed.tzinfo is None:
        raise TraceError("Observation time must include a timezone")
    return value


def _pubkey(value):
    if not isinstance(value, str) or not PUBKEY.fullmatch(value):
        raise TraceError("Invalid Lightning node public key")
    # Reject compressed encodings that do not identify a secp256k1 point.
    x = int(value[2:], 16)
    prime = (1 << 256) - (1 << 32) - 977
    rhs = (pow(x, 3, prime) + 7) % prime
    if x >= prime or pow(rhs, (prime - 1) // 2, prime) != 1:
        raise TraceError("Invalid Lightning node public key")
    return value.lower()


def _channel_ids(identity, short_id, vout):
    if not isinstance(identity, str) or not DECIMAL.fullmatch(identity):
        raise TraceError("Invalid Lightning channel ID")
    value = int(identity)
    if not 1 <= value <= UINT64_MAX:
        raise TraceError("Invalid Lightning channel ID")
    if not isinstance(short_id, str) or not SHORT_ID.fullmatch(short_id):
        raise TraceError("Invalid Lightning short channel ID")
    height, index, output = (int(part) for part in short_id.split("x"))
    if height >= 1 << 24 or index >= 1 << 24 or output >= 1 << 16:
        raise TraceError("Invalid Lightning short channel ID")
    if (height << 40) | (index << 16) | output != value or output != vout:
        raise TraceError("Lightning channel ID does not agree with its funding output")
    return height


def _status(value):
    if not isinstance(value, dict) or type(value.get("confirmed")) is not bool:
        raise TraceError("Invalid Bitcoin confirmation status")
    if not value["confirmed"]:
        if any(key in value for key in ("block_height", "block_hash", "block_time")):
            raise TraceError("Unconfirmed Bitcoin transaction has block metadata")
        return {"confirmed": False}
    return {"confirmed": True,
            "block_height": _integer(value.get("block_height"), 0, (1 << 24) - 1, "Bitcoin block height"),
            "block_hash": _hash(value.get("block_hash"), "Bitcoin block"),
            "block_time": _integer(value.get("block_time"), 0, (1 << 53) - 1, "Bitcoin block time")}


def _script(value):
    if not isinstance(value, str) or not SCRIPT.fullmatch(value):
        raise TraceError("Indexed channel funding output is not a valid 32-byte witness program")
    return value.lower()


def _nodes(left, right):
    result = []
    for value in (left, right):
        if not isinstance(value, dict):
            raise TraceError("Invalid indexed Lightning node")
        key = _pubkey(value.get("public_key"))
        alias = value.get("alias")
        alias = _bounded_text("" if alias is None else alias, 512, "Lightning node alias")
        result.append({"public_key": key, "alias": alias,
                       "explorer_url": "https://mempool.space/lightning/node/" + key})
    if result[0]["public_key"] == result[1]["public_key"]:
        raise TraceError("Indexed Lightning channel has identical node identities")
    return result


def _channel(value, role, queried_txid, map_index):
    if not isinstance(value, dict):
        raise TraceError("Invalid indexed Lightning channel")
    funding_txid = _hash(value.get("transaction_id"), "Indexed channel funding transaction")
    vout = _integer(value.get("transaction_vout"), 0, 65535, "channel funding output index")
    height = _channel_ids(value.get("id"), value.get("short_id"), vout)
    capacity = _integer(value.get("capacity"), 1, MAX_MONEY, "channel capacity")
    closing_txid = value.get("closing_transaction_id")
    if closing_txid is not None:
        closing_txid = _hash(closing_txid, "Indexed channel closing transaction")
        if closing_txid == funding_txid:
            raise TraceError("Indexed channel funding and closing transactions are identical")
    if role == "funding" and (funding_txid != queried_txid or map_index != vout):
        raise TraceError("Indexed channel funding reference does not match the queried transaction")
    if role == "closure" and closing_txid != queried_txid:
        raise TraceError("Indexed channel closing reference does not match the queried transaction")
    return {"role": role, "classification": CLASSIFICATIONS[role], "channel_id": value["id"],
            "short_channel_id": value["short_id"], "funding_txid": funding_txid,
            "funding_vout": vout, "capacity_sat": capacity,
            "nodes": _nodes(value.get("node_left"), value.get("node_right")),
            "closing_txid": closing_txid, "bitcoin_input_index": None,
            "channel_url": "https://mempool.space/lightning/channel/" + value["id"],
            "_funding_height": height}


def _indexed_channels(data, queried_txid):
    if not isinstance(data, list) or len(data) != 1:
        raise TraceError("Invalid Lightning TXID response: expected one transaction result")
    item = data[0]
    if not isinstance(item, dict) or set(item) != {"inputs", "outputs"}:
        raise TraceError("Invalid Lightning TXID response maps")
    if any(not isinstance(item[key], dict) for key in ("inputs", "outputs")):
        raise TraceError("Invalid Lightning TXID response maps")
    if sum(len(item[key]) for key in ("inputs", "outputs")) > MAX_CHANNELS:
        raise TraceError("Lightning TXID response exceeds 100 channel entries")
    result = []
    ids, outpoints = set(), set()
    for name, role in (("outputs", "funding"), ("inputs", "closure")):
        for key, value in item[name].items():
            if (not isinstance(key, str) or not re.fullmatch(r"0|[1-9][0-9]{0,4}", key)
                    or int(key) > 65535):
                raise TraceError("Invalid Lightning TXID channel map index")
            channel = _channel(value, role, queried_txid, int(key))
            outpoint = (channel["funding_txid"], channel["funding_vout"])
            if channel["channel_id"] in ids or outpoint in outpoints:
                raise TraceError("Duplicate indexed Lightning channel or funding outpoint")
            ids.add(channel["channel_id"])
            outpoints.add(outpoint)
            result.append(channel)
    return result


def _channel_signature(channel):
    """Stable index identity, excluding mutable aliases and policy fields."""
    return (channel["role"], channel["channel_id"], channel["short_channel_id"],
            channel["funding_txid"], channel["funding_vout"], channel["capacity_sat"],
            channel["closing_txid"], tuple(sorted(node["public_key"] for node in channel["nodes"])))


def _index_sample_report(samples):
    rows = [{"result": "channels" if channels else "empty", "channel_count": len(channels),
             "observed_at": observed_at} for channels, observed_at in samples]
    positive = sum(bool(channels) for channels, _ in samples)
    empty = len(samples) - positive
    return {"attempts": len(samples), "positive": positive, "empty": empty,
            "consistent": not (positive and empty), "samples": rows}


def _reconcile_index_samples(samples):
    """Let any verified candidate survive empty replicas; reject conflicting positives."""
    positives = [(channels, observed_at) for channels, observed_at in samples if channels]
    if not positives:
        return [], None
    signatures = [{_channel_signature(channel) for channel in channels} for channels, _ in positives]
    if any(signature != signatures[0] for signature in signatures[1:]):
        raise TraceError("Mempool Lightning index returned conflicting channel identities")
    return positives[0]


def _transaction(value, txid):
    if not isinstance(value, dict) or _hash(value.get("txid")) != txid:
        raise TraceError("Bitcoin transaction response does not match the requested hash")
    for name in ("vin", "vout"):
        if not isinstance(value.get(name), list) or not 1 <= len(value[name]) <= MAX_TX_ITEMS:
            raise TraceError("Invalid or oversized Bitcoin transaction " + name)
    _status(value.get("status"))
    return value


def _verify_funding(channel, transaction):
    vout = channel["funding_vout"]
    if vout >= len(transaction["vout"]):
        raise TraceError("Indexed channel funding output is missing from the Bitcoin transaction")
    output = transaction["vout"][vout]
    if (not isinstance(output, dict)
            or _integer(output.get("value"), 0, MAX_MONEY, "Bitcoin funding output value") != channel["capacity_sat"]):
        raise TraceError("Indexed channel capacity does not match the Bitcoin funding output")
    channel["funding_scriptpubkey"] = _script(output.get("scriptpubkey"))
    status = _status(transaction["status"])
    if not status["confirmed"] or status["block_height"] != channel["_funding_height"]:
        raise TraceError("Indexed channel ID does not match the confirmed Bitcoin funding block")
    channel["funding_status"] = status


def _verify_closure(channel, transaction):
    closing_status = _status(transaction["status"])
    if (closing_status["confirmed"]
            and closing_status["block_height"] < channel["funding_status"]["block_height"]):
        raise TraceError("Indexed channel closure precedes its Bitcoin funding block")
    indexes = []
    for index, entry in enumerate(transaction["vin"]):
        if not isinstance(entry, dict):
            raise TraceError("Invalid Bitcoin transaction input")
        if entry.get("is_coinbase") is True:
            continue
        previous_txid = _hash(entry.get("txid"), "Bitcoin input transaction")
        previous_vout = _integer(entry.get("vout"), 0, 0xffffffff, "Bitcoin input output index")
        if previous_txid == channel["funding_txid"] and previous_vout == channel["funding_vout"]:
            indexes.append(index)
            previous = entry.get("prevout")
            if previous is not None:
                if (not isinstance(previous, dict)
                        or _integer(previous.get("value"), 0, MAX_MONEY, "Bitcoin previous output value") != channel["capacity_sat"]
                        or _script(previous.get("scriptpubkey")) != channel["funding_scriptpubkey"]):
                    raise TraceError("Bitcoin closing input disagrees with the checked funding output")
    if len(indexes) != 1:
        raise TraceError("Indexed channel closing transaction does not consume its exact funding outpoint")
    channel["bitcoin_input_index"] = indexes[0]


def _checked_limits(max_requests, max_seconds):
    _integer(max_requests, 1, 100, "Lightning request limit; choose 1 to 100")
    if (type(max_seconds) not in (int, float) or not math.isfinite(max_seconds)
            or not 0 < max_seconds <= 120):
        raise TraceError("Lightning time limit must be finite, greater than zero, and at most 120 seconds")
    return Limits(max_hops=0, max_transactions=0, max_requests=max_requests, max_seconds=max_seconds)


def lookup_lightning(txid, *, max_requests=12, max_seconds=30, mempool_fixture=None,
                     bitcoin_fixture=None, boltz_swap_id=None, boltz_fixture=None, progress=None):
    """Return independent index, witness and optional provider checks for this session."""
    txid = _hash(txid)
    limits = _checked_limits(max_requests, max_seconds)
    if (mempool_fixture is None) != (bitcoin_fixture is None):
        raise TraceError("Provide both Lightning and Bitcoin fixtures for an offline lookup")
    if boltz_swap_id is not None:
        boltz_swap_id = validate_swap_id(boltz_swap_id)
    if boltz_fixture is not None and (bitcoin_fixture is None or boltz_swap_id is None):
        raise TraceError("Boltz fixtures require Bitcoin/index fixtures and a swap ID")
    if bitcoin_fixture is not None and boltz_swap_id is not None and boltz_fixture is None:
        raise TraceError("Provide a Boltz fixture for offline swap verification")
    report = {"schema_version": 2, "kind": "lightning_lookup", "query": {"bitcoin_txid": txid},
              "status": "error", "classifications": [], "bitcoin": None, "matches": [],
              "sources": [], "notice": NOTICE, "stop_reason": None,
              "inspection": None, "provider_verification": None,
              "index_observations": _index_sample_report([]),
              "limits": {"max_requests": max_requests, "max_seconds": max_seconds, "requests_used": 0}}
    with TemporaryDirectory(prefix="liquid-lightning-") as directory:
        store = Store(directory)
        try:
            with Esplora(store, "lightning-bitcoin", limits, base=BITCOIN_API, auth="none",
                         fixture=bitcoin_fixture, tx_cache_seconds=0, blockchain="bitcoin") as bitcoin:
                transactions = {}

                def fetch(api, endpoint, provider, base):
                    data, oid = api.get(endpoint)
                    observed = next(store.observations([oid]))["fetched_at"]
                    if not any(row["provider"] == provider and row["endpoint"] == endpoint for row in report["sources"]):
                        report["sources"].append({"provider": provider, "endpoint": endpoint,
                                                  "url": base + endpoint, "observed_at": observed})
                    return data, observed

                def btc_transaction(identity):
                    if identity not in transactions:
                        raw, _ = fetch(bitcoin, "/tx/" + identity, "Blockstream Bitcoin", BITCOIN_API)
                        transactions[identity] = _transaction(raw, identity)
                    return transactions[identity]

                def query_transaction():
                    transaction = btc_transaction(txid)
                    report["bitcoin"] = {"txid": txid, "status": _status(transaction["status"]),
                                         "explorer_url": "https://mempool.space/tx/" + txid}
                    return transaction

                try:
                    report_progress(progress, "lightning_index")
                    samples = []
                    for attempt in range(INDEX_SAMPLE_COUNT):
                        with Esplora(store, "lightning-index-" + str(attempt + 1), limits,
                                     base=MEMPOOL_API, auth="none", fixture=mempool_fixture,
                                     tx_cache_seconds=0, blockchain="bitcoin") as index:
                            index.budget = bitcoin.budget
                            raw, indexed_at = fetch(index, "/lightning/channels/txids?txId[]=" + txid,
                                                    "Mempool Lightning", MEMPOOL_API)
                        samples.append((_indexed_channels(raw, txid), indexed_at))
                        report["index_observations"] = _index_sample_report(samples)
                    channels, indexed_at = _reconcile_index_samples(samples)
                    report_progress(progress, "lightning_bitcoin", 0, len(channels) + 1)
                    transaction = query_transaction()
                    verified = []
                    for position, channel in enumerate(channels):
                        bitcoin.budget.check()
                        _verify_funding(channel, btc_transaction(channel["funding_txid"]))
                        if channel["role"] == "closure":
                            _verify_closure(channel, transaction)
                        channel.pop("_funding_height")
                        channel["observed_at"] = indexed_at
                        verified.append(channel)
                        report_progress(progress, "lightning_bitcoin", position + 2, len(channels) + 1)
                    bitcoin.budget.check()
                    report["matches"] = verified
                    report["classifications"] = ([CLASSIFICATIONS[role] for role in ("funding", "closure")
                                                  if any(row["role"] == role for row in verified)]
                                                 or ["no_indexed_channel_found"])
                    report["status"] = "complete"
                except StopRun as error:
                    report.update(status="paused", stop_reason=str(error))
                except TraceError as error:
                    report.update(status="error", stop_reason="lookup_failed", error=str(error)[:1000])
                # A failed channel index must not suppress independent Bitcoin evidence.
                try:
                    transaction = query_transaction()
                    report_progress(progress, "lightning_scripts")
                    report["inspection"] = inspect_transaction(transaction, btc_transaction,
                        lambda: fetch(bitcoin, "/tx/" + txid + "/outspends", "Blockstream Bitcoin", BITCOIN_API)[0],
                        bitcoin.budget.check)
                except (StopRun, TraceError) as error:
                    report["inspection"] = {"status": "error", "findings": [], "checked_inputs": 0,
                        "checked_outputs": 0, "total_inputs": 0, "total_outputs": 0, "reason": str(error)[:1000]}
                if boltz_swap_id is not None:
                    try:
                        report_progress(progress, "lightning_swap")
                        with Esplora(store, "lightning-boltz", limits, base=BOLTZ_API, auth="none",
                                     fixture=boltz_fixture, tx_cache_seconds=0, blockchain="bitcoin") as boltz:
                            boltz.budget = bitcoin.budget
                            report["provider_verification"] = verify_boltz_swap(boltz_swap_id, txid,
                                lambda endpoint: fetch(boltz, endpoint, "Boltz", BOLTZ_API)[0])
                    except (StopRun, TraceError) as error:
                        report["provider_verification"] = unavailable(boltz_swap_id, str(error))
                report["limits"]["requests_used"] = bitcoin.budget.requests
                report_progress(progress, "lightning_ready")
        finally:
            store.close()
    return validate_lightning_report(report, txid)


MAX_LIGHTNING_BATCH = 100


def _lightning_result_kind(report):
    candidates = ((report.get("inspection") or {}).get("findings") or [])
    found = bool(report.get("matches") or any(row.get("status") == "candidate" for row in candidates)
                 or (report.get("provider_verification") or {}).get("status") == "verified")
    if found:
        return "matched"
    return "no_matches" if report.get("status") == "complete" else "incomplete"


def lookup_lightning_batch(txids, *, max_requests=12, max_seconds=30, mempool_fixture=None,
                           bitcoin_fixture=None, progress=None):
    """Check a bounded CSV batch while preserving one independently validated report per hash."""
    if not isinstance(txids, list) or not 1 <= len(txids) <= MAX_LIGHTNING_BATCH:
        raise TraceError("Choose 1 to 100 Bitcoin transaction hashes for Lightning Network lookup")
    identities = [_hash(value) for value in txids]
    if len(set(identities)) != len(identities):
        raise TraceError("Remove duplicate Bitcoin transaction hashes from the CSV")
    reports = []
    report_progress(progress, "lightning_batch", 0, len(identities))
    for position, txid in enumerate(identities):
        reports.append(lookup_lightning(txid, max_requests=max_requests, max_seconds=max_seconds,
            mempool_fixture=mempool_fixture, bitcoin_fixture=bitcoin_fixture, progress=None))
        report_progress(progress, "lightning_batch", position + 1, len(identities))
    result = {"schema_version": 1, "kind": "lightning_batch",
              "query": {"bitcoin_txids": identities}, "status": "complete", "reports": reports}
    return validate_lightning_batch_report(result, identities)


def validate_lightning_batch_report(report, txids):
    """Validate a bounded batch wrapper and every transaction-specific public report."""
    if not isinstance(txids, list) or not 1 <= len(txids) <= MAX_LIGHTNING_BATCH:
        raise TraceError("Invalid Lightning batch transaction identities")
    identities = [_hash(value) for value in txids]
    if len(set(identities)) != len(identities):
        raise TraceError("Invalid duplicate Lightning batch transaction identities")
    if (not isinstance(report, dict) or report.get("schema_version") != 1
            or report.get("kind") != "lightning_batch" or report.get("status") != "complete"
            or report.get("query") != {"bitcoin_txids": identities}):
        raise TraceError("Invalid Lightning batch report")
    rows = report.get("reports")
    if not isinstance(rows, list) or len(rows) != len(identities):
        raise TraceError("Incomplete Lightning batch report")
    clean = [validate_lightning_report(row, txid) for row, txid in zip(rows, identities)]
    kinds = [_lightning_result_kind(row) for row in clean]
    return {"schema_version": 1, "kind": "lightning_batch",
            "query": {"bitcoin_txids": identities}, "status": "complete", "reports": clean,
            "summary": {"total": len(clean), "matched": kinds.count("matched"),
                        "no_matches": kinds.count("no_matches"), "incomplete": kinds.count("incomplete")}}


def validate_lightning_report(report, txid):
    """Validate a worker result and return only the bounded public report fields."""
    txid = _hash(txid)
    if (not isinstance(report, dict) or type(report.get("schema_version")) is not int
            or report["schema_version"] not in (1, 2) or report.get("kind") != "lightning_lookup"
            or report.get("query") != {"bitcoin_txid": txid}):
        raise TraceError("Invalid Lightning lookup report or transaction identity")
    status = report.get("status")
    if status not in ("complete", "error", "paused", "cancelled"):
        raise TraceError("Invalid Lightning lookup report status")
    output = {"schema_version": report["schema_version"], "kind": "lightning_lookup", "query": {"bitcoin_txid": txid},
              "status": status, "notice": NOTICE, "matches": [], "sources": [], "bitcoin": None}
    index_observations = None
    if report["schema_version"] == 2:
        value = report.get("index_observations")
        if not isinstance(value, dict) or type(value.get("consistent")) is not bool:
            raise TraceError("Invalid Lightning index observations")
        samples = value.get("samples")
        if not isinstance(samples, list) or len(samples) > INDEX_SAMPLE_COUNT:
            raise TraceError("Invalid Lightning index observation samples")
        clean_samples = []
        for sample in samples:
            if not isinstance(sample, dict) or sample.get("result") not in ("channels", "empty"):
                raise TraceError("Invalid Lightning index observation")
            count = _integer(sample.get("channel_count"), 0, MAX_CHANNELS, "Lightning index channel count")
            if (sample["result"] == "empty") != (count == 0):
                raise TraceError("Invalid Lightning index observation count")
            clean_samples.append({"result": sample["result"], "channel_count": count,
                                  "observed_at": _timestamp(sample.get("observed_at"))})
        positive = sum(sample["result"] == "channels" for sample in clean_samples)
        empty = len(clean_samples) - positive
        if (value.get("attempts") != len(clean_samples) or value.get("positive") != positive
                or value.get("empty") != empty or value["consistent"] != (not (positive and empty))):
            raise TraceError("Invalid Lightning index observation summary")
        index_observations = {"attempts": len(clean_samples), "positive": positive, "empty": empty,
                              "consistent": value["consistent"], "samples": clean_samples}
        output["index_observations"] = index_observations
    limits = report.get("limits")
    if not isinstance(limits, dict):
        raise TraceError("Missing Lightning lookup limits")
    _checked_limits(limits.get("max_requests"), limits.get("max_seconds"))
    output["limits"] = {"max_requests": limits["max_requests"], "max_seconds": limits["max_seconds"],
                        "requests_used": _integer(limits.get("requests_used"), 0, limits["max_requests"], "Lightning requests used")}
    sources = report.get("sources")
    if not isinstance(sources, list) or len(sources) > 102:
        raise TraceError("Invalid Lightning lookup sources")
    seen = set()
    for source in sources:
        if not isinstance(source, dict):
            raise TraceError("Invalid Lightning source")
        provider, endpoint = source.get("provider"), source.get("endpoint")
        if provider == "Mempool Lightning" and endpoint == "/lightning/channels/txids?txId[]=" + txid:
            url = MEMPOOL_API + endpoint
        elif (provider == "Blockstream Bitcoin" and isinstance(endpoint, str)
              and re.fullmatch(r"/tx/[0-9a-f]{64}(?:/outspends)?", endpoint)):
            url = BITCOIN_API + endpoint
        elif (provider == "Boltz" and isinstance(endpoint, str)
              and re.fullmatch(r"/swap/(?:submarine/[A-Za-z0-9_-]{1,128}/transaction|[A-Za-z0-9_-]{1,128})", endpoint)):
            url = BOLTZ_API + endpoint
        else:
            raise TraceError("Invalid Lightning source endpoint")
        if source.get("url") != url or (provider, endpoint) in seen:
            raise TraceError("Invalid or duplicate Lightning source URL")
        seen.add((provider, endpoint))
        output["sources"].append({"provider": provider, "endpoint": endpoint, "url": url,
                                  "observed_at": _timestamp(source.get("observed_at"))})
    if len(sources) > output["limits"]["requests_used"]:
        raise TraceError("Lightning source count exceeds the reported request count")
    bitcoin = report.get("bitcoin")
    if bitcoin is not None:
        if (not isinstance(bitcoin, dict) or bitcoin.get("txid") != txid
                or bitcoin.get("explorer_url") != "https://mempool.space/tx/" + txid):
            raise TraceError("Invalid Lightning report Bitcoin transaction")
        output["bitcoin"] = {"txid": txid, "status": _status(bitcoin.get("status")),
                             "explorer_url": "https://mempool.space/tx/" + txid}
    matches = report.get("matches")
    if not isinstance(matches, list) or len(matches) > MAX_CHANNELS:
        raise TraceError("Invalid Lightning report matches")
    ids, outpoints = set(), set()
    for row in matches:
        if not isinstance(row, dict) or row.get("role") not in CLASSIFICATIONS:
            raise TraceError("Invalid Lightning report match")
        role = row["role"]
        nodes = row.get("nodes")
        if not isinstance(nodes, list) or len(nodes) != 2:
            raise TraceError("Invalid Lightning report node identities")
        channel = _channel({"transaction_id": row.get("funding_txid"),
                            "transaction_vout": row.get("funding_vout"), "id": row.get("channel_id"),
                            "short_id": row.get("short_channel_id"), "capacity": row.get("capacity_sat"),
                            "closing_transaction_id": row.get("closing_txid"),
                            "node_left": nodes[0], "node_right": nodes[1]}, role, txid, row.get("funding_vout"))
        if row.get("classification") != channel["classification"] or row.get("channel_url") != channel["channel_url"]:
            raise TraceError("Invalid Lightning report classification or channel URL")
        if any(node.get("explorer_url") != clean["explorer_url"] for node, clean in zip(nodes, channel["nodes"])):
            raise TraceError("Invalid Lightning report node URL")
        funding_status = _status(row.get("funding_status"))
        if not funding_status["confirmed"] or funding_status["block_height"] != channel.pop("_funding_height"):
            raise TraceError("Invalid Lightning report funding confirmation")
        channel.update(funding_status=funding_status, funding_scriptpubkey=_script(row.get("funding_scriptpubkey")),
                       observed_at=_timestamp(row.get("observed_at")))
        indexed_times = ({sample["observed_at"] for sample in index_observations["samples"]
                          if sample["result"] == "channels"} if index_observations else
                         {next((source["observed_at"] for source in output["sources"]
                                if source["provider"] == "Mempool Lightning"), None)})
        if channel["observed_at"] not in indexed_times:
            raise TraceError("Lightning match has no corresponding index observation")
        if role == "closure":
            channel["bitcoin_input_index"] = _integer(row.get("bitcoin_input_index"), 0, MAX_TX_ITEMS - 1, "Bitcoin closure input index")
        elif row.get("bitcoin_input_index") is not None:
            raise TraceError("Funding match has a closure input index")
        if (role == "funding" and (output["bitcoin"] is None or output["bitcoin"]["status"] != funding_status)):
            raise TraceError("Lightning funding match disagrees with the queried Bitcoin status")
        if (channel["channel_id"] in ids or (channel["funding_txid"], channel["funding_vout"]) in outpoints
                or ("Blockstream Bitcoin", "/tx/" + channel["funding_txid"]) not in seen):
            raise TraceError("Duplicate Lightning report channel or missing funding source")
        ids.add(channel["channel_id"])
        outpoints.add((channel["funding_txid"], channel["funding_vout"]))
        output["matches"].append(channel)
    expected = ([CLASSIFICATIONS[role] for role in ("funding", "closure")
                 if any(row["role"] == role for row in output["matches"])] or ["no_indexed_channel_found"])
    if status == "complete":
        if (output["bitcoin"] is None or report.get("classifications") != expected
                or ("Mempool Lightning", "/lightning/channels/txids?txId[]=" + txid) not in seen
                or ("Blockstream Bitcoin", "/tx/" + txid) not in seen
                or report.get("stop_reason") is not None or report.get("error") is not None):
            raise TraceError("Incomplete Lightning lookup cannot be reported as complete")
        if index_observations and (index_observations["attempts"] != INDEX_SAMPLE_COUNT
                or bool(output["matches"]) != bool(index_observations["positive"])
                or not output["matches"] and index_observations["empty"] != INDEX_SAMPLE_COUNT):
            raise TraceError("Complete Lightning lookup has incomplete index observations")
        output.update(classifications=expected, stop_reason=None)
    else:
        if matches or report.get("classifications") != []:
            raise TraceError("Incomplete Lightning lookup cannot assert channel classifications")
        output.update(classifications=[], stop_reason=_bounded_text(report.get("stop_reason"), 100, "Lightning stop reason"))
        if status == "error":
            output["error"] = _bounded_text(report.get("error"), 1000, "Lightning lookup error")
    if report["schema_version"] == 2:
        output["inspection"] = _validate_inspection(report.get("inspection"), txid, seen)
        output["provider_verification"] = _validate_provider(report.get("provider_verification"), txid, seen)
    return output


def _strings(value, count, size, label):
    if not isinstance(value, list) or len(value) > count:
        raise TraceError('Invalid ' + label)
    return [_bounded_text(item, size, label) for item in value]


def _validate_inspection(value, txid, sources):
    if not isinstance(value, dict) or value.get('status') not in ('complete', 'partial', 'error'):
        raise TraceError('Invalid Lightning witness inspection')
    result = {'status': value['status'], 'findings': []}
    for field in ('checked_inputs', 'checked_outputs', 'total_inputs', 'total_outputs'):
        result[field] = _integer(value.get(field), 0, MAX_TX_ITEMS, 'inspection ' + field)
    if (result['checked_inputs'] > min(result['total_inputs'], 64)
            or result['checked_outputs'] > min(result['total_outputs'], 32)):
        raise TraceError('Invalid Lightning inspection coverage counts')
    reason = value.get('reason')
    result['reason'] = None if reason is None else _bounded_text(reason, 1000, 'inspection reason')
    rows = value.get('findings')
    if not isinstance(rows, list) or len(rows) > 96:
        raise TraceError('Invalid Lightning inspection findings')
    identities = set()
    for row in rows:
        if not isinstance(row, dict) or row.get('role') not in ('input', 'output'):
            raise TraceError('Invalid Lightning inspection role')
        role = row['role']
        limit = result['total_inputs' if role == 'input' else 'total_outputs']
        index = _integer(row.get('index'), 0, limit - 1, 'inspection index')
        identity = (role, index)
        if identity in identities or index >= (64 if role == 'input' else 32):
            raise TraceError('Duplicate or unbounded Lightning inspection finding')
        identities.add(identity)
        clean = {'role': role, 'index': index, 'funding_txid': _hash(row.get('funding_txid')),
                 'funding_vout': _integer(row.get('funding_vout'), 0, 0xffffffff, 'inspection funding vout')}
        spending = row.get('spending_txid')
        clean['spending_txid'] = None if spending is None else _hash(spending)
        vin = row.get('spending_input_index')
        clean['spending_input_index'] = None if vin is None else _integer(vin, 0, MAX_TX_ITEMS - 1, 'inspection spend vin')
        if (spending is None) != (vin is None):
            raise TraceError('Incomplete Lightning inspection spending reference')
        if (role == 'input' and (clean['spending_txid'] != txid or vin != index)
                or role == 'output' and (clean['funding_txid'] != txid or clean['funding_vout'] != index)):
            raise TraceError('Lightning inspection does not belong to queried transaction')
        status = row.get('status')
        if status not in ('candidate', 'hidden', 'unsupported', 'unspent', 'unavailable'):
            raise TraceError('Invalid script inspection result')
        clean['status'] = status
        amount = row.get('value_sat')
        clean['value_sat'] = None if amount is None else _integer(amount, 0, MAX_MONEY, 'inspection output value')
        if status != 'unavailable' and amount is None:
            raise TraceError('Missing inspected output value')
        for field, length in (('script_type', 40), ('summary', 1000)):
            clean[field] = _bounded_text(row.get(field), length, 'inspection ' + field)
        clean['evidence'] = _strings(row.get('evidence'), 10, 1000, 'inspection evidence')
        clean['template'], clean['branch'] = row.get('template'), row.get('branch')
        clean['payment_hash'], clean['hash_algorithm'] = row.get('payment_hash'), row.get('hash_algorithm')
        if clean['branch'] not in ('claim', 'refund', 'unknown'):
            raise TraceError('Invalid contract witness branch')
        if status == 'candidate':
            if (clean['template'] not in TEMPLATES or clean['hash_algorithm'] != 'hash160'
                    or not isinstance(clean['payment_hash'], str) or not re.fullmatch('[0-9a-f]{40}', clean['payment_hash'])
                    or spending is None or not clean['evidence']):
                raise TraceError('Invalid swap-contract candidate')
        elif any(clean[key] is not None for key in ('template', 'payment_hash', 'hash_algorithm')) or clean['branch'] != 'unknown':
            raise TraceError('Non-candidate inspection asserts a swap contract')
        if status == 'unspent' and (role != 'output' or spending is not None):
            raise TraceError('Invalid unspent inspection result')
        if status != 'unavailable':
            required = [('Blockstream Bitcoin', '/tx/' + txid)]
            if role == 'output':
                required.append(('Blockstream Bitcoin', '/tx/' + txid + '/outspends'))
                if spending is not None:
                    required.append(('Blockstream Bitcoin', '/tx/' + clean['spending_txid']))
            if any(source not in sources for source in required):
                raise TraceError('Missing source for Lightning inspection finding')
        result['findings'].append(clean)
    if result['status'] == 'complete':
        if (result['checked_inputs'] != result['total_inputs'] or result['checked_outputs'] != result['total_outputs']
                or result['reason'] is not None or any(row['status'] == 'unavailable' for row in rows)
                or ('Blockstream Bitcoin', '/tx/' + txid + '/outspends') not in sources):
            raise TraceError('Incomplete witness inspection cannot be reported complete')
    elif not result['reason']:
        raise TraceError('Incomplete witness inspection needs a reason')
    return result


def _validate_provider(value, txid, sources):
    if value is None:
        return None
    if (not isinstance(value, dict) or value.get('provider') != 'Boltz' or value.get('swap_type') != 'submarine'
            or value.get('status') not in ('verified', 'unmatched', 'unavailable')):
        raise TraceError('Invalid Lightning provider verification')
    swap_id = validate_swap_id(value.get('swap_id'))
    result = {'provider': 'Boltz', 'swap_type': 'submarine', 'swap_id': swap_id, 'status': value['status'],
              'summary': _bounded_text(value.get('summary'), 1000, 'provider summary'),
              'evidence': _strings(value.get('evidence'), 10, 1000, 'provider evidence')}
    identity = value.get('transaction_id')
    result['transaction_id'] = None if identity is None else _hash(identity)
    state = value.get('provider_status')
    result['provider_status'] = None if state is None else _bounded_text(state, 100, 'provider status')
    if value['status'] != 'unavailable':
        if (identity is None or ('Boltz', '/swap/submarine/' + swap_id + '/transaction') not in sources
                or (value['status'] == 'verified') != (identity == txid)):
            raise TraceError('Unverified provider transaction association')
    elif identity is not None:
        raise TraceError('Unavailable provider lookup asserts a transaction')
    if state is not None and ('Boltz', '/swap/' + swap_id) not in sources:
        raise TraceError('Provider status has no matching source')
    return result
