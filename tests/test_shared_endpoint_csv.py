"""Combined endpoint downloads pin shared revisions independently of private runs."""
import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.combined_endpoint_csv import build_combined_endpoint_csv
from liquid_tracer.common import TraceError, digest, save_json
from liquid_tracer.export import build_graph
from liquid_tracer.investigations import create_investigation, read_case
from liquid_tracer.miro import make_plan
from liquid_tracer.plots import preview_plot
from tests.test_attribution_convergence import graph_state, tx
from tests.test_connections import saved_case
from tests.test_pegout_paths import add_pegout
from tests import test_shared_collection_web


class SharedEndpointCSVTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = create_investigation(self.root, "Recipient", seeds=[tx("a") + ":0"])
        self.identity = read_case(self.case)["case_id"]
        self.dataset = "d" * 32
        self.shared_run = "1" * 16
        layout = patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph)
        layout.start()
        self.addCleanup(layout.stop)

    def private(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        add_pegout(state, tx("b"))
        saved_case(self.case, state)
        return preview_plot(self.case, "pegouts")

    def shared(self, *, source_run=None, derived_run="2" * 16, maximum=10, stamp="2026-10-01T01:00:00Z"):
        """Seal a case-owned projected-run fixture without touching latest_run."""
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        add_pegout(state, tx("b"))
        state.update(case_id=self.identity, run_id=derived_run, status="bounded_complete", stop_reason=None,
                     limits={"max_hops": 10}, include_unconfirmed=False,
                     collection_source={"schema_version": 1, "kind": "shared", "dataset_id": self.dataset,
                                        "run_id": source_run or self.shared_run, "archive_sha256": "e" * 64,
                                        "projection_seeds": state["seeds"]})
        archive = self.case / "runs" / derived_run
        archive.mkdir(parents=True, exist_ok=True)
        graph = build_graph(state)
        for name, value in (("trace.json", state), ("graph.json", graph), ("miro-plan.json", make_plan(graph))):
            save_json(archive / name, value)
        (archive / "SHA256SUMS").write_text("".join(digest(path.read_bytes()) + "  " + path.name + "\n"
            for path in sorted(archive.iterdir()) if path.name != "SHA256SUMS"))
        with patch("liquid_tracer.plots.now", return_value=stamp):
            return preview_plot(self.case, "pegouts", run_id=derived_run, max_hops=maximum)

    def selection(self, **changes):
        return {"case_id": self.identity, "data_source": "shared", "dataset_id": self.dataset,
                "run_id": self.shared_run, **changes}

    def export(self, selection=None):
        return build_combined_endpoint_csv([selection or self.selection()],
            lambda identity: (self.case, read_case(self.case)))

    def test_shared_choice_uses_case_owned_plot_and_reports_original_source(self):
        private = self.private()
        before = (self.case / "case.json").read_bytes()
        shared = self.shared()
        with patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("No Explorer")), \
                patch("liquid_tracer.shared_collection.load_shared_run", side_effect=AssertionError("Use sealed projection")):
            product = self.export()
        row, = list(csv.DictReader(io.StringIO(product["csv"])))
        self.assertEqual(row["Plot ID"], shared["id"])
        self.assertEqual(row["Run ID"], "2" * 16)
        self.assertEqual(row["Shared Run ID"], self.shared_run)
        self.assertEqual(row["Shared Dataset ID"], self.dataset)
        self.assertEqual(row["Data Source"], "shared")
        self.assertEqual(product["included"][0]["collection_source"],
                         {"kind": "shared", "dataset_id": self.dataset, "run_id": self.shared_run})
        self.assertEqual(self.export({"case_id": self.identity})["included"][0]["plot_id"], private["id"])
        self.assertEqual((self.case / "case.json").read_bytes(), before)

    def test_shared_plot_needs_no_private_collection_and_keeps_empty_latest_match(self):
        self.shared()
        empty = self.shared(maximum=0, stamp="2026-10-01T02:00:00Z")
        self.shared(source_run="3" * 16, derived_run="4" * 16, stamp="2026-10-01T03:00:00Z")
        self.assertIsNone(read_case(self.case).get("latest_run"))
        product = self.export()
        self.assertEqual(product["included"][0]["plot_id"], empty["id"])
        self.assertEqual(product["endpoint_count"], 0)
        self.assertEqual(product["skipped"], [])

    def test_missing_matching_shared_revision_skips_without_private_or_other_source_fallback(self):
        self.private()
        self.shared()
        product = self.export(self.selection(run_id="9" * 16))
        self.assertEqual(product["included"], [])
        skipped, = product["skipped"]
        self.assertEqual(skipped["run_id"], "9" * 16)
        self.assertIn("selected shared snapshot", skipped["reason"])
        other = self.export(self.selection(dataset_id="f" * 32))
        self.assertEqual(other["included"], [])

    def test_damaged_latest_shared_plot_blocks_export_without_fallback(self):
        self.private()
        self.shared()
        last = self.shared(stamp="2026-10-01T02:00:00Z")
        (Path(last["directory"]) / "graph.json").write_text("{}")
        with self.assertRaisesRegex(TraceError, "Cannot export endpoints.*Saved plot changed"):
            self.export()

    def test_shared_selection_validation_finishes_before_case_reads(self):
        for change in ({"dataset_id": None}, {"dataset_id": "../other"}, {"dataset_id": 0},
                       {"run_id": "latest"}, {"run_id": None}, {"run_id": "../other"},
                       {"data_source": "investigation"}, {"data_source": "bad"}):
            with self.subTest(change=change), self.assertRaises(TraceError):
                build_combined_endpoint_csv([self.selection(**change)],
                    lambda identity: self.fail("Selection must be validated before reading cases"))


