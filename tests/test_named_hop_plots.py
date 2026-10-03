"""Named-group ranges over saved evidence, independently on each output path."""

from copy import deepcopy
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.connections import connecting_outpoints, connection_graph
from liquid_tracer.investigations import create_investigation, save_collection_reference
from liquid_tracer.legend import legend_notes
from liquid_tracer.pegout_paths import pegout_graph, validate_query
from liquid_tracer.plot_scope import project_full_scope
from liquid_tracer.plots import preview_plot, reviewed_plot, list_plots
from liquid_tracer.services import set_service
from tests.test_attribution_convergence import annotation, graph_state, tx
from tests.test_connections import saved_case
from tests.test_pegout_paths import add_pegout, mark_unspent, set_address


def named_state(links, *, group=("a", "b"), seeds=("a:0",), maximum=1, raw_links=()):
    state = graph_state(links, seeds=seeds, raw_links=raw_links)
    state["hop_reference_name"] = "Perp"
    state["limits"] = {"max_hops": maximum}
    state["labels"] = [annotation(stop=False, name=" pErP ", address="SYNTHETIC-" + name + "-address")
                       for name in group]
    return state


def query(state, lower=0, upper=1, **options):
    return validate_query(seeds=state["seeds"], min_hops=lower, max_hops=upper, **options)


