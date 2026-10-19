"""Real worker/session boundaries with synthetic explorer responses only."""
import sys
import time
import unittest
from unittest.mock import patch

from liquid_tracer.common import read_json, save_json
from tests import test_web as web_tests
from tests.test_pegin_matching import BTC, CLAIM, claim, bitcoin
from tests.test_pegout_matching import request_output


class BridgeSessionWebTests(unittest.TestCase):
    close_server = web_tests.LocalWebTests.close_server
    request = web_tests.LocalWebTests.request
    success = web_tests.LocalWebTests.success
    wait = web_tests.LocalWebTests.wait

    def setUp(self):
        web_tests.LocalWebTests.setUp(self)
        self.fixture = self.base / 'bridge-fixture.json'
        liquid = claim()
        liquid['vout'].append(request_output())
        save_json(self.fixture, {'/tx/' + BTC: bitcoin(), '/tx/' + CLAIM: liquid})
        self.route = '/api/tools/pegin-matches'
        self.body = dict(action='pegin-match', bridge='pegin', network='liquid', txid=CLAIM)

    def offline(self, request, result, live=False):
        self.assertFalse(live, 'Public fixture lookup must not open credentials')
        payload = read_json(request)
        self.assertNotIn('terminal_handoff', payload)
        args = payload['arguments']
        if args[0] == 'bridge-outputs':
            args.extend(['--fixture', str(self.fixture)])
        else:
            self.assertEqual(args[0], 'bridge-match')
            args.extend(['--bitcoin-fixture', str(self.fixture), '--liquid-fixture', str(self.fixture)])
        save_json(request, payload)
        return web_tests.worker_command(request, result, live=False)

    def test_session_result_downloads_then_cleans_only_its_temp_owner(self):
        with patch('liquid_tracer.web.worker_command', side_effect=self.offline):
            result = self.wait(self.success(self.route, self.body, 202))
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(result['matches'][0]['bitcoin_txid'], BTC)
        self.assertEqual(self.success(self.route + '/' + result['id']), result)
        self.assertEqual(self.success(self.route), {'reports': []})
        self.assertEqual(self.success('/api/session')['cases'], [])
        for download in result['downloads']:
            self.assertEqual(self.request(download['url'])[0], 200)
        session = self.server.bridge_workspace
        self.assertTrue((session / '.pegin-matching' / 'pegin-matches' / result['id']).is_dir())
        self.assertFalse((self.server.root / '.pegin-matching').exists())
        index = self.server.root / '.pegin-index'
        self.assertTrue(index.is_dir())
        old = self.server.root / '.pegin-matching' / 'pegin-matches' / ('f' * 32)
        old.mkdir(parents=True)
        (old / 'untouched.txt').write_text('existing archive')
        self.close_server()
        self.assertFalse(session.exists())
        self.assertEqual((old / 'untouched.txt').read_text(), 'existing archive')
        self.assertTrue(index.is_dir())

    def test_liquid_pegout_output_lookup_exposes_bitcoin_destination(self):
        with patch('liquid_tracer.web.worker_command', side_effect=self.offline):
            result = self.wait(self.success(self.route + '/outputs',
                dict(bridge='pegout', network='liquid', txid=CLAIM), 202))
        self.assertEqual(result['blockchain'], 'liquid')
        outputs = result['transactions'][0]['outputs']
        selected = [output for output in outputs if output['selectable']]
        self.assertEqual(len(selected), 1)
        self.assertTrue(selected[0]['is_pegout'])
        self.assertEqual(selected[0]['pegout_address'], 'synthetic-btc')
        self.assertEqual(selected[0]['script_type'], 'op_return')

    def test_cancellation_recovers_only_the_current_jobs_report(self):
        marker = self.base / 'fetch-started'
        script = ("import pathlib, sys, time\n"
                  "from liquid_tracer.api import Esplora\n"
                  "from liquid_tracer.web_worker import main\n"
                  "original = Esplora.get\n"
                  "def delayed(self, *args, **kwargs):\n"
                  "    pathlib.Path(sys.argv[3]).touch()\n"
                  "    time.sleep(30)\n"
                  "    return original(self, *args, **kwargs)\n"
                  "Esplora.get = delayed\n"
                  "raise SystemExit(main(sys.argv[1:3]))\n")
        def slow(request, result, live=False):
            self.offline(request, result, live)
            return [sys.executable, '-c', script, str(request), str(result), str(marker)]
        with patch('liquid_tracer.web.worker_command', side_effect=self.offline):
            previous = self.wait(self.success(self.route, self.body, 202))
        with patch('liquid_tracer.web.worker_command', side_effect=slow):
            job = self.success(self.route, self.body, 202)
            deadline = time.monotonic() + 8
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(marker.exists())
            self.success('/api/jobs/' + job['id'] + '/cancel', {}, 202)
            terminal = None
            while time.monotonic() < deadline:
                terminal = self.success('/api/jobs/' + job['id'])
                if terminal['status'] not in ('running', 'cancelling'):
                    break
                time.sleep(.03)
        self.assertEqual(terminal['status'], 'canceled', terminal)
        result = terminal['result']
        self.assertNotEqual(result['id'], previous['id'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(self.success(self.route + '/' + result['id']), result)
        self.assertEqual(self.success(self.route + '/' + previous['id']), previous)
        self.assertEqual(self.success(self.route), {'reports': []})
        csv = next(item for item in result['downloads'] if item['name'] == 'matches.csv')
        self.assertEqual(self.request(csv['url'])[0], 200)
