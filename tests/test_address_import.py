"""Reviewed, atomic, case-local attribution imports before and between runs."""
import contextlib
import copy
import fcntl
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.address_import import MAX_BYTES, apply_import, parse_import, preview_import, read_import
from liquid_tracer.address_review import list_addresses
from liquid_tracer.api import Limits
from liquid_tracer.cli import main
from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.investigations import create_investigation
from liquid_tracer.services import disable_service, load_services, service_labels, set_service
from tests.test_service_stops import network
from tests.test_trace_concurrency import synthetic_trace

A, B = 'SYNTHETIC-address-import-A', 'SYNTHETIC-address-import-B'


class ImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = create_investigation(self.root, 'Import before first run')

    def apply(self, text, **kwargs):
        plan = preview_import(self.case, text, **kwargs)
        self.assertTrue(plan['valid'], plan['errors'])
        return apply_import(self.case, text, approval_sha256=plan['approval_sha256'], **kwargs)

    def test_preview_is_read_only_and_import_populates_review_without_run(self):
        before = {p.name: p.read_bytes() for p in self.case.iterdir()}
        text = A + '\n' + B
        with patch('liquid_tracer.api.http', side_effect=AssertionError('No network')):
            plan = preview_import(self.case, text)
            self.assertTrue(plan['valid'])
            self.assertEqual(before, {p.name: p.read_bytes() for p in self.case.iterdir()})
            self.assertEqual(self.apply(text)['changed'], 2)
            page = list_addresses(self.case)
            self.assertEqual(page['total'], 2)
            self.assertTrue(all(row['run_output_count'] == 0 and row['activity'] is None for row in page['rows']))
            self.assertFalse((self.case / 'runs').exists())
            self.assertTrue(all(label['stop'] and label['confidence'] == 'suspected'
                                for label in service_labels(load_services(self.case))))

    def test_csv_metadata_multiline_and_json_label_compatibility(self):
        text = '\ufeffaddress,name,confidence,source,notes,stop_tracing,observed_at\r\n'
        text += A + ',"Service, X",confirmed,Records,"First line\nSecond line",true,2026-09-10\r\n'
        self.apply(text)
        rule = load_services(self.case)['rules'][A]
        self.assertEqual((rule['name'], rule['notes']), ('Service, X', 'First line\nSecond line'))
        label = service_labels(load_services(self.case))[0]
        self.assertEqual((label['confidence'], label['source'], label['observed_at']), ('confirmed', 'Records', '2026-09-10'))
        self.apply(json.dumps([{'kind': 'address', 'value': B, 'entity': 'Case alias',
                                'confidence': 'suspected', 'stop': False}]))
        self.assertFalse(next(label for label in service_labels(load_services(self.case)) if label['value'] == B)['stop'])

    def test_identical_duplicates_and_reimports_are_idempotent(self):
        text = json.dumps([A, A, B])
        self.assertEqual(self.apply(text)['duplicate_rows'], 1)
        before = (self.case / 'services.json').read_bytes()
        self.assertEqual(self.apply(text)['changed'], 0)
        self.assertEqual(before, (self.case / 'services.json').read_bytes())

    def test_conflicts_preserved_by_default_and_replaced_only_with_review(self):
        old = set_service(self.case, A, name='Existing', rationale='Keep this evidence')
        text = 'address,name\n' + A + ',New name\n' + B + ',Other name\n'
        self.assertEqual(self.apply(text)['counts']['keep'], 1)
        self.assertEqual(load_services(self.case)['rules'][A], old['rules'][A])
        self.apply(text, policy='replace')
        settings = load_services(self.case)
        self.assertEqual(settings['rules'][A]['name'], 'New name')
        self.assertEqual(settings['history'][-1]['previous'], old['rules'][A])
        self.assertEqual(settings['rules'][A]['created_at'], old['rules'][A]['created_at'])
        self.assertIn('import_sha256', settings['history'][-1])
        self.assertIn('import_row', settings['history'][-1])

    def test_stale_approval_file_options_and_case_rejected(self):
        plan = preview_import(self.case, A)
        for text, options in ((B, {}), (A, {'policy': 'replace'})):
            with self.assertRaisesRegex(TraceError, 'changed'):
                apply_import(self.case, text, approval_sha256=plan['approval_sha256'], **options)
        other = create_investigation(self.root, 'Other case')
        with self.assertRaisesRegex(TraceError, 'changed'):
            apply_import(other, A, approval_sha256=plan['approval_sha256'])
        set_service(self.case, B)
        with self.assertRaisesRegex(TraceError, 'changed'):
            apply_import(self.case, A, approval_sha256=plan['approval_sha256'])
        self.assertNotIn(A, load_services(self.case)['rules'])

    def test_invalid_rows_never_partially_apply(self):
        invalid = [{'address': B, 'stop_tracing': 'maybe'}, {'address': B, 'confidence': 'corroborated'},
                   {'address': B, 'classification': 'label', 'stop_tracing': True},
                   {'address': B, 'kind': 'script'}, {'address': B, 'network': 'bitcoin'},
                   {'address': B, 'observed_at': 'yesterday'}, {'address': 'https://example.invalid/foo'}]
        texts = [json.dumps([A, row]) for row in invalid] + [
            'address,name\n' + A + ',one\n' + A + ',two\n', 'address,name\n' + A + ',one,extra\n']
        for text in texts:
            with self.subTest(text=text):
                result = preview_import(self.case, text)
                self.assertFalse(result['valid'])
                self.assertIsNone(result['approval_sha256'])
                with self.assertRaises(TraceError):
                    apply_import(self.case, text, approval_sha256='a' * 64)
                self.assertFalse((self.case / 'services.json').exists())

    def test_malformed_format_header_duplicate_keys_and_bounds(self):
        for text, format in (('[]', 'json'), ('{}', 'json'), ('[', 'json'), ('', 'auto'),
                            ('address,value\n' + A + ',' + A, 'csv'),
                            ('name,unsupported\nExample,yes', 'csv'),
                            ('[{"address":"' + A + '","address":"' + B + '"}]', 'json'),
                            ('x' * (MAX_BYTES + 1), 'text'), ('\n'.join([A] * 5001), 'text')):
            with self.subTest(format=format), self.assertRaises(TraceError):
                parse_import(text, format)

    def test_boolean_spellings_are_explicit(self):
        for value, expected in (('false', False), ('no', False), ('0', False), (0, False),
                                ('true', True), ('yes', True), ('1', True), (1, True), (False, False)):
            parsed = parse_import(json.dumps([{'address': A, 'stop_tracing': value}]))
            self.assertEqual(parsed['rows'][0]['rule']['stop_tracing'], expected)

    def test_large_list_and_case_sensitive_addresses(self):
        text = '\n'.join('SYNTHETIC-address-' + str(i) for i in range(5000))
        self.assertEqual(preview_import(self.case, text)['unique_addresses'], 5000)
        self.assertEqual(parse_import('VJLqUdAXaKRDFZRnzBaYLBQ8NSYnNn69Qz\nvjLqUdAXaKRDFZRnzBaYLBQ8NSYnNn69Qz')['duplicates'], 0)

    def test_file_encoding_bom_and_size(self):
        path = self.root / 'addresses.csv'
        path.write_bytes(('\ufeffaddress\n' + A).encode())
        self.assertEqual(read_import(path), 'address\n' + A)
        path.write_bytes(b'\xff')
        with self.assertRaisesRegex(TraceError, 'UTF-8'):
            read_import(path)
        path.write_bytes(b'a' * (MAX_BYTES + 1))
        with self.assertRaisesRegex(TraceError, '512'):
            read_import(path)

    def test_lock_and_atomic_single_save(self):
        plan = preview_import(self.case, A)
        with (self.case / 'trace.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(TraceError, 'active'):
                apply_import(self.case, A, approval_sha256=plan['approval_sha256'])
        self.assertFalse((self.case / 'services.json').exists())
        with patch('liquid_tracer.address_import.save_json', wraps=save_json) as save:
            self.apply(A + '\n' + B)
            save.assert_called_once()

    def test_single_edits_disable_and_legacy_rules_preserve_imported_metadata(self):
        self.apply(json.dumps([{'address': A, 'confidence': 'confirmed',
                               'source': 'Reviewed document', 'observed_at': '2023-10-04', 'stop_tracing': False}]))
        set_service(self.case, A, name='Amended name')
        rule = load_services(self.case)['rules'][A]
        self.assertEqual((rule['confidence'], rule['source'], rule['observed_at'], rule['stop_tracing']),
                         ('confirmed', 'Reviewed document', '2023-10-04', False))
        disable_service(self.case, A)
        self.assertFalse(load_services(self.case)['rules'][A]['enabled'])
        legacy = set_service(self.case, B)
        for field in ('stop_tracing', 'confidence', 'source', 'observed_at'):
            legacy['rules'][B].pop(field)
        save_json(self.case / 'services.json', legacy)
        self.assertTrue(service_labels(load_services(self.case))[0]['stop'])

    def test_cli_default_preview_and_explicit_approval_without_credentials(self):
        path = self.root / 'addresses.txt'; path.write_text(A)
        argv = ['address-import', '--case', str(self.case), '--file', str(path)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output): self.assertEqual(main(argv), 0)
        self.assertFalse((self.case / 'services.json').exists())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(argv + ['--approve-plan', json.loads(output.getvalue())['approval_sha256']]), 0)
        self.assertIn(A, load_services(self.case)['rules'])

    def test_first_trace_uses_imported_service_stops_and_preserves_label_only(self):
        ids, addresses, data = network()
        fixture = self.root / 'api.json'; save_json(fixture, data)
        self.apply(json.dumps([{'address': addresses['B'], 'confidence': 'confirmed', 'name': 'Known service'},
                               {'address': addresses['A'], 'stop_tracing': False, 'name': 'Starting output'}]))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main(['trace', '--case', str(self.case), '--fixture', str(fixture),
                                   '--seed', ids['A'] + ':0', '--hops', '5']), 0)
        result = json.loads(stdout.getvalue())
        state = read_json(Path(result['directory']) / 'trace.json')
        self.assertEqual(set(state['transactions']), {ids['A'], ids['B']})
        self.assertEqual(state['outputs'][ids['B'] + ':0']['status'], 'suspected_service_stop')
        self.assertEqual(next(label for label in state['labels'] if label['value'] == addresses['B'])['confidence'], 'confirmed')
        archived = {p.name: p.read_bytes() for p in Path(result['directory']).iterdir() if p.is_file()}
        self.apply(json.dumps([{'address': addresses['B'], 'enabled': False}]), policy='replace')
        self.assertEqual(archived, {p.name: p.read_bytes() for p in Path(result['directory']).iterdir() if p.is_file()})

    def test_seed_stops_and_post_run_downstream_hold_use_same_rules(self):
        ids, addresses, data = network()
        self.apply(json.dumps([{'address': addresses['A'], 'confidence': 'confirmed'}]))
        state, transport = synthetic_trace(self.root / 'trace1', data, [ids['A'] + ':0'],
            limits=Limits(max_hops=4), labels=service_labels(load_services(self.case)))
        self.assertEqual(transport.calls, ['/tx/' + ids['A']])
        self.assertEqual(state['links'], {})
        parent, _ = synthetic_trace(self.root / 'trace2', data, [ids['A'] + ':0'], limits=Limits(max_hops=2))
        original = copy.deepcopy(parent)
        self.apply(json.dumps([{'address': addresses['A'], 'enabled': False},
                               {'address': addresses['B'], 'confidence': 'confirmed'}]), policy='replace')
        result, transport = synthetic_trace(self.root / 'trace3', data, [ids['A'] + ':0'], parent=parent,
            limits=Limits(max_hops=4), labels=service_labels(load_services(self.case)))
        self.assertEqual(parent, original)
        self.assertEqual(transport.calls, [])
        self.assertEqual(result['outputs'][ids['D'] + ':0']['status'], 'held_behind_service')
