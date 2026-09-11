import contextlib
import fcntl
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.address_review import inspect_case_address, list_addresses, saved_activity
from liquid_tracer.cli import main, saved_graph, verify_export
from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.investigations import create_investigation, read_case
from liquid_tracer.services import load_services, set_service
from tests.fixtures import A, B, C, fixture


ADDRESS = "SYNTHETIC-branch-A"


class AddressReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        data = fixture()
        data["/address/" + ADDRESS] = {"address": ADDRESS,
            "chain_stats": {"tx_count": 2, "funded_txo_count": 2, "spent_txo_count": 1},
            "mempool_stats": {"tx_count": 0, "funded_txo_count": 0, "spent_txo_count": 0}}
        data["/address/" + ADDRESS + "/txs/chain"] = [data["/tx/" + C], data["/tx/" + B]]
        self.fixture = self.root / "api.json"
        save_json(self.fixture, data)
        self.case = create_investigation(self.root / "cases", "Synthetic review", fixture=self.fixture, seeds=[A + ":0"])

    def invoke(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(arguments)
        self.assertEqual(code, 0, stderr.getvalue())
        return json.loads(stdout.getvalue())

    def trace(self, *extra):
        return self.invoke(["trace", "--case", str(self.case), "--fixture", str(self.fixture), *extra])

    def initial(self, hops="2"):
        report = self.trace("--seed", A + ":0", "--hops", hops)
        return self.case / "runs" / report["run_id"]

    def test_pre_run_service_review_is_local_and_persists_reopen(self):
        self.assertEqual(list_addresses(self.case)["rows"], [])
        self.invoke(["service-set", "--case", str(self.case), "--address", ADDRESS,
                     "--name", "Suspected exchange", "--rationale", "Investigator assessment"])
        rows = list_addresses(self.case, suspected_only=True)["rows"]
        self.assertEqual(rows[0]["address"], ADDRESS)
        self.assertEqual(rows[0]["run_output_count"], 0)
        self.assertTrue(load_services(self.case)["rules"][ADDRESS]["enabled"])
        self.assertFalse((self.case / "evidence.sqlite").exists())
        self.invoke(["service-set", "--case", str(self.case), "--address", ADDRESS, "--disable"])
        disabled = load_services(self.case)["rules"][ADDRESS]
        self.assertFalse(disabled["enabled"])
        self.assertEqual(disabled["name"], "Suspected exchange")
        self.assertEqual(disabled["rationale"], "Investigator assessment")

    def test_catalog_counts_distinct_observed_outpoints_and_paginates(self):
        self.initial()
        first = list_addresses(self.case, limit=2)
        second = list_addresses(self.case, offset=2, limit=2)
        self.assertGreater(first["total"], 2)
        self.assertFalse({r["address"] for r in first["rows"]} & {r["address"] for r in second["rows"]})
        matching = list_addresses(self.case, query=ADDRESS)
        row = next(r for r in matching["rows"] if r["address"] == ADDRESS)
        self.assertEqual(row["run_output_count"], 2)  # B:0 and C:0; input/output occurrences deduplicate.

    def test_refresh_is_saved_separately_and_old_inspections_and_trace_remain_intact(self):
        archive = self.initial()
        trace_before = (archive / "trace.json").read_bytes()
        one = self.invoke(["address-inspect", "--case", str(self.case), "--address", ADDRESS])
        first_file = self.case / "address-reviews" / (one["inspection_id"] + ".json")
        first_bytes = first_file.read_bytes()
        two = inspect_case_address(self.case, ADDRESS)
        self.assertNotEqual(one["inspection_id"], two["inspection_id"])
        self.assertEqual(saved_activity(self.case, ADDRESS), two)
        self.assertEqual(first_file.read_bytes(), first_bytes)
        self.assertEqual((archive / "trace.json").read_bytes(), trace_before)
        verify_export(archive)
        self.assertEqual(two["confirmed_tx_count"], 2)
        self.assertEqual(two["unspent_output_count"], 1)
        self.assertTrue(two["history_complete"])
        self.assertTrue(two["observation_ids"])

    def test_saved_activity_and_list_do_not_fetch_again(self):
        self.initial()
        result = inspect_case_address(self.case, ADDRESS)
        with patch("liquid_tracer.address_review.Esplora", side_effect=AssertionError("Unexpected API access")):
            self.assertEqual(saved_activity(self.case, ADDRESS), result)
            self.assertEqual(next(r for r in list_addresses(self.case)["rows"] if r["address"] == ADDRESS)["activity"], result)

    def test_altered_report_is_not_presented_as_saved_evidence(self):
        report = inspect_case_address(self.case, ADDRESS)
        path = self.case / "address-reviews" / (report["inspection_id"] + ".json")
        path.write_text("{}")
        with self.assertRaisesRegex(TraceError, "checksum"):
            saved_activity(self.case, ADDRESS)

    def test_changed_fixture_cannot_query_a_different_source(self):
        self.initial()
        data = read_json(self.fixture)
        data["/address/" + ADDRESS]["chain_stats"]["tx_count"] = 3
        save_json(self.fixture, data)
        with self.assertRaisesRegex(TraceError, "source does not match"):
            inspect_case_address(self.case, ADDRESS)

    def test_selected_source_filter_is_shared_by_list_and_detail(self):
        self.initial()
        inspect_case_address(self.case, ADDRESS)
        with patch("liquid_tracer.address_review._run_catalog", return_value=("saved", {}, "fixture://different-source")):
            self.assertIsNone(saved_activity(self.case, ADDRESS, "saved"))
            row = next(r for r in list_addresses(self.case, "saved")["rows"] if r["address"] == ADDRESS)
            self.assertIsNone(row["activity"])

    def test_invalid_parameters_and_active_trace_reject_before_lookup(self):
        with patch("liquid_tracer.address_review.Esplora", side_effect=AssertionError("Unexpected API access")):
            for kwargs in ({"max_pages": 0}, {"max_requests": False}, {"max_seconds": float("inf")}):
                with self.subTest(kwargs=kwargs), self.assertRaises(TraceError):
                    inspect_case_address(self.case, ADDRESS, **kwargs)
            with (self.case / "trace.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(TraceError, "running"):
                    inspect_case_address(self.case, ADDRESS)

    def test_service_added_after_expansion_holds_exclusive_frontier_and_disable_resumes(self):
        archive = self.initial()
        before = (archive / "trace.json").read_bytes()
        self.invoke(["service-set", "--case", str(self.case), "--address", "SYNTHETIC-victim-deposit",
                     "--name", "Suspected service", "--rationale", "Synthetic investigator decision"])
        continued = self.trace("--resume", "latest", "--additional-hops", "1")
        state = read_json(self.case / "runs" / continued["run_id"] / "trace.json")
        self.assertEqual(state["stats"]["requests_this_run"], 0)
        self.assertGreater(state["stats"]["held_behind_service_outputs"], 0)
        self.assertEqual(state["service_controls"]["revision"], 1)
        self.assertEqual((archive / "trace.json").read_bytes(), before)
        set_service(self.case, "SYNTHETIC-victim-deposit", enabled=False)
        resumed = self.trace("--resume", "latest", "--additional-hops", "0")
        self.assertGreater(resumed["stats"]["new_transactions_this_run"], 0)

    def test_current_designation_refreshes_saved_graph_without_rewriting_snapshot(self):
        archive = self.initial()
        before = (archive / "trace.json").read_bytes()
        set_service(self.case, ADDRESS, name="Possible exchange", rationale="Synthetic reasoning")
        _, _, graph = saved_graph(self.case)
        addresses = [node for node in graph["nodes"] if node["details"].get("address") == ADDRESS]
        self.assertTrue(addresses)
        self.assertTrue(all("Suspected service" in node["label"] for node in addresses))
        self.assertEqual((archive / "trace.json").read_bytes(), before)
        set_service(self.case, ADDRESS, enabled=False)
        _, _, updated = saved_graph(self.case)
        self.assertFalse(any("Suspected service" in node["label"] for node in updated["nodes"]))
        verify_export(archive)
