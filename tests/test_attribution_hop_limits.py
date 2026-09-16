"""Path budgets never reset on continuation, reuse or a longer alternate route."""
import copy
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.api import Limits
from liquid_tracer.address_import import parse_import, preview_import, apply_import
from liquid_tracer.common import TraceError
from liquid_tracer.hop_limits import hop_limit_value
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

    def test_one_hop_includes_consolidation_and_stops_before_its_spends(self):
        for workers in (1, 8):
            state, transport = self.run_trace([self.rule()], workers=workers)
            self.assertEqual(state['status'], 'bounded_complete')
            self.assertEqual(set(state['transactions']), {self.ids[n] for n in 'ABD'})
            self.assertEqual(state['outputs'][self.key('D')]['status'], 'attribution_hop_limit')
            self.assertNotIn('/tx/'+self.ids['D']+'/outspends', transport.calls)
            self.assertEqual(set(state['links']), {self.key('A'), self.key('B')})

    def test_zero_and_stop_true_stop_on_arrival_without_any_extra_spend(self):
        for rule in (self.rule(hops=0), self.rule(hops=9, stop=True), self.rule('A', 0)):
            state, transport = self.run_trace([rule], workers=8)
            target = 'A' if rule['value'] == self.addresses['A'] else 'B'
            self.assertNotIn('/tx/'+self.ids[target]+'/outspends', transport.calls)
            self.assertNotIn(self.ids['D'], state['transactions'])

    def test_seed_budget_counts_from_its_selected_output(self):
        state, _ = self.run_trace([self.rule('A', 1)])
        self.assertEqual(set(state['transactions']), {self.ids[n] for n in 'AB'})

    def test_continuation_does_not_reset_limit_and_increasing_it_releases_frontier(self):
        parent, _ = self.run_trace([self.rule()], hops=2)
        original = copy.deepcopy(parent)
        held, requests = self.run_trace([self.rule()], parent=parent, hops=9)
        self.assertEqual(requests.calls, [])
        self.assertEqual(parent, original)
        continued, _ = self.run_trace([self.rule(hops=2)], parent=held, hops=9)
        self.assertIn(self.ids['F'], continued['transactions'])
        self.assertNotIn(self.ids['G'], continued['transactions'])

    def test_reused_limit_address_does_not_replenish_budget(self):
        state, _ = self.run_trace([self.rule(hops=2)])
        self.assertEqual(state['outputs'][self.key('F')]['status'], 'attribution_hop_limit')
        self.assertNotIn(self.ids['G'], state['transactions'])

    def test_downstream_larger_limit_cannot_extend_upstream_smaller_limit(self):
        state, _ = self.run_trace([self.rule(hops=1), self.rule('D', 99)])
        self.assertNotIn(self.ids['F'], state['transactions'])

    def test_independent_starter_route_can_continue_after_other_route_is_exhausted(self):
        state, _ = self.run_trace([self.rule()], seeds='AE', workers=8)
        self.assertIn(self.ids['F'], state['transactions'])
        self.assertIn(self.ids['G'], state['transactions'])  # F reuses B, then one last hop.

    def test_new_limit_holds_saved_descendants_and_only_does_not_bypass_it(self):
        parent, _ = self.run_trace(hops=3)
        state, requests = self.run_trace([self.rule()], parent=parent, only={self.key('F')})
        self.assertEqual(requests.calls, [])
        self.assertEqual(state['links'], parent['links'])
        self.assertEqual(state['outputs'][self.key('F')]['status'], 'held_behind_service')
        released, requests = self.run_trace(parent=state)
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

    def test_exhausted_short_route_cannot_lend_depth_to_longer_open_route(self):
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
        self.assertNotIn(self.ids['F'],state['transactions'])
        continued,_=self.run_trace([self.rule('A',2)],seeds='AE',parent=state,hops=4,workers=8)
        self.assertIn(self.ids['F'],continued['transactions'])

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
