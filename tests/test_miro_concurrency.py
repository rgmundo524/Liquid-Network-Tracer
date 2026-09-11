import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path

from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.miro import make_plan, sync
from tests.test_miro_sync import FakeMiro, graph


class ObservedMiro(FakeMiro):
    """An in-memory board that exposes overlap without racing its own records."""

    def __init__(self, overlap=False):
        super().__init__()
        self.lock = threading.Lock()
        self.active = {}
        self.peak = {}
        self.overlap = overlap
        self.gates = {method: threading.Barrier(2) for method in ("GET", "PATCH")}
        self.started = {}
        self.completed_reads = 0
        self.reads_before_write = []

    def __call__(self, method, url, headers, body, timeout):
        with self.lock:
            self.active[method] = self.active.get(method, 0) + 1
            self.peak[method] = max(self.peak.get(method, 0), self.active[method])
            self.started[method] = self.started.get(method, 0) + 1
            ordinal = self.started[method]
            if method != "GET":
                self.reads_before_write.append(self.completed_reads)
        try:
            if self.overlap and method in self.gates and ordinal <= 2:
                self.gates[method].wait(timeout=3)
            if self.overlap:
                time.sleep(.005)
            with self.lock:
                result = super().__call__(method, url, headers, body, timeout)
                if method == "GET":
                    self.completed_reads += 1
                return result
        finally:
            with self.lock:
                self.active[method] -= 1


class MiroConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "miro.json"
        self.remote = ObservedMiro()
        self.initial_plan = make_plan(graph())
        self.call(self.initial_plan, workers=1)
        self.remote.calls.clear()
        self.remote.active.clear()
        self.remote.peak.clear()
        self.remote.started.clear()
        self.remote.reads_before_write.clear()
        self.remote.completed_reads = 0

    def call(self, plan, **kwargs):
        transport = kwargs.pop("transport", self.remote)
        return sync(plan, "board=", self.state_path, token="test-token",
                    transport=transport, interval=0, **kwargs)

    def revised_plan(self, keys=None, *, extended=False):
        result = graph("two" if extended else "one", extended)
        for node in result["nodes"]:
            if keys is None or node["id"] in keys:
                node["label"] += " revised"
                node["color"] = "#aabbcc"
                node["x"] += 40
        for edge in result["edges"]:
            if keys is None or edge["id"] in keys:
                edge["label"] += " revised"
        return make_plan(result)

    def remote_id(self, key):
        return read_json(self.state_path)["items"][key]["id"]

    def test_parallel_reorganization_matches_serial_and_checks_every_item_first(self):
        serial_path = Path(self.tmp.name) / "serial.json"
        initial_state = read_json(self.state_path)
        save_json(serial_path, initial_state)
        serial_remote = ObservedMiro()
        serial_remote.items = copy.deepcopy(self.remote.items)
        serial_remote.counter = self.remote.counter
        plan = self.revised_plan(extended=True)
        serial = sync(plan, "board=", serial_path, token="test-token",
                      transport=serial_remote, interval=0, workers=1, reorganize=True)
        self.remote.overlap = True
        callback_threads = []
        caller = threading.get_ident()
        parallel = self.call(plan, workers=4, reorganize=True,
                             progress=lambda event: callback_threads.append(threading.get_ident()))
        self.assertEqual(self.remote.items, serial_remote.items)
        self.assertEqual(read_json(self.state_path)["items"], read_json(serial_path)["items"])
        for field in ("created", "updated", "moved", "reattached", "conflicts"):
            self.assertEqual(parallel[field], serial[field])
        self.assertGreater(parallel["updated"], 1)
        for method in ("GET", "PATCH"):
            self.assertGreater(self.remote.peak[method], 1)
            self.assertLessEqual(self.remote.peak[method], 4)
            self.assertEqual(serial_remote.peak[method], 1)
        self.assertEqual(self.remote.peak["POST"], 1)
        self.assertEqual(set(self.remote.reads_before_write), {len(initial_state["items"])})
        self.assertTrue(callback_threads)
        self.assertEqual(set(callback_threads), {caller})

    def test_failed_parallel_preflight_makes_no_writes(self):
        missing = self.remote_id("addr:b")
        del self.remote.items[missing]
        before = self.state_path.read_bytes()
        self.remote.overlap = True
        with self.assertRaisesRegex(TraceError, "[Nn]o board writes"):
            self.call(self.revised_plan(extended=True), workers=4, reorganize=True)
        self.assertEqual(self.remote.writes, [])
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertGreater(self.remote.peak["GET"], 1)

    def test_failed_patch_drains_other_inflight_success_and_keeps_recovery_journal(self):
        failed_id = self.remote_id("addr:a")
        successful_id = self.remote_id("addr:b")
        success_started = threading.Event()
        failure_returned = threading.Event()
        plan = self.revised_plan({"addr:a", "addr:b"}, extended=True)
        pending_seen = []

        def transport(method, url, *args):
            if method == "PATCH":
                pending_seen.append(copy.deepcopy(read_json(self.state_path).get("pending_updates", {})))
                if url.endswith("/" + successful_id):
                    success_started.set()
                    if not failure_returned.wait(3):
                        raise AssertionError("The failed PATCH did not start concurrently")
                    # Finish after the coordinator has an opportunity to see the failure.
                    time.sleep(.02)
                elif url.endswith("/" + failed_id):
                    if not success_started.wait(3):
                        raise AssertionError("The successful PATCH did not start concurrently")
                    failure_returned.set()
                    return 500, {}, b"{}"
            return self.remote(method, url, *args)

        with self.assertRaises(TraceError):
            self.call(plan, workers=2, transport=transport)
        state = read_json(self.state_path)
        intended = next(item["body"]["data"]["content"] for item in plan["shapes"] if item["key"] == "addr:b")
        self.assertEqual(state["items"]["addr:b"]["managed"]["data"]["content"], intended)
        self.assertEqual(state["items"]["addr:b"]["intent"]["data"]["content"], intended)
        self.assertNotIn("addr:b", state.get("pending_updates", {}))
        self.assertEqual(state["pending_updates"]["addr:a"]["id"], failed_id)
        self.assertEqual(state["active_run_id"], "two")
        self.assertEqual([call for call in self.remote.writes if call[0] == "POST"], [])
        self.assertTrue(pending_seen)
        self.assertTrue(all("addr:a" in pending and "addr:b" in pending for pending in pending_seen))
        recovered = self.call(plan, workers=2)
        self.assertEqual(recovered["updated"], 1)
        self.assertEqual(read_json(self.state_path).get("pending_updates", {}), {})
        self.assertIsNone(read_json(self.state_path)["active_run_id"])

    def lose_patch_response(self):
        plan = self.revised_plan({"addr:a"})
        item_id = self.remote_id("addr:a")
        lost = False

        def transport(method, url, *args):
            nonlocal lost
            result = self.remote(method, url, *args)
            if method == "PATCH" and url.endswith("/" + item_id) and not lost:
                lost = True
                raise TraceError("Synthetic lost PATCH response")
            return result

        with self.assertRaisesRegex(TraceError, "lost PATCH"):
            self.call(plan, workers=2, transport=transport)
        self.assertIn("addr:a", read_json(self.state_path)["pending_updates"])
        self.remote.calls.clear()
        return plan, item_id

    def test_lost_patch_response_is_adopted_without_repeating_the_write(self):
        plan, item_id = self.lose_patch_response()
        report = self.call(plan, workers=2)
        self.assertEqual(report["updated"], 0)
        self.assertEqual(report["conflicts"], [])
        self.assertEqual(self.remote.writes, [])
        state = read_json(self.state_path)
        self.assertEqual(state.get("pending_updates", {}), {})
        self.assertEqual(state["items"]["addr:a"]["managed"]["data"]["content"],
                         self.remote.items[item_id]["data"]["content"])

    def test_manual_edit_after_lost_patch_response_is_preserved(self):
        plan, item_id = self.lose_patch_response()
        self.remote.items[item_id]["data"]["content"] = "Investigator annotation after interrupted sync"
        self.remote.items[item_id]["style"]["fillColor"] = "#fedcba"
        expected = copy.deepcopy(self.remote.items[item_id])
        report = self.call(plan, workers=2)
        self.assertEqual(self.remote.items[item_id], expected)
        self.assertEqual(report["updated"], 0)
        self.assertEqual(self.remote.writes, [])
        self.assertTrue(report["conflicts"])
        self.assertEqual(read_json(self.state_path).get("pending_updates", {}), {})

    def test_worker_count_is_validated_before_requests(self):
        for workers in (0, -1, 5, True, False, 1.5, "4", None):
            with self.subTest(workers=workers), self.assertRaisesRegex(TraceError, "worker"):
                self.call(self.initial_plan, workers=workers)
        self.assertEqual(self.remote.calls, [])

    def test_long_rate_limit_after_rejected_post_does_not_leave_uncertain_creation(self):
        attempts = []

        def transport(method, url, *args):
            if method == "POST":
                attempts.append(url)
                return 429, {"Retry-After": "31"}, b"{}"
            return self.remote(method, url, *args)

        with self.assertRaisesRegex(TraceError, "wait for the limit to reset"):
            self.call(make_plan(graph("two", True)), workers=4, transport=transport)
        state = read_json(self.state_path)
        self.assertEqual(len(attempts), 1)
        self.assertIsNone(state["pending"])
        self.assertEqual(len(state["items"]), 7)
        self.assertEqual(state["active_run_id"], "two")

    def test_quota_refusal_before_post_keeps_only_acknowledged_creation(self):
        state_path = Path(self.tmp.name) / "fresh.json"
        remote = ObservedMiro()
        posts = []

        def transport(method, url, *args):
            result = remote(method, url, *args)
            if method == "POST":
                posts.append(url)
                return result[0], {"X-RateLimit-Remaining": "0",
                                   "X-RateLimit-Reset": str(time.time() + 60)}, result[2]
            return result

        with self.assertRaisesRegex(TraceError, "wait for the limit to reset"):
            sync(self.initial_plan, "board=", state_path, token="test-token",
                 transport=transport, interval=0, workers=4)
        state = read_json(state_path)
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(state["items"]), 1)
        self.assertEqual({entry["id"] for entry in state["items"].values()}, set(remote.items))
        self.assertIsNone(state["pending"])
        report = sync(self.initial_plan, "board=", state_path, token="test-token",
                      transport=remote, interval=0, workers=4)
        self.assertEqual(report["created"], 6)
        self.assertEqual(len(remote.items), 7)


if __name__ == "__main__":
    unittest.main()
