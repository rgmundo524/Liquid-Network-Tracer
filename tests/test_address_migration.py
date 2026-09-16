"""Synthetic Miro conversion tests; never contact an investigator's board."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from liquid_tracer.address_migration import apply_merge, preview_merge
from liquid_tracer.cli import main, refresh_presentation
from liquid_tracer.common import TraceError, canonical, digest, read_json, save_json
from liquid_tracer.export import build_graph
from liquid_tracer.miro import make_plan, sync
from liquid_tracer.miro_state import load_state
from tests.fixtures import A, fixture
from tests.test_miro_sync import FakeMiro


class InventoryMiro(FakeMiro):
    def __init__(self):
        super().__init__()
        self.lose_method = None
        self.fail_before = None
        self.page_size = 2
        self.extra_tail = None

    def __call__(self, method, url, headers, body, timeout):
        parsed = urlsplit(url)
        if method == 'GET' and parsed.path.endswith('/connectors'):
            self.calls.append((method, url, None))
            items = [copy.deepcopy(i) for i in self.items.values() if i['type'] == 'connector']
            if self.extra_tail:
                items.append(copy.deepcopy(self.extra_tail))
            offset = int(parse_qs(parsed.query).get('cursor', ['0'])[0])
            data = items[offset:offset + self.page_size]
            result = {'data': data, 'total': len(items)}
            if offset + len(data) < len(items):
                result['cursor'] = str(offset + len(data))
            return 200, {}, canonical(result)
        if self.fail_before == method:
            self.fail_before = None
            raise TraceError('Synthetic disconnect before write')
        response = super().__call__(method, url, headers, body, timeout)
        if self.lose_method == method:
            self.lose_method = None
            raise TraceError('Synthetic lost acknowledged response')
        return response


class AddressMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = self.root / 'case'
        data = self.root / 'api.json'
        save_json(data, fixture())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            status = main(['trace', '--case', str(self.case), '--fixture', str(data), '--seed', A + ':0',
                           '--hops', '3', '--separate-outpoints'])
        self.assertEqual(status, 0)
        metadata = read_json(self.case / 'case.json')
        metadata['miro_board'] = 'SYNTHETIC-BOARD='
        save_json(self.case / 'case.json', metadata)
        self.run = metadata['latest_run']
        self.archive = self.case / 'runs' / self.run
        self.trace = read_json(self.archive / 'trace.json')
        self.path = self.case / 'miro' / (digest(b'SYNTHETIC-BOARD=')[:24] + '.json')
        self.remote = InventoryMiro()
        self.old = make_plan(build_graph(self.trace, merge_addresses=False))
        self.new = make_plan(build_graph(self.trace))
        sync(self.old, 'SYNTHETIC-BOARD=', self.path, transport=self.remote, token='synthetic', interval=0)
        self.remote.calls.clear()
        self.before = {p.relative_to(self.archive): p.read_bytes() for p in self.archive.rglob('*') if p.is_file()}
        self.state = load_state(self.path)

    def preview(self):
        return preview_merge(self.case)

    def apply(self, approval=None):
        return apply_merge(self.case, approval or self.preview()['approval_sha256'],
                           transport=self.remote, token='synthetic', interval=0)

    def assert_archive_intact(self):
        self.assertEqual(self.before, {p.relative_to(self.archive): p.read_bytes()
                                      for p in self.archive.rglob('*') if p.is_file()})

    def test_preview_is_read_only_and_wrong_approval_never_uses_network(self):
        files = {p.relative_to(self.case): p.read_bytes() for p in self.case.rglob('*') if p.is_file()}
        report = self.preview()
        self.assertGreater(report['duplicates_to_remove'], 0)
        self.assertEqual(self.remote.calls, [])
        self.assertEqual(files, {p.relative_to(self.case): p.read_bytes() for p in self.case.rglob('*') if p.is_file()})
        with self.assertRaisesRegex(TraceError, 'exact SHA-256'):
            self.apply('0' * 64)
        self.assertEqual(self.remote.calls, [])

    def test_conversion_reuses_survivors_rewires_then_deletes_and_syncs(self):
        report = self.preview()
        result = self.apply()
        state = load_state(self.path)
        self.assertEqual(state['namespace']['address_mode'], 'merged')
        self.assertNotIn('address_migration', state)
        self.assertEqual(len(state['address_migration_history']), 1)
        self.assertTrue(Path(result['backup']).is_file())
        for new_key, old_key in report['plan']['retained'].items():
            self.assertEqual(state['items'][new_key]['id'], self.state['items'][old_key]['id'])
        for key, entry in report['plan']['rewires'].items():
            record = state['items'][key]
            self.assertEqual(record['source'], entry['logical']['source'])
            self.assertEqual(record['target'], entry['logical']['target'])
            remote = self.remote.items[record['id']]
            self.assertEqual(remote['startItem']['id'], entry['after']['source'])
            self.assertEqual(remote['endItem']['id'], entry['after']['target'])
        methods = [call[0] for call in self.remote.writes]
        self.assertNotIn('POST', methods)
        self.assertLess(max(i for i,m in enumerate(methods) if m == 'PATCH'),
                        min(i for i,m in enumerate(methods) if m == 'DELETE'))
        after = sync(self.new, 'SYNTHETIC-BOARD=', self.path, transport=self.remote, token='synthetic', interval=0)
        self.assertEqual(after['new_shapes'], len(self.new.get('presentation_items', {})))
        self.assertTrue(all(p['kind'] == 'address_count' for p in self.new.get('presentation_items', {}).values()))
        self.assertEqual(after['new_connectors'], 0)
        self.assert_archive_intact()

    def test_manual_duplicate_content_blocks_all_writes(self):
        plan = self.preview()['plan']
        key = plan['removed'][0]
        self.remote.items[self.state['items'][key]['id']]['data']['content'] = 'Investigator note'
        with self.assertRaisesRegex(TraceError, 'manual text/style'):
            self.apply()
        self.assertEqual(self.remote.writes, [])
        self.assert_archive_intact()

    def test_manual_survivor_content_and_position_are_preserved(self):
        plan = self.preview()['plan']
        old_key = next(k for k in plan['retained'].values() if k in plan['aliases'])
        item = self.remote.items[self.state['items'][old_key]['id']]
        item['data']['content'] = 'Retained note'
        item['position'] = {'x': 12345, 'y': -4321}
        self.apply()
        self.assertEqual(item['data']['content'], 'Retained note')
        self.assertEqual(item['position'], {'x': 12345, 'y': -4321})

    def test_unmanaged_connector_on_last_page_blocks_all_writes(self):
        key = self.preview()['plan']['removed'][0]
        self.remote.extra_tail = {'id': 'unmanaged', 'type': 'connector',
            'startItem': {'id': self.state['items'][key]['id']}, 'endItem': {'id': 'external'}}
        with self.assertRaisesRegex(TraceError, 'connector still references'):
            self.apply()
        self.assertEqual(self.remote.writes, [])
        self.assertGreater(sum('/connectors?' in c[1] for c in self.remote.calls), 1)

    def test_manual_rewiring_blocks_all_writes(self):
        key = next(iter(self.preview()['plan']['rewires']))
        self.remote.items[self.state['items'][key]['id']]['endItem']['id'] = 'unrelated'
        with self.assertRaisesRegex(TraceError, 'manually reattached'):
            self.apply()
        self.assertEqual(self.remote.writes, [])

    def test_lost_patch_and_delete_responses_resume_without_recreating_objects(self):
        for method in ('PATCH', 'DELETE'):
            with self.subTest(method=method):
                if method == 'DELETE':
                    self.setUp()
                approval = self.preview()['approval_sha256']
                self.remote.lose_method = method
                with self.assertRaisesRegex(TraceError, 'Synthetic lost'):
                    self.apply(approval)
                self.assertTrue(load_state(self.path)['address_migration'])
                with self.assertRaisesRegex(TraceError, 'unfinished'):
                    sync(self.new, 'SYNTHETIC-BOARD=', self.path, dry_run=True)
                self.assertTrue(self.preview()['resume'])
                self.apply(approval)
                self.assertNotIn('address_migration', load_state(self.path))
                writes = self.remote.writes
                ids = [url for m,url,_ in writes if m == method]
                self.assertEqual(len(ids), len(set(ids)))
                self.assertNotIn('POST', [m for m,_,_ in writes])
                self.assert_archive_intact()

    def test_unsent_patch_is_safe_to_resume(self):
        approval = self.preview()['approval_sha256']
        self.remote.fail_before = 'PATCH'
        with self.assertRaises(TraceError):
            self.apply(approval)
        self.apply(approval)
        self.assertEqual(load_state(self.path)['namespace']['address_mode'], 'merged')

    def test_stale_approval_and_modified_archive_fail_before_network(self):
        approval = self.preview()['approval_sha256']
        state = load_state(self.path)
        state['analyst_note'] = 'New local decision'
        save_json(self.path, state)
        with self.assertRaisesRegex(TraceError, 'exact SHA-256'):
            self.apply(approval)
        self.assertEqual(self.remote.calls, [])
        (self.archive / 'trace.json').write_text((self.archive / 'trace.json').read_text() + '\n')
        with self.assertRaisesRegex(TraceError, 'checksum'):
            self.preview()
        self.assertEqual(self.remote.calls, [])

    def test_missing_duplicate_without_attempt_is_not_treated_as_deleted(self):
        key = self.preview()['plan']['removed'][0]
        del self.remote.items[self.state['items'][key]['id']]
        with self.assertRaisesRegex(TraceError, 'missing or inaccessible'):
            self.apply()
        self.assertEqual(self.remote.writes, [])

    def test_existing_legacy_board_needs_explicit_conversion(self):
        with self.assertRaisesRegex(TraceError, 'legacy per-output circles'):
            sync(self.new, 'SYNTHETIC-BOARD=', self.path, dry_run=True)
        self.assertEqual(self.remote.writes, [])

    def test_new_default_and_saved_previews_merge_without_retracing(self):
        from liquid_tracer.cli import saved_graph
        _, _, graph = saved_graph(self.case)
        self.assertEqual(graph['address_mode'], 'merged')
        self.assertEqual(self.trace['address_mode'], 'outpoint_occurrences')
        self.assert_archive_intact()
