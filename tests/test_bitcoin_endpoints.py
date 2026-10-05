"""Exact endpoint paths across Bitcoin and Liquid, including analyst boundaries."""
from copy import deepcopy
import csv
import io
import tempfile
from pathlib import Path
import unittest

from liquid_tracer.common import TraceError
from liquid_tracer.pegout_csv import endpoint_table_rows, pegout_csv_rows, write_endpoint_table_csv
from liquid_tracer.pegout_paths import pegout_graph, validate_query
from liquid_tracer.plots import _query
from tests.test_attribution_convergence import annotation, graph_state, tx
from tests.test_pegout_paths import add_pegout, add_unspendable, mark_unspent


def bitcoin_state(*args, **kwargs):
    state = graph_state(*args, **kwargs)
    state['blockchain'] = 'bitcoin'
    state['source'] = 'https://blockstream.info/api'
    for record in state['transactions'].values():
        for output in record['data']['vout']:
            for field in ('asset', 'assetcommitment', 'valuecommitment'):
                output.pop(field, None)
            output['value'] = 123456789
    for record in state['transactions'].values():
        for vin in record['data']['vin']:
            vin['prevout'] = deepcopy(state['transactions'][vin['txid']]['data']['vout'][vin['vout']])
    return state


def endpoint_graph(state, **options):
    return pegout_graph(state, validate_query(seeds=state['seeds'], blockchain=state.get('blockchain', 'liquid'), **options))


