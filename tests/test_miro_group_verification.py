"""Real-contract group reads must confirm pairing, not just a successful POST.

These are deterministic protocol/regression tests, not claims of live-board
validation. Use the public /groups/items endpoint and simulate independent
server-side membership that can disagree with the creation response.
"""
import copy
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from liquid_tracer.common import TraceError, canonical, read_json
from liquid_tracer.export import build_graph
from liquid_tracer.miro import make_plan, publish, sync
from liquid_tracer.miro_address_groups import GROUPS, PENDING

VERIFIED = 'membership_verified'
from liquid_tracer.miro_state import SyncState
from liquid_tracer.progress import public_progress
from tests.test_attribution_convergence import graph_state
from tests.test_presentation_annotations import AnnotationMiro


class IndependentGroupMiro(AnnotationMiro):
    def __init__(self):
        super().__init__()
        self.mode = None
        self.drop_during_update = None
        self.extra_member_page = False

    def __call__(self, method, url, headers, body, timeout):
        path = urlsplit(url).path
        if method == 'GET' and path.endswith('/groups/items') and self.extra_member_page:
            query = parse_qs(urlsplit(url).query)
            group = self.groups.get(query['group_item_id'][0])
            if group is not None:
                self.calls.append((method, url, None))
                if query.get('cursor'):
                    return 200, {}, canonical({'data': [{'id': 'user-extra'}]})
                return 200, {}, canonical({'data': [{'id': i} for i in group['items']], 'cursor': 'next page'})
        if method == 'POST' and path.endswith('/groups') and self.mode:
            mode, self.mode = self.mode, None
            response = super().__call__(method, url, headers, body, timeout)
            gid = 'remote-group-' + str(self.group_counter)
            if mode == 'not_persisted':
                del self.groups[gid]
            elif mode == 'wrong_members':
                self.groups[gid]['items'] = ['not-the-circle', 'not-the-count']
            elif mode == 'lost_after':
                raise TraceError('Synthetic lost group response')
            return response
        response = super().__call__(method, url, headers, body, timeout)
        if method == 'PATCH' and self.drop_during_update is not None:
            self.groups.pop(self.drop_during_update, None)
            self.drop_during_update = None
        return response


class VerifiedGroupingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'miro.json'
        self.graph = build_graph(graph_state())
        self.plan = make_plan(self.graph)
        self.remote = IndependentGroupMiro()
        self.events = []

    def send(self, **kwargs):
        return sync(self.plan, 'synthetic-board', self.path, token='test',
                    transport=self.remote, interval=0, progress=self.events.append, **kwargs)

    def record(self):
        return next(iter(read_json(self.path)[GROUPS].items()))

    def test_post_success_is_followed_by_server_membership_get(self):
        result = self.send()
        groups = result['address_groups']
        self.assertEqual(groups['status'], 'complete')
        self.assertGreater(groups['verified'], 0)
        self.assertEqual(groups['verified'], groups['total'])
        self.assertTrue(all(r[VERIFIED] for r in read_json(self.path)[GROUPS].values()))
        calls = self.remote.calls
        for pos, call in enumerate(calls):
            if call[0] != 'POST' or not urlsplit(call[1]).path.endswith('/groups'):
                continue
            members = frozenset(call[2]['data']['items'])
            gid = next(gid for gid, group in self.remote.groups.items()
                       if frozenset(group['items']) == members)
            self.assertTrue(any(c[0] == 'GET' and urlsplit(c[1]).path.endswith('/groups/items')
                and parse_qs(urlsplit(c[1]).query).get('group_item_id') == [gid] for c in calls[pos+1:]))

    def test_success_without_persistence_is_not_acknowledged(self):
        self.remote.mode = 'not_persisted'
        with self.assertRaises(TraceError):
            self.send()
        state = read_json(self.path)
        self.assertTrue(state[PENDING])
        self.assertFalse(state.get(GROUPS))
        self.assertTrue(next(iter(state[PENDING].values()))['response_id'])
        self.assertFalse(any(e['phase'] == 'complete' for e in self.events))

    def test_echoed_post_members_do_not_override_different_server_membership(self):
        self.remote.mode = 'wrong_members'
        with self.assertRaisesRegex(TraceError, 'not verified'):
            self.send()
        self.assertTrue(read_json(self.path)[PENDING])
        self.assertFalse(read_json(self.path).get(GROUPS))
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, 'miro-resolve'):
            self.send()
        self.assertEqual(writes, len(self.remote.writes))

    def test_members_on_later_pages_cannot_be_ignored(self):
        self.remote.extra_member_page = True
        with self.assertRaisesRegex(TraceError, 'not verified'):
            self.send()
        self.assertTrue(read_json(self.path)[PENDING])
        self.assertFalse(read_json(self.path).get(GROUPS))

    def test_group_lost_after_preflight_is_repaired_in_same_sync(self):
        self.send()
        _, record = self.record()
        before = {k: v['id'] for k, v in read_json(self.path)['items'].items()}
        next(n for n in self.graph['nodes'] if n['id'] == record['host'])['tx_count'] = 54321
        self.plan = make_plan(self.graph)
        self.remote.drop_during_update = record['id']
        result = self.send(max_items=0)
        self.assertTrue(any(set(g['items']) == set(record['members']) for g in self.remote.groups.values()))
        self.assertEqual(result['address_groups']['repaired'], 1)
        self.assertEqual(result['address_groups']['status'], 'complete')
        self.assertEqual(before, {k: v['id'] for k, v in read_json(self.path)['items'].items()})
        self.assertNotIn(record['id'], self.remote.groups)
        self.assertTrue(any(set(g['items']) == set(record['members']) for g in self.remote.groups.values()))

    def test_old_unverified_missing_record_does_not_permanently_block_grouping(self):
        self.send()
        key, record = self.record()
        state = read_json(self.path)
        record.pop(VERIFIED)
        with SyncState(self.path, state) as journal:
            journal.commit(sets=[((GROUPS, key), record)])
        del self.remote.groups[record['id']]
        ids = set(self.remote.items)
        result = self.send(max_items=0)
        self.assertEqual(result['address_groups']['repaired'], 1)
        self.assertEqual(result['address_groups']['status'], 'complete')
        self.assertEqual(ids, set(self.remote.items))
        self.assertTrue(read_json(self.path)[GROUPS][key][VERIFIED])

    def test_different_user_group_preserved_and_not_reported_complete(self):
        self.send()
        _, record = self.record()
        self.remote.groups[record['id']]['items'].append('user-note')
        before = copy.deepcopy(self.remote.groups)
        self.events.clear()
        result = self.send(max_items=0)
        self.assertEqual(before, self.remote.groups)
        report = result['address_groups']
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['total'] - report['verified'], 1)
        self.assertFalse(any(e['phase'] == 'complete' for e in self.events))
        self.assertEqual(self.events[-1]['phase'], 'grouping_incomplete')
        public = public_progress(self.events[-1])
        self.assertIn('not grouped', public['message'])

    def test_manual_ungrouping_of_verified_pair_remains_preserved(self):
        self.send()
        _, record = self.record()
        del self.remote.groups[record['id']]
        result = self.send(max_items=0)
        self.assertEqual(result['address_groups']['preserved'], 1)
        self.assertEqual(result['address_groups']['status'], 'incomplete')
        self.assertNotIn(record['id'], self.remote.groups)

    def test_repeat_sync_and_count_update_preserve_group_and_connector_ids(self):
        self.send()
        state = read_json(self.path)
        before = copy.deepcopy(state[GROUPS])
        writes = len(self.remote.writes)
        self.send(max_items=0)
        self.assertEqual(writes, len(self.remote.writes))
        host = next(iter(before.values()))['host']
        next(n for n in self.graph['nodes'] if n['id'] == host)['tx_count'] = 123
        self.plan = make_plan(self.graph)
        self.send(max_items=0)
        self.assertEqual(before, read_json(self.path)[GROUPS])
        self.assertEqual({k: v['id'] for k, v in state['items'].items()},
                         {k: v['id'] for k, v in read_json(self.path)['items'].items()})

    def test_lost_success_is_recovered_without_another_post_for_the_pair(self):
        self.remote.mode = 'lost_after'
        with self.assertRaises(TraceError):
            self.send()
        original = copy.deepcopy(self.remote.groups)
        self.send()
        for gid, group in original.items():
            self.assertEqual(group, self.remote.groups[gid])
        posts = [c for c in self.remote.writes if urlsplit(c[1]).path.endswith('/groups')]
        self.assertEqual(len(posts), len(self.remote.groups))
        self.assertFalse(read_json(self.path).get(PENDING))
        self.assertTrue(all(r[VERIFIED] for r in read_json(self.path)[GROUPS].values()))

    def test_snapshot_publisher_also_checks_actual_group_members(self):
        graph = copy.deepcopy(self.graph)
        graph.pop('namespace')
        plan = make_plan(graph)
        result = publish(plan, 'synthetic-board', self.path, token='test',
                         transport=self.remote, interval=0)
        self.assertEqual(result['address_groups']['verified'], len(self.remote.groups))
        self.assertTrue(all(r[VERIFIED] for r in read_json(self.path)[GROUPS].values()))

    def test_dry_run_never_reads_or_changes_board(self):
        result = self.send(dry_run=True)
        self.assertTrue(result['address_groups_to_check'])
        self.assertFalse(self.remote.calls)
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
