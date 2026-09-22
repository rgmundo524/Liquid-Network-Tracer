"""Exact reviewed-plan application and durable compact preview provenance."""

import contextlib
import copy
import fcntl
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import (latest_compaction_preview, main, saved_graph, sync_run,
                               verified_compaction_preview, verify_export)
from liquid_tracer.common import TraceError, digest, read_json, save_json
from liquid_tracer.compaction import LINKED_HORIZONTAL
from liquid_tracer.compaction_preview import FILES, export_compaction, service_fingerprint
from liquid_tracer.investigations import create_investigation, read_case, update_case
from liquid_tracer.services import set_service
from tests.fixtures import A, fixture


def report():
    metrics = {"main": {"width": 1000., "height": 1000., "area": 1000000.,
                         "edge_length": 2000., "address_distance": 1500.},
               "board": {"width": 1400., "height": 1400., "area": 1960000.}}
    return {"algorithm": "local_address_components_v1", "version": 1,
            "before": copy.deepcopy(metrics), "after": copy.deepcopy(metrics),
            "moved_addresses": 0, "moved_components": 0, "accepted_moves": 0,
            "skipped_moves": 0, "truncated": False, "unchanged": True,
            "clearances": {"linked_horizontal": LINKED_HORIZONTAL, "node_node": 80, "components": 120,
                           "edge_node": 60, "edge_edge": 22},
            "labels_estimated": True, "miro_routes_exact": False}


class CompactionPreviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = self.root / "api.json"
        save_json(self.fixture, fixture())
        self.case = create_investigation(self.root / "cases", "Synthetic compact preview",
                                         fixture=self.fixture, seeds=[A + ":0"], board="SYNTHETIC-board")
        with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["trace", "--case", str(self.case), "--fixture", str(self.fixture),
                                   "--seed", A + ":0", "--hops", "1"]), 0)
        self.run = json.loads(stdout.getvalue())["run_id"]
        self.archive = self.case / "runs" / self.run
        self.snapshot = {p.name: p.read_bytes() for p in self.archive.iterdir() if p.is_file()}

    def preview(self, suffix="12345678"):
        _, _, graph = saved_graph(self.case, self.run)
        # These tests exercise storage/identity, not ELK geometry. Real ELK and
        # compaction are exercised by engine and HTTP workflow tests.
        graph["layout"]["algorithm"] = "elk_layered_v1"
        graph["graph_options"]["connector_style"] = "straight"
        before = copy.deepcopy(graph)
        graph["layout"]["compaction"] = report()
        graph["layout"]["metrics"] = {"before": {"crossings": 0, "node_overlaps": 0, "node_intersections": 0},
                                        "after": {"crossings": 0, "node_overlaps": 0, "node_intersections": 0}}
        result = export_compaction(before, graph, self.case / "previews" / (self.run + "-compact-" + suffix),
                                   archive_sha256=digest((self.archive / "SHA256SUMS").read_bytes()),
                                   service_sha256=service_fingerprint(self.case))
        return result["preview_id"], Path(result["directory"])

    def test_comparison_reopens_with_unique_svg_ids_and_unchanged_archive(self):
        identity, directory = self.preview()
        plan, meta = verified_compaction_preview(self.case, self.run, identity)
        self.assertEqual(plan["sha256"], meta["plan_sha256"])
        self.assertEqual(latest_compaction_preview(self.case), identity)
        document = (directory / "graph.html").read_text()
        self.assertIn("No safe size or proximity improvement", document)
        self.assertIn("Before is a fresh ELK layout", document)
        self.assertIn('href="before.html"', document)
        self.assertNotIn('id="before-arrow-traced"', document)
        self.assertIn('id="after-arrow-traced"', document)
        self.assertNotIn("<script", document)
        self.assertEqual(self.snapshot, {p.name: p.read_bytes() for p in self.archive.iterdir() if p.is_file()})
        verify_export(self.archive)

    def test_dry_run_uses_exact_reviewed_plan_without_rerunning_elk(self):
        identity, directory = self.preview()
        expected = read_json(directory / "miro-plan.json")
        with patch("liquid_tracer.cli.sync", return_value={"dry_run": True}) as sync, \
                patch("liquid_tracer.cli.refresh_presentation", side_effect=AssertionError("Layout changed")), \
                patch("liquid_tracer.elk_layout.optimize_graph", side_effect=AssertionError("ELK reran")):
            result = sync_run(self.case, self.run, dry_run=True, reorganize=True, compact_preview=identity)
        sync.assert_called_once()
        self.assertEqual(sync.call_args.args[0], expected)
        self.assertTrue(sync.call_args.kwargs["reorganize"])
        self.assertEqual(result["plan_sha256"], expected["sha256"])
        self.assertEqual(result["compact_preview"], identity)

    def test_apply_requires_reorganization_and_rejects_presentation_overrides(self):
        identity, _ = self.preview()
        for extra in ({}, {"reorganize": True, "include_fees": True},
                      {"reorganize": True, "connector_style": "curved"},
                      {"reorganize": True, "plan_path": self.archive / "miro-plan.json"}):
            with self.subTest(extra=extra), patch("liquid_tracer.cli.sync") as sync:
                with self.assertRaises(TraceError):
                    sync_run(self.case, self.run, compact_preview=identity, **extra)
                sync.assert_not_called()

    def test_changed_service_assessment_rejects_before_sync_and_discovery(self):
        identity, _ = self.preview()
        set_service(self.case, "SYNTHETIC-service-boundary", name="Candidate")
        with patch("liquid_tracer.cli.sync") as sync:
            with self.assertRaisesRegex(TraceError, "Service assessments changed"):
                sync_run(self.case, self.run, reorganize=True, compact_preview=identity)
            sync.assert_not_called()
        self.assertIsNone(latest_compaction_preview(self.case))

    def test_service_change_after_local_preflight_blocks_live_application(self):
        identity, _ = self.preview()

        def local_preflight(*args, **kwargs):
            self.assertTrue(kwargs["dry_run"])
            set_service(self.case, "SYNTHETIC-service-boundary", name="New assessment")
            return {"dry_run": True}

        with patch("liquid_tracer.cli.sync", side_effect=local_preflight) as sync, \
                patch("liquid_tracer.cli.update_case") as update:
            with self.assertRaisesRegex(TraceError, "Service assessments changed"):
                sync_run(self.case, self.run, reorganize=True, compact_preview=identity)
            sync.assert_called_once()
            update.assert_not_called()

    def test_service_decisions_cannot_change_during_apply_and_lock_releases_after_error(self):
        identity, _ = self.preview()

        def interrupted_apply(*args, **kwargs):
            if not kwargs["dry_run"]:
                with self.assertRaises(TraceError):
                    set_service(self.case, "SYNTHETIC-service-boundary", name="Concurrent decision")
                raise TraceError("Synthetic interrupted compact application")
            return {"dry_run": True}

        with patch("liquid_tracer.cli.sync", side_effect=interrupted_apply) as sync:
            with self.assertRaisesRegex(TraceError, "Synthetic interrupted"):
                sync_run(self.case, self.run, reorganize=True, compact_preview=identity)
            self.assertEqual(sync.call_count, 2)
        set_service(self.case, "SYNTHETIC-service-boundary", name="After application")

    def test_active_trace_blocks_compact_apply_before_board_selection_or_live_sync(self):
        identity, _ = self.preview()
        with (self.case / "trace.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("liquid_tracer.cli.sync", return_value={"dry_run": True}) as sync, \
                    patch("liquid_tracer.cli.update_case") as update:
                with self.assertRaisesRegex(TraceError, "active trace or address review"):
                    sync_run(self.case, self.run, reorganize=True, compact_preview=identity)
                sync.assert_called_once()
                self.assertTrue(sync.call_args.kwargs["dry_run"])
                update.assert_not_called()

    def test_changed_defaults_keep_frozen_preview_options(self):
        identity, _ = self.preview()
        settings = read_case(self.case)["run_defaults"]
        update_case(self.case, {"run_defaults": {**settings, "include_fees": True, "connector_style": "curved"}})
        _, meta = verified_compaction_preview(self.case, self.run, identity)
        self.assertFalse(meta["include_fees"])
        self.assertEqual(meta["connector_style"], "straight")

    def test_grouping_and_hub_changes_reject_compact_apply_and_discovery(self):
        identity, _ = self.preview()
        for selection in ({"group_context_inputs": True}, {"hub_addresses": ["G" + "a" * 33]}):
            with self.subTest(selection=selection):
                update_case(self.case, {"run_defaults": selection})
                with patch("liquid_tracer.cli.sync") as sync, self.assertRaisesRegex(TraceError, "branch hubs changed"):
                    sync_run(self.case, self.run, reorganize=True, compact_preview=identity)
                sync.assert_not_called()
                self.assertIsNone(latest_compaction_preview(self.case))
        update_case(self.case, {"run_defaults": {}})
        self.assertEqual(latest_compaction_preview(self.case), identity)

    def test_selection_change_after_preflight_blocks_live_application(self):
        identity, _ = self.preview()
        def preflight(*args, **kwargs):
            self.assertTrue(kwargs["dry_run"])
            update_case(self.case, {"run_defaults": {"group_context_inputs": True}})
            return {"dry_run": True}
        with patch("liquid_tracer.cli.sync", side_effect=preflight) as sync:
            with self.assertRaisesRegex(TraceError, "branch hubs changed"):
                sync_run(self.case, self.run, reorganize=True, compact_preview=identity)
            sync.assert_called_once()

    def test_arrow_coloring_change_requires_a_fresh_compact_preview(self):
        identity, _ = self.preview()
        update_case(self.case, {"run_defaults": {"color_attribution_arrows": True}})
        with patch("liquid_tracer.cli.sync") as sync, \
                self.assertRaisesRegex(TraceError, "Attribution arrow coloring changed"):
            sync_run(self.case, self.run, reorganize=True, compact_preview=identity)
        sync.assert_not_called()
        self.assertIsNone(latest_compaction_preview(self.case))
        updated, _ = self.preview("87654321")
        _, meta = verified_compaction_preview(self.case, self.run, updated)
        self.assertTrue(meta["graph_options"]["color_attribution_arrows"])
        self.assertEqual(self.snapshot, {p.name: p.read_bytes() for p in self.archive.iterdir() if p.is_file()})

    def test_center_group_change_rejects_review_before_miro_writes(self):
        identity, _ = self.preview()
        update_case(self.case, {"run_defaults": {"center_name": "Example Exchange"}})
        with patch("liquid_tracer.cli.sync") as sync, \
                self.assertRaisesRegex(TraceError, "Centered name group changed"):
            sync_run(self.case, self.run, reorganize=True, compact_preview=identity)
        sync.assert_not_called()
        self.assertIsNone(latest_compaction_preview(self.case))
        fresh, _ = self.preview("abcdef12")
        _, meta = verified_compaction_preview(self.case, self.run, fresh)
        self.assertEqual(meta["graph_options"]["center_name"], "Example Exchange")

    def test_detail_files_are_checked_when_present_but_old_manifests_still_work(self):
        identity, directory = self.preview()
        meta = read_json(directory / "compaction.json")
        self.assertEqual(meta["graph_options"]["hub_addresses"], [])
        self.assertFalse(meta["graph_options"]["group_context_inputs"])
        self.assertIn('href="details.html"', (directory / "graph.html").read_text())
        verified_compaction_preview(self.case, self.run, identity)
        details = directory / "details.html"
        details.write_text(details.read_text() + "changed")
        with self.assertRaisesRegex(TraceError, "checksum mismatch"):
            verified_compaction_preview(self.case, self.run, identity)
        # Recreate the former required-file manifest without new optional files
        # or metadata. Older ungrouped previews retain exact reviewed plans.
        for name in ("details.html", "details.json"):
            (directory / name).unlink()
        meta.pop("graph_options")
        save_json(directory / "compaction.json", meta)
        (directory / "SHA256SUMS").write_text("".join(
            digest((directory / name).read_bytes()) + "  " + name + "\n" for name in sorted(FILES)))
        verified_compaction_preview(self.case, self.run, identity)
        self.assertEqual(latest_compaction_preview(self.case), identity)

    def test_tampered_html_invalidates_previously_verified_preview(self):
        identity, directory = self.preview()
        verified_compaction_preview(self.case, self.run, identity)
        path = directory / "graph.html"
        path.write_text(path.read_text() + "changed")
        with self.assertRaisesRegex(TraceError, "checksum mismatch"):
            verified_compaction_preview(self.case, self.run, identity)
        self.assertIsNone(latest_compaction_preview(self.case))

    def test_tampered_archive_invalidates_cached_source_verification(self):
        identity, _ = self.preview()
        verified_compaction_preview(self.case, self.run, identity)
        path = self.archive / "trace.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(TraceError, "checksum mismatch"):
            verified_compaction_preview(self.case, self.run, identity)

    def test_incomplete_preview_is_not_discovered_or_applied(self):
        identity, directory = self.preview()
        (directory / "SHA256SUMS").unlink()
        self.assertIsNone(latest_compaction_preview(self.case))
        with self.assertRaises(TraceError):
            verified_compaction_preview(self.case, self.run, identity)

    def test_another_case_or_run_and_path_traversal_are_rejected(self):
        identity, directory = self.preview()
        second = create_investigation(self.root / "cases", "Other synthetic case", seeds=[A + ":0"], fixture=self.fixture)
        import shutil
        shutil.copytree(self.archive, second / "runs" / self.run)
        shutil.copytree(directory, second / "previews" / identity)
        with self.assertRaisesRegex(TraceError, "another investigation"):
            verified_compaction_preview(second, self.run, identity)
        for invalid in ("../" + identity, str(directory), identity + "/graph.html", "0" * 16 + "-compact-12345678"):
            with self.subTest(invalid=invalid), self.assertRaises(TraceError):
                verified_compaction_preview(self.case, self.run, invalid)

    def test_symlink_preview_file_is_rejected_even_with_identical_bytes(self):
        identity, directory = self.preview()
        path = directory / "before.svg"
        target = self.root / "outside.svg"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
        with self.assertRaisesRegex(TraceError, "symbolic link"):
            verified_compaction_preview(self.case, self.run, identity)

    def test_changed_plan_with_rewritten_manifest_does_not_match_review_metadata(self):
        identity, directory = self.preview()
        path = directory / "miro-plan.json"
        plan = read_json(path)
        plan["shapes"][0]["body"]["position"]["x"] += 100
        del plan["sha256"]
        from liquid_tracer.common import canonical
        plan["sha256"] = digest(canonical(plan))
        save_json(path, plan)
        manifest = directory / "SHA256SUMS"
        manifest.write_text("\n".join((digest(path.read_bytes()) + "  miro-plan.json") if line.endswith("  miro-plan.json") else line
                                       for line in manifest.read_text().splitlines()) + "\n")
        with self.assertRaises(TraceError):
            verified_compaction_preview(self.case, self.run, identity)


if __name__ == "__main__":
    unittest.main()
