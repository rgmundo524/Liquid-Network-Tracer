"""Reapply CSV boundaries to full plots without changing collected evidence."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer import input_import
from liquid_tracer.common import TraceError, read_json
from liquid_tracer.export import build_graph
from liquid_tracer.investigations import create_investigation
from liquid_tracer.plot_scope import project_full_scope
from liquid_tracer.plots import preview_plot, reviewed_plot
from tests.test_attribution_convergence import graph_state, tx
from tests.test_connections import saved_case


def rule(name, *, hops=None, stop=False):
    return {"kind": "address", "value": "SYNTHETIC-" + name + "-address",
            "hop_limit": hops, "stop": stop}


class PlotScopeTests(unittest.TestCase):
    def test_stop_keeps_boundary_context_and_does_not_mutate_archive(self):
        state = graph_state((("a:0", "b"), ("b:0", "c"), ("c:0", "d")),
                            seeds=("a:0",), labels=[rule("b", stop=True)])
        original = deepcopy(state)
        projected = project_full_scope(state)
        self.assertEqual(state, original)
        self.assertEqual(set(projected["transactions"]), {tx("a"), tx("b")})
        self.assertEqual(set(projected["links"]), {tx("a") + ":0"})
        graph = build_graph(projected)
        boundary = next(node for node in graph["nodes"]
                        if node["id"] == "liquid:address:SYNTHETIC-b-address")
        self.assertIn("STOP TRACING", boundary["label"])
        self.assertNotIn("Unspent", boundary["label"])
        self.assertEqual(projected["transactions"][tx("b")]["data"],
                         state["transactions"][tx("b")]["data"])

    def test_allowance_is_not_reset_by_downstream_larger_limit_or_reused_address(self):
        state = graph_state((("a:0", "b"), ("b:0", "c"), ("c:0", "d"), ("d:0", "e")),
                            seeds=("a:0",), labels=[rule("b", hops=2), rule("c", hops=99)])
        state["transactions"][tx("d")]["data"]["vout"][0]["scriptpubkey_address"] = "SYNTHETIC-b-address"
        result = project_full_scope(state)
        self.assertEqual(set(result["transactions"]), {tx(name) for name in "abcd"})
        self.assertNotIn(tx("d") + ":0", result["links"])

    def test_independent_seed_or_alternate_path_keeps_shared_descendants(self):
        state = graph_state((("a:0", "c"), ("c:0", "d"), ("b:0", "d"), ("d:0", "e")),
                            labels=[rule("a", hops=1)])
        result = project_full_scope(state)
        self.assertEqual(set(result["transactions"]), {tx(name) for name in "abcde"})
        self.assertNotIn(tx("c") + ":0", result["links"])
        self.assertIn(tx("b") + ":0", result["links"])
        graph = build_graph(result)
        edge = next(edge for edge in graph["edges"] if edge["id"] == "in:" + tx("d") + ":0")
        self.assertEqual(edge["role"], "context_input")

    def test_looser_rules_release_previously_held_saved_evidence(self):
        state = graph_state((("a:0", "b"), ("b:0", "c")), seeds=("a:0",))
        state["outputs"][tx("c") + ":0"].update(
            status="held_behind_service", trace_control={"previous_status": "hop_limit"})
        state["labels"] = [rule("a", hops=0)]
        self.assertEqual(set(project_full_scope(state)["transactions"]), {tx("a")})
        state["labels"] = [rule("a", hops=2)]
        result = project_full_scope(state)
        self.assertEqual(set(result["transactions"]), {tx(name) for name in "abc"})
        self.assertEqual(result["outputs"][tx("c") + ":0"]["trace_control"]["reason"],
                         "attribution_hop_limit")
        state["labels"] = []
        result = project_full_scope(state)
        self.assertEqual(result["outputs"][tx("c") + ":0"]["status"], "hop_limit")
        self.assertNotIn("trace_control", result["outputs"][tx("c") + ":0"])

    def test_unselected_sibling_and_raw_context_never_seed_new_paths(self):
        state = graph_state((("a:0", "b"), ("a:1", "c")), seeds=("a:0",),
                            raw_links=(("a:2", "d"),))
        result = project_full_scope(state)
        self.assertEqual(set(result["transactions"]), {tx("a"), tx("b")})
        self.assertEqual(set(result["links"]), {tx("a") + ":0"})
        graph = build_graph(result)
        self.assertIn("out:" + tx("a") + ":1", {edge["id"] for edge in graph["edges"]})
        self.assertNotIn(tx("a") + ":1", result["outputs"])

    def test_pruned_known_spend_cannot_resurrect_stale_unspent_endpoint(self):
        state = graph_state(seeds=("a:0",), raw_links=(("a:0", "b"),))
        state["outputs"][tx("a") + ":0"].update(
            status="unspent_at_observation", observed_spend={"spent": False}, spend_observation_id=1)
        result = project_full_scope(state)
        self.assertNotIn(tx("b"), result["transactions"])
        self.assertEqual(result["outputs"][tx("a") + ":0"]["observed_spend"], {"spent": False})
        self.assertFalse(any("Unspent" in node["label"] for node in build_graph(result)["nodes"]))

    def test_true_saved_unspent_endpoint_is_retained(self):
        state = graph_state(seeds=("a:0",))
        state["outputs"][tx("a") + ":0"].update(
            status="unspent_at_observation", observed_spend={"spent": False}, spend_observation_id=1)
        graph = build_graph(project_full_scope(state))
        self.assertTrue(any("Unspent" in node["label"] for node in graph["nodes"]))


class PlotScopeCSVIntegrationTests(unittest.TestCase):
    def test_updated_csv_changes_full_plot_scope_colors_and_change_without_fetch(self):
        with tempfile.TemporaryDirectory() as temporary:
            case = create_investigation(Path(temporary), "CSV replot", seeds=[tx("a") + ":0"])
            state, archive = saved_case(case, graph_state(
                (("a:0", "b"), ("b:0", "c"), ("c:0", "d")), seeds=("a:0",)))
            before = {path.name: path.read_bytes() for path in archive.iterdir() if path.is_file()}

            def upload(hops, color):
                files = [
                    {"name": "attributions.csv", "policy": "replace", "text":
                     "Address,Name,stop_tracing,hop_limit\nSYNTHETIC-b-address,Exchange,false," + str(hops) + "\n"},
                    {"name": "colors.csv", "policy": "replace", "text": "Name,Color\nExchange," + color + "\n"},
                    {"name": "change.csv", "policy": "replace", "text":
                     "Txid,ChangeVout\n" + tx("b") + ",0\n"},
                ]
                review = input_import.preview_import(case, files)
                self.assertTrue(review["valid"], review["errors"])
                input_import.apply_import(case, files, approval_sha256=review["approval_sha256"])

            with patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("No fetch")), \
                    patch("liquid_tracer.miro.sync", side_effect=AssertionError("No Miro write")), \
                    patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph):
                upload(0, "#123456")
                first = preview_plot(case, "full")
                graph, _ = reviewed_plot(case, first["preview_id"])
                self.assertEqual({node["id"] for node in graph["nodes"] if node["kind"] == "transaction"},
                                 {"tx:" + tx(name) for name in "ab"})
                address = next(node for node in graph["nodes"]
                               if node["id"] == "liquid:address:SYNTHETIC-b-address")
                self.assertEqual(address["color"], "#123456")
                self.assertEqual(graph["change_outputs"]["designations"][0]["outpoint"], tx("b") + ":0")
                upload(2, "#654321")
                with self.assertRaises(TraceError):
                    reviewed_plot(case, first["preview_id"])
                second = preview_plot(case, "full")
                graph, _ = reviewed_plot(case, second["preview_id"])
                self.assertEqual({node["id"] for node in graph["nodes"] if node["kind"] == "transaction"},
                                 {"tx:" + tx(name) for name in "abcd"})
                address = next(node for node in graph["nodes"]
                               if node["id"] == "liquid:address:SYNTHETIC-b-address")
                self.assertEqual(address["color"], "#654321")
            self.assertEqual(read_json(archive / "trace.json"), state)
            self.assertEqual(before, {path.name: path.read_bytes() for path in archive.iterdir() if path.is_file()})
