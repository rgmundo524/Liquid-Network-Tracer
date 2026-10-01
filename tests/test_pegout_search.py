"""Live-search boundaries using synthetic Esplora responses and isolated plots."""
import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stderr

from liquid_tracer.api import Esplora
from liquid_tracer.common import StopRun, TraceError, digest, read_json, save_json
from liquid_tracer.investigations import create_investigation, read_case, update_case
from liquid_tracer.pegouts import (FILES, list_pegout_searches, preview_pegouts, publish_pegouts,
                                  reviewed_pegouts, saved_pegout_snapshot, search_pegouts)
from liquid_tracer.services import set_service
from tests.fixtures import A, B, C, D, fixture


class PegoutSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = self.root / "fixture.json"
        data = fixture()
        data["/tx/" + A + "/outspends"][1] = {"spent": False}
        save_json(self.fixture, data)
        self.case = create_investigation(self.root, "Pegouts", fixture=self.fixture,
            seeds=[B + ":0"], board="primary-board", run_defaults={"layout_attempts": 1})
        self.layout = patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph)
        self.layout_mock = self.layout.start()
        self.addCleanup(self.layout.stop)

    def search(self, **options):
        values = {"max_transactions": 20, "max_requests": 100, "max_outpoints": 100, **options}
        return search_pegouts(self.case, A, 0, 3, **values)

    def state(self, result):
        return read_json(self.case / "pegouts" / result["search_id"] / "trace.json")

    def seed_search(self, **options):
        return search_pegouts(self.case, **{"max_hops": 3, "max_transactions": 20,
            "max_requests": 100, "max_outpoints": 100, **options})

    def save_seeds(self, seeds):
        metadata = read_case(self.case)
        metadata["seeds"] = seeds
        save_json(self.case / "case.json", metadata)

    def test_saved_seeds_work_before_first_main_run_and_exclude_siblings(self):
        before = (self.case / "case.json").read_bytes()
        result = self.seed_search()
        state = self.state(result)
        self.assertEqual(result["query"], {"seeds": [B + ":0"], "min_hops": 0, "max_hops": 3})
        self.assertEqual(state["seeds"], [B + ":0"])
        self.assertEqual(set(state["transactions"]), {B, C, D})
        self.assertNotIn(B + ":1", state["outputs"])
        self.assertNotIn(B + ":2", state["outputs"])
        graph, _ = reviewed_pegouts(self.case, result["preview_id"])
        self.assertEqual(graph["pegouts"]["matches"][0]["hops"], [2])
        self.assertTrue({B + ":1", B + ":2"} <= {edge["outpoint"] for edge in graph["edges"]})
        self.assertEqual(graph["pegouts"]["query"]["transaction_io"], "complete")
        self.assertTrue(all(edge["role"] == "context_output" for edge in graph["edges"]
                            if edge["id"] in {"out:" + B + ":1", "out:" + B + ":2"}))
        self.assertEqual((self.case / "case.json").read_bytes(), before)
        self.assertFalse((self.case / "runs").exists())
        self.assertFalse((self.case / "miro").exists())

    def test_all_selected_seeds_from_multiple_transactions_are_searched(self):
        data = read_json(self.fixture)
        data["/tx/" + A]["vout"][1] = dict(data["/tx/" + D]["vout"][0])
        save_json(self.fixture, data)
        self.save_seeds([B + ":0", A.upper() + ":1", B + ":0"])
        result = self.seed_search(max_hops=2)
        selected = sorted([A + ":1", B + ":0"])
        self.assertEqual(result["query"]["seeds"], selected)
        self.assertEqual(self.state(result)["seeds"], selected)
        self.assertNotIn(A + ":0", self.state(result)["outputs"])
        self.assertNotIn(B + ":1", self.state(result)["outputs"])
        graph, _ = reviewed_pegouts(self.case, result["preview_id"])
        self.assertEqual({row["outpoint"]: row["hops"] for row in graph["pegouts"]["matches"]},
                         {A + ":1": [0], D + ":0": [2]})

    def test_seed_resume_retains_archived_seeds_after_case_seeds_change(self):
        first = self.seed_search(max_transactions=1)
        directory = self.case / "pegouts" / first["search_id"]
        snapshot = {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
        self.save_seeds([A + ":1"])
        resumed = search_pegouts(self.case, resume=first["search_id"], max_transactions=20,
                                max_requests=100, max_outpoints=100)
        self.assertEqual(resumed["query"], first["query"])
        self.assertEqual(self.state(resumed)["seeds"], [B + ":0"])
        self.assertEqual(resumed["match_count"], 1)
        self.assertEqual(snapshot, {str(p.relative_to(directory)): p.read_bytes()
                                  for p in directory.rglob("*") if p.is_file()})
        fresh = self.seed_search()
        self.assertEqual(fresh["seeds"], [A + ":1"])
        self.assertEqual(fresh["match_count"], 0)

    def test_seed_search_interruption_before_first_fetch_remains_resumable(self):
        with patch.object(Esplora, "get", side_effect=StopRun("time_limit")):
            first = self.seed_search()
        self.assertEqual(first["status"], "paused")
        self.assertEqual(self.state(first)["seeds"], [B + ":0"])
        self.assertEqual(set(self.state(first)["outputs"]), {B + ":0"})
        self.assertEqual(self.state(first)["transactions"], {})
        resumed = search_pegouts(self.case, resume=first["search_id"], max_transactions=20,
                                max_requests=100, max_outpoints=100)
        self.assertEqual(resumed["match_count"], 1)

    def test_seed_checkpoint_saved_before_trace_recovers_its_frontier(self):
        with patch("liquid_tracer.pegouts.trace", side_effect=RuntimeError("before trace")):
            with self.assertRaisesRegex(RuntimeError, "before trace"):
                self.seed_search()
        search_id = next((self.case / "pegouts").iterdir()).name
        self.assertTrue(list_pegout_searches(self.case)[0]["recoverable"])
        resumed = search_pegouts(self.case, resume=search_id, max_transactions=20,
                                max_requests=100, max_outpoints=100)
        self.assertEqual(resumed["match_count"], 1)

    def test_interrupted_seed_initialization_resumes_every_frozen_seed(self):
        from liquid_tracer.common import parse_outpoint
        data = read_json(self.fixture)
        data["/tx/" + A]["vout"][1] = dict(data["/tx/" + D]["vout"][0])
        save_json(self.fixture, data)
        selected = sorted([A + ":1", B + ":0"])
        self.save_seeds(selected)
        calls = 0

        def interrupted_parse(value):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt()
            return parse_outpoint(value)

        with patch("liquid_tracer.trace.parse_outpoint", side_effect=interrupted_parse):
            first = self.seed_search(max_hops=2)
        self.assertEqual(first["status"], "paused")
        self.assertEqual(self.state(first)["seeds"], selected)
        self.assertEqual(set(self.state(first)["outputs"]), {selected[0]})
        self.assertEqual(self.state(first)["transactions"], {})
        resumed = search_pegouts(self.case, resume=first["search_id"], max_transactions=20,
                                max_requests=100, max_outpoints=100)
        graph, _ = reviewed_pegouts(self.case, resumed["preview_id"])
        self.assertEqual({row["outpoint"]: row["hops"] for row in graph["pegouts"]["matches"]},
                         {A + ":1": [0], D + ":0": [2]})

    def test_seed_query_and_checkpoint_seeds_must_agree(self):
        first = self.seed_search(max_transactions=1)
        directory = self.case / "pegouts" / first["search_id"]
        (directory / "SHA256SUMS").unlink()
        state = self.state(first)
        state["seeds"] = [B + ":1"]
        save_json(directory / "trace.json", state)
        with self.assertRaisesRegex(TraceError, "does not match"):
            search_pegouts(self.case, resume=first["search_id"])

    def test_saved_seed_frontier_ignores_attribution_hop_caps_but_honors_stops(self):
        # Any active stop rule changes frontier initialization, even off-path.
        set_service(self.case, "SYNTHETIC-other-service", name="Other", stop_tracing=True)
        self.assertEqual(self.seed_search()["match_count"], 1)
        set_service(self.case, "SYNTHETIC-branch-A", name="Service", hop_limit=0, stop_tracing=False)
        first = self.seed_search()
        self.assertEqual(first["match_count"], 1)
        self.assertEqual(set(self.state(first)["transactions"]), {B, C, D})
        set_service(self.case, "SYNTHETIC-branch-A", name="Service", hop_limit=0, stop_tracing=True)
        stopped = self.seed_search()
        self.assertEqual(stopped["match_count"], 0)
        self.assertEqual(set(self.state(stopped)["transactions"]), {B})

    def test_missing_saved_seeds_fail_before_fetch_and_custom_origin_still_works(self):
        self.save_seeds([])
        with patch.object(Esplora, "get", side_effect=AssertionError("must not fetch")):
            with self.assertRaisesRegex(TraceError, "seed"):
                self.seed_search()
        self.assertFalse((self.case / "pegouts").exists())
        self.assertEqual(self.search()["match_count"], 1)

    def test_seed_search_does_not_replace_existing_main_run(self):
        from tests.test_connections import saved_case
        initial = self.search()
        _, archive = saved_case(self.case, self.state(initial))
        before = (self.case / "case.json").read_bytes()
        archived = {p.name: p.read_bytes() for p in archive.iterdir()}
        result = self.seed_search()
        self.assertEqual(self.state(result)["seeds"], [B + ":0"])
        self.assertEqual((self.case / "case.json").read_bytes(), before)
        self.assertEqual(archived, {p.name: p.read_bytes() for p in archive.iterdir()})

    def test_cli_defaults_to_saved_seeds_and_keeps_override_exclusive(self):
        from liquid_tracer.cli import parser
        args = parser().parse_args(["pegouts", "--case", str(self.case), "--max-hops", "4"])
        self.assertIsNone(args.txid)
        self.assertIsNone(args.resume)
        self.assertEqual(args.max_hops, 4)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(["pegouts", "--case", str(self.case), "--txid", A, "--resume", "a" * 16])

    def test_fetches_all_origin_outputs_and_plots_only_pegout_paths(self):
        before = (self.case / "case.json").read_bytes()
        result = self.search()
        state = self.state(result)
        self.assertEqual(state["seeds"], [A + ":0", A + ":1", A + ":2"])
        self.assertEqual(set(state["transactions"]), {A, B, C, D})
        self.assertEqual(result["status"], "bounded_complete")
        self.assertEqual(result["match_count"], 1)
        graph, plan = reviewed_pegouts(self.case, result["preview_id"])
        self.assertEqual(graph["pegouts"]["matches"][0]["outpoint"], D + ":0")
        self.assertTrue(any(e["role"].startswith("context") for e in graph["edges"]))
        self.assertTrue(any(e["outpoint"] == A + ":1" for e in graph["edges"]))
        self.assertNotIn(A + ":1", graph["pegouts"]["outpoints"])
        self.assertNotIn("namespace", plan)
        self.assertEqual((self.case / "case.json").read_bytes(), before)
        self.assertFalse((self.case / "runs").exists())
        self.assertFalse((self.case / "miro").exists())

    def test_regenerated_search_preview_preserves_archive_and_markerless_saved_preview(self):
        from liquid_tracer.pegout_paths import pegout_graph
        from liquid_tracer.plot_csv import build_plot_csv

        def legacy_graph(state, query, **options):
            return pegout_graph(state, {key: value for key, value in query.items()
                                       if key not in {"transaction_io", "attribution_hop_limits"}}, **options)

        with patch("liquid_tracer.pegouts.pegout_graph", side_effect=legacy_graph):
            original = self.seed_search()
        old_graph, old_plan = reviewed_pegouts(self.case, original["preview_id"])
        self.assertNotIn("transaction_io", old_graph["pegouts"]["query"])
        directory = self.case / "pegouts" / original["search_id"]
        archive = {str(path.relative_to(directory)): path.read_bytes()
                   for path in directory.rglob("*") if path.is_file()}
        old_directory = Path(original["directory"])
        snapshot = {path.name: path.read_bytes() for path in old_directory.iterdir() if path.is_file()}
        old_csv = build_plot_csv(self.case, original["preview_id"], "endpoints.csv")["data"]
        with patch.object(Esplora, "get", side_effect=AssertionError("Use saved evidence only")):
            regenerated = preview_pegouts(self.case, original["search_id"])
        new_graph, _ = reviewed_pegouts(self.case, regenerated["preview_id"])
        self.assertEqual(new_graph["pegouts"]["query"]["transaction_io"], "complete")
        self.assertTrue(new_graph["include_fees"])
        self.assertEqual(old_graph["pegouts"]["matches"], new_graph["pegouts"]["matches"])
        self.assertEqual(reviewed_pegouts(self.case, original["preview_id"]), (old_graph, old_plan))
        self.assertEqual(build_plot_csv(self.case, regenerated["preview_id"], "endpoints.csv")["data"], old_csv)
        self.assertEqual({path.name: path.read_bytes() for path in old_directory.iterdir() if path.is_file()}, snapshot)
        self.assertEqual({str(path.relative_to(directory)): path.read_bytes()
                          for path in directory.rglob("*") if path.is_file()}, archive)

    def test_direct_hop_zero_pegout_uses_one_request(self):
        result = search_pegouts(self.case, D, 0, 0, max_requests=1, max_outpoints=10)
        state = self.state(result)
        self.assertEqual(result["match_count"], 1)
        self.assertEqual(state["stats"]["requests_this_run"], 1)
        self.assertEqual(state["stats"]["new_transactions_this_run"], 1)
        self.assertEqual(set(state["transactions"]), {D})
        self.assertEqual(state["links"], {})

    def test_transaction_budget_resume_preserves_parent_archive_and_same_ceiling(self):
        first = self.search(max_transactions=2)
        self.assertEqual(first["status"], "paused")
        self.assertEqual(first["stop_reason"], "transaction_limit")
        self.assertEqual(first["match_count"], 0)
        directory = self.case / "pegouts" / first["search_id"]
        original = {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
        resumed = search_pegouts(self.case, resume=first["search_id"], max_transactions=20,
                                 max_requests=100, max_outpoints=100)
        self.assertEqual(resumed["match_count"], 1)
        self.assertEqual(resumed["max_hops"], 3)
        state = self.state(resumed)
        self.assertEqual(state["parent_run"], first["search_id"])
        self.assertEqual(state["stats"]["new_transactions_this_run"], 2)
        self.assertEqual(original, {str(p.relative_to(directory)): p.read_bytes()
                                    for p in directory.rglob("*") if p.is_file()})

    def test_request_and_outpoint_caps_produce_resumable_partial_results(self):
        for options, reason in (({"max_requests": 1}, "request_limit"),
                                ({"max_outpoints": 1}, "outpoint_limit")):
            with self.subTest(reason=reason):
                result = self.search(**options)
                self.assertEqual(result["status"], "paused")
                self.assertEqual(result["stop_reason"], reason)
                self.assertTrue(result["resumable"])
                self.assertLessEqual(self.state(result)["stats"]["requests_this_run"], options.get("max_requests", 100))

    def test_default_case_budgets_apply_without_mutating_defaults(self):
        update_case(self.case, {"run_defaults": {"budget_limits_enabled": True, "max_transactions": 1, "max_requests": 3,
                                                "max_outpoints": 2, "max_seconds": 12}})
        result = search_pegouts(self.case, A, 0, 3)
        state = self.state(result)
        self.assertEqual(state["limits"], {"max_hops": 3, "max_transactions": 1,
                                          "max_requests": 3, "max_outpoints": 2, "max_seconds": 12})
        self.assertEqual(read_case(self.case)["run_defaults"]["hops"], 1)

    def test_saved_numeric_preferences_do_not_limit_search_when_budgets_are_disabled(self):
        update_case(self.case, {"run_defaults": {"max_transactions": 1, "max_requests": 1,
                                                "max_outpoints": 1, "max_seconds": .001}})
        result = search_pegouts(self.case, A, 0, 3)
        state = self.state(result)
        self.assertEqual(state["status"], "bounded_complete")
        self.assertEqual(set(state["transactions"]), {A, B, C, D})
        self.assertTrue(all(value == 0 for name, value in state["limits"].items() if name != "max_hops"))
        self.assertEqual(read_case(self.case)["run_defaults"]["max_requests"], 1)

    def test_preview_after_layout_failure_never_refetches(self):
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=TraceError("synthetic layout failure")):
            with self.assertRaisesRegex(TraceError, "was saved.*Retry its preview"):
                self.search()
        searches = list_pegout_searches(self.case)
        self.assertEqual(len(searches), 1)
        self.assertNotIn("preview_id", searches[0])
        with patch.object(Esplora, "get", side_effect=AssertionError("preview fetched")):
            result = preview_pegouts(self.case, searches[0]["search_id"])
        self.assertEqual(result["match_count"], 1)

    def test_empty_plot_avoids_elk_and_remains_reviewable(self):
        result = search_pegouts(self.case, A, 0, 0)
        self.assertEqual(result["match_count"], 0)
        self.layout_mock.assert_not_called()
        graph, plan = reviewed_pegouts(self.case, result["preview_id"])
        self.assertEqual(graph["nodes"], [])
        self.assertEqual(plan["shapes"], [])
        self.assertTrue(FILES.issubset({p.name for p in Path(result["directory"]).iterdir()}))

    def test_archive_records_original_responses_and_hashes(self):
        result = self.search()
        directory = self.case / "pegouts" / result["search_id"]
        index = read_json(directory / "evidence-index.json")
        self.assertTrue(index)
        for row in index:
            raw = (directory / row["file"]).read_bytes()
            self.assertEqual(digest(raw), row["sha256"])
            self.assertEqual(json.loads(raw), read_json(self.fixture)[row["endpoint"]])

    def test_fresh_search_reuses_transaction_cache_without_reusing_outspends(self):
        first = self.search()
        second = self.search()
        old, new = self.state(first), self.state(second)
        self.assertLess(new["stats"]["requests_this_run"], old["stats"]["requests_this_run"])
        for txid in (A, B, C, D):
            self.assertEqual(new["transactions"][txid]["observation_id"], old["transactions"][txid]["observation_id"])
        self.assertNotEqual(new["links"][A + ":0"]["observation_id"], old["links"][A + ":0"]["observation_id"])

    def test_bootstrap_time_limit_is_saved_and_resumable(self):
        with patch.object(Esplora, "get", side_effect=StopRun("time_limit")):
            first = self.search()
        self.assertEqual(first["status"], "paused")
        self.assertEqual(first["stop_reason"], "time_limit")
        self.assertEqual(self.state(first)["seeds"], [])
        second = search_pegouts(self.case, resume=first["search_id"], max_transactions=20,
                                max_requests=100, max_outpoints=100)
        self.assertEqual(second["match_count"], 1)

    def test_unsealed_checkpoint_recovers_only_verified_evidence(self):
        first = self.search(max_transactions=2)
        directory = self.case / "pegouts" / first["search_id"]
        (directory / "SHA256SUMS").unlink()
        self.assertTrue(list_pegout_searches(self.case)[0]["recoverable"])
        resumed = search_pegouts(self.case, resume=first["search_id"], max_transactions=20,
                                 max_requests=100, max_outpoints=100)
        self.assertEqual(resumed["match_count"], 1)
        state = read_json(directory / "trace.json")
        state["transactions"][A]["data"]["vout"][0]["scriptpubkey_address"] = "tampered"
        save_json(directory / "trace.json", state)
        with self.assertRaisesRegex(TraceError, "not backed by saved evidence"):
            search_pegouts(self.case, resume=first["search_id"])

    def test_query_and_archive_tampering_are_rejected(self):
        result = self.search()
        directory = self.case / "pegouts" / result["search_id"]
        query = read_json(directory / "query.json")
        query["max_hops"] = 9
        save_json(directory / "query.json", query)
        with self.assertRaisesRegex(TraceError, "changed"):
            search_pegouts(self.case, resume=result["search_id"])
        with self.assertRaisesRegex(TraceError, "changed"):
            reviewed_pegouts(self.case, result["preview_id"])

    def test_updated_controls_require_new_preview_and_resume_honors_them(self):
        result = self.search()
        set_service(self.case, "SYNTHETIC-branch-A", name="Service", stop_tracing=True)
        with self.assertRaisesRegex(TraceError, "controls changed"):
            reviewed_pegouts(self.case, result["preview_id"])
        refreshed = preview_pegouts(self.case, result["search_id"])
        # B's other output also reaches C; the C output shares the stopped address.
        self.assertEqual(refreshed["match_count"], 0)
        self.assertEqual(len(self.state(result)["transactions"]), 4)

    def test_historical_snapshot_survives_rule_changes_without_becoming_publishable(self):
        result = self.search()
        graph, plan = reviewed_pegouts(self.case, result["preview_id"])
        archive = self.case / "pegouts" / result["search_id"]
        preview = Path(result["directory"])
        before = {str(path): path.read_bytes() for directory in (archive, preview)
                  for path in directory.rglob("*") if path.is_file()}
        set_service(self.case, "SYNTHETIC-branch-A", name="Changed attribution", stop_tracing=True)
        update_case(self.case, {"run_defaults": {"center_name": "Changed center", "color_attribution_arrows": True}})
        with patch("liquid_tracer.pegouts.load_services", side_effect=AssertionError("Current rules must not be read")), \
                patch.object(Esplora, "get", side_effect=AssertionError("Snapshot must not fetch")):
            saved_graph, saved_plan, state = saved_pegout_snapshot(self.case, result["preview_id"])
        self.assertEqual((saved_graph, saved_plan), (graph, plan))
        self.assertEqual(state, self.state(result))
        self.assertEqual(saved_graph["pegouts"]["match_count"], 1)
        with patch("liquid_tracer.miro.publish") as publish:
            with self.assertRaisesRegex(TraceError, "controls changed"):
                publish_pegouts(self.case, result["preview_id"], "separate-board")
            publish.assert_not_called()
        self.assertEqual(before, {str(path): path.read_bytes() for directory in (archive, preview)
                                  for path in directory.rglob("*") if path.is_file()})

    def test_historical_snapshot_rejects_changed_archive_even_when_resealed(self):
        result = self.search()
        archive = self.case / "pegouts" / result["search_id"]
        state = self.state(result)
        state["stop_reason"] = "changed"
        save_json(archive / "trace.json", state)
        manifest = archive / "SHA256SUMS"
        names = [line.split("  ", 1)[1] for line in manifest.read_text().splitlines()]
        manifest.write_text("".join(digest((archive / name).read_bytes()) + "  " + name + "\n" for name in names))
        with self.assertRaisesRegex(TraceError, "source archive changed"):
            saved_pegout_snapshot(self.case, result["preview_id"])

    def test_stale_search_listing_exposes_only_newest_intact_export_preview(self):
        original = self.search()
        newer = preview_pegouts(self.case, original["search_id"])
        latest = preview_pegouts(self.case, original["search_id"])
        # A broken newest preview must not hide the preceding intact snapshot.
        (Path(latest["directory"]) / "transactions.csv").write_text("changed\n")
        set_service(self.case, "SYNTHETIC-branch-A", name="Changed", stop_tracing=True)
        listed = next(row for row in list_pegout_searches(self.case) if row["id"] == original["search_id"])
        self.assertEqual(listed["export_preview_id"], newer["preview_id"])
        self.assertNotIn("preview_id", listed)
        self.assertNotIn("match_count", listed)
        with patch("liquid_tracer.miro.publish") as publish:
            with self.assertRaisesRegex(TraceError, "controls changed"):
                publish_pegouts(self.case, listed["export_preview_id"], "separate-board")
            publish.assert_not_called()
        refreshed = preview_pegouts(self.case, original["search_id"])
        listed = next(row for row in list_pegout_searches(self.case) if row["id"] == original["search_id"])
        self.assertEqual(listed["preview_id"], refreshed["preview_id"])
        self.assertNotIn("export_preview_id", listed)

    def test_historical_snapshot_requires_report_and_graph_agreement(self):
        result = self.search()
        preview = Path(result["directory"])
        report = read_json(preview / "pegouts.json")
        report["match_count"] += 1
        save_json(preview / "pegouts.json", report)
        manifest = preview / "SHA256SUMS"
        names = [line.split("  ", 1)[1] for line in manifest.read_text().splitlines()]
        manifest.write_text("".join(digest((preview / name).read_bytes()) + "  " + name + "\n" for name in names))
        with self.assertRaisesRegex(TraceError, "plan disagree"):
            saved_pegout_snapshot(self.case, result["preview_id"])

    def test_center_group_changes_preview_without_refetching_and_invalidates_review(self):
        original = self.search()
        previous, _ = reviewed_pegouts(self.case, original["preview_id"])
        archive = self.case / "pegouts" / original["search_id"]
        snapshot = {p.name: p.read_bytes() for p in archive.iterdir() if p.is_file()}
        update_case(self.case, {"run_defaults": {"center_name": "Example Exchange"}})
        with self.assertRaisesRegex(TraceError, "changed"):
            reviewed_pegouts(self.case, original["preview_id"])
        with patch.object(Esplora, "get", side_effect=AssertionError("preview fetched")):
            result = preview_pegouts(self.case, original["search_id"])
        graph, _ = reviewed_pegouts(self.case, result["preview_id"])
        self.assertEqual(result["center_name"], "Example Exchange")
        self.assertEqual(graph["graph_options"]["center_name"], "Example Exchange")
        self.assertEqual(graph["edges"], previous["edges"])
        self.assertEqual(graph["pegouts"], previous["pegouts"])
        self.assertEqual(snapshot, {p.name: p.read_bytes() for p in archive.iterdir() if p.is_file()})

    def test_new_policy_resumes_old_capped_search_without_changing_attribution_limit(self):
        from liquid_tracer.hop_limits import HopScope
        from liquid_tracer.trace import trace

        def legacy_scope(state, **kwargs):
            return HopScope(state, respect_attribution_hops=True)

        def legacy_trace(*args, **kwargs):
            with patch("liquid_tracer.hop_limits.HopScope", side_effect=legacy_scope):
                trace(*args, **kwargs)
            args[1].pop("collection_policy", None)

        set_service(self.case, "SYNTHETIC-branch-A", name="Service", hop_limit=0, stop_tracing=False)
        with patch("liquid_tracer.pegouts.trace", side_effect=legacy_trace):
            first = self.search()
        self.assertEqual(first["match_count"], 0)
        self.assertNotIn(D, self.state(first)["transactions"])
        directory = self.case / "pegouts" / first["search_id"]
        old_bytes = {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
        second = search_pegouts(self.case, resume=first["search_id"], max_transactions=20,
                                max_requests=100, max_outpoints=100)
        self.assertEqual(second["match_count"], 1)
        self.assertEqual(old_bytes, {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()})

    def test_unconfirmed_origin_pegout_is_excluded(self):
        data = read_json(self.fixture)
        data["/tx/" + D]["status"] = {"confirmed": False}
        save_json(self.fixture, data)
        result = search_pegouts(self.case, D, 0, 0)
        self.assertEqual(result["match_count"], 0)
        self.assertFalse(self.state(result)["include_unconfirmed"])

    def test_current_latest_run_and_source_archive_stay_unchanged(self):
        from tests.test_connections import saved_case
        first = self.search()
        _, archive = saved_case(self.case, self.state(first))
        before = (self.case / "case.json").read_bytes()
        archived = {p.name: p.read_bytes() for p in archive.iterdir()}
        second = search_pegouts(self.case, B, 0, 2, max_transactions=20, max_requests=100)
        self.assertEqual(second["match_count"], 1)
        self.assertEqual((self.case / "case.json").read_bytes(), before)
        self.assertEqual(archived, {p.name: p.read_bytes() for p in archive.iterdir()})
        self.assertEqual(self.state(second)["seeds"], [B + ":0", B + ":1", B + ":2"])

    def test_changed_fixture_source_cannot_resume_old_search(self):
        first = self.search()
        data = read_json(self.fixture)
        data["/tx/" + D]["status"]["block_height"] += 1
        save_json(self.fixture, data)
        with self.assertRaisesRegex(TraceError, "source does not match"):
            search_pegouts(self.case, resume=first["search_id"])

    def test_trace_error_is_archived_and_explained_in_terminal(self):
        stderr = io.StringIO()
        with patch.object(Esplora, "get", side_effect=TraceError("Explorer HTTP 503")), redirect_stderr(stderr):
            result = self.search()
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.state(result)["errors"], ["Explorer HTTP 503"])
        self.assertIn("Peg-out search: Explorer HTTP 503", stderr.getvalue())
        self.assertEqual(result["match_count"], 0)

    def test_search_progress_phases_and_callback_failure_do_not_lose_results(self):
        events = []
        result = self.search(progress=events.append)
        self.assertEqual([event["phase"] for event in events], ["pegout_search", "pegout_paths"])
        self.assertEqual(result["match_count"], 1)
        result = self.search(progress=lambda _: (_ for _ in ()).throw(ValueError("advisory")))
        self.assertEqual(result["match_count"], 1)

    def test_publish_protects_main_and_existing_board_mappings(self):
        result = self.search()
        with patch("liquid_tracer.miro.publish") as publish:
            with self.assertRaisesRegex(TraceError, "protected"):
                publish_pegouts(self.case, result["preview_id"], "primary-board")
            for prefix in ("", "connections-"):
                target = "already-used-" + prefix
                mapping = self.case / "miro" / (prefix + digest(target.encode())[:24] + ".json")
                save_json(mapping, {})
                with self.assertRaisesRegex(TraceError, "protected"):
                    publish_pegouts(self.case, result["preview_id"], target)
            publish.assert_not_called()

    def test_publish_uses_separate_snapshot_mapping_and_keeps_case(self):
        result = self.search()
        before = (self.case / "case.json").read_bytes()
        with patch("liquid_tracer.miro.publish", return_value={"items": 12}) as publish:
            self.assertEqual(publish_pegouts(self.case, result["preview_id"], "separate-board", max_items=90), {"items": 12})
            self.assertEqual(publish.call_args.args[2],
                self.case / "miro" / ("pegouts-" + digest(b"separate-board")[:24] + ".json"))
            self.assertEqual(publish.call_args.kwargs["max_items"], 90)
        self.assertEqual((self.case / "case.json").read_bytes(), before)

    def test_symlinks_invalid_budgets_and_query_overrides_fail_closed(self):
        result = self.search()
        with self.assertRaises(TraceError):
            search_pegouts(self.case, resume=result["search_id"], txid=A)
        for value in (True, -1, 1.5):
            with self.subTest(value=value), self.assertRaises(TraceError):
                self.search(max_transactions=value)
        link = self.case / "previews" / ("0" * 16 + "-pegouts-12345678")
        link.symlink_to(Path(result["directory"]), target_is_directory=True)
        with self.assertRaisesRegex(TraceError, "symbolic"):
            reviewed_pegouts(self.case, link.name)

    def test_explicit_zero_budgets_find_endpoints_within_the_requested_hops(self):
        result = self.search(max_transactions=0, max_outpoints=0, max_requests=0, max_seconds=0)
        state = self.state(result)
        self.assertEqual(state["status"], "bounded_complete")
        self.assertEqual(set(state["transactions"]), {A, B, C, D})
        self.assertEqual(state["limits"], {"max_hops": 3, "max_transactions": 0, "max_outpoints": 0,
                                          "max_requests": 0, "max_seconds": 0})


if __name__ == "__main__":
    unittest.main()
