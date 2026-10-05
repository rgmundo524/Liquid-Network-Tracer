"""Scope preparation is visible before validation and keeps browsing responsive."""
import contextlib
import io
import json
import sys
import time
import unittest
from unittest.mock import patch

from liquid_tracer.cli import main, parser
from liquid_tracer.job_resources import conflicts, job_resources
from liquid_tracer.web import RequestError
from tests import test_web
from tests.test_attribution_convergence import graph_state
from tests.test_connections import saved_case


class ScopeAnalysisWorkflowTests(unittest.TestCase):
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    create = test_web.LocalWebTests.create
    wait = test_web.LocalWebTests.wait

    def setUp(self):
        test_web.LocalWebTests.setUp(self)
        _, self.metadata = self.create()
        self.case, _ = self.server.case(self.metadata['id'])
        state, archive = saved_case(self.case, graph_state(
            (("a:0", "b"), ("b:0", "c")), seeds=("a:0",)))
        from liquid_tracer.common import digest
        observation_bytes = b'[]\n'
        (archive / 'evidence-index.json').write_bytes(observation_bytes)
        with (archive / 'SHA256SUMS').open('a') as manifest:
            manifest.write(digest(observation_bytes) + '  evidence-index.json\n')
        self.run = state['run_id']
        self.route = '/api/cases/' + self.metadata['id']
        self.body = {'action': 'scope-analyze', 'run_id': self.run, 'max_hops': 1}

    def observe(self, identity, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = self.success('/api/jobs/' + identity)
            if predicate(job):
                return job
            time.sleep(.02)
        self.fail('Scope job did not reach expected state: ' + json.dumps(job))

    def test_slow_worker_does_not_block_listing_cancellation_or_conflict_guard(self):
        worker = self.base / 'gated-scope-worker.py'
        worker.write_text('''import sys,time
from pathlib import Path
from liquid_tracer import scope_analysis, web_worker
original = scope_analysis.analyze_scope
gate = Path(sys.argv[3])
def paused(*args, **kwargs):
    gate.with_suffix('.entered').touch()
    deadline = time.monotonic() + 20
    while not gate.exists():
        if time.monotonic() > deadline:
            raise RuntimeError('Test gate was not released')
        time.sleep(.02)
    return original(*args, **kwargs)
scope_analysis.analyze_scope = paused
raise SystemExit(web_worker.main(sys.argv[1:3]))
''')
        gate = self.base / 'release'

        def command(request, result, live):
            self.assertFalse(live)
            return [sys.executable, str(worker), str(request), str(result), str(gate)]

        with patch('liquid_tracer.web.worker_command', side_effect=command), \
                patch('liquid_tracer.scope_analysis.analyze_scope',
                      side_effect=AssertionError('Admission must not analyze or verify')):
            job = self.success(self.route + '/actions', self.body, 202)
            self.addCleanup(gate.touch)
            self.assertTrue(job['cancellable'])
            self.assertFalse(job['live'])
            running = self.observe(job['id'], lambda j: gate.with_suffix('.entered').exists())
            self.assertEqual(running['status'], 'running')
            self.assertIn(job['id'], {j['id'] for j in self.success('/api/jobs')['jobs']})
            self.assertEqual(self.success(self.route + '/analyses'), {'analyses': []})
            with self.server.job_lock, self.assertRaises(RequestError):
                self.server.ensure_case_idle(self.metadata['id'])
            self.assertTrue(conflicts(running, {'case_id': self.metadata['id'],
                                               'resource_kind': 'exclusive'}))
            immutable = {'case_id': self.metadata['id'], **job_resources(
                ['scope-analyze', '--run', self.run], 'scope-analyze', self.case)}
            self.assertFalse(conflicts(running, immutable))
            self.success('/api/jobs/' + job['id'] + '/cancel', {}, 202)
            self.observe(job['id'], lambda j: j['status'] == 'canceled')
            self.assertFalse((self.case / 'analyses').exists())

    def test_rejects_invalid_scope_inputs_before_worker(self):
        for body in [dict(self.body, max_hops=True), dict(self.body, max_hops=-1),
                     dict(self.body, max_hops='10'), dict(self.body, run_id='../outside'),
                     dict(self.body, dataset_id='a' * 32), dict(self.body, token='forbidden'),
                     dict(self.body, data_source='network')]:
            with self.subTest(body=body), patch.object(self.server, 'start_job') as start:
                self.assertEqual(self.request(self.route + '/actions', body)[0], 400)
                start.assert_not_called()

    def test_saved_scope_survives_job_history_without_elk_and_has_verified_downloads(self):
        from liquid_tracer.scope_analysis import analyze_scope
        with patch('liquid_tracer.elk_layout.optimize_graph', side_effect=AssertionError('No ELK')), \
                patch('liquid_tracer.export.build_graph', side_effect=AssertionError('No graph')):
            saved = analyze_scope(self.case, self.run, max_hops=1)
        with patch('liquid_tracer.scope_analysis.analyze_scope', side_effect=AssertionError('No reanalysis')):
            listing = self.success(self.route + '/analyses')['analyses']
            self.assertEqual(len(listing), 1)
            self.assertNotIn('frontier', listing[0])
            detail = self.success(self.route + '/analyses/' + saved['analysis_id'])
            self.assertNotIn(str(self.case), json.dumps(detail))
            self.assertEqual(detail['max_hops'], 1)
            self.assertIn('frontier', detail)
            download = next(item['url'] for item in detail['downloads'] if item['name'] == 'frontiers.csv')
            self.assertEqual(self.request(download)[0], 200)
            self.assertNotEqual(self.request(download.rsplit('/', 1)[0] + '/case.json')[0], 200)
        path = self.case / 'analyses' / saved['analysis_id'] / 'frontiers.csv'
        path.write_bytes(path.read_bytes() + b'tampered')
        self.assertEqual(self.request(download)[0], 400)

    def test_cli_dispatches_without_credentials_and_passes_basis_to_plots(self):
        command = ['scope-analyze', '--case', str(self.case), '--run', self.run, '--max-hops', '10']
        self.assertEqual(parser().parse_args(command).max_hops, 10)
        with patch('liquid_tracer.scope_analysis.analyze_scope', return_value={'ok': True}) as analyze, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(command), 0)
        self.assertEqual(analyze.call_args.kwargs['max_hops'], 10)
        with patch('liquid_tracer.plots.preview_plot', return_value={}) as preview, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['plot', '--case', str(self.case), '--run', self.run,
                                  '--goal', 'full', '--max-hops', '10', '--hop-basis', 'original_seeds']), 0)
        self.assertEqual(preview.call_args.kwargs['hop_basis'], 'original_seeds')


if __name__ == '__main__':
    unittest.main()
