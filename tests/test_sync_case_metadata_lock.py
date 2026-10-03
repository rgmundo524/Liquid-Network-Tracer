"""Legacy trace-and-sync saves its board link without reacquiring trace.lock."""

import fcntl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import sync_run
from liquid_tracer.common import TraceError
from liquid_tracer.investigations import create_investigation, read_case, update_case
from tests.test_connections import saved_case


class SyncCaseMetadataLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.case = create_investigation(Path(temporary.name), "Original name")
        self.state, self.archive = saved_case(self.case)

    def sync(self):
        return sync_run(self.case, self.state["run_id"], "SYNTHETIC=",
                        plan_path=self.archive / "miro-plan.json")

    def test_caller_trace_lock_remains_exclusive_through_board_link_and_live_sync(self):
        def publish(*args, **kwargs):
            if not kwargs["dry_run"]:
                self.assertEqual(read_case(self.case)["miro_board"], "SYNTHETIC=")
                with self.assertRaisesRegex(TraceError, "operation is active"):
                    update_case(self.case, {"run_defaults": {"hops": 9}})
                with (self.case / "trace.lock").open("a") as other:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return {"dry_run": kwargs["dry_run"]}

        with (self.case / "trace.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("liquid_tracer.cli.sync", side_effect=publish) as sync:
                self.sync()
                self.assertEqual(sync.call_count, 2)
        update_case(self.case, {"name": "After sync"})

    def test_busy_case_metadata_blocks_live_sync_without_changing_board(self):
        before = (self.case / "case.json").read_bytes()
        with (self.case / "case.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("liquid_tracer.cli.sync", return_value={"dry_run": True}) as sync:
                with self.assertRaisesRegex(TraceError, "settings are busy"):
                    self.sync()
                sync.assert_called_once()
                self.assertTrue(sync.call_args.kwargs["dry_run"])
        self.assertEqual((self.case / "case.json").read_bytes(), before)

    def test_board_link_merges_latest_metadata_after_local_preflight(self):
        def publish(*args, **kwargs):
            if kwargs["dry_run"]:
                update_case(self.case, {"name": "Concurrent rename",
                                        "run_defaults": {"hops": 9}})
            else:
                metadata = read_case(self.case)
                self.assertEqual(metadata["name"], "Concurrent rename")
                self.assertEqual(metadata["run_defaults"]["hops"], 9)
                self.assertEqual(metadata["miro_board"], "SYNTHETIC=")
            return {"dry_run": kwargs["dry_run"]}

        with patch("liquid_tracer.cli.sync", side_effect=publish):
            self.sync()


if __name__ == "__main__":
    unittest.main()
