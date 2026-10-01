"""Starter maps show all exact saved paths independently of collection rules."""
from copy import deepcopy
import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.connections import connection_graph, connecting_outpoints, preview_connections, reviewed_connections
from liquid_tracer.investigations import create_investigation
from liquid_tracer.plots import _query, list_plots, preview_plot, reviewed_plot
from liquid_tracer.services import set_service
from liquid_tracer.transaction_csv import transaction_csv_rows
from tests.test_attribution_convergence import annotation, graph_state, tx
from tests.test_connections import saved_case
from tests.test_named_hop_plots import named_state


def graph(state, maximum=0):
    return connection_graph(state, maximum, connection_scope="all_saved")


class AllSavedConnectionTests(unittest.TestCase):
    def test_saved_long_paths_ignore_plot_and_collection_limits_and_attribution_rules(self):
        names = "0123456789abcdef"
        state = graph_state(tuple((name + ":0", child) for name, child in zip(names, names[1:])),
                            seeds=("0:0", "f:0"))
        state["limits"] = {"max_hops": 0}
        state["labels"] = [{**annotation(address="SYNTHETIC-0-address", stop=True), "hop_limit": 0}]
        state["transactions"][tx("8")]["data"]["status"] = {"confirmed": False}
        state["include_unconfirmed"] = False
        before = deepcopy(state)
        self.assertEqual(connecting_outpoints(state, 10)["outpoints"], [])
        report = graph(state)["connections"]
        self.assertEqual(report["pairs"], [{"source": tx("0"), "target": tx("f"), "shortest_hops": 15}])
        self.assertEqual(set(report["outpoints"]), {tx(name) + ":0" for name in names[:-1]})
        self.assertIsNone(report["max_hops"])
        self.assertEqual(report["connection_scope"], "all_saved")
        self.assertEqual(state, before)

    def test_stopped_collector_can_still_show_spend_proved_by_independently_saved_starter(self):
        state = graph_state(raw_links=(("a:0", "b"),), labels=[annotation(stop=True)])
        state["outputs"].pop(tx("a") + ":0", None)
        before = deepcopy(state)
        self.assertFalse(connecting_outpoints(state)["outpoints"])
        result = graph(state)
        self.assertEqual(result["connections"]["outpoints"], [tx("a") + ":0"])
        self.assertEqual({edge["id"] for edge in result["edges"]},
                         {"out:" + tx("a") + ":0", "in:" + tx("b") + ":0"})
        source = next(node for node in result["nodes"] if node["kind"] == "address")
        self.assertTrue(source["details"]["address_attributions"])
        self.assertTrue(any("Collection stop" in node["label"] for node in result["nodes"]))
        self.assertEqual(len(transaction_csv_rows(result, state)), 2)
        self.assertEqual(state, before)

    def test_missing_redundant_prevout_uses_funding_evidence_without_rewriting_raw_transaction(self):
        for prevout in (None, {}, {"scriptpubkey_address": "SYNTHETIC-a-address"}):
            with self.subTest(prevout=prevout):
                state = graph_state(raw_links=(("a:0", "b"),))
                state["transactions"][tx("b")]["data"]["vin"][0]["prevout"] = prevout
                before = deepcopy(state)
                result = graph(state)
                rows = transaction_csv_rows(result, state)
                self.assertEqual(len(rows), 2)
                self.assertEqual({row["Address Hash"] for row in rows}, {"SYNTHETIC-a-address"})
                self.assertEqual(state, before)

    def test_selected_seed_scope_excludes_siblings_unknown_funding_and_address_only_links(self):
        sibling = graph_state(raw_links=(("a:1", "b"),))
        self.assertFalse(graph(sibling)["nodes"])
        absent = graph_state(raw_links=(("c:0", "b"),))
        del absent["transactions"][tx("c")]
        absent["outputs"] = {key: value for key, value in absent["outputs"].items()
                              if value["txid"] != tx("c")}
        self.assertFalse(graph(absent)["nodes"])
        same = graph_state()
        same["transactions"][tx("b")]["data"]["vout"][0] = deepcopy(same["transactions"][tx("a")]["data"]["vout"][0])
        self.assertFalse(graph(same)["nodes"])
        for flag in ("is_pegin", "is_coinbase"):
            skipped = graph_state(raw_links=(("a:0", "b"),))
            skipped["transactions"][tx("b")]["data"]["vin"][0][flag] = True
            self.assertFalse(graph(skipped)["nodes"])

    def test_alternate_saved_routes_survive_and_unrelated_branches_do_not(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0", "a:1", "a:2", "b:0"),
                            raw_links=(("a:1", "c"), ("c:0", "d"), ("d:0", "b"), ("a:2", "e")))
        result = graph(state, 1)
        self.assertEqual(set(result["connections"]["outpoints"]),
                         {tx("a") + ":0", tx("a") + ":1", tx("c") + ":0", tx("d") + ":0"})
        self.assertNotIn("tx:" + tx("e"), {node["id"] for node in result["nodes"]})

    def test_named_hop_metadata_does_not_reintroduce_stops_or_a_group_distance_cutoff(self):
        state = named_state((("a:0", "b"), ("b:0", "c"), ("c:0", "d"), ("d:0", "e")),
                            seeds=("a:0", "e:0"), group=("a", "d"), maximum=0)
        state["labels"][0].update(stop=True, hop_limit=0)
        state["transactions"][tx("c")]["data"]["status"] = {"confirmed": False}
        result = graph(state)
        self.assertEqual(set(result["connections"]["outpoints"]), {tx(name) + ":0" for name in "abcd"})
        self.assertEqual(result["connections"]["hop_reference_name"], "Perp")
        self.assertEqual(result["connections"]["pairs"][0]["shortest_hops"], 1)
        self.assertEqual(result["connections"]["transaction_reference_hops"][tx("c")], 2)
        self.assertEqual(result["connections"]["transaction_reference_hops"][tx("d")], 0)

    def test_conflicting_saved_spends_prevouts_and_corrupt_tracked_links_fail_closed(self):
        broken = []
        state = graph_state(raw_links=(("a:0", "b"), ("a:0", "c")))
        broken.append(state)
        state = graph_state(raw_links=(("a:0", "b"),))
        state["transactions"][tx("b")]["data"]["vin"][0]["prevout"] = {"value": 999}
        broken.append(state)
        state = graph_state((("a:0", "b"),))
        state["links"][tx("a") + ":0"]["vin"] = 1
        broken.append(state)
        state = graph_state((("a:0", "b"),))
        del state["outputs"][tx("a") + ":0"]
        broken.append(state)
        broken.append(graph_state(raw_links=(("a:0", "b"), ("b:0", "a"))))
        for number, state in enumerate(broken):
            with self.subTest(number=number), self.assertRaises(TraceError):
                graph(state)

    def test_named_target_with_only_fee_output_still_has_its_verified_connection(self):
        state = named_state((("a:0", "b"),), seeds=("a:0", "b:0"), group=("a",), maximum=0)
        state["transactions"][tx("b")]["data"]["vout"] = [{"scriptpubkey_type": "fee", "scriptpubkey": "", "value": 1}]
        result = graph(state)
        self.assertEqual(result["connections"]["outpoints"], [tx("a") + ":0"])
        self.assertEqual(result["connections"]["transaction_reference_hops"][tx("b")], 1)
        self.assertEqual(len(transaction_csv_rows(result, state)), 2)

    def test_invalid_scope_and_hops_do_not_silently_change_policy(self):
        for value in ("", False, True, [], {}, "all"):
            with self.subTest(value=value), self.assertRaises(TraceError):
                connecting_outpoints(graph_state(), connection_scope=value)
        with self.assertRaises(TraceError):
            connecting_outpoints(graph_state(), True, connection_scope="all_saved")


