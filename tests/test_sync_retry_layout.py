"""Ordinary live sync checkpoints layout before sending publication requests."""

import contextlib
import functools
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import main, sync_run, verify_export
from liquid_tracer.common import TraceError, canonical, digest, read_json, save_json
from liquid_tracer.elk_layout import optimize_graph
from liquid_tracer.miro import sync as real_sync
from liquid_tracer.services import set_service
from tests.fixtures import A, fixture
from tests.test_elk_layout import HAS_ELK
from tests.test_miro_sync import FakeMiro


class RejectUpdateMiro(FakeMiro):
    reject_updates = False

    def __call__(self, method, url, headers, body, timeout):
        if method == "PATCH" and self.reject_updates:
            return 400, {}, canonical({"code": "badRequest", "message": "Synthetic invalid update"})
        return super().__call__(method, url, headers, body, timeout)


@unittest.skipUnless(HAS_ELK, "Run liquid-layout-setup to install the pinned local ELK engine")
class SyncRetryLayoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = self.root / "case"
        self.fixture = self.root / "fixture.json"
        save_json(self.fixture, fixture())
        self.run_id = self.trace("--seed", A + ":0", "--hops", "1")
        self.remote = RejectUpdateMiro()
        adapter = functools.partial(real_sync, token="SYNTHETIC-token", transport=self.remote, interval=0)
        self.patcher = patch("liquid_tracer.cli.sync", adapter)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.archive_before = self.snapshot(self.case / "runs")

    @staticmethod
    def snapshot(directory):
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}

    def trace(self, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            status = main(["trace", "--case", str(self.case), "--fixture", str(self.fixture), *arguments])
        self.assertEqual(status, 0, output.getvalue())
        return json.loads(output.getvalue())["run_id"]

    def sync(self, **options):
        return sync_run(self.case, options.pop("run_id", "latest"), "SYNTHETIC=", reorganize=True,
                        **{"layout_attempts": 2, **options})

    def test_rejected_patch_retry_reuses_exact_completed_layout_and_preserves_ids(self):
        with patch("liquid_tracer.elk_layout.optimize_graph", wraps=optimize_graph) as worker:
            self.sync()
            self.remote.reject_updates = True
            with self.assertRaisesRegex(TraceError, "400"):
                self.sync(connector_style="elbowed")
            self.assertEqual(worker.call_count, 2)
            ids = set(self.remote.items)
            completed = sorted((self.case / "previews").glob("*-elk-*"))
            self.assertEqual(len(completed), 2)
            self.assertTrue(all((path / "graph.html").is_file() for path in completed))
            self.remote.reject_updates = False
            events = []
            result = self.sync(connector_style="elbowed", progress=events.append)
            self.assertEqual(worker.call_count, 2)
        self.assertIn("reusing_layout", [event["phase"] for event in events])
        self.assertEqual(result["created"], 0)
        self.assertEqual(ids, set(self.remote.items))
        self.assertEqual(self.snapshot(self.case / "runs"), self.archive_before)
        verify_export(self.case / "runs" / self.run_id)

    def test_current_settings_invalidate_automatically_saved_layout(self):
        with patch("liquid_tracer.elk_layout.optimize_graph", wraps=optimize_graph) as worker:
            self.sync()
            for index, options in enumerate(({"layout_attempts": 3}, {"connector_style": "elbowed"},
                                            {"include_fees": True}, {"group_context_inputs": True}), 2):
                with self.subTest(options=options):
                    self.sync(**options)
                    self.assertEqual(worker.call_count, index)
                    self.sync(**options)
                    self.assertEqual(worker.call_count, index)

    def test_changed_attribution_and_address_count_recalculate(self):
        with patch("liquid_tracer.elk_layout.optimize_graph", wraps=optimize_graph) as worker:
            self.sync()
            set_service(self.case, "SYNTHETIC-branch-A", name="Updated review", confidence="confirmed",
                        stop_tracing=False, source="Synthetic review")
            self.sync()
            self.assertEqual(worker.call_count, 2)
            self.sync()
            self.assertEqual(worker.call_count, 2)
            trace = read_json(self.case / "runs" / self.run_id / "trace.json")
            address = "SYNTHETIC-branch-A"
            counts = {"schema_version": 1, "case_id": trace["case_id"], "source": trace["source"],
                      "counts": {address: {"source": trace["source"], "address": address,
                                           "observed_at": "2026-09-20T00:00:00+00:00",
                                           "confirmed_tx_count": 123, "mempool_tx_count": 0}}}
            counts["sha256"] = digest(canonical(counts))
            save_json(self.case / "address-counts.json", counts)
            self.sync()
            self.assertEqual(worker.call_count, 3)
            self.sync()
            self.assertEqual(worker.call_count, 3)
        self.assertEqual(self.snapshot(self.case / "runs"), self.archive_before)

    def test_later_saved_graph_cannot_reuse_prior_run_layout(self):
        with patch("liquid_tracer.elk_layout.optimize_graph", wraps=optimize_graph) as worker:
            first = self.sync()
            following = self.trace("--resume", self.run_id, "--additional-hops", "1")
            second = self.sync(run_id=following)
            self.assertEqual(worker.call_count, 2)
            self.assertNotEqual(first["run_id"], second["run_id"])
            self.assertGreater(second["created"], 0)
            self.sync(run_id=following)
            self.assertEqual(worker.call_count, 2)

    def test_dry_run_does_not_save_layout_or_contact_miro(self):
        before = self.snapshot(self.case)
        self.sync(dry_run=True)
        self.assertEqual(self.snapshot(self.case), before)
        self.assertEqual(self.remote.calls, [])

    def test_checkpoint_failure_stops_before_live_miro_requests(self):
        with patch("liquid_tracer.layout_preview.export_layout", side_effect=TraceError("Cannot save preview")):
            with self.assertRaisesRegex(TraceError, "Cannot save preview"):
                self.sync()
        self.assertEqual(self.remote.calls, [])
        self.assertEqual(self.snapshot(self.case / "runs"), self.archive_before)
