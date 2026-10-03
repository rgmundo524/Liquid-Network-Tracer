"""All-saved Starter display scope preserves evidence and other views' bounds."""
import copy
import math
import unittest

from liquid_tracer.common import LBTC, TraceError
from liquid_tracer.export import build_graph, legend_lines
from liquid_tracer.hop_limits import output_budget
from liquid_tracer.legend import legend_notes
from liquid_tracer.miro import make_plan, validate_plan
from liquid_tracer.named_hop_paths import walk_outputs
from liquid_tracer.saved_inputs import saved_input_output
from liquid_tracer.transaction_csv import transaction_csv_rows
from tests.test_attribution_convergence import annotation, graph_state, tx


class StarterScopeProvenanceTests(unittest.TestCase):
    def test_ignored_boundaries_do_not_hide_input_merge_or_branch_memberships(self):
        label = {**annotation(address="SYNTHETIC-c-address"), "hop_limit": 0}
        state = graph_state((("a:0", "c"), ("c:0", "d"), ("b:0", "d")), labels=[label])
        before = copy.deepcopy(state)
        default = build_graph(state)
        self.assertFalse(any(node.get("convergence") for node in default["nodes"]))
        graph = build_graph(state, respect_attribution_hops=False, respect_stops=False)
        merged = next(node for node in graph["nodes"] if node["id"] == "tx:" + tx("d"))
        self.assertEqual(merged["convergence"]["starting_transaction_indices"], [1, 2])
        self.assertEqual(graph["branch_structure"]["edge_memberships"]["in:" + tx("d") + ":0"],
                         ["tx:" + tx("a")])
        attributed = next(node for node in graph["nodes"] if node["id"] == "liquid:address:SYNTHETIC-c-address")
        self.assertIn("Collection stop (not applied here)", attributed["label"])
        self.assertNotIn("STOP TRACING", attributed["label"])
        self.assertEqual(attributed["details"]["address_attributions"], [label])
        validate_plan(make_plan(graph))
        self.assertEqual(state, before)

    def test_stop_and_cap_flags_are_independent_for_every_target_kind(self):
        state = graph_state()
        key = tx("a") + ":0"
        output = state["transactions"][tx("a")]["data"]["vout"][0]
        for kind, value in (("outpoint", key), ("script", output["scriptpubkey"]),
                            ("address", output["scriptpubkey_address"])):
            labels = [{"kind": kind, "value": value, "stop": True, "hop_limit": 2}]
            with self.subTest(kind=kind):
                self.assertEqual(output_budget(labels, key, output), 0)
                self.assertEqual(output_budget(labels, key, output, respect_attribution_hops=False), 0)
                self.assertEqual(output_budget(labels, key, output, respect_stops=False), 2)
                self.assertEqual(output_budget(labels, key, output, respect_attribution_hops=False,
                                               respect_stops=False), math.inf)

    def test_named_distances_reset_without_using_stop_or_cap_as_boundaries(self):
        state = graph_state((("a:0", "c"), ("c:0", "d"), ("d:0", "e"), ("e:0", "b")),
                            labels=[{**annotation(address="SYNTHETIC-c-address"), "hop_limit": 0},
                                    annotation(address="SYNTHETIC-e-address", stop=False, name="Known group")])
        state["hop_reference_name"] = "Known group"
        links = {key: value["spending_txid"] for key, value in state["links"].items()}
        before = copy.deepcopy(state)
        arguments = (state, [tx("a") + ":0"], links, 4, set(state["transactions"]))
        bounded, _ = walk_outputs(*arguments)
        self.assertFalse(any(point[0] == tx("b") + ":0" for point in bounded))
        seen, _ = walk_outputs(*arguments, respect_attribution_hops=False, respect_stops=False)
        self.assertIn((tx("e") + ":0", 0, math.inf), seen)
        self.assertIn((tx("b") + ":0", 1, math.inf), seen)
        self.assertEqual(state, before)

    def test_ignored_labels_never_supply_missing_spend_provenance(self):
        state = graph_state(raw_links=(("a:0", "c"), ("b:0", "c")))
        graph = build_graph(state, respect_attribution_hops=False, respect_stops=False)
        self.assertFalse(any(node.get("convergence") for node in graph["nodes"]))
        self.assertNotIn("tx:" + tx("c"), graph["branch_structure"]["node_memberships"])
        for mutate in (lambda s: s["links"][tx("a") + ":0"].update(vin=4),
                       lambda s: s["transactions"][tx("b")]["data"]["vin"][0].update(is_pegin=True)):
            invalid = graph_state((("a:0", "b"),))
            mutate(invalid)
            with self.assertRaises(TraceError):
                build_graph(invalid, respect_attribution_hops=False, respect_stops=False)
        with self.assertRaisesRegex(TraceError, "cycle"):
            build_graph(graph_state((("a:0", "b"), ("b:0", "a"))),
                        respect_attribution_hops=False, respect_stops=False)

    def test_scope_legend_is_explicit_and_markerless_notes_are_unchanged(self):
        legacy = {"graph_options": {"view": "starter_connections"}, "hop_reference_name": "Known group"}
        graph = {**legacy, "connections": {"connection_scope": "all_saved"}}
        notes = " ".join(legend_notes(graph))
        self.assertIn("do not restrict", notes)
        self.assertIn("unconfirmed", notes)
        self.assertNotIn("allowances still apply", notes)
        self.assertIn("allowances still apply", " ".join(legend_notes(legacy)))
        self.assertNotIn("STOP TRACING: an explicit address boundary", " ".join(legend_lines(graph)))
        self.assertIn("STOP TRACING: an explicit address boundary", " ".join(legend_lines(legacy)))


