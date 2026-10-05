"""Ordinary transaction-hop bounds over all verified saved starter evidence."""
from copy import deepcopy
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import LBTC, TraceError, digest, read_json, save_json
from liquid_tracer.connections import (connection_graph, connecting_outpoints, preview_connections,
                                        reviewed_connections)
from liquid_tracer.export import legend_lines
from liquid_tracer.investigations import create_investigation
from liquid_tracer.legend import legend_notes
from liquid_tracer.miro import make_plan, validate_plan
from liquid_tracer.transaction_csv import transaction_csv_rows
from tests.test_attribution_convergence import annotation, graph_state, tx
from tests.test_connections import saved_case
from tests.test_connections_complete_transactions import io_ids
from tests.test_named_hop_plots import named_state
from tests.test_pegout_paths import add_pegout, add_unspendable, mark_unspent


def bounded(state, hops):
    return connecting_outpoints(state, hops, connection_scope="hop_limited")


class HopLimitedConnectionTests(unittest.TestCase):
    def test_increasing_hops_adds_every_qualifying_alternate_path(self):
        state = graph_state((("a:0", "b"),),
            raw_links=(("a:1", "c"), ("c:0", "b"), ("a:2", "d"), ("d:0", "e"),
                       ("e:0", "b"), ("a:3", "f")),
            seeds=("a:0", "a:1", "a:2", "a:3", "b:0"))
        before = deepcopy(state)
        paths = [set(bounded(state, hops)["outpoints"]) for hops in range(4)]
        self.assertEqual(paths[0], set())
        self.assertEqual(paths[1], {tx("a") + ":0"})
        self.assertEqual(paths[2] - paths[1], {tx("a") + ":1", tx("c") + ":0"})
        self.assertEqual(paths[3] - paths[2], {tx("a") + ":2", tx("d") + ":0", tx("e") + ":0"})
        for hops in range(1, 4):
            report = bounded(state, hops)
            self.assertEqual(report["max_hops"], hops)
            self.assertEqual(report["connection_scope"], "hop_limited")
            self.assertEqual(report["pairs"], [{"source": tx("a"), "target": tx("b"), "shortest_hops": 1}])
        self.assertEqual(state, before)

    def test_stop_hop_and_confirmation_rules_do_not_hide_saved_paths(self):
        state = graph_state(raw_links=(("a:0", "c"), ("c:0", "b")),
            labels=[{**annotation(stop=True, address="SYNTHETIC-c-address"), "hop_limit": 0}])
        state["limits"] = {"max_hops": 0}
        state["include_unconfirmed"] = False
        state["transactions"][tx("c")]["data"]["status"] = {"confirmed": False}
        self.assertFalse(bounded(state, 1)["outpoints"])
        result = connection_graph(state, 2, connection_scope="hop_limited", transaction_io="complete")
        self.assertEqual(set(result["connections"]["outpoints"]), {tx("a") + ":0", tx("c") + ":0"})
        self.assertTrue(any("Collection stop" in node["label"] for node in result["nodes"]))
        self.assertTrue(any(node.get("details", {}).get("transaction", {}).get("status") == {"confirmed": False}
                            for node in result["nodes"]))
        self.assertTrue(any(node.get("details", {}).get("address_attributions") for node in result["nodes"]))

    def test_named_group_does_not_reset_search_limit_or_reported_pair_distance(self):
        state = named_state((("a:0", "c"), ("c:0", "d"), ("d:0", "b")),
                            seeds=("a:0", "b:0"), group=("a", "c", "d", "b"), maximum=0)
        state["labels"][0].update(stop=True, hop_limit=0)
        self.assertFalse(bounded(state, 2)["outpoints"])
        result = connection_graph(state, 3, connection_scope="hop_limited", transaction_io="complete")
        report = result["connections"]
        self.assertEqual(report["pairs"], [{"source": tx("a"), "target": tx("b"), "shortest_hops": 3}])
        self.assertEqual(report["hop_reference_name"], "Perp")
        self.assertEqual(report["transaction_reference_hops"][tx("b")], 0)
        self.assertIn("at most 3 transaction hops", result["notice"])
        self.assertIn("display distances do not reset the transaction-hop search limit", result["notice"])
        self.assertEqual(result["graph_options"]["connection_hops"], 3)

    def test_intermediate_starter_has_its_own_origin_without_resetting_other_sources(self):
        state = graph_state(raw_links=(("a:0", "b"), ("b:0", "c")),
                            seeds=("a:0", "b:0", "c:0"))
        expected = {(tx("a"), tx("b"), 1), (tx("b"), tx("c"), 1)}
        self.assertEqual({tuple(p[k] for k in ("source", "target", "shortest_hops"))
                          for p in bounded(state, 1)["pairs"]}, expected)
        expected.add((tx("a"), tx("c"), 2))
        self.assertEqual({tuple(p[k] for k in ("source", "target", "shortest_hops"))
                          for p in bounded(state, 2)["pairs"]}, expected)

    def test_both_legends_distinguish_transaction_bound_from_named_display_hops(self):
        state = named_state((("a:0", "c"), ("c:0", "d"), ("d:0", "b")),
                            seeds=("a:0", "b:0"), group=("a", "c", "d", "b"), maximum=0)
        state["labels"][0].update(stop=True, hop_limit=0)
        graph = connection_graph(state, 3, connection_scope="hop_limited", transaction_io="complete")
        for renderer in (legend_notes, legend_lines):
            with self.subTest(renderer=renderer.__name__):
                legend = " ".join(renderer(graph))
                self.assertRegex(legend, r"at most 3 (?:ordinary )?transaction steps")
                self.assertIn("do not reset", legend)
                self.assertRegex(legend, r"Attribution stop(?:s| rules) and hop limits are ignored")
                self.assertNotIn("still apply", legend)
                self.assertNotIn("STOP TRACING = an explicit address boundary", legend)
                self.assertNotIn("STOP TRACING: an explicit address boundary", legend)

    def test_first_spend_must_use_selected_seed_output(self):
        state = graph_state(raw_links=(("a:1", "b"),))
        self.assertFalse(bounded(state, 10)["pairs"])
        state["seeds"].append(tx("a") + ":1")
        self.assertEqual(bounded(state, 1)["outpoints"], [tx("a") + ":1"])

    def test_complete_io_fees_and_context_survive_without_expanding_excluded_branches(self):
        state = graph_state((("a:0", "b"), ("a:1", "c"), ("c:0", "d"), ("d:0", "b")),
                            raw_links=(("e:0", "b"), ("f:0", "b")),
                            seeds=("a:0", "a:1", "b:0"))
        state["labels"] = [annotation(stop=True, address="SYNTHETIC-e-address")]
        add_pegout(state, tx("b"))
        add_unspendable(state, tx("b"))
        mark_unspent(state, tx("b") + ":0")
        data = state["transactions"][tx("b")]["data"]
        fee = f"{tx('b')}:{len(data['vout'])}"
        data["vout"].append({"scriptpubkey": "", "scriptpubkey_type": "fee", "value": 7, "asset": LBTC})
        before = deepcopy(state)
        with patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("Use saved data only")):
            result = connection_graph(state, 1, connection_scope="hop_limited", transaction_io="complete",
                                      group_context_inputs=True, include_fees=True)
        self.assertEqual({node["id"] for node in result["nodes"] if node["kind"] == "transaction"},
                         {"tx:" + tx("a"), "tx:" + tx("b")})
        self.assertEqual({edge["id"] for edge in result["edges"]}, io_ids(state, "ab"))
        self.assertEqual(result["connections"]["outpoints"], [tx("a") + ":0"])
        edges = {edge["id"]: edge for edge in result["edges"]}
        self.assertEqual(edges["out:" + tx("a") + ":1"]["role"], "context_output")
        self.assertEqual(edges["out:" + fee]["role"], "context_output")
        self.assertEqual(len(transaction_csv_rows(result, state)), len(edges))
        validate_plan(make_plan(result))
        self.assertEqual(state, before)

    def test_invalid_limits_and_inconsistent_spends_fail_closed(self):
        for limit in (None, True, -1, 1.5, "2", 2147483648):
            with self.subTest(limit=limit), self.assertRaises(TraceError):
                bounded(graph_state(), limit)
        for links in ((("a:0", "b"), ("b:0", "a")), (("a:0", "b"), ("a:0", "c"))):
            with self.subTest(links=links), self.assertRaises(TraceError):
                bounded(graph_state(raw_links=links), 2)

    def test_random_saved_dags_match_exhaustive_path_union(self):
        rng = random.Random(26010)
        for iteration in range(60):
            names = "0123456"
            links = []
            selected = [f"{root}:0" for root in "036"]
            for index, parent in enumerate(names):
                vout = 0
                for child in names[index + 1:]:
                    if rng.random() < 0.4:
                        key = f"{parent}:{vout}"
                        links.append((key, child))
                        if parent in "036" and vout % 2 == 0:
                            selected.append(key)
                        vout += 1
            state = graph_state(raw_links=links, seeds=sorted(set(selected)))
            state["labels"] = [annotation(stop=True, address="SYNTHETIC-3-address")]
            hops = rng.randrange(5)
            expected = set()
            for root in "036":
                pending = [(root, [])]
                while pending:
                    parent, path = pending.pop()
                    if parent in "036" and parent != root:
                        expected.update(path)
                    if len(path) == hops:
                        continue
                    for key, child in links:
                        if key.split(":")[0] == parent and (parent != root or key in selected):
                            pending.append((child, path + [tx(parent) + ":" + key.split(":")[1]]))
            self.assertEqual(set(bounded(state, hops)["outpoints"]), expected, iteration)


