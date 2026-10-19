import copy
import unittest
from liquid_tracer.common import TraceError, save_json
from liquid_tracer.lightning_matching import validate_lightning_report, lookup_lightning
from tests import test_lightning_matching as base
from tests.test_lightning_matching import transaction, FUNDING, CLOSING, OTHER
from tests.test_lightning_scripts import legacy, wsh, SIG, PREIMAGE

class LightningInspectionTests(unittest.TestCase):
    setUp = base.LightningMatchingTests.setUp
    lookup = base.LightningMatchingTests.lookup
    def data(self, outputs=1):
        spend_input=wsh(legacy(),[SIG,PREIMAGE])
        output=spend_input['prevout']
        start=transaction(vin=[{'txid':OTHER,'vout':0,'prevout':{'value':110000,'scriptpubkey':'0014'+'11'*20},'witness':['22'*71,'03'+'33'*32]}],
                          vout=[copy.deepcopy(output) for _ in range(outputs)])
        inputs=[{**copy.deepcopy(spend_input),'txid':FUNDING,'vout':i} for i in range(outputs)]
        spend=transaction(CLOSING,vin=inputs,vout=[{'value':90000,'scriptpubkey':'0014'+'11'*20}])
        return {'/tx/'+FUNDING:start, '/tx/'+CLOSING:spend,
                '/tx/'+FUNDING+'/outspends':[{'spent':True,'txid':CLOSING,'vin':i} for i in range(outputs)]}

    def inspect(self,bitcoin=None,**kwargs):
        return self.lookup(index=[{'inputs':{},'outputs':{}}],bitcoin=self.data() if bitcoin is None else bitcoin,**kwargs)

    def test_revealed_direct_spend_is_candidate_and_exact_outpoint_is_preserved(self):
        report=self.inspect()
        self.assertEqual(report['schema_version'],2)
        self.assertEqual(report['classifications'],['no_indexed_channel_found'])
        detail=report['inspection']; self.assertEqual(detail['status'],'complete')
        row=detail['findings'][-1]
        self.assertEqual((row['status'],row['branch'],row['funding_txid'],row['funding_vout']),('candidate','claim',FUNDING,0))
        self.assertEqual((row['spending_txid'],row['spending_input_index']),(CLOSING,0))
        self.assertEqual(report['limits']['requests_used'],6)

    def test_shared_spender_is_fetched_once_and_no_recursive_spend_is_requested(self):
        report=self.inspect(self.data(2))
        self.assertEqual(report['inspection']['checked_outputs'],2)
        self.assertEqual(report['limits']['requests_used'],6)
        self.assertNotIn('/tx/'+CLOSING+'/outspends',[s['endpoint'] for s in report['sources']])

    def test_wrong_outpoint_value_or_witness_never_produces_candidate(self):
        for mutate in (lambda d:d['/tx/'+CLOSING]['vin'][0].update(vout=9),
                       lambda d:d['/tx/'+CLOSING]['vin'][0]['prevout'].update(value=1),
                       lambda d:d['/tx/'+CLOSING]['vin'][0].update(witness=['ff']),
                       lambda d:d['/tx/'+FUNDING+'/outspends'][0].update(vin=9)):
            data=self.data();mutate(data)
            report=self.inspect(data)
            self.assertEqual(report['inspection']['status'],'partial')
            self.assertFalse(any(f['status']=='candidate' for f in report['inspection']['findings']))

    def test_hidden_unspent_and_unsupported_are_distinct(self):
        data=self.data(3)
        for i in range(3):
            data['/tx/'+FUNDING]['vout'][i]['scriptpubkey']='5120'+'33'*32
            data['/tx/'+CLOSING]['vin'][i]['prevout']['scriptpubkey']='5120'+'33'*32
            data['/tx/'+CLOSING]['vin'][i]['witness']=['22'*64]
        data['/tx/'+FUNDING+'/outspends'][1]={'spent':False}
        output=data['/tx/'+FUNDING]['vout'][2];output['scriptpubkey']='0014'+'11'*20
        data['/tx/'+CLOSING]['vin'][2]['prevout']=copy.deepcopy(output)
        result=self.inspect(data)['inspection']
        self.assertEqual([f['status'] for f in result['findings'] if f['role']=='output'],['hidden','unspent','unsupported'])
        self.assertEqual(result['status'],'complete')

    def test_index_failure_does_not_suppress_independent_script_findings(self):
        report=self.lookup(index={'broken':True},bitcoin=self.data())
        self.assertEqual(report['status'],'error')
        self.assertEqual(report['classifications'],[])
        self.assertEqual(report['inspection']['status'],'complete')
        self.assertEqual(report['inspection']['findings'][-1]['status'],'candidate')

    def test_limit_preserves_index_result_but_marks_inspection_partial(self):
        report=self.inspect(max_requests=5)
        self.assertEqual(report['status'],'complete')
        self.assertEqual(report['inspection']['status'],'partial')
        self.assertIn('request_limit',report['inspection']['reason'])
        self.assertEqual(report['limits']['requests_used'],5)

    def test_output_and_spender_limits_are_explicit(self):
        data=self.data(34)
        report=self.inspect(data)
        self.assertEqual(report['inspection']['checked_outputs'],32)
        self.assertEqual(report['inspection']['total_outputs'],34)
        self.assertEqual(report['inspection']['status'],'partial')
        data=self.data(10)
        for i in range(10):
            identity=('%064x' % (i+1))
            data['/tx/'+FUNDING+'/outspends'][i]={'spent':True,'txid':identity,'vin':0}
            data['/tx/'+identity]=transaction(identity,vin=[{**wsh(legacy(),[SIG,PREIMAGE]),'txid':FUNDING,'vout':i}])
        report=self.inspect(data,max_requests=100)
        self.assertEqual(report['inspection']['checked_outputs'],8)
        self.assertIn('8 transactions',report['inspection']['reason'])

    def test_report_rejects_unsupported_claims_wrong_outpoints_and_false_complete(self):
        good=self.inspect()
        mutations=[lambda r:r['inspection']['findings'][-1].update(funding_txid=OTHER),
                   lambda r:r['inspection']['findings'][-1].update(template='made_up'),
                   lambda r:r['inspection'].update(checked_outputs=0),
                   lambda r:r['inspection']['findings'][-1].update(status='unspent'),
                   lambda r:r['inspection']['findings'].append(copy.deepcopy(r['inspection']['findings'][-1]))]
        for mutation in mutations:
            report=copy.deepcopy(good);mutation(report)
            with self.assertRaises(TraceError):validate_lightning_report(report,FUNDING)

    def test_provider_verification_is_optional_and_fixed_to_known_id(self):
        bolt=self.root/'boltz.json'
        save_json(bolt,{'/swap/submarine/test-id/transaction':{'id':FUNDING},'/swap/test-id':{'status':'invoice.settled'}})
        report=self.inspect(boltz_swap_id='test-id',boltz_fixture=bolt)
        provider=report['provider_verification']
        self.assertEqual(provider['status'],'verified')
        self.assertEqual(provider['provider_status'],'invoice.settled')
        self.assertIn('not independently verified',provider['summary'])
        for change in ({'transaction_id':OTHER},{'swap_id':'other'}):
            tampered=copy.deepcopy(report);tampered['provider_verification'].update(change)
            with self.assertRaises(TraceError):validate_lightning_report(tampered,FUNDING)
        with self.assertRaises(TraceError):self.inspect(boltz_swap_id='test-id')
