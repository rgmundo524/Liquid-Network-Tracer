import copy
import hashlib
import unittest
from liquid_tracer.common import TraceError
from liquid_tracer.lightning_scripts import inspect_input, hash160, taproot_commitment, _tagged, _compact, _add, _mul, G

KEY = bytes.fromhex('0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798')
PREIMAGE = bytes(range(32))
DIGEST = hash160(PREIMAGE)
TIMEOUT = b'\x03\x00\x35\x0c'
SIG = b'\x30' * 71

def legacy(reverse=False):
    return ((b'\x82\x01\x20\x87\x63\xa9\x14'+DIGEST+b'\x88\x21'+KEY+b'\x67\x75') if reverse
        else b'\xa9\x14'+DIGEST+b'\x87\x63\x21'+KEY+b'\x67')+TIMEOUT+b'\xb1\x75\x21'+KEY+b'\x68\xac'

def loop_v2():
    return b'\x21'+KEY+b'\xac\x64\x76\xa9\x14'+hash160(KEY)+b'\x88\xad'+TIMEOUT+b'\xb1\x67\x82\x01\x20\x88\xa9\x14'+DIGEST+b'\x88\x51\xb2\x68'

def wsh(script, stack):
    return {'prevout':{'value':100000, 'scriptpubkey':'0020'+hashlib.sha256(script).hexdigest()},
            'witness':[item.hex() for item in stack+[script]]}

def tr(script, stack):
    internal = KEY[1:]
    root = _tagged('TapLeaf', b'\xc0'+_compact(len(script))+script)
    tweak = int.from_bytes(_tagged('TapTweak', internal+root),'big')
    output = _add(G, _mul(tweak))
    control = bytes([0xc0 | output[1] % 2])+internal
    return {'prevout':{'value':100000,'scriptpubkey':'5120'+output[0].to_bytes(32,'big').hex()},
            'witness':[item.hex() for item in stack+[script,control]]}

class LightningScriptTests(unittest.TestCase):
    def test_boltz_legacy_claim_and_refund(self):
        for reverse in (False,True):
            for preimage,branch in ((PREIMAGE,'claim'),(b'','refund')):
                with self.subTest(reverse=reverse,branch=branch):
                    result=inspect_input(wsh(legacy(reverse),[SIG,preimage]))
                    self.assertEqual(result['status'],'candidate')
                    self.assertEqual(result['branch'],branch)
                    self.assertEqual(result['payment_hash'],DIGEST.hex())
                    self.assertEqual(result['hash_algorithm'],'hash160')
                    self.assertIn('does not establish',result['summary'])
        # Reverse scripts do not take the refund branch just because a 32-byte preimage is wrong.
        self.assertEqual(inspect_input(wsh(legacy(True),[SIG,b'X'*32]))['branch'],'unknown')

    def test_loop_v2_claim_refund_and_bad_refund_key(self):
        self.assertEqual(inspect_input(wsh(loop_v2(),[PREIMAGE,SIG]))['branch'],'claim')
        self.assertEqual(inspect_input(wsh(loop_v2(),[SIG,KEY,b'']))['branch'],'refund')
        self.assertEqual(inspect_input(wsh(loop_v2(),[SIG,b'\x03'+KEY[1:],b'']))['branch'],'unknown')

    def test_nested_witness_requires_both_commitments(self):
        entry=wsh(legacy(),[SIG,PREIMAGE]); redeem=bytes.fromhex(entry['prevout']['scriptpubkey'])
        entry['scriptsig']=(b'\x22'+redeem).hex()
        entry['prevout']['scriptpubkey']='a914'+hash160(redeem).hex()+'87'
        self.assertEqual(inspect_input(entry)['script_type'],'P2SH-P2WSH')
        entry['prevout']['scriptpubkey']='a914'+'00'*20+'87'
        with self.assertRaises(TraceError):inspect_input(entry)

    def test_unrelated_hashlock_and_multisig_are_not_swap_candidates(self):
        for script in (b'\xa9\x14'+DIGEST+b'\x87',b'\x52\x21'+KEY+b'\x21'+KEY+b'\x52\xae',
                       legacy()+b'\x75',b'\x03\x01'):
            with self.subTest(script=script.hex()):
                try:self.assertEqual(inspect_input(wsh(script,[SIG,PREIMAGE]))['status'],'unsupported')
                except TraceError:self.assertEqual(script,b'\x03\x01')

    def test_tampered_or_oversized_witness_is_not_accepted(self):
        entry=wsh(legacy(),[SIG,PREIMAGE]); entry['prevout']['scriptpubkey']='0020'+'00'*32
        with self.assertRaises(TraceError):inspect_input(entry)
        for witness in (['xyz'],['00']*1001,['ff'*400001],None):
            entry=wsh(legacy(),[SIG,PREIMAGE]);entry['witness']=witness
            with self.subTest(witness=str(witness)[:40]),self.assertRaises(TraceError):inspect_input(entry)

    def test_official_bip341_wallet_vector_and_bad_parity(self):
        # bitcoin/bips bip-0341/wallet-test-vectors.json scriptPubKey first script tree.
        program=bytes.fromhex('147c9c57132f6e7ecddba9800bb0c4449251c92a1e60371ee77557b6620f3ea3')
        script=bytes.fromhex('20d85a959b0290bf19bb89ed43c916be835475d013da4b362117393e25a48229b8ac')
        control=bytes.fromhex('c1187791b6f712a8ea41c8ecdd0ee77fab3e85263b37e1ec18a3651926b3a6cf27')
        self.assertEqual(taproot_commitment(program,script,control),0xc0)
        for bad in (bytes([0xc0])+control[1:],control[:-1],control+b'\0',bytes([0xc1])+b'\xff'*32):
            with self.assertRaises(TraceError):taproot_commitment(program,script,bad)

    def test_taproot_claim_templates_and_timeout_only_leaf(self):
        scripts=[(b'\xa9\x14'+DIGEST+b'\x88\x20'+KEY[1:]+b'\xac',[SIG,PREIMAGE],'boltz_taproot_claim'),
                 (b'\x82\x01\x20\x88\xa9\x14'+DIGEST+b'\x88\x20'+KEY[1:]+b'\xac',[SIG,PREIMAGE],'boltz_taproot_reverse_claim'),
                 (b'\x20'+KEY[1:]+b'\xad\x82\x01\x20\x88\xa9\x14'+DIGEST+b'\x88\x51\xb2',[PREIMAGE,SIG],'loop_htlc_v3_claim')]
        for script,stack,name in scripts:
            entry=tr(script,stack);result=inspect_input(entry)
            self.assertEqual((result['template'],result['branch']),(name,'claim'))
            entry['witness'].append('50abcd') # optional annex
            self.assertEqual(inspect_input(entry)['template'],name)
            entry['prevout']['scriptpubkey']='5120'+'00'*32
            with self.assertRaises(TraceError):inspect_input(entry)
        timeout=b'\x20'+KEY[1:]+b'\xad'+TIMEOUT+b'\xb1'
        self.assertEqual(inspect_input(tr(timeout,[SIG]))['status'],'unsupported')

    def test_taproot_keypath_and_no_witness_are_inconclusive(self):
        entry={'prevout':{'scriptpubkey':'5120'+KEY[1:].hex()},'witness':['11'*64]}
        self.assertEqual(inspect_input(entry)['status'],'hidden')
        entry['witness'].append('50')
        self.assertEqual(inspect_input(entry)['status'],'hidden')
        entry['witness']=[]
        with self.assertRaises(TraceError):inspect_input(entry)
