"""Shared saved-evidence plotting, offline boundaries, and reviewed artifacts."""
from copy import deepcopy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import TraceError, canonical, digest, read_json, save_json
from liquid_tracer.investigations import create_investigation, read_case, update_case
from liquid_tracer.plots import FILES, list_plots, preview_plot, reviewed_plot
from liquid_tracer.services import set_service
from tests.test_attribution_convergence import graph_state, tx
from tests.test_connections import saved_case
from tests.test_pegout_paths import add_pegout


class PlotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = create_investigation(self.root, "Plot workspace", seeds=[tx("f") + ":0"],
                                         run_defaults={"layout_attempts": 1})
        state = graph_state((("a:0", "c"), ("c:0", "b")), seeds=("a:0", "b:0"))
        self.endpoint = add_pegout(state, tx("b"))
        self.state, self.archive = saved_case(self.case, state)
        layout = patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph)
        self.layout = layout.start()
        self.addCleanup(layout.stop)
        for name in ("liquid_tracer.api.Esplora.get", "liquid_tracer.address_counts.ensure_counts",
                     "liquid_tracer.miro.publish", "liquid_tracer.miro.sync"):
            blocker = patch(name, side_effect=AssertionError("Plots must not fetch or publish"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def bytes(self, root):
        return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    def rewrite_archive(self, state):
        save_json(self.archive / "trace.json", state)
        names = [line.split("  ", 1)[1] for line in (self.archive / "SHA256SUMS").read_text().splitlines()]
        (self.archive / "SHA256SUMS").write_text("".join(digest((self.archive / name).read_bytes()) + "  " + name + "\n" for name in names))

    def rehash_preview(self, directory):
        (directory / "SHA256SUMS").write_text("".join(digest((directory / name).read_bytes()) + "  " + name + "\n"
                                                    for name in sorted(FILES - {"SHA256SUMS"})))

    def test_three_goals_share_saved_evidence_without_fetching_or_archive_mutation(self):
        before = self.bytes(self.archive)
        case_before = (self.case / "case.json").read_bytes()
        for goal in ("full", "connections", "pegouts"):
            with self.subTest(goal=goal):
                result = preview_plot(self.case, goal, max_hops=2)
                graph, plan = reviewed_plot(self.case, result["preview_id"])
                self.assertEqual(result["goal"], goal)
                self.assertEqual(graph["namespace"], plan["namespace"])
                self.assertEqual(plan["schema_version"], 2)
                self.assertEqual(graph["plot"], read_json(Path(result["directory"]) / "plot.json"))
                self.assertEqual(set(path.name for path in Path(result["directory"]).iterdir()), FILES)
                self.assertTrue(result["saved_data_only"])
                self.assertEqual(result["source_max_hops"], 10)
                self.assertEqual(result["source_run_status"], "bounded_complete")
                self.assertIn("Saved-data-only", result["notice"])
        self.assertEqual(len(list_plots(self.case)), 3)
        self.assertEqual(before, self.bytes(self.archive))
        self.assertEqual(case_before, (self.case / "case.json").read_bytes())
        self.assertFalse((self.case / "miro").exists())

    def test_pegouts_use_selected_archive_seeds_not_changed_case_seeds(self):
        result = preview_plot(self.case, "pegouts", min_hops=2, max_hops=2)
        graph, _ = reviewed_plot(self.case, result["preview_id"])
        self.assertEqual(result["query"], {"seeds": sorted(self.state["seeds"]), "min_hops": 2, "max_hops": 2})
        self.assertEqual((result["min_hops"], result["max_hops"]), (2, 2))
        self.assertEqual([match["outpoint"] for match in graph["pegouts"]["matches"]], [self.endpoint])
        metadata = read_case(self.case)
        metadata["seeds"] = [tx("e") + ":0"]
        save_json(self.case / "case.json", metadata)
        reviewed_plot(self.case, result["preview_id"])

    def test_unselected_sibling_paths_and_context_spends_do_not_become_matches(self):
        state = graph_state((("a:0", "c"), ("a:1", "d")), seeds=("a:0",), raw_links=(("a:2", "e"),))
        selected = add_pegout(state, tx("c"))
        add_pegout(state, tx("d"))
        add_pegout(state, tx("e"))
        self.state, self.archive = saved_case(self.case, state)
        result = preview_plot(self.case, "pegouts", max_hops=1)
        graph, _ = reviewed_plot(self.case, result["preview_id"])
        self.assertEqual([match["outpoint"] for match in graph["pegouts"]["matches"]], [selected])
        self.assertEqual(graph["pegouts"]["outpoints"], [tx("a") + ":0"])

    def test_empty_results_remain_reviewable_with_no_layout_or_miro_objects(self):
        for goal in ("connections", "pegouts"):
            with self.subTest(goal=goal):
                result = preview_plot(self.case, goal, max_hops=0)
                graph, plan = reviewed_plot(self.case, result["preview_id"])
                self.assertTrue(result["empty"])
                self.assertEqual(graph["nodes"], [])
                self.assertEqual(plan["shapes"], [])
                self.assertEqual(plan["connectors"], [])
                self.assertIn("No matching activity", Path(result["html"]).read_text())
        self.layout.assert_not_called()

    def test_partial_collection_reports_coverage_without_claiming_search_complete(self):
        state = deepcopy(self.state)
        state.update(status="paused", stop_reason="request_limit")
        state["limits"]["max_hops"] = 3
        self.rewrite_archive(state)
        result = preview_plot(self.case, "pegouts", max_hops=10)
        self.assertEqual(result["source_max_hops"], 3)
        self.assertEqual(result["source_run_status"], "paused")
        self.assertEqual(result["source_stop_reason"], "request_limit")
        self.assertIn("hop limit 3", result["coverage_notice"])
        self.assertIn("request_limit", result["coverage_notice"])
        self.assertEqual(result["max_hops"], 10)

    def test_changed_controls_invalidate_review_but_keep_history_visible(self):
        result = preview_plot(self.case, "pegouts")
        set_service(self.case, "SYNTHETIC-c-address", name="Stop", stop_tracing=True)
        with self.assertRaisesRegex(TraceError, "changed"):
            reviewed_plot(self.case, result["preview_id"])
        listed = list_plots(self.case)
        self.assertEqual(len(listed), 1)
        self.assertFalse(listed[0]["reviewable"])
        self.assertIn("changed", listed[0]["review_error"])
        fresh = preview_plot(self.case, "pegouts")
        self.assertTrue(fresh["empty"])

    def test_current_settings_apply_and_changes_invalidate_review(self):
        update_case(self.case, {"run_defaults": {"include_fees": True, "group_context_inputs": True,
                                                "center_name": "Example", "connector_style": "elbowed"}})
        result = preview_plot(self.case, "full")
        graph, _ = reviewed_plot(self.case, result["preview_id"])
        self.assertTrue(graph["graph_options"]["group_context_inputs"])
        self.assertTrue(graph["include_fees"])
        self.assertEqual(graph["graph_options"]["center_name"], "Example")
        self.assertEqual(self.layout.call_args.kwargs["connector_style"], "elbowed")
        update_case(self.case, {"run_defaults": {"connector_style": "straight"}})
        with self.assertRaisesRegex(TraceError, "changed"):
            reviewed_plot(self.case, result["preview_id"])

    def test_modified_plot_and_rehashed_disagreeing_plan_are_rejected(self):
        result = preview_plot(self.case, "full")
        directory = Path(result["directory"])
        (directory / "graph.svg").write_text("changed")
        with self.assertRaisesRegex(TraceError, "changed"):
            reviewed_plot(self.case, result["preview_id"])
        result = preview_plot(self.case, "full")
        directory = Path(result["directory"])
        graph = read_json(directory / "graph.json")
        graph["nodes"][0]["label"] += " changed"
        save_json(directory / "graph.json", graph)
        self.rehash_preview(directory)
        with self.assertRaisesRegex(TraceError, "disagree"):
            reviewed_plot(self.case, result["preview_id"])
        self.assertEqual(list_plots(self.case), [])

    def test_changed_source_manifest_rejected_and_new_latest_does_not_retarget_old_plot(self):
        result = preview_plot(self.case, "full")
        metadata = read_case(self.case)
        metadata["latest_run"] = "f" * 16
        save_json(self.case / "case.json", metadata)
        reviewed_plot(self.case, result["preview_id"])
        state = deepcopy(self.state)
        state["stop_reason"] = "changed source"
        self.rewrite_archive(state)
        with self.assertRaisesRegex(TraceError, "changed"):
            reviewed_plot(self.case, result["preview_id"])

    def test_paths_symlinks_foreign_case_and_invalid_queries_rejected(self):
        for identity in (None, "../../private", "a" * 16 + "-pegouts-12345678"):
            with self.assertRaises(TraceError):
                reviewed_plot(self.case, identity)
        for goal, options in (("unknown", {}), ("pegouts", {"min_hops": 3, "max_hops": 1}),
                              ("connections", {"max_hops": True})):
            with self.assertRaises(TraceError):
                preview_plot(self.case, goal, **options)
        result = preview_plot(self.case, "full")
        directory = Path(result["directory"])
        external = self.root / "external.svg"
        external.write_bytes((directory / "graph.svg").read_bytes())
        (directory / "graph.svg").unlink()
        (directory / "graph.svg").symlink_to(external)
        with self.assertRaisesRegex(TraceError, "symbolic"):
            reviewed_plot(self.case, result["preview_id"])
        state = deepcopy(self.state)
        state["case_id"] = "foreign"
        self.rewrite_archive(state)
        with self.assertRaisesRegex(TraceError, "belong"):
            preview_plot(self.case, "full")

    def test_legacy_search_archives_are_not_sources_or_mutated(self):
        legacy = self.case / "pegouts" / ("e" * 16)
        legacy.mkdir(parents=True)
        save_json(legacy / "trace.json", self.state)
        before = self.bytes(legacy)
        with self.assertRaisesRegex(TraceError, "manifest"):
            preview_plot(self.case, "pegouts", run_id=legacy.name)
        preview_plot(self.case, "pegouts")
        self.assertEqual(before, self.bytes(legacy))

    def test_saved_address_counts_are_used_and_updated_counts_require_refresh(self):
        address = "SYNTHETIC-c-address"
        cache = {"schema_version": 1, "case_id": self.state["case_id"], "source": self.state["source"],
                 "counts": {address: {"address": address, "source": self.state["source"],
                            "confirmed_tx_count": 12, "mempool_tx_count": 1, "observed_at": "2026-09-25"}}}
        def save_cache():
            cache["sha256"] = digest(canonical({key: value for key, value in cache.items() if key != "sha256"}))
            save_json(self.case / "address-counts.json", cache)
        save_cache()
        result = preview_plot(self.case, "full")
        graph, _ = reviewed_plot(self.case, result["preview_id"])
        node = next(node for node in graph["nodes"] if node.get("details", {}).get("address") == address)
        self.assertEqual(node["tx_count"], 13)
        cache["counts"][address]["confirmed_tx_count"] = 14
        save_cache()
        with self.assertRaisesRegex(TraceError, "changed"):
            reviewed_plot(self.case, result["preview_id"])

    def test_listing_multiple_goals_verifies_shared_source_once(self):
        from liquid_tracer.cli import verify_export
        for goal in ("full", "connections", "pegouts"):
            preview_plot(self.case, goal)
        with patch("liquid_tracer.cli.verify_export", wraps=verify_export) as verify:
            self.assertEqual(len(list_plots(self.case)), 3)
        verify.assert_called_once_with(self.archive)

    def test_bootstrap_collection_without_transactions_has_empty_reviewable_full_plot(self):
        state = deepcopy(self.state)
        state.update(transactions={}, outputs={}, links={}, status="paused", stop_reason="interrupted")
        self.rewrite_archive(state)
        result = preview_plot(self.case, "full")
        graph, plan = reviewed_plot(self.case, result["preview_id"])
        self.assertTrue(result["empty"])
        self.assertEqual(plan["shapes"], [])
        self.assertEqual(graph["plot"]["status"], "empty")
        self.layout.assert_not_called()

    def test_failed_csv_export_reports_export_stage_without_publishing_partial_plot(self):
        events = []
        before = self.bytes(self.archive)
        with patch("liquid_tracer.transaction_csv.write_transaction_csv", side_effect=TraceError("Synthetic export failure")):
            with self.assertRaisesRegex(TraceError, "Synthetic export failure"):
                preview_plot(self.case, "full", progress=events.append)
        self.assertEqual(events[-1], {"phase": "exporting_plot", "completed": 0, "total": 1})
        self.assertEqual(list_plots(self.case), [])
        self.assertEqual(self.bytes(self.archive), before)
        result = preview_plot(self.case, "full", progress=events.append)
        self.assertEqual(events[-1], {"phase": "exporting_plot", "completed": 1, "total": 1})
        self.assertEqual([item["preview_id"] for item in list_plots(self.case)], [result["preview_id"]])
