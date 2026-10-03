"""Versioned peg-out traversal ignores display hop caps while honoring stops."""
from copy import deepcopy
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.investigations import create_investigation
from liquid_tracer.legend import legend_notes
from liquid_tracer.pegout_csv import endpoint_table_rows, pegout_lbtc_summary
from liquid_tracer.pegout_paths import pegout_graph, validate_query
from liquid_tracer.plot_csv import build_plot_csv
from liquid_tracer.plots import _query, list_plots, preview_plot, reviewed_plot
from liquid_tracer.services import set_service
from liquid_tracer.transaction_csv import transaction_csv_rows
from tests.test_attribution_convergence import annotation, graph_state, tx
from tests.test_connections import saved_case
from tests.test_named_hop_plots import named_state
from tests.test_pegout_csv import set_value
from tests.test_pegout_paths import add_pegout, add_unspendable, mark_unspent


def plotted(state, minimum=0, maximum=10, **options):
    return pegout_graph(state, validate_query(seeds=state["seeds"], min_hops=minimum,
        max_hops=maximum, transaction_io="complete", attribution_hop_limits="ignore", **options))


class PegoutStopOnlyTests(unittest.TestCase):
    def test_new_policy_ignores_each_attribution_cap_but_retains_exact_range_and_stops(self):
        for kind in ("address", "outpoint", "script"):
            for cap in (0, 1):
                with self.subTest(kind=kind, cap=cap):
                    state = graph_state((("a:0", "b"), ("b:0", "c")), seeds=("a:0",))
                    first, last = add_pegout(state, tx("b")), add_pegout(state, tx("c"))
                    label = annotation(stop=False)
                    output = state["transactions"][tx("a")]["data"]["vout"][0]
                    label.update(kind=kind, value={"address": output["scriptpubkey_address"],
                        "outpoint": tx("a") + ":0", "script": output["scriptpubkey"]}[kind], hop_limit=cap)
                    state["labels"] = [label]
                    original = deepcopy(state)
                    legacy = pegout_graph(state, validate_query(seeds=state["seeds"], transaction_io="complete"))
                    self.assertEqual(len(legacy["pegouts"]["matches"]), cap)
                    result = plotted(state, 2, 2)
                    self.assertEqual([row["outpoint"] for row in result["pegouts"]["matches"]], [last])
                    self.assertEqual(endpoint_table_rows(result, state)[0]["Hops from Seed"], 2)
                    self.assertEqual([row["outpoint"] for row in plotted(state, 1, 1)["pegouts"]["matches"]], [first])
                    self.assertEqual(state, original)
                    state["labels"].append(annotation(stop=True, address="SYNTHETIC-b-address"))
                    self.assertEqual([row["outpoint"] for row in plotted(state)["pegouts"]["matches"]], [first])
                    state["labels"][0]["stop"] = True
                    self.assertFalse(plotted(state)["nodes"])

    def test_named_resets_ignore_caps_without_lending_hops_or_crossing_stops(self):
        state = named_state((("a:0", "b"), ("b:0", "c"), ("c:0", "d")), group=("a", "b", "c"))
        state["labels"][0]["hop_limit"] = 0
        endpoint = add_pegout(state, tx("d"))
        result = plotted(state, 1, 1)
        self.assertEqual([row["outpoint"] for row in result["pegouts"]["matches"]], [endpoint])
        row, = endpoint_table_rows(result, state)
        self.assertEqual((row["Hop Counts"], row["Hops from Seed"]), ("1", 3))
        self.assertFalse(plotted(state, 0, 0)["nodes"])
        self.assertTrue(any("CSV hop limits are ignored" in note for note in legend_notes(result)))
        self.assertFalse(any("hop allowances still apply" in note for note in legend_notes(result)))
        state["labels"][1]["stop"] = True
        self.assertFalse(plotted(state, 1, 1)["nodes"])

    def test_unspent_and_unspendable_endpoints_use_same_stop_only_policy_and_complete_io(self):
        state = graph_state((("a:0", "b"), ("b:0", "c")), seeds=("a:0",))
        state["labels"] = [{**annotation(stop=False), "hop_limit": 0}]
        terminal = tx("c") + ":0"
        mark_unspent(state, terminal)
        script = add_unspendable(state, tx("c"))
        result = plotted(state, 2, 2, include_unspent=True, include_unspendable=True)
        self.assertEqual({row["outpoint"] for row in result["pegouts"]["endpoint_matches"]}, {terminal, script})
        rows = transaction_csv_rows(result, state)
        self.assertEqual(len(rows), sum(len(record["data"]["vin"]) + len(record["data"]["vout"])
                                       for record in state["transactions"].values()))
        self.assertEqual(pegout_lbtc_summary(result, state)["lbtc"], "0")
        state["labels"].append(annotation(stop=True, address="SYNTHETIC-b-address"))
        self.assertFalse(plotted(state, 2, 2, include_unspent=True, include_unspendable=True)["nodes"])

    def test_ignored_cap_restores_seed_provenance_without_double_counting_endpoint(self):
        state = named_state((("a:0", "d"), ("b:0", "c"), ("c:0", "d"), ("d:0", "e")),
                            group=("a", "b", "c"), seeds=("a:0", "b:0"), maximum=2)
        state["labels"][0]["hop_limit"] = 1
        endpoint = add_pegout(state, tx("e"))
        set_value(state, endpoint, 200_000_000)
        old = pegout_graph(state, validate_query(seeds=state["seeds"], min_hops=2, max_hops=2,
                                                transaction_io="complete"))
        old_row, = endpoint_table_rows(old, state)
        self.assertEqual(json.loads(old_row["Source Seed Hops"]), {tx("b") + ":0": 3})
        current = plotted(state, 2, 2)
        row, = endpoint_table_rows(current, state)
        self.assertEqual(json.loads(row["Source Seed Hops"]), {tx("a") + ":0": 2, tx("b") + ":0": 3})
        self.assertEqual(row["Hops from Seed"], 2)
        self.assertEqual(pegout_lbtc_summary(current, state)["lbtc"], "2")
        self.assertEqual(pegout_lbtc_summary(current, state)["pegout_count"], 1)

    def test_policy_query_is_strict_and_old_query_identity_is_unchanged(self):
        legacy = validate_query(seeds=[tx("a") + ":0"], transaction_io="complete")
        self.assertNotIn("attribution_hop_limits", legacy)
        self.assertEqual(validate_query(**legacy, attribution_hop_limits="ignore"),
                         {**legacy, "attribution_hop_limits": "ignore"})
        for invalid in (False, True, 0, 1, "", "apply", [], {}):
            with self.subTest(invalid=invalid), self.assertRaises(TraceError):
                validate_query(**legacy, attribution_hop_limits=invalid)

    def test_old_complete_io_and_path_only_snapshots_preserve_cap_scope_after_new_plot(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
                "liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph):
            case = create_investigation(Path(temporary), "Hop policy snapshots")
            state = graph_state((("a:0", "b"), ("b:0", "c")), seeds=("a:0",))
            first, last = add_pegout(state, tx("b")), add_pegout(state, tx("c"))
            set_value(state, first, 100_000_000)
            set_value(state, last, 200_000_000)
            saved_case(case, state)
            set_service(case, "SYNTHETIC-a-address", name="A", hop_limit=1, stop_tracing=False)
            old = []
            for complete in (None, "complete"):
                def legacy_query(*args, **kwargs):
                    return _query(*args, **{**kwargs, "transaction_io": complete, "attribution_hop_limits": None})
                with patch("liquid_tracer.plots._query", side_effect=legacy_query):
                    result = preview_plot(case, "pegouts")
                graph, plan = reviewed_plot(case, result["preview_id"])
                self.assertEqual(graph["plot"]["pegout_lbtc_summary"]["lbtc"], "1")
                self.assertEqual([row["outpoint"] for row in graph["pegouts"]["matches"]], [first])
                directory = Path(result["directory"])
                old.append((result, graph, plan, {p.name: p.read_bytes() for p in directory.iterdir()},
                            build_plot_csv(case, result["preview_id"], "endpoints.csv")["data"]))
            result = preview_plot(case, "pegouts")
            graph, _ = reviewed_plot(case, result["preview_id"])
            self.assertEqual(graph["plot"]["pegout_lbtc_summary"]["lbtc"], "3")
            exported = build_plot_csv(case, result["preview_id"], "endpoints.csv")["data"].decode()
            self.assertEqual({row["Outpoint"] for row in csv.DictReader(io.StringIO(exported))}, {first, last})
            full = preview_plot(case, "full")
            full_graph, _ = reviewed_plot(case, full["preview_id"])
            self.assertNotIn("tx:" + tx("c"), {node["id"] for node in full_graph["nodes"]})
            for saved, previous, plan, contents, endpoints in old:
                self.assertEqual(reviewed_plot(case, saved["preview_id"]), (previous, plan))
                self.assertEqual({p.name: p.read_bytes() for p in Path(saved["directory"]).iterdir()}, contents)
                self.assertEqual(build_plot_csv(case, saved["preview_id"], "endpoints.csv")["data"], endpoints)
            self.assertTrue(all(item["reviewable"] for item in list_plots(case)))


if __name__ == "__main__":
    unittest.main()
