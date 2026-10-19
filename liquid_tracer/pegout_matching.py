"""Bounded peg-out candidate discovery in either direction.

Esplora exposes the Bitcoin destination script and public amount on a Liquid
peg-out, but does not expose a Bitcoin payout's Liquid request reference. Script,
amount and time agreement is therefore a candidate, never an exact linkage.
"""
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import math
from pathlib import Path
import sqlite3
import uuid

from .api import Esplora, Limits
from .common import HEX64, LBTC, StopRun, TraceError, canonical, digest, now, read_json, save_json
from .inspection import transaction_outputs
from .networks import default_api
from .pegin_matching import FILES, HISTORY, REPORT_ID, _csv, read_report, _hash, _history_page, _index, _observation, _ordinary, _status, query
from .pegin_window import WindowDiscovery
from .progress import report_progress
from .store import Store

BITCOIN_GENESIS = '000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f'
NOTICE = (
    'Peg-out results are candidates based on equal Bitcoin destination scripts and public amounts '
    'within the selected block-time window. Esplora does not provide an exact Liquid request '
    'reference in the Bitcoin payout. Even a single candidate does not prove the payout identity, '
    'ownership, or allocation of funds. Multiple candidates remain ambiguous. No candidate in a '
    'bounded search does not prove that a peg-out was never paid. Mempool transactions are not searched.')


def _script(value):
    if (not isinstance(value, str) or not value or len(value) % 2 or len(value) > 20000
            or any(char not in '0123456789abcdefABCDEF' for char in value)):
        raise TraceError('Explorer response contains an invalid destination script')
    return value.lower()


def pegout_outputs(transaction):
    """Eligible mainnet peg-out requests indexed by Liquid vout, for matching/UI."""
    if not isinstance(transaction, dict) or not isinstance(transaction.get('vout'), list):
        raise TraceError('Liquid response has no valid output list')
    found = {}
    for number, output in enumerate(transaction['vout']):
        if not isinstance(output, dict):
            raise TraceError('Liquid response has an invalid output')
        pegout = output.get('pegout')
        if pegout is None:
            continue
        if not isinstance(pegout, dict):
            raise TraceError('Liquid response has invalid peg-out metadata')
        genesis = _hash(pegout.get('genesis_hash'), 'Peg-out destination genesis')
        if genesis != BITCOIN_GENESIS or output.get('asset') != LBTC:
            continue
        value = output.get('value')
        if type(value) is not int or not 0 < value <= 21000000 * 100000000:
            raise TraceError('Liquid peg-out has no valid explicit L-BTC amount')
        script = _script(pegout.get('scriptpubkey'))
        address = pegout.get('scriptpubkey_address')
        if address is not None and not isinstance(address, str):
            raise TraceError('Liquid peg-out has an invalid Bitcoin destination address')
        found[number] = dict(vout=number, scriptpubkey=script, value=value, address=address)
    return found


def pegout_query(txids, *, source_network='liquid', vouts=None, max_pages=100,
                  start_minutes=None, end_minutes=None):
    single = isinstance(txids, str)
    values = [txids] if single else txids
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 500:
        raise TraceError('Choose from 1 to 500 source transaction hashes')
    if source_network not in ('bitcoin', 'liquid'):
        raise TraceError('Choose Bitcoin or Liquid as the source network')
    values = list(dict.fromkeys(_hash(value, 'Source transaction') for value in values))
    if start_minutes is None and end_minutes is None:
        start_minutes, end_minutes = (-5, 4320) if source_network == 'liquid' else (-4320, 5)
    validated = query(values[0], vouts=vouts, max_pages=max_pages,
                      start_minutes=start_minutes, end_minutes=end_minutes)
    if len(values) > 1 and validated['bitcoin_vouts']:
        raise TraceError('Choose individual outputs only for a single transaction')
    return dict(bridge='pegout', network=source_network, mode='single' if single else 'batch',
                txid=values[0] if single else None, txids=values, vouts=validated['bitcoin_vouts'],
                start_minutes=validated['start_minutes'], end_minutes=validated['end_minutes'],
                max_pages=max_pages)


