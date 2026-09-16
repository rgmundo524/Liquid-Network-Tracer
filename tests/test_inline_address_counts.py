"""A count is part of its address shape; group endpoints must never be called."""
import copy
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from liquid_tracer.address_counts import caption, label, position, COUNT_HEIGHT
from liquid_tracer.common import TraceError, canonical, digest, read_json
from liquid_tracer.export import build_graph, svg_graph
from liquid_tracer.layout_preview import render_svg
from liquid_tracer.mermaid import mermaid_source
from liquid_tracer.miro import make_plan, publish, sync, validate_plan
from liquid_tracer.miro_frames import frame_bodies
from liquid_tracer.miro_state import SyncState, load_state
from liquid_tracer.presentation_items import proof
from liquid_tracer.transaction_csv import transaction_csv_rows
from tests.test_attribution_convergence import graph_state
from tests.test_presentation_annotations import AnnotationMiro

NS = '{http://www.w3.org/2000/svg}'


def external_plan(graph):
    """Reconstruct the retired plan solely to test old-board migration."""
    plan = make_plan(graph)
    shapes = {s['key']: s['body'] for s in plan['shapes']}
    for node in graph['nodes']:
        text = caption(node)
        if text is None:
            continue
        host = shapes[node['id']]
        host['data']['content'] = host['data']['content'].removesuffix('<p>' + text + '</p>')
        evidence = proof('address_count', node['id'])
        x, y = position(host)
        plan['shapes'].append({'key': evidence['key'], 'body': {
            'data': {'shape': 'rectangle', 'content': '<p>' + label(node) + '</p>'},
            'position': {'x': x, 'y': y, 'origin': 'center'},
            'geometry': {'width': 96, 'height': COUNT_HEIGHT},
            'style': {'fillColor': '#ffffff', 'fillOpacity': '0.0', 'borderOpacity': '0.0',
                      'borderWidth': '1', 'fontSize': '18', 'color': '#334155', 'textAlign': 'center'}}})
        plan['presentation_items'][evidence['key']] = evidence
    from liquid_tracer.miro import _bounds
    if 'activity_frames' in plan:
        plan['frames'] = frame_bodies(plan['activity_frames'], {
            s['key']: _bounds(s['body'], s['key']) for s in plan['shapes']})
    plan['presentation_version'] = 17
    plan['sha256'] = digest(canonical({k: v for k, v in plan.items() if k != 'sha256'}))
    validate_plan(plan)
    return plan


class NoGroupMiro(AnnotationMiro):
    def __call__(self, method, url, headers, body, timeout):
        if '/groups' in urlsplit(url).path:
            raise AssertionError('The retired grouping API must not be used')
        return super().__call__(method, url, headers, body, timeout)


class InlineCountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'miro.json'
        self.state = graph_state()
        self.state['source'] = 'https://blockstream.info/liquid/api'
        for record in self.state['transactions'].values():
            tx = record['data']
            outputs = list(tx['vout']) + [v.get('prevout', {}) for v in tx['vin']]
            for output in outputs:
                if output.get('scriptpubkey_address'):
                    output['scriptpubkey_address'] = output['scriptpubkey_address'].replace('-', '')
        self.graph = build_graph(self.state)
        self.addresses = [n for n in self.graph['nodes'] if n['kind'] == 'address']
        for node in self.addresses:
            node['tx_count'] = 1234
        self.remote = NoGroupMiro()
        self.events = []

    def send(self, plan=None, **kwargs):
        return sync(plan or make_plan(self.graph), 'board=', self.path, token='test',
                    transport=self.remote, interval=0, progress=self.events.append, **kwargs)

    def seed_legacy(self):
        plan = external_plan(self.graph)
        with patch('liquid_tracer.miro._require_inline_counts'):
            self.send(plan)
        saved = load_state(self.path)
        pairs = {}
        for key, item in plan['presentation_items'].items():
            members = [saved['items'][k]['id'] for k in (item['host'], key)]
            pairs['group:address_count:' + digest(item['host'].encode())] = {
                'host': item['host'], 'label': key, 'members': members,
                'response_id': 'unverified-group'}
        # Simulate failure during verification AFTER circle/count creation.
        with SyncState(self.path, saved) as journal:
            journal.commit(sets=[(('pending_address_count_groups',), pairs),
                                 (('address_count_groups',), {'old-group': {'id': 'legacy-id'}})])
        return plan, load_state(self.path)

    def test_caption_handles_unknown_zero_and_count_without_guessing(self):
        for value, expected in ((None, '??'), (0, '0'), (1234, '1,234'), (True, '??'), (-1, '??')):
            self.assertEqual(caption({'kind': 'address', 'tx_count': value}), 'TX count: ' + expected)
        self.assertIsNone(caption({'kind': 'transaction', 'tx_count': 3}))
        self.assertIsNone(caption({'kind': 'address'}))

    def test_one_circle_contains_count_after_explorer_without_extra_objects(self):
        before = canonical(self.graph)
        rows = transaction_csv_rows(self.graph, self.state)
        plan = make_plan(self.graph)
        validate_plan(plan)
        self.assertEqual(plan['presentation_items'], {})
        self.assertEqual(len(plan['shapes']), len(self.graph['nodes']) + 2)
        shapes = {s['key']: s['body'] for s in plan['shapes']}
        for node in self.addresses:
            body = shapes[node['id']]
            self.assertEqual(body['data']['shape'], 'circle')
            content = body['data']['content']
            self.assertTrue(content.endswith('</a></p><p>TX count: 1,234</p>'))
            self.assertEqual(content.count('TX count:'), 1)
            self.assertIn(node['url'], content)
        self.assertEqual(canonical(self.graph), before)
        self.assertEqual(transaction_csv_rows(self.graph, self.state), rows)

    def test_unknown_and_zero_render_inside_circle_and_without_an_explorer(self):
        for value in (None, 0):
            graph = copy.deepcopy(self.graph)
            node = next(n for n in graph['nodes'] if n['kind'] == 'address')
            node.update(tx_count=value, url=None)
            shape = next(s for s in make_plan(graph)['shapes'] if s['key'] == node['id'])
            self.assertTrue(shape['body']['data']['content'].endswith('<p>' + caption(node) + '</p>'))
            self.assertNotIn('Explorer', shape['body']['data']['content'])

    def test_sync_never_calls_grouping_and_refresh_keeps_circle_and_connector_ids(self):
        first = self.send()
        self.assertNotIn('address_groups', first)
        saved = read_json(self.path)
        ids = {k: v['id'] for k, v in saved['items'].items()}
        node = self.addresses[0]
        actual = self.remote.items[ids[node['id']]]
        actual['position'].update(x=7000, y=-300)
        actual['geometry'].update(width=210, height=210)
        connectors = {k: copy.deepcopy(v) for k, v in self.remote.items.items() if v['type'] == 'connector'}
        node['tx_count'] = 2345
        self.send(max_items=0)
        self.assertEqual(actual['position']['x'], 7000)
        self.assertEqual(actual['geometry']['width'], 210)
        self.assertIn('<p>TX count: 2,345</p>', actual['data']['content'])
        self.assertEqual(ids, {k: v['id'] for k, v in read_json(self.path)['items'].items()})
        self.assertEqual(connectors, {k: v for k, v in self.remote.items.items() if v['type'] == 'connector'})
        writes = len(self.remote.writes)
        self.send(max_items=0)
        self.assertEqual(len(self.remote.writes), writes)
        self.assertEqual(self.events[-1]['phase'], 'complete')
        self.assertFalse(any('grouping' in e['phase'] for e in self.events))

    def test_migration_removes_only_old_count_shapes_and_ignores_pending_groups(self):
        old, saved = self.seed_legacy()
        label_keys = set(old['presentation_items'])
        ids = {k: v['id'] for k, v in saved['items'].items() if k not in label_keys}
        self.remote.items['unrelated-note'] = {'id': 'unrelated-note', 'type': 'shape',
            'data': {'shape': 'rectangle', 'content': 'Analyst note'}}
        self.remote.calls.clear()
        result = self.send(max_items=0)
        current = read_json(self.path)
        self.assertEqual(result['created'], 0)
        self.assertEqual(result['deleted'], len(label_keys))
        self.assertEqual(ids, {k: v['id'] for k, v in current['items'].items()})
        self.assertIn('unrelated-note', self.remote.items)
        self.assertEqual(current['pending_address_count_groups'], saved['pending_address_count_groups'])
        deletes = {urlsplit(c[1]).path.rsplit('/', 1)[-1] for c in self.remote.calls if c[0] == 'DELETE'}
        self.assertEqual(deletes, {saved['items'][k]['id'] for k in label_keys})
        self.assertFalse(any(c[0] == 'POST' for c in self.remote.calls))

    def test_interrupted_count_deletion_recovers_without_replacing_circles(self):
        old, saved = self.seed_legacy()
        ids = {k: v['id'] for k, v in saved['items'].items() if k not in old['presentation_items']}
        self.remote.lose_delete = True
        with self.assertRaises(TraceError):
            self.send(max_items=0)
        self.send(max_items=0)
        self.assertEqual(ids, {k: v['id'] for k, v in read_json(self.path)['items'].items()})

    def test_manually_edited_external_label_is_not_deleted(self):
        old, saved = self.seed_legacy()
        key = next(iter(old['presentation_items']))
        item = self.remote.items[saved['items'][key]['id']]
        item['data']['content'] = '<p>Keep my notes</p>'
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, 'manual edits'):
            self.send()
        self.assertEqual(len(self.remote.writes), writes)
        self.assertEqual(item['data']['content'], '<p>Keep my notes</p>')

    def test_new_snapshot_publishes_inline_and_repeat_is_idempotent(self):
        graph = copy.deepcopy(self.graph)
        graph.pop('namespace')
        plan = make_plan(graph)
        for _ in range(2):
            publish(plan, 'board=', self.path, token='test', transport=self.remote, interval=0)
        self.assertEqual(len(self.remote.items), len(plan['shapes']) + len(plan['connectors']) + len(plan.get('frames', [])))
        self.assertTrue(any('TX count:' in i.get('data', {}).get('content', '') for i in self.remote.items.values()))

    def test_old_external_snapshot_requires_regeneration_before_any_write(self):
        plan = external_plan(self.graph)
        for action in (sync, publish):
            with self.assertRaisesRegex(TraceError, 'fresh preview'):
                action(plan, 'board=', self.path, token='test', transport=self.remote, interval=0)
        self.assertFalse(self.remote.calls)
        self.assertFalse(self.path.exists())

    def test_svg_count_is_below_explorer_and_inside_address(self):
        for node in self.addresses:
            node["label"] = "Suspected Synthetic exchange with a long label\n" + node["label"]
        for renderer, attr in ((svg_graph, 'data-key'), (render_svg, 'data-node-id')):
            root = ET.fromstring(renderer(self.graph))
            for node in self.addresses:
                group = next(e for e in root.iter() if e.get(attr) == node['id'])
                self.assertIn(node['label'], group.find(NS + 'title').text)
                texts = list(group.iter(NS + 'text'))
                count = next(t for t in texts if t.get('class') == 'address-tx-count')
                link = next(t for t in texts if t.text == 'Explorer')
                self.assertEqual(count.text, 'TX count: 1,234')
                self.assertGreater(float(count.get('y')), float(link.get('y')))
                self.assertLess(float(count.get('y')), node['y'] + node['height'] / 2)
                self.assertGreater(float(count.get('y')), node['y'] - node['height'] / 2)
                self.assertEqual(sum(t.get('class') == 'address-tx-count' for t in texts), 1)

    def test_mermaid_count_is_in_native_node_label_not_an_external_postpass(self):
        source = mermaid_source(self.graph)
        self.assertEqual(source.count('<br/>TX count: 1,234'), len(self.addresses))
        self.assertNotIn('external label', source)
        self.assertFalse(any('TX count:' in line and '%%' in line for line in source.splitlines()))

    def test_dry_run_is_offline_and_never_changes_legacy_group_state(self):
        self.seed_legacy()
        before = self.path.read_bytes()
        self.remote.calls.clear()
        self.send(dry_run=True)
        self.assertEqual(self.remote.calls, [])
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