class NamedHopPlotTests(unittest.TestCase):
    def test_internal_chain_does_not_consume_range_and_departure_counts_one(self):
        state = named_state((("a:0", "b"), ("b:0", "c"), ("c:0", "d")), group=("a", "b", "c"))
        endpoint = add_pegout(state, tx("d"))
        original = deepcopy(state)
        graph = pegout_graph(state, query(state, 1, 1))
        self.assertEqual(graph["pegouts"]["matches"][0]["outpoint"], endpoint)
        self.assertEqual(graph["pegouts"]["matches"][0]["hops"], [1])
        self.assertEqual(set(graph["pegouts"]["outpoints"]), {tx(n) + ":0" for n in "abc"})
        self.assertEqual(graph["pegouts"]["query"]["hop_reference_name"], "Perp")
        self.assertTrue(any("group-relative hops" in note for note in legend_notes(graph)))
        self.assertEqual(state, original)

    def test_mixed_outputs_keep_separate_distances_and_return_resets(self):
        state = named_state((("a:0", "b"), ("b:0", "c"), ("b:1", "d"), ("d:0", "e")),
                            group=("a", "b", "d"))
        set_address(state, tx("b") + ":1", "SYNTHETIC-outside-address")
        endpoints = {add_pegout(state, tx(name)) for name in "bce"}
        graph = pegout_graph(state, query(state, 1, 1))
        self.assertEqual({row["outpoint"] for row in graph["pegouts"]["matches"]}, endpoints)
        self.assertTrue(all(row["hops"] == [1] for row in graph["pegouts"]["matches"]))
        self.assertEqual(set(graph["pegouts"]["outpoints"]),
                         {tx("a") + ":0", tx("b") + ":0", tx("b") + ":1", tx("d") + ":0"})

    def test_direct_boundary_return_allowed_but_over_limit_outside_cannot_continue(self):
        state = named_state((("a:0", "b"), ("b:0", "c"), ("c:0", "d"), ("d:0", "e")),
                            group=("a", "d"))
        add_pegout(state, tx("e"))
        self.assertEqual(pegout_graph(state, query(state))["pegouts"]["matches"], [])
        state["labels"].append(annotation(stop=False, name="Perp", address="SYNTHETIC-c-address"))
        graph = pegout_graph(state, query(state))
        self.assertEqual([row["hops"] for row in graph["pegouts"]["matches"]], [[1]])

    def test_zero_range_allows_internal_outputs_without_admitting_seed_siblings(self):
        state = named_state((("a:0", "b"), ("b:0", "c"), ("a:1", "d")),
                            group=("a", "b", "c", "d"), maximum=0)
        mark_unspent(state, tx("c") + ":0")
        mark_unspent(state, tx("d") + ":0")
        graph = pegout_graph(state, query(state, 0, 0, include_unspent=True))
        self.assertEqual([row["outpoint"] for row in graph["pegouts"]["endpoint_matches"]], [tx("c") + ":0"])
        self.assertEqual(set(graph["pegouts"]["outpoints"]), {tx("a") + ":0", tx("b") + ":0"})

    def test_attribution_allowance_does_not_replenish_at_group_returns(self):
        state = named_state((("a:0", "b"), ("b:0", "c"), ("c:0", "d")), group=("a", "b", "c"))
        state["labels"][0]["hop_limit"] = 2
        add_pegout(state, tx("d"))
        self.assertFalse(pegout_graph(state, query(state))["pegouts"]["matches"])
        state["labels"][0]["hop_limit"] = 3
        self.assertTrue(pegout_graph(state, query(state))["pegouts"]["matches"])
        state["labels"][1]["stop"] = True
        self.assertFalse(pegout_graph(state, query(state))["pegouts"]["matches"])

    def test_context_group_input_does_not_reset_unrelated_traced_path(self):
        state = named_state((("a:0", "b"), ("b:0", "c")), group=("a", "d"),
                            raw_links=(("d:0", "c"),))
        add_pegout(state, tx("c"))
        self.assertFalse(pegout_graph(state, query(state, include_context=True))["pegouts"]["matches"])

    def test_query_cannot_override_saved_collection_basis(self):
        state = named_state((("a:0", "b"),))
        with self.assertRaisesRegex(TraceError, "match the selected collection"):
            pegout_graph(state, {**query(state), "hop_reference_name": "Other"})
        self.assertNotIn("hop_reference_name", validate_query(seeds=state["seeds"]))

    def test_full_scope_recalculates_membership_and_omits_boundary_only_transactions(self):
        state = named_state((("a:0", "b"), ("b:0", "c"), ("c:0", "d")), group=("a", "b"))
        original = deepcopy(state)
        projected = project_full_scope(state)
        self.assertEqual(set(projected["transactions"]), {tx(n) for n in "abc"})
        self.assertEqual({key: value["reference_hops"] for key, value in projected["transactions"].items()},
                         {tx("a"): 0, tx("b"): 0, tx("c"): 1})
        state["labels"] = state["labels"][:1]
        changed = project_full_scope(state)
        self.assertEqual(set(changed["transactions"]), {tx("a"), tx("b")})
        self.assertEqual(original["transactions"], state["transactions"])
        self.assertEqual(original["outputs"], state["outputs"])

    def test_starter_connections_apply_group_depth_and_keep_output_paths_separate(self):
        state = named_state((("a:0", "b"), ("b:0", "c"), ("c:0", "d")),
                            group=("a", "b", "c"), seeds=("a:0", "d:0"))
        report = connecting_outpoints(state, 1)
        self.assertEqual(report["pairs"], [{"source": tx("a"), "target": tx("d"), "shortest_hops": 1}])
        self.assertEqual(set(report["outpoints"]), {tx(n) + ":0" for n in "abc"})
        self.assertEqual(connection_graph(state, 1)["connections"]["hop_reference_name"], "Perp")
        self.assertFalse(connecting_outpoints(state, 0)["pairs"])

    def test_short_exhausted_path_cannot_borrow_allowance_from_over_limit_arrival(self):
        state = named_state((("a:0", "d"), ("b:0", "c"), ("c:0", "d"),
                             ("d:0", "e"), ("f:0", "e")),
                            group=("a", "b", "f"), seeds=("a:0", "b:0", "f:0"))
        state["labels"][0]["hop_limit"] = 1
        projected = project_full_scope(state)
        self.assertIn(tx("e"), projected["transactions"])
        self.assertNotIn(tx("d") + ":0", projected["links"])
        add_pegout(state, tx("e"))
        graph = pegout_graph(state, query(state, 1, 1))
        self.assertEqual(graph["pegouts"]["outpoints"], [tx("f") + ":0"])

    def test_merged_arrivals_retain_all_distances_without_changing_effective_output_depth(self):
        state = named_state((("a:0", "d"), ("b:0", "c"), ("c:0", "d"), ("d:0", "e")),
                            group=("a", "b"), seeds=("a:0", "b:0"), maximum=3)
        endpoint = add_pegout(state, tx("e"))
        graph = pegout_graph(state, query(state, 0, 3))
        self.assertEqual([(row["outpoint"], row["hops"]) for row in graph["pegouts"]["matches"]],
                         [(endpoint, [2, 3])])
        transaction = next(node for node in graph["nodes"] if node["id"] == "tx:" + tx("e"))
        self.assertEqual(transaction["details"]["reference_hops"], 2)
        state["labels"][0]["hop_limit"] = 1
        bounded = pegout_graph(state, query(state, 0, 3))["pegouts"]
        self.assertEqual(bounded["matches"][0]["hops"], [3])
        self.assertEqual(set(bounded["outpoints"]), {tx(n) + ":0" for n in "bcd"})

    def test_random_dags_match_exhaustive_output_path_oracle(self):
        rng = random.Random(8129)
        for iteration in range(35):
            names = "abcdef"
            links = []
            for offset, parent in enumerate(names[:-1]):
                children = [names[offset + 1]]
                children.extend(child for child in names[offset + 2:] if rng.random() < .35)
                links.extend((f"{parent}:{index}", child) for index, child in enumerate(children))
            state = named_state(links, group=())
            groups, caps = set(), {}
            for key in state["outputs"]:
                if rng.random() < .45:
                    groups.add(key)
                    set_address(state, key, "SYNTHETIC-group-address")
                if rng.random() < .4:
                    caps[key] = rng.randrange(4)
                    label = annotation(stop=False)
                    label.update(kind="outpoint", value=key, hop_limit=caps[key])
                    state["labels"].append(label)
            state["labels"].append(annotation(stop=False, name="Perp", address="SYNTHETIC-group-address"))
            endpoints = {add_pegout(state, tx(name)) for name in names if rng.random() < .55}
            candidates = list(state["outputs"]) + sorted(endpoints)
            seeds = {key for key in candidates if rng.random() < .2} or {tx("a") + ":0"}
            state["seeds"] = sorted(seeds)
            lower, upper = sorted((rng.randrange(4), rng.randrange(4)))
            expected_edges, expected_matches = set(), {}
            stack = [(key, 0, float("inf"), []) for key in seeds]
            while stack:
                key, depth, allowance, path = stack.pop()
                if depth > upper:
                    continue
                allowance = min(allowance, caps.get(key, float("inf")))
                if key in endpoints and depth >= lower:
                    expected_edges.update(path)
                    expected_matches.setdefault(key, set()).add(depth)
                if allowance <= 0 or key not in state["links"]:
                    continue
                child = state["links"][key]["spending_txid"]
                for index in range(len(state["transactions"][child]["data"]["vout"])):
                    following = f"{child}:{index}"
                    stack.append((following, 0 if following in groups else depth + 1,
                                  allowance - 1, [*path, key]))
            result = pegout_graph(state, query(state, lower, upper))["pegouts"]
            self.assertEqual(set(result["outpoints"]), expected_edges, iteration)
            self.assertEqual({row["outpoint"]: set(row["hops"]) for row in result["matches"]},
                             expected_matches, iteration)


