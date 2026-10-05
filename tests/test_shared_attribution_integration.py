"""Shared labels are opt-in snapshots, never implicit local tracing decisions."""
import fcntl
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer import shared_attributions as shared
from liquid_tracer.common import TraceError, read_json
from liquid_tracer.input_export import build_input_export
from liquid_tracer.investigations import create_investigation
from liquid_tracer.name_colors import name_color_catalog, set_name_colors
from liquid_tracer.plots import preview_plot, reviewed_plot
from liquid_tracer.services import (apply_service_labels, disable_service, effective_services,
    load_services, service_labels, set_service, set_shared_attributions, shared_attribution_status)
from tests.test_attribution_convergence import graph_state, tx
from tests.test_connections import saved_case

A = 'SYNTHETIC-a-address'
B = 'SYNTHETIC-b-address'


def import_shared(root, name='Shared Exchange', address=A):
    text = json.dumps([{'address': address, 'name': name, 'confidence': 'confirmed',
        'source': 'Reviewed synthetic evidence', 'notes': 'Reuse assessment',
        'stop_tracing': True, 'hop_limit': 0}])
    plan = shared.preview_import(root, text, policy='replace')
    return shared.apply_import(root, text, policy='replace', approval_sha256=plan['approval_sha256'])


class SharedAttributionIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = create_investigation(self.root, 'First', seeds=[tx('a') + ':0'])
        self.other = create_investigation(self.root, 'Second')
        import_shared(self.root)

    def enable(self, case=None, value=True):
        case = case or self.case
        return set_shared_attributions(case, value, expected_revision=load_services(case)['revision'])

    def test_opt_in_local_override_and_disabled_override_are_case_specific(self):
        self.assertEqual(effective_services(self.case)['rules'], {})
        self.enable()
        inherited = effective_services(self.case)['rules'][A]
        self.assertEqual(inherited['attribution_origin'], 'shared')
        self.assertFalse(inherited['stop_tracing'])
        self.assertIsNone(inherited['hop_limit'])
        self.assertEqual(load_services(self.case)['rules'], {})
        self.assertEqual(effective_services(self.other)['rules'], {})
        self.enable(self.other)
        set_service(self.case, A, name='Local Decision', stop_tracing=True, hop_limit=2)
        local = effective_services(self.case)['rules'][A]
        self.assertEqual((local['name'], local['stop_tracing'], local['hop_limit']), ('Local Decision', True, 2))
        self.assertNotIn('attribution_origin', local)
        disable_service(self.case, A)
        self.assertEqual(service_labels(effective_services(self.case)), [])
        self.assertEqual(effective_services(self.other)['rules'][A]['name'], 'Shared Exchange')
        self.assertEqual(shared_attribution_status(self.case)['local_overrides'], 1)

    def test_disabling_inherited_label_only_saves_one_local_override(self):
        import_shared(self.root, address=B)
        self.enable()
        disable_service(self.case, A)
        local = load_services(self.case)['rules']
        self.assertEqual(set(local), {A})
        self.assertFalse(local[A]['enabled'])
        self.assertFalse(local[A]['stop_tracing'])
        self.assertEqual([label['value'] for label in service_labels(effective_services(self.case))], [B])
        self.assertTrue(shared.load_library(self.root)['rules'][A]['enabled'])

    def test_partial_edit_inherits_shared_fields_without_inventing_a_stop(self):
        self.enable()
        set_service(self.case, A, notes='Local note')
        local = load_services(self.case)['rules'][A]
        self.assertEqual((local['name'], local['notes'], local['source']),
                         ('Shared Exchange', 'Local note', 'Reviewed synthetic evidence'))
        self.assertFalse(local['stop_tracing'])
        self.assertIsNone(local['hop_limit'])

    def test_local_export_and_color_write_do_not_copy_inherited_rules(self):
        self.enable()
        rows = name_color_catalog(self.case)['rows']
        self.assertEqual(rows[0]['name'], 'Shared Exchange')
        set_name_colors(self.case, [{'name': 'Shared Exchange', 'color': '#123456'}],
                        expected_revision=load_services(self.case)['revision'])
        self.assertEqual(load_services(self.case)['rules'], {})
        self.assertNotIn(A.encode(), build_input_export(self.case, 'attributions')['data'])
        self.assertIn(A.encode(), shared.export_library(self.root)['data'])

    def test_toggle_has_revision_guard_trace_guard_and_audit(self):
        self.enable()
        before = (self.case / 'services.json').read_bytes()
        with self.assertRaisesRegex(TraceError, 'changed'):
            set_shared_attributions(self.case, False, expected_revision=0)
        with (self.case / 'trace.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaisesRegex(TraceError, 'running'):
                self.enable(value=False)
        self.assertEqual((self.case / 'services.json').read_bytes(), before)
        self.enable(value=False)
        settings = load_services(self.case)
        self.assertEqual(settings['history'][-1]['setting'], 'use_shared_attributions')
        self.assertFalse(settings['history'][-1]['value'])
        self.assertEqual(effective_services(self.case)['rules'], {})
        self.assertEqual(shared.load_library(self.root)['revision'], 1)

    def test_opted_out_cases_do_not_read_library_and_labels_remove_old_inheritance(self):
        self.enable()
        labels = service_labels(effective_services(self.case))
        self.enable(value=False)
        with patch('liquid_tracer.shared_attributions.load_library', side_effect=AssertionError('Opted out')):
            settings = effective_services(self.case)
        self.assertEqual(apply_service_labels(labels, settings), [])

    def test_unreadable_library_can_be_disabled_without_losing_local_rules(self):
        self.enable()
        set_service(self.case, B, name='Local', stop_tracing=True)
        before = load_services(self.case)['rules']
        with patch('liquid_tracer.shared_attributions.load_library', side_effect=TraceError('Unreadable')):
            status = shared_attribution_status(self.case)
            self.assertTrue(status['enabled'])
            self.assertTrue(status['shared_unavailable'])
            self.assertIsNone(status['shared_count'])
            self.enable(value=False)
            self.assertEqual(effective_services(self.case)['rules'], before)
            with self.assertRaisesRegex(TraceError, 'Unreadable'):
                self.enable()

    def test_investigation_deletion_preserves_workspace_attributions(self):
        from liquid_tracer.investigation_deletion import delete_investigation
        from liquid_tracer.investigations import read_case
        self.enable()
        library = shared.load_library(self.root)
        metadata = read_case(self.case)
        delete_investigation(self.root, self.case, case_id=metadata['case_id'], confirm_name=metadata['name'])
        self.assertFalse(self.case.exists())
        self.assertEqual(shared.load_library(self.root), library)
        self.enable(self.other)
        self.assertEqual(effective_services(self.other)['rules'][A]['name'], 'Shared Exchange')

    def test_preview_snapshot_survives_shared_edits_and_new_preview_invalidates_cache(self):
        state, archive = saved_case(self.case, graph_state((('a:0', 'b'),), seeds=('a:0',)))
        original = {p.name: p.read_bytes() for p in archive.iterdir() if p.is_file()}
        self.enable()
        with patch('liquid_tracer.elk_layout.optimize_graph', side_effect=lambda graph, **kwargs: graph), \
                patch('liquid_tracer.api.Esplora.get', side_effect=AssertionError('No fetch')), \
                patch('liquid_tracer.miro.publish', side_effect=AssertionError('No publish')):
            first = preview_plot(self.case, 'full', max_hops=2)
            frozen = read_json(Path(first['directory']) / 'inputs.json')
            import_shared(self.root, 'Revised Shared Exchange')
            with patch('liquid_tracer.shared_attributions.load_library', side_effect=AssertionError('Use frozen snapshot')):
                graph, _ = reviewed_plot(self.case, first['preview_id'])
            self.assertEqual(graph['service_controls']['rules'][A]['name'], 'Shared Exchange')
            second = preview_plot(self.case, 'full', max_hops=2)
        self.assertNotEqual(first['service_sha256'], second['service_sha256'])
        self.assertNotEqual(first['preview_id'], second['preview_id'])
        self.assertEqual(read_json(Path(first['directory']) / 'inputs.json'), frozen)
        self.assertEqual(original, {p.name: p.read_bytes() for p in archive.iterdir() if p.is_file()})
        self.assertEqual(load_services(self.case)['rules'], {})

    def test_analysis_snapshots_shared_revision_without_sharing_stop_controls(self):
        from liquid_tracer.common import digest
        from liquid_tracer.scope_analysis import analyze_scope, read_analysis
        state, archive = saved_case(self.case, graph_state((('a:0', 'b'), ('b:0', 'c')), seeds=('a:0',)))
        evidence = b'[]\n'
        (archive / 'evidence-index.json').write_bytes(evidence)
        with (archive / 'SHA256SUMS').open('a') as manifest:
            manifest.write(digest(evidence) + '  evidence-index.json\n')
        self.enable()
        with patch('liquid_tracer.api.Esplora.get', side_effect=AssertionError('No fetch')), \
                patch('liquid_tracer.elk_layout.optimize_graph', side_effect=AssertionError('No render')):
            first = analyze_scope(self.case, state['run_id'], max_hops=2)
            warm = analyze_scope(self.case, state['run_id'], max_hops=2)
            self.assertTrue(warm['cache_hit'])
            self.assertEqual(first['comparisons'][-1]['transaction_count'], 3)
            import_shared(self.root, 'Revised Exchange')
            second = analyze_scope(self.case, state['run_id'], max_hops=2)
            self.assertNotEqual(first['analysis_id'], second['analysis_id'])
            frozen = read_json(self.case / 'analyses' / first['analysis_id'] / 'inputs.json')
            self.assertEqual(frozen['service_controls']['rules'][A]['name'], 'Shared Exchange')
            with patch('liquid_tracer.shared_attributions.load_library', side_effect=AssertionError('No current labels')):
                self.assertEqual(read_analysis(self.case, first['analysis_id'])['analysis_id'], first['analysis_id'])


if __name__ == '__main__':
    unittest.main()
