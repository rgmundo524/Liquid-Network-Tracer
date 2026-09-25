"""Context summaries preserve every exact transaction CSV input occurrence."""
from copy import deepcopy
import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import LBTC, TraceError, canonical
from liquid_tracer.export import build_graph
from liquid_tracer.transaction_csv import transaction_csv_rows, write_transaction_csv
from tests.test_attribution_convergence import annotation
from tests.test_input_order import child_input, input_order_state
from tests.test_layout import txid


CHILD = txid("input-order-child")


def group(graph):
    return next(node for node in graph["nodes"] if node["kind"] == "context_group")


def grouped_edge(graph, index=0):
    return next(edge for edge in graph["edges"] if edge["id"] == child_input(index))


class GroupedTransactionCSVTests(unittest.TestCase):
    def test_small_and_dense_summaries_export_identical_rows_and_bytes(self):
        for count in (4, 12, 252):
            for merged in (True, False):
                with self.subTest(inputs=count, merged=merged):
                    state = input_order_state(count, continuing=(count - 1,))
                    plain = build_graph(state, merge_addresses=merged)
                    grouped = build_graph(state, merge_addresses=merged, group_context_inputs=True)
                    before = canonical((state, grouped))
                    with patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("offline")):
                        expected = transaction_csv_rows(plain, state)
                        actual = transaction_csv_rows(grouped, state)
                        self.assertEqual(actual, expected)
                        self.assertEqual(len(actual), len(grouped["edges"]))
                        self.assertEqual(sum(row["Direction"] == "IN" for row in actual), count)
                        with tempfile.TemporaryDirectory() as temporary:
                            first, second = Path(temporary) / "plain.csv", Path(temporary) / "grouped.csv"
                            write_transaction_csv(first, plain, state)
                            write_transaction_csv(second, grouped, state)
                            self.assertEqual(first.read_bytes(), second.read_bytes())
                    self.assertEqual(canonical((state, grouped)), before)

    def test_repeated_address_inputs_keep_distinct_vins_amounts_assets_and_unknowns(self):
        state = input_order_state(5, continuing=(4,))
        inputs = state["transactions"][CHILD]["data"]["vin"]
        inputs[0]["vout"], inputs[1]["vout"] = 7, 9
        inputs[1]["prevout"]["scriptpubkey_address"] = inputs[0]["prevout"]["scriptpubkey_address"]
        inputs[0]["prevout"].update(value=2 ** 63 - 1, asset=LBTC)
        inputs[0]["prevout"].pop("valuecommitment")
        inputs[0]["prevout"].pop("assetcommitment")
        for merged in (True, False):
            with self.subTest(merged=merged):
                plain = build_graph(state, merge_addresses=merged)
                grouped = build_graph(state, merge_addresses=merged, group_context_inputs=True)
                self.assertEqual(group(grouped)["details"]["address_count"], 3)
                self.assertEqual(group(grouped)["details"]["input_count"], 4)
                rows = transaction_csv_rows(grouped, state)
                self.assertEqual(rows, transaction_csv_rows(plain, state))
                first, second = [row for row in rows if row["Direction"] == "IN"][:2]
                self.assertEqual((first["Number of I/O"], second["Number of I/O"]), (0, 1))
                self.assertEqual(first["Address Hash"], second["Address Hash"])
                self.assertEqual((first["Asset Value"], first["Asset"]), (2 ** 63 - 1, "L-BTC"))
                self.assertEqual((second["Asset Value"], second["Asset"]), ("", ""))
                self.assertIn("CONFIDENTIAL VALUE", second["Address Flags"])

    def test_labels_come_from_exact_member_occurrence_not_state_or_group_union(self):
        state = input_order_state(5, continuing=(4,))
        inputs = state["transactions"][CHILD]["data"]["vin"]
        inputs[1]["prevout"]["scriptpubkey_address"] = inputs[0]["prevout"]["scriptpubkey_address"]
        graph = build_graph(state, group_context_inputs=True)
        first = grouped_edge(graph)
        member = next(node for node in group(graph)["details"]["members"] if node["id"] == first["original_source"])
        label = dict(annotation(name="Current exact input", stop=True), kind="outpoint", value=first["outpoint"])
        occurrence = next(item for item in member["details"]["occurrences"] if item["outpoint"] == first["outpoint"])
        occurrence["labels"] = [label]
        state["labels"] = [{**label, "entity": "Older archived name"}]
        rows = [row for row in transaction_csv_rows(graph, state) if row["Direction"] == "IN"]
        self.assertEqual(rows[0]["Address Label"], "Suspected Current exact input")
        self.assertIn("STOP TRACING", rows[0]["Address Flags"])
        self.assertTrue(all(row["Address Label"] == "" for row in rows[1:]))
        self.assertTrue(all("STOP TRACING" not in row["Address Flags"] for row in rows[1:]))
        occurrence["labels"] = [{**label, "value": "foreign:0"}]
        with self.assertRaisesRegex(TraceError, "attribution"):
            transaction_csv_rows(graph, state)

    def test_missing_foreign_or_wrong_member_original_source_is_rejected(self):
        state = input_order_state(4, continuing=(3,))
        source = build_graph(state, group_context_inputs=True)
        for replacement in (None, "liquid:address:foreign", group(source)["details"]["members"][1]["id"]):
            with self.subTest(original_source=replacement):
                graph = deepcopy(source)
                edge = grouped_edge(graph)
                if replacement is None:
                    edge.pop("original_source")
                else:
                    edge["original_source"] = replacement
                with self.assertRaises(TraceError):
                    transaction_csv_rows(graph, state)

    def test_original_source_swap_between_same_address_occurrences_is_rejected(self):
        state = input_order_state(5, continuing=(4,))
        inputs = state["transactions"][CHILD]["data"]["vin"]
        inputs[1]["prevout"]["scriptpubkey_address"] = inputs[0]["prevout"]["scriptpubkey_address"]
        graph = build_graph(state, merge_addresses=False, group_context_inputs=True)
        first, second = grouped_edge(graph, 0), grouped_edge(graph, 1)
        first["original_source"], second["original_source"] = second["original_source"], first["original_source"]
        with self.assertRaisesRegex(TraceError, "saved UTXO"):
            transaction_csv_rows(graph, state)

    def test_group_membership_and_endpoint_corruption_fail_closed(self):
        state = input_order_state(4, continuing=(3,))
        source = build_graph(state, group_context_inputs=True)
        changes = {
            "missing_member": lambda graph, details: details["members"].pop(),
            "duplicate_member": lambda graph, details: details["members"].append(deepcopy(details["members"][0])),
            "visible_member": lambda graph, details: graph["nodes"].append(deepcopy(details["members"][0])),
            "wrong_member_kind": lambda graph, details: details["members"][0].update(kind="event"),
            "wrong_network": lambda graph, details: details["members"][0]["details"].update(network="bitcoin"),
            "missing_occurrence": lambda graph, details: details["members"][0]["details"].update(occurrences=[]),
            "wrong_address": lambda graph, details: details["members"][0]["details"].update(address="SYNTHETIC-foreign"),
            "wrong_target": lambda graph, details: details.update(transaction_id="tx:" + "f" * 64),
            "wrong_count": lambda graph, details: details.update(input_count=99),
            "wrong_address_count": lambda graph, details: details.update(address_count=99),
            "missing_input": lambda graph, details: details["input_edge_ids"].pop(),
            "duplicate_input": lambda graph, details: details["input_edge_ids"].append(details["input_edge_ids"][0]),
            "unknown_input": lambda graph, details: details["input_edge_ids"].__setitem__(0, "in:" + CHILD + ":99"),
            "wrong_role": lambda graph, details: grouped_edge(graph).update(role="traced_input"),
            "wrong_utxo": lambda graph, details: grouped_edge(graph).update(outpoint="foreign:0"),
            "reversed": lambda graph, details: grouped_edge(graph).update(source="tx:" + CHILD, target=group(graph)["id"]),
        }
        for name, change in changes.items():
            with self.subTest(corruption=name):
                graph = deepcopy(source)
                change(graph, group(graph)["details"])
                with self.assertRaises(TraceError):
                    transaction_csv_rows(graph, state)

    def test_grouped_pegin_and_coinbase_inputs_are_rejected(self):
        for flag in ("is_pegin", "is_coinbase"):
            with self.subTest(flag=flag):
                state = input_order_state(4, continuing=(3,))
                graph = build_graph(state, group_context_inputs=True)
                state["transactions"][CHILD]["data"]["vin"][0][flag] = True
                node = next(node for node in graph["nodes"] if node["id"] == "tx:" + CHILD)
                node["details"]["transaction"]["vin"][0][flag] = True
                with self.assertRaisesRegex(TraceError, "coinbase or peg-in"):
                    transaction_csv_rows(graph, state)

    def test_group_visual_changes_do_not_change_evidence_rows(self):
        state = input_order_state(12, continuing=(11,))
        graph = build_graph(state, group_context_inputs=True)
        expected = transaction_csv_rows(graph, state)
        summary = group(graph)
        summary.update(label="Abbreviated summary", x=0, y=0, width=1, height=1)
        for member in summary["details"]["members"]:
            member.update(label="Abbreviated address", x=99, y=99, color="#112233")
        for edge in graph["edges"]:
            edge.update(label="vin unknown", quantity="fake amount", caption_display="details_only")
        self.assertEqual(transaction_csv_rows(graph, state), expected)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "transactions.csv"
            write_transaction_csv(path, graph, state)
            self.assertEqual(len(list(csv.DictReader(io.StringIO(path.read_text())))), len(expected))


if __name__ == "__main__":
    unittest.main()