class BitcoinEndpointTests(unittest.TestCase):
    def test_native_values_and_addresses_never_become_lbtc_or_pegouts(self):
        state = bitcoin_state((("a:0", "b"),), seeds=("a:0",))
        key = tx('b') + ':0'
        mark_unspent(state, key)
        terminal = add_unspendable(state, tx('b'))
        state['transactions'][tx('b')]['data']['vout'][-1].pop('asset')
        original = deepcopy(state)
        graph = endpoint_graph(state, include_unspent=True, include_unspendable=True)
        self.assertEqual(graph['pegouts']['matches'], [])
        self.assertEqual(graph['pegouts']['status'], 'endpoints_found')
        self.assertEqual({entry['outpoint'] for entry in graph['pegouts']['endpoint_matches']}, {key, terminal})
        rows = endpoint_table_rows(graph, state)
        self.assertEqual({row['Status'] for row in rows}, {'Dormant', 'OP_Return'})
        for row in rows:
            self.assertEqual(row['Network'], 'bitcoin')
            self.assertEqual(row['Asset'], 'BTC')
            self.assertEqual(row['Value LBTC'], '')
            self.assertEqual(row['Pegout LBTC'], '')
            self.assertTrue(row['Explorer URL'].startswith('https://blockstream.info/tx/'))
            self.assertEqual(row['Source Value'], '1.23456789 BTC')
        dormant = next(row for row in rows if row['Status'] == 'Dormant')
        self.assertEqual((dormant['Value Base Units'], dormant['Value BTC'], dormant['Value']),
                         (123456789, '1.23456789', '1.23456789'))
        self.assertEqual(state, original)

    def test_bitcoin_never_treats_pegout_like_metadata_as_a_bridge(self):
        state = bitcoin_state((("a:0", "b"),), seeds=("a:0",))
        key = add_pegout(state, tx('b'))
        state['transactions'][tx('b')]['data']['vout'][-1].pop('asset')
        graph = endpoint_graph(state, include_unspendable=True)
        self.assertEqual(graph['pegouts']['match_count'], 0)
        self.assertEqual(graph['pegouts']['endpoint_matches'][0]['outpoint'], key)
        self.assertEqual(graph['pegouts']['endpoint_matches'][0]['kind'], 'unspendable')

    def test_bitcoin_rejects_lbtc_budget_at_all_query_entrypoints(self):
        state = bitcoin_state(seeds=("a:0",))
        with self.assertRaisesRegex(TraceError, 'Bitcoin'):
            validate_query(seeds=state['seeds'], blockchain='bitcoin', pegout_lbtc_limit='1')
        with self.assertRaisesRegex(TraceError, 'Bitcoin'):
            _query('pegouts', state, 0, 10, pegout_lbtc_limit='1')
        with self.assertRaisesRegex(TraceError, 'Bitcoin'):
            pegout_graph(state, {'seeds': state['seeds'], 'min_hops': 0, 'max_hops': 10, 'pegout_lbtc_limit': '1'})

    def test_attributed_stop_is_an_endpoint_and_sibling_branch_continues(self):
        state = bitcoin_state((("a:0", "b"), ("b:0", "c"), ("b:1", "d")), seeds=("a:0",))
        stop = tx('b') + ':0'
        state['labels'] = [{**annotation(name='Coinbase deposit'), 'kind': 'outpoint', 'value': stop}]
        mark_unspent(state, tx('c') + ':0')
        mark_unspent(state, tx('d') + ':0')
        before = deepcopy(state)
        graph = endpoint_graph(state, include_attributed_stops=True, include_unspent=True)
        matches = {entry['outpoint']: entry for entry in graph['pegouts']['endpoint_matches']}
        self.assertEqual(set(matches), {stop, tx('d') + ':0'})
        self.assertEqual(matches[stop]['kind'], 'attributed_stop')
        self.assertEqual(matches[stop]['boundary_status'], 'attributed_stop')
        self.assertEqual(matches[stop]['trace']['status'], 'spent')
        self.assertEqual(matches[stop]['attributions'][0]['name'], 'Coinbase deposit')
        rows = endpoint_table_rows(graph, state)
        row = next(row for row in rows if row['Outpoint'] == stop)
        self.assertEqual((row['Status'], row['Receiving Entity'], row['Attribution Confidence']),
                         ('Attributed_Stop', 'Coinbase deposit', 'suspected'))
        self.assertEqual(row['Attribution Source'], 'Supplied records <not HTML>')
        self.assertEqual(row['Boundary Status'], 'Explicit attribution stop')
        self.assertNotIn('tx:' + tx('c'), {node['id'] for node in graph['nodes']})
        self.assertEqual(state, before)
        legacy = endpoint_graph(state, include_unspent=True)
        self.assertEqual([entry['outpoint'] for entry in legacy['pegouts']['endpoint_matches']], [tx('d') + ':0'])

    def test_independent_seed_can_reach_beyond_a_stop_without_crossing_it(self):
        state = bitcoin_state((("a:0", "b"), ("b:0", "c"), ("d:0", "c")),
                              seeds=("a:0", "d:0"))
        stop = tx('b') + ':0'
        state['labels'] = [{**annotation(), 'kind': 'outpoint', 'value': stop}]
        mark_unspent(state, tx('c') + ':0')
        graph = endpoint_graph(state, include_attributed_stops=True, include_unspent=True)
        self.assertEqual(set(graph['pegouts']['outpoints']), {tx('a') + ':0', tx('d') + ':0'})
        rows = endpoint_table_rows(graph, state)
        end = next(row for row in rows if row['Outpoint'] == tx('c') + ':0')
        self.assertEqual(end['Source Seed Outpoints'], tx('d') + ':0')
        self.assertEqual({row['Outpoint'] for row in rows}, {stop, tx('c') + ':0'})

    def test_name_and_disabled_label_do_not_become_stops(self):
        for label in (annotation(stop=False), {**annotation(), 'enabled': False}):
            state = bitcoin_state((("a:0", "b"),), seeds=("a:0",), labels=[label])
            mark_unspent(state, tx('b') + ':0')
            graph = endpoint_graph(state, include_attributed_stops=True, include_unspent=True)
            self.assertEqual([entry['outpoint'] for entry in graph['pegouts']['endpoint_matches']], [tx('b') + ':0'])
            self.assertEqual(graph['pegouts']['endpoint_counts']['attributed_stop'], 0)

    def test_explicit_service_rule_qualifies_without_saved_label(self):
        state = bitcoin_state((("a:0", "b"),), seeds=("a:0",))
        rule = {'enabled': True, 'stop_tracing': True, 'name': '=Example', 'confidence': 'confirmed',
                'source': 'TRM analyst review', 'observed_at': '2026-10-05'}
        state['service_controls'] = {'rules': {'SYNTHETIC-a-address': rule}}
        original = deepcopy(state)
        graph = endpoint_graph(state, include_attributed_stops=True)
        rows = endpoint_table_rows(graph, state)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['Outpoint'], tx('a') + ':0')
        self.assertEqual(rows[0]['Hops from Seed'], 0)
        self.assertEqual(rows[0]['Status'], 'Attributed_Stop')
        self.assertEqual(state, original)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'endpoints.csv'
            write_endpoint_table_csv(target, graph, state)
            row, = list(csv.DictReader(io.StringIO(target.read_text())))
            self.assertEqual(row['Receiving Entity'], "'=Example")
        for changes in ({'enabled': False}, {'stop_tracing': False}, {'stop_tracing': None}):
            state['service_controls']['rules']['SYNTHETIC-a-address'] = {**rule, **changes}
            graph = endpoint_graph(state, include_attributed_stops=True)
            self.assertEqual(graph['pegouts']['endpoint_count'], 0)

    def test_option_is_strict_and_legacy_query_identity_is_unchanged(self):
        legacy = validate_query(tx('a'))
        self.assertEqual(legacy, validate_query(tx('a'), include_attributed_stops=False))
        self.assertNotIn('include_attributed_stops', legacy)
        for invalid in (0, 1, None, 'true'):
            with self.subTest(value=invalid), self.assertRaises(TraceError):
                validate_query(tx('a'), include_attributed_stops=invalid)
        for goal in ('full', 'connections'):
            with self.assertRaises(TraceError):
                _query(goal, bitcoin_state(seeds=("a:0",)), 0, 10, include_attributed_stops=True)

    def test_liquid_also_accepts_explicit_stops_without_fabricating_unspent(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",), labels=[annotation()])
        graph = endpoint_graph(state, include_attributed_stops=True)
        row, = endpoint_table_rows(graph, state)
        self.assertEqual(row['Network'], 'liquid')
        self.assertEqual(row['Status'], 'Attributed_Stop')
        self.assertEqual(row['Value BTC'], '')
        self.assertEqual(row['Spend Observation ID'], '')

    def test_source_network_mismatch_is_rejected(self):
        state = bitcoin_state(seeds=("a:0",))
        mark_unspent(state, tx('a') + ':0')
        graph = endpoint_graph(state, include_unspent=True)
        graph['blockchain'] = 'liquid'
        with self.assertRaisesRegex(TraceError, 'different networks'):
            pegout_csv_rows(graph, state)


if __name__ == '__main__':
    unittest.main()
