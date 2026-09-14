"""Explicit assessments and transaction convergence without address-based inference."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.address_import import apply_import, parse_import, preview_import
from liquid_tracer.attribution_presentation import register_html
from liquid_tracer.common import TraceError, save_json
from liquid_tracer.export import build_graph, node_csv_rows
from liquid_tracer.investigations import create_investigation
from liquid_tracer.miro import make_plan, validate_plan
from liquid_tracer.services import confidence_value, load_services, service_labels, set_service
from tests.fixtures import output
from tests.test_layout import state_from


def tx(name):
    return (name.lower() * 64)[:64]


def graph_state(links=(), seeds=("a:0", "b:0"), *, raw_links=(), labels=()):
    """Small deterministic saved-evidence graph. Raw-only links are context."""
    transactions = {}
    def outpoint(text):
        name, index = text.split(":")
        return tx(name) + ":" + index
    names = {seed.split(":")[0] for seed in seeds}
    for key, child in (*links, *raw_links):
        names.update((key.split(":")[0], child))
    for number, name in enumerate(sorted(names)):
        transactions[tx(name)] = {"txid": tx(name), "vin": [], "vout": [output("SYNTHETIC-" + name + "-address")],
                                  "status": {"confirmed": True, "block_time": 1700000000 + number}}
    for key in [*seeds, *(key for key, _ in (*links, *raw_links))]:
        name, index = key.split(":")
        while len(transactions[tx(name)]["vout"]) <= int(index):
            transactions[tx(name)]["vout"].append(output("SYNTHETIC-" + name + "-address"))
    records = {}
    for key, child in (*links, *raw_links):
        name, index = key.split(":")
        values = transactions[tx(child)]["vin"]
        if (key, child) in links:
            records[outpoint(key)] = {"spending_txid": tx(child), "vin": len(values)}
        values.append({"txid": tx(name), "vout": int(index), "prevout": transactions[tx(name)]["vout"][int(index)]})
    state = state_from(transactions)
    state["ancestor_runs"] = []
    state["seeds"] = [outpoint(key) for key in seeds]
    state["links"] = records
    state["labels"] = list(labels)
    for txid, record in state["transactions"].items():
        for index in range(len(record["data"]["vout"])):
            key = txid + ":" + str(index)
            state["outputs"][key] = {"outpoint": key, "txid": txid, "vout": index,
                                      "depth": 0, "status": "spent" if key in records else "hop_limit"}
    return state


def annotation(confidence="suspected", stop=True, name="Example Exchange", address="SYNTHETIC-a-address"):
    return {"kind": "address", "value": address, "entity": name, "confidence": confidence,
            "stop": stop, "source": "Supplied records <not HTML>", "notes": "Review A\nReview B", "observed_at": "2023-10-04"}


class ExplicitAttributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.case = create_investigation(Path(self.tmp.name), "Explicit fields")

    def test_six_column_csv_all_four_confidence_stop_combinations(self):
        for confidence in ("suspected", "confirmed"):
            for stop in (True, False):
                text = 'Address,Name,confidence,stop_tracing,source,notes\nSYNTHETIC-a-address,Example Exchange,' + confidence + ',' + str(stop).lower() + ',Records,"First line\nSecond line"\n'
                review = preview_import(self.case, text, policy="replace")
                self.assertTrue(review["valid"])
                apply_import(self.case, text, policy="replace", approval_sha256=review["approval_sha256"])
                stored = load_services(self.case)["rules"]["SYNTHETIC-a-address"]
                self.assertNotIn("classification", stored)
                self.assertNotIn("rationale", stored)
                self.assertEqual(stored["notes"], "First line\nSecond line")
                state = graph_state(labels=service_labels(load_services(self.case)))
                node = next(n for n in build_graph(state)["nodes"] if n["id"] == "liquid:address:SYNTHETIC-a-address")
                self.assertEqual(node["label"].splitlines()[0], ("Suspected " if confidence == "suspected" else "") + "Example Exchange")
                self.assertEqual("STOP TRACING" in node["label"], stop)
                self.assertNotIn("(confirmed)", node["label"])
                self.assertNotIn("(suspected)", node["label"])

    def test_removed_classification_and_old_confidence_not_accepted_in_new_imports(self):
        for extra in ({"classification": "service"}, {"classification": "label"}, {"confidence": "candidate"},
                      {"confidence": "corroborated"}, {"confidence": "anything"}):
            parsed = parse_import(json.dumps([{"address": "SYNTHETIC-a-address", **extra}]))
            self.assertTrue(parsed["errors"])
        for value in ("suspected", "confirmed"):
            self.assertFalse(parse_import(json.dumps([{"address": "SYNTHETIC-a-address", "confidence": value}]))["errors"])

    def test_confidence_or_stop_edit_does_not_erase_name_or_notes(self):
        address = "SYNTHETIC-a-address"
        set_service(self.case, address, name="Example", notes="Preserve evidence", stop_tracing=False)
        updated = set_service(self.case, address, confidence="confirmed")["rules"][address]
        self.assertEqual((updated["name"], updated["notes"], updated["stop_tracing"]),
                         ("Example", "Preserve evidence", False))
        updated = set_service(self.case, address, notes="")["rules"][address]
        self.assertEqual(updated["notes"], "")
        self.assertEqual(updated["name"], "Example")

    def test_historical_rules_are_read_only_and_never_upgraded(self):
        current = set_service(self.case, "SYNTHETIC-a-address", confidence="confirmed", stop_tracing=False, notes="evidence")
        old = copy.deepcopy(current); old["schema_version"] = 1
        raw = old["rules"]["SYNTHETIC-a-address"]
        raw["classification"] = "label"; raw["confidence"] = "corroborated"
        raw["rationale"] = raw.pop("notes")
        save_json(self.case / "services.json", old)
        before = (self.case / "services.json").read_bytes()
        labels = service_labels(load_services(self.case))
        self.assertEqual(labels[0]["confidence"], "suspected")
        self.assertFalse(labels[0]["stop"])
        self.assertEqual(labels[0]["notes"], "evidence")
        self.assertEqual((self.case / "services.json").read_bytes(), before)
        changed = set_service(self.case, "SYNTHETIC-a-address", name="Revised", notes="New note")
        self.assertEqual(changed["history"][-1]["previous"], raw)
        self.assertFalse(changed["rules"]["SYNTHETIC-a-address"]["stop_tracing"])
        self.assertEqual(confidence_value("candidate"), "suspected")
        self.assertEqual(confidence_value("confirmed"), "confirmed")

    def test_sources_notes_csv_and_local_register_retain_full_safe_text(self):
        label = annotation(name='Example <script>bad</script>', stop=False)
        graph = build_graph(graph_state(labels=[label]))
        before = copy.deepcopy(graph)
        rendered = register_html(graph)
        self.assertIn('Review A\nReview B', rendered)
        self.assertIn('&lt;script&gt;', rendered)
        self.assertNotIn('<script>bad', rendered)
        row = next(row for row in node_csv_rows(graph) if row.get("name"))
        self.assertEqual(row["name"], label["entity"])
        self.assertEqual(row["notes"], label["notes"])
        self.assertEqual(row["source"], label["source"])
        plan = make_plan(graph); validate_plan(plan)
        register = [s["body"]["data"]["content"] for s in plan["shapes"] if s["key"] in plan["presentation_items"]]
        self.assertEqual(register, [])  # Full notes live in HTML/JSON/CSV, not Miro cards.
        self.assertEqual(graph, before)


class ConvergenceTests(unittest.TestCase):
    def test_independent_inputs_merge_but_single_downstream_does_not(self):
        state = graph_state((("a:0", "c"), ("b:0", "c"), ("c:0", "d")))
        original = copy.deepcopy(state); graph = build_graph(state)
        flagged = {n["id"]: n["convergence"] for n in graph["nodes"] if n.get("convergence")}
        self.assertEqual(set(flagged), {"tx:" + tx("c")})
        self.assertEqual(flagged["tx:" + tx("c")]["starting_transaction_indices"], [1, 2])
        self.assertEqual(len(flagged["tx:" + tx("c")]["inputs"]), 2)
        self.assertEqual(state, original)

    def test_starting_transaction_can_meet_an_earlier_start(self):
        graph = build_graph(graph_state((("a:0", "b"), ("b:0", "c"))))
        node = next(n for n in graph["nodes"] if n["id"] == "tx:" + tx("b"))
        self.assertEqual(node["role"], "starting_transaction")
        self.assertEqual(node["convergence"]["starting_transaction_indices"], [1, 2])
        self.assertEqual(node["convergence"]["own_starting_transaction_index"], 2)
        self.assertFalse(any(n.get("convergence") for n in graph["nodes"] if n["id"] != node["id"]))

    def test_two_outputs_of_one_start_do_not_count_as_two_origins(self):
        state = graph_state((("a:0", "c"), ("a:1", "c")), seeds=("a:0", "a:1", "b:0"))
        self.assertFalse(any(n.get("convergence") for n in build_graph(state)["nodes"]))

    def test_repeated_address_does_not_create_lineage(self):
        state = graph_state(raw_links=(("a:0", "c"), ("b:0", "c")))
        for record in state["transactions"].values():
            for out in record["data"]["vout"]:
                out["scriptpubkey_address"] = "SYNTHETIC-same-address"
        graph = build_graph(state)
        self.assertEqual(len(graph["activity_frames"]["activities"]), 1)
        self.assertFalse(any(n.get("convergence") for n in graph["nodes"]))

    def test_split_after_merge_then_identical_lineages_rejoin_has_no_new_star(self):
        state = graph_state((("a:0", "c"), ("b:0", "c"), ("c:0", "d"), ("c:1", "d")))
        self.assertEqual([n["id"] for n in build_graph(state)["nodes"] if n.get("convergence")], ["tx:" + tx("c")])

    def test_different_overlapping_or_new_origin_sets_are_new_interactions(self):
        for links, seeds in (
            ((("a:0", "c"), ("b:0", "c"), ("c:0", "e"), ("b:1", "e")),
             ("a:0", "b:0", "b:1")),
            ((("a:0", "c"), ("b:0", "c"), ("c:0", "e"), ("f:0", "e")),
             ("a:0", "b:0", "f:0")),
        ):
            with self.subTest(seeds=seeds):
                graph = build_graph(graph_state(links, seeds=seeds))
                flagged = {n["id"] for n in graph["nodes"] if n.get("convergence")}
                self.assertEqual(flagged, {"tx:" + tx("c"), "tx:" + tx("e")})

    def test_stop_rules_remove_only_blocked_convergence_not_saved_evidence(self):
        state = graph_state((("a:0", "c"), ("b:0", "c")), labels=[annotation()])
        before = copy.deepcopy(state)
        self.assertFalse(any(n.get("convergence") for n in build_graph(state)["nodes"]))
        self.assertEqual(before, state)
        state["labels"][0]["stop"] = False
        self.assertTrue(any(n.get("convergence") for n in build_graph(state)["nodes"]))

    def test_unselected_root_sibling_never_inherits_own_seed_origin(self):
        state = graph_state((("a:1", "c"), ("b:0", "c")))
        self.assertFalse(any(n.get("convergence") for n in build_graph(state)["nodes"]))

    def test_corrupt_verified_link_fails_closed(self):
        state = graph_state((("a:0", "c"), ("b:0", "c")))
        state["links"][tx("a") + ":0"]["vin"] = 77
        with self.assertRaisesRegex(TraceError, "spend link"):
            build_graph(state)

    def test_input_order_does_not_change_badges_or_indices(self):
        state = graph_state((("a:0", "c"), ("b:0", "c")))
        expected = build_graph(state)
        state["transactions"] = dict(reversed(list(state["transactions"].items())))
        state["links"] = dict(reversed(list(state["links"].items())))
        state["seeds"].reverse()
        self.assertEqual(expected, build_graph(state))