@contextmanager
def _session(workspace, spec, *, max_requests, max_seconds, public, bitcoin_fixture, liquid_fixture,
             report_root, report_id):
    if type(max_requests) is not int or not 1 <= max_requests <= 10000:
        raise TraceError('Bridge matching requires a request limit from 1 to 10000')
    if (type(max_seconds) not in (int, float) or not math.isfinite(max_seconds)
            or not 0 < max_seconds <= 3600):
        raise TraceError('Bridge matching requires a time limit up to 3600 seconds')
    if type(public) is not bool:
        raise TraceError('Public API selection must be true or false')
    if bool(bitcoin_fixture) != bool(liquid_fixture):
        raise TraceError('Provide both Bitcoin and Liquid fixtures')
    workspace = _ordinary(Path(workspace).expanduser()).resolve()
    owner = _ordinary(Path(report_root)) if report_root is not None else workspace / '.pegin-matching'
    if report_id is not None and (not isinstance(report_id, str) or not REPORT_ID.fullmatch(report_id)):
        raise TraceError('Invalid bridge result identifier')
    identity = report_id or uuid.uuid4().hex
    if _ordinary(owner / 'pegin-matches' / identity).exists():
        raise TraceError('Bridge result identifier already exists')
    bases = [default_api(chain).replace('enterprise.blockstream.info', 'blockstream.info')
             if public else default_api(chain) for chain in ('bitcoin', 'liquid')]
    sources = [('fixture://' + digest(canonical(read_json(path)))) if path else base
               for path, base in zip((bitcoin_fixture, liquid_fixture), bases)]
    root = _ordinary(workspace / '.pegout-index' / digest(canonical(sources))[:24])
    result = dict(schema_version=1, id=identity, case_id=None, scope='workspace',
                  created_at=now(), query={**spec, 'public': public}, networks='bitcoin-mainnet/liquid-mainnet',
                  sources=dict(zip(('bitcoin', 'liquid'), sources)), status='searching', stop_reason=None,
                  matches=[], results=[], bitcoin=None, bitcoins={}, liquids={}, liquid_outputs={},
                  coverage={}, notice=NOTICE)
    evidence = {'bitcoin': {}, 'liquid': {}}
    limits = Limits(max_hops=0, max_requests=max_requests, max_seconds=max_seconds)
    limits.validate()
    # Separate peg-out coverage is essential: older peg-in coverage never indexed
    # peg-out destination scripts. The inherited claims table remains unused.
    with _index(root) as (db, state):
        db.executescript('''
            CREATE TABLE IF NOT EXISTS pegout_requests (
                liquid_txid TEXT NOT NULL, vout INTEGER NOT NULL, script TEXT NOT NULL,
                value INTEGER NOT NULL, timestamp INTEGER NOT NULL,
                PRIMARY KEY(liquid_txid,vout));
            CREATE INDEX IF NOT EXISTS pegout_destination ON pegout_requests(script,value,timestamp);
        ''')
        store = Store(root)
        bitcoin_headers = sqlite3.connect(':memory:')
        try:
            with Esplora(store, result['id'], limits, base=bases[0], auth='none' if public else 'blockstream',
                         fixture=bitcoin_fixture, blockchain='bitcoin', tx_cache_seconds=0) as btc, \
                 Esplora(store, result['id'], limits, base=bases[1], auth='none' if public else 'blockstream',
                         fixture=liquid_fixture, blockchain='liquid', tx_cache_seconds=0) as liquid:
                liquid.budget = btc.budget
                yield owner, result, evidence, dict(db=db, state=state, store=store, bitcoin=btc, liquid=liquid,
                    bitcoin_headers=bitcoin_headers, pages=0, max_pages=spec['max_pages'])
        finally:
            bitcoin_headers.close()
            store.close()


