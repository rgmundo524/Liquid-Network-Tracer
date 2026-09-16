"""Address circles and their counts are native pairs, never evidence nodes."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from liquid_tracer.common import TraceError, canonical, read_json
from liquid_tracer.export import build_graph
from liquid_tracer.miro import make_plan, publish, resolve, sync
from liquid_tracer.miro_address_groups import GROUPS, PENDING, inventory
from liquid_tracer.miro_requests import MiroRequests, request_credits
from liquid_tracer.progress import public_progress
from liquid_tracer.transaction_csv import transaction_csv_rows
from tests.test_attribution_convergence import graph_state
from tests.test_presentation_annotations import AnnotationMiro


class GroupMiro(AnnotationMiro):
    def __init__(self):
        super().__init__()
        self.failure = None

    def __call__(self, method, url, headers, body, timeout):
        if method == 'POST' and urlsplit(url).path.endswith('/groups') and self.failure:
            failure, self.failure = self.failure, None
            if failure == 'rejected':
                return 400, {}, b'{}'
            if failure == 'lost_before':
                raise TraceError('Synthetic transport failure')
            result = super().__call__(method, url, headers, body, timeout)
            if failure == 'malformed':
                return 201, {}, b'{}'
            raise TraceError('Synthetic response lost after success')
        return super().__call__(method, url, headers, body, timeout)

    def move_group(self, group_id, dx, dy):
        for item_id in self.groups[group_id]['items']:
            position = self.items[item_id]['position']
            position['x'] += dx
            position['y'] += dy


class AddressGroupingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'miro.json'
        self.remote = GroupMiro()
        self.state = graph_state()
        self.graph = build_graph(self.state)
        self.plan = make_plan(self.graph)
        self.events = []

    def send(self, **kwargs):
        return sync(self.plan, 'board=', self.path, token='test', transport=self.remote,
                    interval=0, progress=self.events.append, **kwargs)

    def test_only_matching_address_and_count_grouped_without_changing_plan_or_csv(self):
        before = canonical(self.plan)
        rows = transaction_csv_rows(self.graph, self.state)
        result = self.send()
        saved = read_json(self.path)
        pairs = [proof for proof in self.plan['presentation_items'].values() if proof['kind'] == 'address_count']
        self.assertEqual(len(self.remote.groups), len(pairs))
        self.assertEqual(result['address_groups']['created'], len(pairs))
        self.assertEqual(set(saved['items']), {s['key'] for s in self.plan['shapes'] + self.plan['connectors'] + self.plan.get('frames', [])})
        for proof in pairs:
            members = {saved['items'][key]['id'] for key in (proof['host'], proof['key'])}
            self.assertEqual(sum(set(group['items']) == members for group in self.remote.groups.values()), 1)
        self.assertEqual(before, canonical(self.plan))
        self.assertEqual(rows, transaction_csv_rows(self.graph, self.state))
        self.assertTrue(any(event['phase'] == 'grouping' for event in self.events))

    def test_repeat_sync_does_not_recreate_groups_or_any_other_items(self):
        self.send(); writes = len(self.remote.writes)
        before = copy.deepcopy(read_json(self.path))
        result = self.send(max_items=0)
        self.assertEqual(result['address_groups']['created'], 0)
        self.assertEqual(result['address_groups']['reused'], len(self.remote.groups))
        self.assertEqual(writes, len(self.remote.writes))
        self.assertEqual(before[GROUPS], read_json(self.path)[GROUPS])

    def test_dragging_pair_keeps_count_above_and_refresh_retains_ids_and_connectors(self):
        self.send(); saved = read_json(self.path)
        pair = next(iter(saved[GROUPS].values()))
        host_id, label_id = pair['members']
        original_ids = copy.deepcopy(saved[GROUPS])
        self.remote.move_group(pair['id'], 700, -350)
        host_position = copy.deepcopy(self.remote.items[host_id]['position'])
        label_position = copy.deepcopy(self.remote.items[label_id]['position'])
        connectors = {k:copy.deepcopy(v) for k,v in self.remote.items.items() if v['type'] == 'connector'}
        next(n for n in self.graph['nodes'] if n['id'] == pair['host'])['tx_count'] = 12003
        self.plan = make_plan(self.graph)
        self.send(max_items=0)
        self.assertEqual(self.remote.items[host_id]['position'], host_position)
        self.assertEqual(self.remote.items[label_id]['position'], label_position)
        self.assertEqual(self.remote.items[label_id]['data']['content'], '<p>12,003</p>')
        self.assertEqual(original_ids, read_json(self.path)[GROUPS])
        self.assertEqual(connectors, {k:v for k,v in self.remote.items.items() if v['type'] == 'connector'})

    def test_existing_ungrouped_board_upgraded_without_replacing_shapes(self):
        # Simulate a board synced before native grouping existed, including its
        # existing count shapes and live item mapping.
        from unittest.mock import patch
        with patch('liquid_tracer.miro_address_groups.AddressCountGroups.prepare'), \
             patch('liquid_tracer.miro_address_groups.AddressCountGroups.apply', return_value={'conflicts': []}):
            self.send()
        before = {k:v['id'] for k,v in read_json(self.path)['items'].items()}
        result = self.send(max_items=0)
        self.assertEqual(result['created'], 0)
        self.assertGreater(result['address_groups']['created'], 0)
        self.assertEqual(before, {k:v['id'] for k,v in read_json(self.path)['items'].items()})

    def test_user_regrouping_and_ungrouping_are_preserved(self):
        self.send(); pair = next(iter(read_json(self.path)[GROUPS].values()))
        self.remote.groups[pair['id']]['items'].append('unrelated-user-item')
        groups = copy.deepcopy(self.remote.groups)
        result = self.send(max_items=0)
        self.assertEqual(groups, self.remote.groups)
        self.assertEqual(result['address_groups']['preserved'], 1)
        del self.remote.groups[pair['id']]
        self.send(max_items=0)
        self.assertNotIn(pair['id'], self.remote.groups)

    def test_lost_success_and_malformed_response_recover_by_membership_without_repost(self):
        for failure in ('lost_after', 'malformed'):
            with self.subTest(failure=failure):
                self.path = Path(self.tmp.name) / (failure + '.json'); self.remote = GroupMiro()
                self.remote.failure = failure
                with self.assertRaises(TraceError): self.send()
                self.assertTrue(read_json(self.path)[PENDING])
                existing = copy.deepcopy(self.remote.groups)
                self.send()
                self.assertFalse(read_json(self.path).get(PENDING))
                for gid, group in existing.items(): self.assertEqual(self.remote.groups[gid], group)
                creations = [c for c in self.remote.writes if urlsplit(c[1]).path.endswith('/groups')]
                self.assertEqual(len(creations), len(self.remote.groups))

    def test_unobserved_uncertain_post_requires_explicit_reconciliation(self):
        self.remote.failure = 'lost_before'
        with self.assertRaises(TraceError): self.send()
        saved = read_json(self.path); key = next(iter(saved[PENDING]))
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, 'miro-resolve'): self.send()
        self.assertEqual(writes, len(self.remote.writes))
        resolve(self.path, absent=True, key=key)
        self.send()
        self.assertFalse(read_json(self.path).get(PENDING))
        self.assertTrue(self.remote.groups)

    def test_definite_rejection_leaves_shapes_saved_and_can_retry_grouping(self):
        self.remote.failure = 'rejected'
        with self.assertRaisesRegex(TraceError, 'HTTP 400'): self.send()
        self.assertFalse(read_json(self.path).get(PENDING))
        ids = set(self.remote.items)
        self.send()
        self.assertEqual(ids, set(self.remote.items))

    def test_dry_run_is_offline_and_makes_no_mapping(self):
        result = self.send(dry_run=True)
        self.assertTrue(result['address_groups_to_check'])
        self.assertEqual(self.remote.calls, [])
        self.assertFalse(self.path.exists())

    def test_snapshot_publication_groups_and_reuses_existing_membership(self):
        graph = copy.deepcopy(self.graph); graph.pop('namespace')
        plan = make_plan(graph)
        def send(): return publish(plan, 'board=', self.path, token='test', transport=self.remote, interval=0)
        result = send(); self.assertGreater(result['address_groups']['created'], 0)
        writes = len(self.remote.writes)
        result = send()
        self.assertEqual(result['address_groups']['created'], 0)
        self.assertEqual(writes, len(self.remote.writes))

    def test_request_costs_and_public_progress(self):
        for path in ('/groups', '/groups/items?group_item_id=3', '/groups/3'):
            self.assertEqual(request_credits('GET', 'https://api.miro.com/v2/boards/b' + path), 100)
        self.assertEqual(request_credits('POST', 'https://api.miro.com/v2/boards/b/groups'), 100)
        event = public_progress({'phase':'grouping','completed':1,'total':2,'message':'secret'})
        self.assertNotIn('secret', event['message'])


class GroupInventoryTests(unittest.TestCase):
    def test_all_group_and_member_pages_covered_without_following_untrusted_links(self):
        calls=[]
        def transport(method,url,headers,body,timeout):
            calls.append(url); parsed=urlsplit(url); query=parse_qs(parsed.query)
            if parsed.path.endswith('/groups'):
                if 'cursor' not in query:
                    return 200,{},canonical({'data':[{'id':'g1','items':['a','b']}], 'cursor':'next page',
                                            'links':{'next':'https://evil.invalid/'}})
                return 200,{},canonical({'data':[{'id':'g2'}]})
            if 'cursor' not in query:
                return 200,{},canonical({'data':[{'id':'c'}], 'cursor':'second'})
            return 200,{},canonical({'data':[{'id':'d'}]})
        with MiroRequests(transport,interval=0) as requests:
            result=inventory(requests,'https://api.miro.com/v2/boards/b',{})
        self.assertEqual(result,{'g1':frozenset(('a','b')),'g2':frozenset(('c','d'))})
        self.assertEqual(len(calls),4)
        self.assertTrue(all(urlsplit(url).hostname=='api.miro.com' for url in calls))

    def test_malformed_or_looping_inventory_fails_closed(self):
        for body in ({'data':[{'id':'g','items':[None]}]}, {'data':None},
                     {'data':[], 'cursor':'again'}, {'data':[{'id':'g','items':['a','a']}]},
                     {'data':[{'id':'g','items':['a','b']}],'cursor':'loop'}):
            with self.subTest(body=body):
                def transport(*args): return 200,{},canonical(body)
                with MiroRequests(transport,interval=0) as requests:
                    with self.assertRaises(TraceError): inventory(requests,'https://api.miro.com/v2/boards/b',{})


if __name__ == '__main__': unittest.main()
