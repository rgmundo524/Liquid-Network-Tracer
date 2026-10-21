"""General UTXO display bundles preserve evidence, layout and Miro migrations."""
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import LBTC, TraceError, read_json
from liquid_tracer.context_connectors import display_graph, evidence_graph, prepare, restore_graph, summaries, display_edge_ids
from liquid_tracer.export import build_graph
from liquid_tracer.edge_labels import caption_text
from liquid_tracer.elk_layout import optimize_graph, fallback_graph, _request_graph
from liquid_tracer.hub_layout import hub_layout_view, hub_plan
from liquid_tracer.input_order import input_orders
from liquid_tracer.layout_preview import export_layout, render_svg
from liquid_tracer.layout_reuse import _fingerprint
from liquid_tracer.mermaid import mermaid_source
from liquid_tracer.miro import make_plan, validate_plan, sync
from liquid_tracer.trace_layout import trace_structure
from liquid_tracer.transaction_csv import transaction_csv_rows
from tests.fixtures import output
from tests.test_layout import state_from, txid
from tests.test_elk_layout import synthetic_candidate, HAS_ELK
from tests.test_context_connector_integration import svg_edge_ids, evidence
from tests.test_dense_context_miro import DenseMiro
from tests.test_context_parallel_miro import checksum
from tests.legacy_context import build_graph as legacy_graph
from tests.test_context_parallel_connectors import repeated_state


def state_for(chain='liquid'):
    a, b, c = map(txid, ('bundle-a', 'bundle-b', 'bundle-c'))
    def coin(address, value):
        result = output(address)
        result.pop('valuecommitment', None)
        result.pop('assetcommitment', None)
        result.update(value=value)
        if chain == 'liquid':
            result['asset'] = LBTC
        else:
            result.pop('asset', None)
        return result
    source = [coin('SYNTHETIC-source', 100_000_001), coin('SYNTHETIC-source', 200_000_002)]
    vins = [{'txid': a, 'vout': i, 'prevout': deepcopy(value)} for i, value in enumerate(source)]
    vins.append({'txid': c, 'vout': 0, 'prevout': coin('SYNTHETIC-source', 5)})
    vins.append({'txid': c, 'vout': 1, 'prevout': coin('SYNTHETIC-context', 7)})
    state = state_from({a: {'txid': a, 'vin': [], 'vout': source, 'status': {}},
                        b: {'txid': b, 'vin': vins, 'vout': [coin('SYNTHETIC-dest', 1),
                                                           coin('SYNTHETIC-dest', 2)], 'status': {}}})
    state.update(blockchain=chain, run_id='1234567890abcdef', ancestor_runs=[], seeds=[a + ':0'])
    state['links'] = {f'{a}:{i}': {'spending_txid': b, 'vin': i} for i in range(2)}
    return state