class SavedInputPresentationTests(unittest.TestCase):
    def test_resolved_missing_prevout_display_and_csv_preserve_original_transactions(self):
        state = graph_state((("a:0", "b"),))
        output = state["transactions"][tx("a")]["data"]["vout"][0]
        output.update(value=123456789, asset=LBTC)
        output.pop("valuecommitment"); output.pop("assetcommitment")
        state["transactions"][tx("b")]["data"]["vin"][0].pop("prevout")
        before = copy.deepcopy(state)
        graph = build_graph(state, merge_addresses=False, resolve_saved_inputs=True)
        inputs = [row for row in transaction_csv_rows(graph, state) if row["Direction"] == "IN"]
        self.assertEqual(len(inputs), 1)
        self.assertEqual((inputs[0]["Address Hash"], inputs[0]["Asset Value"], inputs[0]["Asset"]),
                         ("SYNTHETIC-a-address", 123456789, "L-BTC"))
        transaction = next(node for node in graph["nodes"] if node["id"] == "tx:" + tx("b"))
        self.assertNotIn("prevout", transaction["details"]["transaction"]["vin"][0])
        self.assertEqual(state, before)
        self.assertNotIn("resolve_saved_inputs", build_graph(state)["graph_options"])
        validate_plan(make_plan(graph))

    def test_resolution_rejects_contradictions_and_invalid_funding_indices(self):
        state = graph_state((("a:0", "b"),))
        base = state["transactions"][tx("b")]["data"]["vin"][0]
        for updates in ({"prevout": {"scriptpubkey_address": "SYNTHETIC-other"}},
                        {"vout": True}, {"vout": 99}, {"prevout": []}):
            with self.subTest(updates=updates), self.assertRaises(TraceError):
                saved_input_output(state["transactions"], {**base, **updates})
        state["transactions"][tx("a")]["data"]["vout"][0]["value"] = 0
        with self.assertRaises(TraceError):
            saved_input_output(state["transactions"], {**base, "prevout": {"value": False}})

    def test_resolution_never_uses_liquid_funding_for_bitcoin_or_unknown_inputs(self):
        state = graph_state((("a:0", "b"),))
        for vin in ({"txid": tx("a"), "vout": 0, "is_pegin": True},
                    {"txid": tx("a"), "vout": 0, "is_coinbase": True},
                    {"txid": tx("f"), "vout": 0}):
            self.assertEqual(saved_input_output(state["transactions"], vin), {})


if __name__ == "__main__":
    unittest.main()
