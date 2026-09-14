"""Graph-role palettes stay separate from imported names, evidence and borders."""
import copy
import fcntl
import importlib.util
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.compaction import compact_graph
from liquid_tracer.compaction_preview import service_fingerprint
from liquid_tracer.elk_layout import optimize_graph
from liquid_tracer.export import COLORS, build_graph, legend_lines, node_csv_rows, svg_graph
from liquid_tracer.graph_markers import node_border
from liquid_tracer.layout_preview import render_svg
from liquid_tracer.menu import create_app
from liquid_tracer.mermaid import mermaid_source
from liquid_tracer.miro import make_plan, sync, validate_plan
from liquid_tracer.name_colors import apply_name_colors, name_color_catalog, set_name_colors
from liquid_tracer.role_colors import ROLE_LABELS, set_role_colors, validate_role_colors
from liquid_tracer.services import load_services, set_service
from tests import test_name_colors
from tests.test_attribution_convergence import annotation, graph_state, tx
from tests.test_branch_interactions import receiving, SHARED
from tests.test_presentation_annotations import AnnotationMiro
from tests import test_web, test_menu_addresses


class RoleColorStorageTests(unittest.TestCase):
    setUp = test_name_colors.NameColorStorageTests.setUp
    ingest = test_name_colors.NameColorStorageTests.ingest

    def save(self, role='seed', color='#123ABC', revision=None):
        return set_name_colors(self.case, [{'role': role, 'color': color}],
            expected_revision=load_services(self.case)['revision'] if revision is None else revision)

    def test_defaults_available_without_import_or_run(self):
        report = name_color_catalog(self.case)
        self.assertEqual(report['total'], 0)
        self.assertEqual([r['role'] for r in report['roles']], list(ROLE_LABELS))
        self.assertTrue(all(r['color'] is None and r['default_color'] == COLORS[r['role']] for r in report['roles']))
        self.assertFalse((self.case / 'services.json').exists())
        self.save()
        saved = load_services(self.case)
        self.assertEqual(saved['role_colors'], {'seed': '#123abc'})
        self.assertEqual(saved['rules'], {})
        self.assertEqual(saved['history'][-1]['type'], 'role_colors')
        self.assertNotIn('latest_run', read_json(self.case / 'case.json'))
        self.assertEqual(name_color_catalog(self.case, query='nonexistent', offset=100)['roles'][0]['color'], '#123abc')

    def test_name_collisions_imports_and_noop_keep_separate_palettes(self):
        self.ingest([{'address': 'SYNTHETIC-one', 'name': 'Seed addresses', 'stop_tracing': True}])
        set_name_colors(self.case, [{'name': 'Seed addresses', 'color': '#abcdef'}],
                        expected_revision=load_services(self.case)['revision'])
        before = copy.deepcopy(load_services(self.case)['rules'])
        self.save()
        path = self.case / 'services.json'; unchanged = path.read_bytes()
        self.save(color='#123abc')
        self.assertEqual(path.read_bytes(), unchanged)
        self.ingest([{'address': 'SYNTHETIC-two', 'name': 'Another'}])
        self.assertEqual(load_services(self.case)['role_colors'], {'seed': '#123abc'})
        self.save(color=None)
        saved = load_services(self.case)
        self.assertEqual(saved['role_colors'], {})
        self.assertEqual(saved['name_colors'], {'seed addresses': '#abcdef'})
        self.assertEqual(saved['rules']['SYNTHETIC-one'], before['SYNTHETIC-one'])

    def test_invalid_batches_are_atomic_and_stale_edits_fail(self):
        self.save(); path = self.case / 'services.json'; before = path.read_bytes()
        revision = load_services(self.case)['revision']
        batches = [[], [{'role': 'unknown', 'color': '#123456'}],
            [{'role': [], 'color': '#123456'}],
            [{'role': 'seed', 'color': '#123456'}, {'role': 'seed', 'color': '#654321'}],
            [{'role': 'seed', 'color': '#123456'}, {'name': 'seed', 'color': '#654321'}],
            [{'role': 'seed', 'color': '#123456', 'stop_tracing': False}]]
        batches += [[{'role': 'seed', 'color': v}] for v in ['red', '#fff', '#12345678', '#fff"/>', True, [], 1]]
        for batch in batches:
            with self.subTest(batch=batch), self.assertRaises(TraceError):
                set_role_colors(self.case, batch, expected_revision=revision)
            self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(TraceError): self.save(revision=True)
        with self.assertRaises(TraceError): self.save(revision=revision - 1)
        self.assertEqual(path.read_bytes(), before)
        with (self.case / 'trace.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(TraceError): self.save(color='#000000')
        self.assertEqual(path.read_bytes(), before)

    def test_saved_validation_and_case_scope(self):
        self.save()
        for bad in [[], {'unknown': '#123456'}, {'seed': '#ABCDEF'}, {'seed': None}]:
            with self.subTest(bad=bad), self.assertRaises(TraceError): validate_role_colors(bad)
        data = load_services(self.case); data['role_colors'] = {'seed': '#fff"/>'}
        save_json(self.case / 'services.json', data)
        with self.assertRaises(TraceError): load_services(self.case)


class RoleColorGraphTests(unittest.TestCase):
    def graph(self, palette=None):
        state = graph_state((('a:0', 'c'), ('b:0', 'd')))
        receiving(state, 'a:0'); receiving(state, 'b:0')
        state['outputs'][tx('c') + ':0'].update(status='unspent_at_observation',
            observed_spend={'spent': False}, spend_observation_id=123)
        state['transactions'][tx('a')]['data']['vin'].append({'txid': tx('e'), 'vout': 0,
            'prevout': {'scriptpubkey_address': 'SYNTHETIC-context', 'scriptpubkey': '00'}})
        state['transactions'][tx('d')]['data']['vout'].append({'scriptpubkey': '6a', 'scriptpubkey_type': 'op_return'})
        state['service_controls'] = {'role_colors': palette or {}}
        before = copy.deepcopy(state)
        graph = build_graph(state)
        self.assertEqual(state, before)
        return graph

    def test_every_role_renders_with_unchanged_identity_edges_and_borders(self):
        palette = {role: f'#{(index + 1) * 123457:06x}' for index, role in enumerate(ROLE_LABELS)}
        original = self.graph(); graph = self.graph(palette)
        before = {n['id']: n for n in original['nodes']}
        self.assertEqual(graph['edges'], original['edges'])
        self.assertEqual({n['id'] for n in graph['nodes']}, set(before))
        self.assertEqual({n.get('role', n['kind']) for n in graph['nodes']}, set(ROLE_LABELS))
        plan = make_plan(graph); validate_plan(plan)
        for node in graph['nodes']:
            role = node.get('role', node['kind'])
            self.assertEqual(node['color'], palette[role])
            self.assertEqual(node['color_source'], 'role_palette')
            self.assertEqual(node_border(node), node_border(before[node['id']]))
            self.assertEqual(node['label'], before[node['id']]['label'])
            self.assertEqual((node['x'], node['y']), (before[node['id']]['x'], before[node['id']]['y']))
            shape = next(s for s in plan['shapes'] if s['key'] == node['id'])
            self.assertEqual(shape['body']['style']['fillColor'], palette[role])
            for svg, key in ((svg_graph(graph), 'data-key'), (render_svg(graph), 'data-node-id')):
                group = next(e for e in ET.fromstring(svg).iter() if e.get(key) == node['id'])
                self.assertTrue(any(e.get('fill') == palette[role] for e in group.iter()))
        from liquid_tracer.mermaid import _preview_html
        preview = _preview_html(graph, render_svg(graph))
        self.assertIn(palette["event"] + " diamonds: events.", preview)
        self.assertNotIn("Pink diamonds: events.", preview)
        self.assertIn("Pink diamonds: events.", _preview_html(original, render_svg(original)))
        source = mermaid_source(graph)
        for color in palette.values(): self.assertIn('fill:' + color, source)
        self.assertIn(palette['seed'], '\n'.join(legend_lines(graph)))
        self.assertTrue(all(row['color_source'] == 'role_palette' for row in node_csv_rows(graph)))
        self.assertEqual(self.graph()['nodes'], original['nodes'])

    def test_names_override_children_not_seeds_and_conflicts_keep_role_palette(self):
        for role in ('seed', 'candidate', 'unspent_endpoint', 'address'):
            node = {'id': role, 'kind': 'address', 'role': role, 'color': COLORS[role],
                'details': {'network': 'liquid', 'address_attributions': [annotation(name='Example', stop=False)]}}
            apply_name_colors([node], {'example': '#abcdef'}, role_colors={role: '#112233'})
            self.assertEqual(node['color'], '#112233' if role == 'seed' else '#abcdef')
            self.assertEqual(node['role'], role)
        node['color'] = COLORS['address']; node.pop('color_source', None)
        node['details']['address_attributions'].append(annotation(name='Other', stop=False))
        apply_name_colors([node], {'example': '#abcdef', 'other': '#ffffff'}, role_colors={'address': '#112233'})
        self.assertEqual(node['color'], '#112233')
        self.assertTrue(node['details']['name_color_conflict'])

    def test_layout_and_miro_sync_retain_palette_and_manual_edits(self):
        import tempfile
        from pathlib import Path
        graph = self.graph({'seed': '#112233', 'transaction': '#ffffff'})
        compact = compact_graph(optimize_graph(graph))
        self.assertEqual({n['id']: n['color'] for n in compact['nodes']}, {n['id']: n['color'] for n in graph['nodes']})
        with tempfile.TemporaryDirectory() as tmp:
            remote = AnnotationMiro(); path = Path(tmp) / 'miro.json'
            def publish(g): return sync(make_plan(g), 'synthetic-board', path, token='test', transport=remote, interval=0)
            publish(self.graph())
            mapping = read_json(path)['items']; key = 'liquid:address:' + SHARED
            remote.items[mapping[key]['id']]['position'].update(x=-777, y=555)
            publish(graph)
            self.assertEqual({k: v['id'] for k, v in read_json(path)['items'].items()}, {k: v['id'] for k, v in mapping.items()})
            node = remote.items[mapping[key]['id']]
            self.assertEqual(node['style']['fillColor'], '#112233')
            self.assertEqual(node['style']['borderWidth'], '12')
            self.assertEqual(node['position']['x'], -777)
            node['style']['fillColor'] = '#987654'
            publish(self.graph({'seed': '#000000'}))
            self.assertEqual(node['style']['fillColor'], '#987654')


class WebRoleColorTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    create = test_web.LocalWebTests.create

    def test_same_editor_endpoint_without_import_save_reset_and_stale(self):
        _, case = self.create(); path, _ = self.server.case(case['id'])
        endpoint = '/api/cases/' + case['id'] + '/name-colors'
        with patch.object(self.server, 'start_job') as process:
            report = self.success(endpoint, {})
            self.assertEqual(len(report['roles']), 7)
            self.success(endpoint, {'updates': [{'role': 'seed', 'color': '#123ABC'}], 'expected_revision': report['revision']})
            self.assertEqual(load_services(path)['role_colors'], {'seed': '#123abc'})
            self.assertEqual(self.request(endpoint, {'updates': [{'role': 'address', 'color': '#654321'}], 'expected_revision': report['revision']})[0], 400)
            report = self.success(endpoint, {})
            self.success(endpoint, {'updates': [{'role': 'seed', 'color': None}], 'expected_revision': report['revision']})
            self.assertEqual(load_services(path)['role_colors'], {})
            process.assert_not_called()


@unittest.skipUnless(importlib.util.find_spec('textual'), 'optional terminal UI')
class MenuRoleColorTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_menu_addresses.AddressMenuTests.asyncSetUp
    click = test_menu_addresses.AddressMenuTests.click

    async def test_role_selector_uses_existing_palette_save_and_reset(self):
        from textual.widgets import Input, Select
        app = create_app(self.root)
        async with app.run_test(size=(115, 65)) as pilot:
            app.created(self.case); await pilot.pause()
            await self.click(app, pilot, '#name-colors')
            app.screen.query_one('#name-color-role', Select).value = 'seed'
            await pilot.pause()
            self.assertEqual(app.screen.query_one('#name-color-value', Input).value, COLORS['seed'])
            app.screen.query_one('#name-color-value', Input).value = '#112233'
            await self.click(app, pilot, '#name-color-save')
            self.assertEqual(load_services(self.case)['role_colors'], {'seed': '#112233'})
            app.screen.query_one('#name-color-role', Select).value = 'seed'
            await pilot.pause(); await self.click(app, pilot, '#name-color-clear')
            self.assertEqual(load_services(self.case)['role_colors'], {})
