"""Cross-process render grants protect live children and fairly share capacity."""

import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.elk_errors import ElkWorkerFailure
from liquid_tracer.elk_parallel import iter_attempts
from liquid_tracer.elk_layout import _worker
from liquid_tracer.shared_render_resources import SharedRenderResources


class SharedRenderResourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.now = 10.
        self.capacity = (12000, 8)

    def search(self, **kwargs):
        search = SharedRenderResources(directory=self.directory, capacity=lambda: self.capacity,
                                       clock=lambda: self.now, **kwargs)
        self.addCleanup(search.close)
        return search

    def state(self):
        return json.loads((self.directory / "state.json").read_text())

    def test_newcomer_waits_then_searches_share_memory_and_cpu(self):
        first, second = self.search(), self.search()
        pilot = first.acquire(1, 12000)
        self.assertEqual((pilot.worker_count, pilot.heap_mb), (1, 12000))
        self.assertIsNone(second._try_acquire(8, 12000, 512)[0])
        first.release()
        other = second.acquire(8, 12000, peak_rss_mb=512)
        self.assertEqual(other.total_heap_mb, 6000)
        resumed = first.acquire(8, 12000, peak_rss_mb=512)
        self.assertEqual(resumed.total_heap_mb, 6000)
        self.assertLessEqual(other.worker_count + resumed.worker_count, 8)
        self.assertEqual(sum(job["heap"] for job in self.state()["jobs"].values()), 12000)
        first.release()
        second.release()

    def test_waiting_search_has_priority_over_previous_batch(self):
        first, second = self.search(), self.search()
        first.acquire(1, 12000)
        second._try_acquire(1, 12000, None)
        first.release()
        self.assertIsNone(first._try_acquire(8, 12000, 512)[0])
        second.acquire(1, 12000)
        self.assertIsNotNone(first._try_acquire(8, 12000, 512)[0])

    def test_explicit_caps_lend_unused_capacity_without_multiplication(self):
        first, second = self.search(), self.search()
        limited = first.acquire(2, 2000, peak_rss_mb=100)
        other = second.acquire(8, 12000, peak_rss_mb=100)
        self.assertEqual((limited.worker_count, limited.total_heap_mb), (1, 2000))
        self.assertEqual(other.total_heap_mb, 9996)
        self.assertLessEqual(other.worker_count + limited.worker_count, 8)
        self.assertLessEqual(other.total_heap_mb + limited.total_heap_mb, 12000)

    def test_idle_scoring_grace_expires_without_reserving_memory(self):
        first, second = self.search(), self.search()
        first.acquire(8, 12000, peak_rss_mb=512)
        first.release()
        during = second.acquire(8, 12000, peak_rss_mb=512)
        self.assertEqual(during.total_heap_mb, 6000)
        second.release()
        self.now += 6
        alone = second.acquire(8, 12000, peak_rss_mb=512)
        self.assertEqual(alone.total_heap_mb, 12000)
        self.assertEqual(alone.worker_count, 8)

    def test_changed_available_memory_does_not_increase_running_grants(self):
        first, second = self.search(), self.search()
        lease = first.acquire(1, 12000)
        self.capacity = (20000, 8)
        self.assertIsNone(second._try_acquire(1, 12000, None)[0])
        self.assertEqual(self.state()["pool_heap"], 12000)
        self.assertEqual(lease.heap_mb, 12000)
        first.release()
        second.acquire(1, 12000)
        self.assertEqual(self.state()["pool_heap"], 20000)

    def test_exclusive_memory_retry_waits_for_all_batches_without_deadlock(self):
        first, second = self.search(), self.search()
        first.acquire(1, 6000)
        second.acquire(1, 6000)
        first.release()
        self.assertIsNone(first._try_acquire(1, 12000, None, exclusive=True)[0])
        second.release()
        self.assertIsNone(second._try_acquire(1, 12000, None, exclusive=True)[0])
        retry = first.acquire(1, 12000, exclusive=True)
        self.assertEqual(retry.total_heap_mb, 12000)
        first.release()
        retry = second.acquire(1, 12000, exclusive=True)
        self.assertEqual(retry.total_heap_mb, 12000)

    def test_cancellation_while_waiting_removes_queued_request(self):
        first = self.search()
        first.acquire(1, 12000)
        events = []

        def cancel(_):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            with self.search(sleep=cancel) as second:
                second.acquire(1, 12000, progress=events.append)
        self.assertEqual(len(self.state()["jobs"]), 1)
        self.assertEqual(events[0]["stage"], "resource_wait")
        self.assertNotIn("heap_mb", events[0])

    def test_corrupt_coordination_fails_closed(self):
        (self.directory / "state.json").write_text("not valid JSON")
        with self.assertRaisesRegex(TraceError, "shared ELK resource"):
            self.search()

    def test_two_searches_overlap_and_keep_all_seed_results_in_order(self):
        second_waiting, second_pilot, first_parallel = (threading.Event() for _ in range(3))
        lock = threading.Lock()
        live = {}
        peak_jobs = peak_heap = 0

        def run(name):
            def report(event):
                if name == "second" and event.get("stage") == "resource_wait":
                    second_waiting.set()

            def worker(request, seeds, *, heap_mb, progress, **kwargs):
                nonlocal peak_jobs, peak_heap
                key = (name, seeds[0])
                with lock:
                    live[key] = heap_mb
                    peak_jobs = max(peak_jobs, len({key[0] for key in live}))
                    peak_heap = max(peak_heap, sum(live.values()))
                try:
                    if key == ("first", 1):
                        self.assertTrue(second_waiting.wait(5))
                    elif key == ("second", 1):
                        second_pilot.set()
                        self.assertTrue(first_parallel.wait(5))
                    elif key == ("first", 7):
                        first_parallel.set()
                        self.assertTrue(second_pilot.wait(5))
                    progress({"stage": "memory_measured", "peak_rss_mb": 512})
                    return [{"seed": seeds[0]}]
                finally:
                    with lock:
                        del live[key]

            return list(iter_attempts({"children": [{}]}, [1, 7, 19, 42], worker,
                                      lambda index, seed: report, {}))

        factory = lambda: SharedRenderResources(directory=self.directory, capacity=lambda: (12000, 8))
        budget = lambda attempts, peak_rss_mb=None: (min(3, attempts) if peak_rss_mb else 1, 12000, 12000)
        with patch("liquid_tracer.elk_parallel.SharedRenderResources", side_effect=factory), \
                patch("liquid_tracer.elk_parallel.elk_worker_budget", side_effect=budget), \
                ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(run, "first")
            # Wait for the first lease to be active so this also exercises a
            # newcomer arriving after the original full-budget grant.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with lock:
                    if live:
                        break
                time.sleep(.01)
            second = executor.submit(run, "second")
            results = [first.result(10), second.result(10)]
        self.assertEqual(peak_jobs, 2)
        self.assertLessEqual(peak_heap, 12000)
        for result in results:
            self.assertEqual([seed for _, seed, _ in result], [1, 7, 19, 42])

    def test_reduced_pilot_memory_failure_retries_after_other_search_drains(self):
        other = self.search()
        other.acquire(1, 6000)
        heaps, events, metadata = [], [], {}

        def worker(request, seeds, *, heap_mb, progress, **kwargs):
            heaps.append(heap_mb)
            if heap_mb == 6000:
                raise ElkWorkerFailure("Synthetic small-share failure", failure_code="heap_exhausted")
            return [{"seed": seeds[0]}]

        def report(event):
            events.append(event)
            if event.get("stage") == "resource_wait":
                other.release()

        factory = lambda: SharedRenderResources(directory=self.directory, capacity=lambda: self.capacity,
                                                clock=lambda: self.now)
        with patch("liquid_tracer.elk_parallel.SharedRenderResources", side_effect=factory), \
                patch("liquid_tracer.elk_parallel.elk_worker_budget", return_value=(1, 12000, 12000)), \
                patch("liquid_tracer.elk_parallel.renderer_heap_mb", return_value=12000):
            result = list(iter_attempts({"children": [{}]}, [1], worker, lambda index, seed: report, metadata))
        self.assertEqual(heaps, [6000, 12000])
        self.assertEqual(metadata["memory_retry_count"], 1)
        self.assertEqual(result, [(1, 1, [{"seed": 1}])])
        self.assertTrue(any(event.get("stage") == "resource_wait" for event in events))

    def test_serial_retry_refreshes_auto_allowance_after_wait_but_keeps_explicit_cap(self):
        for setting, expected in (("auto", 12000), ("6000", 6000)):
            with self.subTest(setting=setting):
                self.capacity = (12000, 8)
                with self.search() as other:
                    other.acquire(1, 6000)
                    self.capacity = (6000, 8)
                    heaps, metadata = [], {}

                    def worker(request, seeds, *, heap_mb, **kwargs):
                        heaps.append(heap_mb)
                        if len(heaps) == 1:
                            raise ElkWorkerFailure("Synthetic pressure", failure_code="heap_exhausted")
                        return [{"seed": seeds[0]}]

                    def report(event):
                        if event.get("stage") == "resource_wait":
                            other.release()
                            self.capacity = (12000, 8)

                    factory = lambda: SharedRenderResources(directory=self.directory, capacity=lambda: self.capacity,
                                                            clock=lambda: self.now)
                    heap = lambda: self.capacity[0] if setting == "auto" else 6000
                    with patch.dict(os.environ, {"LIQUID_RENDER_HEAP_MB": setting}), \
                            patch("liquid_tracer.elk_parallel.SharedRenderResources", side_effect=factory), \
                            patch("liquid_tracer.elk_parallel.elk_worker_budget", return_value=(1, 6000, 6000)), \
                            patch("liquid_tracer.elk_parallel.renderer_heap_mb", side_effect=heap):
                        result = list(iter_attempts({"children": [{}]}, [1], worker, lambda index, seed: report, metadata))
                    self.assertEqual(heaps, [6000, expected])
                    self.assertEqual(metadata["memory_retry_count"], 1)
                    self.assertEqual(len(result), 1)

    def test_reservation_survives_killed_owner_until_inheriting_child_exits(self):
        # Node inherits this same descriptor in production. A small Python
        # child exercises identical kernel flock/open-file-description rules.
        script = r'''
import json, os, subprocess, sys
from liquid_tracer.shared_render_resources import SharedRenderResources
directory = sys.argv[1]
search = SharedRenderResources(directory=directory, capacity=lambda: (12000, 8))
search.acquire(1, 12000)
child_code = "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]);\nwhile not p.exists(): time.sleep(.02)"
child = subprocess.Popen([sys.executable, '-c', child_code, directory + '/finish'],
                         pass_fds=(search.lease_fd,), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(json.dumps({'child': child.pid, 'job': search.identity}), flush=True)
os._exit(0)
'''
        process = subprocess.run([sys.executable, "-c", script, str(self.directory)],
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        report = json.loads(process.stdout)
        self.addCleanup(lambda: self._stop_child(report["child"]))
        newcomer = self.search()
        self.assertIn(report["job"], self.state()["jobs"])
        self.assertIsNone(newcomer._try_acquire(1, 12000, None)[0])
        (self.directory / "finish").touch()
        deadline = time.monotonic() + 5
        lease = None
        while time.monotonic() < deadline:
            lease = newcomer._try_acquire(1, 12000, None)[0]
            if lease:
                break
            time.sleep(.02)
        self.assertIsNotNone(lease)
        self.assertEqual(lease.total_heap_mb, 12000)
        self.assertNotIn(report["job"], self.state()["jobs"])

    @unittest.skipUnless(shutil.which("node"), "Node is required for worker inheritance")
    def test_production_worker_passes_lease_descriptor_to_node(self):
        search = self.search()
        lease = search.acquire(1, 128)
        project = self.directory / "project"
        package = project / "layout" / "node_modules" / "elkjs" / "package.json"
        package.parent.mkdir(parents=True)
        package.write_text("{}")
        info = os.fstat(lease.resource_lease_fd)
        (project / "layout" / "run.mjs").write_text(
            "import fs from 'node:fs';\n"
            f"const info = fs.fstatSync({lease.resource_lease_fd});\n"
            f"if (info.ino !== {info.st_ino} || info.dev !== {info.st_dev}) throw Error('Missing lease');\n"
            "process.stdout.write(JSON.stringify({version:'0.12.0',candidates:[{leaseInherited:true}]}));\n")
        with patch.dict(os.environ, {"LIQUID_TRACER_ROOT": str(project), "LIQUID_NODE_BIN": shutil.which("node")}):
            result = _worker({}, [1], heap_mb=128, resource_lease_fd=lease.resource_lease_fd)
        self.assertEqual(result, [{"leaseInherited": True}])
        search.release()

    @staticmethod
    def _stop_child(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


if __name__ == "__main__":
    unittest.main()
