"""Changing investigation roots preserves evidence and pins future work correctly."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from liquid_tracer.api import ENTERPRISE
from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.investigations import create_investigation, read_case
from liquid_tracer.menu import _trace_arguments
from liquid_tracer.seed_settings import SeedEditConflict, normalize_seeds, save_seeds, seed_settings
from tests import test_web
from tests.test_attribution_convergence import graph_state, tx
from tests.test_connections import saved_case


class SeedSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.old, self.new = tx("a") + ":0", tx("b") + ":1"
        self.case = create_investigation(self.root, "Editable", seeds=[self.old])

    def test_canonical_duplicates_and_invalid_input(self):
        self.assertEqual(normalize_seeds([self.new.upper(), self.old, tx("b") + ":01"]), [self.old, self.new])
        self.assertEqual(normalize_seeds([self.old] * 1001), [self.old])
        for value in ([], "a", [1], ["a"], [tx("a") + ":-1"], [tx("a") + ":4294967296"]):
            with self.subTest(value=str(value)[:30]), self.assertRaises(TraceError):
                normalize_seeds(value)

    def test_updates_only_metadata_and_preserves_history_and_latest(self):
        archive = self.case / "runs" / "0123456789abcdef"
        archive.mkdir(parents=True)
        (archive / "trace.json").write_text("immutable evidence")
        preview = self.case / "previews" / "saved"
        preview.mkdir(parents=True)
        (preview / "graph.html").write_text("immutable graph")
        metadata = read_case(self.case)
        metadata.update(latest_run=archive.name, miro_board="BOARD=")
        save_json(self.case / "case.json", metadata)
        self.assertEqual(save_seeds(self.case, [self.new], expected_revision=0), {"seeds": [self.new], "revision": 1})
        changed = read_case(self.case)
        self.assertEqual(changed, {**metadata, "seeds": [self.new], "seed_revision": 1})
        self.assertEqual((archive / "trace.json").read_text(), "immutable evidence")
        self.assertEqual((preview / "graph.html").read_text(), "immutable graph")

    def test_optimistic_concurrency_and_noop(self):
        self.assertEqual(seed_settings(self.case), {"seeds": [self.old], "revision": 0})
        self.assertEqual(save_seeds(self.case, [self.old.upper()], expected_revision=0)["revision"], 0)
        save_seeds(self.case, [self.new], expected_revision=0)
        before = (self.case / "case.json").read_bytes()
        with self.assertRaises(SeedEditConflict):
            save_seeds(self.case, [self.old], expected_revision=0)
        for value in (True, -1, "1", 2 ** 53):
            with self.subTest(value=value), self.assertRaises(TraceError):
                save_seeds(self.case, [self.old], expected_revision=value)
        self.assertEqual((self.case / "case.json").read_bytes(), before)

    def test_revision_limit_does_not_write_an_unreadable_revision(self):
        metadata = read_case(self.case)
        metadata["seed_revision"] = 2 ** 53 - 1
        save_json(self.case / "case.json", metadata)
        self.assertEqual(seed_settings(self.case)["revision"], 2 ** 53 - 1)
        with self.assertRaisesRegex(TraceError, "revision limit"):
            save_seeds(self.case, [self.new], expected_revision=2 ** 53 - 1)
        self.assertEqual(read_case(self.case), metadata)

    def test_external_collection_or_metadata_writer_blocks_edit(self):
        shared = self.root / ".shared-collection"
        shared.mkdir()
        for target in (self.case / "trace.lock", self.case / "case.lock", shared / "trace.lock", self.root / ".shared-collection.lock"):
            with self.subTest(target=target.name), target.open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(SeedEditConflict):
                    save_seeds(self.case, [self.new], expected_revision=0)
        self.assertEqual(seed_settings(self.case)["seeds"], [self.old])

    def test_changed_seeds_start_fresh_and_matching_seeds_resume(self):
        parent = {"source": ENTERPRISE, "seeds": [self.old]}
        metadata = {**read_case(self.case), "latest_run": "0123456789abcdef"}
        with patch("liquid_tracer.menu._latest", return_value=(self.case, parent)):
            resumed, live = _trace_arguments(self.case, metadata, {"hops": 7})
            fresh, _ = _trace_arguments(self.case, {**metadata, "seeds": [self.new]}, {"hops": 7})
        self.assertTrue(live)
        self.assertIn("--resume", resumed)
        self.assertEqual(resumed[resumed.index("--additional-hops") + 1], "7")
        self.assertNotIn("--resume", fresh)
        self.assertEqual(fresh[fresh.index("--seed") + 1], self.new)
        self.assertEqual(fresh[fresh.index("--hops") + 1], "7")

    def test_saved_preview_stays_reviewable_and_keeps_original_seeds(self):
        from liquid_tracer.plots import preview_plot, reviewed_plot
        from liquid_tracer.workflow_api import plot_summary, selected_plot
        saved_case(self.case, graph_state((("a:0", "b"),), seeds=("a:0",)))
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kw: graph):
            result = preview_plot(self.case, "full")
            archive = Path(result["directory"])
            original = {p.name: p.read_bytes() for p in archive.iterdir() if p.is_file()}
            save_seeds(self.case, [self.new], expected_revision=0)
            graph, _ = reviewed_plot(self.case, result["preview_id"])
            self.assertEqual(graph["plot"]["seeds"], [self.old])
            self.assertEqual(plot_summary(self.case, result["preview_id"])["seeds"], [self.old])
            self.assertEqual(selected_plot(self.case, result["preview_id"])["seeds"], [self.old])
            # An explicit old private snapshot remains an old-root graph.
            second = preview_plot(self.case, "full")
            self.assertEqual(second["seeds"], [self.old])
            self.assertEqual(original, {p.name: p.read_bytes() for p in archive.iterdir() if p.is_file()})

    def test_legacy_preview_without_seed_metadata_survives_edit(self):
        from liquid_tracer.plots import preview_plot, reviewed_plot
        from liquid_tracer.workflow_api import plot_summary
        from tests.test_plots import PlotTests
        saved_case(self.case, graph_state((("a:0", "b"),), seeds=("a:0",)))
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kw: graph):
            result = preview_plot(self.case, "full")
        directory = Path(result["directory"])
        graph = read_json(directory / "graph.json")
        graph["plot"].pop("seeds")
        save_json(directory / "graph.json", graph)
        save_json(directory / "plot.json", graph["plot"])
        PlotTests.rehash_preview(self, directory)
        original = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}

        save_seeds(self.case, [self.new], expected_revision=0)
        reviewed, _ = reviewed_plot(self.case, result["preview_id"])
        self.assertEqual(reviewed["run"]["seeds"], [self.old])
        self.assertNotIn("seeds", plot_summary(self.case, result["preview_id"]))
        self.assertEqual(original, {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()})

    def test_resealed_seed_provenance_must_match_original_archive(self):
        from liquid_tracer.plots import _snapshot, preview_plot, reviewed_plot
        from tests.test_plots import PlotTests
        saved_case(self.case, graph_state((("a:0", "b"),), seeds=("a:0",)))
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kw: graph):
            result = preview_plot(self.case, "full")
        directory = Path(result["directory"])
        graph = read_json(directory / "graph.json")
        graph["plot"]["seeds"] = [self.new]
        save_json(directory / "graph.json", graph)
        save_json(directory / "plot.json", graph["plot"])
        PlotTests.rehash_preview(self, directory)
        # The internally consistent manifest is insufficient: the original
        # evidence must still prove the seed provenance, even after an edit.
        _snapshot(self.case, result["preview_id"])
        save_seeds(self.case, [self.new], expected_revision=0)
        with self.assertRaisesRegex(TraceError, "changed; regenerate"):
            reviewed_plot(self.case, result["preview_id"])


class SeedSettingsHTTPTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success

    def prepare(self):
        case = create_investigation(self.server.root, "Editable", seeds=[tx("a") + ":0"])
        identity = read_case(case)["case_id"]
        return case, identity, "/api/cases/" + identity

    def test_round_trip_security_and_unknown_fields(self):
        case, _, route = self.prepare()
        self.assertEqual(self.success(route + "/seeds"), {"seeds": [tx("a") + ":0"], "revision": 0})
        body = {"seeds": [tx("b").upper() + ":01"], "expected_revision": 0}
        for headers in ({"Origin": "https://attacker.invalid"}, {"X-Liquid-CSRF": "wrong"}):
            self.assertEqual(self.request(route + "/seeds", body, headers=headers)[0], 403)
        self.assertEqual(self.request(route + "/seeds", {**body, "path": "/tmp"})[0], 400)
        self.assertEqual(self.success(route + "/seeds", body), {"seeds": [tx("b") + ":1"], "revision": 1})
        self.assertEqual(self.request(route + "/seeds", body)[0], 409)
        self.assertEqual(read_case(case)["seeds"], [tx("b") + ":1"])

    def test_active_case_and_shared_jobs_reject_but_unrelated_jobs_do_not(self):
        _, identity, route = self.prepare()
        body = {"seeds": [tx("b") + ":1"], "expected_revision": 0}
        for kind, owner in (("plot", identity), ("board", identity), ("collection", identity), ("shared_collection", "f" * 32)):
            with self.subTest(kind=kind):
                self.server.jobs["active"] = {"id": "active", "status": "running", "action": kind,
                                              "resource_kind": kind, "case_id": owner}
                self.assertEqual(self.request(route + "/seeds", body)[0], 409)
        self.server.jobs["active"] = {"id": "active", "status": "running", "action": "plot",
                                      "resource_kind": "plot", "case_id": "f" * 32}
        self.assertEqual(self.request(route + "/seeds", body)[0], 200)

    def test_slow_save_does_not_block_jobs_but_rejects_conflicting_admission(self):
        case, identity, route = self.prepare()
        other = create_investigation(self.server.root, "Other", seeds=[tx("c") + ":0"])
        other_id = read_case(other)["case_id"]
        run = case / "runs" / "0123456789abcdef"
        run.mkdir(parents=True)
        entered, release = threading.Event(), threading.Event()
        actual = save_json
        def slow(path, value):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test failed to release seed save")
            actual(path, value)
        with patch("liquid_tracer.seed_settings.save_json", side_effect=slow), ThreadPoolExecutor() as workers:
            pending = workers.submit(self.request, route + "/seeds", {"seeds": [tx("b") + ":1"], "expected_revision": 0})
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.request("/api/jobs")[0], 200)
                self.assertEqual(self.request(route + "/seeds", {"seeds": [tx("c") + ":0"], "expected_revision": 0})[0], 409)
                self.assertEqual(self.request(route + "/actions", {"action": "plot", "goal": "full", "run_id": run.name,
                                                                      "min_hops": 0, "max_hops": 10})[0], 409)
                self.assertEqual(self.request("/api/cases/" + other_id + "/actions", {"action": "shared-trace", "mode": "collect",
                                                                                     "hops": 10, "case_ids": [other_id]})[0], 409)
                self.assertEqual(self.server.jobs, {})
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=3)[0], 200)
        self.assertEqual(self.server.pending_seed_edits, set())

    def test_failed_save_releases_admission_reservation(self):
        _, _, route = self.prepare()
        body = {"seeds": [], "expected_revision": 0}
        self.assertEqual(self.request(route + "/seeds", body)[0], 400)
        self.assertEqual(self.server.pending_seed_edits, set())
        self.assertEqual(self.request(route + "/seeds", {**body, "seeds": [tx("b") + ":0"]})[0], 200)


class SeedSharedProjectionTests(unittest.TestCase):
    def test_new_shared_preview_uses_new_roots_and_combined_export_skips_old_roots(self):
        from tests.test_shared_projection import SharedProjectionTests
        from liquid_tracer.plots import preview_plot, reviewed_plot
        from liquid_tracer.combined_endpoint_csv import build_combined_endpoint_csv
        from liquid_tracer.plot_csv import build_plot_csv
        fixture = SharedProjectionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.collect()
        identity = read_case(fixture.case)["case_id"]
        args = {"data_source": "shared", "dataset_id": fixture.shared_id, "run_id": fixture.shared_run}
        original = preview_plot(fixture.case, "pegouts", **args)
        source_bytes = fixture.files(fixture.source_archive)
        selection = [{"case_id": identity, "data_source": "shared", "dataset_id": fixture.shared_id, "run_id": fixture.shared_run}]
        resolve = lambda _identity: (fixture.case, read_case(fixture.case))
        self.assertEqual(len(build_combined_endpoint_csv(selection, resolve)["included"]), 1)
        save_seeds(fixture.case, [tx("a") + ":1"], expected_revision=0)
        skipped = build_combined_endpoint_csv(selection, resolve)
        self.assertEqual(skipped["included"], [])
        self.assertIn("different starting outputs", skipped["skipped"][0]["reason"])
        # Explicit historical download/review remains valid and unchanged.
        reviewed_plot(fixture.case, original["preview_id"])
        self.assertTrue(build_plot_csv(fixture.case, original["preview_id"], "endpoints.csv")["data"])
        newer = preview_plot(fixture.case, "pegouts", **args)
        self.assertEqual(newer["seeds"], [tx("a") + ":1"])
        product = build_combined_endpoint_csv(selection, resolve)
        self.assertEqual(product["included"][0]["plot_id"], newer["preview_id"])
        self.assertEqual(source_bytes, fixture.files(fixture.source_archive))
