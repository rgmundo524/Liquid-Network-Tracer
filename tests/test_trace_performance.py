"""Trace collection overlaps validated dependencies without widening scope."""

import os
import tempfile
import threading
import unittest
from collections import Counter
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.api import ENTERPRISE, Esplora, Limits
from liquid_tracer.common import TraceError, canonical, read_json
from liquid_tracer.store import Store
from liquid_tracer.trace import new_state, trace
from liquid_tracer.trace_fetch import TraceConcurrency
from tests.fixtures import CONFIRMED, output
from tests.test_trace_concurrency import RecordingTransport, converging_fixture, evidence_topology


class TracePerformanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.counter = 0
        for target, options in (
                ('os.environ', {'values': {'LIQUID_TRACE_WORKERS': 'auto'}}),):
            patcher = patch.dict(target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, result in (('_available_cpu_count', 16), ('_available_bytes', 32 * 1024 ** 3)):
            patcher = patch('liquid_tracer.count_concurrency.' + target, return_value=result)
            patcher.start()
            self.addCleanup(patcher.stop)

    def collect(self, data, seeds, *, workers=8, adaptive=True, transport=None, limits=None, labels=()):
        self.counter += 1
        directory = self.root / str(self.counter)
        limits = limits or Limits(max_hops=2, max_transactions=1000, max_requests=2000)
        transport = transport or RecordingTransport(data, delay=.015)
        with closing(Store(directory)) as store:
            with Esplora(store, 'pending', limits, auth='none', transport=transport,
                         workers=workers, adaptive_workers=adaptive, advertised_rps=1_000_000) as api:
                state = new_state(seeds, api.base, limits, list(labels))
                api.run_id = state['run_id']
                result = trace(api, state, limits, directory / 'trace.json')
                self.assertFalse(any(not future.done() for future in api._results.values()))
        return result, transport

    def test_adaptive_transaction_requests_grow_beyond_eight_without_changing_evidence(self):
        data, roots, _, _, unrelated = converging_fixture(32)
        seeds = [txid + ':0' for txid in roots]
        serial, serial_transport = self.collect(data, seeds, workers=1, adaptive=False,
                                                transport=RecordingTransport(data))
        concurrent, transport = self.collect(data, seeds)
        self.assertEqual(evidence_topology(concurrent), evidence_topology(serial))
        self.assertEqual(Counter(transport.calls), Counter(serial_transport.calls))
        self.assertGreater(transport.maximum_active, 8)
        self.assertLessEqual(transport.maximum_active, 64)
        self.assertGreater(concurrent['performance']['worker_peak'], 8)
        self.assertNotIn('/tx/' + unrelated, transport.calls)
        self.assertEqual(concurrent['performance']['checkpoint_count'], 2)
        self.assertGreater(concurrent['performance']['fetch_wait_seconds'], 0)
        self.assertGreater(concurrent['performance']['checkpoint_seconds'], 0)
        self.assertGreaterEqual(concurrent['performance']['processing_seconds'], 0)

    def test_ready_outspend_starts_before_slow_funding_finishes(self):
        data, roots, _, _, _ = converging_fixture(2)
        fast, slow = roots
        outspend_started = threading.Event()
        observations = []
        lock = threading.Lock()

        def transport(method, url, headers, body, timeout):
            endpoint = url.removeprefix(ENTERPRISE)
            with lock:
                observations.append(endpoint)
            if endpoint == '/tx/' + slow:
                if not outspend_started.wait(3):
                    raise AssertionError('Fast dependency was blocked by the slow funding request')
            if endpoint == '/tx/' + fast + '/outspends':
                outspend_started.set()
            return 200, {}, canonical(data[endpoint])

        result, _ = self.collect(data, [txid + ':0' for txid in roots], workers=2,
                                 adaptive=False, transport=transport)
        self.assertEqual(result['status'], 'bounded_complete')
        self.assertTrue(outspend_started.is_set())
        self.assertEqual(len(observations), len(set(observations)))

    def test_many_outputs_of_one_funding_transaction_fill_distinct_child_request_slots(self):
        root = '01' * 32
        children = [f'{index + 10:064x}' for index in range(40)]
        outputs = [output('selected-' + str(index)) for index in range(40)]
        data = {'/tx/' + root: {'txid': root, 'status': dict(CONFIRMED),
                                'vin': [], 'vout': outputs + [output('unselected')]},
                '/tx/' + root + '/outspends': [
                    {'spent': True, 'txid': child, 'vin': 0, 'status': dict(CONFIRMED)}
                    for child in children] + [{'spent': True, 'txid': 'ff' * 32, 'vin': 0,
                                               'status': dict(CONFIRMED)}]}
        for index, child in enumerate(children):
            data['/tx/' + child] = {'txid': child, 'status': dict(CONFIRMED),
                                    'vin': [{'txid': root, 'vout': index, 'prevout': outputs[index]}],
                                    'vout': [output('child-' + str(index))]}
        result, transport = self.collect(data, [root + ':' + str(index) for index in range(40)],
                                         limits=Limits(max_hops=1, max_requests=200, max_transactions=100))
        self.assertEqual(result['status'], 'bounded_complete')
        self.assertGreater(transport.maximum_active, 8)
        self.assertEqual(Counter(transport.calls)['/tx/' + root + '/outspends'], 1)
        self.assertEqual(set(result['transactions']), {root, *children})
        self.assertNotIn(root + ':40', result['outputs'])
        self.assertNotIn('/tx/' + 'ff' * 32, transport.calls)

    def test_adaptive_tight_budgets_retain_serial_frontier_and_request_scope(self):
        data, roots, _, _, _ = converging_fixture(24)
        seeds = [txid + ':0' for txid in roots]
        for options in ({'max_requests': 2}, {'max_requests': 9}, {'max_requests': 20},
                        {'max_transactions': 10}, {'max_outpoints': 3}):
            with self.subTest(options=options):
                limits = Limits(max_hops=2, **options)
                serial, one = self.collect(data, seeds, workers=1, adaptive=False, limits=limits,
                                           transport=RecordingTransport(data))
                result, many = self.collect(data, seeds, limits=limits, transport=RecordingTransport(data))
                self.assertEqual(evidence_topology(result), evidence_topology(serial))
                self.assertEqual(Counter(many.calls), Counter(one.calls))
                self.assertLessEqual(result['performance']['request_count'], limits.max_requests)

    def test_auto_preserves_explicit_lower_worker_cap_and_other_policy_environment(self):
        with patch.dict(os.environ, {'LIQUID_COUNT_WORKERS': '64', 'LIQUID_TRACE_WORKERS': 'auto'}):
            policy = TraceConcurrency(3, 500, 49)
            self.assertEqual(policy.ceiling, 3)
            self.assertEqual(policy.target({'completed_requests': 80, 'latency_seconds': 10}), 3)
        with patch.dict(os.environ, {'LIQUID_TRACE_WORKERS': '32'}):
            self.assertEqual(TraceConcurrency(3, 500, 49).target({}), 32)
        with patch.dict(os.environ, {'LIQUID_TRACE_WORKERS': '65'}):
            with self.assertRaisesRegex(TraceError, 'LIQUID_TRACE_WORKERS'):
                TraceConcurrency(8, 500, 49)

    def test_perf_reset_does_not_inherit_a_previous_run(self):
        data, roots, _, _, _ = converging_fixture(2)
        state, _ = self.collect(data, [roots[0] + ':0'])
        state['performance'] = {'tracing_seconds': 99999, 'checkpoint_count': 99999}
        limits = Limits(max_hops=2)
        directory = self.root / 'continued'
        with closing(Store(directory)) as store:
            with Esplora(store, 'continued', limits, auth='none', transport=RecordingTransport(data)) as api:
                child = new_state(state['seeds'], api.base, limits, [], parent=state)
                result = trace(api, child, limits, directory / 'trace.json')
        self.assertLess(result['performance']['tracing_seconds'], 99999)
        self.assertLess(result['performance']['checkpoint_count'], 99999)
        self.assertEqual(state['performance']['checkpoint_count'], 99999)

    def test_interrupt_reported_after_drain_still_saves_a_terminal_checkpoint(self):
        data, roots, _, _, _ = converging_fixture(2)
        original = Esplora.drain_pending

        def interrupted_after_drain(api, cancel=False):
            original(api, cancel=cancel)
            raise KeyboardInterrupt()

        with patch.object(Esplora, 'drain_pending', interrupted_after_drain):
            result, _ = self.collect(data, [roots[0] + ':0'], workers=1, adaptive=False,
                                      transport=RecordingTransport(data))
        saved = read_json(self.root / str(self.counter) / 'trace.json')
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(result['stop_reason'], 'interrupted')
        self.assertEqual(evidence_topology(saved), evidence_topology(result))
        self.assertEqual(result['performance']['checkpoint_count'], 2)


if __name__ == '__main__':
    unittest.main()
