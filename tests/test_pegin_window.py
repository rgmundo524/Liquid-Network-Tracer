"""Synthetic timestamp searches, with no external APIs or saved case data."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from liquid_tracer.api import Budget, Limits
from liquid_tracer.common import StopRun, TraceError, save_json
from liquid_tracer.pegin_matching import HISTORY, _index, _inputs, _status, match_pegin
from liquid_tracer.pegin_window import WindowDiscovery, gaps
from tests.test_pegin_matching import BTC, CLAIM, OTHER, bitcoin, claim, STATUS

EPOCH = 1700000000


def block_hash(height):
    return format(height + 10000, '064x')


class Chain:
    def __init__(self, tip=1000):
        self.headers = {h: dict(id=block_hash(h), height=h, timestamp=EPOCH+h*60,
                               mediantime=EPOCH+h*60-300, tx_count=1) for h in range(tip+1)}
        self.transactions = {}
        self.history = []
        self.calls = []
        self.responses = {}
        self.store = SimpleNamespace(observations=lambda ids: [self.responses[i] for i in ids])
        self.api = SimpleNamespace(store=self.store, budget=Budget(Limits()), base='fixture://liquid')

    def add(self, height, txid, *, pegin=True, target=BTC):
        tx = claim(txid, target)
        tx['status'] = dict(confirmed=True, block_height=height, block_hash=self.headers[height]['id'],
                            block_time=self.headers[height]['timestamp'])
        if not pegin:
            tx['vin'][0]['is_pegin'] = False
        if height not in self.transactions:
            coinbase = dict(txid=format(height+20000,'064x'), vin=[], vout=[], status=deepcopy(tx['status']))
            self.transactions[height] = [coinbase]
        self.transactions[height].append(tx)
        self.headers[height]['tx_count'] = len(self.transactions[height])
        if pegin:
            self.history.append(tx)
            self.history.sort(key=lambda t: -t['status']['block_height'])
        return tx

    def response(self, endpoint):
        if endpoint.startswith('/blocks'):
            height = int(endpoint.rsplit('/', 1)[-1]) if endpoint != '/blocks' else max(self.headers)
            return [self.headers[h] for h in range(height, max(-1, height-10), -1)]
        if endpoint.startswith('/block/') and endpoint.endswith('/status'):
            return {'in_best_chain':True}
        if endpoint.startswith('/block/'):
            parts = endpoint.split('/')
            height = next(h for h,b in self.headers.items() if b['id'] == parts[2])
            offset = int(parts[4]) if len(parts)>4 else 0
            return self.transactions[height][offset:offset+25]
        if endpoint.startswith(HISTORY+'/'):
            cursor = endpoint.rsplit('/', 1)[-1]
            index = next(i for i,t in enumerate(self.history) if t['txid'] == cursor)
            return self.history[index+1:index+26]
        if endpoint.startswith('/tx/'):
            return next(t for txs in self.transactions.values() for t in txs if t['txid'] == endpoint[4:])
        raise AssertionError('Unexpected route '+endpoint)

    def fetch(self, api, endpoint):
        api.budget.request()
        self.calls.append(endpoint)
        data = deepcopy(self.response(endpoint))
        oid = len(self.responses)+1
        self.responses[oid] = dict(source=api.base, body=json.dumps(data))
        return data, oid


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.db, self.state = self.enterContext(_index(Path(self.temp)))
        self.chain = Chain()

    def search(self, start=100, end=200, pages=100):
        return WindowDiscovery(self.chain.api, self.db, self.state, HISTORY, self.chain.fetch,
                               _inputs, _status, None, pages, dict(confirmed=True, block_time=EPOCH),
                               start/60, end/60)

    def finish(self, search):
        for _ in range(3000):
            if not search.step():
                return search.coverage()
        self.fail('Search failed to terminate')

    def test_historical_jump_does_not_walk_recent_asset_history(self):
        self.chain.add(300, OTHER, target='f'*64)
        self.chain.add(150, CLAIM)
        self.chain.add(20, 'e'*64, target='f'*64)
        search = self.search()
        report = self.finish(search)
        self.assertTrue(report['complete'])
        self.assertLess(report['end_height'], 400)
        self.assertNotIn(HISTORY, self.chain.calls)
        self.assertFalse(any(block_hash(999) in p for p in self.chain.calls))
        self.assertEqual(self.db.execute('SELECT liquid_txid FROM claims WHERE bitcoin_txid=?', (BTC,)).fetchone()[0], CLAIM)
        self.assertIn('selected block range', report['boundary_note'])
        self.assertLess(len([p for p in self.chain.calls if p.startswith('/blocks')]), 40)

    def test_no_anchor_finishes_only_bounded_blocks(self):
        result = self.finish(self.search(300, 301))
        self.assertTrue(result['complete'])
        self.assertGreater(result['start_height'], 150)
        self.assertFalse(any(p.startswith(HISTORY) for p in self.chain.calls))
        self.assertEqual(result['pages_this_task'], 0)

    def test_partial_anchor_block_resumes_all_pages_before_asset_cursor(self):
        self.chain = Chain(300)
        for n in range(40):
            self.chain.add(300, format(n+40000,'064x'), target='f'*64)
        search = self.search(100, 200, pages=1)
        self.assertTrue(search.step())
        self.assertEqual(search.coverage()['covered_ranges'], [])
        with self.assertRaisesRegex(StopRun, 'page_limit'):
            search.step()
        self.assertFalse(any(p.startswith(HISTORY) for p in self.chain.calls))
        resumed = self.search(100,200,pages=1)
        self.assertTrue(resumed.step())
        self.assertIn('/block/'+block_hash(300)+'/txs/25', self.chain.calls)
        self.assertEqual(len(self.db.execute('SELECT * FROM claims').fetchall()), 40)
        self.assertEqual(resumed.coverage()['covered_ranges'], [dict(start_height=300,end_height=300)])

    def test_history_last_height_stays_partial_until_next_page(self):
        self.chain = Chain(300)
        self.chain.add(300, OTHER, target='f'*64)
        for n in range(40):
            self.chain.add(180, format(n+50000,'064x'), target='f'*64)
        search=self.search(100,200)
        search.step() # whole anchor
        search.step() # first 25 at height180
        ranges=search.coverage()['covered_ranges']
        self.assertEqual(ranges, [dict(start_height=181,end_height=300)])
        search.step() # 15 more, still same lastheight
        self.assertEqual(search.coverage()['covered_ranges'], ranges)
        result=self.finish(search)
        self.assertTrue(result['complete'])
        self.assertEqual(len(self.db.execute('SELECT * FROM claims').fetchall()),41)

    def test_overlaps_reuse_ranges_and_expansion_fetches_only_gap(self):
        self.chain.add(300, OTHER, target='f'*64)
        self.chain.add(150, CLAIM)
        first=self.finish(self.search())
        self.chain.calls.clear()
        nested=self.finish(self.search(110,190))
        self.assertTrue(nested['complete'])
        self.assertFalse(any((p.startswith('/block/') and '/txs' in p) or p.startswith(HISTORY) for p in self.chain.calls))
        self.chain.calls.clear()
        expanded=self.finish(self.search(100,400))
        self.assertTrue(expanded['complete'])
        self.assertGreater(expanded['end_height'],first['end_height'])
        self.assertFalse(any(block_hash(150) in p for p in self.chain.calls))

    def test_timestamp_disorder_does_not_stop_on_one_outside_timestamp(self):
        self.chain.add(300, OTHER, target='f'*64)
        self.chain.headers[190]['timestamp']=EPOCH+90*60
        self.chain.add(190,'e'*64,target='f'*64)
        self.chain.add(180,CLAIM)
        search=self.search()
        self.assertTrue(self.finish(search)['complete'])
        self.assertIn((CLAIM,),self.db.execute('SELECT liquid_txid FROM claims').fetchall())

    def test_expansion_can_join_a_previously_saved_lower_cursor(self):
        self.chain.add(300,OTHER,target='f'*64)
        self.chain.add(180,CLAIM)
        search=self.search()
        search.prepare()
        with self.db:
            self.db.execute('INSERT INTO window_cursors VALUES(?,?)',(180,CLAIM))
            self.db.execute('INSERT INTO window_cursor_seen VALUES(?,?)',(180,CLAIM))
            self.db.execute('INSERT INTO window_coverage VALUES(?,?)',(search.window['start_height'],180))
        self.assertTrue(self.finish(search)['complete'])

    def test_reorg_invalidates_height_coverage_and_reuses_immutable_pages(self):
        self.chain.add(300,OTHER,target='f'*64)
        self.chain.add(150,CLAIM)
        self.finish(self.search())
        self.chain.calls.clear()
        self.chain.headers[1000]['id']='f'*64
        search=self.search()
        result=self.finish(search)
        self.assertTrue(result['cache_invalidated_by_reorg'])
        self.assertTrue(result['complete'])
        self.assertGreater(result['reused_block_pages'],0)
        self.assertFalse(any(p.startswith('/block/') and '/txs' in p for p in self.chain.calls))

    def test_cancellation_after_response_does_not_commit_false_coverage(self):
        self.chain=Chain(300)
        self.chain.add(300,CLAIM)
        search=self.search()
        original=search.fetch
        def cancel(api,endpoint):
            response=original(api,endpoint)
            if endpoint.startswith('/block/'):
                raise KeyboardInterrupt
            return response
        search.fetch=cancel
        with self.assertRaises(KeyboardInterrupt):
            search.step()
        self.assertFalse(search.coverage()['complete'])
        self.assertEqual(search.coverage()['covered_ranges'],[])
        self.assertTrue(self.finish(self.search())['complete'])

    def test_malformed_page_does_not_advance_cursor_or_coverage(self):
        self.chain=Chain(300)
        self.chain.add(300,OTHER,target='f'*64)
        self.chain.add(150,CLAIM)
        search=self.search()
        search.step()
        original=search.fetch
        def malformed(api,endpoint):
            data,oid=original(api,endpoint)
            if endpoint.startswith(HISTORY):
                data[0]['status']['block_height']=301
            return data,oid
        search.fetch=malformed
        before=search.coverage()['covered_ranges']
        with self.assertRaisesRegex(TraceError,'descending'):
            search.step()
        self.assertEqual(search.coverage()['covered_ranges'],before)

    def test_future_window_is_not_complete_and_unconfirmed_requires_known_claim(self):
        result=self.finish(self.search(1000,1200))
        self.assertFalse(result['complete'])
        self.assertTrue(result['end_not_yet_covered_by_chain'])
        with self.assertRaisesRegex(TraceError,'confirmed Bitcoin block timestamp'):
            WindowDiscovery(self.chain.api,self.db,self.state,HISTORY,self.chain.fetch,
                            _inputs,_status,None,100,dict(confirmed=False),0,72)

    def test_mid_scan_reorg_does_not_certify_coverage(self):
        search=self.search(300,301)
        original=search.fetch
        def reorg(api,endpoint):
            data,oid=original(api,endpoint)
            return ({'in_best_chain':False},oid) if endpoint.endswith('/status') else (data,oid)
        search.fetch=reorg
        with self.assertRaisesRegex(StopRun,'liquid_chain_changed'):
            self.finish(search)
        self.assertFalse(search.coverage()['complete'])
        self.assertEqual(search.coverage()['covered_ranges'],[])

    def test_rejoining_partial_block_keeps_older_continuation(self):
        self.chain.add(300,OTHER,target='f'*64)
        for n in range(50):
            self.chain.add(180,format(n+50000,'064x'),target='f'*64)
        search=self.search()
        search.prepare()
        saved=format(50039,'064x')
        # Earlier work already processed the prefix through member39 of block180.
        with self.db:
            self.db.executemany('INSERT OR REPLACE INTO claims VALUES(?,?,?,?)',
                                [row for tx in self.chain.history if tx['txid'] in
                                 {format(n+50000,'064x') for n in range(40)} for row in _inputs(tx)])
            self.db.execute('INSERT INTO window_cursors VALUES(?,?)',(180,saved))
            self.db.execute('INSERT INTO window_cursor_seen VALUES(?,?)',(180,saved))
        search.step() # scan anchor block300
        search.step() # page ends at member24, earlier than saved39
        self.assertEqual(self.db.execute('SELECT txid FROM window_cursors WHERE height=180').fetchone()[0],saved)
        self.assertTrue(self.finish(search)['complete'])
        self.assertEqual(len(self.db.execute('SELECT * FROM claims').fetchall()),51)

    def test_interval_subtraction(self):
        self.assertEqual(gaps(10,50,[(0,15),(20,30),(40,90)]),[(16,19),(31,39)])


class WindowIntegrationTests(unittest.TestCase):
    def test_cached_exact_reference_must_be_inside_requested_window(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            chain=Chain(300)
            chain.add(150,CLAIM)
            fixture={'/blocks':chain.response('/blocks'), '/block/'+block_hash(300)+'/status':{'in_best_chain':True}}
            fixture.update({'/blocks/'+str(h):chain.response('/blocks/'+str(h)) for h in chain.headers})
            fixture.update({'/block/'+block_hash(150)+'/txs':chain.response('/block/'+block_hash(150)+'/txs'),
                            '/tx/'+CLAIM:chain.response('/tx/'+CLAIM), HISTORY+'/'+CLAIM:[]})
            save_json(root/'liquid.json',fixture)
            save_json(root/'bitcoin.json',{'/tx/'+BTC:bitcoin()})
            options=dict(workspace=root/'cases',bitcoin_fixture=root/'bitcoin.json',liquid_fixture=root/'liquid.json',public=True)
            with patch('liquid_tracer.api.default_min_interval',return_value=.00001):
                direct=match_pegin(None,BTC,liquid_txid=CLAIM,**options)
                self.assertEqual(direct['status'],'matched')
                outside=match_pegin(None,BTC,start_hours=0,end_hours=1,**options)
                self.assertEqual(outside['status'],'no_match')
                self.assertTrue(outside['coverage']['window']['complete'])
                inside=match_pegin(None,BTC,start_hours=2,end_hours=3,**options)
                self.assertEqual(inside['status'],'matched')
                self.assertEqual(inside['matches'][0]['elapsed_seconds'],150*60)
                self.assertFalse(inside['coverage']['window']['complete'])
                self.assertEqual(inside['coverage']['pages_this_task'],0)
                self.assertTrue(inside['query']['public'])


if __name__ == '__main__':
    unittest.main()
