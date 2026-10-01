"""Named-group distances remain path-local and separate from spend budgets."""
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.api import Esplora, Limits
from liquid_tracer.group_hops import reference_addresses, normalize_reference_name
from liquid_tracer.plot_scope import project_full_scope
from liquid_tracer.store import Store
from liquid_tracer.trace import new_state, trace
from tests.fixtures import CONFIRMED, output
from tests.test_trace_concurrency import RecordingTransport, evidence_topology


def network():
    rows = {
        "A": ([], ["group-a"]),
        "B": ([("A", 0)], ["group-b", "group-c"]),
        "C": ([("B", 0)], ["group-d", "outside-x"]),
        "D": ([("B", 1)], ["group-e"]),
        "E": ([("C", 1)], ["outside-y"]),
        "F": ([("C", 0)], ["group-f", "outside-z"]),
        "G": ([("E", 0)], ["group-return"]),
        "H": ([("G", 0)], ["outside-again"]),
        "I": ([("H", 0)], ["outside-two"]),
        "J": ([("I", 0)], ["outside-three"]),
        "K": ([("J", 0)], ["group-too-late"]),
    }
    ids = {name: hashlib.sha256(("group-hops-" + name).encode()).hexdigest() for name in rows}
    data, labels = {}, []
    for name, (parents, addresses) in rows.items():
        data["/tx/" + ids[name]] = {"txid": ids[name], "status": dict(CONFIRMED),
            "vin": [{"txid": ids[parent], "vout": index,
                     "prevout": output("SYNTHETIC-" + rows[parent][1][index])}
                    for parent, index in parents],
            "vout": [output("SYNTHETIC-" + address) for address in addresses]}
        spends = []
        for index, address in enumerate(addresses):
            target = next(((child, children.index((name, index)))
                           for child, (children, _) in rows.items() if (name, index) in children), None)
            spends.append({"spent": True, "txid": ids[target[0]], "vin": target[1],
                           "status": dict(CONFIRMED)} if target else {"spent": False})
            if address.startswith("group-"):
                labels.append({"kind": "address", "value": "SYNTHETIC-" + address,
                               "entity": "Perp", "stop": False})
        data["/tx/" + ids[name] + "/outspends"] = spends
    return ids, data, labels


class NamedGroupHopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ids, self.data, self.labels = network()
        self.counter = 0

    def key(self, name, index=0):
        return self.ids[name] + ":" + str(index)

    def run_trace(self, *, hops=2, name="Perp", workers=1, parent=None, labels=None,
                  limits=None, only=None, seeds=None):
        self.counter += 1
        directory = self.root / str(self.counter)
        limits = limits or Limits(max_hops=hops)
        transport = RecordingTransport(self.data)
        store = Store(directory)
        try:
            with Esplora(store, "pending", limits, auth="none", transport=transport,
                         min_interval=0, workers=workers, advertised_rps=1_000_000) as api:
                state = new_state(seeds or [self.key("A")], api.base, limits,
                                  copy.deepcopy(self.labels if labels is None else labels), parent)
                state["hop_reference_name"] = name
                api.run_id = state["run_id"]
                result = trace(api, state, limits, directory / "trace.json", only=only)
        finally:
            store.close()
        self.assertEqual(result["errors"], [])
        return result, transport

    def test_group_chains_mixed_outputs_and_returns_have_independent_hops(self):
        state, _ = self.run_trace()
        depths = {self.key(n, i): d for n, i, d in (
            ("A", 0, 0), ("B", 0, 0), ("B", 1, 0), ("C", 0, 0), ("C", 1, 1),
            ("D", 0, 0), ("E", 0, 2), ("F", 0, 0), ("F", 1, 1),
            ("G", 0, 0), ("H", 0, 1), ("I", 0, 2), ("J", 0, 3))}
        self.assertEqual({key: row["trace_scope_depth"] for key, row in state["outputs"].items()}, depths)
        self.assertEqual(state["transactions"][self.ids["C"]]["reference_hops"], 1)
        self.assertEqual(state["transactions"][self.ids["G"]]["reference_hops"], 0)
        self.assertEqual(state["transactions"][self.ids["G"]]["depth"], 4)
        self.assertEqual(state["outputs"][self.key("G")]["depth"], 4)
        self.assertEqual(state["links"][self.key("E")]["hop"], 4)

    def test_boundary_lookahead_does_not_follow_external_overlimit_output(self):
        state, transport = self.run_trace()
        self.assertIn(self.ids["J"], state["transactions"])
        self.assertNotIn(self.ids["K"], state["transactions"])
        self.assertNotIn("/tx/" + self.ids["J"] + "/outspends", transport.calls)
        self.assertEqual(state["outputs"][self.key("J")]["trace_control"]["reason"], "named_group_hop_limit")
        self.assertIsNone(state["transactions"][self.ids["J"]]["reference_hops"])

    def test_zero_allows_internal_group_activity_with_boundary_inspection(self):
        state, _ = self.run_trace(hops=0)
        self.assertEqual(set(state["transactions"]), {self.ids[n] for n in "ABCDF"})
        self.assertNotIn(self.ids["E"], state["transactions"])
        self.assertEqual(state["outputs"][self.key("C", 1)]["trace_control"]["reason"], "named_group_hop_limit")
        self.assertTrue(all(record["reference_hops"] == 0 for record in state["transactions"].values()))

    def test_parallel_requests_match_serial_and_other_caps_still_bound_zero_chains(self):
        serial, _ = self.run_trace(hops=0)
        parallel, _ = self.run_trace(hops=0, workers=8)
        self.assertEqual(evidence_topology(serial), evidence_topology(parallel))
        for workers in (1, 8):
            state, _ = self.run_trace(workers=workers, limits=Limits(max_hops=0, max_transactions=2))
            self.assertEqual(state["stop_reason"], "transaction_limit")
            self.assertEqual(len(state["transactions"]), 2)

    def test_context_input_cannot_reset_an_external_branch(self):
        self.data["/tx/" + self.ids["E"]]["vin"].append({"txid": "f" * 64, "vout": 0,
            "prevout": output("SYNTHETIC-group-a")})
        state, _ = self.run_trace(hops=1)
        self.assertEqual(state["outputs"][self.key("E")]["trace_scope_depth"], 2)
        self.assertNotIn(self.ids["G"], state["transactions"])

    def test_group_collection_ignores_attribution_cap_but_preserves_stop(self):
        labels = copy.deepcopy(self.labels)
        labels[0]["hop_limit"] = 2
        state, _ = self.run_trace(hops=9, labels=labels)
        self.assertEqual(set(state["transactions"]), set(self.ids.values()))
        projected = project_full_scope(state)
        self.assertEqual(set(projected["transactions"]), {self.ids[n] for n in "ABCD"})
        self.assertEqual(projected["outputs"][self.key("C")]["trace_control"]["reason"], "attribution_hop_limit")
        self.assertNotIn('trace_control', state["outputs"][self.key("C")])
        labels[0]["stop"] = True
        state, transport = self.run_trace(hops=9, labels=labels)
        self.assertEqual(set(state["transactions"]), {self.ids["A"]})
        self.assertNotIn("/tx/" + self.ids["A"] + "/outspends", transport.calls)

    def test_legacy_to_named_and_back_resume_preserves_evidence_distances(self):
        original, _ = self.run_trace(hops=1, name="")
        saved = copy.deepcopy(original)
        named, _ = self.run_trace(hops=1, parent=original)
        self.assertEqual(original, saved)
        self.assertIn(self.ids["F"], named["transactions"])
        self.assertNotIn(self.ids["G"], named["transactions"])
        ordinary, transport = self.run_trace(hops=1, name="", parent=named)
        self.assertEqual(transport.calls, [])
        self.assertTrue(all("reference_hops" not in row for row in ordinary["transactions"].values()))
        expanded, _ = self.run_trace(hops=4, name="", parent=ordinary)
        self.assertIn(self.ids["G"], expanded["transactions"])
        self.assertNotIn(self.ids["H"], expanded["transactions"])
        self.assertEqual(expanded["transactions"][self.ids["G"]]["depth"], 4)

    def test_new_attribution_releases_held_frontier_and_partial_snapshot_outputs(self):
        state, _ = self.run_trace(hops=1)
        self.assertNotIn(self.ids["G"], state["transactions"])
        # E was inspected but outside the limit. Attribution can make it reset.
        labels = self.labels + [{"kind": "address", "value": "SYNTHETIC-outside-y", "entity": "Perp", "stop": False}]
        original = copy.deepcopy(state)
        del state["outputs"][self.key("E")]
        resumed, _ = self.run_trace(hops=1, parent=state, labels=labels)
        self.assertIn(self.ids["G"], resumed["transactions"])
        self.assertEqual(resumed["outputs"][self.key("E")]["trace_scope_depth"], 0)
        self.assertEqual(resumed["transactions"][self.ids["E"]]["depth"], original["transactions"][self.ids["E"]]["depth"])

    def test_exhausted_short_route_cannot_lend_depth_to_long_open_route(self):
        # A -> B -> C exhausts its attribution budget at C. The independent
        # U -> V -> W -> C route reaches C at relative 3. C's group output
        # resets, but its outside output cannot borrow the exhausted depth 1.
        for name in "UVW":
            self.ids[name] = hashlib.sha256(("independent-" + name).encode()).hexdigest()
        for name, parent, child in (("U", None, "V"), ("V", "U", "W"), ("W", "V", "C")):
            self.data["/tx/" + self.ids[name]] = {"txid": self.ids[name], "status": dict(CONFIRMED),
                "vin": [] if parent is None else [{"txid": self.ids[parent], "vout": 0,
                    "prevout": output("SYNTHETIC-outside-" + parent)}],
                "vout": [output("SYNTHETIC-outside-" + name)]}
            self.data["/tx/" + self.ids[name] + "/outspends"] = [{"spent": True,
                "txid": self.ids[child], "vin": 1 if child == "C" else 0, "status": dict(CONFIRMED)}]
        self.data["/tx/" + self.ids["C"]]["vin"].append({"txid": self.ids["W"], "vout": 0,
            "prevout": output("SYNTHETIC-outside-W")})
        labels = copy.deepcopy(self.labels)
        labels[0]["hop_limit"] = 2
        for workers in (1, 8):
            state, _ = self.run_trace(hops=2, labels=labels, workers=workers,
                                      seeds=[self.key("A"), self.key("U")])
            self.assertIn(self.ids["F"], state["transactions"])
            self.assertIn(self.ids["E"], state["transactions"])
            projected = project_full_scope(state)
            self.assertNotIn(self.ids["E"], projected["transactions"])

    def test_boundary_parent_cannot_launder_longer_path_budget_through_group_return(self):
        # Put V first in the hop-1 queue, so its over-limit arrival at C's
        # external output precedes processing that output's allowed short path.
        self.ids.update(U="0" * 63 + "2", V="0" * 63 + "1")
        for name, parent, child in (("U", None, "V"), ("V", "U", "C")):
            self.data["/tx/" + self.ids[name]] = {"txid": self.ids[name], "status": dict(CONFIRMED),
                "vin": [] if parent is None else [{"txid": self.ids[parent], "vout": 0,
                    "prevout": output("SYNTHETIC-outside-" + parent)}],
                "vout": [output("SYNTHETIC-outside-" + name)]}
            self.data["/tx/" + self.ids[name] + "/outspends"] = [{"spent": True,
                "txid": self.ids[child], "vin": 1 if child == "C" else 0, "status": dict(CONFIRMED)}]
        self.data["/tx/" + self.ids["C"]]["vin"].append({"txid": self.ids["V"], "vout": 0,
            "prevout": output("SYNTHETIC-outside-V")})
        self.data["/tx/" + self.ids["E"]]["vout"] = [output("SYNTHETIC-group-a")]
        self.data["/tx/" + self.ids["G"]]["vin"][0]["prevout"] = output("SYNTHETIC-group-a")
        labels = copy.deepcopy(self.labels)
        labels[0]["hop_limit"] = 3
        for workers in (1, 8):
            state, _ = self.run_trace(hops=1, labels=labels, workers=workers,
                                      seeds=[self.key("A"), self.key("U")])
            self.assertIn(self.ids["E"], state["transactions"])
            self.assertIn(self.ids["G"], state["transactions"])
            self.assertEqual(state["outputs"][self.key("E")]["trace_scope_depth"], 0)
            self.assertNotIn(self.ids["G"], project_full_scope(state)["transactions"])
            resumed, _ = self.run_trace(hops=1, parent=state, labels=labels, workers=workers)
            self.assertIn(self.ids["G"], resumed["transactions"])
            self.assertNotIn(self.ids["G"], project_full_scope(resumed)["transactions"])

    def test_legacy_named_cap_releases_without_resetting_global_group_distance(self):
        labels = copy.deepcopy(self.labels)
        labels[0]["hop_limit"] = 0
        parent, _ = self.run_trace(hops=0, labels=labels)
        # Model a prior cap-zero run, before any first spender was fetched.
        parent["transactions"] = {self.ids["A"]: parent["transactions"][self.ids["A"]]}
        parent["outputs"] = {self.key("A"): parent["outputs"][self.key("A")]}
        parent["links"] = {}
        item = parent["outputs"][self.key("A")]
        item.update(status="attribution_hop_limit",
                    trace_control={"reason": "attribution_hop_limit", "previous_status": "pending"})
        parent.pop("collection_policy")
        original = copy.deepcopy(parent)
        for workers in (1, 8):
            resumed, calls = self.run_trace(hops=1, parent=parent, labels=labels, workers=workers)
            self.assertEqual(parent, original)
            self.assertIn(self.ids["F"], resumed["transactions"])
            self.assertIn(self.ids["E"], resumed["transactions"])
            self.assertNotIn(self.ids["G"], resumed["transactions"])
            self.assertNotIn("/tx/" + self.ids["E"] + "/outspends", calls.calls)
            self.assertEqual(resumed["outputs"][self.key("E")]["trace_scope_depth"], 2)

    def test_boundary_inspection_keeps_terminal_observation_status(self):
        self.data["/tx/" + self.ids["C"]]["vout"].append({"scriptpubkey": "", "scriptpubkey_type": "fee"})
        self.data["/tx/" + self.ids["C"] + "/outspends"].append({"spent": False})
        state, _ = self.run_trace(hops=0)
        fee = state["outputs"][self.key("C", 2)]
        self.assertEqual(fee["status"], "fee")
        self.assertEqual(fee["trace_control"]["reason"], "named_group_hop_limit")

    def test_name_matching_is_exact_casefolded_enabled_address_attribution(self):
        labels = [{"kind": "address", "value": "a", "entity": " PeRp "},
                  {"kind": "address", "value": "b", "entity": "Perp", "enabled": False},
                  {"kind": "script", "value": "c", "entity": "Perp"},
                  {"kind": "address", "value": "d", "entity": "Perp service"}]
        self.assertEqual(reference_addresses({"hop_reference_name": " perp ", "labels": labels}), {"a"})
        self.assertEqual(normalize_reference_name(" Perp "), "Perp")


if __name__ == "__main__":
    unittest.main()
