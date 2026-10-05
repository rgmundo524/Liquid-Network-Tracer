"""Display summaries load in stages without reading large traces in requests."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import tracemalloc
import unittest
from unittest.mock import patch

from liquid_tracer.common import read_json, save_json
from liquid_tracer import run_summaries
from liquid_tracer.run_summaries import get_run_summary, remember_run_summary


class RunSummaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(run_summaries._queue.join)
        self.root = Path(temporary.name)
        self.case_id = "a" * 32
        self.archive = self.root / "case" / "runs" / ("b" * 16)
        self.archive.mkdir(parents=True)
        self.state = {"case_id": self.case_id, "run_id": self.archive.name,
                      "source": "fixture://synthetic-summary", "status": "bounded_complete",
                      "stop_reason": "max_hops", "started_at": "2026-10-02T00:00:00Z",
                      "seeds": ["1" * 64 + ":0"], "limits": {"max_hops": 4},
                      "stats": {"transactions_cumulative": 7, "frontier_count": 3},
                      "transactions": {"first": {"depth": 0}, "last": {"depth": 2}},
                      "performance": {"schema_version": 1, "request_count": 5, "secret": "not public"},
                      "shared_collection": {"dataset_id": self.case_id, "members": [{"id": "c" * 32, "name": "Case"}],
                                            "policy_case_name": "Case", "service_controls": {"large": "not needed"}}}
        self.write()

    def write(self, state=None):
        save_json(self.archive / "trace.json", self.state if state is None else state)
        (self.archive / "SHA256SUMS").write_text("synthetic display-only seal\n")

    def clear_memory(self):
        run_summaries._queue.join()
        with run_summaries._lock:
            run_summaries._results.clear()

    def finish(self):
        run_summaries._queue.join()
        return get_run_summary(self.archive, self.case_id, schedule=False)

    def test_known_sealed_run_remembers_in_memory_without_reading_trace(self):
        with patch("liquid_tracer.run_summaries._read_summary_fields", side_effect=AssertionError("no trace read")):
            self.assertTrue(remember_run_summary(self.archive, self.state))
            summary, status = get_run_summary(self.archive, self.case_id)
        self.assertEqual(status, "ready")
        self.assertEqual(summary["transaction_count"], 7)
        self.assertEqual(summary["frontier_count"], 3)
        self.assertEqual(summary["collected_hops"], 2)
        self.assertEqual(summary["seeds"], self.state["seeds"])
        self.assertEqual(summary["max_hops"], 4)
        self.assertEqual(summary["performance"], {"schema_version": 1, "request_count": 5})
        self.assertNotIn("service_controls", summary["shared_collection"])
        summary["seeds"].append("external mutation")
        self.assertEqual(get_run_summary(self.archive, self.case_id)[0]["seeds"], self.state["seeds"])

    def test_schedule_false_does_not_read_or_queue_legacy_archive(self):
        with patch("liquid_tracer.run_summaries._read_summary_fields", side_effect=AssertionError("no bootstrap")):
            self.assertEqual(get_run_summary(self.archive, self.case_id, schedule=False), (None, "loading"))
            with run_summaries._lock:
                self.assertFalse(any(key == run_summaries.digest(run_summaries.canonical(
                    run_summaries._binding(self.archive, self.case_id))) for key in run_summaries._pending))

    def test_slow_legacy_read_is_background_and_deduplicated(self):
        started, release = threading.Event(), threading.Event()
        reader = run_summaries._read_summary_fields
        calls = []
        main_thread = threading.get_ident()
        def slow(path):
            calls.append(threading.get_ident())
            started.set()
            if not release.wait(5):
                raise AssertionError("test failed to release reader")
            return reader(path)
        with patch("liquid_tracer.run_summaries._read_summary_fields", side_effect=slow):
            try:
                self.assertEqual(get_run_summary(self.archive, self.case_id), (None, "loading"))
                self.assertTrue(started.wait(5))
                with ThreadPoolExecutor(max_workers=8) as pool:
                    answers = list(pool.map(lambda _: get_run_summary(self.archive, self.case_id), range(40)))
                self.assertEqual(answers, [(None, "loading")] * 40)
                self.assertEqual(len(calls), 1)
                self.assertNotEqual(calls[0], main_thread)
            finally:
                release.set()
                run_summaries._queue.join()
        self.assertEqual(self.finish()[1], "ready")
        self.assertTrue(run_summaries._worker.daemon)

    def test_persistent_summary_survives_memory_reset_without_trace_read(self):
        self.assertTrue(remember_run_summary(self.archive, self.state))
        expected = get_run_summary(self.archive, self.case_id)
        self.clear_memory()
        with patch("liquid_tracer.run_summaries._read_summary_fields", side_effect=AssertionError("warm read")):
            self.assertEqual(get_run_summary(self.archive, self.case_id, schedule=False), expected)

    def test_trace_or_manifest_change_invalidates_even_with_restored_mtime(self):
        self.assertTrue(remember_run_summary(self.archive, self.state))
        path = self.archive / "trace.json"
        previous = path.stat()
        self.state["stats"]["transactions_cumulative"] = 9
        save_json(path, self.state)
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        self.assertEqual(get_run_summary(self.archive, self.case_id, schedule=False), (None, "loading"))
        get_run_summary(self.archive, self.case_id)
        self.assertEqual(self.finish()[0]["transaction_count"], 9)
        (self.archive / "SHA256SUMS").write_text("changed manifest\n")
        self.assertEqual(get_run_summary(self.archive, self.case_id, schedule=False), (None, "loading"))

    def test_corrupt_persistent_cache_rebuilds_and_missing_source_is_unavailable(self):
        remember_run_summary(self.archive, self.state)
        cached = next((self.archive.parent.parent / "run-summaries").glob("*.json"))
        self.clear_memory()
        content = read_json(cached)
        content["payload"]["summary"]["transaction_count"] = 1000
        save_json(cached, content)
        self.assertEqual(get_run_summary(self.archive, self.case_id), (None, "loading"))
        self.assertEqual(self.finish()[0]["transaction_count"], 7)
        (self.archive / "trace.json").unlink()
        self.assertEqual(get_run_summary(self.archive, self.case_id), (None, "unavailable"))

    def test_failed_bootstrap_stays_unavailable_until_source_changes(self):
        self.write({**self.state, "case_id": "wrong"})
        get_run_summary(self.archive, self.case_id)
        self.assertEqual(self.finish(), (None, "unavailable"))
        with patch("liquid_tracer.run_summaries._read_summary_fields", side_effect=AssertionError("repeat failure")):
            self.assertEqual(get_run_summary(self.archive, self.case_id), (None, "unavailable"))
        self.write()
        get_run_summary(self.archive, self.case_id)
        self.assertEqual(self.finish()[1], "ready")

    def test_source_mutation_during_bootstrap_never_returns_stale_ready(self):
        original = run_summaries._read_summary_fields
        def changing(path):
            state = original(path)
            self.state["stats"]["transactions_cumulative"] = 11
            self.write()
            return state
        with patch("liquid_tracer.run_summaries._read_summary_fields", side_effect=changing):
            get_run_summary(self.archive, self.case_id)
            run_summaries._queue.join()
        self.assertEqual(get_run_summary(self.archive, self.case_id, schedule=False), (None, "loading"))
        get_run_summary(self.archive, self.case_id)
        self.assertEqual(self.finish()[0]["transaction_count"], 11)

    def test_symlinks_are_rejected_without_trace_read(self):
        trace = self.archive / "trace.json"
        actual = self.root / "outside.json"
        trace.replace(actual)
        trace.symlink_to(actual)
        self.assertEqual(get_run_summary(self.archive, self.case_id), (None, "unavailable"))
        self.assertFalse(remember_run_summary(self.archive, self.state))
        trace.unlink()
        actual.replace(trace)
        cache_dir = self.archive.parent.parent / "run-summaries"
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        cache_dir.symlink_to(elsewhere, target_is_directory=True)
        self.assertEqual(get_run_summary(self.archive, self.case_id), (None, "unavailable"))

    def test_streaming_compact_and_pretty_match_in_memory_named_hops(self):
        state = deepcopy(self.state)
        state.update(hop_reference_name=" Treasury ", source="fixture://snowman-\u2603")
        state["transactions"] = {"first": {"depth": 7, "reference_hops": 1},
                                 "last": {"depth": 9, "reference_hops": 3},
                                 "boundary": {"depth": 10, "reference_hops": None}}
        state["outputs"] = {"unused": [True, False, None, 1.2e-10, {"escaped": '\\"'}]}
        for indent in (None, 2):
            with self.subTest(indent=indent):
                (self.archive / "trace.json").write_text(json.dumps(state, indent=indent))
                binding = run_summaries._binding(self.archive, self.case_id)
                streamed = run_summaries._read_summary_fields(self.archive / "trace.json")
                self.assertEqual(run_summaries._summary(streamed, binding), run_summaries._summary(state, binding))
                self.assertEqual(run_summaries._summary(streamed, binding)["collected_hops"], 3)

    def test_large_unused_json_is_skipped_with_bounded_memory(self):
        state = deepcopy(self.state)
        state["outputs"] = {"unused": {"payload": "x" * (8 * 1024 * 1024)}}
        state["shared_collection"]["service_controls"] = {"large": "y" * (5 * 1024 * 1024)}
        state["transactions"]["last"]["data"] = {"payload": '\\"' * (1024 * 1024)}
        (self.archive / "trace.json").write_text(json.dumps(state, separators=(",", ":")))
        del state
        tracemalloc.start()
        try:
            fields = run_summaries._read_summary_fields(self.archive / "trace.json")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 2 * 1024 * 1024)
        self.assertEqual(fields["_summary_depths"]["count"], 2)
        self.assertNotIn("outputs", fields)
        self.assertNotIn("transactions", fields)
        self.assertNotIn("service_controls", fields["shared_collection"])

    def test_invalid_json_in_skipped_fields_is_rejected(self):
        for bad in ('{"outputs":{"bad":falsee}}', '{"outputs":[1,]}', '{"outputs":"bad\\q"}',
                    '{"outputs":{"nested":[1,2}}}', '{"outputs":1} trailing'):
            with self.subTest(bad=bad):
                (self.archive / "trace.json").write_text(bad)
                with self.assertRaises((ValueError, json.JSONDecodeError)):
                    run_summaries._read_summary_fields(self.archive / "trace.json")

    def test_transient_read_error_is_retryable_without_modifying_source(self):
        with patch("liquid_tracer.run_summaries._read_summary_fields", side_effect=OSError("temporarily unavailable")):
            get_run_summary(self.archive, self.case_id)
            self.assertEqual(self.finish(), (None, "loading"))
        get_run_summary(self.archive, self.case_id)
        self.assertEqual(self.finish()[1], "ready")

    def test_background_queue_is_bounded_across_different_archives(self):
        started, release = threading.Event(), threading.Event()
        reader = run_summaries._read_summary_fields
        calls = []
        def slow(path):
            calls.append(path)
            started.set()
            if not release.wait(5):
                raise AssertionError("test did not release reader")
            return reader(path)
        with patch("liquid_tracer.run_summaries._read_summary_fields", side_effect=slow):
            try:
                get_run_summary(self.archive, self.case_id)
                self.assertTrue(started.wait(5))
                archives = []
                for number in range(run_summaries._QUEUE_LIMIT + 3):
                    archive = self.archive.parent / f"{number:016x}"
                    archive.mkdir()
                    save_json(archive / "trace.json", {"case_id": self.case_id, "run_id": archive.name})
                    (archive / "SHA256SUMS").write_text("synthetic seal")
                    self.assertEqual(get_run_summary(archive, self.case_id), (None, "loading"))
                    archives.append(archive)
                self.assertEqual(len(calls), 1)
                self.assertEqual(run_summaries._queue.qsize(), run_summaries._QUEUE_LIMIT)
                get_run_summary(archives[-1], self.case_id, priority=True)
                self.assertEqual(run_summaries._queue.qsize(), run_summaries._QUEUE_LIMIT)
            finally:
                release.set()
                run_summaries._queue.join()
        self.assertEqual(len(calls), run_summaries._QUEUE_LIMIT + 1)
        self.assertEqual(calls[1].parent, archives[-1])
        self.assertNotIn(archives[run_summaries._QUEUE_LIMIT - 1] / "trace.json", calls)

    def test_priority_promotes_selected_summary_without_duplicate_reads(self):
        started, release = threading.Event(), threading.Event()
        reader = run_summaries._read_summary_fields
        order = []
        def slow(path):
            order.append(Path(path).parent.name)
            started.set()
            if not release.wait(5):
                raise AssertionError("test did not release reader")
            return reader(path)
        archives = []
        for number in range(3):
            archive = self.archive.parent / f"{number:016x}"
            archive.mkdir()
            save_json(archive / "trace.json", {"case_id": self.case_id, "run_id": archive.name})
            (archive / "SHA256SUMS").write_text("synthetic seal")
            archives.append(archive)
        with patch("liquid_tracer.run_summaries._read_summary_fields", side_effect=slow):
            try:
                get_run_summary(self.archive, self.case_id)
                self.assertTrue(started.wait(5))
                for archive in archives:
                    get_run_summary(archive, self.case_id)
                get_run_summary(archives[-1], self.case_id, priority=True)
                self.assertEqual(run_summaries._queue.qsize(), 3)
            finally:
                release.set()
                run_summaries._queue.join()
        self.assertEqual(order, [self.archive.name, archives[-1].name, archives[0].name, archives[1].name])

    def test_summary_cache_failure_never_turns_completed_archive_into_job_failure(self):
        with patch("liquid_tracer.run_summaries._save", side_effect=OSError("read-only cache")):
            self.assertFalse(remember_run_summary(self.archive, self.state))
        self.assertEqual(read_json(self.archive / "trace.json"), self.state)
