"""Dated address transaction counts, fetched explicitly and rendered offline.

GET /address/:address provides confirmed and mempool tx_count in one response.
No address-history scan, UTXO expansion or attribution follows from a count.
"""
import copy
import fcntl
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .address_activity import _stats, validate_address
from .common import StopRun, TraceError, canonical, digest, read_json, save_json

COUNT_GAP = 20
COUNT_HEIGHT = 28


def label(node):
    value = node.get("tx_count")
    return f"{value:,}" if type(value) is int and value >= 0 else "??"


def position(body, current=None):
    """Center a count just above its host, including live Miro rotations."""
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


def fetch_counts(case, run_id='latest', max_requests=1000, max_seconds=300, refresh=False, progress=None, transport=None):
    """Resume fetching missing counts in one bounded batch, checkpointing each response."""
    from .api import ENTERPRISE, Esplora, Limits
    from .cli import resolve_latest, run_path, verify_export
    from .investigations import read_case
    from .store import Store
    case = Path(case)
    limits = Limits(max_hops=0, max_transactions=1, max_outpoints=1,
                    max_requests=max_requests, max_seconds=max_seconds)
    limits.validate()
    with (case/'trace.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError('A trace or lookup is active; fetch counts after it finishes') from None
        selected = resolve_latest(case, run_id)
        archive = run_path(case, selected)
        verify_export(archive)
        state = read_json(archive/'trace.json')
        metadata = read_case(case)
        if state['case_id'] != metadata['case_id'] or state['run_id'] != selected:
            raise TraceError('Address counts do not match the selected investigation')
        counts = apply_saved_counts(case, state)
        wanted = addresses(state)
        todo = [address for address in wanted if refresh or address not in counts]
        source = state['source']
        fixture = metadata.get('fixture')
        if todo and source.startswith('fixture://') and not fixture:
            raise TraceError('The saved synthetic fixture is required to fetch address counts')
        base = source if not source.startswith('fixture://') else ENTERPRISE
        auth = 'blockstream' if urlsplit(base).hostname == 'enterprise.blockstream.info' else 'none'
        options = {'transport': transport} if transport is not None else {}
        store = Store(case)
        api = None
        fetched, stop = 0, None
        try:
            if todo:
                api = Esplora(store, 'counts-'+uuid.uuid4().hex, limits, base=base, auth=auth,
                              fixture=fixture, tx_cache_seconds=0, workers=1, **options)
                if api.base != source:
                    raise TraceError('Address-count source does not match the saved run')
                for address in todo:
                    try:
                        body, oid = api.get('/address/'+address)
                    except StopRun as exc:
                        stop = str(exc)
                        break
                    chain, mempool = _stats(body, address)
                    observation = next(store.observations([oid]))
                    counts[address] = {'address':address, 'source':source, 'observed_at':observation['fetched_at'],
                                       'confirmed_tx_count':chain['tx_count'], 'mempool_tx_count':mempool['tx_count'],
                                       'observation_ids':[oid]}
                    data = {'schema_version':1, 'case_id':state['case_id'], 'source':source, 'counts':counts}
                    data['sha256'] = digest(canonical(data))
                    save_json(case/'address-counts.json', data)
                    fetched += 1
                    if progress:
                        progress({'phase':'address_counts', 'completed':fetched, 'total':len(todo)})
            return {'run_id':selected, 'fetched':fetched, 'known':sum(a in counts for a in wanted),
                    'total':len(wanted), 'remaining':len(todo)-fetched, 'stop_reason':stop,
                    'requests_this_lookup':api.budget.requests if api is not None else 0,
                    'notice':'Dated confirmed plus mempool counts saved. Regenerate previews or sync Miro to display them.'}
        finally:
            if api is not None:
                api.close()
            store.close()