class PegoutWindow(WindowDiscovery):
    """Use proven bounded Liquid discovery, with a separate destination index."""
    def _transactions(self, data, **options):
        ids, rows, heights = super()._transactions(data, **options)
        hints = []
        for transaction in data:
            for vout, output in pegout_outputs(transaction).items():
                hints.append((transaction['txid'], vout, output['scriptpubkey'], output['value'],
                              transaction['status']['block_time']))
        # These remain hints. Candidate transactions are fetched and rechecked.
        self.db.executemany('INSERT OR REPLACE INTO pegout_requests VALUES(?,?,?,?,?)', hints)
        return ids, rows, heights


def _fetch(context, result, evidence, network, txid):
    transaction, oid = context[network].get('/tx/' + txid)
    inspection = transaction_outputs(txid, transaction, blockchain=network)
    inspection['status'] = _status(transaction)
    inspection['observation_id'] = oid
    result['bitcoins' if network == 'bitcoin' else 'liquids'][txid] = inspection
    evidence[network][txid] = _observation(context['store'], oid)
    evidence.setdefault(network + '_checks', {})[str(oid)] = evidence[network][txid]
    if network == 'liquid':
        result['liquid_outputs'][txid] = inspection['outputs']
    return transaction, inspection


def _selected(transaction, network, vouts):
    inspection = transaction_outputs(transaction['txid'], transaction, blockchain=network)
    if any(index >= len(inspection['outputs']) for index in vouts):
        raise TraceError('Selected output is outside the source transaction')
    if network == 'liquid':
        eligible = pegout_outputs(transaction)
    else:
        eligible = {}
        for output in inspection['outputs']:
            if output['selectable']:
                script = _script(transaction['vout'][output['vout']]['scriptpubkey'])
                value = output['value']
                if type(value) is not int or not 0 <= value <= 21000000 * 100000000:
                    raise TraceError('Bitcoin output has no valid explicit amount')
                eligible[output['vout']] = {**output, 'scriptpubkey': script}
    if vouts and not set(vouts) <= eligible.keys():
        raise TraceError('A selected output is not eligible for this bridge lookup')
    return {index: output for index, output in eligible.items() if not vouts or index in vouts}


def _candidate(result, bitcoin, btc_number, liquid, liquid_number):
    out = bitcoin['vout'][btc_number]
    request = pegout_outputs(liquid)[liquid_number]
    btc_status, liq_status = _status(bitcoin), _status(liquid)
    key = (bitcoin['txid'], btc_number, liquid['txid'], liquid_number)
    if any((item['bitcoin_txid'], item['bitcoin_vout'], item['liquid_txid'], item['liquid_vout']) == key
           for item in result['matches']):
        return
    result['matches'].append(dict(bitcoin_txid=bitcoin['txid'], bitcoin_vout=btc_number,
        bitcoin_outpoint=f"{bitcoin['txid']}:{btc_number}",
        bitcoin_address=out.get('scriptpubkey_address') or request['address'],
        bitcoin_value_sats=out['value'], bitcoin_status=btc_status,
        liquid_txid=liquid['txid'], liquid_vout=liquid_number, liquid_status=liq_status,
        liquid_outpoint=f"{liquid['txid']}:{liquid_number}", destination_script=request['scriptpubkey'],
        bitcoin_observation_id=result['bitcoins'][bitcoin['txid']]['observation_id'],
        liquid_observation_id=result['liquids'][liquid['txid']]['observation_id'],
        verification='script_amount_time_candidate', status='candidate',
        elapsed_seconds=btc_status['block_time'] - liq_status['block_time']))


def _validate_history(transaction):
    if not isinstance(transaction, dict) or not isinstance(transaction.get('vin'), list):
        raise TraceError('Liquid history has an invalid transaction')
    _hash(transaction.get('txid'), 'Liquid history transaction')
    pegout_outputs(transaction)
    return []


def _window(context, spec, status, progress, *, bitcoin=False):
    cls = WindowDiscovery if bitcoin else PegoutWindow
    db = context['bitcoin_headers'] if bitcoin else context['db']
    discovery = cls(context['bitcoin' if bitcoin else 'liquid'], db, context['state'], HISTORY,
                    _history_page, _validate_history, _status, progress,
                    context['max_pages'] - context['pages'], status,
                    spec['start_minutes'] / 60, spec['end_minutes'] / 60)
    if bitcoin:
        discovery.window['boundary_note'] = discovery.window['boundary_note'].replace('Liquid', 'Bitcoin')
    return discovery


