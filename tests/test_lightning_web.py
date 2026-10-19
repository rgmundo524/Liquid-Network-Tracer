"""Offline workspace jobs for Lightning lookups and their cancellation boundary."""
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.job_resources import job_resources, conflicts
from tests import test_web as web_tests

BTC='a'*64
BTC2='b'*64
CHANNEL_ID=str((100 << 40) | (2 << 16) | 1)

def fixtures(base):
    btc=base/'bitcoin.json'; index=base/'lightning.json'
    tx={'txid':BTC,'vin':[{'txid':'c'*64,'vout':0}], 'vout':[
        {'scriptpubkey':'0014'+'33'*20,'scriptpubkey_type':'v0_p2wpkh','value':2000},
        {'scriptpubkey':'0020'+'11'*32,'scriptpubkey_type':'v0_p2wsh','value':100000}],
        'status':{'confirmed':True,'block_height':100,'block_hash':'d'*64,'block_time':1700000000}}
    channel={'id':CHANNEL_ID,'short_id':'100x2x1','capacity':100000,
        'transaction_id':BTC,'transaction_vout':1,'closing_transaction_id':None,
        'node_left':{'public_key':'02'+'79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798','alias':'First node'},
        'node_right':{'public_key':'03'+'79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798','alias':'Second node'}}
    tx2={**tx,'txid':BTC2,'vin':[{'txid':'d'*64,'vout':0}],
         'status':{**tx['status'],'block_height':101,'block_hash':'e'*64}}
    save_json(btc,{'/tx/'+BTC:tx,'/tx/'+BTC2:tx2})
    path='/lightning/channels/txids?txId[]='+BTC
    path2='/lightning/channels/txids?txId[]='+BTC2
    channel2={**channel,'id':str((101 << 40) | (2 << 16) | 1),'short_id':'101x2x1','transaction_id':BTC2}
    save_json(index,{path:[{'inputs':{},'outputs':{'1':channel}}],path2:[{'inputs':{},'outputs':{'1':channel2}}]})
    return btc,index,path

