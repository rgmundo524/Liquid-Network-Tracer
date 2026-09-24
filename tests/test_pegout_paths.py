"""Inclusive peg-out path ranges over exact saved UTXO evidence."""
from copy import deepcopy
import random
import unittest

from liquid_tracer.common import LBTC, TraceError
from liquid_tracer.miro import make_plan, validate_plan
from liquid_tracer.pegout_paths import pegout_graph, validate_query
from tests.fixtures import output
from tests.test_attribution_convergence import annotation, graph_state, tx
from tests.test_layout import state_from


def add_pegout(state, txid):
    rows = state["transactions"][txid]["data"]["vout"]
    index = len(rows)
    rows.append({"scriptpubkey": "6a", "scriptpubkey_type": "op_return", "value": 50,
                 "asset": LBTC, "pegout": {"genesis_hash": "00" * 32,
                    "scriptpubkey": "0014" + "bb" * 20,
                    "scriptpubkey_address": "SYNTHETIC-bitcoin-request"}})
    return f"{txid}:{index}"


def report(state, minimum=0, maximum=10, origin=None):
    return pegout_graph(state, validate_query(origin or tx("a"), minimum, maximum))["pegouts"]


class PegoutPathTests(unittest.TestCase):
    def test_validate_selected_seed_outputs_preserves_legacy_query_shape(self):
        seeds = ["  " + tx("b").upper() + ":01 ", tx("a") + ":0", tx("b") + ":1"]
        before = list(seeds)
        self.assertEqual(validate_query(seeds=seeds),
                         {"seeds": [tx("a") + ":0", tx("b") + ":1"], "min_hops": 0, "max_hops": 10})
        self.assertEqual(seeds, before)
        for value in ([], "bad", [True], [None], ["bad"], [tx("a") + ":-1"],
                      [tx("a") + ":4294967296"]):
            with self.subTest(seeds=value), self.assertRaises(TraceError):
                validate_query(seeds=value)
        with self.assertRaises(TraceError):
            validate_query(tx("a"), seeds=[tx("a") + ":0"])
        with self.assertRaises(TraceError):
            validate_query(seeds=[tx("a") + ":0"], min_hops=2, max_hops=1)
        self.assertEqual(validate_query(tx("a")), {"txid": tx("a"), "min_hops": 0, "max_hops": 10})

    def test_seed_query_uses_all_selected_roots_without_unselected_siblings(self):
        state = graph_state((("a:0", "c"), ("a:1", "d"), ("b:0", "e")), seeds=("a:0", "b:0"))
        selected = {add_pegout(state, tx("c")), add_pegout(state, tx("e"))}
        add_pegout(state, tx("d"))
        add_pegout(state, tx("a"))
        before = deepcopy(state)
        query = validate_query(seeds=state["seeds"], max_hops=1)
        graph = pegout_graph(state, query)
        self.assertEqual({row["outpoint"] for row in graph["pegouts"]["matches"]}, selected)
        self.assertEqual(graph["pegouts"]["outpoints"], sorted(state["seeds"]))
        self.assertEqual({node["id"] for node in graph["nodes"] if node.get("role") == "starting_transaction"},
                         {"tx:" + tx("a"), "tx:" + tx("b")})
        self.assertTrue(all(row["hops"] == [1] for row in graph["pegouts"]["matches"]))
        self.assertIn("selected seed outputs", graph["pegouts"]["scope"])
        self.assertIn("each seed transaction is hop 0", graph["notice"])
        self.assertEqual(graph["graph_options"]["pegout_query"], query)
        validate_plan(make_plan(graph))
        self.assertEqual(state, before)

    def test_seed_hop_zero_matches_only_selected_pegout_outputs(self):
        state = graph_state(seeds=("a:0", "b:0"))
        selected = add_pegout(state, tx("a"))
        add_pegout(state, tx("a"))
        add_pegout(state, tx("b"))
        graph = pegout_graph(state, validate_query(seeds=[selected, tx("b") + ":0"], max_hops=0))
        self.assertEqual([(row["outpoint"], row["hops"]) for row in graph["pegouts"]["matches"]],
                         [(selected, [0])])
        self.assertEqual([edge["id"] for edge in graph["edges"]], ["out:" + selected])

    def test_reached_seed_transaction_allows_its_other_outputs_only_downstream(self):
        state = graph_state((("a:0", "b"), ("b:0", "c"), ("b:1", "d")), seeds=("a:0", "b:0"))
        selected = add_pegout(state, tx("b"))
        sibling = add_pegout(state, tx("b"))
        endpoint_c = add_pegout(state, tx("c"))
        endpoint_d = add_pegout(state, tx("d"))
        query = validate_query(seeds=[*state["seeds"], selected], max_hops=2)
        result = pegout_graph(state, query)["pegouts"]
        self.assertEqual({row["outpoint"]: row["hops"] for row in result["matches"]},
                         {selected: [0, 1], sibling: [1], endpoint_c: [1, 2], endpoint_d: [2]})
        self.assertEqual(set(result["outpoints"]), {tx("a") + ":0", tx("b") + ":0", tx("b") + ":1"})
        one = pegout_graph(state, {**query, "min_hops": 1, "max_hops": 1})["pegouts"]
        self.assertEqual({row["outpoint"] for row in one["matches"]}, {selected, sibling, endpoint_c})
        self.assertNotIn(tx("b") + ":1", one["outpoints"])

    def test_multiple_seed_paths_keep_distances_and_service_allowances_on_merge(self):
        state = graph_state((("a:0", "d"), ("b:0", "c"), ("c:0", "d"), ("d:0", "e")),
                            seeds=("a:0", "b:0"))
        endpoint = add_pegout(state, tx("e"))
        capped = annotation(stop=False)
        capped.update(kind="outpoint", value=tx("a") + ":0", hop_limit=1)
        state["labels"] = [capped]
        query = validate_query(seeds=state["seeds"], max_hops=3)
        result = pegout_graph(state, query)["pegouts"]
        self.assertEqual([(row["outpoint"], row["hops"]) for row in result["matches"]], [(endpoint, [3])])
        self.assertEqual(set(result["outpoints"]), {tx("b") + ":0", tx("c") + ":0", tx("d") + ":0"})
        state["labels"] = []
        result = pegout_graph(state, query)["pegouts"]
        self.assertEqual(result["matches"][0]["hops"], [2, 3])
        self.assertEqual(result["match_count"], 1)

    def test_seed_query_handles_partial_bootstrap_and_rejects_missing_known_output(self):
        state = graph_state(seeds=("a:0",))
        with self.assertRaisesRegex(TraceError, "does not exist"):
            pegout_graph(state, validate_query(seeds=[tx("a") + ":1"]))
        state["transactions"] = {}
        graph = pegout_graph(state, validate_query(seeds=state["seeds"]))
        self.assertEqual(graph["nodes"], [])
        self.assertEqual(graph["edges"], [])

    def test_seed_roots_each_respect_saved_confirmation_policy(self):
        state = graph_state((("a:0", "c"), ("b:0", "d")), seeds=("a:0", "b:0"))
        unconfirmed_path = add_pegout(state, tx("c"))
        confirmed_path = add_pegout(state, tx("d"))
        state["transactions"][tx("a")]["data"]["status"] = {"confirmed": False}
        query = validate_query(seeds=state["seeds"])
        graph = pegout_graph(state, query)
        self.assertEqual([row["outpoint"] for row in graph["pegouts"]["matches"]], [confirmed_path])
        state["include_unconfirmed"] = True
        graph = pegout_graph(state, query)
        self.assertEqual({row["outpoint"] for row in graph["pegouts"]["matches"]},
                         {unconfirmed_path, confirmed_path})

    def test_validate_transaction_and_inclusive_bounds(self):
        self.assertEqual(validate_query("  " + tx("a").upper() + " ", 0, 0),
                         {"txid": tx("a"), "min_hops": 0, "max_hops": 0})
        for value in (None, True, "bad", tx("a") + ":0"):
            with self.subTest(txid=value), self.assertRaises(TraceError):
                validate_query(value)
        for minimum, maximum in ((True, 2), (0, False), ("1", 2), (0, 1.5), (-1, 2),
                                  (3, 2), (0, 2147483648)):
            with self.subTest(bounds=(minimum, maximum)), self.assertRaises(TraceError):
                validate_query(tx("a"), minimum, maximum)

    def test_direct_origin_pegout_is_hop_zero_without_spendable_seeds(self):
        state = graph_state(seeds=("a:0",))
        endpoint = add_pegout(state, tx("a"))
        state["seeds"] = []
        before = deepcopy(state)
        graph = pegout_graph(state, validate_query(tx("a"), 0, 0))
        self.assertEqual(graph["pegouts"]["matches"][0]["hops"], [0])
        self.assertEqual({node["id"] for node in graph["nodes"]}, {"tx:" + tx("a"), "event:" + endpoint})
        self.assertEqual(len(graph["edges"]), 1)
        self.assertEqual(graph["edges"][0]["id"], "out:" + endpoint)
        self.assertEqual(next(node for node in graph["nodes"] if node["kind"] == "transaction")["role"],
                         "starting_transaction")
        self.assertEqual(report(state, 1, 5)["match_count"], 0)
        validate_plan(make_plan(graph))
        self.assertEqual(state, before)

    def test_longer_path_qualifies_when_shortest_path_is_below_minimum(self):
        state = graph_state((("a:0", "d"), ("a:1", "b"), ("b:0", "c"), ("c:0", "d"),
                             ("a:2", "e")), seeds=("a:0",))
        endpoint = add_pegout(state, tx("d"))
        result = report(state, 3, 3)
        self.assertEqual(result["matches"][0]["outpoint"], endpoint)
        self.assertEqual(result["matches"][0]["hops"], [3])
        self.assertEqual(set(result["outpoints"]), {tx("a")+":1", tx("b")+":0", tx("c")+":0"})
        self.assertEqual(report(state, 1, 3)["matches"][0]["hops"], [1, 3])
        self.assertEqual(report(state, 2, 2)["match_count"], 0)
        self.assertEqual(report(state, 4, 2147483647)["match_count"], 0)

    def test_all_origin_outputs_qualify_but_context_and_dead_siblings_do_not(self):
        state = graph_state((("a:1", "b"), ("a:2", "c")), raw_links=(("d:0", "b"),), seeds=("a:0",))
        endpoint = add_pegout(state, tx("b"))
        add_pegout(state, tx("d"))
        graph = pegout_graph(state, validate_query(tx("a"), 1, 1))
        self.assertEqual(graph["pegouts"]["match_count"], 1)
        self.assertEqual({edge["id"] for edge in graph["edges"]},
                         {"out:" + tx("a")+":1", "in:"+tx("b")+":0", "out:"+endpoint})
        self.assertFalse(any(edge["role"].startswith("context") for edge in graph["edges"]))
        self.assertEqual(graph["graph_options"]["view"], "pegout_paths")

    def test_reused_addresses_and_raw_input_references_never_make_a_path(self):
        state = graph_state(raw_links=(("a:0", "b"),), seeds=("a:0", "b:0"))
        state["transactions"][tx("b")]["data"]["vout"][0] = deepcopy(state["transactions"][tx("a")]["data"]["vout"][0])
        add_pegout(state, tx("b"))
        self.assertEqual(report(state)["match_count"], 0)

    def test_shared_merge_retains_per_path_service_allowances(self):
        state = graph_state((("a:0", "d"), ("a:1", "b"), ("b:0", "c"),
                             ("c:0", "d"), ("d:0", "e")), seeds=("a:0",))
        add_pegout(state, tx("e"))
        capped = annotation(stop=False)
        capped.update(kind="outpoint", value=tx("a")+":0", hop_limit=1)
        state["labels"] = [capped]
        self.assertEqual(report(state, 0, 2)["match_count"], 0)
        result = report(state, 4, 4)
        self.assertEqual(result["matches"][0]["hops"], [4])
        self.assertNotIn(tx("a")+":0", result["outpoints"])
        self.assertEqual(len(result["outpoints"]), 4)
        later = annotation(stop=False, address="SYNTHETIC-c-address")
        later["hop_limit"] = 1
        state["labels"].append(later)
        self.assertEqual(report(state)["match_count"], 0)

    def test_stop_rules_exclude_saved_spends_but_do_not_hide_requests_already_reached(self):
        state = graph_state((("a:0", "b"), ("b:0", "c")), seeds=("a:0",))
        endpoint = add_pegout(state, tx("b"))
        add_pegout(state, tx("c"))
        state["labels"] = [annotation(stop=True, address="SYNTHETIC-b-address")]
        self.assertEqual([match["outpoint"] for match in report(state)["matches"]], [endpoint])
        state["labels"][0] = annotation(stop=True)
        self.assertEqual(report(state)["match_count"], 0)

    def test_unconfirmed_origin_and_descendant_require_saved_policy(self):
        for unconfirmed in ("a", "b"):
            state = graph_state((("a:0", "b"),), seeds=("a:0",))
            add_pegout(state, tx("b"))
            state["transactions"][tx(unconfirmed)]["data"]["status"] = {"confirmed": False}
            self.assertEqual(report(state)["match_count"], 0)
            state["include_unconfirmed"] = True
            self.assertEqual(report(state)["match_count"], 1)
        state = graph_state(seeds=("a:0",))
        add_pegout(state, tx("a"))
        state["transactions"][tx("a")]["data"]["status"] = {"confirmed": False}
        self.assertEqual(report(state, 0, 0)["match_count"], 0)

    def test_detects_unclassified_pegouts_from_full_reached_transaction(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        endpoint = add_pegout(state, tx("b"))
        state.update(status="paused", stop_reason="outpoint_limit")
        graph = pegout_graph(state, validate_query(tx("a")))
        self.assertNotIn(endpoint, state["outputs"])
        self.assertEqual(graph["pegouts"]["match_count"], 1)
        self.assertEqual(graph["pegouts"]["source_stop_reason"], "outpoint_limit")
        self.assertIn("does not confirm a Bitcoin payout", graph["notice"])
        self.assertIn("no result is not proof", graph["notice"])

    def test_corrupt_indices_outputs_and_spends_are_rejected(self):
        mutations = [
            lambda s: s["links"][tx("a")+":0"].update(vin=True),
            lambda s: s["links"][tx("a")+":0"].update(vin=99),
            lambda s: s["outputs"][tx("a")+":0"].update(vout=True),
            lambda s: s["outputs"][tx("a")+":0"].update(txid=tx("b")),
            lambda s: s["transactions"][tx("b")]["data"]["vin"][0].update(is_pegin=True),
            lambda s: s["transactions"][tx("b")]["data"]["vin"][0].update(is_coinbase=True),
            lambda s: s["transactions"][tx("b")]["data"]["vin"][0].update(vout=True),
            lambda s: s["transactions"][tx("b")]["data"]["vin"][0].update(prevout={"value": 999}),
            lambda s: s["transactions"][tx("b")]["data"]["vin"][0].update(prevout={"scriptpubkey_type": "op_return"}),
            lambda s: s["transactions"][tx("a")]["data"]["vout"][0].update(scriptpubkey_type="op_return"),
            lambda s: s["transactions"][tx("b")]["data"].update(txid=tx("c")),
            lambda s: s["transactions"][tx("b")]["data"]["vin"].append(deepcopy(s["transactions"][tx("b")]["data"]["vin"][0])),
        ]
        for mutation in mutations:
            state = graph_state((("a:0", "b"),), seeds=("a:0",))
            add_pegout(state, tx("b"))
            mutation(state)
            with self.subTest(mutation=mutation), self.assertRaises(TraceError):
                report(state)

    def test_invalid_pegout_metadata_and_cycles_fail_closed(self):
        for value in (True, "payout", ["payout"], {"genesis_hash": "bad"}, {"scriptpubkey_address": 4}):
            state = graph_state(seeds=("a:0",))
            state["transactions"][tx("a")]["data"]["vout"][0]["pegout"] = value
            with self.subTest(pegout=value), self.assertRaises(TraceError):
                report(state)
        for edges in ((("a:0", "a"),), (("a:0", "b"), ("b:0", "a"))):
            with self.subTest(edges=edges), self.assertRaises(TraceError):
                report(graph_state(edges, seeds=("a:0",)))

    def test_partial_bootstrap_and_generic_op_return_produce_empty_graph(self):
        state = graph_state(seeds=("a:0",))
        state["transactions"][tx("a")]["data"]["vout"][0].update(scriptpubkey="6a", scriptpubkey_type="op_return")
        self.assertEqual(report(state)["match_count"], 0)
        state["transactions"] = {}
        graph = pegout_graph(state, validate_query(tx("a")))
        self.assertEqual(graph["nodes"], [])
        self.assertEqual(graph["edges"], [])

    def test_parallel_utxos_and_shared_address_keep_separate_circles(self):
        state = graph_state((("a:0", "b"), ("a:1", "b")), seeds=("a:0",))
        add_pegout(state, tx("b"))
        graph = pegout_graph(state, validate_query(tx("a")))
        self.assertEqual(len([node for node in graph["nodes"] if node["kind"] == "address"]), 2)
        self.assertEqual(len(graph["edges"]), 5)
        before = deepcopy(state)
        validate_plan(make_plan(graph))
        state["links"] = dict(reversed(list(state["links"].items())))
        self.assertEqual(pegout_graph(state, validate_query(tx("a"))), graph)
        self.assertEqual(state, before)

    def test_miro_legend_preserves_search_scope_without_copying_arbitrary_notice(self):
        from liquid_tracer.export import build_graph
        from liquid_tracer.legend import legend_notes

        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        add_pegout(state, tx("b"))
        state["status"] = "paused"
        ordinary = build_graph(state)
        ordinary_notes = legend_notes(ordinary)
        self.assertEqual(len(ordinary_notes), 6)
        graph = pegout_graph(state, validate_query(tx("a"), 1, 4))
        graph["notice"] = "UNTRUSTED NOTICE MUST NOT BECOME LEGEND CONTENT"
        plan = make_plan(graph)
        validate_plan(plan)
        content = "".join(item["body"]["data"]["content"] for item in plan["shapes"]
                          if item["key"] == "legend" or plan.get("presentation_items", {}).get(item["key"], {}).get("kind") == "legend")
        self.assertIn(tx("a"), content)
        self.assertIn("Range: 1 to 4 transaction hops, inclusive", content)
        self.assertIn("origin is hop 0", content)
        self.assertIn("not confirmation of Bitcoin payouts", content)
        self.assertIn("Coverage: partial search", content)
        self.assertIn("outside the selected range", "".join(legend_notes(graph)))
        self.assertNotIn("UNTRUSTED NOTICE", content)
        self.assertEqual(legend_notes(ordinary), ordinary_notes)
        graph["pegouts"]["query"]["txid"] = "not-a-transaction"
        with self.assertRaises(TraceError):
            legend_notes(graph)

    def test_random_dags_match_exhaustive_bounded_path_oracle(self):
        rng = random.Random(5144)
        for iteration in range(50):
            ids = [f"{index+1:064x}" for index in range(7)]
            txs = {key: {"txid": key, "vin": [], "vout": [], "status": {"confirmed": True}}
                   for key in ids}
            edges = []
            for index, parent in enumerate(ids):
                for child in ids[index+1:]:
                    if rng.random() < .35:
                        out = output("SYNTHETIC-shared-address")
                        vout = len(txs[parent]["vout"])
                        txs[parent]["vout"].append(out)
                        vin = len(txs[child]["vin"])
                        txs[child]["vin"].append({"txid": parent, "vout": vout, "prevout": deepcopy(out)})
                        edges.append((parent, child, f"{parent}:{vout}", vin, rng.choice((0, 1, 2, 7))))
            state = state_from(txs)
            for parent, child, key, vin, cap in edges:
                state["links"][key] = {"spending_txid": child, "vin": vin}
                state["outputs"][key] = {"txid": parent, "vout": int(key.rsplit(":", 1)[1]), "status": "spent"}
                label = annotation(stop=False)
                label.update(kind="outpoint", value=key, hop_limit=cap)
                state["labels"].append(label)
            targets = {key for key in ids if rng.random() < .3}
            for key in targets:
                add_pegout(state, key)
            minimum, maximum = sorted((rng.randrange(5), rng.randrange(7)))
            expected_edges, expected_matches = set(), {}
            stack = [(ids[0], [], maximum)]
            while stack:
                current, path, allowance = stack.pop()
                if current in targets and minimum <= len(path) <= maximum:
                    expected_edges.update(path)
                    expected_matches.setdefault(current, set()).add(len(path))
                if len(path) >= maximum:
                    continue
                for parent, child, key, _, cap in edges:
                    budget = min(allowance, cap)
                    if parent == current and budget > 0:
                        stack.append((child, path + [key], budget - 1))
            actual = report(state, minimum, maximum, ids[0])
            self.assertEqual(set(actual["outpoints"]), expected_edges, iteration)
            self.assertEqual({row["txid"]: set(row["hops"]) for row in actual["matches"]}, expected_matches, iteration)

            # Compare selected-output searches against independent exhaustive
            # traversal too, including seed transactions reached downstream.
            saved = {key: record["data"] for key, record in state["transactions"].items()}
            candidates = [f"{key}:{index}" for key in ids for index in range(len(saved[key]["vout"]))]
            if not candidates:
                continue
            seeds = {key for key in candidates if rng.random() < .4} or {candidates[0]}
            expected_edges, expected_matches = set(), {}
            stack = [(root, [], maximum) for root in {key.split(":")[0] for key in seeds}]
            while stack:
                current, path, allowance = stack.pop()
                if minimum <= len(path) <= maximum:
                    for index, value in enumerate(saved[current]["vout"]):
                        key = f"{current}:{index}"
                        if value.get("pegout") and (path or key in seeds):
                            expected_edges.update(path)
                            expected_matches.setdefault(key, set()).add(len(path))
                if len(path) >= maximum:
                    continue
                for parent, child, key, _, cap in edges:
                    budget = min(allowance, cap)
                    if parent == current and budget > 0 and (path or key in seeds):
                        stack.append((child, path + [key], budget - 1))
            query = validate_query(seeds=sorted(seeds), min_hops=minimum, max_hops=maximum)
            actual = pegout_graph(state, query)["pegouts"]
            self.assertEqual(set(actual["outpoints"]), expected_edges, iteration)
            self.assertEqual({row["outpoint"]: set(row["hops"]) for row in actual["matches"]},
                             expected_matches, iteration)


if __name__ == "__main__":
    unittest.main()
