"""Named hop origins through saved preferences, the CLI, and the local HTTP UI."""
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError, read_json
from liquid_tracer.investigations import read_case, save_collection_reference, update_case, validate_settings
from liquid_tracer.menu import _trace_arguments
from liquid_tracer.progress import public_progress
from liquid_tracer.services import set_service
from liquid_tracer.web import collected_hops
from tests import test_web


class GroupHopWorkflowTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    wait = test_web.LocalWebTests.wait
    create = test_web.LocalWebTests.create

    def named_case(self):
        _, detail = self.create()
        case, metadata = self.server.case(detail['id'])
        for address in ('SYNTHETIC-victim-deposit', 'SYNTHETIC-branch-A'):
            set_service(case, address, name='Perp', enabled=True, stop_tracing=False)
        return case, metadata, '/api/cases/' + detail['id']

    def test_collection_runs_keep_their_basis_and_original_seed_distances(self):
        case, metadata, route = self.named_case()
        seeds = metadata['seeds'][:]
        first = self.wait(self.success(route + '/actions', {
            'action': 'trace', 'hops': 0, 'hop_reference_name': ' Perp '}, 202))
        state = read_json(case / 'runs' / first['run_id'] / 'trace.json')
        self.assertEqual(state['hop_reference_name'], 'Perp')
        self.assertEqual(state['seeds'], seeds)
        self.assertGreater(max(tx['depth'] for tx in state['transactions'].values()), 0)
        self.assertEqual(collected_hops(state), 0)
        detail = self.success(route)
        self.assertEqual(detail['latest']['hop_reference_name'], 'Perp')
        self.assertEqual(detail['latest']['collected_hops'], 0)
        self.assertEqual(detail['run_defaults']['hop_reference_name'], 'Perp')
        before = (case / 'runs' / first['run_id'] / 'trace.json').read_bytes()
        second = self.wait(self.success(route + '/actions', {
            'action': 'trace', 'hops': 1, 'hop_reference_name': ''}, 202))
        next_state = read_json(case / 'runs' / second['run_id'] / 'trace.json')
        self.assertNotIn('hop_reference_name', next_state)
        self.assertEqual(next_state['limits']['max_hops'], 1)
        self.assertEqual(next_state['seeds'], seeds)
        runs = {r['id']: r for r in self.success(route)['runs']}
        self.assertEqual(runs[first['run_id']]['hop_reference_name'], 'Perp')
        self.assertNotIn('hop_reference_name', runs[second['run_id']])
        self.assertEqual(before, (case / 'runs' / first['run_id'] / 'trace.json').read_bytes())

    def test_unknown_or_invalid_reference_does_not_start_or_change_settings(self):
        case, _, route = self.named_case()
        before = (case / 'case.json').read_bytes()
        with patch.object(self.server, 'start_job') as start:
            for name in ('Missing', None, [], True, 'bad\nname', 'x' * 121):
                with self.subTest(name=name):
                    self.assertEqual(self.request(route + '/actions', {
                        'action': 'trace', 'hops': 2, 'hop_reference_name': name})[0], 400)
            start.assert_not_called()
        self.assertEqual(before, (case / 'case.json').read_bytes())

    def test_reference_updates_preserve_other_preferences_and_reject_plot_setting(self):
        case, metadata, route = self.named_case()
        current = {**metadata['run_defaults'], 'budget_limits_enabled': True,
                   'max_requests': 79, 'center_name': 'Different group'}
        update_case(case, {'run_defaults': current})
        saved = save_collection_reference(case, ' Perp ')
        self.assertEqual(saved['run_defaults'], {**current, 'hop_reference_name': 'Perp'})
        with patch.object(self.server, 'start_job', return_value={'id': 'synthetic'}) as start:
            self.success(route + '/actions', {'action': 'trace', 'hops': 2, 'hop_reference_name': 'Perp'}, 202)
            arguments = start.call_args.args[0]
            self.assertEqual(arguments[arguments.index('--hop-reference-name') + 1], 'Perp')
            self.assertEqual(arguments[arguments.index('--max-requests') + 1], '79')
        self.assertEqual(self.request(route + '/plot-settings', {'settings': {'hop_reference_name': 'Perp'}})[0], 400)
        self.assertEqual(read_case(case)['run_defaults'], saved['run_defaults'])

    def test_changed_reference_uses_new_ceiling_and_unchanged_reference_adds_hops(self):
        case, metadata, _ = self.named_case()
        parent = {'source': 'fixture://synthetic', 'hop_reference_name': 'Perp',
                  'labels': [], 'seeds': metadata['seeds'][:]}
        metadata = {**metadata, 'latest_run': 'a' * 16}
        with patch('liquid_tracer.menu._latest', return_value=(None, parent)):
            same, _ = _trace_arguments(case, metadata, validate_settings({'hops': 2, 'hop_reference_name': ' PERP '}))
            changed, _ = _trace_arguments(case, metadata, validate_settings({'hops': 2, 'hop_reference_name': ''}))
        self.assertIn('--additional-hops', same)
        self.assertNotIn('--hops', same)
        self.assertIn('--hops', changed)
        self.assertNotIn('--additional-hops', changed)


class GroupHopMetadataTests(unittest.TestCase):
    def test_named_coverage_ignores_boundary_inspection_but_never_uses_raw_seed_depth(self):
        state = {'hop_reference_name': 'Perp', 'transactions': {
            'internal': {'depth': 30, 'reference_hops': 0},
            'outside': {'depth': 31, 'reference_hops': 2},
            'boundary': {'depth': 32, 'reference_hops': None}}}
        self.assertEqual(collected_hops(state), 2)
        state['transactions']['outside'].pop('reference_hops')
        self.assertIsNone(collected_hops(state))
        self.assertIsNone(collected_hops({'hop_reference_name': 'Perp', 'transactions': {
            'boundary': {'depth': 30, 'reference_hops': None}}}))

    def test_progress_roundtrip_keeps_basis_without_duplicating_message(self):
        event = {'phase': 'collecting', 'completed': 0, 'total': 2, 'hop_reference_name': ' Perp '}
        clean = public_progress(event)
        self.assertEqual(clean['hop_reference_name'], 'Perp')
        self.assertIn('reset to hop 0', clean['message'])
        self.assertEqual(public_progress(clean), clean)
        self.assertNotIn('hop_reference_name', public_progress({**event, 'hop_reference_name': 'bad\nname'}))
        self.assertNotIn('hop_reference_name', public_progress({**event, 'phase': 'creating'}))
        for value in (None, True, [], 'x' * 121, 'bad\nname'):
            with self.subTest(value=value), self.assertRaises(TraceError):
                validate_settings({'hop_reference_name': value})
