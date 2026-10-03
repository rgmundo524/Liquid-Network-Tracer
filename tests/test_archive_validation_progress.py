"""Archive validation stays authoritative while reporting bounded read progress."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.cli import verify_export
from liquid_tracer.common import TraceError
from liquid_tracer.progress import public_progress


PRIVATE = "SYNTHETIC-PRIVATE-ARCHIVE-NAME"


class ArchiveValidationProgressTests(unittest.TestCase):
    def setUp(self):
        self.temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.archive = Path(self.temporary) / PRIVATE
        self.archive.mkdir()
        self.payloads = {
            "trace.json": b'{"transactions": {}, "padding": "' + b"x" * 100000 + b'"}',
            "graph.json": b'{"nodes": [], "edges": []}',
            "miro-plan.json": b'{"items": []}',
            "responses/page.json": b'{"result": []}',
        }
        for name, payload in self.payloads.items():
            path = self.archive / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        self.lines = [hashlib.sha256(payload).hexdigest() + "  " + name
                      for name, payload in self.payloads.items()]
        self.manifest = self.archive / "SHA256SUMS"
        self.write_manifest(self.lines)

    def write_manifest(self, lines):
        self.manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_byte_progress_covers_all_files_and_stays_public(self):
        events = []
        self.assertIsNone(verify_export(self.archive, progress=events.append))
        total = sum(map(len, self.payloads.values()))
        self.assertTrue(events)
        self.assertEqual(events[0]["completed"], 0)
        self.assertEqual(events[-1]["completed"], total)
        completed = [event["completed"] for event in events]
        self.assertEqual(completed, sorted(completed))
        self.assertTrue(any(0 < count < total for count in completed))
        for event in events:
            self.assertEqual(event["phase"], "verifying_files")
            self.assertEqual(event["total"], total)
            self.assertEqual(public_progress(event), event)
        self.assertNotIn(PRIVATE, json.dumps(events))
        self.assertNotIn("trace.json", json.dumps(events))
        # Existing callers remain compatible without an observer.
        self.assertIsNone(verify_export(self.archive))

    def test_payloads_use_bounded_streaming_reads_including_short_reads(self):
        opened, requested, consumed = set(), [], []
        original_open = Path.open
        paths = {self.archive / name for name in self.payloads}
        test = self

        class GuardedReader:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def read(self, size=-1):
                test.assertGreater(size, 0, "archive reads must have a positive bound")
                test.assertLessEqual(size, 8 * 1024 * 1024, "archive reads must stay bounded")
                requested.append(size)
                data = self.stream.read(min(size, 4096))
                consumed.append(len(data))
                return data

            def readinto(self, buffer):
                test.assertGreater(len(buffer), 0)
                test.assertLessEqual(len(buffer), 8 * 1024 * 1024)
                requested.append(len(buffer))
                count = self.stream.readinto(memoryview(buffer)[:4096])
                consumed.append(count)
                return count

        def open_guard(path, mode="r", *args, **kwargs):
            stream = original_open(path, mode, *args, **kwargs)
            if path in paths and "b" in mode:
                opened.add(path)
                return GuardedReader(stream)
            return stream

        with patch.object(Path, "open", open_guard), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read is forbidden")):
            verify_export(self.archive)
        self.assertEqual(opened, paths)
        self.assertGreater(len(requested), len(paths))
        self.assertEqual(sum(consumed), sum(map(len, self.payloads.values())))

    def test_missing_and_malformed_manifests_still_fail(self):
        self.manifest.unlink()
        with self.assertRaisesRegex(TraceError, "no SHA256SUMS"):
            verify_export(self.archive, progress=lambda event: None)
        for line in ("", "not a checksum", "f" * 63 + "  trace.json",
                     "g" * 64 + "  trace.json", "f" * 64 + " trace.json"):
            with self.subTest(line=line):
                self.write_manifest([line])
                with self.assertRaisesRegex(TraceError, "Malformed saved-run checksum manifest"):
                    verify_export(self.archive, progress=lambda event: None)

    def test_missing_duplicate_and_escaping_entries_still_fail(self):
        checksum = hashlib.sha256(b"outside").hexdigest()
        (self.archive.parent / "outside.json").write_bytes(b"outside")
        bad_entries = [
            [*self.lines, self.lines[0]],
            [*self.lines, checksum + "  missing.json"],
            [*self.lines, checksum + "  ../outside.json"],
        ]
        for lines in bad_entries:
            with self.subTest(lines=lines):
                self.write_manifest(lines)
                with self.assertRaisesRegex(TraceError, "Missing, duplicated, or invalid file"):
                    verify_export(self.archive, progress=lambda event: None)
        self.write_manifest(self.lines[1:])
        with self.assertRaisesRegex(TraceError, "missing required trace or graph files"):
            verify_export(self.archive, progress=lambda event: None)

    def test_tampered_bytes_still_fail_with_progress(self):
        (self.archive / "graph.json").write_bytes(b'{"nodes": [1], "edges": []}')
        events = []
        with self.assertRaisesRegex(TraceError, "checksum mismatch: graph.json"):
            verify_export(self.archive, progress=events.append)
        self.assertTrue(events)

    def test_keyboard_interrupt_from_observer_is_not_swallowed(self):
        events = []

        def cancel(event):
            events.append(event)
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            verify_export(self.archive, progress=cancel)
        self.assertEqual(len(events), 1)
        # Cancellation does not alter evidence or invalidate a later check.
        verify_export(self.archive)

    def test_broken_observer_does_not_skip_integrity_checks(self):
        called = []

        def broken(event):
            called.append(event)
            raise RuntimeError("observer unavailable")

        self.assertIsNone(verify_export(self.archive, progress=broken))
        self.assertTrue(called)
        (self.archive / "trace.json").write_bytes(b"tampered")
        with self.assertRaisesRegex(TraceError, "checksum mismatch: trace.json"):
            verify_export(self.archive, progress=broken)

    def test_preparation_phases_are_allowlisted_without_private_text(self):
        for phase in ("verifying_files", "loading_collection"):
            with self.subTest(phase=phase):
                event = public_progress({"phase": phase, "completed": 0, "total": 100,
                                         "message": PRIVATE, "path": PRIVATE, "exception": PRIVATE})
                self.assertIsNotNone(event)
                self.assertEqual((event["phase"], event["completed"], event["total"]), (phase, 0, 100))
                self.assertTrue(event["message"])
                self.assertNotIn(PRIVATE, json.dumps(event))
                self.assertNotIn("path", event)
                self.assertNotIn("exception", event)


if __name__ == "__main__":
    unittest.main()
