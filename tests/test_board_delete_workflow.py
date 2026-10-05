"""Board deletion uses the visible task and terminal-credential workflow."""
import contextlib
import io
import json
import sys
import time
import unittest
from unittest.mock import patch

from liquid_tracer.cli import main, parser
from liquid_tracer.investigation_boards import link_board
from liquid_tracer.workflow_api import public_board
from tests import test_web


class DeleteWorkflowTests(unittest.TestCase):
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    create = test_web.LocalWebTests.create
    wait = test_web.LocalWebTests.wait

    def setUp(self):
        test_web.LocalWebTests.setUp(self)
        _, self.metadata = self.create()
        self.case, _ = self.server.case(self.metadata['id'])
        self.board = link_board(self.case, 'pegouts', 'Synthetic paths', 'synthetic-board=')
        self.route = '/api/cases/' + self.metadata['id']
        self.body = {'action': 'board-delete', 'record_id': self.board['id'],
                     'board_id': self.board['board_id'], 'confirm_delete': True}

    def test_rejects_missing_or_malformed_confirmation_without_starting_worker(self):
        bodies = [dict(self.body, confirm_delete=value) for value in (False, 1, 'true', None)]
        bodies += [{key: value for key, value in self.body.items() if key != 'confirm_delete'},
                   dict(self.body, record_id='../case.json'), dict(self.body, board_id='../board'),
                   dict(self.body, token='never-accept-browser-tokens')]
        with patch.object(self.server, 'start_job') as start:
            for body in bodies:
                with self.subTest(body=body):
                    self.assertEqual(self.request(self.route + '/actions', body)[0], 400)
            start.assert_not_called()

    def test_cli_requires_explicit_confirmation_and_dispatches_pinned_target(self):
        command = ['investigation-board-delete', '--case', str(self.case),
                   '--record', self.board['id'], '--board', self.board['board_id']]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(command)
        with patch('liquid_tracer.board_deletion.delete_board', return_value={'deleted': True}) as delete, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(command + ['--confirm-delete']), 0)
        self.assertEqual(delete.call_args.args, (self.case, self.board['id'], self.board['board_id']))

    def test_visible_job_waits_for_worker_validation_and_blocks_conflicting_work(self):
        worker = self.base / 'mock-delete-worker.py'
        worker.write_text('''import sys,time
from pathlib import Path
from liquid_tracer import board_deletion, web_worker
original = board_deletion.delete_board
gate = Path(sys.argv[3])
def delete(*args, **kwargs):
    gate.with_suffix('.entered').touch()
    deadline = time.monotonic() + 15
    while not gate.exists():
        if time.monotonic() > deadline:
            raise RuntimeError('Test gate was not released')
        time.sleep(.02)
    return original(*args, **kwargs, token='synthetic-token',
                    transport=lambda *a: (204, {}, b''))
board_deletion.delete_board = delete
raise SystemExit(web_worker.main(sys.argv[1:3]))
''')
        gate = self.base / 'release'
        self.addCleanup(gate.touch)

        def command(request, result, live):
            self.assertTrue(live)
            # Keep the real credential readiness/ack protocol; only replace
            # the external provider command and network transport in this test.
            return [sys.executable, str(worker), str(request), str(result), str(gate)]

        with patch('liquid_tracer.web.worker_command', side_effect=command), \
                patch('liquid_tracer.investigation_boards.list_boards',
                      side_effect=AssertionError('Admission must not scan mappings')):
            job = self.success(self.route + '/actions', self.body, 202)
            self.assertEqual(job['resource_kind'], 'board_delete')
            self.assertEqual(job['resource_key'], self.board['board_id'])
            self.assertTrue(job['live'])
            self.assertFalse(job['cancellable'])
            deadline = time.monotonic() + 8
            while not gate.with_suffix('.entered').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(gate.with_suffix('.entered').exists(), self.success('/api/jobs/' + job['id']))
            jobs = self.success('/api/jobs')['jobs']
            self.assertEqual(next(item for item in jobs if item['id'] == job['id'])['status'], 'running')
            self.assertEqual(self.request(self.route + '/actions', self.body)[0], 409)
            self.assertEqual(self.request('/api/jobs/' + job['id'] + '/cancel', {})[0], 409)
            gate.touch()
            result = self.wait(job)
        self.assertEqual(result['status'], 'deleted')
        self.assertTrue(result['deleted'])
        self.assertNotIn('board_url', result)
        self.assertNotIn('synthetic-token', json.dumps(self.success('/api/jobs')))
        self.assertEqual(self.success(self.route + '/boards')['boards'], [])

    def test_deleted_result_has_no_active_board_link(self):
        result = public_board({'id': self.board['id'], 'board_id': self.board['board_id'],
                               'status': 'deleted', 'deleted': True, 'token': 'forbidden'})
        self.assertNotIn('board_url', result)
        self.assertNotIn('token', result)

    def test_other_case_overview_does_not_resurrect_a_deleted_link(self):
        from liquid_tracer.board_deletion import delete_board
        from liquid_tracer.investigations import create_investigation, read_case
        from liquid_tracer.investigation_views import overview
        other = create_investigation(self.server.root, 'Other case', board=self.board['board_id'])
        metadata = read_case(other)
        delete_board(self.case, self.board['id'], self.board['board_id'],
                     token='synthetic-token', transport=lambda *a: (204, {}, b''))
        self.assertIsNone(overview(other, metadata)['miro_board'])
        self.assertIsNone(self.server.case_summary(other, metadata)['miro_board'])
        self.assertEqual(read_case(other)['miro_board'], self.board['board_id'])

    def test_unreadable_deletion_state_does_not_hide_the_investigation(self):
        from liquid_tracer.board_write_guard import visible_board
        from liquid_tracer.common import TraceError
        with patch('liquid_tracer.board_write_guard.deletion_receipt', side_effect=TraceError('broken receipt')):
            self.assertIsNone(visible_board(self.case, self.board['board_id']))


if __name__ == '__main__':
    unittest.main()
