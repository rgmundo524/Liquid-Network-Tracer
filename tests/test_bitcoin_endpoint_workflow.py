"""Saved Bitcoin endpoint publication and CSV exports retain network identity."""
from copy import deepcopy
import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.combined_endpoint_csv import build_combined_endpoint_csv
from liquid_tracer.investigation_boards import generate_and_sync, list_boards
from liquid_tracer.investigations import create_investigation, read_case
from liquid_tracer.legend import legend_notes, legend_rows
from liquid_tracer.pegouts import search_pegouts, saved_pegout_snapshot
from liquid_tracer.plot_csv import build_plot_csv
from liquid_tracer.plots import preview_plot, reviewed_plot
from liquid_tracer.services import set_service
from tests.test_attribution_convergence import graph_state, tx
from tests.test_bitcoin_endpoints import bitcoin_state, endpoint_graph
from tests.test_board_workflow import BoardRemote
from tests.test_connections import saved_case
from tests.test_pegout_csv import set_value
from tests.test_pegout_paths import add_pegout, add_unspendable, mark_unspent


class BitcoinEndpointWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        layout = patch('liquid_tracer.elk_layout.optimize_graph', side_effect=lambda graph, **kwargs: graph)
        layout.start()
        self.addCleanup(layout.stop)

    def case(self, name='Bitcoin endpoints', blockchain='bitcoin', state=None):
        case = create_investigation(self.root, name, blockchain=blockchain, seeds=[tx('a') + ':0'])
        saved_case(case, state or bitcoin_state((("a:0", "b"), ("b:0", "c")), seeds=("a:0",)))
        return case

    @staticmethod
    def rows(case, preview_id):
        return list(csv.DictReader(io.StringIO(build_plot_csv(case, preview_id, 'endpoints.csv')['data'].decode())))

    def test_plot_sync_publishes_explicit_bitcoin_stop_and_preserves_board_identity_on_update(self):
        case = self.case()
        set_service(case, 'SYNTHETIC-b-address', name='Exchange deposit', confidence='suspected',
                    source='Analyst review', stop_tracing=True)
        transport = BoardRemote()
        first = generate_and_sync(case, 'pegouts', include_attributed_stops=True,
                                  token='synthetic-token', transport=transport, interval=0)
        self.assertTrue(first['published'])
        graph, _ = reviewed_plot(case, first['preview_id'])
        self.assertEqual(graph['blockchain'], 'bitcoin')
        self.assertTrue(graph['plot']['query']['include_attributed_stops'])
        self.assertEqual([row['outpoint'] for row in graph['pegouts']['endpoint_matches']], [tx('b') + ':0'])
        self.assertNotIn('tx:' + tx('c'), {node['id'] for node in graph['nodes']})
        row, = self.rows(case, first['preview_id'])
        self.assertEqual((row['Status'], row['Network'], row['Value BTC'], row['Value LBTC']),
                         ('Attributed_Stop', 'bitcoin', '1.23456789', ''))
        record, = list_boards(case)
        self.assertEqual(record['goal'], 'pegouts')
        self.assertIn('Endpoints', record['name'])
        before = read_json(case / record['state_file'])['items']
        updated = generate_and_sync(case, 'pegouts', include_attributed_stops=True,
                                    layout_mode='update', board_record_id=record['id'],
                                    token='synthetic-token', transport=transport, interval=0)
        self.assertEqual(updated['board_id'], first['board_id'])
        self.assertEqual(len(transport.creations), 1)
        after = read_json(case / record['state_file'])['items']
        self.assertEqual({key: value['id'] for key, value in before.items()},
                         {key: value['id'] for key, value in after.items()})

    def test_combined_export_keeps_bitcoin_and_liquid_amounts_and_provenance_separate(self):
        bitcoin = bitcoin_state((("a:0", "b"),), seeds=("a:0",))
        mark_unspent(bitcoin, tx('b') + ':0')
        btc_case = self.case(state=bitcoin)
        btc_plot = preview_plot(btc_case, 'pegouts', include_unspent=True, include_attributed_stops=True)
        liquid = graph_state((("a:0", "b"),), seeds=("a:0",))
        set_value(liquid, add_pegout(liquid, tx('b')), 125000001)
        lbtc_case = self.case('Liquid endpoints', 'liquid', liquid)
        lbtc_plot = preview_plot(lbtc_case, 'pegouts')
        cases = {read_case(case)['case_id']: case for case in (btc_case, lbtc_case)}
        selections = [{'case_id': identity, 'run_id': 'latest'} for identity in cases]
        with patch('liquid_tracer.api.Esplora.get', side_effect=AssertionError('No refetch')):
            product = build_combined_endpoint_csv(selections, lambda identity: (cases[identity], read_case(cases[identity])))
        rows = list(csv.DictReader(io.StringIO(product['csv'])))
        self.assertEqual(product['endpoint_count'], 2)
        btc, lbtc = rows
        self.assertEqual((btc['Network'], btc['Asset'], btc['Value BTC'], btc['Value LBTC'], btc['Pegout LBTC']),
                         ('bitcoin', 'BTC', '1.23456789', '', ''))
        self.assertEqual((lbtc['Network'], lbtc['Asset'], lbtc['Value BTC'], lbtc['Pegout LBTC']),
                         ('liquid', 'L-BTC', '', '1.25000001'))
        self.assertEqual(btc['Plot ID'], btc_plot['preview_id'])
        self.assertEqual(lbtc['Plot ID'], lbtc_plot['preview_id'])
        self.assertEqual(btc['Include Attributed Stops'], 'true')
        self.assertEqual(lbtc['Include Attributed Stops'], 'false')

    def test_legacy_bitcoin_search_uses_real_fixture_collection_and_exports_endpoints(self):
        state = bitcoin_state((("a:0", "b"),), seeds=("a:0",))
        add_unspendable(state, tx('b'))
        fixture = {}
        for identity, record in state['transactions'].items():
            transaction = deepcopy(record['data'])
            for output in transaction['vout']:
                output.pop('asset', None)
            fixture['/tx/' + identity] = transaction
            fixture['/tx/' + identity + '/outspends'] = [{'spent': False} for _ in transaction['vout']]
        for identity, transaction in list(fixture.items()):
            if not isinstance(transaction, dict):
                continue
            for index, vin in enumerate(transaction['vin']):
                fixture['/tx/' + vin['txid'] + '/outspends'][vin['vout']] = {
                    'spent': True, 'txid': transaction['txid'], 'vin': index, 'status': transaction['status']}
        fixture_file = self.root / 'bitcoin-fixture.json'
        save_json(fixture_file, fixture)
        case = create_investigation(self.root, 'Bitcoin live endpoint search', blockchain='bitcoin',
                                    fixture=fixture_file, seeds=state['seeds'])
        result = search_pegouts(case, max_hops=3, include_unspent=True, include_unspendable=True,
                               max_transactions=20, max_outpoints=100, max_requests=100)
        graph, _, evidence = saved_pegout_snapshot(case, result['preview_id'])
        self.assertEqual(evidence['blockchain'], 'bitcoin')
        self.assertEqual(evidence['status'], 'bounded_complete')
        self.assertEqual(graph['pegouts']['matches'], [])
        rows = self.rows(case, result['preview_id'])
        self.assertEqual({row['Status'] for row in rows}, {'Dormant', 'OP_Return'})
        self.assertTrue(all(row['Network'] == 'bitcoin' and not row['Value LBTC'] for row in rows))

    def test_legacy_liquid_endpoint_legend_preserves_saved_plan_notes(self):
        state = graph_state(seeds=("a:0",))
        state["status"] = "bounded_complete"
        graph = endpoint_graph(state)
        graph.pop('blockchain', None)  # Older archived graphs have no network field.
        notes = legend_notes(graph)
        start = next(index for index, note in enumerate(notes) if note.startswith('Peg-out search:'))
        self.assertEqual(notes[start:start + 5], [
            'Peg-out search: 1 selected seed UTXO(s) from 1 starting transaction(s).',
            'Range: 0 to 10 transaction hops, inclusive. Each starting transaction is hop 0.',
            'Only qualifying paths are plotted. Their combined edges can also form routes outside the selected range.',
            'Peg-out diamonds are Liquid requests, not confirmation of Bitcoin payouts.',
            'Coverage: bounded search completed. Stopped, unconfirmed or unsearched branches may contain undiscovered peg-outs; no result does not prove absence.',
        ])

    def test_endpoint_legend_explains_bitcoin_boundaries_without_pegout_claims(self):
        state = bitcoin_state(seeds=("a:0",))
        state['service_controls'] = {'rules': {'SYNTHETIC-a-address': {'enabled': True, 'stop_tracing': True}}}
        graph = endpoint_graph(state, include_attributed_stops=True)
        notes = ' '.join(legend_notes(graph))
        self.assertIn('Endpoint search', notes)
        self.assertIn('explicit attribution stops', notes)
        self.assertIn('A tracing boundary does not establish that its output is unspent', notes)
        self.assertNotIn('peg-out', notes.lower())
        self.assertFalse(any('peg-out' in row['description'].lower() for row in legend_rows(graph)))


if __name__ == '__main__':
    unittest.main()
