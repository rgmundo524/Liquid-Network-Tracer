"""Context summaries toggle without losing evidence or unreviewed board edits."""

import copy
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.common import TraceError, canonical, digest, read_json
from liquid_tracer.context_groups import group_context_inputs
from liquid_tracer.export import build_graph
from liquid_tracer.miro import make_plan, sync, validate_plan
from tests.test_input_order import input_order_state, child_input
from tests.test_presentation_annotations import AnnotationMiro


class ContextGroupMiroTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'miro.json'
        self.remote = AnnotationMiro()
        self.graph = build_graph(input_order_state())
        # Frame composition is exercised separately by the grouped layout tests.
        self.graph.pop('activity_frames', None)
        self.graph['run']['ancestor_runs'] = []
        self.plain = make_plan(self.graph)
        self.grouped = make_plan(group_context_inputs(self.graph, enabled=True))
        self.group_key = next(iter(self.grouped['context_group_items']))
        self.members = set(self.grouped['context_group_items'][self.group_key]['members'])

    def sync(self, plan, **options):
        return sync(plan, 'test-board', self.path, token='test-token', interval=0,
                    transport=self.remote, reorganize=True, **options)

    def item(self, key):
        return self.remote.items[read_json(self.path)['items'][key]['id']]

    def test_grouping_and_restoration_are_reversible_and_idempotent(self):
        self.sync(self.plain)
        before = read_json(self.path)['items']
        report = self.sync(self.grouped)
        mapped = read_json(self.path)['items']
        self.assertEqual(report['context_items_to_replace'], 4)
        self.assertFalse(self.members.intersection(mapped))
        self.assertIn(self.group_key, mapped)
        self.assertEqual(self.item(child_input(0))['startItem']['id'], mapped[self.group_key]['id'])
        self.assertEqual(self.item(child_input(1))['startItem']['id'], mapped[self.group_key]['id'])
        expected = next(edge['body']['captions'] for edge in self.plain['connectors']
                        if edge['key'] == child_input(0))
        self.assertEqual(self.item(child_input(0))['captions'], expected)
        self.assertEqual(mapped[child_input(2)]['id'], before[child_input(2)]['id'])
        writes = len(self.remote.writes)
        self.sync(self.grouped, max_items=0)
        self.assertEqual(writes, len(self.remote.writes))
        self.sync(self.plain)
        restored = read_json(self.path)['items']
        self.assertNotIn(self.group_key, restored)
        self.assertTrue(self.members.issubset(restored))
        for edge in self.plain['connectors']:
            self.assertEqual(self.item(edge['key'])['startItem']['id'], restored[edge['source']]['id'])
        self.assertEqual(set(self.remote.items), {item['id'] for item in restored.values()})

    def test_ordinary_sync_preserves_manual_positions_ports_and_connector_shape(self):
        self.sync(self.plain)
        key = next(iter(self.members))
        self.item(key)['position'].update(x=9999, y=-5000)
        self.item(child_input(0))['startItem']['position'] = {'x': '50%', 'y': '0%'}
        self.item(child_input(0))['shape'] = 'curved'
        before = copy.deepcopy(self.remote.items)
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, 'Sync and reorganize'):
            sync(self.grouped, 'test-board', self.path, token='test-token', interval=0,
                 transport=self.remote, reorganize=False)
        self.assertEqual(self.remote.items, before)
        self.assertEqual(len(self.remote.writes), writes)
        # Choosing reorganization authorizes replacing the grouping's layout.
        self.sync(self.grouped)
        self.assertIn(self.group_key, read_json(self.path)['items'])

    def test_first_publication_grouped_can_be_restored(self):
        self.sync(self.grouped)
        self.sync(self.plain)
        self.assertTrue(self.members.issubset(read_json(self.path)['items']))

    def test_manual_member_and_connector_text_style_edits_block_before_writes(self):
        for key in (next(iter(self.members)), child_input(0)):
            with self.subTest(key=key):
                self.path.unlink(missing_ok=True)
                self.path.with_suffix('.json.journal').unlink(missing_ok=True)
                self.remote = AnnotationMiro()
                self.sync(self.plain)
                self.item(key)['style']['strokeColor' if key.startswith('in:') else 'fillColor'] = '#987654'
                writes = len(self.remote.writes)
                with self.assertRaisesRegex(TraceError, 'manual edits'):
                    self.sync(self.grouped)
                self.assertEqual(writes, len(self.remote.writes))

    def test_manual_summary_notes_block_restoration(self):
        self.sync(self.grouped)
        self.item(self.group_key)['data']['content'] += '<p>Analyst note</p>'
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, 'manual edits'):
            self.sync(self.plain)
        self.assertEqual(writes, len(self.remote.writes))

    def test_resizing_and_unmanaged_attachments_block_before_writes(self):
        self.sync(self.plain)
        key = next(iter(self.members))
        self.item(key)['geometry']['width'] += 20
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, 'resized'):
            self.sync(self.grouped)
        self.item(key)['geometry']['width'] -= 20
        self.remote.items['manual-link'] = {
            'id': 'manual-link', 'type': 'connector',
            'startItem': {'id': self.item(key)['id']},
            'endItem': {'id': self.item(child_input(0))['endItem']['id']}}
        with self.assertRaisesRegex(TraceError, 'connector attaches'):
            self.sync(self.grouped)
        self.assertEqual(writes, len(self.remote.writes))

    def test_lost_delete_response_reconciles_without_orphan_objects(self):
        self.sync(self.plain)
        self.remote.lose_delete = True
        with self.assertRaisesRegex(TraceError, 'lost'):
            self.sync(self.grouped)
        self.assertTrue(read_json(self.path)['pending_deletions'])
        with self.assertRaisesRegex(TraceError, 'same plan'):
            self.sync(self.plain)
        self.sync(self.grouped)
        state = read_json(self.path)
        self.assertFalse(state['pending_deletions'])
        self.assertEqual(set(self.remote.items), {record['id'] for record in state['items'].values()})

    def test_uncertain_summary_creation_is_not_blindly_replayed(self):
        self.sync(self.plain)
        self.remote.lose_next_post = True
        with self.assertRaisesRegex(TraceError, 'lost|uncertain'):
            self.sync(self.grouped)
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, 'uncertain'):
            self.sync(self.grouped)
        self.assertEqual(writes, len(self.remote.writes))

    def test_tampered_evidence_and_missing_proofs_are_rejected(self):
        for change in ('evidence', 'proof'):
            plan = copy.deepcopy(self.grouped)
            if change == 'evidence':
                next(e for e in plan['connectors'] if e['key'] == child_input(0))['context_evidence']['outpoint'] = 'other:0'
            else:
                plan['context_group_items'] = {}
            plan['sha256'] = digest(canonical({k: v for k, v in plan.items() if k != 'sha256'}))
            with self.subTest(change=change), self.assertRaises(TraceError):
                validate_plan(plan)

    def test_changed_original_outpoint_blocks_restoration(self):
        self.sync(self.grouped)
        plan = copy.deepcopy(self.plain)
        next(e for e in plan['connectors'] if e['key'] == child_input(0))['context_evidence']['outpoint'] = 'different:0'
        plan['sha256'] = digest(canonical({k: v for k, v in plan.items() if k != 'sha256'}))
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, 'original input evidence'):
            self.sync(plan)
        self.assertEqual(writes, len(self.remote.writes))

    def test_membership_change_replaces_summary_and_retains_original_edges(self):
        self.sync(self.grouped)
        expanded = build_graph(input_order_state(count=4))
        expanded.pop('activity_frames', None)
        expanded['run']['ancestor_runs'] = []
        plan = make_plan(group_context_inputs(expanded, enabled=True))
        self.sync(plan)
        state = read_json(self.path)
        self.assertEqual(len(state['items'][self.group_key]['context_group_proof']['members']), 3)
        self.assertEqual(set(self.remote.items), {record['id'] for record in state['items'].values()})
        self.sync(make_plan(expanded))
        self.assertNotIn(self.group_key, read_json(self.path)['items'])

    def test_new_cross_branch_interaction_automatically_restores_individual_addresses(self):
        self.sync(self.grouped)
        state = input_order_state()
        from tests.test_layout import txid
        # Discover the producer of a formerly external input in a later trace.
        child = state['transactions'][txid('input-order-child')]['data']
        parent = child['vin'][0]['txid']
        state['transactions'][parent] = {
            'data': {'txid': parent, 'vin': [],
                     'vout': [copy.deepcopy(child['vin'][0]['prevout'])], 'status': {}},
            'hops': [0], 'depth': 0, 'observation_id': parent}
        graph = build_graph(state, group_context_inputs=True)
        graph['run']['ancestor_runs'] = []
        self.assertFalse(any(node['kind'] == 'context_group' for node in graph['nodes']))
        self.sync(make_plan(graph))
        self.assertNotIn(self.group_key, read_json(self.path)['items'])
        self.assertTrue(self.members.issubset(read_json(self.path)['items']))

    def test_grouped_elk_publication_restores_frames_and_full_graph(self):
        from tests.test_elk_layout import HAS_ELK
        if not HAS_ELK:
            self.skipTest('Pinned ELK is unavailable')
        from liquid_tracer.elk_layout import optimize_graph
        state = input_order_state()
        for grouped in (False, True, False):
            graph = build_graph(state, group_context_inputs=grouped)
            graph['run']['ancestor_runs'] = []
            plan = make_plan(optimize_graph(graph, connector_style='elbowed'))
            self.sync(plan)
            validate_plan(plan)
            mapping = read_json(self.path)['items']
            self.assertEqual(self.group_key in mapping, grouped)
            self.assertEqual(set(self.remote.items), {record['id'] for record in mapping.values()})


if __name__ == '__main__':
    unittest.main()