def _liquid_source(context, spec, result, evidence, transaction, outputs, row, progress):
    discovery = _window(context, spec, transaction['status'], progress, bitcoin=True)
    row['coverage']['window'] = discovery.window
    discovery.prepare()
    window = discovery.window
    low = window['start_height']
    row['coverage']['destination_scripts_searched'] = 0
    by_script = {}
    for number, output in outputs.items():
        by_script.setdefault(output['scriptpubkey'], []).append((number, output))
    checked = {}
    histories = row['coverage']['bitcoin_histories'] = {}
    previous = context.get('previous_report')
    old_row = next((item for item in (previous or {}).get('results', [])
                    if item.get('source_txid') == row['source_txid']), {})
    old_window = old_row.get('coverage', {}).get('window', {})
    # A cursor is valid only for the same historical range on the same Bitcoin
    # chain. If the old range extended into the future, start again at the head
    # so newly confirmed transactions cannot be omitted.
    compatible = (old_window.get('end_not_yet_covered_by_chain') is False
        and all(old_window.get(key) == window[key] for key in
                ('start_time', 'end_time', 'start_height', 'end_height'))
        and isinstance(old_window.get('snapshot_hash'), str)
        and HEX64.fullmatch(old_window['snapshot_hash']))
    if compatible:
        old_chain, _ = _history_page(context['bitcoin'], '/block/' + old_window['snapshot_hash'] + '/status')
        if not isinstance(old_chain, dict) or type(old_chain.get('in_best_chain')) is not bool:
            raise TraceError('Bitcoin API returned an invalid saved snapshot status')
        compatible = old_chain['in_best_chain']
    row['coverage']['continued_bitcoin_history'] = bool(compatible)
    if compatible:
        histories.update(deepcopy(old_row.get('coverage', {}).get('bitcoin_histories', {})))
        # Candidate observations are refreshed before any scanned prefix is
        # skipped. Their older evidence remains available by observation ID.
        hints = {(item['bitcoin_txid'], item['liquid_vout'])
                 for item in old_row.get('coverage', {}).get('candidate_hints', [])}
        hints.update((item['bitcoin_txid'], item['liquid_vout']) for item in previous['matches']
                     if item['liquid_txid'] == transaction['txid'])
        row['coverage']['candidate_hints'] = [dict(bitcoin_txid=target, liquid_vout=number)
                                              for target, number in sorted(hints)]
        for target, liquid_number in sorted(hints):
            if liquid_number not in outputs:
                continue
            if target not in checked:
                checked[target] = _fetch(context, result, evidence, 'bitcoin', target)[0]
            _check_payout(result, checked[target], transaction, liquid_number, outputs[liquid_number], window)
    # Esplora script history has no date cursor. Completed prefixes are retained
    # only in the current session report, never in a new permanent history.
    for script, requests in by_script.items():
        checkpoint = histories.setdefault(script, dict(cursor=None, ceiling=None, seen=[], finished=False))
        if checkpoint['finished']:
            row['coverage']['destination_scripts_searched'] += 1
            continue
        scripthash = sha256(bytes.fromhex(script)).digest()[::-1].hex()
        endpoint = '/scripthash/' + scripthash + '/txs/chain'
        cursor, ceiling, seen = checkpoint['cursor'], checkpoint['ceiling'], set(checkpoint['seen'])
        while True:
            if context['pages'] >= context['max_pages']:
                raise StopRun('page_limit')
            report_progress(progress, 'pegin_history', context['pages'], context['max_pages'])
            data, _ = _history_page(context['bitcoin'], endpoint + ('/' + cursor if cursor else ''))
            context['pages'] += 1
            if not isinstance(data, list) or len(data) > 25:
                raise TraceError('Bitcoin script history returned an invalid page')
            previous_height = ceiling
            crossed = False
            for item in data:
                if not isinstance(item, dict):
                    raise TraceError('Bitcoin script history returned an invalid transaction')
                txid = _hash(item.get('txid'), 'Bitcoin transaction')
                status = _status(item)
                if (not status['confirmed'] or type(status.get('block_time')) is not int
                        or (previous_height is not None and status['block_height'] > previous_height) or txid in seen):
                    raise TraceError('Bitcoin script history is unconfirmed, unordered, or repeated')
                previous_height = status['block_height']
                if previous_height < low:
                    crossed = True
                elif window['start_time'] <= status['block_time'] <= window['end_time']:
                    if txid not in checked:
                        checked[txid] = _fetch(context, result, evidence, 'bitcoin', txid)[0]
                    for liquid_number, request in requests:
                        _check_payout(result, checked[txid], transaction, liquid_number, request, window)
                # Commit a fully verified prefix, including within a page. A
                # request/time limit while checking later members can resume.
                seen.add(txid)
                checkpoint.update(cursor=txid, ceiling=previous_height)
                checkpoint['seen'].append(txid)
            if not data or crossed:
                checkpoint['finished'] = True
                break
            cursor, ceiling = checkpoint['cursor'], checkpoint['ceiling']
        row['coverage']['destination_scripts_searched'] += 1
    chain, _ = _history_page(context['bitcoin'], '/block/' + window['snapshot_hash'] + '/status')
    if not isinstance(chain, dict) or type(chain.get('in_best_chain')) is not bool:
        raise TraceError('Bitcoin API returned an invalid chain status')
    if not chain['in_best_chain']:
        raise StopRun('bitcoin_chain_changed')
    window['complete'] = not window['end_not_yet_covered_by_chain']
    window['remaining_ranges'] = []
    window['covered_ranges'] = [dict(start_height=window['start_height'], end_height=window['end_height'])]
    row['stop_reason'] = 'window_range_complete' if window['complete'] else 'window_not_yet_elapsed'


