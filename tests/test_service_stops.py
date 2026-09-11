"""Investigator boundaries gate new requests while retaining prior evidence."""

import copy
import fcntl
import hashlib
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.api import Limits
from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.investigations import create_investigation
from liquid_tracer.services import (apply_service_labels, disable_service, load_services,
                                    service_labels, set_service)
from tests.fixtures import CONFIRMED, output
from tests.test_trace_concurrency import synthetic_trace


def network():
    ids = {name: hashlib.sha256(("SYNTHETIC-services-" + name).encode()).hexdigest() for name in "ABCDEFG"}
    # Two independent seed routes merge at D. An address is reused at F, which
    # tests an address-level cycle without inventing a cyclic UTXO history.
    parents = {"A": [], "B": ["A"], "C": ["E"], "D": ["B", "C"], "E": [], "F": ["D"], "G": ["F"]}
    addresses = {name: "SYNTHETIC-address-" + ("B" if name == "F" else name) for name in ids}
    data = {}
    for name, txid in ids.items():
        data["/tx/" + txid] = {"txid": txid, "status": dict(CONFIRMED),
            "vin": [{"txid": ids[parent], "vout": 0, "prevout": output(addresses[parent])}
                    for parent in parents[name]], "vout": [output(addresses[name])]}
        spending = next((child for child, inputs in parents.items() if name in inputs), None)
        data["/tx/" + txid + "/outspends"] = ([{"spent": True, "txid": ids[spending],
            "vin": parents[spending].index(name), "status": dict(CONFIRMED)}] if spending else [{"spent": False}])
    return ids, addresses, data


def stop(address):
    return {"kind": "address", "value": address, "entity": "Suspected service",
            "source": "Investigator designation", "confidence": "candidate", "observed_at": "2026-01-01",
            "classification": "suspected_service", "managed_by": "case_service_rules", "stop": True}


class ServiceStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.case = create_investigation(self.temp.name, "Synthetic service case")

    def test_absent_settings_are_read_only_and_case_bound(self):
        before = set(self.case.iterdir())
        data = load_services(self.case)
        self.assertEqual(set(self.case.iterdir()), before)
        self.assertEqual(data["rules"], {})
        self.assertEqual(data["revision"], 0)
        self.assertEqual(data["case_id"], read_json(self.case / "case.json")["case_id"])

    def test_designation_update_disable_and_audit_preserve_investigator_decisions(self):
        first = set_service(self.case, "SYNTHETIC-address", name="Possible exchange", rationale="Frequent consolidation")
        second = set_service(self.case, "SYNTHETIC-address", name="Possible exchange", rationale="Review pending")
        final = disable_service(self.case, "SYNTHETIC-address")
        self.assertEqual(final["revision"], 3)
        self.assertEqual(len(final["history"]), 3)
        self.assertEqual(final["history"][1]["previous"], first["rules"]["SYNTHETIC-address"])
        self.assertEqual(final["rules"]["SYNTHETIC-address"]["created_at"], first["rules"]["SYNTHETIC-address"]["created_at"])
        self.assertEqual(final["rules"]["SYNTHETIC-address"]["rationale"], second["rules"]["SYNTHETIC-address"]["rationale"])
        self.assertFalse(final["rules"]["SYNTHETIC-address"]["enabled"])
        self.assertEqual(service_labels(final), [])
        self.assertEqual(load_services(self.case), final)

    def test_overlay_replaces_our_previous_snapshot_without_mutating_or_removing_imported_labels(self):
        settings = set_service(self.case, "SYNTHETIC-address")
        generic = {"kind": "address", "value": "SYNTHETIC-other", "stop": True, "entity": "Imported"}
        inherited = [generic, stop("SYNTHETIC-stale")]
        original = copy.deepcopy(inherited)
        result = apply_service_labels(inherited, settings)
        self.assertEqual(inherited, original)
        self.assertEqual(result[0], generic)
        self.assertIsNot(result[0], generic)
        self.assertEqual(result[1]["value"], "SYNTHETIC-address")
        self.assertEqual(result[1]["confidence"], "candidate")
        self.assertEqual(result[1]["classification"], "suspected_service")
        self.assertNotIn("confirmed", result[1].values())
        disabled = disable_service(self.case, "SYNTHETIC-address")
        self.assertEqual(apply_service_labels(result, disabled), [generic])

    def test_mutation_is_blocked_during_trace_and_does_not_create_settings(self):
        with (self.case / "trace.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(TraceError, "trace is running"):
                set_service(self.case, "SYNTHETIC-address")
        self.assertFalse((self.case / "services.json").exists())

    def test_foreign_case_or_malformed_rule_fails_closed(self):
        data = set_service(self.case, "SYNTHETIC-address")
        data["case_id"] = "0" * 32
        save_json(self.case / "services.json", data)
        with self.assertRaisesRegex(TraceError, "Invalid"):
            load_services(self.case)
        data["case_id"] = read_json(self.case / "case.json")["case_id"]
        data["rules"]["SYNTHETIC-address"]["enabled"] = "false"
        save_json(self.case / "services.json", data)
        with self.assertRaisesRegex(TraceError, "Invalid"):
            load_services(self.case)

    def test_invalid_rule_inputs_do_not_write(self):
        for address in ("", "two addresses", "https://example.test/address", "bad\naddress", "bad?query"):
            with self.subTest(address=address), self.assertRaises(TraceError):
                set_service(self.case, address)
        for options in ({"name": "x" * 121}, {"rationale": "x" * 4001}, {"enabled": 1}):
            with self.subTest(options=options), self.assertRaises(TraceError):
                set_service(self.case, "SYNTHETIC-address", **options)
        self.assertFalse((self.case / "services.json").exists())

    def test_multiline_rationale_is_preserved_and_controls_rejected(self):
        data = set_service(self.case, "SYNTHETIC-address", rationale="First line\r\n\tSecond line")
        self.assertEqual(data["rules"]["SYNTHETIC-address"]["rationale"], "First line\n\tSecond line")
        for options in ({"name": "Two\nlines"}, {"rationale": "text\x1b[0m"}, {"rationale": "text\x00tail"}):
            with self.subTest(options=options), self.assertRaises(TraceError):
                set_service(self.case, "SYNTHETIC-address", **options)
        self.assertEqual(load_services(self.case), data)


class ServiceTraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ids, self.addresses, self.data = network()
        self.counter = 0

    def key(self, name):
        return self.ids[name] + ":0"

    def run_trace(self, seeds="A", *, hops=5, parent=None, only=None, services=(), workers=8, labels=None):
        self.counter += 1
        return synthetic_trace(self.root / str(self.counter), self.data, [self.key(name) for name in seeds],
            workers=workers, limits=Limits(max_hops=hops), parent=parent, only=only,
            labels=labels if labels is not None else [stop(self.addresses[name]) for name in services])

    def test_seed_stop_prevents_outspend_requests_in_serial_and_prefetch_modes(self):
        for workers in (1, 8):
            with self.subTest(workers=workers):
                state, transport = self.run_trace(services="A", workers=workers)
                self.assertEqual(state["status"], "bounded_complete")
                self.assertEqual(transport.calls, ["/tx/" + self.ids["A"]])
                self.assertEqual(state["outputs"][self.key("A")]["status"], "suspected_service_stop")
                self.assertEqual(state["links"], {})
                self.assertEqual(state["stats"]["active_frontier_count"], 0)

    def test_new_encounter_stops_before_spend_prefetch_and_retains_arrival_link(self):
        state, transport = self.run_trace(services="B")
        self.assertEqual(state["status"], "bounded_complete")
        self.assertEqual(set(state["links"]), {self.key("A")})
        self.assertEqual(set(state["transactions"]), {self.ids[name] for name in "AB"})
        self.assertNotIn("/tx/" + self.ids["B"] + "/outspends", transport.calls)
        self.assertEqual(state["outputs"][self.key("B")]["status"], "suspected_service_stop")

    def test_new_rule_holds_already_expanded_branch_without_requests_or_evidence_deletion(self):
        parent, _ = self.run_trace(hops=2)
        original = copy.deepcopy(parent)
        state, transport = self.run_trace(hops=8, parent=parent, services="B")
        self.assertEqual(transport.calls, [])
        self.assertEqual(state["transactions"], parent["transactions"])
        self.assertEqual(state["links"], parent["links"])
        self.assertEqual(state["outputs"][self.key("B")]["status"], "spent")
        self.assertEqual(state["outputs"][self.key("B")]["trace_control"]["reason"], "suspected_service_stop")
        self.assertEqual(state["outputs"][self.key("D")]["status"], "held_behind_service")
        self.assertEqual(parent, original)

    def test_only_cannot_bypass_a_new_boundary_on_an_old_ancestor(self):
        parent, _ = self.run_trace(hops=2)
        state, transport = self.run_trace(hops=8, parent=parent, services="B", only={self.key("D")})
        self.assertEqual(transport.calls, [])
        self.assertEqual(state["outputs"][self.key("D")]["status"], "held_behind_service")

    def test_removing_rule_releases_saved_frontier_without_refetching_confirmed_past_transactions(self):
        parent, _ = self.run_trace(hops=2)
        held, _ = self.run_trace(hops=3, parent=parent, services="B")
        original = copy.deepcopy(held)
        resumed, transport = self.run_trace(hops=3, parent=held)
        self.assertEqual(resumed["status"], "bounded_complete")
        self.assertEqual(set(transport.calls), {"/tx/" + self.ids["D"] + "/outspends", "/tx/" + self.ids["F"]})
        self.assertEqual(resumed["outputs"][self.key("D")]["status"], "spent")
        self.assertEqual(resumed["outputs"][self.key("F")]["status"], "hop_limit")
        self.assertFalse(any(item.get("trace_control") for item in resumed["outputs"].values()))
        self.assertEqual(held, original)

    def test_independent_unblocked_seed_route_preserves_merged_frontier(self):
        parent, _ = self.run_trace(seeds="AE", hops=2)
        state, transport = self.run_trace(seeds="AE", hops=3, parent=parent, services="B")
        self.assertIn("/tx/" + self.ids["D"] + "/outspends", transport.calls)
        self.assertEqual(state["outputs"][self.key("D")]["status"], "spent")
        self.assertNotIn("trace_control", state["outputs"][self.key("D")])
        # The reused address at F is independently stopped at that occurrence.
        self.assertEqual(state["outputs"][self.key("F")]["status"], "suspected_service_stop")
        self.assertNotIn("/tx/" + self.ids["F"] + "/outspends", transport.calls)

    def test_an_independently_provided_downstream_seed_is_not_held_by_an_upstream_stop(self):
        parent, _ = self.run_trace(seeds="AD", hops=0)
        state, transport = self.run_trace(seeds="AD", hops=1, parent=parent, services="A")
        self.assertNotIn("/tx/" + self.ids["A"] + "/outspends", transport.calls)
        self.assertIn("/tx/" + self.ids["D"] + "/outspends", transport.calls)
        self.assertEqual(state["outputs"][self.key("D")]["status"], "spent")

    def test_a_new_alternate_route_releases_old_held_descendants_during_the_same_continuation(self):
        roots, _ = self.run_trace(seeds="AE", hops=0)
        parent, _ = self.run_trace(seeds="AE", hops=2, parent=roots, only={self.key("A")})
        self.assertEqual(parent["outputs"][self.key("D")]["status"], "hop_limit")
        state, transport = self.run_trace(seeds="AE", hops=3, parent=parent,
                                          services="B", only={self.key("E")})
        self.assertEqual(state["status"], "bounded_complete")
        self.assertIn("/tx/" + self.ids["D"] + "/outspends", transport.calls)
        self.assertEqual(state["outputs"][self.key("D")]["status"], "spent")
        self.assertNotIn("trace_control", state["outputs"][self.key("D")])
        self.assertEqual(state["outputs"][self.key("F")]["status"], "suspected_service_stop")

    def test_context_input_from_unblocked_address_does_not_bypass_boundary(self):
        parent, _ = self.run_trace(seeds="A", hops=2)
        # C is a context input to D but was never an independently traced root.
        state, transport = self.run_trace(seeds="A", hops=3, parent=parent, services="B")
        self.assertEqual(transport.calls, [])
        self.assertNotIn(self.key("C"), state["outputs"])
        self.assertEqual(state["outputs"][self.key("D")]["status"], "held_behind_service")

    def test_generic_label_behavior_is_preserved(self):
        label = {"kind": "address", "value": self.addresses["B"], "stop": True}
        state, transport = self.run_trace(labels=[label])
        self.assertEqual(state["outputs"][self.key("B")]["status"], "analyst_stop")
        self.assertNotIn("/tx/" + self.ids["B"] + "/outspends", transport.calls)
        self.assertNotIn("trace_control", state["outputs"][self.key("B")])

    def test_parallel_shared_outspends_response_never_prefetches_stopped_siblings_child(self):
        # One response contains every spend from A, but the service's output is
        # not expanded merely because its selected sibling needs that response.
        a, e = self.ids["A"], self.ids["E"]
        sibling = output("SYNTHETIC-open-sibling")
        self.data["/tx/" + a]["vout"].append(sibling)
        self.data["/tx/" + a + "/outspends"].append(
            {"spent": True, "txid": e, "vin": 0, "status": dict(CONFIRMED)})
        self.data["/tx/" + e]["vin"] = [{"txid": a, "vout": 1, "prevout": sibling}]
        state, transport = synthetic_trace(self.root / "siblings", self.data, [a + ":0", a + ":1"],
            workers=8, limits=Limits(max_hops=1), labels=[stop(self.addresses["A"])])
        self.assertEqual(state["status"], "bounded_complete")
        self.assertNotIn("/tx/" + self.ids["B"], transport.calls)
        self.assertEqual(transport.calls.count("/tx/" + a + "/outspends"), 1)
        self.assertEqual(set(state["links"]), {a + ":1"})
        self.assertEqual(state["outputs"][a + ":0"]["status"], "suspected_service_stop")

    def test_generic_imported_stop_at_old_ancestor_keeps_existing_continuation_behavior(self):
        parent, _ = self.run_trace(hops=2)
        generic = {"kind": "address", "value": self.addresses["B"], "stop": True}
        state, transport = self.run_trace(hops=3, parent=parent, labels=[generic])
        self.assertEqual(state["outputs"][self.key("D")]["status"], "spent")
        self.assertIn("/tx/" + self.ids["D"] + "/outspends", transport.calls)

    def make_unequal_routes(self):
        """A -> B -> D is shorter than E -> C -> H -> D."""
        h = hashlib.sha256(b"SYNTHETIC-services-H").hexdigest()
        c, d, f = (self.ids[name] for name in "CDF")
        self.data["/tx/" + h] = {"txid": h, "status": dict(CONFIRMED),
            "vin": [{"txid": c, "vout": 0, "prevout": output(self.addresses["C"])}],
            "vout": [output("SYNTHETIC-address-H")]}
        self.data["/tx/" + c + "/outspends"] = [{"spent": True, "txid": h, "vin": 0, "status": dict(CONFIRMED)}]
        self.data["/tx/" + h + "/outspends"] = [{"spent": True, "txid": d, "vin": 1, "status": dict(CONFIRMED)}]
        self.data["/tx/" + d]["vin"][1] = {"txid": h, "vout": 0, "prevout": output("SYNTHETIC-address-H")}
        self.data["/tx/" + f]["vout"] = [output("SYNTHETIC-address-F")]

    def test_new_longer_alternate_path_cannot_use_blocked_short_path_depth_to_exceed_hop_limit(self):
        self.make_unequal_routes()
        for workers in (1, 8):
            with self.subTest(workers=workers):
                roots, _ = self.run_trace(seeds="AE", hops=0, workers=workers)
                parent, _ = self.run_trace(seeds="AE", hops=2, parent=roots, only={self.key("A")}, workers=workers)
                original = copy.deepcopy(parent)
                state, transport = self.run_trace(seeds="AE", hops=3, parent=parent,
                    only={self.key("E")}, services="B", workers=workers)
                self.assertEqual(state["status"], "bounded_complete")
                self.assertEqual(state["outputs"][self.key("D")]["depth"], 2)
                self.assertEqual(state["outputs"][self.key("D")]["trace_scope_depth"], 3)
                self.assertEqual(state["outputs"][self.key("D")]["status"], "hop_limit")
                self.assertNotIn("/tx/" + self.ids["D"] + "/outspends", transport.calls)
                self.assertNotIn("/tx/" + self.ids["F"], transport.calls)
                self.assertEqual(parent, original)

    def test_existing_longer_alternate_path_uses_current_allowed_depth_for_frontier(self):
        self.make_unequal_routes()
        for workers in (1, 8):
            with self.subTest(workers=workers):
                parent, _ = self.run_trace(seeds="AE", hops=3, workers=workers)
                state, transport = self.run_trace(seeds="AE", hops=4, parent=parent, services="B", workers=workers)
                self.assertEqual(state["status"], "bounded_complete")
                self.assertEqual(state["outputs"][self.key("F")]["depth"], 3)
                self.assertEqual(state["outputs"][self.key("F")]["trace_scope_depth"], 4)
                self.assertEqual(state["outputs"][self.key("F")]["status"], "hop_limit")
                self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
