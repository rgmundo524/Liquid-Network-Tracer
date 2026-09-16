"""Dated address transaction counts, automatically hydrated at visual entry points.

GET /address/:address provides confirmed and mempool tx_count in one response.
No address-history scan, UTXO expansion or attribution follows from a count.
"""
import copy
import fcntl
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .address_activity import validate_address
from .common import StopRun, TraceError, canonical, digest, read_json, save_json

COUNT_GAP = 20
COUNT_HEIGHT = 28


def label(node):
    value = node.get("tx_count")
    return f"{value:,}" if type(value) is int and value >= 0 else "??"


def caption(node):
    """The bottom row of an address shape; never a separate board item."""
    if node.get("kind") == "address" and "tx_count" in node:
        return "TX: " + label(node)
    return None


def position(body, current=None):
    """Historical external-label geometry, retained only for old plan validation."""
    import math
    x, y = current if current is not None else (body['position'][axis] for axis in ('x', 'y'))
    width, height = (float(body['geometry'][axis]) for axis in ('width', 'height'))
    angle = math.radians(float(body.get('rotation', body['geometry'].get('rotation', 0))))
    extent = abs(width * math.sin(angle)) / 2 + abs(height * math.cos(angle)) / 2
    return x, y - extent - COUNT_GAP


def _valid(record, source, address):
    return (isinstance(record, dict) and record.get('source') == source
            and record.get('address') == address
            and all(type(record.get(k)) is int and record[k] >= 0
                    for k in ('confirmed_tx_count', 'mempool_tx_count'))
            and isinstance(record.get('observed_at'), str) and bool(record['observed_at']))


def addresses(state):
    result = set()
    for record in state['transactions'].values():
        transaction = record['data']
        outputs = list(transaction['vout'])
        outputs.extend(vin.get('prevout') or {} for vin in transaction['vin']
                       if not vin.get('is_pegin') and not vin.get('is_coinbase'))
        for output in outputs:
            address = output.get('scriptpubkey_address')
            if address:
                try:
                    result.add(validate_address(address))
                except TraceError:
                    continue  # A malformed provider address must not become an API path.
    return sorted(result)


def _cache(case, state):
    path = Path(case) / 'address-counts.json'
    if path.is_symlink():
        raise TraceError('Address count storage must not be a symbolic link')
    if not path.exists():
        return {}
    data = read_json(path)
    if (data.get('schema_version') != 1 or data.get('case_id') != state['case_id']
            or digest(canonical({k:v for k,v in data.items() if k != 'sha256'})) != data.get('sha256')):
        raise TraceError('Invalid address count cache; restore its last intact version')
    if data.get('source') != state['source']:
        return {}
    counts = data.get('counts')
    if not isinstance(counts, dict) or any(not _valid(v, state['source'], k) for k,v in counts.items()):
        raise TraceError('Invalid address transaction count observation')
    return counts


def apply_saved_counts(case, state):
    """Overlay cached counts and verified address reviews, without any API call."""
    from .address_review import _index, _read_activity
    counts = {a: copy.deepcopy(r) for a, r in state.get('address_tx_counts', {}).items()
              if _valid(r, state['source'], a)}
    for address, record in _cache(case, state).items():
        if address not in counts or record['observed_at'] >= counts[address]['observed_at']:
            counts[address] = copy.deepcopy(record)
    index = _index(case)
    for address in addresses(state):
        if address not in index['addresses']:
            continue
        record = _read_activity(case, address, index)
        if not _valid(record, state['source'], address):
            continue
        if address not in counts or record['observed_at'] >= counts[address]['observed_at']:
            counts[address] = {key: record[key] for key in ('address', 'source', 'observed_at',
                                                          'confirmed_tx_count', 'mempool_tx_count')}
            counts[address]['observation_ids'] = record.get('observation_ids', [])[:1]
    state['address_tx_counts'] = counts
    return counts


def annotate(graph, state):
    for node in graph['nodes']:
        if node['kind'] != 'address':
            continue
        address = node['details'].get('address')
        record = state.get('address_tx_counts', {}).get(address)
        node['tx_count'] = None
        if node['details'].get('network') == 'liquid' and _valid(record, state['source'], address):
            node['tx_count'] = record['confirmed_tx_count'] + record['mempool_tx_count']
            node['details']['tx_count_observation'] = copy.deepcopy(record)
        node['details']['tx_count_basis'] = 'Confirmed plus mempool transactions at last address statistics observation; not visible arrows.'


def _transaction_counts(body, address):
    """Read the explorer's two totals, not history pages or unrelated TXO fields."""
    if not isinstance(body, dict) or body.get("address") != address:
        raise TraceError("Address-count response does not match the requested address")
    values = []
    for field in ("chain_stats", "mempool_stats"):
        stats = body.get(field)
        value = stats.get("tx_count") if isinstance(stats, dict) else None
        if type(value) is not int or value < 0:
            raise TraceError("Address-count response is missing a valid transaction total")
        values.append(value)
    return values