class HopLimitedPreviewTests(unittest.TestCase):
    def test_frozen_legacy_all_saved_and_bounded_previews_remain_distinct(self):
        with tempfile.TemporaryDirectory() as temporary, patch("liquid_tracer.elk_layout.optimize_graph",
                side_effect=lambda graph, **kwargs: graph):
            case = create_investigation(Path(temporary), "Bounded starter evidence")
            saved_case(case, graph_state((("a:0", "b"),),
                raw_links=(("a:1", "c"), ("c:0", "b")), seeds=("a:0", "a:1", "b:0")))
            previews = [preview_connections(case, max_hops=1, connection_scope=scope)
                        for scope in (None, "all_saved", "hop_limited")]
            reports = [reviewed_connections(case, preview["preview_id"])[0]["connections"] for preview in previews]
            self.assertNotIn("connection_scope", reports[0])
            self.assertEqual([len(report["outpoints"]) for report in reports], [1, 3, 1])
            self.assertEqual([report["max_hops"] for report in reports], [1, None, 1])
            larger = preview_connections(case, max_hops=2, connection_scope="hop_limited")
            self.assertEqual(len(reviewed_connections(case, larger["preview_id"])[0]["connections"]["outpoints"]), 3)
            self.assertEqual(larger["connection_scope"], "hop_limited")
            for preview, original in zip(previews, reports):
                self.assertEqual(reviewed_connections(case, preview["preview_id"])[0]["connections"], original)

    def test_review_rejects_invalid_hop_limit_even_when_manifest_matches(self):
        with tempfile.TemporaryDirectory() as temporary, patch("liquid_tracer.elk_layout.optimize_graph",
                side_effect=lambda graph, **kwargs: graph):
            case = create_investigation(Path(temporary), "Bounded limit consistency")
            saved_case(case, graph_state((("a:0", "b"),)))
            preview = preview_connections(case, max_hops=1, connection_scope="hop_limited")
            directory = Path(preview["directory"])
            graph = read_json(directory / "graph.json")
            graph["connections"]["max_hops"] = None
            graph["graph_options"]["connection_hops"] = None
            save_json(directory / "graph.json", graph)
            manifest = directory / "SHA256SUMS"
            names = [line.split("  ")[1] for line in manifest.read_text().splitlines()]
            manifest.write_text("".join(digest((directory / name).read_bytes()) + "  " + name + "\n" for name in names))
            with self.assertRaisesRegex(TraceError, "Connection hops"):
                reviewed_connections(case, preview["preview_id"])


if __name__ == "__main__":
    unittest.main()
