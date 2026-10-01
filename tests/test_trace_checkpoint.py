"""Trace snapshots stay bounded and save complete terminal/recovery states."""

import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from liquid_tracer.api import Esplora, Limits
from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.store import Store
from liquid_tracer.trace import new_state, trace
from liquid_tracer.trace_checkpoint import TraceCheckpoint
from tests.fixtures import A, B, fixture
from tests.test_trace_concurrency import evidence_topology


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class TraceCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.checkpoint = TraceCheckpoint(clock=self.clock)

    def test_skipped_saves_do_not_build_statistics_or_serialize(self):
        build_and_save = Mock()
        self.assertTrue(self.checkpoint.save(build_and_save, force=True, completed=False))
        for _ in range(255):
            self.assertFalse(self.checkpoint.save(build_and_save))
        self.assertEqual(build_and_save.call_count, 1)
        self.assertTrue(self.checkpoint.save(build_and_save))
        self.assertEqual(build_and_save.call_count, 2)
        self.assertEqual(self.checkpoint.pending_operations, 0)

    def test_time_limit_saves_before_operation_limit(self):
        callback = Mock()
        self.clock.advance(1.99)
        self.assertFalse(self.checkpoint.save(callback))
        self.clock.advance(.02)
        self.assertTrue(self.checkpoint.save(callback))
        callback.assert_called_once_with()

    def test_interval_starts_after_slow_write_completes(self):
        self.checkpoint.save(lambda: self.clock.advance(10), force=True, completed=False)
        self.assertEqual(self.checkpoint.write_seconds, 10)
        self.assertEqual(self.checkpoint.writes, 1)
        callback = Mock()
        self.assertFalse(self.checkpoint.save(callback))
        self.clock.advance(2)
        self.assertTrue(self.checkpoint.save(callback))

    def test_clean_final_save_includes_changes_outside_output_processing(self):
        state, snapshots = {"status": "running"}, []
        save = lambda: snapshots.append(dict(state))
        self.checkpoint.save(save, force=True, completed=False)
        state["status"] = "bounded_complete"
        self.checkpoint.save(save, force=True, completed=False)
        self.assertEqual(snapshots, [{"status": "running"}, {"status": "bounded_complete"}])
        self.assertEqual(self.checkpoint.pending_operations, 0)

    def test_idle_poll_does_not_save_an_unchanged_snapshot(self):
        callback = Mock()
        self.clock.advance(100)
        self.assertFalse(self.checkpoint.save(callback, completed=False))
        callback.assert_not_called()

    def test_failed_write_preserves_dirty_operations_and_counts_elapsed(self):
        checkpoint = TraceCheckpoint(max_operations=1, clock=self.clock)

        def fail():
            self.clock.advance(.5)
            raise OSError("synthetic disk failure")

        with self.assertRaises(OSError):
            checkpoint.save(fail)
        self.assertEqual(checkpoint.pending_operations, 1)
        self.assertEqual(checkpoint.writes, 0)
        self.assertEqual(checkpoint.write_seconds, .5)
        self.assertTrue(checkpoint.save(lambda: None, completed=False))
        self.assertEqual(checkpoint.writes, 1)
        self.assertEqual(checkpoint.pending_operations, 0)

    def test_recursive_callback_fails_without_unbounded_writes(self):
        with self.assertRaisesRegex(RuntimeError, "recursively"):
            self.checkpoint.save(lambda: self.checkpoint.save(lambda: None, force=True), force=True)
        self.assertEqual(self.checkpoint.writes, 0)
        self.assertTrue(self.checkpoint.save(lambda: None, force=True))

    def test_invalid_policy_values(self):
        for interval in (0, -1, float("nan"), float("inf")):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                TraceCheckpoint(interval_seconds=interval)
        for maximum in (0, -1, 1.5, True):
            with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                TraceCheckpoint(max_operations=maximum)


class TraceCheckpointRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture_path = self.root / "fixture.json"
        save_json(self.fixture_path, fixture())
        self.store = Store(self.root / "evidence")
        self.addCleanup(self.store.close)

    def run_trace(self, *, parent=None, fail=None, max_outpoints=2000):
        limits = Limits(max_hops=3, max_outpoints=max_outpoints)
        with Esplora(self.store, "pending", limits, fixture=self.fixture_path,
                     min_interval=0, workers=1) as api:
            state = new_state([A + ":0"], api.base, limits, [], parent)
            api.run_id = state["run_id"]
            path = self.root / state["run_id"] / "trace.json"
            actual_get = api.get
            snapshots = []

            def get(endpoint):
                if fail and endpoint == "/tx/" + B + "/outspends":
                    raise fail
                return actual_get(endpoint)

            def write(destination, value):
                save_json(destination, value)
                snapshots.append(read_json(destination))

            module = importlib.import_module("liquid_tracer.trace")
            with patch.object(api, "get", get), patch.object(module, "save_json", write):
                result = trace(api, state, limits, path)
            saved = read_json(path)
            self.assertEqual(evidence_topology(saved), evidence_topology(result))
            self.assertEqual(saved["finished_at"], result["finished_at"])
            self.assertEqual(saved["observations"], result["observations"])
            return result, snapshots

    def test_completed_run_saves_initial_and_terminal_state(self):
        result, snapshots = self.run_trace()
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0]["status"], "running")
        self.assertEqual(snapshots[-1]["status"], "bounded_complete")
        self.assertGreater(result["stats"]["outpoints_examined_this_run"], len(snapshots))

    def test_empty_continuation_still_saves_terminal_state(self):
        parent, _ = self.run_trace()
        result, snapshots = self.run_trace(parent=parent)
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(result["stats"]["outpoints_examined_this_run"], 0)
        self.assertEqual(snapshots[-1]["status"], "bounded_complete")

    def test_error_interruption_and_limit_force_resumable_checkpoint(self):
        complete, _ = self.run_trace()
        for failure, maximum, expected in (
                (TraceError("synthetic API failure"), 2000, "error"),
                (KeyboardInterrupt(), 2000, "interrupted"),
                (None, 1, "outpoint_limit")):
            with self.subTest(reason=expected):
                interrupted, snapshots = self.run_trace(fail=failure, max_outpoints=maximum)
                self.assertEqual(len(snapshots), 2)
                self.assertEqual(snapshots[-1]["stop_reason"], expected)
                self.assertFalse(any(item["status"] == "pending"
                                     for item in snapshots[-1]["outputs"].values()))
                resumed, _ = self.run_trace(parent=snapshots[-1])
                self.assertEqual(evidence_topology(resumed), evidence_topology(complete))


if __name__ == "__main__":
    unittest.main()
