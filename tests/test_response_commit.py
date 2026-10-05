import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from liquid_tracer.api import ENTERPRISE, TOKEN_URL, Esplora, Limits
from liquid_tracer.common import StopRun, TraceError, digest
from liquid_tracer.store import Store


class ResponseCommitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.case = Path(self.temp.name) / 'case'
        self.store = Store(self.case)
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.store.close()
        self.temp.cleanup()

    def client(self, transport):
        client = Esplora(self.store, 'run', Limits(), auth='none',
                         advertised_rps=1_000_000, transport=transport)
        self.clients.append(client)
        return client

    def test_response_commits_outcome_and_body_once_and_is_visible_to_read_only_reader(self):
        statements = []
        self.store.db.set_trace_callback(statements.append)
        raw = b'{"chain_stats":{"tx_count":7}}'
        client = self.client(Mock(return_value=(200, {}, raw)))
        data, oid = client.get('/address/one')
        self.assertEqual(data['chain_stats']['tx_count'], 7)
        self.assertEqual(sum(statement == 'COMMIT' for statement in statements), 2)
        with sqlite3.connect((self.case / 'evidence.sqlite').as_uri() + '?mode=ro', uri=True) as reader:
            self.assertEqual(reader.execute('SELECT status FROM attempts ORDER BY id').fetchall(),
                             [('started',), ('200',)])
            saved = reader.execute('SELECT id, status, sha256, body FROM observations').fetchone()
        self.assertEqual(saved, (oid, 200, digest(raw), raw))

    def test_observation_insert_failure_rolls_back_outcome_without_losing_started_attempt(self):
        self.store.attempt('run', 'esplora', '/address/one', 'started')
        self.store.db.executescript("""CREATE TRIGGER reject_observation BEFORE INSERT ON observations
            BEGIN SELECT RAISE(FAIL, 'synthetic write failure'); END;""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'synthetic write failure'):
            self.store.record_response('run', 'esplora', ENTERPRISE, '/address/one', b'{}', 200)
        with sqlite3.connect(self.case / 'evidence.sqlite') as reader:
            self.assertEqual(reader.execute('SELECT status FROM attempts').fetchall(), [('started',)])
            self.assertEqual(reader.execute('SELECT COUNT(*) FROM observations').fetchone(), (0,))

    def test_network_started_attempt_is_durable_before_transport_and_response_failure_is_retained(self):
        client = None

        def transport(*_):
            with sqlite3.connect(self.case / 'evidence.sqlite') as reader:
                self.assertEqual(reader.execute('SELECT status FROM attempts').fetchall(), [('started',)])
            client._cancelled.set()
            return 429, {}, b'{"rate_limit":true}'

        client = self.client(transport)
        with self.assertRaisesRegex(StopRun, 'interrupted'):
            client.get('/address/one')
        rows = list(self.store.observations(client.used))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 429)
        self.assertEqual(rows[0]['body'], b'{"rate_limit":true}')
        self.assertEqual([row[0] for row in self.store.db.execute('SELECT status FROM attempts')],
                         ['started', '429'])

    def test_direct_call_keeps_three_values_and_cannot_archive_oauth_body(self):
        transport = Mock(return_value=(200, {}, b'{"access_token":"private"}'))
        client = self.client(transport)
        self.assertEqual(len(client.call('POST', TOKEN_URL, 'oauth', '/token')), 3)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 0)
        with self.assertRaisesRegex(TraceError, 'Only explorer GET'):
            client.call('POST', TOKEN_URL, 'oauth', '/token', archive_response=True)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(client.budget.requests, 1)
        archive = b''.join(path.read_bytes() for path in self.case.glob('evidence.sqlite*') if path.is_file())
        self.assertNotIn(b'private', archive)


if __name__ == '__main__':
    unittest.main()
