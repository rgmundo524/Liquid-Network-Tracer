"""Synthetic peg-out candidates, never assertions of exact payout identity."""
from copy import deepcopy
import csv
from hashlib import sha256
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.api import Esplora
from liquid_tracer.common import LBTC, TraceError, save_json
from liquid_tracer.pegin_matching import HISTORY, read_report, verified_file
from liquid_tracer.pegout_matching import BITCOIN_GENESIS, match_pegout, pegout_outputs, pegout_query
from tests.test_pegin_window import Chain, EPOCH

BTC, LIQUID, OTHER = 'a' * 64, 'b' * 64, 'c' * 64
SCRIPT = '0014' + '11' * 20
VALUE = 60_000_000


def btc_output(script=SCRIPT, value=VALUE):
    return dict(scriptpubkey=script, scriptpubkey_type='v0_p2wpkh',
                scriptpubkey_address='synthetic-btc', value=value)


def request_output(script=SCRIPT, value=VALUE):
    return dict(scriptpubkey='6a00', scriptpubkey_type='op_return', value=value, asset=LBTC,
                pegout=dict(genesis_hash=BITCOIN_GENESIS, scriptpubkey=script, scriptpubkey_address='synthetic-btc'))


def fixture(chain):
    values = {'/blocks': chain.response('/blocks')}
    for height, header in chain.headers.items():
        values['/blocks/' + str(height)] = chain.response('/blocks/' + str(height))
        values['/block/' + header['id'] + '/status'] = {'in_best_chain': True}
    for height, txs in chain.transactions.items():
        for offset in range(0, len(txs), 25):
            endpoint = '/block/' + chain.headers[height]['id'] + '/txs' + ('/' + str(offset) if offset else '')
            values[endpoint] = txs[offset:offset + 25]
        for tx in txs:
            values['/tx/' + tx['txid']] = tx
    for pos, tx in enumerate(chain.history):
        values[HISTORY + '/' + tx['txid']] = chain.history[pos + 1:pos + 26]
    return values


class PegoutTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = self.root / 'workspace'
        self.btc_chain = Chain()
        self.liquid_chain = Chain()
        self.liquid = self.liquid_chain.add(100, LIQUID)
        self.liquid['vin'] = []
        self.liquid['vout'] = [request_output()]
        self.btc = self.btc_chain.add(130, BTC)
        self.btc['vin'] = []
        self.btc['vout'] = [btc_output(), btc_output('0014' + '22' * 20, 500)]
        self.btc_fixture = self.root / 'bitcoin.json'
        self.liquid_fixture = self.root / 'liquid.json'
        self.scripthash = sha256(bytes.fromhex(SCRIPT)).digest()[::-1].hex()
        self.endpoint = '/scripthash/' + self.scripthash + '/txs/chain'
        self.write()

    def write(self, *, btc_extra=None, liquid_extra=None):
        self.btc_data = fixture(self.btc_chain)
        self.btc_data.update({self.endpoint: [self.btc], self.endpoint + '/' + BTC: []})
        self.btc_data.update(btc_extra or {})
        self.liquid_data = fixture(self.liquid_chain)
        self.liquid_data.update(liquid_extra or {})
        save_json(self.btc_fixture, self.btc_data)
        save_json(self.liquid_fixture, self.liquid_data)

    def run_match(self, txids=LIQUID, *, source_network='liquid', **kwargs):
        kwargs.setdefault('start_minutes', -5 if source_network == 'liquid' else -60)
        kwargs.setdefault('end_minutes', 60 if source_network == 'liquid' else 5)
        return match_pegout(self.workspace, txids, source_network=source_network,
            bitcoin_fixture=self.btc_fixture, liquid_fixture=self.liquid_fixture, **kwargs)

    def test_liquid_request_to_btc_output_is_candidate_not_exact_reference(self):
        result = self.run_match()
        self.assertEqual(result['status'], 'candidate')
        candidate, = result['matches']
        self.assertEqual((candidate['bitcoin_txid'], candidate['bitcoin_vout']), (BTC, 0))
        self.assertEqual((candidate['liquid_txid'], candidate['liquid_vout']), (LIQUID, 0))
        self.assertEqual(candidate['verification'], 'script_amount_time_candidate')
        self.assertEqual(candidate['status'], 'candidate')
        self.assertEqual(candidate['elapsed_seconds'], 30 * 60)
        self.assertIn('does not prove', result['notice'])
        self.assertTrue(result['results'][0]['coverage']['window']['complete'])
        saved = read_report(None, result['id'], workspace=self.workspace)
        self.assertEqual(result, saved)
        rows = list(csv.DictReader(io.StringIO(verified_file(None, result['id'], 'matches.csv',
            workspace=self.workspace).read_text())))
        self.assertEqual(rows[0]['Status'], 'candidate')
        self.assertEqual(rows[0]['Liquid transaction'], LIQUID)
        self.assertEqual(rows[0]['Bitcoin transaction'], BTC)

    def test_bitcoin_output_discovers_liquid_pegout_from_filtered_history(self):
        result = self.run_match(BTC, source_network='bitcoin', vouts=[0])
        self.assertEqual(result['status'], 'candidate')
        self.assertEqual(result['matches'][0]['liquid_txid'], LIQUID)
        self.assertTrue(result['results'][0]['coverage']['window']['complete'])
        self.assertNotIn('confirmed_reference', json.dumps(result))
        # Dedicated peg-out hints/coverage cannot mutate the peg-in index.
        self.assertFalse((self.workspace / '.pegin-index').exists())
        second = self.run_match(BTC, source_network='bitcoin', vouts=[0])
        self.assertEqual([(m['bitcoin_outpoint'], m['liquid_outpoint']) for m in second['matches']],
                         [(m['bitcoin_outpoint'], m['liquid_outpoint']) for m in result['matches']])
        self.assertEqual(second['coverage']['pages_this_task'], 0)

    def test_wrong_amount_script_time_asset_and_genesis_are_not_candidates(self):
        for change in ('amount', 'script', 'time', 'asset', 'genesis'):
            with self.subTest(change=change):
                btc = deepcopy(self.btc)
                liquid = deepcopy(self.liquid)
                if change == 'amount':
                    btc['vout'][0]['value'] += 1
                elif change == 'script':
                    btc['vout'][0]['scriptpubkey'] = '0014' + '33' * 20
                elif change == 'time':
                    btc['status']['block_time'] += 100_000
                elif change == 'asset':
                    liquid['vout'][0]['asset'] = 'f' * 64
                else:
                    liquid['vout'][0]['pegout']['genesis_hash'] = 'f' * 64
                self.write(btc_extra={'/tx/' + BTC: btc}, liquid_extra={'/tx/' + LIQUID: liquid})
                result = self.run_match()
                self.assertFalse(result['matches'])
                self.assertEqual(result['status'], 'no_match')

    def test_repeated_payouts_and_shared_requests_are_ambiguous(self):
        other = self.btc_chain.add(131, OTHER)
        other['vout'] = [btc_output()]
        self.write(btc_extra={self.endpoint: [other, self.btc]})
        result = self.run_match()
        self.assertEqual(result['status'], 'ambiguous')
        self.assertEqual(len(result['matches']), 2)
        self.assertEqual({m['status'] for m in result['matches']}, {'ambiguous'})
        self.liquid['vout'].append(request_output())
        self.write()
        result = self.run_match(vouts=[0, 1])
        self.assertEqual(result['status'], 'ambiguous')
        self.assertEqual(len(result['matches']), 2)
        self.assertEqual({m['status'] for m in result['matches']}, {'ambiguous'})

    def test_selecting_multiple_outputs_keeps_partial_results_explicit(self):
        self.btc['vout'][1] = btc_output(SCRIPT, VALUE + 1)
        self.write()
        result = self.run_match(BTC, source_network='bitcoin', vouts=[0, 1])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['results'][0]['coverage']['outputs_without_candidate'], [1])
        self.assertEqual(len(result['matches']), 1)

    def test_invalid_selected_outputs_and_unconfirmed_sources_are_errors(self):
        for options in ({'vouts': [99]},):
            result = self.run_match(**options)
            self.assertEqual(result['status'], 'error')
            self.assertFalse(result['matches'])
        self.liquid['status'] = {'confirmed': False}
        self.write()
        result = self.run_match()
        self.assertEqual(result['status'], 'error')

    def test_fresh_confirmation_check_rejects_stale_history_hint(self):
        candidate = deepcopy(self.btc)
        candidate['status'] = {'confirmed': False}
        self.write(btc_extra={'/tx/' + BTC: candidate})
        result = self.run_match()
        self.assertFalse(result['matches'])
        self.assertEqual(result['status'], 'no_match')

    def test_page_budget_is_shared_and_csv_retains_all_batch_inputs(self):
        other = self.liquid_chain.add(110, OTHER)
        other['vout'] = [request_output()]
        self.write()
        result = self.run_match([LIQUID, OTHER], max_pages=1)
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(result['coverage']['pages_this_task'], 1)
        self.assertEqual([r['status'] for r in result['results']], ['paused', 'unprocessed'])
        self.assertEqual(len(result['matches']), 1)
        rows = list(csv.DictReader(io.StringIO(verified_file(None, result['id'], 'matches.csv',
            workspace=self.workspace).read_text())))
        self.assertEqual({row['Source transaction'] for row in rows}, {LIQUID, OTHER})
        self.assertTrue(any(row['Status'] == 'unprocessed' for row in rows))
        self.assertTrue(any(row['Status'] == 'paused' for row in rows))

    def test_request_budget_and_cancellation_preserve_partial_results(self):
        result = self.run_match([LIQUID, OTHER], max_requests=1)
        self.assertEqual(result['stop_reason'], 'request_limit')
        self.assertEqual(result['coverage']['requests_this_task'], 1)
        from liquid_tracer.pegout_matching import _history_page
        original = _history_page
        def cancel(api, endpoint):
            if endpoint == self.endpoint + '/' + BTC:
                raise KeyboardInterrupt
            return original(api, endpoint)
        identity = '1' * 32
        with patch('liquid_tracer.pegout_matching._history_page', cancel), self.assertRaises(KeyboardInterrupt):
            self.run_match([LIQUID, OTHER], report_id=identity)
        saved = read_report(None, identity, workspace=self.workspace)
        self.assertEqual(saved['status'], 'cancelled')
        self.assertEqual(saved['results'][0]['status'], 'cancelled')
        self.assertEqual(saved['results'][1]['status'], 'unprocessed')
        self.assertEqual(len(saved['matches']), 1)

    def test_session_report_root_keeps_artifacts_out_of_workspace(self):
        session = self.root / 'session'
        result = self.run_match(report_root=session / '.pegin-matching', report_id='2' * 32)
        self.assertEqual(read_report(None, result['id'], workspace=session), result)
        self.assertFalse((self.workspace / '.pegin-matching').exists())
        self.assertTrue((self.workspace / '.pegout-index').exists())
        with self.assertRaisesRegex(TraceError, 'already exists'):
            self.run_match(report_root=session / '.pegin-matching', report_id='2' * 32)

    def test_no_pegout_fails_closed_and_input_validation_precedes_writes(self):
        self.liquid['vout'] = [dict(scriptpubkey='6a00', scriptpubkey_type='op_return', value=0, asset=LBTC)]
        self.write()
        result = self.run_match()
        self.assertEqual(result['stop_reason'], 'no_eligible_pegout_outputs')
        self.assertEqual(result['coverage']['pages_this_task'], 0)
        for options in ({'source_network': 'testnet'}, {'vouts': [True]}, {'start_minutes': float('nan')},
                        {'max_pages': 0}, {'max_seconds': float('inf')}, {'public': 'yes'}):
            with self.subTest(options=options), self.assertRaises(TraceError):
                self.run_match(**options)
        self.assertEqual(pegout_query(BTC, source_network='bitcoin')['start_minutes'], -4320)
        self.assertEqual(pegout_query(LIQUID)['end_minutes'], 4320)
        with self.assertRaises(TraceError):
            pegout_query([LIQUID, OTHER], vouts=[0])

    def test_candidate_search_continues_to_detect_ambiguity_and_records_page_limit(self):
        second = self.btc_chain.add(129, OTHER)
        second['vout'] = [btc_output()]
        self.write(btc_extra={self.endpoint + '/' + BTC: [second], self.endpoint + '/' + OTHER: []})
        result = self.run_match()
        self.assertEqual(result['status'], 'ambiguous')
        self.assertEqual(len(result['matches']), 2)
        limited = self.run_match(max_pages=1)
        self.assertEqual(limited['status'], 'paused')
        self.assertEqual(len(limited['matches']), 1)
        self.assertFalse(limited['results'][0]['coverage']['window']['complete'])

    def test_fresh_liquid_body_rejects_stale_destination_hint(self):
        changed = deepcopy(self.liquid)
        changed['vout'][0]['pegout']['scriptpubkey'] = '0014' + '99' * 20
        self.write(liquid_extra={'/tx/' + LIQUID: changed})
        result = self.run_match(BTC, source_network='bitcoin')
        self.assertFalse(result['matches'])
        self.assertEqual(result['status'], 'no_match')

    def test_liquid_index_checks_all_requests_and_many_to_one_is_ambiguous(self):
        second = self.liquid_chain.add(101, OTHER)
        second['vout'] = [request_output()]
        self.write()
        result = self.run_match(BTC, source_network='bitcoin')
        self.assertEqual(result['status'], 'ambiguous')
        self.assertEqual({item['liquid_txid'] for item in result['matches']}, {LIQUID, OTHER})
        self.assertEqual({item['status'] for item in result['matches']}, {'ambiguous'})

    def test_batch_errors_do_not_hide_later_completed_inputs(self):
        result = self.run_match([OTHER, LIQUID])
        self.assertEqual(result['stop_reason'], 'batch_complete')
        self.assertEqual([row['status'] for row in result['results']], ['error', 'candidate'])
        self.assertEqual(result['status'], 'partial')

    def test_repeated_script_page_is_error_not_false_completion(self):
        self.write(btc_extra={self.endpoint + '/' + BTC: [self.btc]})
        result = self.run_match()
        self.assertEqual(result['status'], 'error')
        self.assertFalse(result['results'][0]['coverage']['window']['complete'])
        self.assertEqual(len(result['matches']), 1)

    def test_changed_bitcoin_snapshot_keeps_results_incomplete(self):
        tip = self.btc_chain.headers[max(self.btc_chain.headers)]['id']
        self.write(btc_extra={'/block/' + tip + '/status': {'in_best_chain': False}})
        result = self.run_match()
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(result['stop_reason'], 'bitcoin_chain_changed')
        self.assertFalse(result['results'][0]['coverage']['window']['complete'])

    def test_future_search_window_is_explicitly_incomplete(self):
        result = self.run_match(end_minutes=2000)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['stop_reason'], 'window_not_yet_elapsed')
        self.assertFalse(result['results'][0]['coverage']['window']['complete'])


    def test_resume_skips_completed_rows_and_advances_under_same_page_budget(self):
        other = self.liquid_chain.add(110, OTHER)
        other['vout'] = [request_output()]
        self.write()
        first = self.run_match([LIQUID, OTHER], max_pages=2)
        self.assertEqual([row['status'] for row in first['results']], ['candidate', 'paused'])
        second = self.run_match([LIQUID, OTHER], max_pages=2, resume_report=first['id'])
        self.assertEqual(second['stop_reason'], 'batch_complete')
        self.assertEqual(second['status'], 'ambiguous')
        self.assertEqual(second['results'][0]['carried_from_report'], first['id'])
        self.assertNotIn('carried_from_report', second['results'][1])
        self.assertEqual(second['coverage']['pages_this_task'], 2)
        self.assertEqual({row['source_txid'] for row in second['results']}, {LIQUID, OTHER})
        for options in ({'end_minutes': 120}, {'txids': [OTHER, LIQUID]}, {'public': True}):
            with self.subTest(options=options), self.assertRaises(TraceError):
                kwargs = {**dict(txids=[LIQUID, OTHER], max_pages=2, resume_report=first['id']), **options}
                self.run_match(**kwargs)


    def test_single_resume_continues_a_paused_script_page_without_repeating_head(self):
        first = self.run_match(max_pages=1)
        self.assertEqual(first['status'], 'paused')
        from liquid_tracer.pegout_matching import _history_page
        calls = []
        def record(api, endpoint):
            calls.append(endpoint)
            return _history_page(api, endpoint)
        with patch('liquid_tracer.pegout_matching._history_page', record):
            second = self.run_match(max_pages=1, resume_report=first['id'])
        self.assertEqual(second['status'], 'candidate')
        self.assertTrue(second['results'][0]['coverage']['continued_bitcoin_history'])
        self.assertNotIn(self.endpoint, calls)
        self.assertIn(self.endpoint + '/' + BTC, calls)
        self.assertEqual(second['coverage']['pages_this_task'], 1)
        self.assertEqual(len(second['matches']), 1)
        self.assertNotEqual(first['matches'][0]['bitcoin_observation_id'], second['matches'][0]['bitcoin_observation_id'])
        evidence = json.loads(verified_file(None, second['id'], 'evidence.json', workspace=self.workspace).read_text())
        self.assertIn(str(first['matches'][0]['bitcoin_observation_id']), evidence['bitcoin_checks'])
        self.assertIn(str(second['matches'][0]['bitcoin_observation_id']), evidence['bitcoin_checks'])

    def test_resume_revalidates_old_candidates_before_skipping_history_prefix(self):
        first = self.run_match(max_pages=1)
        original = Esplora.get
        def changed(api, endpoint):
            data, oid = original(api, endpoint)
            if api.blockchain == 'bitcoin' and endpoint == '/tx/' + BTC:
                data = deepcopy(data)
                data['vout'][0]['value'] += 1
            return data, oid
        with patch.object(Esplora, 'get', changed):
            second = self.run_match(max_pages=1, resume_report=first['id'])
        self.assertEqual(second['status'], 'no_match')
        self.assertFalse(second['matches'])

    def test_resume_discards_history_cursor_if_saved_snapshot_changed(self):
        first = self.run_match(max_pages=1)
        from liquid_tracer.pegout_matching import _history_page
        calls = []
        def changed(api, endpoint):
            calls.append(endpoint)
            if endpoint.endswith('/status'):
                return {'in_best_chain': False}, 1
            return _history_page(api, endpoint)
        with patch('liquid_tracer.pegout_matching._history_page', changed):
            second = self.run_match(max_pages=1, resume_report=first['id'])
        self.assertEqual(second['status'], 'paused')
        self.assertFalse(second['results'][0]['coverage']['continued_bitcoin_history'])
        self.assertIn(self.endpoint, calls)

    def test_request_interruption_keeps_completed_prefix_inside_script_page(self):
        other = self.btc_chain.add(129, OTHER)
        other['vout'] = [btc_output()]
        self.write(btc_extra={self.endpoint: [self.btc, other], self.endpoint + '/' + BTC: [other],
                              self.endpoint + '/' + OTHER: []})
        from liquid_tracer.common import StopRun
        original = Esplora.get
        def limited(api, endpoint):
            if api.blockchain == 'bitcoin' and endpoint == '/tx/' + OTHER:
                raise StopRun('request_limit')
            return original(api, endpoint)
        with patch.object(Esplora, 'get', limited):
            first = self.run_match(max_pages=1)
        checkpoint = first['results'][0]['coverage']['bitcoin_histories'][SCRIPT]
        self.assertEqual(checkpoint['cursor'], BTC)
        second = self.run_match(max_pages=1, resume_report=first['id'])
        self.assertEqual(second['status'], 'paused')
        self.assertEqual(len(second['matches']), 2)
        third = self.run_match(max_pages=1, resume_report=second['id'])
        self.assertEqual(third['status'], 'ambiguous')
        self.assertEqual(len(third['matches']), 2)


    def test_interrupted_candidate_recheck_does_not_lose_unchecked_hints(self):
        other = self.btc_chain.add(129, OTHER)
        other['vout'] = [btc_output()]
        self.write(btc_extra={self.endpoint: [self.btc, other], self.endpoint + '/' + OTHER: []})
        first = self.run_match(max_pages=1)
        self.assertEqual(len(first['matches']), 2)
        from liquid_tracer.common import StopRun
        original = Esplora.get
        def limited(api, endpoint):
            if api.blockchain == 'bitcoin' and endpoint == '/tx/' + OTHER:
                raise StopRun('request_limit')
            return original(api, endpoint)
        with patch.object(Esplora, 'get', limited):
            second = self.run_match(max_pages=1, resume_report=first['id'])
        self.assertEqual(second['status'], 'paused')
        self.assertEqual(len(second['matches']), 1)
        self.assertEqual(len(second['results'][0]['coverage']['candidate_hints']), 2)
        third = self.run_match(max_pages=1, resume_report=second['id'])
        self.assertEqual(third['status'], 'ambiguous')
        self.assertEqual({item['bitcoin_txid'] for item in third['matches']}, {BTC, OTHER})


    def test_malformed_mainnet_request_cannot_be_offered_for_selection(self):
        for mutate in (lambda out: out.update(value=True), lambda out: out['pegout'].update(scriptpubkey='zz'),
                       lambda out: out['pegout'].update(genesis_hash='bad')):
            tx = deepcopy(self.liquid)
            mutate(tx['vout'][0])
            with self.assertRaises(TraceError):
                pegout_outputs(tx)
        self.assertEqual(pegout_outputs(self.liquid)[0]['value'], VALUE)


if __name__ == '__main__':
    unittest.main()
