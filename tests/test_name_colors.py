"""Name colors are case-local presentation, never confidence or trace control."""
import copy
import fcntl
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.address_import import apply_import, preview_import
from liquid_tracer.api import Esplora
from liquid_tracer.attribution_presentation import attribution_lines
from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.compaction import compact_graph
from liquid_tracer.compaction_preview import service_fingerprint
from liquid_tracer.elk_layout import optimize_graph
from liquid_tracer.export import COLORS, build_graph, node_csv_rows, svg_graph
from liquid_tracer.investigations import create_investigation
from liquid_tracer.layout_preview import render_svg
from liquid_tracer.mermaid import mermaid_source
from liquid_tracer.miro import make_plan, sync
from liquid_tracer.name_colors import (color_text, name_color_catalog, name_key,
                                      set_name_colors, validate_name_colors)
from liquid_tracer.services import load_services, set_service
from tests.fixtures import A, B, C, D
from tests import test_service_presentation
from tests.test_miro_sync import FakeMiro


class NameColorStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = create_investigation(self.root, 'Name colors', seeds=[A + ':0'])

    def ingest(self, rows):
        text = json.dumps(rows)
        plan = preview_import(self.case, text)
        self.assertTrue(plan['valid'], plan['errors'])
        return apply_import(self.case, text, approval_sha256=plan['approval_sha256'])

    def save(self, name='BTSE', color='#123ABC', **kwargs):
        return set_name_colors(self.case, [{'name': name, 'color': color}],
            expected_revision=kwargs.get('revision', load_services(self.case)['revision']))

    def test_no_run_required_and_no_default_colors(self):
        self.assertEqual(name_color_catalog(self.case)['total'], 0)
        self.assertFalse((self.case / 'services.json').exists())
        self.ingest([{'Address': 'SYNTHETIC-one', 'Name': 'BTSE'},
                     {'address': 'SYNTHETIC-two', 'name': 'btse', 'confidence': 'Confirmed', 'stop_tracing': False}])
        before = load_services(self.case)
        catalog = name_color_catalog(self.case)
        self.assertEqual(catalog['total'], 1)
        self.assertEqual(catalog['rows'][0]['variants'], ['BTSE', 'btse'])
        self.assertEqual(catalog['rows'][0]['addresses'], 2)
        self.assertIsNone(catalog['rows'][0]['color'])
        with patch('liquid_tracer.api.http', side_effect=AssertionError('No network')):
            self.save()
        after = load_services(self.case)
        self.assertEqual(after['rules'], before['rules'])
        self.assertEqual(after['name_colors'], {'btse': '#123abc'})
        self.assertEqual(after['history'][:-1], before['history'])
        self.assertEqual(after['history'][-1]['type'], 'name_colors')
        self.assertNotIn('latest_run', read_json(self.case / 'case.json'))

    def test_color_survives_import_and_case_only_reimport_is_noop(self):
        self.ingest([{'address': 'SYNTHETIC-one', 'name': 'BTSE'}]); self.save()
        path = self.case / 'services.json'; before = path.read_bytes()
        self.save(name='bTsE', color='#123ABC')
        self.assertEqual(path.read_bytes(), before)
        self.ingest([{'address': 'SYNTHETIC-one', 'name': 'btse'}])
        self.assertEqual(path.read_bytes(), before)
        self.ingest([{'address': 'SYNTHETIC-two', 'name': 'bTsE'}])
        row = name_color_catalog(self.case)['rows'][0]
        self.assertEqual((row['addresses'], row['color']), (2, '#123abc'))

    def test_casefold_unicode_and_different_names_stay_distinct(self):
        self.ingest([{'address': 'SYNTHETIC-one', 'name': 'Straße'},
                     {'address': 'SYNTHETIC-two', 'name': 'STRASSE'},
                     {'address': 'SYNTHETIC-three', 'name': 'Perp'},
                     {'address': 'SYNTHETIC-four', 'name': 'Perpetrator'}])
        self.assertEqual(name_key('  Straße  '), 'strasse')
        self.save(name='STRASSE')
        self.assertEqual(name_color_catalog(self.case)['total'], 3)
        self.assertEqual(name_color_catalog(self.case, query='straße')['rows'][0]['color'], '#123abc')
        self.assertEqual(name_color_catalog(self.case, query='PERP', limit=1, offset=1)['rows'][0]['key'], 'perpetrator')

    def test_unused_unicode_palette_entry_can_be_cleared(self):
        original = 'ß' * 120
        self.ingest([{'address': 'SYNTHETIC-one', 'name': original}])
        self.save(name=original)
        set_service(self.case, 'SYNTHETIC-one', name='Renamed')
        retained = next(row for row in name_color_catalog(self.case)['rows'] if row['addresses'] == 0)
        self.save(name=retained['name'], color=None)
        self.assertEqual(load_services(self.case)['name_colors'], {})

    def test_clear_is_scoped_to_name_and_case_and_preserves_decisions(self):
        self.ingest([{'address': 'SYNTHETIC-one', 'name': 'BTSE', 'stop_tracing': True},
                     {'address': 'SYNTHETIC-two', 'name': 'Other', 'stop_tracing': False}])
        self.save(); self.save('Other', '#000000')
        before = load_services(self.case)['rules']
        self.save(color=None)
        self.assertEqual(load_services(self.case)['name_colors'], {'other': '#000000'})
        self.assertEqual(load_services(self.case)['rules'], before)
        other = create_investigation(self.root, 'Other case', seeds=[A + ':0'])
        set_service(other, 'SYNTHETIC-one', name='BTSE')
        self.assertIsNone(name_color_catalog(other)['rows'][0]['color'])

    def test_invalid_updates_are_all_or_none(self):
        self.ingest([{'address': 'SYNTHETIC-one', 'name': 'BTSE'}]); self.save()
        path = self.case / 'services.json'; before = path.read_bytes()
        for invalid in ('red', '#fff', '#12345g', '#12345678', '#000000;background:red', True, [], 12):
            with self.subTest(color=invalid), self.assertRaises(TraceError):
                self.save(color=invalid)
            self.assertEqual(path.read_bytes(), before)
        for updates in ([], [{'name': 'unknown', 'color': '#abcdef'}],
                        [{'name': 'BTSE', 'color': '#123456'}, {'name': 'btse', 'color': '#654321'}],
                        [{'name': 'BTSE', 'color': '#abcdef', 'stop_tracing': False}]):
            with self.subTest(updates=updates), self.assertRaises(TraceError):
                set_name_colors(self.case, updates, expected_revision=load_services(self.case)['revision'])
            self.assertEqual(path.read_bytes(), before)

    def test_stale_revision_locks_and_frozen_imports(self):
        self.ingest([{'address': 'SYNTHETIC-one', 'name': 'BTSE'}])
        revision = load_services(self.case)['revision']
        text = 'SYNTHETIC-new'; preview = preview_import(self.case, text)
        fingerprint = service_fingerprint(self.case)
        self.save()
        self.assertNotEqual(service_fingerprint(self.case), fingerprint)
        with self.assertRaises(TraceError): self.save(revision=revision)
        with self.assertRaises(TraceError): apply_import(self.case, text, approval_sha256=preview['approval_sha256'])
        with (self.case / 'trace.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            with self.assertRaises(TraceError): self.save(color='#654321')

    def test_saved_colors_validate_and_missing_map_is_backward_compatible(self):
        self.ingest([{'address': 'SYNTHETIC-one', 'name': 'BTSE'}])
        before = load_services(self.case)
        self.assertNotIn('name_colors', before)
        for colors in ([], {'BTSE': '#abcdef'}, {'btse': '#ABCDEF'}, {'btse': '<script>'}):
            with self.subTest(colors=colors), self.assertRaises(TraceError): validate_name_colors(colors)
        malformed = copy.deepcopy(before); malformed['name_colors'] = {'btse': 'red'}
        save_json(self.case / 'services.json', malformed)
        with self.assertRaises(TraceError): load_services(self.case)


class NameColorGraphTests(unittest.TestCase):
    setUp = test_service_presentation.ServicePresentationTests.setUp
    node = staticmethod(test_service_presentation.ServicePresentationTests.node)

    def assessment(self, *, address='SYNTHETIC-branch-A', name='BTSE', confidence='suspected', stop=False):
        return {'kind': 'address', 'value': address, 'entity': name, 'confidence': confidence,
                'source': 'Synthetic evidence', 'notes': 'Preserve these notes', 'stop': stop,
                'observed_at': '2026-01-01'}

    def test_confidence_alone_never_changes_color_or_role(self):
        baseline = self.node(build_graph(self.state))
        for confidence in ('suspected', 'confirmed'):
            self.state['labels'] = [self.assessment(confidence=confidence)]
            node = self.node(build_graph(self.state))
            self.assertEqual((node['role'], node['color']), (baseline['role'], baseline['color']))
            self.assertEqual(node['label'].splitlines()[0], ('Suspected ' if confidence == 'suspected' else '') + 'BTSE')

    def test_assignments_are_case_insensitive_and_keep_display_spelling(self):
        for name, confidence in (('BTSE', 'suspected'), ('btse', 'confirmed'), ('BtSe', 'suspected')):
            with self.subTest(name=name, confidence=confidence):
                self.state['labels'] = [self.assessment(name=name, confidence=confidence)]
                self.state['service_controls'] = {'name_colors': {'btse': '#123abc'}}
                before = copy.deepcopy(self.state)
                graph = build_graph(self.state); node = self.node(graph)
                self.assertEqual(node['color'], '#123abc')
                self.assertEqual(node['color_source'], 'name')
                self.assertIn(name, node['label'])
                self.assertEqual(self.state, before)
                self.assertEqual(node['details']['address_attributions'][0]['notes'], 'Preserve these notes')

    def test_seed_red_wins_over_names_confidence_stops_and_unspent(self):
        # The single seed output has a saved unspent observation in this variant.
        self.state['transactions'] = {A: self.state['transactions'][A]}
        self.state['links'] = {}
        self.state['outputs'] = {A + ':0': self.state['outputs'][A + ':0']}
        self.state['outputs'][A + ':0'].update(status='unspent_at_observation',
                observed_spend={'spent': False}, spend_observation_id=99)
        for confidence in ('suspected', 'confirmed'):
            for stop in (False, True):
                for merge in (False, True):
                    self.state['labels'] = [self.assessment(address='SYNTHETIC-victim-deposit', confidence=confidence, stop=stop)]
                    self.state['service_controls'] = {'name_colors': {'btse': '#123abc'}}
                    graph = build_graph(self.state, merge); node = self.node(graph, A + ':0')
                    self.assertEqual((node['color'], node['role'], node['color_source']), (COLORS['seed'], 'seed', 'seed'))
                    self.assertIn('Unspent', node['label'])
                    self.assertEqual('STOP TRACING' in node['label'], stop)
                    self.assertEqual(node['details']['name_colors'], {'btse': '#123abc'})
                    start = next(n for n in graph['nodes'] if n['id'] == 'tx:' + A)
                    self.assertEqual(start['color'], COLORS['starting_transaction'])
                    self.assertIn('selected seed red', '\n'.join(attribution_lines(node)))

    def test_shared_seed_address_red_regardless_of_order_and_other_outputs(self):
        self.state['seeds'].append(B + ':0')
        self.state['labels'] = [self.assessment()]
        self.state['service_controls'] = {'name_colors': {'btse': '#123abc'}}
        for reverse in (False, True):
            if reverse: self.state['transactions'] = dict(reversed(list(self.state['transactions'].items())))
            graph = build_graph(self.state)
            nodes = [n for n in graph['nodes'] if n['id'] == 'liquid:address:SYNTHETIC-branch-A']
            self.assertEqual(len(nodes), 1)
            self.assertEqual(nodes[0]['color'], COLORS['seed'])
            self.assertEqual({v['outpoint'] for v in nodes[0]['details']['occurrences']}, {B + ':0', C + ':0'})

    def test_named_unspent_color_overrides_orange_not_its_evidence(self):
        self.state['transactions'].pop(D, None); self.state['links'].pop(C + ':0', None)
        self.state['outputs'][C + ':0'].update(status='unspent_at_observation', observed_spend={'spent': False}, spend_observation_id=99)
        self.state['labels'] = [self.assessment()]
        self.state['service_controls'] = {'name_colors': {'btse': '#123abc'}}
        graph = build_graph(self.state); node = self.node(graph)
        self.assertEqual(node['color'], '#123abc'); self.assertEqual(node['role'], 'unspent_endpoint')
        self.assertIn('Unspent', node['label'])
        self.state['service_controls']['name_colors'] = {}
        self.assertEqual(self.node(build_graph(self.state))['color'], COLORS['unspent_endpoint'])

    def test_distinct_name_color_conflicts_do_not_pick_by_confidence(self):
        self.state['labels'] = [self.assessment(), self.assessment(name='Other', confidence='confirmed')]
        self.state['service_controls'] = {'name_colors': {'btse': '#123abc', 'other': '#000000'}}
        for reverse in (False, True):
            if reverse: self.state['labels'].reverse()
            node = self.node(build_graph(self.state))
            self.assertEqual(node['color'], COLORS['candidate'])
            self.assertTrue(node['details']['name_color_conflict'])
            self.assertIn('conflicting name colors', '\n'.join(attribution_lines(node)))
        self.state['service_controls']['name_colors']['other'] = '#123abc'
        self.assertEqual(self.node(build_graph(self.state))['color'], '#123abc')

    def test_renderers_csv_and_real_elk_compaction_keep_name_color(self):
        self.state['labels'] = [self.assessment()]
        self.state['service_controls'] = {'name_colors': {'btse': '#123abc'}}
        with patch.object(Esplora, 'get', side_effect=AssertionError('No API')):
            graph = compact_graph(optimize_graph(build_graph(self.state)))
        node = self.node(graph); self.assertEqual(node['color'], '#123abc')
        plan = make_plan(graph)
        item = next(item for item in plan['shapes'] if item['key'] == node['id'])
        self.assertEqual(item['body']['style']['fillColor'], '#123abc')
        self.assertEqual(item['body']['style']['color'], '#ffffff')
        self.assertIn('fill:#123abc', mermaid_source(graph))
        for svg, key in ((svg_graph(graph), 'data-key'), (render_svg(graph), 'data-node-id')):
            group = next(e for e in ET.fromstring(svg).iter() if e.get(key) == node['id'])
            self.assertTrue(any(e.get('fill') == '#123abc' for e in group.iter()))
            self.assertTrue(any(e.get('fill') == '#ffffff' for e in group.iter()))
        row = next(row for row in node_csv_rows(graph) if row['id'] == node['id'])
        self.assertEqual(row['color_source'], 'name'); self.assertEqual(row['name_colors'], {'btse': '#123abc'})
        self.assertEqual(row['notes'], 'Preserve these notes')

    def test_miro_refresh_reuses_ids_preserves_manual_edits_and_seed_red(self):
        self.state['labels'] = [self.assessment()]
        remote = FakeMiro(); path = self.root / 'miro.json'
        graph = build_graph(self.state); key = self.node(graph)['id']
        sync(make_plan(graph), 'board=', path, token='test', transport=remote, interval=0)
        ids = {key: entry['id'] for key, entry in read_json(path)['items'].items()}
        self.state['service_controls'] = {'name_colors': {'btse': '#123abc'}}
        sync(make_plan(build_graph(self.state)), 'board=', path, token='test', transport=remote, interval=0)
        self.assertEqual(remote.items[ids[key]]['style']['fillColor'], '#123abc')
        self.assertEqual({k: e['id'] for k, e in read_json(path)['items'].items()}, ids)
        remote.items[ids[key]]['style']['fillColor'] = '#abcdef'
        self.state['service_controls']['name_colors']['btse'] = '#000000'
        sync(make_plan(build_graph(self.state)), 'board=', path, token='test', transport=remote, interval=0)
        self.assertEqual(remote.items[ids[key]]['style']['fillColor'], '#abcdef')
        seed = self.node(graph, A + ':0')['id']
        self.assertEqual(remote.items[ids[seed]]['style']['fillColor'], COLORS['seed'])

    def test_foreground_is_legible_for_light_and_dark_assignments(self):
        self.assertEqual(color_text('#000000'), '#ffffff')
        self.assertEqual(color_text('#ffffff'), '#000000')
        with self.assertRaises(TraceError): color_text('url(evil)')
