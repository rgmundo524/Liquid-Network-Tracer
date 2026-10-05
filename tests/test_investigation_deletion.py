"""Investigation deletion is confined, explicit, and safe around active work."""

import fcntl
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import StopRun, TraceError, read_json, save_json
from liquid_tracer.investigation_deletion import delete_investigation, operation_guard
from liquid_tracer.investigations import create_investigation, list_investigations, read_case


class InvestigationDeletionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.root = self.base / "cases"
        self.case = create_investigation(self.root, "Theft investigation", board="retained-miro-board")
        self.metadata = read_case(self.case)

    def delete(self, **overrides):
        arguments = {"case_id": self.metadata["case_id"], "confirm_name": self.metadata["name"], **overrides}
        return delete_investigation(self.root, self.case, **arguments)

    def receipts(self):
        return list((self.root / ".deleted-investigations").glob("*/receipt.json"))

    def test_success_removes_all_case_files_and_preserves_shared_other_external_and_miro(self):
        other = create_investigation(self.root, "Other investigation")
        shared = self.root / ".shared-collection"
        save_json(shared / "case.json", {"case_id": "f" * 32, "name": "Shared", "shared_dataset": True})
        save_json(shared / "runs" / "run" / "trace.json", {"evidence": "shared"})
        external = self.base / "external-evidence"
        save_json(external / "evidence.json", {"original": True})
        (self.case / "shared-link").symlink_to(shared, target_is_directory=True)
        (self.case / "external-link").symlink_to(external, target_is_directory=True)
        (self.case / "broken-link").symlink_to(self.base / "missing")
        for name in ("runs/r/trace.json", "previews/p/graph.json", "reports/r/report.json", "services.json",
                     "miro/boards.json", "annotations.json", "evidence.sqlite"):
            path = self.case / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("private case data")
        retained = {path: path.read_bytes() for directory in (other, shared, external)
                    for path in directory.rglob("*") if path.is_file()}
        events = []
        with patch("liquid_tracer.api.http", side_effect=AssertionError("Must not contact Miro")):
            result = self.delete(progress=events.append)
        self.assertEqual(result, {"case_id": self.metadata["case_id"], "name": self.metadata["name"],
                                  "deleted": True, "cleanup_pending": False})
        self.assertFalse(self.case.exists())
        self.assertEqual({path: path.read_bytes() for path in retained}, retained)
        self.assertEqual([case for case, _ in list_investigations(self.root)], [other])
        self.assertEqual([event["completed"] for event in events], [0, 1])
        receipt, = self.receipts()
        self.assertEqual(read_json(receipt)["status"], "deleted")
        self.assertFalse((receipt.parent / "investigation").exists())
        self.assertNotIn(str(self.base), str(result))

    def test_confirmation_is_exact_and_rejects_missing_or_wrong_identity(self):
        for field, values in (("case_id", (None, "", "other", "0" * 32)),
                              ("confirm_name", (None, "", "theft investigation", "Theft investigation ", "Different"))):
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(TraceError):
                    self.delete(**{field: value})
                self.assertEqual(read_case(self.case), self.metadata)
        self.assertEqual(self.receipts(), [])

    def test_read_current_name_under_locks_and_recheck_before_move(self):
        def changed(_):
            save_json(self.case / "case.json", {**self.metadata, "name": "Renamed"})
        with self.assertRaisesRegex(TraceError, "changed"):
            self.delete(progress=changed)
        self.assertTrue(self.case.exists())
        self.assertEqual(read_case(self.case)["name"], "Renamed")

    def test_lifetime_guards_coexist_without_writes_and_block_deletion(self):
        before = {str(path): path.read_bytes() for path in self.case.rglob("*") if path.is_file()}
        with operation_guard(self.case), operation_guard(self.case):
            with self.assertRaisesRegex(TraceError, "operation is active"):
                self.delete()
            self.assertEqual({str(path): path.read_bytes() for path in self.case.rglob("*") if path.is_file()}, before)
        self.assertTrue(self.delete()["deleted"])

    def test_other_process_lifetime_guard_blocks_deletion_until_worker_finishes(self):
        worker = subprocess.Popen([sys.executable, "-c",
            "import sys; from liquid_tracer.investigation_deletion import operation_guard; "
            "guard = operation_guard(sys.argv[1]); guard.__enter__(); "
            "print('ready', flush=True); sys.stdin.readline(); guard.__exit__(None, None, None)", str(self.case)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(worker.stdout.readline().strip(), "ready")
            with self.assertRaisesRegex(TraceError, "operation is active"):
                self.delete()
            _, errors = worker.communicate("finish\n", timeout=10)
            self.assertEqual(worker.returncode, 0, errors)
            self.assertTrue(self.delete()["deleted"])
        finally:
            if worker.poll() is None:
                worker.kill()
            worker.communicate(timeout=10)

    def test_lifetime_guard_rejects_exclusive_deletion_lock_without_waiting(self):
        descriptor = os.open(self.case, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(TraceError, "deletion is active"), operation_guard(self.case):
                self.fail("Operation entered while deletion owns the investigation")
        finally:
            os.close(descriptor)

    def test_lifetime_guard_rechecks_identity_after_lock_when_case_moved(self):
        flock = fcntl.flock
        def moved(descriptor, operation):
            self.case.rename(self.root / "removed")
            flock(descriptor, operation)
        with patch("liquid_tracer.investigation_deletion.fcntl.flock", side_effect=moved):
            with self.assertRaisesRegex(TraceError, "deleted or replaced"), operation_guard(self.case):
                self.fail("An operation must not run on a removed investigation")

    def test_lifetime_guard_rechecks_identity_after_lock_when_case_replaced(self):
        flock = fcntl.flock
        def replaced(descriptor, operation):
            self.case.rename(self.root / "removed")
            self.case.mkdir()
            save_json(self.case / "case.json", self.metadata)
            flock(descriptor, operation)
        with patch("liquid_tracer.investigation_deletion.fcntl.flock", side_effect=replaced):
            with self.assertRaisesRegex(TraceError, "deleted or replaced"), operation_guard(self.case):
                self.fail("An operation must not run on a replaced investigation")

    def test_lifetime_guard_allows_initial_trace_and_none_without_creating_files(self):
        empty = self.root / "not-yet-initialized"
        empty.mkdir()
        missing = self.base / "missing-parent" / "new-investigation"
        for case in (None, empty, missing):
            with self.subTest(case=case), operation_guard(case):
                pass
        self.assertEqual(list(empty.iterdir()), [])
        self.assertFalse(missing.parent.exists())

    def test_busy_trace_and_case_locks_fail_without_waiting(self):
        for filename in ("trace.lock", "case.lock"):
            for lock_kind in (fcntl.LOCK_SH, fcntl.LOCK_EX):
                with self.subTest(filename=filename, lock_kind=lock_kind):
                    with (self.case / filename).open("a") as lock:
                        fcntl.flock(lock, lock_kind | fcntl.LOCK_NB)
                        with self.assertRaisesRegex(TraceError, "active"):
                            self.delete()
                    self.assertTrue(self.case.exists())
        self.assertEqual(self.receipts(), [])

    def test_shared_dataset_and_hidden_workspace_directories_cannot_be_deleted(self):
        shared = self.root / ".shared-collection"
        save_json(shared / "case.json", {**self.metadata, "shared_dataset": True})
        with self.assertRaises(TraceError):
            delete_investigation(self.root, shared, case_id=self.metadata["case_id"], confirm_name=self.metadata["name"])
        for marker in (True, False, "yes"):
            save_json(self.case / "case.json", {**self.metadata, "shared_dataset": marker})
            with self.assertRaisesRegex(TraceError, "shared collection"):
                self.delete()
            self.assertTrue(self.case.exists())
        self.assertTrue(shared.exists())

    def test_outside_nested_and_parent_traversal_are_rejected(self):
        outside = create_investigation(self.base / "outside", "Outside")
        nested = create_investigation(self.root / "nested", "Nested")
        for case in (outside, nested, self.root, self.root / ".." / "cases" / self.case.name):
            with self.subTest(case=case), self.assertRaises(TraceError):
                delete_investigation(self.root, case, case_id=self.metadata["case_id"], confirm_name=self.metadata["name"])
        self.assertTrue(self.case.exists())
        self.assertTrue(outside.exists())
        self.assertTrue(nested.exists())

    def test_root_case_metadata_and_lock_symlinks_are_rejected(self):
        root_link = self.base / "root-link"
        root_link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(TraceError):
            delete_investigation(root_link, root_link / self.case.name,
                                 case_id=self.metadata["case_id"], confirm_name=self.metadata["name"])
        case_link = self.root / "case-link"
        case_link.symlink_to(self.case, target_is_directory=True)
        with self.assertRaises(TraceError):
            delete_investigation(self.root, case_link,
                                 case_id=self.metadata["case_id"], confirm_name=self.metadata["name"])
        for filename in ("case.json", "trace.lock", "case.lock"):
            with self.subTest(filename=filename):
                path = self.case / filename
                target = self.base / (filename + ".original")
                if path.exists():
                    path.rename(target)
                else:
                    target.write_text("")
                original = target.read_bytes()
                path.symlink_to(target)
                try:
                    with self.assertRaises(TraceError):
                        self.delete()
                    self.assertEqual(target.read_bytes(), original)
                finally:
                    path.unlink()
                    target.rename(path)
        self.assertTrue(self.case.exists())

    def test_hardlinked_locks_and_metadata_are_rejected(self):
        for filename in ("case.json", "trace.lock", "case.lock"):
            with self.subTest(filename=filename):
                path = self.case / filename
                path.touch(exist_ok=True)
                alias = self.base / (filename + ".alias")
                os.link(path, alias)
                try:
                    with self.assertRaises(TraceError):
                        self.delete()
                finally:
                    alias.unlink()
        self.assertTrue(self.case.exists())

    def test_trash_symlink_cannot_redirect_deletion_or_receipts(self):
        external = self.base / "elsewhere"
        external.mkdir()
        (self.root / ".deleted-investigations").symlink_to(external, target_is_directory=True)
        with self.assertRaises(TraceError):
            self.delete()
        self.assertEqual(list(external.iterdir()), [])
        self.assertTrue(self.case.exists())

    def test_mount_and_bind_mount_subtrees_are_rejected_before_any_deletion(self):
        nested = self.case / "mounted"
        nested.mkdir()
        for mounted in (self.case, nested):
            with self.subTest(mounted=mounted), patch("liquid_tracer.investigation_deletion._mount_points", return_value={mounted}):
                with self.assertRaisesRegex(TraceError, "mounted"):
                    self.delete()
        self.assertTrue(nested.exists())
        self.assertEqual(self.receipts(), [])

    def test_mounted_deletion_directory_cannot_redirect_receipts(self):
        trash = self.root / ".deleted-investigations"
        trash.mkdir()
        with patch("liquid_tracer.investigation_deletion._mount_points", return_value={trash}):
            with self.assertRaisesRegex(TraceError, "filesystem"):
                self.delete()
        self.assertEqual(list(trash.iterdir()), [])
        self.assertTrue(self.case.exists())

    def test_interruption_after_atomic_move_reports_committed_deletion(self):
        rename = os.rename
        def interrupted(*args, **kwargs):
            rename(*args, **kwargs)
            raise KeyboardInterrupt()
        with patch("liquid_tracer.investigation_deletion.os.rename", side_effect=interrupted):
            result = self.delete()
        self.assertTrue(result["deleted"])
        self.assertTrue(result["cleanup_pending"])
        self.assertFalse(self.case.exists())
        receipt, = self.receipts()
        self.assertEqual(read_json(receipt.parent / "investigation" / "case.json"), self.metadata)

    def test_failed_atomic_move_leaves_original_investigation_intact(self):
        with patch("liquid_tracer.investigation_deletion.os.rename", side_effect=OSError("read only")):
            with self.assertRaises(TraceError):
                self.delete()
        self.assertEqual(read_case(self.case), self.metadata)

    def test_cleanup_failure_hides_partial_case_and_leaves_recoverable_receipt(self):
        save_json(self.case / "runs" / "r" / "trace.json", {"evidence": True})
        with patch("liquid_tracer.investigation_deletion._cleanup", side_effect=OSError("disk")):
            result = self.delete()
        self.assertTrue(result["deleted"])
        self.assertTrue(result["cleanup_pending"])
        self.assertFalse(self.case.exists())
        self.assertEqual(list_investigations(self.root), [])
        receipt, = self.receipts()
        self.assertEqual(read_json(receipt)["status"], "cleanup_pending")
        self.assertEqual(read_json(receipt.parent / "investigation" / "case.json"), self.metadata)
        self.assertEqual(read_json(receipt.parent / "investigation" / "runs" / "r" / "trace.json"), {"evidence": True})
        self.assertNotIn(str(self.base), str(result))

    def test_cancellation_before_commit_leaves_case_and_after_commit_reports_deletion(self):
        with self.assertRaises(StopRun):
            self.delete(progress=lambda _: (_ for _ in ()).throw(StopRun()))
        self.assertTrue(self.case.exists())
        with patch("liquid_tracer.investigation_deletion._cleanup", side_effect=KeyboardInterrupt()):
            result = self.delete()
        self.assertTrue(result["deleted"])
        self.assertTrue(result["cleanup_pending"])
        self.assertFalse(self.case.exists())

    def test_late_progress_cancellation_does_not_hide_success(self):
        def progress(event):
            if event["completed"]:
                raise StopRun()
        self.assertFalse(self.delete(progress=progress)["cleanup_pending"])
        self.assertFalse(self.case.exists())

    def test_changed_directory_does_not_delete_replacement(self):
        def changed(_):
            self.case.rename(self.root / "moved")
            self.case.mkdir()
            save_json(self.case / "case.json", self.metadata)
        with self.assertRaisesRegex(TraceError, "directory changed"):
            self.delete(progress=changed)
        self.assertTrue(self.case.exists())
        self.assertTrue((self.root / "moved").exists())


if __name__ == "__main__":
    unittest.main()
