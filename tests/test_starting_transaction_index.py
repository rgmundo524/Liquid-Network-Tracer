"""Chronological display numbers never change frame identity or UTXO evidence."""
import copy
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.common import TraceError, read_json
from liquid_tracer.export import build_graph, svg_graph
from liquid_tracer.miro import make_plan, sync, sync_frames, validate_plan
from liquid_tracer.miro_frames import activity_frames, validate_activity_frames
from tests.fixtures import A, B, C, output
from tests.test_layout import state_from
from tests.test_miro_frame_groups import edge, node, three_trees
from tests.test_miro_frame_sync import FrameMiro, framed_graph
from tests.test_elk_layout import HAS_ELK


def dated_graph():
    graph = three_trees()
    for item, stamp in zip(graph['nodes'][:3], (300, 100, 200)):
        item['details']['transaction'] = {'status': {'confirmed': True, 'block_time': stamp}}
    graph['edges'].append(edge('join:a:c', 'output:a', 'tx:c'))
    return graph


def validate(graph, metadata):
    validate_activity_frames(metadata, [n['id'] for n in graph['nodes']],
                             [{'key': e['id'], 'source': e['source'], 'target': e['target']}
                              for e in graph['edges']])


def dated_state():
    txs = {key: {'txid': key, 'vin': [], 'vout': [output('SYNTHETIC-shared')],
                 'status': {'confirmed': True, 'block_time': stamp}}
           for key, stamp in ((A, 300), (B, 100), (C, 200))}
    state = state_from(txs)
    state['seeds'] = [A + ':0', B + ':0', C + ':0']
    state['ancestor_runs'] = []
    return state


class StartingTransactionIndexTests(unittest.TestCase):
    def test_global_time_index_and_comma_separated_members(self):
        graph = dated_graph()
        original = copy.deepcopy(graph)
        metadata = activity_frames(graph)
        self.assertEqual(metadata['schema_version'], 2)
        self.assertEqual(metadata['starting_transactions'], [
            {'key': 'tx:b', 'index': 1, 'block_time': 100},
            {'key': 'tx:c', 'index': 2, 'block_time': 200},
            {'key': 'tx:a', 'index': 3, 'block_time': 300},
        ])
        self.assertEqual([g['title'] for g in metadata['activities']], [
            'Activity 1 · Starting transactions 2, 3',
            'Activity 2 · Starting transaction 1',
        ])
        validate(graph, metadata)
        self.assertEqual(graph, original)

    def test_input_order_and_seed_order_do_not_change_numbers(self):
        graph = dated_graph()
        graph['run'] = {'seeds': ['a:0', 'b:0', 'c:0']}
        expected = activity_frames(graph)
        graph['nodes'].reverse()
        graph['edges'].reverse()
        graph['run']['seeds'].reverse()
        self.assertEqual(activity_frames(graph), expected)

    def test_equal_times_use_full_key_not_input_order(self):
        graph = dated_graph()
        for item in graph['nodes'][:3]:
            item['details']['transaction']['status']['block_time'] = 100
        self.assertEqual([r['key'] for r in activity_frames(graph)['starting_transactions']],
                         ['tx:a', 'tx:b', 'tx:c'])

    def test_unknown_and_unconfirmed_times_are_last(self):
        graph = dated_graph()
        graph['nodes'][0]['details']['transaction']['status'] = {'confirmed': False, 'block_time': 1}
        graph['nodes'][1]['details']['transaction']['status'] = {'confirmed': True}
        metadata = activity_frames(graph)
        self.assertEqual(metadata['starting_transactions'], [
            {'key': 'tx:c', 'index': 1, 'block_time': 200},
            {'key': 'tx:a', 'index': 2, 'block_time': None},
            {'key': 'tx:b', 'index': 3, 'block_time': None},
        ])
        validate(graph, metadata)

    def test_invalid_timestamps_are_not_treated_as_known_dates(self):
        for value in (True, False, -1, 1.5, float('inf'), float('nan'), '100', 253402300800):
            graph = dated_graph()
            graph['nodes'][0]['details']['transaction']['status']['block_time'] = value
            with self.subTest(timestamp=value):
                catalog = activity_frames(graph)['starting_transactions']
                self.assertEqual(catalog[-1], {'key': 'tx:a', 'index': 3, 'block_time': None})
        graph['nodes'][0]['details']['transaction']['status']['block_time'] = 0
        self.assertEqual(activity_frames(graph)['starting_transactions'][0]['key'], 'tx:a')

    def test_multiple_seed_outputs_count_once_and_descendants_are_excluded(self):
        graph = dated_graph()
        for item in graph['nodes']:
            item.pop('role', None)
        graph['run'] = {'seeds': ['a:0', 'a:1', 'b:0', 'missing:0']}
        catalog = activity_frames(graph)['starting_transactions']
        self.assertEqual([r['key'] for r in catalog], ['tx:b', 'tx:a'])
        self.assertEqual([r['index'] for r in catalog], [1, 2])

    def test_frame_identity_and_memberships_are_unchanged_from_legacy(self):
        graph = dated_graph()
        new = activity_frames(graph)
        old = activity_frames(graph, indexed=False)
        self.assertEqual(new['outer'], old['outer'])
        for before, after in zip(old['activities'], new['activities']):
            self.assertEqual({k: v for k, v in before.items() if k != 'title'},
                             {k: v for k, v in after.items() if k != 'title'})
        self.assertEqual(old['schema_version'], 1)
        self.assertNotIn('starting_transactions', old)
        self.assertEqual(old['activities'][0]['title'], 'Activity 1 · 2 starting transactions')
        validate(graph, old)

    def test_legacy_titles_still_require_exact_valid_partition(self):
        graph = dated_graph()
        old = activity_frames(graph, indexed=False)
        old['activities'][0]['title'] = 'Activity 1 · Starting transactions 2, 3'
        with self.assertRaises(TraceError):
            validate(graph, old)

    def test_incorrect_or_incomplete_catalogs_are_rejected(self):
        graph = dated_graph()
        mutations = [
            lambda m: m['starting_transactions'].pop(),
            lambda m: m['starting_transactions'].reverse(),
            lambda m: m['starting_transactions'][0].update(index=True),
            lambda m: m['starting_transactions'][0].update(index=2),
            lambda m: m['starting_transactions'][0].update(key='tx:a'),
            lambda m: m['starting_transactions'][0].update(block_time=True),
            lambda m: m['starting_transactions'][0].update(block_time=-1),
            lambda m: m['starting_transactions'][0].update(block_time='100'),
            lambda m: m['starting_transactions'][0].update(block_time=999),
            lambda m: m['starting_transactions'][0].update(extra='untrusted'),
            lambda m: m['activities'][0].update(title='Activity 1 · Starting transactions 1, 3'),
            lambda m: m.update(schema_version=3),
        ]
        for mutate in mutations:
            metadata = activity_frames(graph)
            mutate(metadata)
            with self.subTest(mutation=mutate), self.assertRaises(TraceError):
                validate(graph, metadata)

    def test_no_starts_or_empty_graph_have_explicit_metadata(self):
        graph = {'nodes': [node('out:alone')], 'edges': []}
        metadata = activity_frames(graph)
        self.assertEqual(metadata['starting_transactions'], [])
        self.assertEqual(metadata['activities'][0]['title'], 'Activity 1 · No starting transactions')
        validate(graph, metadata)
        graph = {'nodes': [], 'edges': []}
        validate(graph, activity_frames(graph))

    def test_continuation_without_new_starts_retains_global_numbers(self):
        graph = dated_graph()
        expected = activity_frames(graph)['starting_transactions']
        graph['nodes'].append(node('tx:earliest-descendant'))
        graph['nodes'][-1]['details']['transaction'] = {'status': {'confirmed': True, 'block_time': 1}}
        graph['edges'].append(edge('later-hop', 'output:a', 'tx:earliest-descendant'))
        self.assertEqual(activity_frames(graph)['starting_transactions'], expected)

    def test_transaction_labels_and_plan_use_same_numbers_without_evidence_edits(self):
        state = dated_state()
        before = copy.deepcopy(state)
        graph = build_graph(state)
        self.assertEqual(state, before)
        for key, index in ((A, 3), (B, 1), (C, 2)):
            tx = next(n for n in graph['nodes'] if n['id'] == 'tx:' + key)
            self.assertEqual(tx['starting_transaction_index'], index)
            self.assertTrue(tx['label'].startswith(f'Starting TX {index}\n'))
            self.assertIn(f'Starting TX {index}', svg_graph(graph))
        self.assertEqual(graph['activity_frames']['activities'][0]['title'],
                         'Activity 1 · Starting transactions 1, 2, 3')
        plan = make_plan(graph)
        validate_plan(plan)
        self.assertEqual(plan['activity_frames'], graph['activity_frames'])
        self.assertEqual(len(plan['connectors']), 3)

    @unittest.skipUnless(HAS_ELK, 'Requires the local ELK worker')
    def test_index_survives_real_elk_and_compaction(self):
        from liquid_tracer.elk_layout import optimize_graph
        from liquid_tracer.compaction import compact_graph
        from liquid_tracer.layout_preview import render_svg
        graph = build_graph(dated_state())
        arranged = compact_graph(optimize_graph(graph))
        self.assertEqual(arranged['activity_frames'], graph['activity_frames'])
        self.assertIn(b'Starting TX 1', render_svg(arranged))
        validate_plan(make_plan(arranged))


