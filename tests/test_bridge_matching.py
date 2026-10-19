"""Four source/bridge CLI paths and shared session continuation, using fixtures only."""
import contextlib
import csv
from hashlib import sha256
import io
import json
from pathlib import Path
import tempfile
import unittest

from liquid_tracer.bridge_matching import match_bridge, inspect_bridge_outputs
from liquid_tracer.cli import main
from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.pegin_matching import read_report, verified_file
from tests.test_pegin_matching import bitcoin
from tests.test_pegin_window import Chain
from tests.test_pegout_matching import fixture, btc_output, request_output, SCRIPT

DEPOSIT, CLAIM, PAYOUT, REQUEST = ('d'*64, 'e'*64, 'a'*64, 'b'*64)
CLAIM2, BAD = 'f'*64, 'c'*64


class BridgeMatchingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = self.root / 'workspace'
        self.owner = self.root / 'session' / '.pegin-matching'
        self.btc_chain, self.liquid_chain = Chain(200), Chain(200)
        self.deposit = self.btc_chain.add(90, DEPOSIT)
        self.deposit.update(vin=[], vout=bitcoin()['vout'])
        self.claim = self.liquid_chain.add(100, CLAIM, target=DEPOSIT)
        self.request = self.liquid_chain.add(110, REQUEST)
        self.request.update(vin=[], vout=[request_output()])
        self.payout = self.btc_chain.add(130, PAYOUT)
        self.payout.update(vin=[], vout=[btc_output()])
        self.btc_fixture, self.liquid_fixture = self.root/'btc.json', self.root/'liquid.json'
        self.endpoint = '/scripthash/' + sha256(bytes.fromhex(SCRIPT)).digest()[::-1].hex() + '/txs/chain'
        self.write()

    def write(self):
        btc, liquid = fixture(self.btc_chain), fixture(self.liquid_chain)
        btc.update({self.endpoint: [self.payout], self.endpoint+'/'+PAYOUT: []})
        save_json(self.btc_fixture, btc)
        save_json(self.liquid_fixture, liquid)

    def run_match(self, **kwargs):
        return match_bridge(self.workspace, report_root=self.owner, public=True,
            bitcoin_fixture=self.btc_fixture, liquid_fixture=self.liquid_fixture, **kwargs)

    def report_file(self, report, name):
        return verified_file(None, report['id'], name, report_root=self.owner)

    def test_real_cli_all_four_single_and_batch_directions_keep_only_session_reports(self):
        for mode in ('single', 'batch'):
            for bridge, network, source, expected in (
                    ('pegin', 'bitcoin', DEPOSIT, 'matched'), ('pegin', 'liquid', CLAIM, 'matched'),
                    ('pegout', 'bitcoin', PAYOUT, 'candidate'), ('pegout', 'liquid', REQUEST, 'candidate')):
                with self.subTest(mode=mode, bridge=bridge, network=network):
                    output = io.StringIO()
                    start, end = (-60, 5) if bridge == 'pegout' and network == 'bitcoin' else (-5, 60)
                    args = ['bridge-match', '--bridge', bridge, '--network', network, '--mode', mode,
                        '--txid' if mode == 'single' else '--txids', source,
                        '--investigations-dir', str(self.workspace), '--report-root', str(self.owner),
                        '--start-minutes', str(start), '--end-minutes', str(end), '--public',
                        '--bitcoin-fixture', str(self.btc_fixture), '--liquid-fixture', str(self.liquid_fixture)]
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(main(args), 0)
                    report = json.loads(output.getvalue())
                    self.assertEqual(report['status'], expected)
                    self.assertEqual(report['query']['mode'], mode)
                    self.assertEqual(report['query']['network'], network)
                    self.assertEqual(report['query']['bridge'], bridge)
                    self.assertEqual(len(report['matches']), 1)
                    if mode == 'batch':
                        self.assertEqual(report['stop_reason'], 'batch_complete')
                    self.assertEqual(read_report(None, report['id'], report_root=self.owner), report)
                    rows = list(csv.DictReader(io.StringIO(self.report_file(report, 'matches.csv').read_text())))
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]['Status'], expected)
                    self.assertIn(source, json.dumps(rows))
        self.assertFalse((self.workspace / '.pegin-matching').exists())
        self.assertTrue((self.workspace / '.pegin-index').is_dir())
        self.assertTrue((self.workspace / '.pegout-index').is_dir())

    def test_reverse_batch_budget_continues_later_rows_without_refetching_completed_source(self):
        self.liquid_chain.add(105, CLAIM2, target=DEPOSIT)
        self.write()
        options = dict(bridge='pegin', network='liquid', mode='batch', txids=[CLAIM, CLAIM2],
                       start_minutes=-5, end_minutes=60)
        first = self.run_match(max_requests=2, **options)
        self.assertEqual(first['status'], 'paused')
        self.assertEqual([row['status'] for row in first['results']], ['matched', 'paused'])
        self.assertEqual(first['coverage']['requests_this_task'], 2)
        continued = self.run_match(max_requests=2, resume_report=first['id'], **options)
        self.assertEqual(continued['status'], 'matched')
        self.assertEqual(continued['stop_reason'], 'batch_complete')
        self.assertEqual(continued['coverage']['requests_this_task'], 2)
        self.assertEqual(continued['results'][0]['checked_at'], first['results'][0]['checked_at'])
        self.assertEqual(continued['results'][0]['carried_from_report'], first['id'])
        self.assertEqual(continued['matches'][0], first['matches'][0])
        checks = read_json(self.report_file(continued, 'evidence.json'))['bitcoin_checks']
        self.assertEqual(len(checks), 2)
        for match in continued['matches']:
            self.assertIn(str(match['bitcoin_observation_id']), checks)
        self.assertEqual(read_report(None, first['id'], report_root=self.owner), first)

    def test_completed_single_continuation_does_no_new_requests_and_changed_query_is_rejected(self):
        options = dict(bridge='pegin', network='liquid', txid=CLAIM, start_minutes=-5, end_minutes=60)
        first = self.run_match(**options)
        repeated = self.run_match(max_requests=1, resume_report=first['id'], **options)
        self.assertEqual(repeated['status'], 'matched')
        self.assertEqual(repeated['coverage']['requests_this_task'], 0)
        self.assertEqual(repeated['matches'], first['matches'])
        for changed in ({'txid': REQUEST}, {'start_minutes': -6}, {'network': 'bitcoin'}, {'bridge': 'pegout'}):
            with self.subTest(changed=changed), self.assertRaises(TraceError):
                self.run_match(resume_report=first['id'], **{**options, **changed})
        self.report_file(first, 'matches.csv').write_text('tampered')
        with self.assertRaisesRegex(TraceError, 'checksums'):
            self.run_match(resume_report=first['id'], **options)

    def test_terminal_errors_do_not_prevent_other_batch_rows_or_completed_download_status(self):
        options = dict(bridge='pegin', network='liquid', mode='batch', txids=[BAD, CLAIM])
        result = self.run_match(**options)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['stop_reason'], 'batch_complete')
        self.assertEqual([row['status'] for row in result['results']], ['error', 'matched'])
        rows = list(csv.DictReader(io.StringIO(self.report_file(result, 'matches.csv').read_text())))
        self.assertEqual({row['Status'] for row in rows}, {'error', 'matched'})
        self.assertEqual({row['Liquid transaction'] for row in rows}, {BAD, CLAIM})

    def test_output_inspection_marks_native_pegout_selectable_without_changing_claim_outputs(self):
        report = inspect_bridge_outputs(REQUEST, bridge='pegout', network='liquid', public=True,
                                        fixture=self.liquid_fixture)
        output, = report['transactions'][0]['outputs']
        self.assertTrue(output['selectable'])
        self.assertTrue(output['is_pegout'])
        self.assertEqual(output['pegout_scriptpubkey'], SCRIPT)
        self.assertEqual(output['script_type'], 'op_return')
        informational = inspect_bridge_outputs(CLAIM, bridge='pegin', network='liquid', public=True,
                                                fixture=self.liquid_fixture)
        self.assertTrue(all(not row['selectable'] for row in informational['transactions'][0]['outputs']))


if __name__ == '__main__':
    unittest.main()