class SharedEndpointWebTests(unittest.TestCase):
    setUp = test_shared_collection_web.SharedCollectionWebTests.setUp
    close_server = test_shared_collection_web.SharedCollectionWebTests.close_server
    request = test_shared_collection_web.SharedCollectionWebTests.request
    success = test_shared_collection_web.SharedCollectionWebTests.success
    wait = test_shared_collection_web.SharedCollectionWebTests.wait
    create = test_shared_collection_web.SharedCollectionWebTests.create
    investigations = test_shared_collection_web.SharedCollectionWebTests.investigations
    collect = test_shared_collection_web.SharedCollectionWebTests.collect

    def test_shared_collection_plot_and_combined_download_without_private_runs(self):
        first, second, _, _ = self.investigations()
        collected = self.collect(first, second)
        source = {"data_source": "shared", "dataset_id": collected["shared_collection"]["dataset_id"],
                  "run_id": collected["run_id"]}
        identity = read_case(first)["case_id"]
        job = self.success("/api/cases/" + identity + "/actions", {
            "action": "plot", "goal": "pegouts", "min_hops": 0, "max_hops": 3,
            "include_unspent": True, "include_unspendable": True, **source}, 202)
        plot = self.wait(job)
        with patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("No Explorer")), \
                patch.object(self.server, "start_job", side_effect=AssertionError("No worker")):
            product = self.success("/api/endpoint-exports", {"investigations": [
                {"case_id": identity, **source}, {"case_id": read_case(second)["case_id"], **source}]})
        self.assertEqual(product["included"][0]["plot_id"], plot["preview_id"])
        self.assertEqual(product["included"][0]["collection_source"]["run_id"], collected["run_id"])
        self.assertEqual(product["skipped"][0]["case_id"], read_case(second)["case_id"])
        self.assertGreater(product["endpoint_count"], 0)
        self.assertTrue(all(row["Data Source"] == "shared" and row["Shared Run ID"] == collected["run_id"]
                            for row in csv.DictReader(io.StringIO(product["csv"]))))
        self.assertIsNone(read_case(first).get("latest_run"))
        self.assertIsNone(read_case(second).get("latest_run"))


if __name__ == "__main__":
    unittest.main()