class UtxoConnectorTests(unittest.TestCase):
    def test_both_chains_bundle_inputs_and_outputs_without_changing_evidence(self):
        for chain in ('liquid', 'bitcoin'):
            with self.subTest(chain=chain):
                state = state_for(chain)
                before = deepcopy(state)
                graph = build_graph(state)
                self.assertEqual(graph['context_connectors']['version'], 3)
                self.assertEqual(len(graph['edges']), 8)
                self.assertEqual(len(summaries(graph)), 3)
                displayed = display_graph(graph)
                self.assertEqual(len(displayed['edges']), 4)
                self.assertEqual(evidence_graph(displayed)['edges'], graph['edges'])
                self.assertEqual(restore_graph(displayed, graph), graph)
                self.assertEqual(transaction_csv_rows(evidence_graph(displayed), state), transaction_csv_rows(graph, state))
                self.assertEqual(state, before)
                bundles = summaries(graph)
                entry = next(edge for edge in bundles if edge['details']['context_summary']['direction'] == 'input')
                self.assertEqual(entry['details']['context_summary']['role_counts'], {'context_input': 1, 'traced_input': 2})
                self.assertIn('2 traced, 1 context', caption_text(entry))
                self.assertEqual(entry['quantity'], '3.00000008 ' + ('BTC' if chain == 'bitcoin' else 'L-BTC'))
                self.assertIsNone(entry['outpoint'])
                self.assertEqual(len({(edge['source'], edge['target']) for edge in displayed['edges']}), 4)
                validate_plan(make_plan(graph))

    def test_confidential_mixed_asset_and_missing_values_never_produce_partial_total(self):
        for mode in ('hidden_value', 'hidden_asset', 'mixed_asset', 'boolean_value'):
            state = state_for()
            value = state['transactions'][txid('bundle-b')]['data']['vout'][1]
            if mode == 'hidden_value': value.pop('value')
            elif mode == 'hidden_asset': value.pop('asset')
            elif mode == 'mixed_asset': value['asset'] = 'ab' * 32
            else: value['value'] = True
            graph = build_graph(state)
            bundle = next(edge for edge in summaries(graph) if edge['source'] == 'tx:' + txid('bundle-b'))
            self.assertEqual(bundle['quantity'], '')
            self.assertIn('total unavailable', caption_text(bundle))
        state = state_for('bitcoin')
        for value in state['transactions'][txid('bundle-b')]['data']['vout']:
            value['value'] = 2**53 + 1
        graph = build_graph(state)
        bundle = next(edge for edge in summaries(graph) if edge['source'] == 'tx:' + txid('bundle-b'))
        self.assertEqual(bundle['quantity'], '180143985.09481986 BTC')

    def test_unknown_addresses_distinct_pairs_and_utxo_occurrences_are_not_merged(self):
        state = state_for()
        unknown = state['transactions'][txid('bundle-b')]['data']['vout']
        for value in unknown: value.pop('scriptpubkey_address')
        self.assertEqual(len(summaries(build_graph(state))), 2)
        state = state_for()
        values = state['transactions'][txid('bundle-b')]['data']['vout']
        values[0]['scriptpubkey_address'] = 'abcdef-one-1234'
        values[1]['scriptpubkey_address'] = 'abcdef-two-1234'
        self.assertEqual(len(summaries(build_graph(state))), 2)
        self.assertEqual(summaries(build_graph(state_for(), merge_addresses=False)), [])

    def test_tampering_fails_and_legacy_previews_are_not_upgraded(self):
        graph = build_graph(state_for())
        for field, value in (('quantity','999 BTC'), ('source','forged'), ('role','context_input')):
            changed = deepcopy(graph)
            changed['context_connectors']['summaries'][0][field] = value
            with self.assertRaises(TraceError): display_graph(changed)
        changed = deepcopy(graph)
        changed['context_connectors']['summaries'][0]['details']['context_summary']['member_edge_ids'].pop()
        with self.assertRaises(TraceError): display_graph(changed)
        with self.assertRaises(TraceError): display_graph(json.loads(json.dumps(display_graph(graph))))
        legacy = legacy_graph(repeated_state(), group_context_inputs=True)
        before = deepcopy(make_plan(legacy))
        self.assertEqual(prepare(legacy), legacy)
        self.assertEqual(make_plan(restore_graph(display_graph(legacy), legacy)), before)

    def test_grouping_toggle_does_not_disable_parallel_utxo_bundles(self):
        for enabled in (False, True):
            graph = build_graph(state_for(), group_context_inputs=enabled)
            self.assertEqual(len(summaries(graph)), 3)

    def test_trace_spine_input_order_and_hubs_use_exact_members(self):
        graph = build_graph(state_for('bitcoin'))
        graph['graph_options']['layout_style'] = 'trace'
        displayed = display_graph(graph)
        original = trace_structure(graph)
        result = trace_structure(displayed)
        self.assertIn('tx:' + txid('bundle-b'), result['spine'])
        self.assertEqual(result['spine'], original['spine'])
        ids = display_edge_ids(displayed)
        self.assertEqual(result['edges'], {ids.get(key,key) for key in original['edges']})
        order = input_orders(displayed)['tx:' + txid('bundle-b')]
        self.assertEqual(len(order), 2)
        self.assertTrue(order[0].startswith('context-parallel:'))
        node = next(node for node in graph['nodes'] if node['id'].endswith('SYNTHETIC-source'))
        node['layout_hub'] = True
        displayed = display_graph(graph)
        self.assertEqual(hub_plan(displayed), hub_plan(graph))
        self.assertEqual(len(evidence_graph(hub_layout_view(displayed))['edges']), len(graph['edges']))
        # Reusing the address for other outpoints must not extend the spine.
        state = state_for()
        for vin in state['transactions'][txid('bundle-b')]['data']['vin'][:2]: vin['txid'] = txid('unrelated')
        graph = build_graph(state)
        graph['graph_options']['layout_style'] = 'trace'
        self.assertNotIn('tx:' + txid('bundle-b'), trace_structure(display_graph(graph))['spine'])

    def test_layout_svg_mermaid_miro_and_details_share_one_display_inventory(self):
        for chain in ('liquid', 'bitcoin'):
            graph = build_graph(state_for(chain))
            with patch('liquid_tracer.elk_layout._worker', side_effect=synthetic_candidate):
                result = optimize_graph(graph, connector_style='straight', layout_attempts=1)
            self.assertEqual(_fingerprint(graph), _fingerprint(result))
            self.assertEqual(evidence(result), evidence(graph))
            self.assertEqual(transaction_csv_rows(result, state_for(chain)), transaction_csv_rows(graph, state_for(chain)))
            visible = {edge['id'] for edge in display_graph(result)['edges']}
            self.assertEqual(svg_edge_ids(render_svg(result)), visible)
            self.assertEqual({edge['id'] for edge in _request_graph(graph)[0]['edges']}, visible)
            for item in summaries(result): self.assertIn(caption_text(item), mermaid_source(result))
            plan = make_plan(result)
            validate_plan(plan)
            self.assertEqual({edge['key'] for edge in plan['connectors']}, visible)
            self.assertTrue(all(proof['version']==2 for proof in plan['context_parallel_items'].values()))
            validate_plan(make_plan(fallback_graph(graph)))
            with tempfile.TemporaryDirectory() as directory:
                files = export_layout(result, Path(directory)/'preview')
                text = Path(files['details']).read_text()
                self.assertIn('Each member retains its traced or context role', text)
                members = {key for item in summaries(result)
                           for key in item['details']['context_summary']['member_edge_ids']}
                for edge in graph['edges']:
                    self.assertIn(edge['id'], text)
                    if edge['id'] in members:
                        self.assertIn(edge['outpoint'], text)

    def test_miro_roundtrip_replaces_only_connectors_and_preserves_shapes(self):
        for chain in ('liquid', 'bitcoin'):
            with tempfile.TemporaryDirectory() as directory:
                graph = build_graph(state_for(chain))
                graph.pop('activity_frames')
                plain = deepcopy(graph)
                plain.pop('context_connectors')
                plain_plan, bundled = make_plan(plain), make_plan(graph)
                remote, path = DenseMiro(), Path(directory)/'miro.json'
                def send(plan):
                    return sync(plan, 'test-board', path, token='test-token', interval=0,
                                reorganize=True, transport=remote)
                send(plain_plan)
                shapes = {k:v['id'] for k,v in read_json(path)['items'].items() if v['endpoint']=='shapes'}
                for plan in (bundled, plain_plan, bundled):
                    send(plan)
                    mapping = read_json(path)['items']
                    self.assertEqual({k:v['id'] for k,v in mapping.items() if v['endpoint']=='shapes'}, shapes)
                    self.assertEqual({k for k,v in mapping.items() if v['endpoint']=='connectors'},
                                     {item['key'] for item in plan['connectors']})
                    writes = len(remote.writes)
                    send(plan)
                    self.assertEqual(len(remote.writes), writes)
                key = next(iter(bundled['context_parallel_items']))
                record = read_json(path)['items'][key]
                remote.items[record['id']]['captions'] = [{'content':'Keep this note', 'position':'50%'}]
                writes = len(remote.writes)
                with self.assertRaises(TraceError): send(plain_plan)
                self.assertEqual(len(remote.writes), writes)

    def test_output_proof_cannot_forge_outpoints_direction_or_membership(self):
        graph = build_graph(state_for())
        graph.pop('activity_frames')
        plan = make_plan(graph)
        key = next(k for k,v in plan['context_parallel_items'].items() if v['direction']=='output')
        for mutation in ('outpoint','direction','missing','role'):
            candidate = deepcopy(plan)
            proof = candidate['context_parallel_items'][key]
            member = next(iter(proof['inputs']))
            if mutation=='outpoint': proof['inputs'][member]['outpoint'] = 'forged:0'
            elif mutation=='direction': proof['direction']='input'
            elif mutation=='role': proof['inputs'][member]['role']='traced_input'
            else: proof['inputs'].pop(member)
            with self.assertRaises(TraceError): validate_plan(checksum(candidate))


    def test_legacy_context_bundle_upgrades_to_all_utxos_without_changing_shapes(self):
        state = repeated_state()
        old = legacy_graph(state, group_context_inputs=True)
        new = build_graph(state, group_context_inputs=True)
        for graph in (old, new):
            graph.pop('activity_frames')
            graph['run']['ancestor_runs'] = []
        with tempfile.TemporaryDirectory() as directory:
            path, remote = Path(directory)/'miro.json', DenseMiro()
            def send(graph):
                return sync(make_plan(graph), 'test-board', path, token='test-token', interval=0,
                            reorganize=True, transport=remote)
            send(old)
            before = {key:record['id'] for key, record in read_json(path)['items'].items()
                      if record['endpoint']=='shapes'}
            send(new)
            mapping = read_json(path)['items']
            self.assertEqual({key:record['id'] for key, record in mapping.items() if record['endpoint']=='shapes'}, before)
            key, = make_plan(new)['context_parallel_items']
            self.assertEqual(len(mapping[key]['context_parallel_proof']['inputs']), 5)
            self.assertEqual(mapping[key]['context_parallel_proof']['version'], 2)
            send(old)
            self.assertEqual(read_json(path)['items'][key]['context_parallel_proof']['version'], 1)

    def test_change_designation_is_not_applied_to_an_entire_mixed_bundle(self):
        from liquid_tracer.change_layout import apply_change_layout
        state = state_for()
        state['service_controls'] = {'change_outputs': {txid('bundle-a'): {'vout': 0, 'notes': 'Synthetic'}}}
        graph = build_graph(state)
        entry = next(edge for edge in summaries(graph) if edge['source']=='tx:' + txid('bundle-a'))
        self.assertIn('1 designated change', entry['label'])
        self.assertNotIn('change_output', entry)
        self.assertEqual(sum(bool(edge.get('change_output')) for edge in graph['edges']), 1)
        projected = display_graph(graph)
        apply_change_layout(projected)
        skipped = projected['layout']['change_outputs']['skipped']
        self.assertEqual(len(skipped), 1)
        self.assertIn('shared by other outpoints', skipped[0]['reason'])

    def test_duplicate_and_cross_chain_inputs_are_not_summed(self):
        state = state_for()
        vins = state['transactions'][txid('bundle-b')]['data']['vin']
        vins[1]['vout'] = 0
        graph = build_graph(state)
        self.assertFalse(any(edge['details']['context_summary']['direction']=='input' for edge in summaries(graph)))
        state = state_for()
        vins = state['transactions'][txid('bundle-b')]['data']['vin']
        for vin in vins[:3]: vin['is_pegin'] = True
        graph = build_graph(state)
        self.assertFalse(any(edge['details']['context_summary']['direction']=='input' for edge in summaries(graph)))

    @unittest.skipUnless(HAS_ELK, 'Pinned local ELK is unavailable')
    def test_real_elk_accepts_mixed_inputs_bundled_into_one_connection(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict('os.environ', {
            'XDG_CACHE_HOME': temporary, 'XDG_STATE_HOME': temporary,
        }))
        for chain in ('bitcoin', 'liquid'):
            with self.subTest(chain=chain):
                state = state_for(chain)
                child = 'tx:' + txid('bundle-b')
                # Keep the two traced inputs and one context input from the
                # same address; remove the separate context-address input.
                state['transactions'][txid('bundle-b')]['data']['vin'].pop()
                graph = build_graph(state)
                self.assertEqual(len(input_orders(graph)[child]), 3)
                displayed = display_graph(graph)
                self.assertEqual(sum(edge['target'] == child for edge in displayed['edges']), 1)
                result = optimize_graph(graph, layout_attempts=1)
                self.assertEqual(evidence(result), evidence(graph))
                self.assertEqual({node['id'] for node in result['nodes']},
                                 {node['id'] for node in graph['nodes']})
                self.assertEqual(len(display_graph(result)['edges']), 3)

    @unittest.skipUnless(HAS_ELK, 'Pinned local ELK is unavailable')
    def test_real_elk_handles_small_input_and_output_bundles_on_both_chains(self):
        for chain in ('bitcoin', 'liquid'):
            graph = build_graph(state_for(chain))
            graph['graph_options']['layout_style'] = 'trace'
            result = optimize_graph(graph, layout_attempts=1)
            self.assertEqual(evidence(result), evidence(graph))
            self.assertEqual(len(display_graph(result)['edges']), 4)
            validate_plan(make_plan(result))
            self.assertTrue(render_svg(result).startswith(b'<svg'))
