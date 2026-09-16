"""Input merges and retroactive shared-address receipts remain separate signals."""
import copy
import json
import random
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import TraceError, read_json, canonical
from liquid_tracer.convergence import branch_interactions
from liquid_tracer.export import build_graph, node_csv_rows, svg_graph, COLORS
from liquid_tracer.graph_markers import node_border
from liquid_tracer.elk_layout import optimize_graph
from liquid_tracer.compaction import compact_graph
from liquid_tracer.layout_preview import render_svg
from liquid_tracer.mermaid import mermaid_source
from liquid_tracer.miro import make_plan, sync, validate_plan
from tests.test_attribution_convergence import graph_state, annotation, tx
from tests.test_presentation_annotations import AnnotationMiro

SHARED = "SYNTHETIC-shared-receiving-address"
ADDRESS_KEY = "liquid:address:" + SHARED
RED = ("#ff0000", 12)
PLAIN = ("#334155", 2)


def receiving(state, text, address=SHARED):
    name, vout = text.split(":")
    output = state["transactions"][tx(name)]["data"]["vout"][int(vout)]
    output["scriptpubkey_address"] = address
    # Keep synthetic prevout copies consistent with the exact funding output.
    for record in state["transactions"].values():
        for vin in record["data"]["vin"]:
            if (not vin.get("is_pegin") and not vin.get("is_coinbase")
                    and (vin.get("txid"), vin.get("vout")) == (tx(name), int(vout))):
                vin["prevout"] = copy.deepcopy(output)


def shared_state():
    state = graph_state()
    receiving(state, "a:0")
    receiving(state, "b:0")
    return state


def nodes(graph):
    return {node["id"]: node for node in graph["nodes"]}


