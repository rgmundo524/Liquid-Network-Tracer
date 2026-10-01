"""Shared evidence remains immutable while each case selects its own ancestry."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from copy import deepcopy
import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.cli import parser, run_trace, verify_export
from liquid_tracer.common import TraceError, digest, read_json, save_json
from liquid_tracer.investigation_boards import generate_and_sync, list_boards
from liquid_tracer.investigations import create_investigation, read_case, update_case
from liquid_tracer.plot_csv import build_plot_csv
from liquid_tracer.plots import preview_plot, reviewed_plot
from liquid_tracer.services import set_service
from liquid_tracer.shared_collection import collect_prepared, dataset_path, prepare_collection
from liquid_tracer.shared_projection import materialize_shared_run
from tests.test_attribution_convergence import graph_state, tx
from tests.test_pegout_paths import add_pegout
from tests.test_board_workflow import BoardRemote


class SharedProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        state = graph_state((("a:0", "c"), ("c:0", "b"), ("a:1", "d"), ("f:0", "e")),
                            seeds=("a:0", "a:1", "b:0", "f:0"))
        self.endpoint = add_pegout(state, tx("b"))
        fixture = {}
        for txid, record in state["transactions"].items():
            fixture["/tx/" + txid] = record["data"]
            fixture["/tx/" + txid + "/outspends"] = [
                ({"spent": True, "txid": state["links"][key]["spending_txid"],
                  "vin": state["links"][key]["vin"], "status": {"confirmed": True}}
                 if key in state["links"] else {"spent": False})
                for key in (f"{txid}:{index}" for index in range(len(record["data"]["vout"])))
            ]
        self.fixture = self.root / "fixture.json"
        save_json(self.fixture, fixture)
        self.case = create_investigation(self.root, "Active", fixture=self.fixture, seeds=[tx("a") + ":0"],
                                         run_defaults={"layout_attempts": 1})
        self.other = create_investigation(self.root, "Other", fixture=self.fixture,
                                          seeds=[tx("a") + ":1", tx("b") + ":0", tx("f") + ":0"])
        self.layout = patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kw: graph)
        self.layout.start(); self.addCleanup(self.layout.stop)

    def collect(self, *, stop=False):
        if stop:
            set_service(self.case, "SYNTHETIC-c-address", name="Collection policy stop", stop_tracing=True)
        request = prepare_collection(self.case, [read_case(self.case)["case_id"], read_case(self.other)["case_id"]],
                                     hops=3, settings={"max_transactions": 100, "max_outpoints": 100,
                                                       "max_requests": 1000, "max_seconds": 60})
        with redirect_stdout(io.StringIO()):
            self.assertEqual(collect_prepared(self.case, request["request_id"]), 0)
        self.dataset = dataset_path(self.case)
        self.shared_id = read_case(self.dataset)["case_id"]
        self.shared_run = read_case(self.dataset)["latest_run"]
        self.source_archive = self.dataset / "runs" / self.shared_run
        self.source = read_json(self.source_archive / "trace.json")

    @staticmethod
    def files(path):
        return {str(file.relative_to(path)): file.read_bytes() for file in path.rglob("*") if file.is_file()}

    def test_projection_uses_active_seeds_and_depths_preserves_observations_and_local_io(self):
        self.collect()
        source_before = self.files(self.source_archive)
        case_before = (self.case / "case.json").read_bytes()
        identity = materialize_shared_run(self.case, self.shared_run, self.shared_id)
        archive = self.case / "runs" / identity
        state = read_json(archive / "trace.json")
        verify_export(archive)
        self.assertEqual(set(state["transactions"]), {tx("a"), tx("b"), tx("c")})
        self.assertEqual(state["seeds"], [tx("a") + ":0"])
        self.assertEqual(state["transactions"][tx("b")]["depth"], 2)
        self.assertEqual(self.source["transactions"][tx("b")]["depth"], 0)
        self.assertNotIn(tx("a") + ":1", state["outputs"])
        self.assertEqual(len(state["transactions"][tx("a")]["data"]["vout"]), 2)
        for txid, record in state["transactions"].items():
            self.assertEqual(record["data"], self.source["transactions"][txid]["data"])
            self.assertEqual(record["observation_id"], self.source["transactions"][txid]["observation_id"])
        self.assertEqual(state["labels"], [])
        self.assertNotIn("service_controls", state)
        self.assertEqual(state["collection_source"]["run_id"], self.shared_run)
        self.assertEqual(state["collection_source"]["projection_seeds"], state["seeds"])
        source_index = {row["id"]: row for row in read_json(self.source_archive / "evidence-index.json")}
        for row in read_json(archive / "evidence-index.json"):
            self.assertEqual(row, source_index[row["id"]])
            self.assertEqual((archive / row["file"]).read_bytes(), (self.source_archive / row["file"]).read_bytes())
        self.assertEqual(self.files(self.source_archive), source_before)
        self.assertEqual((self.case / "case.json").read_bytes(), case_before)
        self.assertNotIn("latest_run", read_case(self.case))

    def test_known_saved_vin_can_recover_a_stopped_link_without_inventing_observations(self):
        self.collect(stop=True)
        key = tx("c") + ":0"
        self.assertNotIn(key, self.source["links"])
        set_service(self.case, "SYNTHETIC-c-address", name="Recipient label", stop_tracing=False)
        identity = materialize_shared_run(self.case, self.shared_run)
        state = read_json(self.case / "runs" / identity / "trace.json")
        self.assertEqual(state["links"][key]["relationship"], "saved_transaction_input")
        self.assertNotIn("observation_id", state["links"][key])
        self.assertNotIn("spend_observation_id", state["outputs"][key])
        plot = preview_plot(self.case, "pegouts", self.shared_run, data_source="shared", dataset_id=self.shared_id)
        graph, _ = reviewed_plot(self.case, plot["preview_id"])
        self.assertEqual([match["outpoint"] for match in graph["pegouts"]["matches"]], [self.endpoint])
        self.assertFalse(any("Collection policy stop" in node["label"] for node in graph["nodes"]))

    def test_current_recipient_rules_scope_full_and_pegout_plots_without_mutating_raw_projection(self):
        self.collect()
        identity = materialize_shared_run(self.case, self.shared_run)
        original = self.files(self.case / "runs" / identity)
        set_service(self.case, "SYNTHETIC-c-address", name="Recipient boundary", hop_limit=0, stop_tracing=False)
        full = preview_plot(self.case, "full", self.shared_run, data_source="shared")
        full_graph, _ = reviewed_plot(self.case, full["preview_id"])
        self.assertNotIn("tx:" + tx("b"), {node["id"] for node in full_graph["nodes"]})
        pegout = preview_plot(self.case, "pegouts", self.shared_run, data_source="shared")
        data = build_plot_csv(self.case, pegout["preview_id"], "endpoints.csv")["data"]
        rows = list(csv.DictReader(io.StringIO(data.decode())))
        self.assertEqual([row["Outpoint"] for row in rows], [self.endpoint])
        self.assertEqual(rows[0]["Hops from Seed"], "2")
        self.assertTrue(rows[0]["Transaction Observed At"])
        set_service(self.case, "SYNTHETIC-c-address", stop_tracing=True)
        stopped = preview_plot(self.case, "pegouts", self.shared_run, data_source="shared")
        self.assertTrue(stopped["empty"])
        self.assertEqual(build_plot_csv(self.case, pegout["preview_id"], "endpoints.csv")["data"], data)
        self.assertEqual(self.files(self.case / "runs" / identity), original)

    def test_named_hops_use_recipient_group_and_keep_seed_distance_separate(self):
        self.collect()
        set_service(self.case, "SYNTHETIC-c-address", name="Recipient group", stop_tracing=False)
        update_case(self.case, {"run_defaults": {"hop_reference_name": "Recipient group", "layout_attempts": 1}})
        plot = preview_plot(self.case, "pegouts", self.shared_run, max_hops=1, data_source="shared")
        graph, _ = reviewed_plot(self.case, plot["preview_id"])
        self.assertEqual(graph["plot"]["query"]["hop_reference_name"], "Recipient group")
        rows = list(csv.DictReader(io.StringIO(build_plot_csv(self.case, plot["preview_id"], "endpoints.csv")["data"].decode())))
        self.assertEqual(rows[0]["Hops from Seed"], "2")
        self.assertEqual(rows[0]["Hop Counts"], "1")

    def test_sealed_projection_and_plot_survive_shared_source_removal_and_rule_changes(self):
        self.collect()
        plot = preview_plot(self.case, "pegouts", self.shared_run, data_source="shared")
        source = deepcopy(plot["collection_source"])
        before = build_plot_csv(self.case, plot["preview_id"], "endpoints.csv")["data"]
        self.dataset.rename(self.root / "archived-shared-source")
        set_service(self.case, "SYNTHETIC-c-address", stop_tracing=True)
        graph, plan = reviewed_plot(self.case, plot["preview_id"])
        self.assertEqual(graph["plot"]["collection_source"], source)
        self.assertEqual(plan["namespace"]["case_id"], read_case(self.case)["case_id"])
        self.assertEqual(build_plot_csv(self.case, plot["preview_id"], "endpoints.csv")["data"], before)

    def test_concurrent_materialization_is_deterministic_and_cannot_be_resumed_as_private(self):
        self.collect()
        with ThreadPoolExecutor(max_workers=2) as pool:
            identities = list(pool.map(lambda _: materialize_shared_run(self.case, self.shared_run), range(2)))
        self.assertEqual(identities[0], identities[1])
        args = parser().parse_args(["trace", "--case", str(self.case), "--resume", identities[0],
                                    "--fixture", str(self.fixture)])
        with self.assertRaisesRegex(TraceError, "shared|Shared"):
            run_trace(args)
        self.assertNotIn("latest_run", read_case(self.case))

    def test_missing_seed_or_wrong_dataset_fails_without_publishing_a_projection(self):
        self.collect()
        with self.assertRaises(TraceError):
            materialize_shared_run(self.case, self.shared_run, "f" * 32)
        metadata = read_case(self.case)
        save_json(self.case / "case.json", {**metadata, "seeds": [tx("9") + ":0"]})
        with self.assertRaisesRegex(TraceError, "missing a selected starting transaction"):
            materialize_shared_run(self.case, self.shared_run)
        self.assertEqual(list((self.case / "runs").glob("*/SHA256SUMS")), [])

    def test_generate_and_sync_keeps_shared_source_and_active_investigation_board_identity(self):
        self.collect()
        remote = BoardRemote()
        result = generate_and_sync(self.case, "pegouts", self.shared_run, data_source="shared",
                                   dataset_id=self.shared_id, token="test", transport=remote, interval=0)
        self.assertTrue(result["published"])
        graph, plan = reviewed_plot(self.case, result["preview_id"])
        self.assertEqual(graph["plot"]["collection_source"]["dataset_id"], self.shared_id)
        self.assertEqual(graph["plot"]["collection_source"]["run_id"], self.shared_run)
        self.assertEqual(plan["namespace"]["case_id"], read_case(self.case)["case_id"])
        board, = list_boards(self.case)
        self.assertEqual(board["run_id"], graph["run_id"])
        self.assertNotIn("latest_run", read_case(self.case))

    def test_observation_mismatch_fails_before_atomic_projection_publication(self):
        self.collect()
        path = self.source_archive / "evidence-index.json"
        rows = read_json(path)
        oid = self.source["transactions"][tx("b")]["observation_id"]
        next(row for row in rows if row["id"] == oid)["endpoint"] = "/tx/" + tx("d")
        save_json(path, rows)
        names = [line.split("  ", 1)[1] for line in (self.source_archive / "SHA256SUMS").read_text().splitlines()]
        (self.source_archive / "SHA256SUMS").write_text("".join(
            digest((self.source_archive / name).read_bytes()) + "  " + name + "\n" for name in names))
        with self.assertRaisesRegex(TraceError, "transaction disagrees with its original observation"):
            materialize_shared_run(self.case, self.shared_run)
        self.assertEqual(list((self.case / "runs").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
