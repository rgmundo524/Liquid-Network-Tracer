"""Saved address reviews alongside, never inside, archived tracing runs."""

import fcntl
import json
import math
import os
import re
import uuid
from collections import Counter
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from .address_activity import inspect_address, validate_address
from .api import ENTERPRISE, Esplora, Limits
from .common import TraceError, canonical, digest, read_json, save_json
from .investigations import read_case
from .services import load_services
from .store import Store


def _directory(case):
    directory = Path(case) / "address-reviews"
    if directory.is_symlink():
        raise TraceError("Address review storage must not be a symlink")
    return directory


def _index(case):
    metadata = read_case(case)
    path = _directory(case) / "index.json"
    if path.is_symlink():
        raise TraceError("Address review index must not be a symlink")
    if not path.exists():
        return {"schema_version": 1, "case_id": metadata["case_id"], "addresses": {}}
    data = read_json(path)
    if (not isinstance(data, dict) or data.get("schema_version") != 1
            or data.get("case_id") != metadata["case_id"] or not isinstance(data.get("addresses"), dict)):
        raise TraceError("Invalid address review index; restore its last intact version")
    for address, entry in data["addresses"].items():
        if (validate_address(address) != address or not isinstance(entry, dict)
                or not isinstance(entry.get("inspection_id"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", entry["inspection_id"])
                or not isinstance(entry.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
            raise TraceError("Invalid address review index entry")
    return data


def _read_activity(case, address, index):
    entry = index["addresses"].get(address)
    if entry is None:
        return None
    path = _directory(case) / (entry["inspection_id"] + ".json")
    if path.is_symlink():
        raise TraceError("Address review report must not be a symlink")
    raw = path.read_bytes()
    if digest(raw) != entry["sha256"]:
        raise TraceError("Address review checksum failed; restore the original report")
    result = json.loads(raw)
    if (not isinstance(result, dict) or result.get("case_id") != index["case_id"]
            or result.get("address") != address or result.get("inspection_id") != entry["inspection_id"]
            or result.get("schema_version") != 1):
        raise TraceError("Address review identity does not match this investigation")
    return result


def saved_activity(case, address, run_id="latest"):
    """Return a checksum-verified observation summary without any API request."""
    address = validate_address(address)
    result = _read_activity(case, address, _index(case))
    if result is None:
        return None
    _, _, source = _run_catalog(case, run_id)
    return None if source and result.get("source") != source else result


def _archive(case, run_id):
    from .cli import resolve_latest, run_path
    metadata = read_case(case)
    if run_id == "latest" and not metadata.get("latest_run"):
        return None, None
    selected = resolve_latest(case, run_id)
    path = run_path(case, selected)
    for candidate in (Path(case) / "runs", path, path / "trace.json", path / "SHA256SUMS"):
        if candidate.is_symlink():
            raise TraceError("Address review requires ordinary saved run files")
    return selected, path


@lru_cache(maxsize=2)
def _catalog(archive_text, identity, selected, signature):
    """Cache only a compact address catalog, not the full large investigation.

    Trace and manifest file stats key the cache. On change, reverify the archive
    before reading it. Service decisions and activity reports are never cached.
    """
    from .cli import verify_export
    archive = Path(archive_text)
    verify_export(archive)
    state = read_json(archive / "trace.json")
    if state.get("run_id") != selected or state.get("case_id") != identity:
        raise TraceError("Saved run does not match this investigation")
    counts, seen = Counter(), set()
    for txid, record in state["transactions"].items():
        transaction = record["data"]
        outputs = [(f"{txid}:{index}", output) for index, output in enumerate(transaction["vout"])]
        outputs.extend((f"{vin.get('txid')}:{vin.get('vout')}", vin.get("prevout") or {})
                       for vin in transaction["vin"] if not vin.get("is_pegin") and not vin.get("is_coinbase"))
        for outpoint, output in outputs:
            address = output.get("scriptpubkey_address")
            if not address:
                continue
            address = validate_address(address)
            key = (address, outpoint)
            if key not in seen:
                seen.add(key)
                counts[address] += 1
    return dict(counts), state["source"]


def _run_catalog(case, run_id):
    case = Path(case).resolve()
    metadata = read_case(case)
    selected, archive = _archive(case, run_id)
    if archive is None:
        return selected, {}, None
    signature = tuple((path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_ctime_ns)
                      for path in (archive / "trace.json", archive / "SHA256SUMS"))
    counts, source = _catalog(str(archive), metadata["case_id"], selected, signature)
    return selected, counts, source


def list_addresses(case, run_id="latest", query="", offset=0, limit=25, suspected_only=False):
    if (not isinstance(query, str) or len(query) > 256
            or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 100
            or type(suspected_only) is not bool):
        raise TraceError("Use a bounded address search and a page size from 1 to 100")
    case = Path(case).resolve()
    selected, counts, source = _run_catalog(case, run_id)
    services, index = load_services(case), _index(case)
    addresses = set(counts) | set(services["rules"]) | set(index["addresses"])
    needle = query.strip().casefold()
    addresses = sorted(address for address in addresses
                       if (not suspected_only or services["rules"].get(address, {}).get("enabled"))
                       and (not needle or needle in address.casefold()
                            or needle in services["rules"].get(address, {}).get("name", "").casefold()))
    rows = []
    for address in addresses[offset:offset + limit]:
        activity = _read_activity(case, address, index)
        # A report from a different API source is retained locally but must not
        # be presented as an observation of this selected saved graph.
        if activity and source and activity.get("source") != source:
            activity = None
        rows.append({"address": address, "run_output_count": counts.get(address, 0),
                     "service": services["rules"].get(address), "activity": activity})
    return {"run_id": selected, "rows": rows, "total": len(addresses), "offset": offset, "limit": limit}


def inspect_case_address(case, address, *, run_id="latest", max_pages=5, max_requests=10, max_seconds=60, transport=None):
    """Refresh one address under explicit budgets and the case's trace lock."""
    case, address = Path(case), validate_address(address)
    if (type(max_pages) is not int or max_pages < 1 or type(max_requests) is not int or max_requests < 1
            or isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float))
            or not math.isfinite(max_seconds) or max_seconds <= 0):
        raise TraceError("Address review page, request and time limits must be positive")
    metadata = read_case(case)
    with (case / "trace.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("Another trace or address lookup is running in this case") from None
        metadata = read_case(case)
        selected, archive = _archive(case, run_id)
        source = None
        if archive:
            from .cli import verify_export
            verify_export(archive)
            state = read_json(archive / "trace.json")
            if state.get("case_id") != metadata["case_id"] or state.get("run_id") != selected:
                raise TraceError("Saved trace does not match this investigation")
            source = state["source"]
        fixture = metadata.get("fixture")
        if fixture and not Path(fixture).is_file():
            raise TraceError("The saved synthetic fixture is unavailable")
        if source and source.startswith("fixture://") and not fixture:
            raise TraceError("The original synthetic fixture is required for this address review")
        base = source if source and not source.startswith("fixture://") else ENTERPRISE
        auth = "blockstream" if urlsplit(base).hostname == "enterprise.blockstream.info" else "none"
        limits = Limits(max_hops=0, max_transactions=1, max_outpoints=1,
                        max_requests=max_requests, max_seconds=max_seconds)
        inspection_id = uuid.uuid4().hex
        store, api = Store(case), None
        try:
            options = {"transport": transport} if transport is not None else {}
            api = Esplora(store, "address-" + inspection_id, limits, base=base, auth=auth,
                          fixture=fixture, tx_cache_seconds=0, workers=1, **options)
            if source and api.base != source:
                raise TraceError("Address review source does not match the investigation's saved source")
            # Validate the existing index before spending any API requests.
            index = _index(case)
            result = inspect_address(api, address, max_pages=max_pages)
            result.update(case_id=metadata["case_id"], inspection_id=inspection_id,
                          requests_this_lookup=api.budget.requests)
            directory = _directory(case)
            directory.mkdir(parents=True, exist_ok=True)
            raw = canonical(result) + b"\n"
            path = directory / (inspection_id + ".json")
            with path.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            index["addresses"][address] = {"inspection_id": inspection_id, "sha256": digest(raw)}
            save_json(directory / "index.json", index)
            return result
        finally:
            if api is not None:
                api.close()
            store.close()
