"""Indexed shared plots preserve native paths, evidence and CSV exports."""

from contextlib import redirect_stdout
import csv
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from liquid_tracer.api import Esplora
from liquid_tracer.cli import parser, run_trace
from liquid_tracer.common import TraceError, digest, read_json, save_json
from liquid_tracer.investigations import read_case, update_case
from liquid_tracer.plot_csv import build_plot_csv
from liquid_tracer.plots import preview_plot, reviewed_plot
from liquid_tracer.services import set_service
from liquid_tracer.shared_projection import materialize_shared_run
from tests.test_attribution_convergence import graph_state, tx
from tests.test_pegout_paths import add_pegout
from tests import test_shared_projection


class SharedSnapshotPlotTests(unittest.TestCase):
    collect = test_shared_projection.SharedProjectionTests.collect
    files = staticmethod(test_shared_projection.SharedProjectionTests.files)

    def setUp(self):
        test_shared_projection.SharedProjectionTests.setUp(self)
        environment = patch.dict("os.environ", {
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_STATE_HOME": str(self.root / "state"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        # The other member seeds b, so its collected descendants reach five
        # hops from this case's a:0 even though the shared budget is three.
        state = graph_state((("a:0", "c"), ("c:0", "b"), ("b:0", "0"),
                             ("0:0", "1"), ("1:0", "2"),
                             ("a:1", "d"), ("f:0", "e")),
                            seeds=("a:0", "a:1", "b:0", "f:0"))
        self.endpoint = add_pegout(state, tx("b"))
        self.boundary_endpoint = add_pegout(state, tx("0"))
        self.beyond_endpoint = add_pegout(state, tx("1"))
        fixture = {}
        for txid, record in state["transactions"].items():
            fixture["/tx/" + txid] = record["data"]
            fixture["/tx/" + txid + "/outspends"] = [
                ({"spent": True, "txid": state["links"][key]["spending_txid"],
                  "vin": state["links"][key]["vin"], "status": {"confirmed": True}}
                 if key in state["links"] else {"spent": False})
                for key in (f"{txid}:{index}" for index in range(len(record["data"]["vout"])))
            ]
        save_json(self.fixture, fixture)
        self.collect()
        self.assertIn(tx("2"), self.source["transactions"])
        self.esplora_get = Esplora.get
        for name in ("liquid_tracer.api.Esplora.get", "liquid_tracer.address_counts.ensure_counts",
                     "liquid_tracer.miro.publish", "liquid_tracer.miro.sync"):
            blocker = patch(name, side_effect=AssertionError("Indexed plots must stay offline"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def select_seeds(self, *names):
        save_json(self.case / "case.json", {**read_case(self.case),
                  "seeds": [tx(name) + ":0" for name in names]})

    def full_plot(self, goal, **query):
        # Explicit default materialization remains the full-projection path.
        run = materialize_shared_run(self.case, self.shared_run, self.shared_id)
        return preview_plot(self.case, goal, run, **query)

    def shared_plot(self, goal, **query):
        return preview_plot(self.case, goal, self.shared_run, data_source="shared",
                            dataset_id=self.shared_id, **query)

    def reseal(self, directory):
        names = [line.split("  ", 1)[1] for line in (directory / "SHA256SUMS").read_text().splitlines()]
        (directory / "SHA256SUMS").write_text("".join(
            digest((directory / name).read_bytes()) + "  " + name + "\n"
            for name in names if (directory / name).is_file()))

    def private_collection(self):
        args = parser().parse_args(["trace", "--case", str(self.case), "--fixture", str(self.fixture),
                                    "--seed", tx("a") + ":0", "--hops", "1", "--max-seconds", "30"])
        with patch.object(Esplora, "get", self.esplora_get), redirect_stdout(io.StringIO()):
            self.assertEqual(run_trace(args), 0)
        return self.case / "runs" / read_case(self.case)["latest_run"]

    def assert_same_plot_evidence(self, expected, actual):
        first, first_plan = reviewed_plot(self.case, expected["preview_id"])
        second, second_plan = reviewed_plot(self.case, actual["preview_id"])
        # Snapshot IDs and coverage provenance can differ; graph evidence,
        # topology, selected paths and native rendering inputs must not.
        for key in ("nodes", "edges", "graph_options", "branch_structure", "activity_frames"):
            self.assertEqual(first.get(key), second.get(key), key)
        goal = first["plot"]["goal"]
        self.assertEqual(first["plot"]["query"], second["plot"]["query"])
        self.assertEqual(first_plan["shapes"], second_plan["shapes"])
        self.assertEqual(first_plan["connectors"], second_plan["connectors"])
        for name in ("transactions.csv", *(
                ("path-transactions.csv", "trace-endpoints.csv") if goal == "pegouts" else ())):
            self.assertEqual((Path(expected["directory"]) / name).read_bytes(),
                             (Path(actual["directory"]) / name).read_bytes(), name)
        if goal == "pegouts":
            self.assertEqual(first["pegouts"], second["pegouts"])
            for name in ("transactions.csv", "endpoints.csv"):
                self.assertEqual(build_plot_csv(self.case, expected["preview_id"], name)["data"],
                                 build_plot_csv(self.case, actual["preview_id"], name)["data"], name)
        if goal == "connections":
            self.assertEqual(first["connections"], second["connections"])
        return second

    def test_three_hop_pegouts_match_full_projection_and_preserve_observations(self):
        before = self.files(self.source_archive)
        expected = self.full_plot("pegouts", max_hops=3)
        actual = self.shared_plot("pegouts", max_hops=3)
        graph = self.assert_same_plot_evidence(expected, actual)
        projected = read_json(self.case / "runs" / actual["run_id"] / "trace.json")
        self.assertEqual(set(projected["transactions"]), {tx(name) for name in ("a", "c", "b", "0")})
        self.assertNotEqual(actual["run_id"], expected["run_id"])
        self.assertEqual({match["outpoint"] for match in graph["pegouts"]["matches"]},
                         {self.endpoint, self.boundary_endpoint})
        self.assertNotIn(self.beyond_endpoint, {match["outpoint"] for match in graph["pegouts"]["matches"]})
        self.assertEqual(self.files(self.source_archive), before)
        self.assertNotIn("latest_run", read_case(self.case))

    def test_three_hop_starter_connections_match_full_projection(self):
        self.select_seeds("a", "b")
        query = {"max_hops": 3, "connection_scope": "hop_limited"}
        graph = self.assert_same_plot_evidence(self.full_plot("connections", **query),
                                              self.shared_plot("connections", **query))
        self.assertGreater(graph["connections"]["connection_count"], 0)

    def test_known_spend_beyond_boundary_does_not_become_an_unspent_endpoint(self):
        query = {"max_hops": 3, "include_unspent": True,
                 "include_unspendable": True, "include_context": True}
        expected = self.full_plot("pegouts", **query)
        actual = self.shared_plot("pegouts", **query)
        self.assert_same_plot_evidence(expected, actual)
        rows = list(csv.DictReader(io.StringIO(
            build_plot_csv(self.case, actual["preview_id"], "endpoints.csv")["data"].decode())))
        self.assertNotIn(tx("0") + ":0", {row["Outpoint"] for row in rows})
        self.assertNotIn(self.beyond_endpoint, {row["Outpoint"] for row in rows})

    def test_all_saved_keeps_connections_beyond_the_requested_display_limit(self):
        self.select_seeds("a", "1")
        finite = self.shared_plot("connections", max_hops=3, connection_scope="hop_limited")
        all_saved = self.shared_plot("connections", max_hops=3, connection_scope="all_saved")
        self.assertTrue(finite["empty"])
        self.assertFalse(all_saved["empty"])
        graph = self.assert_same_plot_evidence(
            self.full_plot("connections", max_hops=3, connection_scope="all_saved"), all_saved)
        self.assertIn("tx:" + tx("0"), {node["id"] for node in graph["nodes"]})

    def test_named_hops_keep_distant_group_relative_matches(self):
        set_service(self.case, "SYNTHETIC-0-address", name="Late group", stop_tracing=False)
        update_case(self.case, {"run_defaults": {"hop_reference_name": "Late group", "layout_attempts": 1}})
        expected = self.full_plot("pegouts", max_hops=3)
        actual = self.shared_plot("pegouts", max_hops=3)
        graph = self.assert_same_plot_evidence(expected, actual)
        self.assertEqual(actual["run_id"], expected["run_id"])
        self.assertIn(self.beyond_endpoint, {match["outpoint"] for match in graph["pegouts"]["matches"]})
        self.assertEqual(graph["plot"]["query"]["hop_reference_name"], "Late group")

    def test_warm_different_scope_reads_only_selected_transaction_bodies(self):
        from liquid_tracer.snapshot_index import SnapshotIndex

        first = self.shared_plot("pegouts", max_hops=3)
        previous_csv = build_plot_csv(self.case, first["preview_id"], "endpoints.csv")["data"]
        original_open = Path.open
        original_transaction = SnapshotIndex.transaction
        transaction_reads = []
        selected = {tx(name) for name in ("a", "b", "c")}
        unrelated = {tx(name) for name in ("0", "1", "2", "d", "e", "f")}
        observations = read_json(self.source_archive / "evidence-index.json")
        forbidden_files = {self.source_archive / "trace.json"}
        forbidden_files.update(self.source_archive / row["file"] for row in observations
                               if row["endpoint"] in {"/tx/" + txid for txid in unrelated})

        def open_selected(path, *args, **kwargs):
            self.assertNotIn(Path(path), forbidden_files, "Warm scoped plot read unrelated archive bytes")
            return original_open(path, *args, **kwargs)

        def transaction(index, txid):
            self.assertIn(txid, selected, "Warm scoped plot decoded an unrelated transaction body")
            transaction_reads.append(txid)
            return original_transaction(index, txid)

        with patch.object(Path, "open", open_selected), \
                patch.object(SnapshotIndex, "transaction", transaction), \
                patch("liquid_tracer.shared_collection.load_shared_run",
                      side_effect=AssertionError("Warm scoped plot loaded the whole shared archive")):
            second = self.shared_plot("pegouts", max_hops=2)
        self.assertEqual(set(transaction_reads), selected)
        self.assertNotEqual(first["run_id"], second["run_id"])
        projected = read_json(self.case / "runs" / second["run_id"] / "trace.json")
        self.assertEqual(set(projected["transactions"]), selected)
        graph, _ = reviewed_plot(self.case, second["preview_id"])
        self.assertEqual([item["outpoint"] for item in graph["pegouts"]["matches"]], [self.endpoint])
        self.assertEqual(build_plot_csv(self.case, first["preview_id"], "endpoints.csv")["data"], previous_csv)

    def test_warm_source_corruption_cannot_publish_a_new_scope(self):
        first = self.shared_plot("pegouts", max_hops=3)
        previous_csv = build_plot_csv(self.case, first["preview_id"], "endpoints.csv")["data"]
        observations = read_json(self.source_archive / "evidence-index.json")
        unrelated = next(row for row in observations if row["endpoint"] == "/tx/" + tx("f"))
        for file in (self.source_archive / "trace.json", self.source_archive / unrelated["file"]):
            with self.subTest(file=file.name):
                before = file.read_bytes()
                previews = {path.name for path in (self.case / "previews").iterdir()}
                runs = {path.name for path in (self.case / "runs").iterdir()}
                try:
                    file.write_bytes(before + b"\n ")
                    with self.assertRaises(TraceError):
                        self.shared_plot("pegouts", max_hops=2)
                    self.assertEqual({path.name for path in (self.case / "previews").iterdir()}, previews)
                    self.assertEqual({path.name for path in (self.case / "runs").iterdir()}, runs)
                    self.assertEqual(build_plot_csv(self.case, first["preview_id"], "endpoints.csv")["data"], previous_csv)
                finally:
                    file.write_bytes(before)

    def test_prior_private_latest_without_observation_index_is_compatible_and_reused(self):
        private = self.private_collection()
        (private / "evidence-index.json").unlink()
        self.reseal(private)
        before = self.files(private)
        metadata = (self.case / "case.json").read_bytes()
        first = self.shared_plot("pegouts", max_hops=3)
        original_open = Path.open

        def warm_open(path, *args, **kwargs):
            self.assertNotIn(Path(path), {private / "trace.json", self.source_archive / "trace.json"},
                             "Warm compatibility check reloaded a full trace")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", warm_open):
            second = self.shared_plot("pegouts", max_hops=2)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(self.files(private), before)
        self.assertEqual((self.case / "case.json").read_bytes(), metadata)
        self.assertEqual(read_case(self.case)["latest_run"], private.name)

    def test_missing_prevout_uses_saved_funding_context_without_expanding_paths(self):
        self.select_seeds("b")
        state = read_json(self.source_archive / "trace.json")
        transaction = state["transactions"][tx("b")]
        transaction["data"]["vin"][0].pop("prevout")
        state["address_tx_counts"] = {"SYNTHETIC-c-address": {
            "address": "SYNTHETIC-c-address", "source": state["source"],
            "observed_at": "2026-10-02T00:00:00Z", "confirmed_tx_count": 17, "mempool_tx_count": 0}}
        index = read_json(self.source_archive / "evidence-index.json")
        observation = next(row for row in index if row["id"] == transaction["observation_id"])
        response = self.source_archive / observation["file"]
        save_json(response, transaction["data"])
        observation["sha256"] = digest(response.read_bytes())
        save_json(self.source_archive / "trace.json", state)
        save_json(self.source_archive / "evidence-index.json", index)
        self.reseal(self.source_archive)
        expected = self.full_plot("pegouts", max_hops=1)
        actual = self.shared_plot("pegouts", max_hops=1)
        graph = self.assert_same_plot_evidence(expected, actual)
        projected = read_json(self.case / "runs" / actual["run_id"] / "trace.json")
        self.assertEqual(set(projected["transactions"]), {tx("b"), tx("0")})
        self.assertIn(tx("c"), projected["saved_transactions"])
        self.assertEqual(projected["address_tx_counts"]["SYNTHETIC-c-address"]["confirmed_tx_count"], 17)
        funding = next(node for node in graph["nodes"] if node.get("details", {}).get("address") == "SYNTHETIC-c-address")
        self.assertEqual(funding["tx_count"], 17)
        self.assertNotIn("prevout", projected["transactions"][tx("b")]["data"]["vin"][0])
        self.assertNotIn("tx:" + tx("c"), {node["id"] for node in graph["nodes"]})
        rows = list(csv.DictReader(io.StringIO(
            build_plot_csv(self.case, actual["preview_id"], "transactions.csv")["data"].decode())))
        row = next(row for row in rows if row["Transaction Hash"] == tx("b") and row["Direction"] == "IN")
        self.assertEqual(row["Address Hash"], "SYNTHETIC-c-address")
        self.assertEqual(row["Asset Value"], "")
        self.assertIn("CONFIDENTIAL VALUE", row["Address Flags"])
        self.assertIn("CONFIDENTIAL ASSET", row["Address Flags"])


if __name__ == "__main__":
    unittest.main()
