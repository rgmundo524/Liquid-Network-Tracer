"""Inclusive peg-out path ranges over exact saved UTXO evidence."""
from copy import deepcopy
import random
import unittest

from liquid_tracer.common import LBTC, TraceError
from liquid_tracer.export import COLORS, build_graph, short_address
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


def add_unspendable(state, txid):
    rows = state["transactions"][txid]["data"]["vout"]
    index = len(rows)
    rows.append({"scriptpubkey": "6a", "scriptpubkey_type": "op_return", "value": 0, "asset": LBTC})
    return f"{txid}:{index}"


def mark_unspent(state, key, observation_id=7):
    state["outputs"][key].update(status="unspent_at_observation",
                                observed_spend={"spent": False}, spend_observation_id=observation_id)


def report(state, minimum=0, maximum=10, origin=None):
    return pegout_graph(state, validate_query(origin or tx("a"), minimum, maximum))["pegouts"]


def set_address(state, key, address):
    """Keep synthetic funding and spending evidence consistent."""
    parent, index = key.rsplit(":", 1)
    state["transactions"][parent]["data"]["vout"][int(index)]["scriptpubkey_address"] = address
    for record in state["transactions"].values():
        for vin in record["data"]["vin"]:
            if vin.get("txid") == parent and vin.get("vout") == int(index):
                vin["prevout"]["scriptpubkey_address"] = address