class ChronologicalFrameSyncTests(unittest.TestCase):
    def test_legacy_frames_rename_in_place_and_manual_titles_survive(self):
        graph = framed_graph()
        graph['activity_frames'] = activity_frames(graph, indexed=False)
        old_plan = make_plan(graph)
        validate_plan(old_plan)
        untouched = copy.deepcopy(old_plan)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            remote = FrameMiro()
            options = {'token': 'synthetic-token', 'transport': remote, 'interval': 0}
            sync(old_plan, 'board=', path, **options)
            sync_frames(old_plan, 'board=', path, **options)
            original = read_json(path)
            keys = [g['key'] for g in graph['activity_frames']['activities']]
            manual_id = original['items'][keys[0]]['id']
            remote.items[manual_id]['data']['title'] = 'Analyst-reviewed title'
            graph['activity_frames'] = activity_frames(graph)
            sync(make_plan(graph), 'board=', path, max_items=0, **options)
            report = sync_frames(make_plan(graph), 'board=', path, max_items=0, **options)
            current = read_json(path)
            self.assertEqual(report['created'], 0)
            self.assertEqual(report['deleted'], 0)
            self.assertEqual({k: v['id'] for k, v in original['items'].items()},
                             {k: v['id'] for k, v in current['items'].items()})
            self.assertEqual(remote.items[manual_id]['data']['title'], 'Analyst-reviewed title')
            for group in graph['activity_frames']['activities'][1:]:
                remote_id = current['items'][group['key']]['id']
                self.assertEqual(remote.items[remote_id]['data']['title'], group['title'])
            self.assertEqual(old_plan, untouched)
            repeated = sync_frames(make_plan(graph), 'board=', path, max_items=0, **options)
            self.assertEqual(repeated['updated'], 0)


if __name__ == '__main__':
    unittest.main()