class AllSavedConnectionSnapshotTests(unittest.TestCase):
    def test_old_bounded_plot_and_new_all_saved_plot_keep_distinct_frozen_memberships(self):
        with tempfile.TemporaryDirectory() as temporary, patch("liquid_tracer.elk_layout.optimize_graph",
                side_effect=lambda graph, **kwargs: graph), patch("liquid_tracer.api.Esplora.get",
                side_effect=AssertionError("Use saved data only")), patch("liquid_tracer.address_counts.ensure_counts",
                side_effect=AssertionError("Use saved counts only")):
            case = create_investigation(Path(temporary), "Starter snapshots")
            state = graph_state((("a:0", "b"), ("a:1", "c"), ("c:0", "d"), ("d:0", "b")),
                                seeds=("a:0", "a:1", "b:0"))
            saved_case(case, state)
            set_service(case, "SYNTHETIC-c-address", name="Stopped in collection", stop_tracing=True, hop_limit=0)

            def bounded_query(*args, **kwargs):
                return _query(*args, **{**kwargs, "connection_scope": None, "transaction_io": None})

            with patch("liquid_tracer.plots._query", side_effect=bounded_query):
                old = preview_plot(case, "connections", max_hops=1)
            old_graph, old_plan = reviewed_plot(case, old["preview_id"])
            original = {p.name: p.read_bytes() for p in Path(old["directory"]).iterdir()}
            new = preview_plot(case, "connections", max_hops=0)
            new_graph, _ = reviewed_plot(case, new["preview_id"])
            self.assertEqual(old["query"], {"max_hops": 1})
            self.assertEqual(new["query"], {"connection_scope": "all_saved", "transaction_io": "complete"})
            self.assertIsNone(new["max_hops"])
            self.assertEqual(len(old_graph["connections"]["outpoints"]), 1)
            self.assertEqual(len(new_graph["connections"]["outpoints"]), 4)
            self.assertEqual(reviewed_plot(case, old["preview_id"]), (old_graph, old_plan))
            self.assertEqual(original, {p.name: p.read_bytes() for p in Path(old["directory"]).iterdir()})
            self.assertTrue(all(item["reviewable"] for item in list_plots(case)))
            with (Path(new["directory"]) / "transactions.csv").open(newline="") as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 9)

    def test_legacy_cli_new_preview_uses_saved_inputs_while_old_snapshot_stays_reviewable(self):
        with tempfile.TemporaryDirectory() as temporary, patch("liquid_tracer.elk_layout.optimize_graph",
                side_effect=lambda graph, **kwargs: graph), patch("liquid_tracer.address_counts.ensure_counts",
                side_effect=AssertionError("Offline view must not fetch address counts")):
            case = create_investigation(Path(temporary), "Saved input proof")
            state = graph_state(raw_links=(("a:0", "b"),))
            state["transactions"][tx("b")]["data"]["vin"][0]["prevout"] = None
            _, archive = saved_case(case, state)
            before = {p.name: p.read_bytes() for p in archive.iterdir()}

            def bounded_graph(state, maximum, **kwargs):
                return connection_graph(state, maximum, **{**kwargs, "connection_scope": None, "transaction_io": None})

            with patch("liquid_tracer.connections.connection_graph", side_effect=bounded_graph):
                old = preview_connections(case, max_hops=1)
            old_graph, old_plan = reviewed_connections(case, old["preview_id"])
            new = preview_connections(case, max_hops=0)
            new_graph, _ = reviewed_connections(case, new["preview_id"])
            self.assertFalse(old_graph["nodes"])
            self.assertTrue(new_graph["nodes"])
            self.assertEqual(new["connection_scope"], "all_saved")
            self.assertIsNone(new["max_hops"])
            with (Path(new["directory"]) / "transactions.csv").open(newline="") as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 3)
            self.assertEqual(reviewed_connections(case, old["preview_id"]), (old_graph, old_plan))
            self.assertEqual(before, {p.name: p.read_bytes() for p in archive.iterdir()})


if __name__ == "__main__":
    unittest.main()
