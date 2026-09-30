import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.api import Esplora, Limits
from liquid_tracer.common import save_json
from liquid_tracer.export import build_graph, export_run
from liquid_tracer.miro import make_plan, validate_plan
from liquid_tracer.store import Store
from liquid_tracer.trace import new_state, trace
from tests.fixtures import A, B, C, D, X, fixture


class GroupHopPresentationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "case")
        self.addCleanup(self.store.close)
        fixture_path = self.root / "fixture.json"
        save_json(fixture_path, fixture())
        limits = Limits(max_hops=3)
        api = Esplora(self.store, "pending", limits, fixture=fixture_path, min_interval=0)
        state = new_state([A + ":0"], api.base, limits, [])
        api.run_id = state["run_id"]
        self.state = trace(api, state, limits, self.root / "trace.json")

    def named_state(self):
        state = copy.deepcopy(self.state)
        state["hop_reference_name"] = "Perp group"
        # B is mixed: the retained group output stays zero while the other
        # branch is one hop away. C returns to the group, then D exits again.
        depths = {A + ":0": 0, B + ":0": 0, B + ":1": 1,
                  C + ":0": 0, C + ":1": 1, D + ":0": 1}
        for key, depth in depths.items():
            state["outputs"][key]["trace_scope_depth"] = depth
        for txid, hops in ((A, 0), (B, 1), (C, 1), (D, 1)):
            state["transactions"][txid]["reference_hops"] = hops
        return state

    def test_named_labels_keep_original_depths_and_dependency_columns(self):
        state = self.named_state()
        before = copy.deepcopy(state)
        graph = build_graph(state, include_fees=True)
        normal = build_graph(self.state, include_fees=True)
        nodes = {node["id"]: node for node in graph["nodes"]}
        self.assertEqual(graph["hop_reference_name"], "Perp group")
        self.assertEqual(graph["run"]["hop_reference_name"], "Perp group")
        self.assertEqual({n["id"]: n["column"] for n in graph["nodes"]},
                         {n["id"]: n["column"] for n in normal["nodes"]})
        self.assertEqual([(e["id"], e["source"], e["target"]) for e in graph["edges"]],
                         [(e["id"], e["source"], e["target"]) for e in normal["edges"]])
        for txid in (A, B, C, D):
            node = nodes["tx:" + txid]
            self.assertIn("hop " + str(state["transactions"][txid]["reference_hops"]), node["label"].splitlines())
            self.assertEqual(node["details"]["seed_depth"], self.state["transactions"][txid]["depth"])
            self.assertEqual(node["details"]["transaction"], self.state["transactions"][txid]["data"])
        self.assertEqual(nodes["tx:" + D]["details"]["seed_depth"], 3)
        self.assertEqual(nodes["tx:" + D]["details"]["reference_hops"], 1)
        validate_plan(make_plan(graph))
        self.assertEqual(state, before)

    def test_mixed_output_hops_remain_per_occurrence_with_no_context_or_fee_claims(self):
        graph = build_graph(self.named_state(), include_fees=True)
        nodes = {node["id"]: node for node in graph["nodes"]}
        edges = {edge["id"]: edge for edge in graph["edges"]}
        self.assertEqual(edges["out:" + B + ":0"]["details"]["reference_hops"], 0)
        self.assertEqual(edges["out:" + B + ":1"]["details"]["reference_hops"], 1)
        shared = nodes["liquid:address:SYNTHETIC-branch-A"]
        self.assertEqual({o["outpoint"]: (o["seed_depth"], o["reference_hops"])
                          for o in shared["details"]["occurrences"]},
                         {B + ":0": (1, 0), C + ":0": (2, 0)})
        event = nodes["event:" + D + ":0"]
        self.assertEqual((event["details"]["seed_depth"], event["details"]["reference_hops"]), (3, 1))
        for key in (A + ":1", B + ":2"):
            self.assertNotIn("reference_hops", edges["out:" + key]["details"])
        for key in ("SYNTHETIC-funding-context", "SYNTHETIC-external-coinput", "SYNTHETIC-unrelated-seed-sibling"):
            for occurrence in nodes["liquid:address:" + key]["details"]["occurrences"]:
                self.assertNotIn("reference_hops", occurrence)
                self.assertIsNone(occurrence["trace"])
        self.assertNotIn("reference_hops", nodes["event:" + B + ":2"]["details"])

    def test_boundary_inspection_does_not_fall_back_to_seed_distance(self):
        state = self.named_state()
        state["transactions"][D]["reference_hops"] = None
        graph = build_graph(state)
        node = next(node for node in graph["nodes"] if node["id"] == "tx:" + D)
        self.assertIn("Boundary inspection", node["label"].splitlines())
        self.assertFalse(any(line.startswith("hop ") for line in node["label"].splitlines()))
        self.assertEqual(node["details"]["seed_depth"], 3)
        self.assertNotIn("reference_hops", node["details"])

    def test_csv_preserves_raw_depth_and_adds_relative_output_depth_only_in_named_mode(self):
        for named in (False, True):
            with self.subTest(named=named):
                state = self.named_state() if named else self.state
                before = copy.deepcopy(state)
                destination = self.root / ("named" if named else "ordinary")
                export_run(self.store, state, destination)
                with (destination / "outputs.csv").open(newline="", encoding="utf-8") as stream:
                    reader = csv.DictReader(stream)
                    self.assertEqual("reference_hops" in reader.fieldnames, named)
                    rows = {row["outpoint"]: row for row in reader}
                self.assertEqual(rows[D + ":0"]["depth"], "3")
                if named:
                    self.assertEqual(rows[D + ":0"]["reference_hops"], "1")
                    self.assertEqual(rows[B + ":0"]["reference_hops"], "0")
                    self.assertEqual(rows[B + ":1"]["reference_hops"], "1")
                    self.assertEqual(rows[A + ":1"]["reference_hops"], "")
                    self.assertEqual(rows[B + ":2"]["reference_hops"], "")
                with (destination / "frontier.csv").open(newline="", encoding="utf-8") as stream:
                    self.assertEqual("reference_hops" in csv.DictReader(stream).fieldnames, named)
                archived = json.loads((destination / "trace.json").read_text())
                self.assertEqual(archived, before)
                self.assertEqual(state, before)

    def test_default_mode_has_no_named_hop_metadata(self):
        graph = build_graph(self.state)
        self.assertNotIn("hop_reference_name", graph)
        self.assertNotIn("hop_reference_name", graph["run"])
        for node in graph["nodes"]:
            self.assertNotIn("seed_depth", node["details"])
            self.assertNotIn("reference_hops", node["details"])
            if node["kind"] == "transaction":
                record = self.state["transactions"][node["id"][3:]]
                self.assertIn("hop " + str(record["depth"]), node["label"].splitlines())


if __name__ == "__main__":
    unittest.main()
