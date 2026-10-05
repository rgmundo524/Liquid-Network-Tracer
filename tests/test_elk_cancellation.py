"""Layout cancellation reaches queued leases, nested schedulers and live workers."""

import json
import os
import signal
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from liquid_tracer.elk_layout import _report_progress, _worker, optimize_graph
from liquid_tracer.elk_parallel import _emit, iter_attempts
from liquid_tracer.elk_sections_parallel import iter_sections
from liquid_tracer.shared_render_resources import SharedRenderResources
from tests.test_elk_layout import crossing_graph, synthetic_candidate


class ElkCancellationTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.factory = lambda: SharedRenderResources(directory=self.directory, capacity=lambda: (16384, 8))
        self.enterContext(patch('liquid_tracer.elk_parallel.SharedRenderResources', side_effect=self.factory))
        self.enterContext(patch('liquid_tracer.elk_sections_parallel.SharedRenderResources', side_effect=self.factory))
        self.enterContext(patch('liquid_tracer.elk_parallel.elk_worker_budget', return_value=(1, 2048, 2048)))
        self.enterContext(patch('liquid_tracer.elk_sections_parallel.elk_worker_budget', return_value=(2, 4096, 2048)))

    def state(self):
        return json.loads((Path(self.directory) / 'state.json').read_text())['jobs']

    def test_pre_cancelled_graph_never_starts_preparation_or_worker(self):
        cancel = threading.Event()
        cancel.set()
        with patch('liquid_tracer.elk_layout._validate_graph') as validate, \
                patch('liquid_tracer.elk_layout._worker') as worker, self.assertRaises(CancelledError):
            optimize_graph(crossing_graph(), cancel_event=cancel)
        validate.assert_not_called()
        worker.assert_not_called()

    def test_report_cancellation_reaches_single_attempt_and_releases_resources(self):
        cancel, started, stopped = threading.Event(), threading.Event(), threading.Event()
        def worker(request, seeds, *, cancel_event, **kwargs):
            self.assertIs(cancel_event, cancel)
            started.set()
            self.assertTrue(cancel_event.wait(5))
            stopped.set()
            raise CancelledError()
        with patch('liquid_tracer.elk_layout._worker', side_effect=worker), ThreadPoolExecutor(1) as executor:
            future = executor.submit(optimize_graph, crossing_graph(), layout_attempts=1,
                                     cancel_event=cancel)
            self.assertTrue(started.wait(5))
            cancel.set()
            with self.assertRaises(CancelledError):
                future.result(timeout=5)
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.state(), {})

    def test_report_cancellation_reaches_trace_section_layout(self):
        from tests.test_trace_layout import fixture
        cancel, started, stopped = threading.Event(), threading.Event(), threading.Event()
        graph, _, _ = fixture()
        def worker(request, seeds, *, cancel_event, **kwargs):
            started.set()
            self.assertTrue(cancel_event.wait(5))
            stopped.set()
            raise CancelledError()
        with patch('liquid_tracer.trace_sections.MIN_SECTION_NODES', 1), \
                patch('liquid_tracer.elk_layout._worker', side_effect=worker), ThreadPoolExecutor(1) as executor:
            future = executor.submit(optimize_graph, graph, layout_attempts=1,
                                     cancel_event=cancel)
            self.assertTrue(started.wait(5))
            cancel.set()
            with self.assertRaises(CancelledError):
                future.result(timeout=5)
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.state(), {})

    def test_parent_cancellation_drains_all_nested_section_workers(self):
        cancel = threading.Event()
        barrier = threading.Barrier(3)
        stopped = []
        def worker(request, seeds, *, cancel_event, progress, **kwargs):
            if request.get('id') == '0':
                progress({'stage': 'memory_measured', 'peak_rss_mb': 512})
                return [{'seed': seeds[0]}]
            barrier.wait(timeout=5)
            self.assertTrue(cancel_event.wait(5))
            stopped.append(request['id'])
            raise CancelledError()
        inputs = [{'id': str(index), 'children': [{}], 'edges': []} for index in range(4)]
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(lambda: list(iter_sections(inputs, 1, worker, None, {}, cancel_event=cancel)))
            barrier.wait(timeout=5)
            cancel.set()
            with self.assertRaises(CancelledError):
                future.result(timeout=5)
        self.assertCountEqual(stopped, ['1', '2'])
        self.assertEqual(self.state(), {})

    def test_parent_cancellation_drains_parallel_attempts(self):
        cancel = threading.Event()
        barrier = threading.Barrier(3)
        stopped = []
        def worker(request, seeds, *, cancel_event, progress, **kwargs):
            if seeds[0] == 1:
                progress({'stage': 'memory_measured', 'peak_rss_mb': 512})
                return [{'seed': 1}]
            barrier.wait(timeout=5)
            self.assertTrue(cancel_event.wait(5))
            stopped.append(seeds[0])
            raise CancelledError()
        with patch('liquid_tracer.elk_parallel.elk_worker_budget', return_value=(2, 4096, 2048)), \
                ThreadPoolExecutor(1) as executor:
            future = executor.submit(lambda: list(iter_attempts({'children': [{}], 'edges': []}, [1, 7, 19, 42],
                                     worker, lambda *_: lambda _: None, {}, cancel_event=cancel)))
            barrier.wait(timeout=5)
            cancel.set()
            with self.assertRaises(CancelledError):
                future.result(timeout=5)
        self.assertCountEqual(stopped, [7, 19])
        self.assertEqual(self.state(), {})

    def test_nested_cleanup_does_not_cancel_successful_parent_report(self):
        cancel = threading.Event()
        worker = lambda request, seeds, **kwargs: [{'seed': seeds[0]}]
        list(iter_sections([{'children': [{}]}, {'children': [{}]}], 1, worker, None, {}, cancel_event=cancel))
        self.assertFalse(cancel.is_set())
        self.assertEqual(self.state(), {})

    def test_waiting_report_cancels_without_waiting_for_other_lease(self):
        cancel, waiting = threading.Event(), threading.Event()
        with self.factory() as holder, holder.acquire(8, 16384):
            worker = Mock()
            def progress(event):
                if event['stage'] == 'resource_wait':
                    waiting.set()
            with ThreadPoolExecutor(1) as executor:
                future = executor.submit(lambda: list(iter_attempts({'children': [{}], 'edges': []}, [1],
                                         worker, lambda *_: progress, {}, cancel_event=cancel)))
                self.assertTrue(waiting.wait(5))
                cancel.set()
                with self.assertRaises(CancelledError):
                    future.result(timeout=5)
            worker.assert_not_called()
            self.assertEqual(list(self.state()), [holder.identity])

    def test_cancellation_during_grant_releases_unclaimed_lease(self):
        for raises in (False, True):
            with self.subTest(raises=raises), self.factory() as resources:
                cancel = threading.Event()
                def progress(event):
                    cancel.set()
                    if raises:
                        raise CancelledError()
                with self.assertRaises(CancelledError):
                    resources.acquire(1, 2048, progress=progress, cancel_event=cancel)
                self.assertEqual(self.state()[resources.identity]['mode'], 'idle')
                self.assertEqual(self.state()[resources.identity]['heap'], 0)

    def test_progress_cancellation_is_not_suppressed(self):
        def progress(event):
            raise CancelledError()
        with self.assertRaises(CancelledError):
            _report_progress(progress, 'testing')
        with self.assertRaises(CancelledError):
            _emit(progress, {})

    def test_cancellation_during_scoring_closes_search_and_returns_no_layout(self):
        cancel = threading.Event()
        from liquid_tracer.elk_layout import layout_metrics
        calls = []
        def measured(graph, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                cancel.set()
            return layout_metrics(graph, **kwargs)
        with patch('liquid_tracer.elk_layout._worker', side_effect=synthetic_candidate), \
                patch('liquid_tracer.elk_layout.layout_metrics', side_effect=measured), \
                self.assertRaises(CancelledError):
            optimize_graph(crossing_graph(), layout_attempts=1, cancel_event=cancel)
        self.assertEqual(self.state(), {})

    def test_live_worker_cancellation_kills_and_reaps_owned_process_group(self):
        cancel = threading.Event()
        process = Mock(returncode=None, pid=12345)
        def communicate(*args, **kwargs):
            if process.communicate.call_count == 1:
                cancel.set()
                raise subprocess.TimeoutExpired('node', .2)
            return '', ''
        process.communicate.side_effect = communicate
        process.poll.return_value = None
        with patch.dict(os.environ, {'LIQUID_NODE_BIN': '/synthetic/node'}), \
                patch('pathlib.Path.is_file', return_value=True), \
                patch('liquid_tracer.elk_layout.subprocess.Popen', return_value=process), \
                patch('liquid_tracer.elk_layout.os.killpg') as kill, self.assertRaises(CancelledError):
            _worker({'children': [{}], 'edges': []}, [1], heap_mb=2048, cancel_event=cancel)
        kill.assert_called_once_with(12345, signal.SIGKILL)
        self.assertEqual(process.communicate.call_count, 2)
        self.assertEqual(process.communicate.call_args_list[0].kwargs['timeout'], .2)


if __name__ == '__main__':
    unittest.main()
