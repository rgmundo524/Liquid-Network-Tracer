"""Attribution caps shape full plots while collection preserves wider evidence."""
import copy
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.api import Limits
from liquid_tracer.address_import import parse_import, preview_import, apply_import
from liquid_tracer.common import TraceError
from liquid_tracer.hop_limits import HopScope, hop_limit_value, output_budget
from liquid_tracer.plot_scope import project_full_scope
from liquid_tracer.services import set_service, load_services, service_labels
from liquid_tracer.investigations import create_investigation
from liquid_tracer.connections import connecting_outpoints
from tests.test_service_stops import network
from tests.test_trace_concurrency import synthetic_trace


class AttributionHopLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ids, self.addresses, self.data = network()
        self.counter = 0

    def key(self, n):
        return self.ids[n] + ':0'

    def rule(self, n='B', hops=1, stop=False):
        return {'kind': 'address', 'value': self.addresses[n], 'stop': stop, 'hop_limit': hops}

    def run_trace(self, rules=(), seeds='A', parent=None, hops=8, workers=1, only=None):
        self.counter += 1
        return synthetic_trace(self.root/str(self.counter), self.data, [self.key(n) for n in seeds],
            workers=workers, limits=Limits(max_hops=hops), labels=list(rules), parent=parent, only=only)

    def test_collection_ignores_cap_but_full_plot_stops_after_consolidation(self):
        for workers in (1, 8):
            state, transport = self.run_trace([self.rule()], workers=workers)
            self.assertEqual(state['status'], 'bounded_complete')
            self.assertEqual(set(state['transactions']), {self.ids[n] for n in 'ABDFG'})
            self.assertIn('/tx/'+self.ids['D']+'/outspends', transport.calls)
            self.assertEqual(state['collection_policy']['attribution_hop_limits'], 'ignore')
            plotted = project_full_scope(state)
            self.assertEqual(set(plotted['transactions']), {self.ids[n] for n in 'ABD'})
            self.assertEqual(set(plotted['links']), {self.key('A'), self.key('B')})
            self.assertEqual(state['labels'], [self.rule()])
            self.assertNotIn('trace_control', state['outputs'][self.key('D')])

    def test_zero_is_ignored_but_explicit_stop_prevents_any_extra_spend(self):
        for rule in (self.rule(hops=0), self.rule('A', 0)):
            state, transport = self.run_trace([rule], workers=8)
            self.assertIn(self.ids['G'], state['transactions'])
            self.assertIn('/tx/'+self.ids['B']+'/outspends', transport.calls)
        for rule in (self.rule(hops=0, stop=True), self.rule(hops=9, stop=True), self.rule('A', 0, stop=True)):
            state, transport = self.run_trace([rule], workers=8)
            target = 'A' if rule['value'] == self.addresses['A'] else 'B'
            self.assertNotIn('/tx/'+self.ids[target]+'/outspends', transport.calls)
            self.assertNotIn(self.ids['D'], state['transactions'])

    def test_seed_budget_is_retained_for_full_plot_only(self):
        state, _ = self.run_trace([self.rule('A', 1)])
        self.assertIn(self.ids['G'], state['transactions'])
        self.assertEqual(set(project_full_scope(state)['transactions']), {self.ids[n] for n in 'AB'})

    def test_continuation_collects_past_display_cap_without_changing_it(self):
        parent, _ = self.run_trace([self.rule()], hops=2)
        original = copy.deepcopy(parent)
        continued, requests = self.run_trace([self.rule()], parent=parent, hops=9)
        self.assertIn('/tx/'+self.ids['D']+'/outspends', requests.calls)
        self.assertEqual(parent, original)
        self.assertIn(self.ids['G'], continued['transactions'])
        self.assertEqual(set(project_full_scope(continued)['transactions']), {self.ids[n] for n in 'ABD'})

    def test_reused_limit_address_does_not_replenish_budget(self):
        state, _ = self.run_trace([self.rule(hops=2)])
        self.assertIn(self.ids['G'], state['transactions'])
        self.assertNotIn(self.ids['G'], project_full_scope(state)['transactions'])

    def test_downstream_larger_limit_cannot_extend_upstream_smaller_limit(self):
        state, _ = self.run_trace([self.rule(hops=1), self.rule('D', 99)])
        self.assertIn(self.ids['G'], state['transactions'])
        self.assertNotIn(self.ids['F'], project_full_scope(state)['transactions'])

    def test_independent_starter_route_can_continue_after_other_route_is_exhausted(self):
        state, _ = self.run_trace([self.rule()], seeds='AE', workers=8)
        self.assertIn(self.ids['F'], state['transactions'])
        self.assertIn(self.ids['G'], state['transactions'])  # F reuses B, then one last hop.

    def test_new_display_limit_does_not_block_selected_frontier_but_stop_does(self):
        parent, _ = self.run_trace(hops=3)
        state, requests = self.run_trace([self.rule()], parent=parent, only={self.key('F')})
        self.assertIn('/tx/'+self.ids['F']+'/outspends', requests.calls)
        self.assertIn(self.ids['G'], state['transactions'])
        stopped, requests = self.run_trace([self.rule(stop=True)], parent=parent, only={self.key('F')})
        self.assertEqual(requests.calls, [])
        self.assertEqual(stopped['links'], parent['links'])
        self.assertEqual(stopped['outputs'][self.key('F')]['status'], 'held_behind_service')
        released, requests = self.run_trace(parent=stopped)
        self.assertNotIn('trace_control', released['outputs'][self.key('F')])
        self.assertIn('/tx/'+self.ids['F']+'/outspends', requests.calls)

    def test_global_hops_remains_an_upper_bound(self):
        state, _ = self.run_trace([self.rule(hops=99)], hops=1)
        self.assertNotIn(self.ids['D'], state['transactions'])

    def test_csv_validation_storage_clear_and_legacy_import(self):
        for raw in ('', None, '  ', 0, '0', 2, ' 2 '):
            self.assertEqual(hop_limit_value(raw), None if raw is None or isinstance(raw,str) and not raw.strip() else int(raw))
        for raw in (True, False, -1, '1.2', 1.0, 'two', '1e2'):
            with self.assertRaises(TraceError): hop_limit_value(raw)
        text = 'Address,Name,confidence,stop_tracing,hop_limit,source,notes\nSYNTHETIC-deposit,Exchange,suspected,false,1,Research,Consolidation\n'
        case = create_investigation(self.root/'case', 'Limits')
        plan = preview_import(case, text)
        apply_import(case, text, approval_sha256=plan['approval_sha256'])
        rule = load_services(case)['rules']['SYNTHETIC-deposit']
        self.assertEqual(rule['hop_limit'], 1)
        self.assertEqual(service_labels(load_services(case))[0]['hop_limit'], 1)
        set_service(case, 'SYNTHETIC-deposit', name='Renamed')
        self.assertEqual(load_services(case)['rules']['SYNTHETIC-deposit']['hop_limit'], 1)
        set_service(case, 'SYNTHETIC-deposit', hop_limit='')
        self.assertIsNone(load_services(case)['rules']['SYNTHETIC-deposit']['hop_limit'])
        self.assertEqual(parse_import('Address,Name\nSYNTHETIC-old,Old\n')['rows'][0]['rule']['hop_limit'], None)

    def test_full_plot_keeps_short_capped_and_long_open_routes_separate(self):
        # E -> C -> B -> D is longer than A -> B -> D. A is capped at B;
        # E remains open but cannot reach F inside the global three-hop bound.
        from tests.fixtures import output, CONFIRMED
        self.data['/tx/'+self.ids['B']]['vin'].append({'txid':self.ids['C'],'vout':0,
                                                     'prevout':output(self.addresses['C'])})
        self.data['/tx/'+self.ids['C']+'/outspends']=[{'spent':True,'txid':self.ids['B'],
                                                   'vin':1,'status':dict(CONFIRMED)}]
        self.data['/tx/'+self.ids['D']]['vin']=self.data['/tx/'+self.ids['D']]['vin'][:1]
        # Place the local cap on A rather than the shared B address.
        state,_=self.run_trace([self.rule('A',2)],seeds='AE',hops=3,workers=8)
        self.assertIn(self.ids['D'],state['transactions'])
        self.assertIn(self.ids['F'],state['transactions'])
        # Its full plot still cannot use the capped short route to reach F.
        self.assertNotIn(self.ids['F'], project_full_scope(state)['transactions'])
        continued,_=self.run_trace([self.rule('A',2)],seeds='AE',parent=state,hops=4,workers=8)
        self.assertIn(self.ids['F'],continued['transactions'])

    def test_output_budget_ignores_caps_without_ignoring_any_explicit_stop(self):
        key = self.key('B')
        output = self.data['/tx/' + self.ids['B']]['vout'][0]
        for kind, value in (('address', self.addresses['B']), ('outpoint', key),
                            ('script', output['scriptpubkey'])):
            label = {'kind': kind, 'value': value, 'hop_limit': 0, 'stop': False}
            self.assertEqual(output_budget([label], key, output), 0)
            self.assertEqual(output_budget([label], key, output, respect_attribution_hops=False), float('inf'))
            label['stop'] = True
            self.assertEqual(output_budget([label], key, output, respect_attribution_hops=False), 0)

    def test_legacy_capped_frontier_releases_with_unchanged_or_removed_cap(self):
        parent, _ = self.run_trace([self.rule()], hops=2)
        # Reproduce the old cap boundary, including its saved restoration state.
        parent.pop('collection_policy')
        item = parent['outputs'][self.key('D')]
        item.update(status='attribution_hop_limit',
                    trace_control={'reason': 'attribution_hop_limit', 'previous_status': 'pending'})
        original = copy.deepcopy(parent)
        for rules in ([self.rule()], []):
            state, calls = self.run_trace(rules, parent=parent, hops=5)
            self.assertIn(self.ids['G'], state['transactions'])
            self.assertIn('/tx/' + self.ids['D'] + '/outspends', calls.calls)
            self.assertNotIn('trace_control', state['outputs'][self.key('D')])
            self.assertEqual(parent, original)

    def test_context_input_and_reused_address_never_seed_collection(self):
        state, calls = self.run_trace([self.rule(hops=0)])
        self.assertIn(self.ids['G'], state['transactions'])
        for name in 'CE':
            self.assertNotIn(self.ids[name], state['transactions'])
            self.assertNotIn('/tx/' + self.ids[name], calls.calls)
        self.assertFalse(any('/address/' in endpoint for endpoint in calls.calls))

    def test_legacy_descendants_held_behind_cap_release_on_selected_continuation(self):
        parent, _ = self.run_trace(hops=3)
        parent['labels'] = [self.rule()]
        parent.pop('collection_policy')
        HopScope(parent)  # Reconstruct the historical cap-respecting controls.
        self.assertEqual(parent['outputs'][self.key('F')]['status'], 'held_behind_service')
        original = copy.deepcopy(parent)
        for rules in ([self.rule()], []):
            state, calls = self.run_trace(rules, parent=parent, hops=5, only={self.key('F')})
            self.assertIn(self.ids['G'], state['transactions'])
            self.assertIn('/tx/' + self.ids['F'] + '/outspends', calls.calls)
            self.assertNotIn('trace_control', state['outputs'][self.key('F')])
            self.assertEqual(parent, original)

    def test_script_and_outpoint_stops_hold_existing_descendants_until_removed(self):
        output = self.data['/tx/' + self.ids['B']]['vout'][0]
        output['scriptpubkey'] = '0014' + 'b' * 40
        self.data['/tx/' + self.ids['D']]['vin'][0]['prevout']['scriptpubkey'] = output['scriptpubkey']
        parent, _ = self.run_trace(hops=3)
        for kind, value in (('outpoint', self.key('B')), ('script', output['scriptpubkey'])):
            rule = {'kind': kind, 'value': value, 'stop': True}
            stopped, calls = self.run_trace([rule], parent=parent, only={self.key('F')})
            self.assertEqual(calls.calls, [])
            self.assertEqual(stopped['links'], parent['links'])
            self.assertEqual(stopped['outputs'][self.key('F')]['status'], 'held_behind_service')
            self.assertEqual(stopped['outputs'][self.key('B')]['trace_control']['reason'], 'analyst_stop')
            resumed, calls = self.run_trace(parent=stopped, only={self.key('F')})
            self.assertIn(self.ids['G'], resumed['transactions'])
            self.assertIn('/tx/' + self.ids['F'] + '/outspends', calls.calls)

    def test_generic_seed_stop_restores_original_frontier_after_removal(self):
        for workers in (1, 8):
            rule = {'kind': 'outpoint', 'value': self.key('A'), 'stop': True}
            parent, calls = self.run_trace([rule], workers=workers)
            self.assertEqual(set(parent['transactions']), {self.ids['A']})
            self.assertEqual(parent['outputs'][self.key('A')]['status'], 'analyst_stop')
            self.assertNotIn('/tx/' + self.ids['A'] + '/outspends', calls.calls)
            resumed, _ = self.run_trace(parent=parent, workers=workers)
            self.assertIn(self.ids['G'], resumed['transactions'])
            self.assertNotIn('trace_control', resumed['outputs'][self.key('A')])

    def test_merge_highlights_do_not_propagate_exhausted_origins(self):
        from tests.test_attribution_convergence import graph_state, tx
        from liquid_tracer.export import build_graph
        state=graph_state((('a:0','c'),('c:0','d'),('b:0','d')))
        address=state['transactions'][tx('a')]['data']['vout'][0]['scriptpubkey_address']
        state['labels']=[{'kind':'address','value':address,'stop':False,'hop_limit':1}]
        graph=build_graph(state)
        d=next(n for n in graph['nodes'] if n['id']=='tx:'+tx('d'))
        self.assertNotIn('convergence',d)
        state['labels'][0]['hop_limit']=2
        graph=build_graph(state)
        self.assertIn('convergence',next(n for n in graph['nodes'] if n['id']=='tx:'+tx('d')))

    def test_connection_search_cannot_use_another_roots_allowance(self):
        state, _ = self.run_trace(seeds='AEG')
        state['labels'] = [self.rule(hops=1)]
        report = connecting_outpoints(state, 10)
        self.assertNotIn((self.ids['A'], self.ids['G']), {(r['source'],r['target']) for r in report['pairs']})
        self.assertIn((self.ids['E'], self.ids['G']), {(r['source'],r['target']) for r in report['pairs']})


if __name__ == '__main__':
    unittest.main()
