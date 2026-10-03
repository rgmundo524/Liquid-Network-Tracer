"""Exact, trace-scoped peg-out request totals exposed to the investigation UI."""
from copy import deepcopy
import csv
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError, digest, read_json, save_json
from liquid_tracer.investigations import create_investigation
from liquid_tracer.pegout_csv import pegout_lbtc_summary
from liquid_tracer.plots import list_plots, plot_files, preview_plot, reviewed_plot
from liquid_tracer.workflow_api import case_workflow, public_plot, workflow_result
from tests.test_attribution_convergence import graph_state, tx
from tests.test_connections import saved_case
from tests.test_pegout_csv import graph, set_value
from tests.test_pegout_paths import add_pegout, add_unspendable, mark_unspent


class PegoutAmountSummaryTests(unittest.TestCase):
    def test_multiple_sources_and_shared_destination_count_each_outpoint_once(self):
        state = graph_state((("a:0", "c"), ("b:0", "c")), seeds=("a:0", "b:0"))
        first, second = add_pegout(state, tx("c")), add_pegout(state, tx("c"))
        set_value(state, first, 2 ** 53 + 1)
        set_value(state, second, 2)
        plotted = graph(state)
        result = pegout_lbtc_summary(plotted, state)
        self.assertEqual(result, {
            "pegout_count": 2, "valued_lbtc_count": 2, "unknown_amount_count": 0,
            "unknown_asset_count": 0, "non_lbtc_count": 0,
            "lbtc": "90071992.54740995", "value_base_units": "9007199254740995",
        })
        plotted["pegouts"]["matches"].append(deepcopy(plotted["pegouts"]["matches"][0]))
        self.assertEqual(pegout_lbtc_summary(plotted, state), result)

    def test_unknowns_and_other_assets_do_not_become_zero_valued_lbtc(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        valued, hidden, unknown_asset, other = [add_pegout(state, tx("b")) for _ in range(4)]
        set_value(state, valued, 125_000_001)
        set_value(state, hidden, None)
        set_value(state, unknown_asset, 800_000_000, asset=None)
        set_value(state, other, 900_000_000, asset="bc" * 32)
        set_value(state, add_unspendable(state, tx("b")), 700_000_000)
        set_value(state, tx("b") + ":0", 600_000_000)
        mark_unspent(state, tx("b") + ":0")
        result = pegout_lbtc_summary(graph(state, include_unspent=True, include_unspendable=True), state)
        self.assertEqual(result, {
            "pegout_count": 4, "valued_lbtc_count": 1, "unknown_amount_count": 1,
            "unknown_asset_count": 1, "non_lbtc_count": 1,
            "lbtc": "1.25000001", "value_base_units": "125000001",
        })

    def test_only_selected_seed_paths_and_hop_range_contribute(self):
        state = graph_state((("a:0", "b"), ("b:0", "c"), ("a:1", "d")), seeds=("a:0",),
                            raw_links=(("e:0", "b"),))
        for name, amount in (("b", 100_000_000), ("c", 200_000_000),
                             ("d", 300_000_000), ("e", 400_000_000)):
            set_value(state, add_pegout(state, tx(name)), amount)
        for maximum, total in ((0, "0"), (1, "1"), (2, "3")):
            with self.subTest(maximum=maximum):
                result = pegout_lbtc_summary(graph(state, maximum=maximum, include_context=True), state)
                self.assertEqual(result["lbtc"], total)
        self.assertEqual(pegout_lbtc_summary(graph(state, minimum=2, maximum=2), state)["lbtc"], "2")

    def test_public_summary_rejects_inconsistent_or_inexact_amount_metadata(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        set_value(state, add_pegout(state, tx("b")), 1)
        summary = pegout_lbtc_summary(graph(state), state)
        value = {"goal": "pegouts", "match_count": 1, "pegout_lbtc_summary": summary}
        self.assertEqual(public_plot(value)["pegout_lbtc_summary"], summary)
        for change in ({"lbtc": 0.00000001}, {"value_base_units": 1}, {"value_base_units": "1.0"},
                       {"valued_lbtc_count": True}, {"unknown_amount_count": 1},
                       {"pegout_count": 2}, {"lbtc": "0.00000002"}):
            with self.subTest(change=change):
                self.assertNotIn("pegout_lbtc_summary", public_plot(
                    {**value, "pegout_lbtc_summary": {**summary, **change}}))
        self.assertNotIn("pegout_lbtc_summary", public_plot({**value, "goal": "full"}))

    def test_generated_summary_persists_with_plot_and_matches_endpoint_export(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
                "liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph):
            case = create_investigation(Path(temporary), "Peg-out total")
            state = graph_state((("a:0", "b"),), seeds=("a:0",))
            set_value(state, add_pegout(state, tx("b")), 100_000_001)
            set_value(state, add_pegout(state, tx("b")), 25_000_000)
            saved_case(case, state)
            result = preview_plot(case, "pegouts")
            directory = Path(result["directory"])
            expected = result["pegout_lbtc_summary"]
            with (directory / "trace-endpoints.csv").open(newline="") as stream:
                endpoint_sum = sum(Decimal(row["Value LBTC"]) for row in csv.DictReader(stream)
                                   if row["Status"] == "Peg-out" and row["Value LBTC"])
            self.assertEqual(Decimal(expected["lbtc"]), endpoint_sum)
            self.assertEqual(expected["lbtc"], "1.25000001")
            self.assertEqual(read_json(directory / "plot.json")["pegout_lbtc_summary"], expected)
            self.assertEqual(reviewed_plot(case, result["preview_id"])[0]["plot"]["pegout_lbtc_summary"], expected)
            with patch("liquid_tracer.pegout_csv.pegout_lbtc_summary", side_effect=AssertionError("Do not recompute")):
                self.assertEqual(case_workflow(case)["plots"][0]["pegout_lbtc_summary"], expected)
                self.assertEqual(workflow_result(case, result, "plot")["pegout_lbtc_summary"], expected)
            empty = preview_plot(case, "pegouts", max_hops=0)
            self.assertEqual(empty["pegout_lbtc_summary"]["lbtc"], "0")
            self.assertEqual(empty["pegout_lbtc_summary"]["pegout_count"], 0)

    def test_old_plot_without_summary_still_opens_and_bad_summary_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
                "liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph):
            case = create_investigation(Path(temporary), "Earlier peg-out total")
            state = graph_state((("a:0", "b"),), seeds=("a:0",))
            add_pegout(state, tx("b"))
            saved_case(case, state)
            result = preview_plot(case, "pegouts")
            directory = Path(result["directory"])
            plotted = read_json(directory / "graph.json")
            summary = plotted["plot"].pop("pegout_lbtc_summary")

            def persist():
                save_json(directory / "graph.json", plotted)
                save_json(directory / "plot.json", plotted["plot"])
                (directory / "SHA256SUMS").write_text("".join(
                    digest((directory / name).read_bytes()) + "  " + name + "\n"
                    for name in sorted(plot_files(directory) - {"SHA256SUMS"})))

            persist()
            reviewed_plot(case, result["preview_id"])
            self.assertNotIn("pegout_lbtc_summary", list_plots(case)[0])
            self.assertNotIn("pegout_lbtc_summary", case_workflow(case)["plots"][0])
            plotted["plot"]["pegout_lbtc_summary"] = {**summary, "lbtc": "99"}
            persist()
            with self.assertRaisesRegex(TraceError, "amount summary"):
                reviewed_plot(case, result["preview_id"])
