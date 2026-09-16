"""Visual operations hydrate counts automatically; pure evidence/CSV stays offline."""
import contextlib
import copy
import csv
import fcntl
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.address_counts import (addresses, ensure_counts, _transaction_counts,
    count_credentials_required, public_count_report)
from liquid_tracer.api import Esplora, ENTERPRISE
from liquid_tracer.cli import (main, saved_graph, layout_preview_run, mermaid_run,
    compact_preview_run, sync_run, verify_export, csv_run)
from liquid_tracer.common import TraceError, canonical, read_json, save_json
from liquid_tracer.connections import preview_connections, reviewed_connections
from liquid_tracer.export import build_graph
from liquid_tracer.investigations import create_investigation, read_case, update_case
from tests.fixtures import A, B, fixture
from tests.test_connections import saved_case
from tests.test_attribution_convergence import graph_state


def statistics(address, confirmed=19, mempool=2):
    # Only the two counts are required. No history scan/TXO totals are needed.
    return {"address": address, "chain_stats": {"tx_count": confirmed},
            "mempool_stats": {"tx_count": mempool}}


class AutomaticCountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        data = fixture()
        state = {"transactions": {key[4:]: {"data": value} for key, value in data.items()
                 if key.startswith("/tx/") and not key.endswith("/outspends")}}
        for index, address in enumerate(addresses(state)):
            data["/address/" + address] = statistics(address, index * 100, 0)
        self.fixture = self.root / "fixture.json"; save_json(self.fixture, data)
        self.case = create_investigation(self.root / "cases", "Automatic counts",
                                         fixture=self.fixture, seeds=[A+":0", B+":0"])

    def invoke(self, args):
        output, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(err):
            code = main(args)
        self.assertEqual(code, 0, err.getvalue())
        return json.loads(output.getvalue())

    def trace(self, legacy=False):
        with patch("liquid_tracer.cli.ensure_counts", return_value={}) if legacy else contextlib.nullcontext():
            return self.invoke(["trace", "--case", str(self.case), "--fixture", str(self.fixture),
                               "--seed", A+":0", "--seed", B+":0", "--hops", "2"])

    def snapshot(self):
        return {str(p): p.read_bytes() for p in (self.case / "runs").rglob("*") if p.is_file()}

    def check_counts(self, graph):
        count_nodes = [node for node in graph["nodes"] if node["kind"] == "address"]
        self.assertTrue(count_nodes)
        for node in count_nodes:
            observed = node["details"]["tx_count_observation"]
            self.assertEqual(node["tx_count"], observed["confirmed_tx_count"] + observed["mempool_tx_count"])
            self.assertIsInstance(node["tx_count"], int)

    def test_trace_automatically_populates_snapshot_and_preserves_true_zero(self):
        result = self.trace()
        self.assertGreater(result["address_counts"]["fetched"], 0)
        self.assertEqual(result["address_counts"]["remaining"], 0)
        _, archive, graph = saved_graph(self.case)
        self.check_counts(graph)
        self.assertTrue(any(n.get("tx_count") == 0 for n in graph["nodes"]))
        self.check_counts(read_json(archive / "graph.json"))
        verify_export(archive)

    def test_legacy_elk_preview_fetches_unique_missing_addresses_without_retracing(self):
        self.trace(legacy=True); before = self.snapshot(); requests = []
        get = Esplora.get
        def record(api, endpoint):
            requests.append(endpoint)
            self.assertTrue(endpoint.startswith("/address/"))
            self.assertEqual(endpoint.count("/"), 2)
            return get(api, endpoint)
        with patch.object(Esplora, "get", record):
            result = layout_preview_run(self.case)
        graph = read_json(Path(result["directory"]) / "graph.json")
        self.check_counts(graph)
        self.assertEqual(len(requests), len(set(requests)))
        self.assertEqual(result["address_counts"]["remaining"], 0)
        self.assertEqual(before, self.snapshot())
        with patch.object(Esplora, "get", side_effect=AssertionError("No duplicate fetch")):
            again = layout_preview_run(self.case)
        self.assertEqual(again["address_counts"]["requests_this_lookup"], 0)

    def test_mermaid_and_compact_previews_also_hydrate_missing_counts(self):
        self.trace(legacy=True)
        with patch("liquid_tracer.mermaid.export_mermaid", return_value={"html": "unused"}) as render:
            result = mermaid_run(self.case)
        self.check_counts(render.call_args.args[0])
        self.assertEqual(result["address_counts"]["remaining"], 0)
        (self.case / "address-counts.json").unlink()
        result = compact_preview_run(self.case)
        self.assertGreater(result["address_counts"]["fetched"], 0)
        self.check_counts(read_json(Path(result["directory"]) / "graph.json"))

    def test_ordinary_miro_sync_fetches_then_builds_count_shapes_with_trace_lock_held(self):
        self.trace(legacy=True); before = self.snapshot(); plans = []
        def fake_sync(plan, *args, **kwargs):
            plans.append(copy.deepcopy(plan)); return {"created": 0, "dry_run": kwargs["dry_run"]}
        with (self.case / "trace.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("liquid_tracer.cli.sync", fake_sync):
                result = sync_run(self.case, "latest", "SYNTHETIC=")
        self.assertEqual(result["address_counts"]["remaining"], 0)
        for plan in plans:
            keys = {key for key, item in plan["presentation_items"].items() if item["kind"] == "address_count"}
            labels = [item["body"]["data"]["content"] for item in plan["shapes"] if item["key"] in keys]
            self.assertTrue(labels); self.assertFalse(any("??" in text for text in labels))
        self.assertEqual(before, self.snapshot())

    def test_starter_connections_fetches_only_addresses_in_its_selected_view(self):
        self.trace(legacy=True)
        result = preview_connections(self.case, max_hops=1)
        graph, _ = reviewed_connections(self.case, result["preview_id"])
        self.check_counts(graph)
        visible = {n["details"]["address"] for n in graph["nodes"] if n["kind"] == "address"}
        self.assertEqual(set(read_json(self.case/"address-counts.json")["counts"]), visible)
        self.assertEqual(result["address_counts"]["total"], len(visible))
        with patch.object(Esplora, "get", side_effect=AssertionError("Empty chart is offline")):
            result = preview_connections(self.case, max_hops=0)
        self.assertIsNone(result["address_counts"])

    def test_read_only_and_csv_and_miro_dry_run_do_not_fetch(self):
        self.trace(legacy=True)
        with patch.object(Esplora, "get", side_effect=AssertionError("Offline operation")):
            saved_graph(self.case)
            csv_run(self.case)
            sync_run(self.case, "latest", "SYNTHETIC=", dry_run=True)

    def test_budget_exhaustion_reports_missing_and_next_chart_resumes_automatically(self):
        self.trace(legacy=True)
        update_case(self.case, {"run_defaults": {**read_case(self.case)["run_defaults"], "max_requests": 2}})
        result = layout_preview_run(self.case)
        report = result["address_counts"]
        self.assertEqual(report["fetched"], 2)
        self.assertEqual(report["stop_reason"], "request_limit")
        self.assertGreater(report["remaining"], 0)
        again = layout_preview_run(self.case)["address_counts"]
        self.assertEqual(again["fetched"], 2)
        self.assertEqual(again["known"], 4)


class StatisticsFailureTests(unittest.TestCase):
    def test_statistics_totals_are_explicit_and_never_count_history(self):
        self.assertEqual(_transaction_counts(statistics("SYNTHETIC-address", 1234, 7), "SYNTHETIC-address"), [1234, 7])
        self.assertEqual(_transaction_counts(statistics("SYNTHETIC-address", 0, 0), "SYNTHETIC-address"), [0, 0])
        for value in (None, True, -1, "2", 2.0):
            with self.assertRaises(TraceError):
                _transaction_counts(statistics("SYNTHETIC-address", value, 0), "SYNTHETIC-address")
        with self.assertRaises(TraceError):
            _transaction_counts(statistics("SYNTHETIC-other"), "SYNTHETIC-address")
        with self.assertRaises(TraceError):
            _transaction_counts({"address": "SYNTHETIC-address", "chain_stats": {"tx_count": 2}}, "SYNTHETIC-address")

    def test_failed_address_does_not_erase_successes_and_is_retried_without_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = create_investigation(Path(tmp), "Mock public explorer")
            state = graph_state(); state["source"] = "https://blockstream.info/liquid/api"
            state, _ = saved_case(case, state)
            graph = build_graph(state); wanted = addresses(state); requests = []; failing = [True]
            def transport(method, url, headers, body, timeout):
                self.assertEqual(method, "GET"); self.assertNotIn("Authorization", headers)
                self.assertTrue(url.startswith(state["source"] + "/address/"))
                address = url.rsplit("/", 1)[1]; requests.append(address)
                if address == wanted[0] and failing[0]:
                    return 404, {}, b"not found"
                return 200, {}, canonical(statistics(address, 50, 2))
            with patch("liquid_tracer.api.default_min_interval", return_value=0.000001):
                report = ensure_counts(case, state, graph=graph, transport=transport)
                self.assertEqual(report["failed"], 1); self.assertEqual(report["remaining"], 1)
                self.assertEqual(report["errors"][0]["reason"], "http_failed")
                self.assertEqual(len(requests), len(wanted))
                failing[0] = False; requests.clear()
                report = ensure_counts(case, state, graph=graph, transport=transport)
                self.assertEqual(requests, [wanted[0]])
                self.assertEqual(report["remaining"], 0)
            self.assertTrue(all(n["tx_count"] == 52 for n in graph["nodes"] if n["kind"] == "address"))

    def test_enterprise_credential_preflight_is_read_only_and_skips_cached_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = create_investigation(Path(tmp), "Credentials")
            state = graph_state(); state["source"] = ENTERPRISE; state, archive = saved_case(case, state)
            before = {str(p):p.read_bytes() for p in case.rglob("*") if p.is_file()}
            with patch.object(Esplora, "get", side_effect=AssertionError("No network in launch preflight")):
                self.assertTrue(count_credentials_required(case))
            self.assertEqual(before, {str(p):p.read_bytes() for p in case.rglob("*") if p.is_file()})
            state["address_tx_counts"] = {a: {"address":a, "source":ENTERPRISE, "observed_at":"2026-01-01T00:00:00Z",
                 "confirmed_tx_count":0, "mempool_tx_count":0} for a in addresses(state)}
            saved_case(case, state)
            self.assertFalse(count_credentials_required(case))

    def test_public_report_never_exposes_addresses_provider_messages_or_credentials(self):
        report = {key: 0 for key in ("fetched", "known", "total", "remaining", "failed", "requests_this_lookup")}
        report.update(stop_reason="secret-token", errors=[{"address":"private", "reason":"secret"}], notice="secret")
        cleaned = public_count_report(report)
        self.assertNotIn("secret", json.dumps(cleaned)); self.assertNotIn("private", json.dumps(cleaned))


class CountJobRoutesTests(unittest.TestCase):
    def test_web_visual_jobs_request_credentials_and_keep_cancel_available(self):
        from liquid_tracer.web import LocalServer
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); assets = root / "assets"; assets.mkdir()
            case = create_investigation(root / "cases", "Routes")
            saved_case(case)
            server = LocalServer(root / "cases", assets, port=0)
            try:
                for action in ("layout", "mermaid", "compact", "connections"):
                    with patch("liquid_tracer.address_counts.count_credentials_required", return_value=True), \
                         patch.object(server, "start_job", return_value={}) as launch:
                        server.action(case, read_case(case), {"action":action})
                    self.assertTrue(launch.call_args.kwargs["live"])
                with patch("threading.Thread.start"):
                    job = server.start_job([], action="layout", live=True, case=case)
                self.assertTrue(job["cancellable"])
                self.assertEqual(server.cancel_job(job["id"])["status"], "cancelling")
            finally:
                server.job_thread = None; server.server_close()

    def test_auto_lookup_busy_preserves_trace_and_does_not_duplicate_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = create_investigation(Path(tmp), "Busy counts")
            state, _ = saved_case(case)
            with (case / "address-counts.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(Esplora, "get", side_effect=AssertionError("No duplicate lookup")):
                    report = ensure_counts(case, state)
            self.assertEqual(report["stop_reason"], "lookup_in_progress")
            self.assertEqual(report["requests_this_lookup"], 0)


class CountMenuPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_saved_run_is_reported_without_starting_or_crashing_menu(self):
        from liquid_tracer.menu import create_app
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = create_investigation(root, "No saved run")
            app = create_app(root)
            with patch("liquid_tracer.menu._OfflineCalculation.run") as worker:
                async with app.run_test(size=(120, 70)) as pilot:
                    app.created(case)
                    await pilot.pause()
                    with patch.object(app, "notify") as notify:
                        app.screen.perform((["layout-preview", "--case", str(case)], False))
                    self.assertIn("No latest run", notify.call_args.args[0])
                    await pilot.pause()
                    self.assertFalse(app.busy)
                    self.assertIsNone(app.active_calculation)
                    worker.assert_not_called()


if __name__ == "__main__": unittest.main()