def _progress(progress, phase, done, total):
    if progress is not None:
        try:
            progress({"phase": phase, "completed": done, "total": total})
        except Exception:
            pass  # Progress is advisory; never lose a successful observation.


def _failure_reason(error):
    # Do not expose provider messages, request credentials or arbitrary text.
    text = str(error)
    if "BLOCKSTREAM_CLIENT" in text or "authentication" in text or "HTTP 401" in text or "HTTP 403" in text:
        return "authentication_failed"
    if "Network request failed" in text:
        return "network_failed"
    if "fixture" in text.lower():
        return "fixture_unavailable"
    if "HTTP " in text or "retries exhausted" in text:
        return "http_failed"
    return "invalid_response"


def _collect_counts(case, state, wanted, *, max_requests, max_seconds, refresh=False,
                    progress=None, transport=None, fixture=None, best_effort=False):
    """One bounded, serialized cache update. Never acquires trace.lock.

    Callers may already own trace.lock (trace -> export -> Miro). A separate
    ancillary-cache lock avoids recursive flock deadlocks and lost updates.
    A visual job uses a separate, reported statistics budget, not trace hops.
    """
    from .api import ENTERPRISE, Esplora, Limits
    from .store import Store
    limits = Limits(max_hops=0, max_transactions=1, max_outpoints=1,
                    max_requests=max_requests, max_seconds=max_seconds)
    limits.validate()
    case = Path(case)
    wanted = sorted({validate_address(address) for address in wanted})
    lock_path = case / "address-counts.lock"
    if lock_path.is_symlink():
        raise TraceError("Address count lock must not be a symbolic link")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not best_effort:
                raise TraceError("An address-count lookup is already active for this investigation") from None
            counts = apply_saved_counts(case, state)
            known = sum(address in counts for address in wanted)
            _progress(progress, "address_counts_incomplete", known, len(wanted))
            return {"run_id": state["run_id"], "fetched": 0, "known": known, "total": len(wanted),
                    "remaining": len(wanted)-known, "failed": 0, "stop_reason": "lookup_in_progress",
                    "errors": [], "requests_this_lookup": 0,
                    "notice": "Another count lookup is active; using available saved counts without duplicate requests."}
        counts = apply_saved_counts(case, state)
        todo = [address for address in wanted if refresh or address not in counts]
        source = state["source"]
        fetched, examined, stop = 0, 0, None
        failures = []
        store, api = None, None
        _progress(progress, "address_counts", len(wanted)-len(todo), len(wanted))
        try:
            if todo:
                if source.startswith("fixture://") and not fixture:
                    if not best_effort:
                        raise TraceError("The saved synthetic fixture is required to fetch address counts")
                    stop = "fixture_unavailable"
                else:
                    base = source if not source.startswith("fixture://") else ENTERPRISE
                    auth = "blockstream" if urlsplit(base).hostname == "enterprise.blockstream.info" else "none"
                    options = {"transport": transport} if transport is not None else {}
                    fetch_options = state.get("fetch_options", {})
                    store = Store(case)
                    api = Esplora(store, "counts-"+uuid.uuid4().hex, limits, base=base, auth=auth,
                                  fixture=fixture, tx_cache_seconds=0, workers=1,
                                  advertised_rps=fetch_options.get("advertised_rps"),
                                  min_interval=fetch_options.get("min_interval"), **options)
                    if api.base != source:
                        raise TraceError("Address-count source does not match the saved run")
                    for address in todo:
                        try:
                            body, oid = api.get("/address/"+address)
                            confirmed, mempool = _transaction_counts(body, address)
                        except StopRun as error:
                            stop = str(error)
                            break
                        except TraceError as error:
                            if not best_effort:
                                raise
                            reason = _failure_reason(error)
                            failures.append({"address": address, "reason": reason})
                            examined += 1
                            _progress(progress, "address_counts", len(wanted)-len(todo)+examined, len(wanted))
                            # A global connection/authentication problem should not
                            # cause the same failing request for every address.
                            if reason in ("authentication_failed", "network_failed"):
                                stop = reason
                                break
                            continue
                        observation = next(store.observations([oid]))
                        counts[address] = {"address": address, "source": source,
                            "observed_at": observation["fetched_at"],
                            "confirmed_tx_count": confirmed, "mempool_tx_count": mempool,
                            "observation_ids": [oid]}
                        data = {"schema_version": 1, "case_id": state["case_id"], "source": source, "counts": counts}
                        data["sha256"] = digest(canonical(data))
                        save_json(case/"address-counts.json", data)
                        fetched += 1
                        examined += 1
                        _progress(progress, "address_counts", len(wanted)-len(todo)+examined, len(wanted))
            known = sum(address in counts for address in wanted)
            remaining = len(todo)-fetched
            reason = stop or ("lookup_failed" if failures else None)
            notice = (f"Address transaction counts: {known:,}/{len(wanted):,} available. "
                      + (f"Lookup incomplete ({reason}); unavailable counts remain ??." if remaining
                         else "Confirmed plus mempool totals, at their saved observation times."))
            _progress(progress, "address_counts_incomplete" if remaining else "address_counts_ready", known, len(wanted))
            return {"run_id": state["run_id"], "fetched": fetched, "known": known,
                    "total": len(wanted), "remaining": remaining, "failed": len(failures),
                    "stop_reason": reason, "errors": failures,
                    "requests_this_lookup": api.budget.requests if api is not None else 0,
                    "notice": notice}
        finally:
            if api is not None:
                api.close()
            if store is not None:
                store.close()


