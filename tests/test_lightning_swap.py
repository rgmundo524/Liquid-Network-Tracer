import hashlib
import unittest
from liquid_tracer.common import TraceError
from liquid_tracer.lightning_swap import transaction_id, validate_swap_id, verify_boltz_swap

class LightningSwapTests(unittest.TestCase):
    def wire(self):
        # One input/output, deliberately synthetic transaction for wire parsing only.
        body='01'+'11'*32+'00000000'+'00'+'ffffffff'+'01'+'e803000000000000'+'01'+'51'
        stripped='02000000'+body+'00000000'
        witness='02000000'+'0001'+body+'01'+'01'+'01'+'00000000'
        identity=hashlib.sha256(hashlib.sha256(bytes.fromhex(stripped)).digest()).digest()[::-1].hex()
        return stripped,witness,identity

    def test_raw_txid_excludes_witness(self):
        plain,witness,identity=self.wire()
        self.assertEqual(transaction_id(plain),identity)
        self.assertEqual(transaction_id(witness),identity)
        for bad in (plain[:-2],plain+'00','zz','00'*10):
            with self.assertRaises(TraceError):transaction_id(bad)

    def test_readonly_provider_match_checks_raw_identity_and_preserves_status_as_reported(self):
        plain,_,identity=self.wire();calls=[]
        def fetch(path):
            calls.append(path)
            return {'id':identity,'hex':plain} if path.endswith('/transaction') else {'status':'invoice.settled','transaction':{'id':'f'*64}}
        result=verify_boltz_swap('test-id',identity,fetch)
        self.assertEqual(result['status'],'verified')
        self.assertEqual(calls,['/swap/submarine/test-id/transaction','/swap/test-id'])
        self.assertEqual(verify_boltz_swap('test-id','a'*64,fetch)['status'],'unmatched')
        with self.assertRaises(TraceError):verify_boltz_swap('test-id',identity,lambda _:{'id':'a'*64,'hex':plain})

    def test_unavailable_status_does_not_erase_verified_funding_reference(self):
        def fetch(path):
            if path.endswith('/transaction'):return {'id':'a'*64}
            raise TraceError('unavailable')
        result=verify_boltz_swap('test-id','a'*64,fetch)
        self.assertEqual(result['status'],'verified')
        self.assertIsNone(result['provider_status'])

    def test_id_validation_blocks_paths_and_invalid_inputs(self):
        for value in ('','../test','https://example.com','a b','x'*129,None):
            with self.assertRaises(TraceError):validate_swap_id(value)
        for value in ({'id':'bad'},None,{'hex':'00'}):
            with self.assertRaises(TraceError):verify_boltz_swap('known','a'*64,lambda _:value)