class SharedReceiptTests(unittest.TestCase):
    def test_two_starts_one_circle_and_both_senders_without_a_joint_spend(self):
        state = shared_state()
        before = copy.deepcopy(state)
        with patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("Offline only")):
            graph = build_graph(state)
        found = nodes(graph)
        self.assertEqual(set(found), {"tx:" + tx("a"), "tx:" + tx("b"), ADDRESS_KEY})
        self.assertFalse(any(node.get("convergence") for node in found.values()))
        self.assertEqual(found[ADDRESS_KEY]["address_convergence"]["starting_transaction_indices"], [1, 2])
        self.assertEqual(found[ADDRESS_KEY]["address_convergence"]["receipt_count"], 2)
        for name, index in (("a", 1), ("b", 2)):
            node = found["tx:" + tx(name)]
            self.assertEqual(node["interaction_types"], ["shared_address_sender"])
            self.assertEqual(node["address_interactions"][0]["sent_starting_transaction_indices"], [index])
            self.assertEqual(node["address_interactions"][0]["outpoints"], [tx(name) + ":0"])
            self.assertNotIn("SHARED ADDRESS", node["label"])
            self.assertNotIn("INPUT MERGE", node["label"])
            self.assertEqual(node_border(node), RED)
        self.assertEqual(node_border(found[ADDRESS_KEY]), RED)
        self.assertNotIn("SHARED ADDRESS", found[ADDRESS_KEY]["label"])
        self.assertEqual(found[ADDRESS_KEY]["interaction_types"], ["shared_address_receipts"])
        self.assertEqual(found[ADDRESS_KEY]["color"], COLORS["seed"])
        self.assertEqual(state, before)
        self.assertEqual(len(graph["edges"]), 2)

    def test_second_arrival_retroactively_updates_earlier_sender_and_existing_circle(self):
        first = graph_state()
        receiving(first, "a:0")
        old = build_graph(first)
        before = copy.deepcopy(old)
        self.assertEqual(node_border(nodes(old)[ADDRESS_KEY]), PLAIN)
        second = graph_state((("b:0", "c"),))
        receiving(second, "a:0")
        receiving(second, "c:0")
        graph = build_graph(second)
        found = nodes(graph)
        self.assertEqual(node_border(found["tx:" + tx("a")]), RED)
        self.assertEqual(node_border(found["tx:" + tx("c")]), RED)
        self.assertEqual(node_border(found["tx:" + tx("b")]), PLAIN)
        self.assertEqual(node_border(found[ADDRESS_KEY]), RED)
        self.assertEqual(before, old)

    def test_shuffled_input_order_has_identical_results(self):
        state = graph_state((("a:0", "c"), ("b:0", "d"), ("c:0", "e")))
        receiving(state, "c:0")
        receiving(state, "d:0")
        expected = build_graph(state)
        randomizer = random.Random(41)
        for _ in range(8):
            changed = copy.deepcopy(state)
            for field in ("transactions", "links", "outputs"):
                entries = list(changed[field].items())
                randomizer.shuffle(entries)
                changed[field] = dict(entries)
            randomizer.shuffle(changed["seeds"])
            actual = build_graph(changed)
            # Preserve the literal input-list order in run metadata, while the
            # layout, catalog and both interaction signals stay deterministic.
            actual["run"]["seeds"] = expected["run"]["seeds"]
            self.assertEqual(actual, expected)

    def test_timestamps_do_not_exclude_an_earlier_receipt(self):
        state = shared_state()
        state["transactions"][tx("a")]["data"]["status"]["block_time"] += 10000
        graph = build_graph(state)
        receipts = graph["address_convergences"][ADDRESS_KEY]["receipts"]
        by_sender = {row["transaction_key"]: row["starting_transaction_indices"] for row in receipts}
        self.assertEqual(by_sender, {"tx:" + tx("a"): [2], "tx:" + tx("b"): [1]})
        self.assertTrue(all(node_border(n) == RED for n in graph["nodes"]))

    def test_unconfirmed_start_is_included_without_inventing_a_timestamp(self):
        state = shared_state()
        state["transactions"][tx("a")]["data"]["status"] = {"confirmed": False}
        graph = build_graph(state)
        self.assertEqual(len(graph["address_convergences"][ADDRESS_KEY]["receipts"]), 2)
        self.assertIsNone(graph["activity_frames"]["starting_transactions"][-1]["block_time"])

    def test_legacy_output_occurrences_are_all_marked(self):
        graph = build_graph(shared_state(), merge_addresses=False)
        addresses = [n for n in graph["nodes"] if n["kind"] == "address"]
        self.assertEqual(len(addresses), 2)
        self.assertEqual(len(graph["address_convergences"]), 1)
        self.assertTrue(all(node_border(n) == RED for n in addresses))
        self.assertTrue(all("SHARED ADDRESS" not in n["label"] for n in addresses))
        self.assertEqual(addresses[0]["address_convergence"], addresses[1]["address_convergence"])

    def test_two_outputs_of_one_start_are_not_two_branches(self):
        state = graph_state(seeds=("a:0", "a:1", "b:0"))
        receiving(state, "a:0")
        receiving(state, "a:1")
        graph = build_graph(state)
        self.assertEqual(graph["address_convergences"], {})
        self.assertFalse(any(n.get("address_interactions") for n in graph["nodes"]))

    def test_repeated_receipts_from_the_same_single_branch_do_not_qualify(self):
        state = graph_state((("a:0", "c"), ("c:0", "d")))
        for text in ("a:0", "c:0", "d:0"):
            receiving(state, text)
        self.assertEqual(build_graph(state)["address_convergences"], {})

    def test_single_receipt_from_previously_merged_lineage_is_not_shared(self):
        state = graph_state((("a:0", "c"), ("b:0", "c")))
        receiving(state, "c:0")
        graph = build_graph(state)
        self.assertEqual(graph["address_convergences"], {})
        self.assertTrue(nodes(graph)["tx:" + tx("c")].get("convergence"))

    def test_identical_merged_origin_sets_do_not_create_another_interaction(self):
        state = graph_state((("a:0", "c"), ("b:0", "c"), ("c:0", "d")))
        receiving(state, "c:0")
        receiving(state, "d:0")
        graph = build_graph(state)
        self.assertEqual(graph["address_convergences"], {})
        self.assertEqual(node_border(nodes(graph)["tx:" + tx("d")]), PLAIN)

    def test_different_overlapping_sets_qualify_and_transaction_can_have_both_signals(self):
        state = graph_state((("a:0", "c"), ("b:0", "c")), seeds=("a:0", "a:1", "b:0"))
        receiving(state, "a:1")
        receiving(state, "c:0")
        graph = build_graph(state)
        combined = nodes(graph)["tx:" + tx("c")]
        self.assertEqual(combined["interaction_types"], ["input_merge", "shared_address_sender"])
        self.assertIn("INPUT MERGE", combined["label"])
        self.assertNotIn("SHARED ADDRESS", combined["label"])
        self.assertEqual(node_border(combined), RED)
        self.assertEqual(nodes(graph)["tx:" + tx("a")]["address_interactions"][0]["outpoints"], [tx("a") + ":1"])

    def test_unselected_start_sibling_has_no_own_origin(self):
        state = graph_state((("a:1", "c"),))
        receiving(state, "a:1")
        receiving(state, "b:0")
        self.assertEqual(build_graph(state)["address_convergences"], {})

    def test_context_only_inputs_do_not_assign_receipt_origins(self):
        state = graph_state(raw_links=(("a:0", "c"), ("b:0", "d")))
        receiving(state, "c:0")
        receiving(state, "d:0")
        self.assertEqual(build_graph(state)["address_convergences"], {})

    def test_shared_address_does_not_cross_contaminate_later_utxo_spends(self):
        state = graph_state((("a:0", "c"), ("b:0", "d")))
        receiving(state, "a:0")
        receiving(state, "b:0")
        before = copy.deepcopy(state)
        graph = build_graph(state)
        self.assertIn(ADDRESS_KEY, graph["address_convergences"])
        for name in ("c", "d"):
            self.assertEqual(node_border(nodes(graph)["tx:" + tx(name)]), PLAIN)
            self.assertFalse(nodes(graph)["tx:" + tx(name)].get("convergence"))
        self.assertEqual(state, before)
        self.assertEqual(len(graph["edges"]), 6)

    def test_arrivals_at_active_stop_still_intersect_but_do_not_propagate(self):
        state = graph_state((("a:0", "c"), ("b:0", "c"), ("c:0", "d")),
                            labels=(annotation(address=SHARED, stop=True),))
        receiving(state, "a:0")
        receiving(state, "b:0")
        graph = build_graph(state)
        self.assertIn(ADDRESS_KEY, graph["address_convergences"])
        self.assertFalse(any(n.get("convergence") for n in graph["nodes"]))
        self.assertEqual(node_border(nodes(graph)["tx:" + tx("c")]), PLAIN)
        self.assertIn("STOP TRACING", nodes(graph)[ADDRESS_KEY]["label"])

    def test_upstream_stop_hides_blocked_descendants_but_independent_seed_restores_them(self):
        state = graph_state((("a:0", "c"),), labels=(annotation(stop=True),))
        receiving(state, "c:0")
        receiving(state, "b:0")
        self.assertEqual(build_graph(state)["address_convergences"], {})
        state["seeds"].append(tx("c") + ":0")
        graph = build_graph(state)
        self.assertIn(ADDRESS_KEY, graph["address_convergences"])
        self.assertEqual(node_border(nodes(graph)["tx:" + tx("a")]), PLAIN)

    def test_full_address_not_casefolded_names_or_short_display_labels(self):
        for a, b in (("SYNTHETIC-Base58AbCd", "SYNTHETIC-Base58abcd"),
                     ("1234567890" + "a" * 30 + "1234567", "1234567890" + "b" * 30 + "1234567")):
            state = graph_state(labels=(annotation(address=a, name="BTSE"), annotation(address=b, name="btse")))
            receiving(state, "a:0", a)
            receiving(state, "b:0", b)
            self.assertEqual(build_graph(state)["address_convergences"], {})

    def test_pegout_fee_and_unspendable_are_not_receiving_address_circles(self):
        for extra in ({"scriptpubkey": "", "scriptpubkey_type": "fee"},
                      {"scriptpubkey": "6a", "scriptpubkey_type": "op_return"},
                      {"pegout": {"scriptpubkey_address": "bitcoin-destination"}}):
            state = shared_state()
            for record in state["transactions"].values():
                record["data"]["vout"][0].update(extra)
            self.assertEqual(build_graph(state, include_fees=True)["address_convergences"], {})

    def test_bitcoin_context_with_same_text_never_inherits_liquid_marker(self):
        state = shared_state()
        state["transactions"][tx("a")]["data"]["vin"].append({
            "txid": tx("z"), "vout": 0, "is_pegin": True,
            "prevout": copy.deepcopy(state["transactions"][tx("a")]["data"]["vout"][0])})
        graph = build_graph(state)
        self.assertEqual(node_border(nodes(graph)["bitcoin:address:" + SHARED]), PLAIN)
        self.assertEqual(node_border(nodes(graph)[ADDRESS_KEY]), RED)

    def test_stale_or_malformed_tracked_output_is_rejected(self):
        state = shared_state()
        state["outputs"][tx("a") + ":0"]["vout"] = 5
        with self.assertRaisesRegex(TraceError, "exact saved output"):
            build_graph(state)

    def test_many_receipts_stored_once_not_copied_to_each_sender(self):
        count = 250
        state = graph_state()
        template = copy.deepcopy(state["transactions"][tx("a")])
        for index in range(1, count + 1):
            txid = f"{index:064x}"
            record = copy.deepcopy(template)
            record["data"]["txid"] = txid
            record["data"]["vout"][0]["scriptpubkey_address"] = SHARED
            state["transactions"][txid] = record
            key = txid + ":0"
            state["seeds"].append(key)
            state["outputs"][key] = {"txid": txid, "vout": 0, "outpoint": key, "status": "hop_limit", "depth": 0}
        graph = build_graph(state)
        record = graph["address_convergences"][ADDRESS_KEY]
        self.assertEqual(len(record["receipts"]), count)
        sender_rows = [r for n in graph["nodes"] for r in n.get("address_interactions", [])]
        self.assertEqual(sum(len(r["outpoints"]) for r in sender_rows), count)
        self.assertFalse(any("receipts" in r for r in sender_rows))
        self.assertNotIn("receipts", nodes(graph)[ADDRESS_KEY]["address_convergence"])