def ensure_counts(case, state, *, graph=None, progress=None, fixture=None, transport=None):
    """Automatically populate missing counts before a new chart/ordinary sync.

    This is an orchestration step, not a network side effect in build_graph or
    saved_graph. CSV, read-only pages, dry runs and immutable snapshot publication
    remain offline. Only addresses in the selected graph are queried when supplied.
    """
    from .investigations import read_case
    metadata = read_case(case)
    if metadata["case_id"] != state["case_id"]:
        raise TraceError("Address counts do not match this investigation")
    wanted = (addresses(state) if graph is None else
              {node["details"]["address"] for node in graph["nodes"]
               if node["kind"] == "address" and node["details"].get("network") == "liquid"})
    defaults = metadata.get("run_defaults", {})
    limits = state.get("limits", {})
    report = _collect_counts(case, state, wanted,
        max_requests=defaults.get("max_requests", limits.get("max_requests", 1000)),
        max_seconds=defaults.get("max_seconds", limits.get("max_seconds", 300)),
        fixture=fixture or metadata.get("fixture"), progress=progress,
        transport=transport, best_effort=True)
    if graph is not None:
        annotate(graph, state)
    return report


def ensure_graph_counts(case, graph, *, progress=None):
    """Hydrate an already verified graph without rebuilding or changing geometry."""
    from .cli import run_path
    namespace = graph["namespace"]
    state = read_json(run_path(case, graph["run_id"]) / "trace.json")
    if (state.get("case_id") != namespace["case_id"] or state.get("source") != namespace["source"]
            or state.get("run_id") != graph["run_id"]):
        raise TraceError("Address counts do not match the selected graph")
    # Preserve the selected run's API pacing settings as well as its source.
    return ensure_counts(case, state, graph=graph, progress=progress)


def fetch_counts(case, run_id="latest", max_requests=1000, max_seconds=300, refresh=False, progress=None, transport=None):
    """Explicit missing-count lookup/refresh; ordinary visual jobs call ensure_counts."""
    from .cli import resolve_latest, run_path, verify_export
    from .investigations import read_case
    case = Path(case)
    with (case/"trace.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("A trace or lookup is active; fetch counts after it finishes") from None
        selected = resolve_latest(case, run_id)
        archive = run_path(case, selected)
        verify_export(archive)
        state = read_json(archive/"trace.json")
        metadata = read_case(case)
        if state["case_id"] != metadata["case_id"] or state["run_id"] != selected:
            raise TraceError("Address counts do not match the selected investigation")
        return _collect_counts(case, state, addresses(state), max_requests=max_requests,
            max_seconds=max_seconds, refresh=refresh, progress=progress, transport=transport,
            fixture=metadata.get("fixture"))


def count_credentials_required(case, run_id="latest"):
    """Read-only launch preflight: unlock only for missing Enterprise statistics."""
    from .cli import resolve_latest, run_path, verify_export
    from .investigations import read_case
    case = Path(case)
    selected = resolve_latest(case, run_id)
    archive = run_path(case, selected)
    verify_export(archive)
    state = read_json(archive / "trace.json")
    if state.get("case_id") != read_case(case)["case_id"] or state.get("run_id") != selected:
        raise TraceError("Address counts do not match the selected investigation")
    if urlsplit(state["source"]).hostname != "enterprise.blockstream.info":
        return False
    counts = apply_saved_counts(case, state)
    return any(address not in counts for address in addresses(state))


def public_count_report(report):
    """Allow only numeric totals and known reasons into the browser result."""
    if not isinstance(report, dict):
        return None
    fields = ("fetched", "known", "total", "remaining", "failed", "requests_this_lookup")
    if any(type(report.get(key)) is not int or not 0 <= report[key] <= 2**53-1 for key in fields):
        return None
    value = {key: report[key] for key in fields}
    reason = report.get("stop_reason")
    allowed = {"authentication_failed", "network_failed", "fixture_unavailable", "http_failed",
               "invalid_response", "lookup_failed", "lookup_in_progress", "request_limit", "time_limit", "server_retry_later", "interrupted"}
    value["stop_reason"] = reason if isinstance(reason, str) and reason in allowed else None
    return value
