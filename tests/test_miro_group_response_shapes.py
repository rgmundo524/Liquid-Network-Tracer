"""Miro group API envelopes, including the documented nested member response.

Synthetic IDs only. Contract source: official Platform groups OpenAPI embedded
in https://developers.miro.com/reference/getitemsbygroupid.md (2026-09-16).
The response data is a group object; its data contains ItemPagedResponse records.
These are protocol tests, not a recording of a live investigator board.
"""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from liquid_tracer.common import TraceError, canonical, read_json
from liquid_tracer.export import build_graph
from liquid_tracer.miro import make_plan, publish, sync
from liquid_tracer.miro_address_groups import GROUPS, PENDING, _members, inventory
from liquid_tracer.miro_requests import MiroRequests
from tests.test_attribution_convergence import graph_state
from tests.test_presentation_annotations import AnnotationMiro


def wrapped_members(group_id, members, *, paged=True, **pagination):
    data = [{'id': value, 'type': 'shape'} for value in members]
    if paged:
        data = [{'data': data, 'size': len(data), 'limit': 50}]
    return {'data': {'id': group_id, 'type': 'group', 'data': data}, **pagination}


class WrappedGroupMiro(AnnotationMiro):
    """Keep independent stored membership; only change the wire envelopes."""
    def __call__(self, method, url, headers, body, timeout):
        status, response_headers, raw = super().__call__(method, url, headers, body, timeout)
        path = urlsplit(url).path
        if method == 'GET' and status == 200:
            if path.endswith('/groups'):
                raw = canonical({'data': [
                    {'id': gid, 'type': 'group', 'data': {'data': {'items': group['items']}}}
                    for gid, group in self.groups.items()]})
            elif path.endswith('/groups/items'):
                gid = parse_qs(urlsplit(url).query)['group_item_id'][0]
                raw = canonical(wrapped_members(gid, self.groups[gid]['items']))
        return status, response_headers, raw


class GroupResponseShapeTests(unittest.TestCase):
    def read_members(self, responses, group_id='g1'):
        calls = []
        def transport(method, url, headers, body, timeout):
            calls.append((method, url))
            self.assertLessEqual(len(calls), len(responses), 'Unexpected pagination request')
            return 200, {}, canonical(responses[len(calls) - 1])
        with MiroRequests(transport, interval=0) as requests:
            result = _members(requests, 'https://api.miro.com/v2/boards/b', {}, group_id)
        return result, calls

    def test_documented_group_with_item_paged_response(self):
        members, calls = self.read_members([wrapped_members('g1', ['a', 'b'])])
        self.assertEqual(members, frozenset(('a', 'b')))
        self.assertEqual(len(calls), 1)

    def test_group_wrapper_with_direct_item_array(self):
        self.assertEqual(self.read_members([wrapped_members('g1', ['a', 'b'], paged=False)])[0],
                         frozenset(('a', 'b')))

    def test_legacy_flat_array_is_still_supported(self):
        self.assertEqual(self.read_members([{'data': [{'id': 'a'}, {'id': 'b'}]}])[0],
                         frozenset(('a', 'b')))

    def test_nested_cursors_and_top_level_cursors_are_followed(self):
        for location in ('outer', 'group', 'page'):
            with self.subTest(location=location):
                first = wrapped_members('g1', ['a'])
                container = (first if location == 'outer' else first['data'] if location == 'group'
                             else first['data']['data'][0])
                container['cursor'] = 'next +/='
                second = wrapped_members('g1', ['b'], cursor=None)
                members, calls = self.read_members([first, second])
                self.assertEqual(members, frozenset(('a', 'b')))
                self.assertEqual(parse_qs(urlsplit(calls[1][1]).query)['cursor'], ['next +/='])
                self.assertTrue(all(parse_qs(urlsplit(url).query)['group_item_id'] == ['g1']
                                    for _, url in calls))

    def test_group_identity_must_match_on_every_page(self):
        first = wrapped_members('g1', ['a'], cursor='next')
        with self.assertRaises(TraceError):
            self.read_members([first, wrapped_members('other-group', ['b'])])

    def test_wrong_group_type_and_missing_identity_are_rejected(self):
        for change in ('wrong_id', 'missing_id', 'wrong_type'):
            body = wrapped_members('g1', ['a', 'b'])
            if change == 'wrong_id': body['data']['id'] = 'g2'
            if change == 'missing_id': body['data'].pop('id')
            if change == 'wrong_type': body['data']['type'] = 'shape'
            with self.subTest(change=change), self.assertRaises(TraceError):
                self.read_members([body])

    def test_repeated_cursor_conflicting_cursor_and_empty_continuation_rejected(self):
        first = wrapped_members('g1', ['a'], cursor='next')
        conflict = copy.deepcopy(first)
        conflict['data']['cursor'] = 'different'
        cases = ([first, wrapped_members('g1', ['b'], cursor='next')],
                 [conflict], [wrapped_members('g1', [], cursor='next')],
                 [wrapped_members('g1', ['a'], cursor=123)])
        for bodies in cases:
            with self.subTest(bodies=bodies), self.assertRaises(TraceError):
                self.read_members(bodies)

    def test_missing_member_page_is_not_accepted_as_complete(self):
        body = wrapped_members('g1', ['a', 'b'])
        body['data']['data'][0].update(total=3, offset=0)
        with self.assertRaises(TraceError):
            self.read_members([body])

    def test_member_total_across_two_pages(self):
        first = wrapped_members('g1', ['a'], total=2, cursor='next')
        second = wrapped_members('g1', ['b'], total=2)
        self.assertEqual(self.read_members([first, second])[0], frozenset(('a', 'b')))

    def test_extra_member_on_later_page_is_retained(self):
        first = wrapped_members('g1', ['a', 'b'], cursor='next')
        second = wrapped_members('g1', ['user-note'])
        self.assertEqual(self.read_members([first, second])[0], frozenset(('a', 'b', 'user-note')))

    def test_duplicate_member_across_pages_is_rejected(self):
        with self.assertRaises(TraceError):
            self.read_members([wrapped_members('g1', ['a'], cursor='next'), wrapped_members('g1', ['a'])])

    def test_bad_envelopes_are_not_silently_treated_as_empty(self):
        for body in ({}, {'data': None}, {'data': {}}, {'data': 'private-content'},
                     {'data': {'id': 'g1', 'type': 'group', 'data': [None]}},
                     {'data': {'id': 'g1', 'type': 'group', 'data': [{'data': None}]}}):
            with self.subTest(body=body), self.assertRaises(TraceError) as error:
                self.read_members([body])
            self.assertNotIn('private-content', str(error.exception))

    def test_empty_group_with_observed_zero_is_valid(self):
        self.assertEqual(self.read_members([wrapped_members('g1', [], total=0)])[0], frozenset())

    def test_links_are_not_followed(self):
        first = wrapped_members('g1', ['a'], cursor='next', links={'next': 'https://evil.invalid/'})
        second = wrapped_members('g1', ['b'])
        _, calls = self.read_members([first, second])
        self.assertTrue(all(urlsplit(url).hostname == 'api.miro.com' for _, url in calls))

    def test_documented_nested_inventory_does_not_need_extra_member_requests(self):
        calls = []
        def transport(method, url, headers, body, timeout):
            calls.append(url)
            self.assertTrue(urlsplit(url).path.endswith('/groups'))
            return 200, {}, canonical({'data': [
                {'id': 'g1', 'type': 'group', 'data': {'data': {'items': ['a', 'b']}}}]})
        with MiroRequests(transport, interval=0) as requests:
            result = inventory(requests, 'https://api.miro.com/v2/boards/b', {})
        self.assertEqual(result, {'g1': frozenset(('a', 'b'))})
        self.assertEqual(len(calls), 1)


class GroupResponseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'miro.json'
        self.plan = make_plan(build_graph(graph_state()))
        self.remote = WrappedGroupMiro()

    def send(self):
        return sync(self.plan, 'test-board', self.path, token='synthetic-token',
                    transport=self.remote, interval=0)

    def test_native_wire_shapes_work_in_normal_sync_and_repeat_without_writes(self):
        report = self.send()['address_groups']
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['verified'], report['total'])
        self.assertGreater(report['verified'], 0)
        before = read_json(self.path)
        writes = len(self.remote.writes)
        self.send()
        self.assertEqual(len(self.remote.writes), writes)
        self.assertEqual(before[GROUPS], read_json(self.path)[GROUPS])

    def test_resume_first_group_after_old_parser_failure_without_duplicate_post(self):
        # Reproduce the precise failure location: POST succeeded, then member
        # parsing failed. The next normal sync must reconcile the saved intent.
        with patch('liquid_tracer.miro_address_groups._members', side_effect=TraceError(
                'Miro group pagination is incomplete or malformed; grouping cannot be verified')):
            with self.assertRaisesRegex(TraceError, 'pagination'): self.send()
        before = read_json(self.path)
        self.assertTrue(before[PENDING])
        self.assertFalse(before.get(GROUPS))
        self.assertEqual(len(self.remote.groups), 1)
        group_id = next(iter(self.remote.groups))
        report = self.send()['address_groups']
        after = read_json(self.path)
        self.assertEqual(report['status'], 'complete')
        self.assertFalse(after.get(PENDING))
        self.assertIn(group_id, self.remote.groups)
        self.assertEqual({k: v['id'] for k, v in before['items'].items()},
                         {k: v['id'] for k, v in after['items'].items()})
        self.assertEqual(sum(urlsplit(c[1]).path.endswith('/groups') for c in self.remote.writes),
                         len(self.remote.groups))

    def test_all_25_address_pairs_are_verified_with_documented_responses(self):
        seeds = tuple(f"{number:064x}:0" for number in range(1, 26))
        self.plan = make_plan(build_graph(graph_state(seeds=seeds)))
        report = self.send()['address_groups']
        self.assertEqual((report['total'], report['verified'], report['created']), (25, 25, 25))
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(len(self.remote.groups), 25)

    def test_native_wire_shapes_work_in_snapshot_publication(self):
        graph = build_graph(graph_state()); graph.pop('namespace')
        plan = make_plan(graph)
        result = publish(plan, 'test-board', self.path, token='synthetic-token',
                         transport=self.remote, interval=0)
        self.assertEqual(result['address_groups']['status'], 'complete')
        writes = len(self.remote.writes)
        publish(plan, 'test-board', self.path, token='synthetic-token', transport=self.remote, interval=0)
        self.assertEqual(len(self.remote.writes), writes)


if __name__ == '__main__': unittest.main()
