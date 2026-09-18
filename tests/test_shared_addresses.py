"""Unique address presentation must never replace UTXO-level tracing semantics."""
import copy
import unittest

from liquid_tracer.export import build_graph, short
from liquid_tracer.miro import make_plan
from tests.fixtures import A, B, C, X, output
from tests.test_layout import state_from


class SharedAddressTests(unittest.TestCase):
    def shared(self):
        address = 'SYNTHETIC-shared-receiver'
        txs = {key: {'txid': key, 'vin': [], 'vout': [output(address)], 'status': {}}
               for key in (A, B)}
        state = state_from(txs)
        state['seeds'] = [A + ':0', B + ':0']
        return state, address

    def test_two_starting_transactions_share_one_circle_and_keep_two_edges(self):
        state, address = self.shared()
        original = copy.deepcopy(state)
        graph = build_graph(state)
        self.assertEqual(graph['address_mode'], 'merged')
        addresses = [n for n in graph['nodes'] if n['kind'] == 'address']
        self.assertEqual(len(addresses), 1)
        self.assertEqual(addresses[0]['details']['address'], address)
        edges = graph['edges']
        self.assertEqual(len(edges), 2)
        self.assertEqual({e['target'] for e in edges}, {addresses[0]['id']})
        self.assertEqual({e['outpoint'] for e in edges}, {A + ':0', B + ':0'})
        self.assertEqual({o['outpoint'] for o in addresses[0]['details']['occurrences']}, {A + ':0', B + ':0'})
        self.assertEqual(len(graph['activity_frames']['activities']), 1)
        plan = make_plan(graph)
        self.assertEqual(sum(s['body']['data']['shape'] == 'circle' for s in plan['shapes']), 1)
        self.assertEqual(len(plan['connectors']), 2)
        self.assertEqual(state, original)

    def test_same_transaction_multiple_outputs_keep_parallel_connectors(self):
        state, address = self.shared()
        state['transactions'][A]['data']['vout'].append(output(address))
        graph = build_graph(state)
        self.assertEqual(sum(n['kind'] == 'address' for n in graph['nodes']), 1)
        self.assertEqual(len(graph['edges']), 3)
        self.assertEqual(len({e['id'] for e in graph['edges']}), 3)

    def test_same_display_label_is_not_an_identity(self):
        state, _ = self.shared()
        addresses = ['SYNTHETIC-' + middle + '-abcdefg' for middle in ('11111', '22222')]
        self.assertEqual(short(addresses[0]), short(addresses[1]))
        for key, address in zip((A, B), addresses):
            state['transactions'][key]['data']['vout'][0] = output(address)
        graph = build_graph(state)
        self.assertEqual(sum(n['kind'] == 'address' for n in graph['nodes']), 2)

    def test_unknown_addresses_never_merge_by_missing_or_shared_script(self):
        state, _ = self.shared()
        for record in state['transactions'].values():
            record['data']['vout'][0].pop('scriptpubkey_address')
        graph = build_graph(state)
        self.assertEqual(sum(n['kind'] == 'address' for n in graph['nodes']), 2)

    def test_network_identity_and_case_sensitive_addresses_are_distinct(self):
        state, address = self.shared()
        state['transactions'][A]['data']['vin'] = [{'txid': X, 'vout': 0, 'is_pegin': True,
                                                    'prevout': output(address)}]
        graph = build_graph(state)
        nodes = [n for n in graph['nodes'] if n['kind'] == 'address']
        self.assertEqual({n['details']['network'] for n in nodes}, {'bitcoin', 'liquid'})
        state['transactions'][B]['data']['vout'][0] = output(address.upper())
        self.assertEqual(sum(n['kind'] == 'address' for n in build_graph(state)['nodes']), 3)

    def test_reuse_across_hops_does_not_manufacture_spend_links(self):
        state, address = self.shared()
        state['transactions'][C] = {'data': {'txid': C, 'vin': [
            {'txid': A, 'vout': 0, 'prevout': output(address)}],
            'vout': [output(address)], 'status': {}}, 'depth': 1, 'observation_id': C}
        original = copy.deepcopy(state)
        graph = build_graph(state)
        self.assertEqual(sum(n['kind'] == 'address' for n in graph['nodes']), 1)
        self.assertEqual(state, original)
        self.assertEqual(state['links'], {})
        self.assertFalse(any(e['role'] == 'traced_input' for e in graph['edges']))
        legacy = build_graph(state, merge_addresses=False)
        self.assertEqual({(e['id'], e['outpoint'], e['role']) for e in graph['edges']},
                         {(e['id'], e['outpoint'], e['role']) for e in legacy['edges']})