class LightningWebTests(unittest.TestCase):
    close_server=web_tests.LocalWebTests.close_server
    request=web_tests.LocalWebTests.request
    success=web_tests.LocalWebTests.success
    wait=web_tests.LocalWebTests.wait

    def setUp(self):
        web_tests.LocalWebTests.setUp(self)
        self.btc,self.index,self.path=fixtures(self.base)
        self.route='/api/tools/lightning-network'
        self.body={'action':'lightning-lookup','txid':BTC}

    def offline(self,request,result,live=False):
        self.assertFalse(live)
        payload=read_json(request)
        self.assertEqual(payload['arguments'][0],'lightning-lookup')
        self.assertNotIn('terminal_handoff',payload)
        payload['arguments'] += ['--bitcoin-fixture',str(self.btc),'--mempool-fixture',str(self.index)]
        save_json(request,payload)
        return web_tests.worker_command(request,result,live=False)

    def test_actual_worker_returns_funding_evidence_without_case_or_credentials(self):
        lock=Mock(acquire=Mock(side_effect=AssertionError('No credential gate')))
        with patch('liquid_tracer.web.worker_command',side_effect=self.offline),patch.object(self.server,'credential_lock',lock):
            job=self.success(self.route,self.body,202)
            self.assertIsNone(job['case_id']);self.assertTrue(job['cancellable'])
            self.assertEqual(job['resource_kind'],'lightning_lookup')
            result=self.wait(job)
        lock.acquire.assert_not_called()
        self.assertEqual(result['classifications'],['indexed_channel_funding'])
        self.assertEqual(result['matches'][0]['funding_vout'],1)
        self.assertEqual(self.success('/api/session')['cases'],[])
        self.assertFalse(self.server.root.exists(),'Lookup must not create an investigation or history library')

    def test_csv_batch_returns_one_validated_report_per_transaction(self):
        body={'action':'lightning-lookup','txids':[BTC,BTC2]}
        with patch('liquid_tracer.web.worker_command',side_effect=self.offline):
            result=self.wait(self.success(self.route,body,202))
        self.assertEqual(result['kind'],'lightning_batch')
        self.assertEqual(result['query']['bitcoin_txids'],[BTC,BTC2])
        self.assertEqual([row['query']['bitcoin_txid'] for row in result['reports']],[BTC,BTC2])
        self.assertEqual(result['summary'],{'total':2,'matched':2,'no_matches':0,'incomplete':0})

    def test_empty_index_is_unresolved_and_fetch_failure_is_not_negative_evidence(self):
        save_json(self.index,{self.path:[{'inputs':{},'outputs':{}}]})
        with patch('liquid_tracer.web.worker_command',side_effect=self.offline):
            empty=self.wait(self.success(self.route,self.body,202))
            save_json(self.index,{self.path:{'invalid':'not an index response'}})
            failed=self.wait(self.success(self.route,self.body,202))
        self.assertEqual(empty['status'],'complete')
        self.assertEqual(empty['classifications'],['no_indexed_channel_found'])
        self.assertIn('private',empty['notice'].lower())
        self.assertEqual(failed['status'],'error')
        self.assertNotIn('no_indexed_channel_found',failed['classifications'])

    def test_admission_is_fast_and_duplicate_lookup_conflicts_but_other_jobs_do_not(self):
        entered,release=threading.Event(),threading.Event()
        def blocked(*args,**kwargs):
            entered.set();release.wait(5)
        self.addCleanup(release.set)
        with patch.object(self.server,'run_job',side_effect=blocked):
            job=self.success(self.route,self.body,202)
            self.assertTrue(entered.wait(1))
            start=time.monotonic();jobs=self.success('/api/jobs')
            self.assertLess(time.monotonic()-start,1)
            self.assertTrue(any(item['id']==job['id'] for item in jobs['jobs']))
            self.assertEqual(self.request(self.route,self.body)[0],409)
            resource=job_resources(['lightning-lookup','--txid',BTC],'lightning-lookup')
            self.assertFalse(conflicts(resource,{'resource_kind':'pegin_index','resource_key':'workspace'}))
            self.assertFalse(conflicts(resource,{'resource_kind':'plot','case_id':'case'}))
            self.assertTrue(conflicts(resource,resource))
            release.set()

    def test_browser_cannot_supply_paths_hosts_or_limits(self):
        with patch.object(self.server,'start_job') as start:
            for body in ({'action':'lightning-lookup','txid':'bad'},
                {**self.body,'fixture':str(self.btc)},{**self.body,'base_url':'https://untrusted.invalid'},
                {**self.body,'max_requests':5000},{**self.body,'case_id':'c'*32},{**self.body,'action':'trace'}):
                with self.subTest(body=body):self.assertEqual(self.request(self.route,body)[0],400)
            for body in ({'action':'lightning-lookup','txids':[]},
                         {'action':'lightning-lookup','txids':[BTC,BTC]},
                         {'action':'lightning-lookup','txids':[BTC],'boltz_swap_id':'known'}):
                with self.subTest(body=body):self.assertEqual(self.request(self.route,body)[0],400)
            start.assert_not_called()

    def test_public_result_refuses_wrong_query_hash(self):
        with self.assertRaises(TraceError):
            self.server.public_result({'query':{'bitcoin_txid':'b'*64}},'lightning-lookup',None,[BTC])

    def test_real_worker_cancellation_terminates_lookup_without_false_result(self):
        marker=self.base/'fetch-started'
        script=('import pathlib,sys,time\nfrom liquid_tracer.api import Esplora\n'
            'from liquid_tracer.web_worker import main\noriginal=Esplora.get\n'
            'def delayed(self,*args,**kwargs):\n    pathlib.Path(sys.argv[3]).touch()\n'
            '    time.sleep(30)\n    return original(self,*args,**kwargs)\n'
            'Esplora.get=delayed\nraise SystemExit(main(sys.argv[1:3]))\n')
        def slow(request,result,live=False):
            self.offline(request,result,live)
            return [sys.executable,'-c',script,str(request),str(result),str(marker)]
        with patch('liquid_tracer.web.worker_command',side_effect=slow):
            job=self.success(self.route,self.body,202);deadline=time.monotonic()+8
            while not marker.exists() and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue(marker.exists())
            self.success('/api/jobs/'+job['id']+'/cancel',{},202)
            while time.monotonic()<deadline:
                terminal=self.success('/api/jobs/'+job['id'])
                if terminal['status'] not in ('running','cancelling'):break
                time.sleep(.03)
        self.assertEqual(terminal['status'],'canceled',terminal)
        self.assertNotIn('result',terminal)
        self.assertEqual(self.success('/api/session')['cases'],[])
