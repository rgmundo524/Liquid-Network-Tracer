"""Broad collection evidence keeps attribution-limited full graph views."""
from copy import deepcopy
import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.cli import csv_run, refresh_presentation, saved_graph, verify_export
from liquid_tracer.common import read_json, save_json
from liquid_tracer.export import build_graph, export_run
from liquid_tracer.investigations import create_investigation, read_case
from liquid_tracer.miro import make_plan
from liquid_tracer.plot_scope import project_collected_full_scope, project_full_scope
from liquid_tracer.services import load_services, service_labels, set_service
from liquid_tracer.trace import COLLECTION_POLICY
from tests.test_attribution_convergence import annotation, graph_state, tx


def transaction_ids(graph):
    return {node["id"][3:] for node in graph["nodes"] if node["kind"] == "transaction"}


def capped_state():
    state = graph_state((("a:0", "b"), ("b:0", "c"), ("c:0", "d")), seeds=("a:0",),
                        labels=[{**annotation(stop=False, address="SYNTHETIC-b-address"), "hop_limit": 0}])
    state.update(collection_policy=dict(COLLECTION_POLICY), limits={"max_hops": 10},
                 observations=[], parent_run=None, status="bounded_complete", stats={},
                 started_at="2026-10-01T00:00:00Z", finished_at="2026-10-01T00:00:01Z")
    return state


class StopOnlyPresentationTests(unittest.TestCase):
    def test_policy_gate_preserves_legacy_raw_views_and_projected_copy(self):
        state = capped_state()
        original = deepcopy(state)
        self.assertEqual(set(project_collected_full_scope(state)["transactions"]), {tx("a"), tx("b")})
        self.assertEqual(state, original)
        state.pop("collection_policy")
        self.assertIs(project_collected_full_scope(state), state)
        self.assertEqual(transaction_ids(build_graph(state)), {tx(name) for name in "abcd"})

    def test_independent_seeds_survive_while_blocked_spends_become_context(self):
        for control in ({"hop_limit": 0, "stop": False}, {"hop_limit": 99, "stop": True}):
            with self.subTest(control=control):
                state = graph_state((("a:0", "c"), ("b:0", "c"), ("c:0", "d")),
                                    labels=[{**annotation(), **control}])
                state.update(collection_policy=dict(COLLECTION_POLICY), limits={"max_hops": 10})
                graph = build_graph(project_collected_full_scope(state))
                self.assertEqual(transaction_ids(graph), {tx(name) for name in "abcd"})
                blocked = next(edge for edge in graph["edges"] if edge["id"] == "in:" + tx("c") + ":0")
                self.assertEqual(blocked["role"], "context_input")
                self.assertNotIn(blocked["id"], graph["branch_structure"]["edge_memberships"])
                self.assertFalse(any(node.get("convergence") for node in graph["nodes"]))

    def test_ignored_caps_keep_pegout_lineages_without_ignoring_stops(self):
        state = graph_state((("a:0", "c"), ("b:0", "c"), ("c:0", "d")),
                            labels=[{**annotation(stop=False), "hop_limit": 0}])
        original = deepcopy(state)
        graph = build_graph(state, respect_attribution_hops=False)
        merge = next(node for node in graph["nodes"] if node["id"] == "tx:" + tx("c"))
        self.assertEqual(merge["convergence"]["starting_transaction_indices"], [1, 2])
        self.assertEqual(graph["branch_structure"]["edge_memberships"]["in:" + tx("c") + ":0"],
                         ["tx:" + tx("a")])
        source = next(node for node in graph["nodes"] if node["id"] == "liquid:address:SYNTHETIC-a-address")
        self.assertIn("Display hop limit: 0", source["label"])
        self.assertEqual(state, original)
        state["labels"][0]["stop"] = True
        stopped = build_graph(state, respect_attribution_hops=False)
        self.assertFalse(any(node.get("convergence") for node in stopped["nodes"]))

    def test_full_projection_global_limit_cannot_borrow_an_exhausted_shorter_path(self):
        state = graph_state((("a:0", "b"), ("b:0", "d"), ("d:0", "f"),
                             ("e:0", "c"), ("c:0", "b")), seeds=("a:0", "e:0"),
                            labels=[{**annotation(stop=False), "hop_limit": 2}])
        state.update(limits={"max_hops": 3}, collection_policy=dict(COLLECTION_POLICY))
        projected = project_full_scope(state)
        self.assertNotIn(tx("f"), projected["transactions"])
        self.assertNotIn(tx("d") + ":0", projected["links"])
        self.assertIn(tx("d"), projected["transactions"])
        legacy = deepcopy(state)
        legacy.pop("collection_policy")
        self.assertIn(tx("f"), project_full_scope(legacy)["transactions"])


class StopOnlyArchivePresentationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.case = create_investigation(Path(temporary.name), "Broad collection", seeds=[tx("a") + ":0"])
        self.state = capped_state()
        controls = set_service(self.case, "SYNTHETIC-b-address", name="Display cap", stop_tracing=False, hop_limit=0)
        self.state["labels"] = service_labels(controls)
        self.state["service_controls"] = {key: value for key, value in controls.items() if key != "history"}
        self.state.update(case_id=read_case(self.case)["case_id"], run_id="0123456789abcdef")
        self.archive = self.case / "runs" / self.state["run_id"]
        class EmptyStore:
            def observations(self, observations):
                return []
        export_run(EmptyStore(), self.state, self.archive)
        save_json(self.case / "case.json", {**read_case(self.case), "latest_run": self.state["run_id"]})

    def test_archive_and_full_run_csv_keep_broad_evidence_while_graphs_are_capped(self):
        self.assertEqual(read_json(self.archive / "trace.json"), self.state)
        graph = read_json(self.archive / "graph.json")
        self.assertEqual(transaction_ids(graph), {tx("a"), tx("b")})
        self.assertEqual(read_json(self.archive / "miro-plan.json"), make_plan(graph))
        for filename, field in (("transactions.csv", "Transaction Hash"), ("outputs.csv", "outpoint")):
            with (self.archive / filename).open(newline="") as stream:
                values = [row[field] for row in csv.DictReader(stream)]
            self.assertTrue(any(value.startswith(tx("d")) for value in values))
        verify_export(self.archive)
        self.assertEqual(transaction_ids(saved_graph(self.case)[2]), {tx("a"), tx("b")})
        result = csv_run(self.case)
        with Path(result["files"][0]).open(newline="") as stream:
            self.assertIn(tx("d"), {row["Transaction Hash"] for row in csv.DictReader(stream)})

    def test_current_rules_change_direct_previews_and_miro_refresh_without_archive_rewrite(self):
        before = {str(path.relative_to(self.archive)): path.read_bytes()
                  for path in self.archive.rglob("*") if path.is_file()}
        set_service(self.case, "SYNTHETIC-b-address", name="Broader display", stop_tracing=False, hop_limit=2)
        self.assertEqual(transaction_ids(saved_graph(self.case)[2]), {tx(name) for name in "abcd"})
        archived_plan = read_json(self.archive / "miro-plan.json")
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph):
            refreshed = refresh_presentation(archived_plan, self.archive / "trace.json",
                                              service_settings=load_services(self.case))
        self.assertTrue({"tx:" + tx(name) for name in "abcd"} <= {item["key"] for item in refreshed["shapes"]})
        self.assertEqual(before, {str(path.relative_to(self.archive)): path.read_bytes()
                                 for path in self.archive.rglob("*") if path.is_file()})

    def test_uncapped_collection_reuses_graph_for_complete_csv_accounting(self):
        state = deepcopy(self.state)
        state["labels"] = []
        with patch("liquid_tracer.export.build_graph", wraps=build_graph) as build:
            export_run(type("Store", (), {"observations": lambda self, ids: []})(), state,
                       self.case / "uncapped-export")
        self.assertEqual(build.call_count, 1)

    def test_separate_address_migration_uses_archived_display_scope(self):
        from liquid_tracer.address_migration import preview_merge
        from liquid_tracer.common import digest
        from liquid_tracer.miro import sync
        from tests.test_address_migration import InventoryMiro

        board = "SYNTHETIC-CAPPED-BOARD="
        save_json(self.case / "case.json", {**read_case(self.case), "miro_board": board})
        path = self.case / "miro" / (digest(board.encode())[:24] + ".json")
        graph = build_graph(project_collected_full_scope(self.state), merge_addresses=False)
        remote = InventoryMiro()
        sync(make_plan(graph), board, path, transport=remote, token="synthetic", interval=0)
        remote.calls.clear()
        preview = preview_merge(self.case)
        self.assertEqual(preview["run_id"], self.state["run_id"])
        self.assertEqual(set(preview["plan"]["rewires"]), {edge["id"] for edge in graph["edges"]})
        self.assertEqual(remote.calls, [])
