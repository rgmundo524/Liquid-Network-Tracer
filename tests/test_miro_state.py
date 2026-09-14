"""Crash recovery and write-volume contracts for Miro's incremental state."""

from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer import miro_state
from liquid_tracer.miro_state import SyncState, journal_path, load_state


class MiroStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "sync.json"
        self.initial = {"schema_version": 2, "board_id": "synthetic-board",
                        "namespace": {"case_id": "synthetic-case"},
                        "items": {}, "pending": None, "runs": {}}

    def begin(self):
        state = load_state(self.path, self.initial)
        writer = SyncState(self.path, state).__enter__()
        self.addCleanup(self.close_abruptly, writer)
        return state, writer

    @staticmethod
    def close_abruptly(writer):
        if writer._connection is not None:
            writer._connection.close()
            writer._connection = None

    def test_legacy_snapshot_and_default_reads_do_not_create_files(self):
        default = load_state(self.path, self.initial)
        default["items"]["changed"] = {}
        self.assertEqual(self.initial["items"], {})
        self.assertEqual(list(self.path.parent.iterdir()), [])
        save_json(self.path, self.initial)
        before = (self.path.stat().st_mtime_ns, self.path.read_bytes())
        self.assertEqual(load_state(self.path), self.initial)
        self.assertEqual(before, (self.path.stat().st_mtime_ns, self.path.read_bytes()))
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_success_and_exception_materialize_portable_snapshot(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                state = load_state(self.path, self.initial)
                try:
                    with SyncState(self.path, state) as writer:
                        writer.commit(sets=[(("pending",), {"key": "shape-a"})])
                        writer.commit(sets=[(("items", "shape-a"), {"id": "remote-a"}),
                                            (("pending",), None)])
                        if failure:
                            raise RuntimeError("synthetic renderer failure")
                except RuntimeError:
                    pass
                self.assertEqual(read_json(self.path), state)
                self.assertNotIn("_sync_journal", state)
                self.assertFalse(journal_path(self.path).exists())
                copy_path = self.path.with_name("copied.json")
                copy_path.write_bytes(self.path.read_bytes())
                self.assertEqual(load_state(copy_path), state)

    def test_real_process_crash_preserves_intent_and_acknowledgement(self):
        script = '''
import json, os, sys
from liquid_tracer.miro_state import SyncState
path, acknowledged = sys.argv[1:]
state = json.loads(sys.stdin.read())
writer = SyncState(path, state).__enter__()
writer.commit(sets=[(("pending",), {"key": "shape-a", "endpoint": "shapes"})])
if acknowledged == "yes":
    writer.commit(sets=[(("items", "shape-a"), {"id": "remote-a"}), (("pending",), None)])
os._exit(23)
'''
        for acknowledged in (False, True):
            with self.subTest(acknowledged=acknowledged), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "sync.json"
                result = subprocess.run([sys.executable, "-c", script, str(path), "yes" if acknowledged else "no"],
                                        input=json.dumps(self.initial), text=True, capture_output=True)
                self.assertEqual(result.returncode, 23, result.stderr)
                self.assertIn("_sync_journal", read_json(path))
                recovered = load_state(path)
                if acknowledged:
                    self.assertIsNone(recovered["pending"])
                    self.assertEqual(recovered["items"]["shape-a"]["id"], "remote-a")
                else:
                    self.assertEqual(recovered["pending"]["key"], "shape-a")
                    self.assertEqual(recovered["items"], {})
                with SyncState(path, recovered):
                    pass
                self.assertEqual(read_json(path), recovered)
                self.assertFalse(journal_path(path).exists())

    @unittest.skipUnless(hasattr(__import__("signal"), "SIGKILL"), "requires process termination")
    def test_sigkill_during_spilled_transaction_recovers_without_writing_original(self):
        script = """
import json, sys
from liquid_tracer.miro_state import SyncState
state = json.loads(sys.stdin.readline())
writer = SyncState(sys.argv[1], state).__enter__()
writer.commit(sets=[(("items", "acknowledged"), {"id": "durable-id"})])
connection = writer._connection
connection.execute("PRAGMA cache_size=1")
connection.execute("BEGIN")
connection.execute("INSERT INTO operations(sequence,payload,checksum) VALUES (2,?,?)", ("x" * (2 * 1024 * 1024), "uncommitted"))
connection.execute("UPDATE metadata SET value='2' WHERE key='last_sequence'")
print("spilled", flush=True)
sys.stdin.read()
"""
        process = subprocess.Popen([sys.executable, "-c", script, str(self.path)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            process.stdin.write(json.dumps(self.initial) + "\n")
            process.stdin.flush()
            self.assertEqual(process.stdout.readline().strip(), "spilled")
            process.kill()
            process.wait(timeout=5)
            rollback = journal_path(self.path).with_name(journal_path(self.path).name + "-journal")
            self.assertTrue(rollback.exists())
            originals = {path: (path.read_bytes(), miro_state._file_identity(path))
                         for path in self.path.parent.iterdir()}
            recovered = load_state(self.path)
            self.assertEqual(recovered["items"], {"acknowledged": {"id": "durable-id"}})
            self.assertEqual(originals, {path: (path.read_bytes(), miro_state._file_identity(path))
                                         for path in self.path.parent.iterdir()})
            with SyncState(self.path, recovered) as writer:
                writer.commit(sets=[(("items", "continued"), {"id": "next-id"})])
            self.assertEqual(set(read_json(self.path)["items"]), {"acknowledged", "continued"})
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()

    def test_recovery_read_is_read_only_and_preserves_file_permissions(self):
        state, writer = self.begin()
        writer.commit(sets=[(("pending",), {"key": "shape-a"})])
        files = sorted(self.path.parent.iterdir())
        before = {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
        for path in files:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(load_state(self.path), state)
        self.assertEqual(files, sorted(self.path.parent.iterdir()))
        self.assertEqual(before, {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in files})

    def test_missing_journal_and_unrelated_snapshot_fail_closed(self):
        _, writer = self.begin()
        writer.commit(sets=[(("pending",), {"key": "shape-a"})])
        self.close_abruptly(writer)
        original = self.path.read_bytes()
        modified = read_json(self.path)
        modified["board_id"] = "different-board"
        save_json(self.path, modified)
        with self.assertRaisesRegex(TraceError, "do not match"):
            load_state(self.path)
        self.path.write_bytes(original)
        journal_path(self.path).unlink()
        with self.assertRaisesRegex(TraceError, "journal is missing"):
            load_state(self.path, self.initial)
        with self.assertRaisesRegex(TraceError, "journal is missing"):
            SyncState(self.path, self.initial).__enter__()

    def test_corrupt_checksum_missing_row_and_truncated_database_fail_closed(self):
        for damage in ("checksum", "missing", "truncated"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.json"
                writer = SyncState(path, copy.deepcopy(self.initial)).__enter__()
                writer.commit(sets=[(("pending",), {"key": "shape-a"})])
                self.close_abruptly(writer)
                if damage == "truncated":
                    data = journal_path(path).read_bytes()
                    journal_path(path).write_bytes(data[:100])
                else:
                    with sqlite3.connect(journal_path(path)) as connection:
                        if damage == "missing":
                            connection.execute("DELETE FROM operations")
                        else:
                            connection.execute("UPDATE operations SET checksum='incorrect'")
                with self.assertRaises(TraceError):
                    load_state(path)

    def test_checkpoint_crashes_on_either_side_of_snapshot_replace_recover(self):
        original_write = miro_state._write_snapshot
        for after_replace in (False, True):
            with self.subTest(after_replace=after_replace), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.json"
                state = copy.deepcopy(self.initial)
                writer = SyncState(path, state).__enter__()
                writer.commit(sets=[(("items", "shape-a"), {"id": "remote-a"})])
                def interrupted_write(target, text):
                    if after_replace:
                        original_write(target, text)
                    raise KeyboardInterrupt()
                with patch.object(miro_state, "_write_snapshot", side_effect=interrupted_write):
                    with self.assertRaises(KeyboardInterrupt):
                        writer.__exit__(None, None, None)
                recovered = load_state(path)
                self.assertEqual(recovered, state)
                with SyncState(path, recovered) as replacement:
                    replacement.commit(sets=[(("items", "shape-b"), {"id": "remote-b"})])
                self.assertEqual(set(read_json(path)["items"]), {"shape-a", "shape-b"})

    def test_setup_crash_before_marked_snapshot_retains_recoverable_state(self):
        save_json(self.path, self.initial)
        state = load_state(self.path)
        with patch.object(miro_state, "_write_snapshot", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                SyncState(self.path, state).__enter__()
        self.assertEqual(load_state(self.path), state)
        with SyncState(self.path, state):
            pass
        self.assertEqual(read_json(self.path), state)

    def test_reopening_crash_does_not_orphan_existing_snapshot_token(self):
        state, writer = self.begin()
        writer.commit(sets=[(("pending",), {"key": "shape-a"})])
        self.close_abruptly(writer)
        recovered = load_state(self.path)
        with patch.object(miro_state, "_write_snapshot", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                SyncState(self.path, recovered).__enter__()
        self.assertEqual(load_state(self.path), recovered)

    def test_failed_ack_commit_cannot_publish_uncommitted_memory(self):
        state, writer = self.begin()
        writer.commit(sets=[(("pending",), {"key": "shape-a"})])
        writer._connection.execute("CREATE TRIGGER reject_op BEFORE INSERT ON operations BEGIN SELECT RAISE(ABORT, 'full'); END")
        state["items"]["shape-a"] = {"id": "remote-a"}
        state["pending"] = None
        with self.assertRaisesRegex(TraceError, "could not be saved"):
            writer.commit(sets=[(("items", "shape-a"), state["items"]["shape-a"]), (("pending",), None)])
        writer.__exit__(None, None, None)
        restored = read_json(self.path)
        self.assertEqual(restored["pending"], {"key": "shape-a"})
        self.assertEqual(restored["items"], {})

    def test_invalid_overlapping_paths_are_rejected_before_durable_commit(self):
        _, writer = self.begin()
        with self.assertRaisesRegex(TraceError, "non-object"):
            writer.commit(sets=[(("items",), None), (("items", "shape-a"), {})])
        with self.assertRaisesRegex(TraceError, "mutation path"):
            writer.commit(sets=[(("_sync_journal",), {})])
        self.assertEqual(writer._connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)
        self.assertEqual(load_state(self.path), self.initial)

    def test_commits_support_deletes_and_detach_mutable_values(self):
        state, writer = self.begin()
        intent = {"patch": {"style": {"color": "blue"}}}
        writer.commit(sets=[(("pending_updates", "shape-a"), intent)])
        intent["patch"]["style"]["color"] = "red"
        self.assertEqual(state["pending_updates"]["shape-a"]["patch"]["style"]["color"], "blue")
        writer.commit(deletes=[("pending_updates", "shape-a"), ("missing", "ignored")])
        self.assertEqual(load_state(self.path)["pending_updates"], {})

    def test_phase_checkpoint_keeps_journal_required_and_captures_committed_state(self):
        state, writer = self.begin()
        writer.commit(sets=[(("pending_updates", "shape-a"), {"patch": {}})])
        writer.checkpoint()
        self.assertEqual(read_json(self.path)["pending_updates"], state["pending_updates"])
        self.assertIn("_sync_journal", read_json(self.path))
        self.assertEqual(writer._connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)
        writer.commit(deletes=[("pending_updates", "shape-a")])
        self.assertEqual(load_state(self.path)["pending_updates"], {})

    def test_incremental_payload_volume_is_linear_and_snapshots_are_constant(self):
        with patch.object(miro_state, "_write_snapshot", wraps=miro_state._write_snapshot) as snapshots:
            with SyncState(self.path, copy.deepcopy(self.initial)) as writer:
                size_at_half = None
                for index in range(400):
                    writer.commit(sets=[(("items", f"shape-{index:04d}"), {"id": f"remote-{index:04d}", "body": "x" * 500})])
                    if index == 199:
                        size_at_half = writer._connection.execute("SELECT SUM(length(payload)) FROM operations").fetchone()[0]
                size_at_end = writer._connection.execute("SELECT SUM(length(payload)) FROM operations").fetchone()[0]
                self.assertEqual(size_at_end, size_at_half * 2)
                self.assertEqual(snapshots.call_count, 1)
            self.assertEqual(snapshots.call_count, 2)
        self.assertEqual(len(read_json(self.path)["items"]), 400)

    def test_finalization_waits_for_inflight_durable_commit(self):
        state, writer = self.begin()
        entered, release, finalized = threading.Event(), threading.Event(), threading.Event()
        original_apply = miro_state._apply
        def paused_apply(target, changes):
            if target is state:
                entered.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("synthetic test synchronization timed out")
            return original_apply(target, changes)
        def finalize():
            writer.__exit__(None, None, None)
            finalized.set()
        with patch.object(miro_state, "_apply", side_effect=paused_apply), ThreadPoolExecutor(max_workers=2) as workers:
            commit = workers.submit(writer.commit, sets=[(("items", "shape-a"), {"id": "remote-a"})])
            try:
                self.assertTrue(entered.wait(timeout=5))
                finish = workers.submit(finalize)
                self.assertFalse(finalized.wait(timeout=.05))
            finally:
                release.set()
            commit.result(timeout=5)
            finish.result(timeout=5)
        self.assertEqual(read_json(self.path)["items"], {"shape-a": {"id": "remote-a"}})
        self.assertFalse(journal_path(self.path).exists())

    def test_read_retries_when_rollback_file_disappears_before_copy(self):
        state, writer = self.begin()
        writer.commit(sets=[(("pending",), {"key": "shape-a"})])
        rollback = journal_path(self.path).with_name(journal_path(self.path).name + "-journal")
        rollback.write_bytes(b"")
        original_open = Path.open
        disappeared = []
        def interrupted_open(path, *args, **kwargs):
            if path == rollback and not disappeared:
                disappeared.append(True)
                rollback.unlink()
                raise FileNotFoundError(str(rollback))
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", interrupted_open):
            recovered = load_state(self.path)
        self.assertEqual(disappeared, [True])
        self.assertEqual(recovered, state)

    def test_read_waits_for_local_inflight_commit_and_returns_durable_intent(self):
        state, writer = self.begin()
        entered, release = threading.Event(), threading.Event()
        original_apply = miro_state._apply
        def paused_apply(target, changes):
            if target is state:
                entered.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("synthetic synchronization timed out")
            return original_apply(target, changes)
        with patch.object(miro_state, "_apply", side_effect=paused_apply), ThreadPoolExecutor(max_workers=2) as workers:
            commit = workers.submit(writer.commit, sets=[(("pending",), {"key": "shape-a"})])
            try:
                self.assertTrue(entered.wait(timeout=5))
                reading = workers.submit(load_state, self.path)
                self.assertFalse(reading.done())
            finally:
                release.set()
            commit.result(timeout=5)
            self.assertEqual(reading.result(timeout=5)["pending"], {"key": "shape-a"})

    def test_continuously_changing_source_has_bounded_clear_retry_error(self):
        with patch.object(miro_state, "_load_state_once", side_effect=miro_state._StateChanged("synthetic transition")) as reading:
            with patch.object(miro_state.time, "sleep"):
                with self.assertRaisesRegex(TraceError, "active sync.*retry"):
                    load_state(self.path, self.initial)
        self.assertEqual(reading.call_count, 5)

    def test_parallel_commits_are_serialized_and_closed_writer_rejects_late_work(self):
        state, writer = self.begin()
        with ThreadPoolExecutor(max_workers=8) as workers:
            futures = [workers.submit(writer.commit, sets=[(("items", str(index)), {"id": str(index)})])
                       for index in range(80)]
            for future in futures:
                future.result()
        self.assertEqual(len(load_state(self.path)["items"]), 80)
        writer.__exit__(None, None, None)
        with self.assertRaisesRegex(TraceError, "closed"):
            writer.commit(sets=[(("items", "late"), {})])
        self.assertEqual(read_json(self.path), state)


if __name__ == "__main__":
    unittest.main()