def _check_payout(result, candidate, transaction, liquid_number, request, window):
    status = _status(candidate)
    if (not status['confirmed'] or type(status.get('block_time')) is not int
            or not window['start_time'] <= status['block_time'] <= window['end_time']):
        return
    for btc_number, out in enumerate(candidate['vout']):
        if (out.get('scriptpubkey', '').lower() == request['scriptpubkey']
                and type(out.get('value')) is int and out['value'] == request['value']):
            _candidate(result, candidate, btc_number, transaction, liquid_number)


def _bitcoin_source(context, spec, result, evidence, transaction, outputs, row, progress):
    discovery = _window(context, spec, transaction['status'], progress)
    row['coverage']['window'] = discovery.window
    checked = set()
    try:
        discovery.prepare()
        while True:
            for btc_number, output in outputs.items():
                rows = context['db'].execute('SELECT liquid_txid,vout FROM pegout_requests '
                    'WHERE script=? AND value=? AND timestamp BETWEEN ? AND ?',
                    (output['scriptpubkey'], output['value'], discovery.window['start_time'],
                     discovery.window['end_time'])).fetchall()
                for liquid_txid, liquid_number in rows:
                    key = (btc_number, liquid_txid, liquid_number)
                    if key in checked:
                        continue
                    checked.add(key)
                    candidate, inspection = _fetch(context, result, evidence, 'liquid', liquid_txid)
                    status = inspection['status']
                    if (not status['confirmed'] or type(status.get('block_time')) is not int
                            or not discovery.window['start_time'] <= status['block_time'] <= discovery.window['end_time']):
                        continue
                    request = pegout_outputs(candidate).get(liquid_number)
                    if request and request['scriptpubkey'] == output['scriptpubkey'] and request['value'] == output['value']:
                        _candidate(result, transaction, btc_number, candidate, liquid_number)
            if not discovery.step():
                row['stop_reason'] = ('window_range_complete' if discovery.coverage()['complete']
                                      else 'window_not_yet_elapsed')
                break
    finally:
        context['pages'] += discovery.pages
        row['coverage']['window'] = discovery.coverage()


def _row_matches(result, row, network):
    return [item for item in result['matches'] if item[network + '_txid'] == row['source_txid']]


