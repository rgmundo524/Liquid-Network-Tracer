"""Independent full-trace analysis limits preserve saved collection evidence."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError, digest, read_json, save_json
from liquid_tracer.investigations import create_investigation
from liquid_tracer.plot_scope import project_full_scope
from liquid_tracer.plots import _query, list_plots, plot_files, preview_plot, reviewed_plot
from liquid_tracer.plot_csv import build_plot_csv
from liquid_tracer.services import set_service
from liquid_tracer.investigations import update_case
from tests import test_shared_snapshot_plots as shared_fixtures
from tests.test_attribution_convergence import graph_state, tx
from tests.test_connections import saved_case
from tests.test_named_hop_plots import named_state
from tests.test_plot_scope import rule
from tests.test_pegout_paths import add_pegout


class FullPlotHopLimitTests(unittest.TestCase):
    def test_cap_zero_and_boundary_preserve_source_and_complete_transaction_context(self):
        state = graph_state((("a:0", "b"), ("b:0", "c"), ("c:0", "d")), seeds=("a:0",))
        state["limits"] = {"max_hops": 15}
        before = deepcopy(state)
        for cap, expected in ((0, "a"), (1, "ab"), (2, "abc")):
            with self.subTest(cap=cap):
                result = project_full_scope(state, max_hops=cap)
                self.assertEqual(set(result["transactions"]), {tx(n) for n in expected})
                self.assertEqual(result["limits"], {"max_hops": 15})
                self.assertNotIn(tx(expected[-1]) + ":0", result["links"])
                self.assertEqual(result["transactions"][tx(expected[-1])]["data"],
                                 state["transactions"][tx(expected[-1])]["data"])
        self.assertEqual(state, before)
        self.assertEqual(set(project_full_scope(state)["transactions"]), {tx(n) for n in "abcd"})

    def test_cap_does_not_admit_unselected_siblings_shared_addresses_or_context_inputs(self):
        state = graph_state((("a:0", "b"), ("a:1", "c"), ("c:0", "e")),
                            seeds=("a:0",), raw_links=(("d:0", "b"),))
        for record in state["transactions"].values():
            for output in record["data"]["vout"]:
                output["scriptpubkey_address"] = "SYNTHETIC-shared-address"
            for vin in record["data"]["vin"]:
                vin["prevout"]["scriptpubkey_address"] = "SYNTHETIC-shared-address"
        result = project_full_scope(state, max_hops=3)
        self.assertEqual(set(result["transactions"]), {tx(n) for n in "ab"})
        self.assertEqual(set(result["links"]), {tx("a") + ":0"})
        self.assertNotIn(tx("a") + ":1", result["outputs"])

    def test_independent_seed_keeps_short_route_without_lending_allowance_to_long_route(self):
        state = graph_state((("a:0", "c"), ("c:0", "d"), ("b:0", "d"), ("d:0", "e")),
                            seeds=("a:0", "b:0"), labels=(rule("a", hops=1),))
        result = project_full_scope(state, max_hops=2)
        self.assertEqual(set(result["transactions"]), {tx(n) for n in "abcde"})
        self.assertNotIn(tx("c") + ":0", result["links"])
        self.assertIn(tx("d") + ":0", result["links"])
        state["labels"].append(rule("d", stop=True))
        stopped = project_full_scope(state, max_hops=2)
        self.assertNotIn(tx("e"), stopped["transactions"])

    def test_named_basis_keeps_resets_but_uses_explicit_plot_cap(self):
        state = named_state((("a:0", "b"), ("b:0", "c"), ("c:0", "d"), ("d:0", "e")),
                            group=("a", "b"), maximum=15)
        before = deepcopy(state)
        result = project_full_scope(state, max_hops=1)
        self.assertEqual(set(result["transactions"]), {tx(n) for n in "abc"})
        self.assertEqual(result["transactions"][tx("c")]["reference_hops"], 1)
        self.assertEqual(result["limits"], {"max_hops": 15})
        self.assertEqual(state, before)
        state["limits"]["max_hops"] = 1
        wider = project_full_scope(state, max_hops=2)
        self.assertEqual(wider["transactions"][tx("d")]["reference_hops"], 2)
        self.assertEqual(wider["limits"], {"max_hops": 1})

    def test_explicit_cap_recovers_exact_private_spends_and_does_not_revive_stale_unspent(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",), raw_links=(("b:0", "c"), ("c:0", "d")))
        state["outputs"][tx("b") + ":0"].update(status="unspent_at_observation",
            observed_spend={"spent": False}, spend_observation_id=1)
        # An archived transaction may precede its final output checkpoint.
        state["outputs"].pop(tx("d") + ":0")
        before = deepcopy(state)
        self.assertEqual(set(project_full_scope(state)["transactions"]), {tx(n) for n in "ab"})
        bounded = project_full_scope(state, max_hops=1)
        self.assertEqual(set(bounded["transactions"]), {tx(n) for n in "ab"})
        self.assertEqual(bounded["outputs"][tx("b") + ":0"]["status"], "spent_in_saved_evidence")
        extended = project_full_scope(state, max_hops=3)
        self.assertEqual(set(extended["transactions"]), {tx(n) for n in "abcd"})
        self.assertIn(tx("d") + ":0", extended["outputs"])
        self.assertEqual(state, before)

    def test_full_query_rejects_noninteger_negative_and_unbounded_excess_values(self):
        state = {"seeds": [tx("a") + ":0"]}
        for value in (True, -1, 1.5, "10", 2147483648):
            with self.subTest(value=value), self.assertRaisesRegex(TraceError, "Full trace hops"):
                _query("full", state, 0, value)
        self.assertEqual(_query("full", state, 0, 0), {"max_hops": 0})
        self.assertEqual(_query("full", state, 0, None), {})

    def test_original_seed_query_explicitly_overrides_named_basis_for_every_goal(self):
        state = {"seeds": [tx("a") + ":0"], "hop_reference_name": "Perp"}
        for goal in ("full", "pegouts", "connections"):
            with self.subTest(goal=goal):
                query = _query(goal, state, 0, 10, hop_basis="original_seeds")
                self.assertEqual(query["hop_basis"], "original_seeds")
                self.assertNotIn("hop_reference_name", query)
                with self.assertRaisesRegex(TraceError, "hop distances"):
                    _query(goal, state, 0, 10, hop_basis="unknown")


class FullPlotSavedScopeTests(unittest.TestCase):
    def test_original_seed_basis_overrides_named_distance_for_full_and_pegout_reviews_and_csvs(self):
        with tempfile.TemporaryDirectory() as temporary:
            case = create_investigation(Path(temporary), "Original seed scope", seeds=[tx("a") + ":0"])
            state = named_state((("a:0", "b"), ("b:0", "c"), ("c:0", "d")),
                                group=("a", "b"), maximum=15)
            endpoint_c = add_pegout(state, tx("c"))
            endpoint_d = add_pegout(state, tx("d"))
            state, archive = saved_case(case, state)
            for name in "ab":
                set_service(case, "SYNTHETIC-" + name + "-address", name="Perp", stop_tracing=False)
            before = (archive / "trace.json").read_bytes()
            with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda value, **_: value):
                configured = preview_plot(case, "full", max_hops=1)
                original = preview_plot(case, "full", max_hops=1, hop_basis="original_seeds")
                named_pegouts = preview_plot(case, "pegouts", max_hops=2)
                ordinary_pegouts = preview_plot(case, "pegouts", max_hops=2, hop_basis="original_seeds")
            self.assertEqual((configured["transaction_count"], original["transaction_count"]), (3, 2))
            self.assertNotIn("hop_basis", configured["query"])
            self.assertEqual(original["query"], {"max_hops": 1, "hop_basis": "original_seeds"})
            self.assertNotIn("hop_reference_name", original)
            graph, _ = reviewed_plot(case, ordinary_pegouts["preview_id"])
            self.assertEqual(graph["pegouts"]["query"]["hop_basis"], "original_seeds")
            self.assertNotIn("hop_reference_name", graph)
            self.assertEqual({row["outpoint"] for row in graph["pegouts"]["matches"]}, {endpoint_c})
            named_graph, _ = reviewed_plot(case, named_pegouts["preview_id"])
            self.assertEqual({row["outpoint"] for row in named_graph["pegouts"]["matches"]},
                             {endpoint_c, endpoint_d})
            csv = build_plot_csv(case, ordinary_pegouts["preview_id"], "endpoints.csv")["data"]
            self.assertIn(endpoint_c.encode(), csv)
            self.assertNotIn(endpoint_d.encode(), csv)
            self.assertTrue(all(row["reviewable"] for row in list_plots(case)))
            self.assertEqual((archive / "trace.json").read_bytes(), before)

    def test_old_unbounded_and_new_bounded_previews_review_without_rewriting_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            case = create_investigation(Path(temporary), "Full cap", seeds=[tx("a") + ":0"])
            state, archive = saved_case(case, graph_state(
                (("a:0", "b"), ("b:0", "c")), seeds=("a:0",)))
            before = {path.name: path.read_bytes() for path in archive.iterdir()}
            with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda value, **_: value), \
                    patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("No fetch")):
                legacy = preview_plot(case, "full")
                bounded = preview_plot(case, "full", max_hops=1)
            old_graph, _ = reviewed_plot(case, legacy["preview_id"])
            new_graph, _ = reviewed_plot(case, bounded["preview_id"])
            self.assertEqual(legacy["query"], {})
            self.assertNotIn("full_trace_max_hops", old_graph["graph_options"])
            self.assertEqual(bounded["query"], {"max_hops": 1})
            self.assertEqual(new_graph["graph_options"]["full_trace_max_hops"], 1)
            self.assertEqual((legacy["transaction_count"], bounded["transaction_count"]), (3, 2))
            self.assertEqual(bounded["source_max_hops"], 10)
            self.assertTrue(all(plot["reviewable"] for plot in list_plots(case)))
            self.assertEqual(before, {path.name: path.read_bytes() for path in archive.iterdir()})

            # Even when a modified report is rehashed, the reviewed layout's
            # own recorded cap prevents a different scope being advertised.
            directory = Path(bounded["directory"])
            graph = read_json(directory / "graph.json")
            graph["plot"]["query"]["max_hops"] = 2
            graph["plot"]["max_hops"] = 2
            save_json(directory / "graph.json", graph)
            save_json(directory / "plot.json", graph["plot"])
            (directory / "SHA256SUMS").write_text("".join(
                digest((directory / name).read_bytes()) + "  " + name + "\n"
                for name in sorted(plot_files(directory) - {"SHA256SUMS"})))
            with self.assertRaisesRegex(TraceError, "full trace scope"):
                reviewed_plot(case, bounded["preview_id"])


class FullSharedScopeTests(unittest.TestCase):
    collect = shared_fixtures.SharedSnapshotPlotTests.collect
    files = staticmethod(shared_fixtures.SharedSnapshotPlotTests.files)
    setUp = shared_fixtures.SharedSnapshotPlotTests.setUp
    full_plot = shared_fixtures.SharedSnapshotPlotTests.full_plot
    shared_plot = shared_fixtures.SharedSnapshotPlotTests.shared_plot
    assert_same_plot_evidence = shared_fixtures.SharedSnapshotPlotTests.assert_same_plot_evidence
    select_seeds = shared_fixtures.SharedSnapshotPlotTests.select_seeds

    def test_indexed_full_cap_matches_full_evidence_projection(self):
        before = self.files(self.source_archive)
        graph = self.assert_same_plot_evidence(self.full_plot("full", max_hops=2),
                                              self.shared_plot("full", max_hops=2))
        self.assertEqual({node["id"] for node in graph["nodes"] if node["kind"] == "transaction"},
                         {"tx:" + tx(n) for n in "acb"})
        self.assertEqual(self.files(self.source_archive), before)

    def test_original_seed_basis_bounds_shared_projection_despite_configured_group(self):
        set_service(self.case, "SYNTHETIC-0-address", name="Late group", stop_tracing=False)
        update_case(self.case, {"run_defaults": {"hop_reference_name": "Late group", "layout_attempts": 1}})
        for goal in ("full", "pegouts"):
            with self.subTest(goal=goal):
                result = self.shared_plot(goal, max_hops=3, hop_basis="original_seeds")
                graph, _ = reviewed_plot(self.case, result["preview_id"])
                projected = read_json(self.case / "runs" / result["run_id"] / "trace.json")
                self.assertEqual(set(projected["transactions"]), {tx(n) for n in "acb0"})
                self.assertEqual(projected["collection_source"]["selection"]["max_hops"], 3)
                self.assertNotIn("hop_reference_name", projected)
                self.assertNotIn("hop_reference_name", graph)
                self.assertEqual(graph["plot"]["query"]["hop_basis"], "original_seeds")

    def test_original_seed_starter_connections_keep_exact_limit_and_saved_review(self):
        self.select_seeds("a", "0")
        set_service(self.case, "SYNTHETIC-c-address", name="Perp", stop_tracing=False)
        update_case(self.case, {"run_defaults": {"hop_reference_name": "Perp", "layout_attempts": 1}})
        short = self.shared_plot("connections", max_hops=2, connection_scope="hop_limited",
                                 hop_basis="original_seeds")
        longer = self.shared_plot("connections", max_hops=3, connection_scope="hop_limited",
                                  hop_basis="original_seeds")
        # Selected starters remain visible even before a connecting path falls
        # within the requested depth. The path count still obeys the cutoff.
        self.assertFalse(short["empty"])
        self.assertEqual(short["connection_count"], 0)
        self.assertFalse(longer["empty"])
        self.assertGreater(longer["connection_count"], 0)
        for result in (short, longer):
            graph, _ = reviewed_plot(self.case, result["preview_id"])
            self.assertEqual(graph["plot"]["query"]["hop_basis"], "original_seeds")
            self.assertNotIn("hop_reference_name", graph)


if __name__ == "__main__":
    unittest.main()