class PegoutPathTests(unittest.TestCase):
    def test_optional_endpoint_query_flags_are_strict_and_preserve_disabled_queries(self):
        legacy = validate_query(tx("a"))
        self.assertEqual(validate_query(tx("a"), include_unspent=False, include_unspendable=False), legacy)
        self.assertEqual(validate_query(tx("a"), include_unspent=True), {**legacy, "include_unspent": True})
        self.assertEqual(validate_query(seeds=[tx("a") + ":0"], include_unspendable=True),
                         {"seeds": [tx("a") + ":0"], "min_hops": 0, "max_hops": 10,
                          "include_unspendable": True})
        for field in ("include_unspent", "include_unspendable"):
            for value in (None, 0, 1, "true", [], {}):
                with self.subTest(field=field, value=value), self.assertRaises(TraceError):
                    validate_query(tx("a"), **{field: value})
                with self.subTest(graph_field=field, value=value), self.assertRaises(TraceError):
                    pegout_graph(graph_state(), {**legacy, field: value})

    def test_optional_endpoints_keep_pegout_report_and_exact_observation_evidence(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        unspent = tx("b") + ":0"
        mark_unspent(state, unspent, "saved-observation")
        unspendable = add_unspendable(state, tx("b"))
        pegout = add_pegout(state, tx("b"))
        data = state["transactions"][tx("b")]["data"]
        fee = tx("b") + ":" + str(len(data["vout"]))
        data["vout"].append({"scriptpubkey": "", "scriptpubkey_type": "fee", "value": 1, "asset": LBTC})
        before = deepcopy(state)
        ordinary = pegout_graph(state, validate_query(seeds=state["seeds"]))
        self.assertEqual(pegout_graph(state, {**validate_query(seeds=state["seeds"]),
                                              "include_unspent": False, "include_unspendable": False}), ordinary)
        self.assertNotIn("endpoint_matches", ordinary["pegouts"])
        self.assertNotIn("endpoint_count", ordinary["pegouts"])
        self.assertNotIn("observed unspent", ordinary["notice"])
        query = validate_query(seeds=state["seeds"], include_unspent=True, include_unspendable=True)
        graph = pegout_graph(state, query)
        result = graph["pegouts"]
        self.assertEqual(result["matches"], ordinary["pegouts"]["matches"])
        self.assertEqual(result["match_count"], 1)
        self.assertEqual(result["status"], "pegouts_found")
        self.assertEqual(result["endpoint_counts"], {"pegout": 1, "unspent": 1, "unspendable": 1})
        self.assertEqual(result["endpoint_count"], 3)
        by_key = {match["outpoint"]: match for match in result["endpoint_matches"]}
        self.assertEqual({key: row["kind"] for key, row in by_key.items()},
                         {pegout: "pegout", unspent: "unspent", unspendable: "unspendable"})
        self.assertEqual(by_key[unspent]["observed_spend"], {"spent": False})
        self.assertEqual(by_key[unspent]["spend_observation_id"], "saved-observation")
        self.assertEqual(by_key[unspent]["trace"], state["outputs"][unspent])
        self.assertEqual(by_key[unspendable]["output"], data["vout"][1])
        self.assertIsNone(by_key[unspendable]["trace"])
        self.assertTrue(all(match["hops"] == [1] for match in by_key.values()))
        self.assertEqual({edge["id"] for edge in graph["edges"]},
                         {"out:" + state["seeds"][0], "in:" + tx("b") + ":0",
                          "out:" + unspent, "out:" + unspendable, "out:" + pegout})
        self.assertNotIn("out:" + fee, {edge["id"] for edge in graph["edges"]})
        self.assertEqual(graph["fee_items"], {})
        self.assertIn("status at observation", result["scope"])
        self.assertIn("Unspendable endpoints", result["scope"])
        validate_plan(make_plan(graph))
        self.assertEqual(state, before)

    def test_endpoint_bounds_and_service_limits_keep_only_successful_paths(self):
        state = graph_state((("a:0", "b"), ("b:0", "c"), ("a:1", "d")), seeds=("a:0",))
        at_one = add_unspendable(state, tx("b"))
        at_two = tx("c") + ":0"
        mark_unspent(state, at_two)
        mark_unspent(state, tx("d") + ":0")
        query = validate_query(seeds=state["seeds"], min_hops=1, max_hops=1,
                               include_unspent=True, include_unspendable=True)
        at_boundary = pegout_graph(state, query)
        self.assertEqual([(row["outpoint"], row["hops"]) for row in at_boundary["pegouts"]["endpoint_matches"]],
                         [(at_one, [1])])
        self.assertEqual(at_boundary["pegouts"]["status"], "endpoints_found")
        self.assertEqual(at_boundary["pegouts"]["outpoints"], state["seeds"])
        deeper = pegout_graph(state, {**query, "min_hops": 2, "max_hops": 2})
        self.assertEqual([(row["outpoint"], row["hops"]) for row in deeper["pegouts"]["endpoint_matches"]],
                         [(at_two, [2])])
        capped = annotation(stop=False)
        capped["hop_limit"] = 1
        state["labels"] = [capped]
        limited = pegout_graph(state, {**query, "max_hops": 10})
        self.assertEqual([row["outpoint"] for row in limited["pegouts"]["endpoint_matches"]], [at_one])
        state["labels"] = [annotation(stop=True, address="SYNTHETIC-b-address")]
        stopped = pegout_graph(state, {**query, "max_hops": 10})
        self.assertEqual([row["outpoint"] for row in stopped["pegouts"]["endpoint_matches"]], [at_one])
        state["labels"] = [annotation(stop=True)]
        empty = pegout_graph(state, {**query, "max_hops": 10})
        self.assertEqual(empty["pegouts"]["endpoint_count"], 0)
        self.assertEqual(empty["pegouts"]["status"], "no_endpoints_found")
        self.assertEqual(empty["nodes"], [])

    def test_original_archive_spends_override_stale_unspent_on_excluded_branches(self):
        for recorded_link in (False, True):
            links = (("a:0", "b"), ("b:0", "c")) if recorded_link else (("a:0", "b"),)
            raw = () if recorded_link else (("b:0", "c"),)
            state = graph_state(links, raw_links=raw, seeds=("a:0", "b:1"))
            stale, current = tx("b") + ":0", tx("b") + ":1"
            mark_unspent(state, stale)
            mark_unspent(state, current)
            before = deepcopy(state)
            graph = pegout_graph(state, validate_query(seeds=[tx("a") + ":0"], max_hops=1,
                                                       include_unspent=True))
            with self.subTest(recorded_link=recorded_link):
                self.assertEqual([row["outpoint"] for row in graph["pegouts"]["endpoint_matches"]], [current])
                self.assertEqual({edge["outpoint"] for edge in graph["edges"]}, {tx("a") + ":0", current})
                shared = next(node for node in graph["nodes"] if node["id"] == "liquid:address:SYNTHETIC-b-address")
                self.assertEqual(shared["details"]["unspent_endpoints"], [current])
                self.assertEqual({row["outpoint"] for row in shared["details"]["occurrences"]}, {current})
                self.assertEqual(state, before)

    def test_unspent_requires_saved_observation_and_not_only_a_terminal_status(self):
        invalid = [{"status": status} for status in ("hop_limit", "attribution_hop_limit", "suspected_service_stop",
                                                    "held_behind_service", "unconfirmed", "pending")]
        invalid.extend({"spend_observation_id": value} for value in (None, False, True, 0, -1, "", "  "))
        invalid.extend({"observed_spend": value} for value in (None, {}, {"spent": True}, {"spent": 0}))
        for change in invalid:
            state = graph_state(seeds=("a:0",))
            mark_unspent(state, state["seeds"][0])
            state["outputs"][state["seeds"][0]].update(change)
            with self.subTest(change=change):
                graph = pegout_graph(state, validate_query(seeds=state["seeds"], include_unspent=True))
                self.assertEqual(graph["pegouts"]["endpoint_count"], 0)
                self.assertEqual(graph["nodes"], [])
        for change in ({"vout": True}, {"vout": 1}, {"txid": tx("b")}, {"outpoint": tx("b") + ":0"}):
            state = graph_state(seeds=("a:0",))
            mark_unspent(state, state["seeds"][0])
            state["outputs"][state["seeds"][0]].update(change)
            with self.subTest(inconsistent=change), self.assertRaises(TraceError):
                pegout_graph(state, validate_query(seeds=state["seeds"], include_unspent=True))

    def test_hop_zero_endpoint_selection_excludes_other_outputs_and_no_implicit_kinds(self):
        state = graph_state(seeds=("a:0", "a:1"))
        for key in state["seeds"]:
            mark_unspent(state, key)
        unspendable = add_unspendable(state, tx("a"))
        selected = [state["seeds"][0], unspendable]
        query = validate_query(seeds=selected, max_hops=0, include_unspent=True, include_unspendable=True)
        graph = pegout_graph(state, query)
        self.assertEqual({row["outpoint"] for row in graph["pegouts"]["endpoint_matches"]}, set(selected))
        self.assertTrue(all(row["hops"] == [0] for row in graph["pegouts"]["endpoint_matches"]))
        self.assertEqual({edge["id"] for edge in graph["edges"]}, {"out:" + key for key in selected})
        for option, expected in (("include_unspent", state["seeds"][0]), ("include_unspendable", unspendable)):
            filtered = pegout_graph(state, validate_query(seeds=selected, max_hops=0, **{option: True}))
            self.assertEqual([row["outpoint"] for row in filtered["pegouts"]["endpoint_matches"]], [expected])
        state["transactions"][tx("a")]["data"]["status"] = {"confirmed": False}
        self.assertEqual(pegout_graph(state, query)["pegouts"]["endpoint_count"], 0)
        state["include_unconfirmed"] = True
        self.assertEqual(pegout_graph(state, query)["pegouts"]["endpoint_count"], 2)

    def test_multiple_unspent_utxos_keep_exact_arrows_while_addresses_are_merged(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0", "b:1"))
        endpoints = {tx("b") + ":0", tx("b") + ":1"}
        for key in endpoints:
            mark_unspent(state, key)
        query = validate_query(seeds=[tx("a") + ":0"], include_unspent=True)
        graph = pegout_graph(state, query)
        self.assertEqual(graph["pegouts"]["endpoint_count"], 2)
        self.assertEqual(graph["pegouts"]["match_count"], 0)
        node = next(node for node in graph["nodes"] if node["id"] == "liquid:address:SYNTHETIC-b-address")
        self.assertEqual({row["outpoint"] for row in node["details"]["occurrences"]}, endpoints)
        self.assertEqual(set(node["details"]["unspent_endpoints"]), endpoints)
        self.assertEqual(node["role"], "unspent_endpoint")
        self.assertEqual({edge["id"] for edge in graph["edges"] if edge["target"] == node["id"]},
                         {"out:" + key for key in endpoints})
        validate_plan(make_plan(graph))
        for key in endpoints:
            set_address(state, key, None)
        unknown = pegout_graph(state, query)
        self.assertEqual({node["id"] for node in unknown["nodes"] if node.get("role") == "unspent_endpoint"},
                         {"liquid:outpoint:" + key for key in endpoints})

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

    def test_parallel_utxos_share_address_circle_but_keep_exact_connectors(self):
        state = graph_state((("a:0", "b"), ("a:1", "b")), seeds=("a:0",))
        endpoint = add_pegout(state, tx("b"))
        before = deepcopy(state)
        graph = pegout_graph(state, validate_query(tx("a")))
        addresses = [node for node in graph["nodes"] if node["kind"] == "address"]
        self.assertEqual(len(addresses), 1)
        self.assertEqual(addresses[0]["id"], "liquid:address:SYNTHETIC-a-address")
        self.assertEqual({row["outpoint"] for row in addresses[0]["details"]["occurrences"]},
                         {tx("a") + ":0", tx("a") + ":1"})
        edges = {edge["id"]: edge for edge in graph["edges"]}
        self.assertEqual(set(edges), {"out:" + tx("a") + ":0", "out:" + tx("a") + ":1",
                                     "in:" + tx("b") + ":0", "in:" + tx("b") + ":1", "out:" + endpoint})
        for index in (0, 1):
            key = tx("a") + ":" + str(index)
            incoming, outgoing = edges[f"in:{tx('b')}:{index}"], edges["out:" + key]
            self.assertEqual(incoming["source"], addresses[0]["id"])
            self.assertEqual(outgoing["target"], addresses[0]["id"])
            self.assertEqual(incoming["outpoint"], key)
            self.assertEqual(incoming["details"]["vin"]["vout"], index)
            self.assertEqual((incoming["label"], outgoing["label"]), (f"vin {index}", f"vout {index}"))
        self.assertEqual(graph["address_mode"], "merged")
        self.assertEqual(graph["namespace"]["address_mode"], "merged")
        validate_plan(make_plan(graph))
        self.assertEqual(state, before)
        state["links"] = dict(reversed(list(state["links"].items())))
        state["transactions"] = dict(reversed(list(state["transactions"].items())))
        self.assertEqual(pegout_graph(state, validate_query(tx("a"))), graph)
        self.assertEqual(state, before)

    def test_repeated_address_across_hops_does_not_duplicate_node_or_shorten_paths(self):
        state = graph_state((("a:0", "b"), ("b:0", "c")), seeds=("a:0",))
        address = "SYNTHETIC-shared-across-hops"
        for name in ("a", "b"):
            set_address(state, tx(name) + ":0", address)
        endpoint = add_pegout(state, tx("c"))
        before = deepcopy(state)
        graph = pegout_graph(state, validate_query(seeds=state["seeds"], min_hops=2, max_hops=2))
        self.assertEqual([node["id"] for node in graph["nodes"] if node["kind"] == "address"],
                         ["liquid:address:" + address])
        self.assertEqual([(row["outpoint"], row["hops"]) for row in graph["pegouts"]["matches"]],
                         [(endpoint, [2])])
        self.assertEqual(len(graph["edges"]), 5)
        self.assertEqual(report(state, 0, 1)["match_count"], 0)
        validate_plan(make_plan(graph))
        self.assertEqual(state, before)

    def test_excluded_siblings_and_context_cannot_pollute_a_shared_address(self):
        state = graph_state((("a:0", "b"), ("b:1", "c")), raw_links=(("d:0", "b"),),
                            seeds=("a:0", "b:0"))
        shared = "SYNTHETIC-shared-path-and-context"
        kept = tx("b") + ":1"
        excluded = {tx("b") + ":0", tx("d") + ":0"}
        for key in [kept, *excluded]:
            set_address(state, key, shared)
        for number, key in enumerate(sorted(excluded)):
            label = annotation(stop=True, name=f"Excluded occurrence {number}")
            label.update(kind="outpoint", value=key)
            state["labels"].append(label)
        state["outputs"][tx("b") + ":0"].update(status="unspent_at_observation",
                observed_spend={"spent": False}, spend_observation_id=1)
        endpoint = add_pegout(state, tx("c"))
        before = deepcopy(state)
        graph = pegout_graph(state, validate_query(seeds=state["seeds"], max_hops=2),
                             color_attribution_arrows=True)
        shared_node = next(node for node in graph["nodes"] if node["id"] == "liquid:address:" + shared)
        self.assertEqual({row["outpoint"] for row in shared_node["details"]["occurrences"]}, {kept})
        self.assertEqual(shared_node["role"], "candidate")
        self.assertEqual(shared_node["color"], COLORS["candidate"])
        self.assertNotIn("Excluded occurrence", shared_node["label"])
        self.assertNotIn("STOP TRACING", shared_node["label"])
        self.assertNotIn("Unspent", shared_node["label"])
        self.assertNotIn("address_attributions", shared_node["details"])
        self.assertEqual({edge["outpoint"] for edge in graph["edges"]},
                         {tx("a") + ":0", kept, endpoint})
        # Display filtering must not rewrite the archived transaction context.
        for node in graph["nodes"]:
            if node["kind"] == "transaction":
                self.assertEqual(node["details"]["transaction"], state["transactions"][node["id"][3:]]["data"])
        self.assertEqual(state, before)

    def test_unknown_addresses_and_same_destination_requests_remain_distinct(self):
        state = graph_state((("a:0", "b"), ("a:1", "b")), seeds=("a:0", "a:1"))
        for index in (0, 1):
            set_address(state, f"{tx('a')}:{index}", None)
        endpoints = {add_pegout(state, tx("b")), add_pegout(state, tx("b"))}
        graph = pegout_graph(state, validate_query(seeds=state["seeds"]))
        self.assertEqual({node["id"] for node in graph["nodes"] if node["kind"] == "address"},
                         {f"liquid:outpoint:{tx('a')}:0", f"liquid:outpoint:{tx('a')}:1"})
        self.assertEqual({node["id"] for node in graph["nodes"] if node["kind"] == "event"},
                         {"event:" + key for key in endpoints})
        self.assertEqual(graph["pegouts"]["match_count"], 2)
        self.assertEqual(len(graph["edges"]), 6)
        validate_plan(make_plan(graph))

    def test_same_short_label_does_not_merge_different_full_addresses(self):
        state = graph_state((("a:0", "b"), ("a:1", "b")), seeds=("a:0", "a:1"))
        addresses = ["SYNTHETIC-one-full-address-shared-end", "SYNTHETIC-other-full-address-shared-end"]
        self.assertEqual(short_address(addresses[0]), short_address(addresses[1]))
        for index, address in enumerate(addresses):
            set_address(state, f"{tx('a')}:{index}", address)
        add_pegout(state, tx("b"))
        graph = pegout_graph(state, validate_query(seeds=state["seeds"]))
        self.assertEqual({node["id"] for node in graph["nodes"] if node["kind"] == "address"},
                         {"liquid:address:" + address for address in addresses})

    def test_edge_selection_preserves_network_separation_and_original_indices(self):
        state = graph_state(seeds=("a:0",))
        data = state["transactions"][tx("a")]["data"]
        data["vin"] = [{"txid": tx("d"), "vout": 2, "prevout": deepcopy(data["vout"][0]), "is_pegin": True}]
        kept = {f"in:{tx('a')}:0", f"out:{tx('a')}:0"}
        graph = build_graph(state, edge_ids=kept)
        self.assertEqual({node["id"] for node in graph["nodes"] if node["kind"] == "address"},
                         {"bitcoin:address:SYNTHETIC-a-address", "liquid:address:SYNTHETIC-a-address"})
        self.assertEqual({edge["id"] for edge in graph["edges"]}, kept)

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