def _ambiguities(result, network):
    groups = {}
    for item in result['matches']:
        item['status'] = 'candidate'
        for side in ('bitcoin', 'liquid'):
            groups.setdefault((side, item[side + '_txid'], item[side + '_vout']), []).append(item)
    for items in groups.values():
        if len(items) > 1:
            for item in items:
                item['status'] = 'ambiguous'
    for row in result['results']:
        matches = _row_matches(result, row, network)
        row['match_count'] = len(matches)
        if row['status'] in ('candidate', 'ambiguous'):
            row['status'] = 'ambiguous' if any(item['status'] == 'ambiguous' for item in matches) else 'candidate'


def _save(owner, result, evidence, context):
    _ambiguities(result, result['query']['network'])
    if result['query']['network'] == 'liquid':
        for row in result['results']:
            hints = {(item['bitcoin_txid'], item['liquid_vout'])
                     for item in row['coverage'].get('candidate_hints', [])}
            hints.update((item['bitcoin_txid'], item['liquid_vout'])
                         for item in _row_matches(result, row, 'liquid'))
            if hints:
                row['coverage']['candidate_hints'] = [dict(bitcoin_txid=target, liquid_vout=number)
                                                      for target, number in sorted(hints)]
    folder = _ordinary(Path(owner) / 'pegin-matches' / result['id'])
    folder.mkdir(parents=True, exist_ok=True)
    result['coverage'] = dict(pages_this_task=context['pages'], requests_this_task=context['bitcoin'].budget.requests,
        total_transactions=len(result['results']), processed_transactions=sum(
            row['status'] != 'unprocessed' for row in result['results']), searched_mempool=False)
    result['finished_at'] = now()
    save_json(_ordinary(folder / 'match.json'), result)
    save_json(_ordinary(folder / 'evidence.json'), evidence)
    fields = ['Source network', 'Source transaction', 'Bitcoin transaction', 'Bitcoin vout',
              'Bitcoin address', 'Value (sats)', 'Liquid transaction', 'Liquid vout', 'Status', 'Verification',
              'Reason', 'Bitcoin block timestamp (Unix)', 'Liquid block timestamp (Unix)', 'Block-time delay (seconds)']
    rows = []
    network = result['query']['network']
    for item in result['matches']:
        rows.append(dict(zip(fields, [network, item[network + '_txid'], item['bitcoin_txid'], item['bitcoin_vout'],
            item['bitcoin_address'], item['bitcoin_value_sats'], item['liquid_txid'], item['liquid_vout'],
            item['status'], item['verification'], 'script_amount_time_agreement',
            item['bitcoin_status']['block_time'], item['liquid_status']['block_time'], item['elapsed_seconds']])))
    for row in result['results']:
        if row['status'] not in ('candidate', 'ambiguous'):
            rows.append({'Source network': network, 'Source transaction': row['source_txid'],
                'Bitcoin transaction': row.get('bitcoin_txid') or '', 'Liquid transaction': row.get('liquid_txid') or '',
                'Status': row['status'], 'Reason': row['stop_reason']})
    _ordinary(folder / 'matches.csv').write_text(_csv(rows, fields), encoding='utf-8')
    fields = ['Liquid transaction', 'Liquid vout', 'Liquid address', 'Public value (base units)', 'Public asset']
    rows = [dict(zip(fields, [txid, out['vout'], out['address'], out['value'], out['asset']]))
            for txid, outputs in result['liquid_outputs'].items() for out in outputs]
    _ordinary(folder / 'liquid-outputs.csv').write_text(_csv(rows, fields), encoding='utf-8')
    _ordinary(folder / 'SHA256SUMS').write_text(''.join(digest((folder / name).read_bytes()) + '  ' + name + '\n'
        for name in sorted(FILES - {'SHA256SUMS'})), encoding='ascii')


