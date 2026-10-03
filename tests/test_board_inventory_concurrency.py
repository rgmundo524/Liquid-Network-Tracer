"""Independent board inventories overlap without changing publication safeguards."""

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from liquid_tracer.board_layout import read_live
from liquid_tracer.common import TraceError, canonical
from liquid_tracer.miro_quota import SharedMiroQuota, TARGET_CREDITS_PER_MINUTE
from liquid_tracer.miro_requests import MiroRequests


BASE = "https://api.miro.com/v2/boards/synthetic"
STATE = {"items": {}}


class PagedInventory:
    def __init__(self, overlap=False):
        self.barrier = threading.Barrier(2) if overlap else None
        self.lock = threading.Lock()
        self.calls = []
        self.active = self.peak = 0

    def __call__(self, method, url, headers, body, timeout):
        if method != "GET":
            raise AssertionError("Inventory must remain read-only")
        parsed = urlsplit(url)
        kind = parsed.path.rsplit("/", 1)[-1]
        cursor = parse_qs(parsed.query).get("cursor", [None])[0]
        if kind not in ("items", "connectors") or cursor not in (None, kind + "-next"):
            raise AssertionError("Unexpected inventory endpoint or cursor")
        with self.lock:
            previous = [call for call in self.calls if call[0] == kind]
            if bool(previous) != bool(cursor) or len(previous) > 1:
                raise AssertionError("Each cursor must follow its first page exactly once")
            self.calls.append((kind, cursor, time.monotonic()))
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.barrier is not None and cursor is None:
                self.barrier.wait(timeout=3)
            index = 2 if cursor else 1
            if kind == "items":
                item = {"id": "shape-" + str(index), "type": "shape",
                        "position": {"x": index * 100, "y": 20},
                        "geometry": {"width": 80, "height": 60},
                        "data": {"content": "Synthetic " + str(index)}}
            else:
                item = {"id": "connector-" + str(index),
                        "startItem": {"id": "shape-1"}, "endItem": {"id": "shape-2"}}
            page = {"data": [item], "total": 2}
            if cursor is None:
                page["cursor"] = kind + "-next"
            return 200, {}, canonical(page)
        finally:
            with self.lock:
                self.active -= 1


class BoardInventoryConcurrencyTests(unittest.TestCase):
    @staticmethod
    def capture(transport, workers=4, quota=None):
        with MiroRequests(transport, interval=0, workers=workers, quota=quota) as requests:
            return read_live(requests, BASE, {}, STATE, remote={})

    def test_chains_overlap_and_match_single_worker_snapshot(self):
        sequential = PagedInventory()
        expected = self.capture(sequential, workers=1)
        self.assertEqual(sequential.peak, 1)
        self.assertEqual([kind for kind, _, _ in sequential.calls],
                         ["items", "items", "connectors", "connectors"])

        for workers in (2, 4):
            with self.subTest(workers=workers):
                parallel = PagedInventory(overlap=True)
                result = self.capture(parallel, workers=workers)
                self.assertEqual(parallel.peak, 2)
                self.assertEqual(canonical(result), canonical(expected))
                self.assertEqual(len(parallel.calls), 4)
                self.assertEqual(result[0]["bounds"], [60., -10., 240., 50.])
                self.assertEqual(result[1]["connector-1"]["type"], "connector")

    def test_both_chains_keep_shared_credit_pacing(self):
        transport = PagedInventory()
        with tempfile.TemporaryDirectory() as directory:
            quota = SharedMiroQuota("synthetic-token", directory=directory)
            self.capture(transport, quota=quota)
        starts = sorted(timestamp for _, _, timestamp in transport.calls)
        self.assertEqual(len(starts), 4)
        # Both list endpoints cost 100 credits. Allow scheduling jitter without
        # accepting simultaneous starts that bypass the shared quota gate.
        minimum_gap = 60 * 100 / TARGET_CREDITS_PER_MINUTE * .8
        self.assertTrue(all(right - left >= minimum_gap
                            for left, right in zip(starts, starts[1:])), starts)

    def test_failure_drains_inflight_read_and_cancels_later_pages(self):
        together = threading.Barrier(2)
        release = threading.Event()
        connector_finished = threading.Event()
        requests_ready = threading.Event()
        instance = []
        calls = []
        failure = TraceError("Synthetic item inventory failure")

        def transport(method, url, headers, body, timeout):
            parsed = urlsplit(url)
            kind = parsed.path.rsplit("/", 1)[-1]
            self.assertEqual(method, "GET")
            self.assertNotIn("cursor", parse_qs(parsed.query))
            calls.append(kind)
            together.wait(timeout=3)
            if kind == "items":
                raise failure
            self.assertTrue(release.wait(timeout=3))
            connector_finished.set()
            return 200, {}, canonical({"data": [{"id": "connector-1"}], "cursor": "unused"})

        def capture():
            with MiroRequests(transport, interval=0, workers=2) as requests:
                instance.append(requests)
                requests_ready.set()
                return read_live(requests, BASE, {}, STATE)

        with patch("liquid_tracer.miro_reads.preflight") as preflight:
            with ThreadPoolExecutor(max_workers=1) as executor:
                result = executor.submit(capture)
                try:
                    self.assertTrue(requests_ready.wait(timeout=3))
                    self.assertTrue(instance[0]._cancel.wait(timeout=3))
                    self.assertFalse(result.done())
                    self.assertFalse(connector_finished.is_set())
                finally:
                    release.set()
                with self.assertRaises(TraceError) as caught:
                    result.result(timeout=3)
            self.assertIs(caught.exception, failure)
            self.assertTrue(connector_finished.is_set())
            self.assertCountEqual(calls, ["items", "connectors"])
            preflight.assert_not_called()


if __name__ == "__main__":
    unittest.main()
