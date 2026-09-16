"""Address totals are dated external annotations, never UTXO or ownership data."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.address_counts import addresses, annotate, apply_saved_counts, fetch_counts, position
from liquid_tracer.cli import main, saved_graph, verify_export
from liquid_tracer.common import TraceError, canonical, digest, read_json, save_json
from liquid_tracer.export import build_graph, short_address, svg_graph
from liquid_tracer.investigations import create_investigation
from liquid_tracer.layout_preview import render_svg
from liquid_tracer.mermaid import _address_counts_svg, _items
from liquid_tracer.miro import make_plan, sync, validate_plan
from liquid_tracer.transaction_csv import transaction_csv_rows
from tests.fixtures import A, B, fixture
from tests.test_attribution_convergence import graph_state
from tests.test_presentation_annotations import AnnotationMiro

NS = '{http://www.w3.org/2000/svg}'


class AddressCountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        data = fixture()
        state = {'transactions':{key[4:]:{'data':value} for key,value in data.items()
                                if key.startswith('/tx/') and not key.endswith('/outspends')}}
        for index, address in enumerate(addresses(state)):
            data['/address/'+address] = {'address':address,
                'chain_stats':{'tx_count':1000+index, 'funded_txo_count':2000, 'spent_txo_count':1900},
                'mempool_stats':{'tx_count':2, 'funded_txo_count':2, 'spent_txo_count':1}}
        self.fixture = self.root/'fixture.json'; save_json(self.fixture, data)
        self.case = create_investigation(self.root/'cases', 'Counts', fixture=self.fixture, seeds=[A+':0'])
        # Model an older saved run before automatic count hydration.
        with patch("liquid_tracer.cli.ensure_counts", return_value={}):
            self.invoke(['trace','--case',str(self.case),'--fixture',str(self.fixture),'--seed',A+':0','--hops','2'])
        self.run, self.archive, self.graph = saved_graph(self.case)
        self.state = read_json(self.archive/'trace.json')

    def invoke(self, args):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            status = main(args)
        self.assertEqual(status, 0, error.getvalue())
        return json.loads(output.getvalue())

    def test_counts_use_unique_statistics_not_history_and_resume_missing(self):
        before = {p.name:p.read_bytes() for p in self.archive.iterdir() if p.is_file()}
        first = fetch_counts(self.case, max_requests=2)
        self.assertEqual(first['fetched'], 2); self.assertGreater(first['remaining'],0)
        self.assertEqual(first['requests_this_lookup'],2)
        again = fetch_counts(self.case)
        self.assertEqual(again['remaining'],0)
        self.assertEqual(again['fetched'],first['remaining'])
        no_work = fetch_counts(self.case)
        self.assertEqual(no_work['requests_this_lookup'],0)
        with patch('liquid_tracer.api.Esplora.get',side_effect=AssertionError('offline render')):
            _,_,graph = saved_graph(self.case)
            rows = transaction_csv_rows(graph,self.state)
        self.assertEqual(len(rows),len(graph['edges']))
        for node in graph['nodes']:
            if node['kind']=='address':
                record = node['details']['tx_count_observation']
                self.assertEqual(node['tx_count'],record['confirmed_tx_count']+2)
                self.assertTrue(record['observed_at']); self.assertTrue(record['observation_ids'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.archive.iterdir() if p.is_file()})
        verify_export(self.archive)

    def test_unknown_zero_and_foreign_network_not_confused(self):
        node = next(n for n in self.graph['nodes'] if n['kind']=='address')
        address=node['details']['address']
        self.assertIsNone(node['tx_count'])
        self.state['address_tx_counts']={address:{'address':address,'source':self.state['source'],
            'observed_at':'2026-01-01T00:00:00Z','confirmed_tx_count':0,'mempool_tx_count':0}}
        graph=build_graph(self.state)
        self.assertEqual(next(n for n in graph['nodes'] if n['id']==node['id'])['tx_count'],0)
        annotate({'nodes':[dict(copy.deepcopy(node),details={**node['details'],'network':'bitcoin'})]},self.state)
        bitcoin=copy.deepcopy(node);bitcoin['details']['network']='bitcoin'
        annotate({'nodes':[bitcoin]},self.state);self.assertIsNone(bitcoin['tx_count'])
        other=copy.deepcopy(self.state);other['address_tx_counts'][address]['source']='wrong'
        self.assertIsNone(next(n for n in build_graph(other)['nodes'] if n['id']==node['id'])['tx_count'])

    def test_archived_counts_preserved_without_case_cache(self):
        address=addresses(self.state)[0]
        self.state['address_tx_counts']={address:{'address':address,'source':self.state['source'],
            'observed_at':'2026-01-01T00:00:00Z','confirmed_tx_count':12,'mempool_tx_count':3}}
        counts=apply_saved_counts(self.case,self.state)
        self.assertEqual(counts[address]['confirmed_tx_count'],12)

    def test_corrupt_cache_rejected_without_live_requests(self):
        fetch_counts(self.case,max_requests=1)
        path=self.case/'address-counts.json';data=read_json(path)
        next(iter(data['counts'].values()))['confirmed_tx_count']=99999;save_json(path,data)
        with self.assertRaisesRegex(TraceError,'cache'):
            saved_graph(self.case)

    def test_cli_fetch_counts_and_header_stay_independent(self):
        report=self.invoke(['address-counts','--case',str(self.case),'--max-requests','1'])
        self.assertEqual(report['fetched'],1)
        self.assertEqual(set(transaction_csv_rows(self.graph,self.state)[0]),
                         {'Block','Time','Transaction Label','Transaction Hash','Address Label','Address Flags',
                          'Address Hash','Asset Value','Asset','PegOut Value','Direction','Number of I/O'})


class AddressDisplayTests(unittest.TestCase):
    def test_address_only_six_four_shortening_full_data_preserved(self):
        state=graph_state();state["source"]="https://blockstream.info/liquid/api";graph=build_graph(state)
        for node in graph['nodes']:
            if node['kind']=='address':
                address=node['details']['address']
                self.assertIn(address[:6]+'...'+address[-4:],node['label'])
                self.assertIn(address,node['url'])
        self.assertEqual(short_address('short'), 'short')
        self.assertEqual(short_address('abcdefghijklmn'), 'abcdef...klmn')
        self.assertTrue(all(n['details']['transaction']['txid'] in state['transactions']
                            for n in graph['nodes'] if n['kind']=='transaction'))

    def test_native_count_labels_are_above_circles_not_csv_rows(self):
        state=graph_state();graph=build_graph(state)
        address=next(n for n in graph['nodes'] if n['kind']=='address')
        address['tx_count']=1234
        plan=make_plan(graph);validate_plan(plan)
        shapes={s['key']:s['body'] for s in plan['shapes']}
        for key,proof in plan['presentation_items'].items():
            self.assertEqual(proof['kind'],'address_count')
            host=shapes[proof['host']];count=shapes[key]
            self.assertEqual(tuple(count['position'][axis] for axis in ('x','y')),position(host))
            self.assertLess(count['position']['y']+14,host['position']['y']-host['geometry']['height']/2)
        for svg in (svg_graph(graph),render_svg(graph)):
            text=' '.join(ET.fromstring(svg).itertext());self.assertIn('1,234',text)
        self.assertEqual(len(transaction_csv_rows(graph,state)),len(graph['edges']))

    def test_count_refresh_keeps_ids_and_follows_manually_moved_address(self):
        state=graph_state();graph=build_graph(state);remote=AnnotationMiro()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'miro.json'
            def send(): return sync(make_plan(graph),'synthetic-board',path,token='test',transport=remote,interval=0)
            send();mapping=read_json(path)['items']
            host=next(n for n in graph['nodes'] if n['kind']=='address')
            proof=next(p for p in make_plan(graph)['presentation_items'].values() if p['host']==host['id'])
            key=proof['key'];remote.items[mapping[host['id']]['id']]['position'].update(x=777,y=555)
            host['tx_count']=5432;send()
            after=read_json(path)['items'];self.assertEqual(mapping[key]['id'],after[key]['id'])
            count=remote.items[after[key]['id']]
            self.assertEqual(count['data']['content'],'<p>5,432</p>')
            self.assertEqual((count['position']['x'],count['position']['y']),
                             position(remote.items[after[host['id']]['id']]))
            writes=len(remote.writes);send();self.assertEqual(writes,len(remote.writes))

    def test_mermaid_svg_count_uses_actual_circle_coordinates(self):
        graph=build_graph(graph_state());nodes,_,ids=_items(graph)
        node=next(n for n in nodes if n['kind']=='address');node['tx_count']=120
        svg=(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 500 500">'
             f'<g class="node default" id="flowchart-{ids[node["id"]]}-12" transform="translate(100,100)">'
             '<circle cx="0" cy="0" r="80"/></g></svg>').encode()
        root=ET.fromstring(_address_counts_svg(graph,svg));text=root.find('.//'+NS+'text')
        self.assertEqual(text.text,'120');self.assertEqual(float(text.get('y')),-94)
        self.assertEqual(root.get('viewBox'),'0.0 -42.0 500.0 542.0')

if __name__=='__main__':unittest.main()