def match_pegout(workspace, txids, *, source_network='liquid', vouts=None, max_pages=100,
                  max_requests=1200, max_seconds=300, public=False, start_minutes=None, end_minutes=None,
                  bitcoin_fixture=None, liquid_fixture=None, progress=None, report_root=None, report_id=None, resume_report=None):
    spec = pegout_query(txids, source_network=source_network, vouts=vouts, max_pages=max_pages,
                        start_minutes=start_minutes, end_minutes=end_minutes)
    if resume_report is not None:
        if not isinstance(resume_report, str) or not REPORT_ID.fullmatch(resume_report):
            raise TraceError('Invalid bridge result identifier to continue')
        spec['resume_report'] = resume_report
    with _session(workspace, spec, max_requests=max_requests, max_seconds=max_seconds, public=public,
                   bitcoin_fixture=bitcoin_fixture, liquid_fixture=liquid_fixture,
                   report_root=report_root, report_id=report_id) as (owner, result, evidence, context):
        result['results'] = [dict(source_txid=txid, **{source_network + '_txid': txid},
            status='unprocessed', stop_reason='not_processed', match_count=0, coverage={}) for txid in spec['txids']]
        if resume_report is not None:
            from .bridge_matching import carry_bridge_completed
            carry_bridge_completed(owner, resume_report, result, evidence)
            context['previous_report'] = read_report(None, resume_report, report_root=owner)
        _save(owner, result, evidence, context)
        active = None
        try:
            for position, row in enumerate(result['results']):
                if row.get('carried_from_report'):
                    continue
                active = row
                context['bitcoin'].budget.check_request()
                report_progress(progress, 'pegin_batch', position, len(result['results']))
                first_page = context['pages']
                row['status'] = 'searching'
                try:
                    transaction, inspection = _fetch(context, result, evidence, source_network, row['source_txid'])
                    status = inspection['status']
                    if not status['confirmed'] or type(status.get('block_time')) is not int:
                        raise TraceError('A confirmed source transaction with a block timestamp is required')
                    outputs = _selected(transaction, source_network, spec['vouts'])
                    if outputs:
                        runner = _liquid_source if source_network == 'liquid' else _bitcoin_source
                        runner(context, spec, result, evidence, transaction, outputs, row, progress)
                        candidates = _row_matches(result, row, source_network)
                        found = {item[source_network + '_vout'] for item in candidates}
                        row['coverage']['candidate_vouts'] = sorted(found)
                        row['coverage']['outputs_without_candidate'] = sorted(set(outputs) - found)
                        row['status'] = 'candidate' if candidates else 'no_match'
                        if (row['stop_reason'] == 'window_not_yet_elapsed'
                                or (candidates and spec['vouts'] and set(outputs) - found)):
                            row['status'] = 'partial'
                    else:
                        row.update(status='no_match', stop_reason='no_eligible_pegout_outputs')
                except StopRun:
                    raise
                except TraceError:
                    row.update(status='error', stop_reason='lookup_failed')
                finally:
                    row['coverage']['pages_this_task'] = context['pages'] - first_page
                    row['checked_at'] = now()
                    _save(owner, result, evidence, context)
            statuses = {row['status'] for row in result['results']}
            result.update(status=next(iter(statuses)) if len(statuses) == 1 else 'partial',
                          stop_reason='batch_complete' if spec['mode'] == 'batch' else result['results'][0]['stop_reason'])
        except StopRun as error:
            result.update(status='paused', stop_reason=str(error))
            if active is not None:
                active.update(status='paused', stop_reason=str(error))
            for row in result['results']:
                if row['status'] == 'unprocessed':
                    row['stop_reason'] = 'not_processed_after_' + str(error)
        except KeyboardInterrupt:
            result.update(status='cancelled', stop_reason='cancelled')
            if active is not None:
                active.update(status='cancelled', stop_reason='cancelled')
            for row in result['results']:
                if row['status'] == 'unprocessed':
                    row['stop_reason'] = 'not_processed_after_cancellation'
            raise
        except Exception:
            result.update(status='error', stop_reason='lookup_failed')
            if active is not None:
                active.update(status='error', stop_reason='lookup_failed')
            raise
        finally:
            _save(owner, result, evidence, context)
    report_progress(progress, 'pegin_saved')
    return result