class SharedReceiptPresentationTests(unittest.TestCase):
    def test_miro_svg_mermaid_csv_preserve_separate_signals_and_fill_priority(self):
        state = shared_state()
        state["labels"] = [annotation(address=SHARED, stop=True, name="Example Exchange")]
        graph = build_graph(state)
        plan = make_plan(graph)
        validate_plan(plan)
        self.assertEqual(plan["address_convergences"], graph["address_convergences"])
        self.assertEqual(plan["presentation_items"], {})
        self.assertEqual(len(plan["shapes"]), len(graph["nodes"]) + 2 + len(plan["presentation_items"]))
        for node in graph["nodes"]:
            shape = next(s for s in plan["shapes"] if s["key"] == node["id"])
            self.assertEqual((shape["body"]["style"]["borderColor"], shape["body"]["style"]["borderWidth"]), ("#ff0000", "12"))
            self.assertNotIn("SHARED ADDRESS", shape["body"]["data"]["content"])
            if node["kind"] == "address":
                self.assertEqual(shape["body"]["style"]["fillColor"], COLORS["seed"])
                self.assertIn("Suspected Example Exchange", shape["body"]["data"]["content"])
            for content, attr in ((svg_graph(graph), "data-key"), (render_svg(graph), "data-node-id")):
                self.assertNotIn("SHARED ADDRESS", content.decode("utf-8") if isinstance(content, bytes) else content)
                element = next(e for e in ET.fromstring(content).iter() if e.get(attr) == node["id"])
                self.assertTrue(any(e.get("stroke") == "#ff0000" and e.get("stroke-width") == "12" for e in element.iter()))
        source = mermaid_source(graph)
        self.assertEqual(source.count("stroke:#ff0000,stroke-width:12px"), len(graph["nodes"]))
        self.assertNotIn("★", source)
        self.assertNotIn("SHARED ADDRESS", source)
        rows = list(node_csv_rows(graph))
        self.assertTrue(any(r.get("address_convergence") for r in rows))
        self.assertEqual(sum(bool(r.get("address_interactions")) for r in rows), 2)
        self.assertFalse(any(r.get("convergence") for r in rows))

    def test_real_elk_and_compaction_preserve_signals_without_new_edges(self):
        graph = build_graph(shared_state())
        expected = copy.deepcopy(graph)
        result = compact_graph(optimize_graph(graph))
        self.assertEqual(result["address_convergences"], graph["address_convergences"])
        self.assertEqual({e["id"] for e in graph["edges"]}, {e["id"] for e in result["edges"]})
        self.assertTrue(all(node_border(n) == RED for n in result["nodes"]))
        validate_plan(make_plan(result))
        self.assertEqual(graph, expected)

    def test_normal_sync_updates_existing_earlier_sender_preserves_ids_and_positions(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "miro.json"
            remote = AnnotationMiro()
            def publish(graph):
                plan = make_plan(graph); validate_plan(plan)
                return sync(plan, "synthetic-board", path, token="test", transport=remote, interval=0)
            state = graph_state(raw_links=(("b:0", "c"),))
            receiving(state, "a:0")
            receiving(state, "c:0")
            publish(build_graph(state))
            first = read_json(path)["items"]
            key = "tx:" + tx("a")
            remote.items[first[key]["id"]]["position"].update(x=-777, y=555)
            later = graph_state((("b:0", "c"),))
            receiving(later, "a:0")
            receiving(later, "c:0")
            later.update(run_id="later", parent_run=state["run_id"], ancestor_runs=[state["run_id"]])
            publish(build_graph(later))
            after = read_json(path)["items"]
            for host in (key, ADDRESS_KEY):
                self.assertEqual(first[host]["id"], after[host]["id"])
                actual = remote.items[after[host]["id"]]
                self.assertEqual(actual["style"]["borderColor"], "#ff0000")
                self.assertEqual(actual["style"]["borderWidth"], "12")
                self.assertNotIn("SHARED ADDRESS", actual["data"]["content"])
            self.assertEqual(remote.items[after[key]["id"]]["position"], {"x": -777, "y": 555, "origin": "center"})
            writes = len(remote.writes)
            publish(build_graph(later))
            self.assertEqual(writes, len(remote.writes))

    def test_manual_border_is_preserved_as_a_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "miro.json"
            remote = AnnotationMiro()
            graph = build_graph(shared_state())
            old = copy.deepcopy(graph)
            for node in old["nodes"]:
                node.pop("address_convergence", None)
                node.pop("address_interactions", None)
            def publish(g):
                return sync(make_plan(g), "synthetic-board", path, token="test", transport=remote, interval=0)
            publish(old)
            mapping = read_json(path)["items"]
            actual = remote.items[mapping[ADDRESS_KEY]["id"]]
            actual["style"]["borderColor"] = "#123456"
            publish(graph)
            self.assertEqual(actual["style"]["borderColor"], "#123456")


if __name__ == "__main__":
    unittest.main()
