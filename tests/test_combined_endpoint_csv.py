"""Cross-investigation endpoint exports keep exact saved scopes and provenance."""
import csv
import fcntl
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from liquid_tracer.combined_endpoint_csv import FIELDS, build_combined_endpoint_csv
from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.investigations import create_investigation, read_case
from liquid_tracer.pegout_csv import ENDPOINT_TABLE_FIELDS
from liquid_tracer.plot_csv import build_plot_csv
from liquid_tracer.plots import preview_plot
from liquid_tracer.services import set_service
from tests.test_attribution_convergence import graph_state, tx
from tests.test_connections import saved_case
from tests.test_pegout_csv import set_value
from tests.test_pegout_paths import add_pegout, add_unspendable, mark_unspent
from tests import test_web


class CombinedEndpointCSVTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cases = {}
        layout = patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph)
        layout.start()
        self.addCleanup(layout.stop)

    def case(self, name="Endpoint case", *, collect=True, plot=True, stamp="2026-10-01T01:00:00Z", **options):
        case = create_investigation(self.root, name)
        identity = read_case(case)["case_id"]
        self.cases[identity] = case
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        set_value(state, add_pegout(state, tx("b")), 125_000_001)
        mark_unspent(state, tx("b") + ":0")
        add_unspendable(state, tx("b"))
        if collect:
            saved_case(case, state)
        result = None
        if plot and collect:
            with patch("liquid_tracer.plots.now", return_value=stamp):
                result = preview_plot(case, "pegouts", **options)
        return case, {"case_id": identity, "run_id": "0123456789abcdef" if collect else "latest"}, result

    def resolve(self, identity):
        case = self.cases[identity]
        return case, read_case(case)

    def build(self, selections):
        return build_combined_endpoint_csv(selections, self.resolve)

    @staticmethod
    def rows(product):
        return list(csv.DictReader(io.StringIO(product["csv"])))

    def test_cumulative_limit_provenance_is_exported_without_affecting_unlimited_plot(self):
        _, limited, _ = self.case("Limited", pegout_lbtc_limit="1")
        _, unlimited, _ = self.case("Unlimited")
        rows = self.rows(self.build([limited, unlimited]))
        first, second = rows
        self.assertEqual(first["Pegout L-BTC Limit"], "1")
        self.assertEqual(first["Selected Pegout L-BTC"], "1.25000001")
        self.assertEqual(first["Pegout Limit Excess L-BTC"], "0.25000001")
        self.assertEqual(first["Pegout Limit Stop Reason"], "limit_reached")
        self.assertEqual(second["Pegout L-BTC Limit"], "")
        self.assertEqual(second["Pegout Limit Stop Reason"], "")

    def test_combines_only_selected_cases_and_preserves_shared_endpoints_per_case(self):
        first, a, plot_a = self.case("First", include_unspent=True, include_unspendable=True)
        second, b, plot_b = self.case("Second")
        self.case("Closed case")
        with patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("No Explorer")), \
                patch("liquid_tracer.miro.sync", side_effect=AssertionError("No Miro")), \
                patch("liquid_tracer.elk_layout.optimize_graph", side_effect=AssertionError("No layout")):
            product = self.build([a, b])
        rows = self.rows(product)
        self.assertEqual(product["endpoint_count"], 4)
        self.assertEqual([item["endpoint_count"] for item in product["included"]], [3, 1])
        self.assertEqual(product["skipped"], [])
        self.assertEqual(tuple(rows[0]), FIELDS)
        self.assertEqual(FIELDS[:len(ENDPOINT_TABLE_FIELDS)], ENDPOINT_TABLE_FIELDS)
        self.assertEqual([row["Investigation Name"] for row in rows], ["First"] * 3 + ["Second"])
        self.assertEqual({row["Status"] for row in rows}, {"Pegout", "Dormant", "OP_Return"})
        pegouts = [row for row in rows if row["Status"] == "Pegout"]
        self.assertEqual(pegouts[0]["Outpoint"], pegouts[1]["Outpoint"])
        self.assertEqual([row["Pegout LBTC"] for row in pegouts], ["1.25000001"] * 2)
        for case, selection, plot in ((first, a, plot_a), (second, b, plot_b)):
            original = list(csv.DictReader(io.StringIO(build_plot_csv(case, plot["id"], "endpoints.csv")["data"].decode())))
            selected = [row for row in rows if row["Investigation ID"] == selection["case_id"]]
            self.assertEqual([{key: row[key] for key in ENDPOINT_TABLE_FIELDS} for row in selected], original)
            self.assertTrue(all(row["Hops from Seed"] == "1" for row in selected))
            self.assertTrue(all(row["Plot ID"] == plot["id"] for row in selected))

    def test_latest_completed_matching_plot_is_used_even_when_empty(self):
        case, selection, old = self.case()
        with patch("liquid_tracer.plots.now", return_value="2026-10-01T02:00:00Z"):
            latest = preview_plot(case, "pegouts", max_hops=0)
        with patch("liquid_tracer.plots.now", return_value="2026-10-01T03:00:00Z"):
            preview_plot(case, "full")
        unfinished = case / "previews" / "0123456789abcdef-plots-ffffffff"
        unfinished.mkdir()
        (unfinished / "plot.json").write_text("not finished")
        product = self.build([selection])
        self.assertEqual(product["endpoint_count"], 0)
        self.assertEqual(product["included"][0]["plot_id"], latest["id"])
        self.assertEqual(product["skipped"], [])
        self.assertEqual(self.rows(product), [])

    def test_explicit_run_selection_does_not_follow_new_latest(self):
        case, selection, plot = self.case()
        old_meta = read_case(case)
        save_json(case / "case.json", {**old_meta, "latest_run": "fedcba9876543210"})
        product = self.build([selection])
        self.assertEqual(product["included"][0]["plot_id"], plot["id"])
        with self.assertRaisesRegex(TraceError, "Selected collected run is unavailable"):
            self.build([{**selection, "run_id": "latest"}])

    def test_other_run_plot_is_never_used_and_empty_cases_are_reported(self):
        case, selection, plot = self.case()
        another = case / "runs" / "fedcba9876543210"
        another.mkdir()
        (another / "SHA256SUMS").write_text("")
        _, uncollected, _ = self.case("New case", collect=False)
        _, no_plot, _ = self.case("No plot", plot=False)
        product = self.build([{**selection, "run_id": another.name}, uncollected, no_plot])
        self.assertEqual(product["included"], [])
        self.assertEqual(len(product["skipped"]), 3)
        self.assertEqual(product["skipped"][0]["run_id"], another.name)
        self.assertIn("No completed", product["skipped"][0]["reason"])
        self.assertIsNone(product["skipped"][1]["run_id"])
        self.assertIn("No collected", product["skipped"][1]["reason"])
        self.assertEqual(tuple(csv.reader(io.StringIO(product["csv"])))[0], list(FIELDS))

    def test_new_attribution_controls_do_not_change_the_saved_endpoint_scope(self):
        case, selection, _ = self.case()
        before = self.build([selection])
        set_service(case, "G" * 34, name="New service", stop_tracing=True, hop_limit=0)
        after = self.build([selection])
        self.assertEqual(after, before)

    def test_spreadsheet_formula_protection_in_investigation_names(self):
        _, selection, _ = self.case('=HYPERLINK("https://example.invalid")')
        self.assertEqual(self.rows(self.build([selection]))[0]["Investigation Name"],
                         '\'=HYPERLINK("https://example.invalid")')

    def test_corrupt_latest_plot_fails_instead_of_falling_back_or_partial_download(self):
        first, selection, old = self.case()
        with patch("liquid_tracer.plots.now", return_value="2026-10-01T02:00:00Z"):
            latest = preview_plot(first, "pegouts", max_hops=0)
        (Path(latest["directory"]) / "graph.json").write_text("{}")
        _, other, _ = self.case("Good case")
        with self.assertRaisesRegex(TraceError, "Cannot export endpoints.*Saved plot changed"):
            self.build([other, selection])

    def test_missing_artifact_and_symlink_are_rejected(self):
        for modification in ("missing", "symlink"):
            with self.subTest(modification=modification):
                case, selection, plot = self.case(modification)
                target = Path(plot["directory"]) / "graph.json"
                if modification == "missing":
                    target.unlink()
                else:
                    original = target.read_bytes()
                    target.unlink()
                    elsewhere = self.root / "outside.json"
                    elsewhere.write_bytes(original)
                    target.symlink_to(elsewhere)
                with self.assertRaises(TraceError):
                    self.build([selection])

    def test_all_plot_choices_are_pinned_before_reading_archives(self):
        _, first, _ = self.case("First")
        case, second, old = self.case("Second")
        from liquid_tracer.combined_endpoint_csv import _saved_source
        calls = []
        def source(path, preview_id):
            calls.append(preview_id)
            if len(calls) == 1:
                with patch("liquid_tracer.plots.now", return_value="2026-10-01T03:00:00Z"):
                    preview_plot(case, "pegouts", max_hops=0)
            return _saved_source(path, preview_id)
        with patch("liquid_tracer.combined_endpoint_csv._saved_source", side_effect=source):
            product = self.build([first, second])
        self.assertEqual(product["included"][1]["plot_id"], old["id"])
        self.assertEqual(product["endpoint_count"], 2)

    def test_collection_trace_lock_does_not_block_saved_endpoint_export(self):
        case, selection, _ = self.case()
        with (case / "trace.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.build([selection])["endpoint_count"], 1)

    def test_metadata_writes_are_not_locked_during_archive_validation_or_path_work(self):
        case, selection, _ = self.case()
        from liquid_tracer.combined_endpoint_csv import _saved_source, endpoint_table_rows
        checked = []
        def unlocked(function):
            def call(*args, **kwargs):
                with (case / "case.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    checked.append(function.__name__)
                return function(*args, **kwargs)
            return call
        with patch("liquid_tracer.combined_endpoint_csv._saved_source", side_effect=unlocked(_saved_source)), \
                patch("liquid_tracer.combined_endpoint_csv.endpoint_table_rows", side_effect=unlocked(endpoint_table_rows)):
            self.assertEqual(self.build([selection])["endpoint_count"], 1)
        self.assertEqual(checked, ["_saved_source", "endpoint_table_rows"])

    def test_csv_size_limit_rejects_entire_export(self):
        _, selection, _ = self.case()
        with patch("liquid_tracer.combined_endpoint_csv.MAX_CSV_BYTES", 10):
            with self.assertRaisesRegex(TraceError, "exceeds 64 MiB"):
                self.build([selection])

    def test_validation_precedes_all_case_reads(self):
        invalid = [[], None, [None], [{"case_id": "../other"}],
                   [{"case_id": "a" * 32, "run_id": "../other"}],
                   [{"case_id": "a" * 32, "run_id": 1}],
                   [{"case_id": "a" * 32, "preview_id": "untrusted"}],
                   [{"case_id": "a" * 32}] * 2, [{"case_id": "a" * 32}] * 101]
        for value in invalid:
            with self.subTest(value=value), patch.object(self, "resolve", side_effect=AssertionError("No case read")):
                with self.assertRaises(TraceError):
                    self.build(value)


class CombinedEndpointWebTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    create = test_web.LocalWebTests.create

    def prepare(self):
        _, info = self.create()
        case, _ = self.server.case(info["id"])
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        add_pegout(state, tx("b"))
        saved_case(case, state)
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph):
            plot = preview_plot(case, "pegouts")
        return case, info, plot

    def test_route_exports_while_case_job_runs_without_jobs_or_live_requests(self):
        _, info, plot = self.prepare()
        running = test_web.synthetic_running_job(info["id"])
        self.server.jobs[running["id"]] = running
        with patch.object(self.server, "start_job", side_effect=AssertionError("No worker")), \
                patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("No API")):
            product = self.success("/api/endpoint-exports", {"investigations": [{"case_id": info["id"]}]})
        self.assertEqual(product["included"][0]["plot_id"], plot["id"])
        self.assertEqual(product["endpoint_count"], 1)
        self.assertEqual(self.server.jobs, {running["id"]: running})

    def test_endpoint_work_releases_job_lock_for_polling_and_admission(self):
        _, info, _ = self.prepare()
        entered, release = threading.Event(), threading.Event()
        responses = []
        def build(*args):
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("Export held the job lock")
            return {"csv": "", "included": [], "skipped": [], "endpoint_count": 0}
        with patch("liquid_tracer.combined_endpoint_csv.build_combined_endpoint_csv", side_effect=build):
            thread = threading.Thread(target=lambda: responses.append(self.request(
                "/api/endpoint-exports", {"investigations": [{"case_id": info["id"]}]})))
            thread.start()
            try:
                self.assertTrue(entered.wait(timeout=2))
                acquired = self.server.job_lock.acquire(timeout=.5)
                self.assertTrue(acquired, "Endpoint work must not reserve job admission")
                if acquired:
                    self.server.job_lock.release()
                self.assertEqual(self.request("/api/jobs")[0], 200)
            finally:
                release.set()
                thread.join(timeout=5)
        self.assertEqual(responses[0][0], 200)

    def test_invalid_selection_security_and_unavailable_case_are_rejected(self):
        _, info = self.create()
        body = {"investigations": [{"case_id": info["id"]}]}
        for headers in ({"Origin": "https://attacker.invalid"}, {"X-Liquid-CSRF": "wrong"}):
            self.assertEqual(self.request("/api/endpoint-exports", body, headers=headers)[0], 403)
        for bad in ({}, {**body, "path": "/tmp/other"}, {"investigations": []},
                    {"investigations": [{"case_id": []}]},
                    {"investigations": [{"case_id": info["id"], "run_id": "../secret"}]}):
            self.assertEqual(self.request("/api/endpoint-exports", bad)[0], 400)
        self.assertEqual(self.request("/api/endpoint-exports",
                                     {"investigations": [{"case_id": "f" * 32}]})[0], 404)

    def test_corruption_returns_clear_error_and_no_csv(self):
        _, info, plot = self.prepare()
        (Path(plot["directory"]) / "graph.json").write_text("{}")
        code, product, _ = self.request("/api/endpoint-exports", {"investigations": [{"case_id": info["id"]}]})
        self.assertEqual(code, 400)
        self.assertIn("Saved plot changed", product["error"])
        self.assertNotIn("csv", product)