class NamedHopSavedPlotTests(unittest.TestCase):
    def test_saved_run_basis_and_group_membership_survive_current_preference_and_label_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            case = create_investigation(Path(temporary), "Named hops", seeds=[tx("a") + ":0"])
            state = named_state((("a:0", "b"), ("b:0", "c")), group=())
            add_pegout(state, tx("c"))
            state, archive = saved_case(case, state)
            set_service(case, "SYNTHETIC-a-address", name="Perp", stop_tracing=False)
            set_service(case, "SYNTHETIC-b-address", name="Perp", stop_tracing=False)
            before = {path.name: path.read_bytes() for path in archive.iterdir() if path.is_file()}
            with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda value, **_: value):
                first = preview_plot(case, "pegouts", max_hops=1)
                second = preview_plot(case, "full")
            self.assertEqual(first["hop_reference_name"], "Perp")
            self.assertEqual(first["query"]["hop_reference_name"], "Perp")
            # Listing both goals exercises the compact per-source review cache.
            self.assertTrue(all(row["reviewable"] for row in list_plots(case)))
            save_collection_reference(case, "Different")
            self.assertEqual(reviewed_plot(case, first["preview_id"])[0]["plot"]["hop_reference_name"], "Perp")
            self.assertEqual(reviewed_plot(case, second["preview_id"])[0]["plot"]["hop_reference_name"], "Perp")
            original = reviewed_plot(case, first["preview_id"])
            set_service(case, "SYNTHETIC-b-address", name="Other", stop_tracing=False)
            self.assertEqual(reviewed_plot(case, first["preview_id"]), original)
            with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda value, **_: value):
                changed = preview_plot(case, "pegouts", max_hops=1)
            self.assertEqual(changed["match_count"], 0)
            self.assertEqual({path.name: path.read_bytes() for path in archive.iterdir() if path.is_file()}, before)


if __name__ == "__main__":
    unittest.main()
