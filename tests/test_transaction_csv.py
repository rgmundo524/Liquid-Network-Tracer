"""Transaction CSV contract, exact UTXO incidence, and confidential-data handling."""
import copy
import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import LBTC, TraceError, canonical, digest, read_json
from liquid_tracer.export import build_graph, write_csv, node_csv_rows, NODE_CSV_FIELDS
from liquid_tracer.transaction_csv import (TRANSACTION_CSV_FIELDS, transaction_csv_rows,
                                           write_transaction_csv)
from liquid_tracer.connections import (connection_graph, preview_connections, reviewed_connections,
                                       FILES, LEGACY_FILES)
from liquid_tracer.investigations import create_investigation
from tests.test_connections import saved_case
from tests.test_attribution_convergence import annotation, graph_state, tx
from tests.test_branch_interactions import receiving
from tests.fixtures import A, B, C, D, fixture


def state_fixture():
    data = fixture()
    state = graph_state()
    state["transactions"] = {key[4:]: {"data": value, "observation_id": key[4:], "depth": 0}
                             for key, value in data.items() if key.startswith("/tx/") and not key.endswith("/outspends")}
    state["outputs"] = {}
    state["links"] = {}
    state["seeds"] = [A + ":0", B + ":0"]
    return state


def find(rows, txid, direction, index):
    return next(row for row in rows if (row["Transaction Hash"], row["Direction"], row["Number of I/O"])
                == (txid, direction, index))


class TransactionCSVTests(unittest.TestCase):
    def setUp(self):
        self.state = state_fixture()
        self.graph = build_graph(self.state)

    def test_exact_headers_one_row_per_arrow_and_no_graph_object_fields(self):
        rows = transaction_csv_rows(self.graph, self.state)
        self.assertEqual(len(rows), len(self.graph["edges"]))
        self.assertTrue(all(tuple(row) == TRANSACTION_CSV_FIELDS for row in rows))
        self.assertEqual(TRANSACTION_CSV_FIELDS, (
            "Block", "Time", "Transaction Label", "Transaction Hash", "Address Label",
            "Address Flags", "Address Hash", "Asset Value", "Asset",
            "PegOut Value", "Direction", "Number of I/O"))
        self.assertFalse({"id", "source", "target", "color", "x", "y", "details"} & set(rows[0]))

    def test_input_index_is_vin_not_funding_vout(self):
        vin = self.state["transactions"][C]["data"]["vin"][2]
        vin["vout"] = 7
        rows = transaction_csv_rows(build_graph(self.state), self.state)
        row = find(rows, C, "IN", 2)
        self.assertEqual(row["Number of I/O"], 2)
        self.assertEqual(row["Address Hash"], vin["prevout"]["scriptpubkey_address"])
        self.assertEqual(row["Transaction Hash"], C)

    def test_same_utxo_has_distinct_output_and_spending_input_rows(self):
        rows = transaction_csv_rows(self.graph, self.state)
        sent, received = find(rows, A, "OUT", 0), find(rows, B, "IN", 0)
        self.assertEqual(sent["Address Hash"], received["Address Hash"])
        self.assertEqual(sent["Asset Value"], received["Asset Value"])
        self.assertNotEqual(sent["Transaction Hash"], received["Transaction Hash"])

    def test_utc_block_belongs_to_the_host_transaction_not_the_funding_transaction(self):
        self.state["transactions"][B]["data"]["status"].update(block_time=0, block_height=0)
        rows = transaction_csv_rows(build_graph(self.state), self.state)
        self.assertEqual(find(rows, B, "IN", 0)["Time"], "1970-01-01T00:00:00Z")
        self.assertEqual(find(rows, B, "IN", 0)["Block"], 0)
        self.assertEqual(find(rows, A, "OUT", 0)["Time"], "2023-11-14T22:13:20Z")

    def test_missing_invalid_and_unconfirmed_dates_are_blank_not_run_times(self):
        for status in ({"confirmed": False, "block_time": 10, "block_height": 4},
                       {"confirmed": True}, {"confirmed": True, "block_time": True, "block_height": False},
                       {"confirmed": True, "block_time": -1, "block_height": -1}):
            self.state["transactions"][A]["data"]["status"] = status
            row = find(transaction_csv_rows(build_graph(self.state), self.state), A, "OUT", 0)
            self.assertEqual((row["Block"], row["Time"]), ("", ""))

    def test_pegout_value_is_actual_request_output_and_destination_not_payout_claim(self):
        row = find(transaction_csv_rows(self.graph, self.state), D, "OUT", 0)
        self.assertEqual(row["PegOut Value"], "0.00001234")
        self.assertEqual(row["Asset Value"], "0.00001234")
        self.assertEqual(row["Address Hash"], "SYNTHETIC-bitcoin-payout-request")
        self.assertIn("PEG-OUT REQUEST", row["Address Flags"])
        self.assertNotIn("USD Value", row)
        self.assertEqual(find(transaction_csv_rows(self.graph, self.state), D, "IN", 0)["PegOut Value"], "")

    def test_hidden_amounts_are_empty_and_real_zero_is_preserved(self):
        rows = transaction_csv_rows(self.graph, self.state)
        self.assertEqual(find(rows, B, "OUT", 0)["Asset Value"], "")
        self.assertIn("CONFIDENTIAL VALUE", find(rows, B, "OUT", 0)["Address Flags"])
        self.assertEqual(find(rows, C, "OUT", 1)["Asset Value"], "0.00000000")
        self.assertTrue(all("USD Value" not in row for row in rows))
        self.state["transactions"][D]["data"]["vout"][0].pop("value")
        row = find(transaction_csv_rows(build_graph(self.state), self.state), D, "OUT", 0)
        self.assertEqual(row["PegOut Value"], "")

    def test_big_integer_amounts_remain_exact_and_no_token_decimals_are_guessed(self):
        value = 2 ** 63 - 1
        out = self.state["transactions"][D]["data"]["vout"][0]
        out.update(value=value, asset="ab" * 32)
        graph = build_graph(self.state)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transactions.csv"
            write_transaction_csv(path, graph, self.state)
            rows = list(csv.DictReader(io.StringIO(path.read_text())))
        row = next(r for r in rows if r["Transaction Hash"] == D and r["Direction"] == "OUT")
        self.assertEqual(row["Asset Value"], str(value))
        self.assertEqual(row["PegOut Value"], str(value))

    def test_invalid_explicit_values_fail_before_writing(self):
        for value in (True, -1, 1.5, "100"):
            self.state["transactions"][D]["data"]["vout"][0]["value"] = value
            with self.assertRaises(TraceError): transaction_csv_rows(build_graph(self.state), self.state)

    def test_only_graph_selected_arrows_including_fee_visibility(self):
        for fees in (True, False):
            graph = build_graph(self.state, include_fees=fees)
            rows = transaction_csv_rows(graph, self.state)
            self.assertEqual(len(rows), len(graph["edges"]))
            self.assertEqual(any("FEE" in r["Address Flags"] for r in rows), fees)
        graph = copy.deepcopy(self.graph)
        graph["edges"] = [edge for edge in graph["edges"] if edge["id"] == "out:" + A + ":0"]
        self.assertEqual(len(transaction_csv_rows(graph, self.state)), 1)

    def test_labels_are_exact_outpoint_assessments_not_merged_node_aggregation(self):
        state = graph_state(seeds=("a:0", "a:1", "b:0"))
        receiving(state, "a:0", "SYNTHETIC-shared")
        receiving(state, "a:1", "SYNTHETIC-shared")
        state["labels"] = [dict(annotation(name="Only output zero", stop=True), kind="outpoint", value=tx("a") + ":0")]
        rows = transaction_csv_rows(build_graph(state), state)
        first, second = find(rows, tx("a"), "OUT", 0), find(rows, tx("a"), "OUT", 1)
        self.assertEqual(first["Address Label"], "Suspected Only output zero")
        self.assertNotIn("Address Entities", first)
        self.assertIn("STOP TRACING", first["Address Flags"])
        self.assertEqual(second["Address Label"], "")
        self.assertNotIn("STOP TRACING", second["Address Flags"])

    def test_pegin_and_coinbase_inputs_have_no_invented_liquid_attribution(self):
        transaction = self.state["transactions"][A]["data"]
        vin = transaction["vin"][0]
        address = vin["prevout"]["scriptpubkey_address"]
        self.state["labels"] = [annotation(address=address, name="Liquid only")]
        vin["is_pegin"] = True
        row = find(transaction_csv_rows(build_graph(self.state), self.state), A, "IN", 0)
        self.assertEqual(row["Address Label"], "")
        self.assertIn("PEG-IN", row["Address Flags"])
        transaction["vin"] = [{"is_coinbase": True}]
        row = find(transaction_csv_rows(build_graph(self.state), self.state), A, "IN", 0)
        self.assertEqual((row["Address Hash"], row["Asset Value"]), ("", ""))
        self.assertIn("COINBASE", row["Address Flags"])

    def test_caption_styles_positions_and_truncation_are_never_evidence(self):
        graph = copy.deepcopy(self.graph)
        for node in graph["nodes"]:
            node.update(label="WRONG HASH", color="#112233", x=99999)
        for edge in graph["edges"]: edge.update(label="WRONG INDEX", quantity="999 FAKE")
        self.assertEqual(transaction_csv_rows(graph, self.state), transaction_csv_rows(self.graph, self.state))

    def test_starter_labels_and_transaction_group_order(self):
        rows = transaction_csv_rows(self.graph, self.state)
        self.assertTrue(find(rows, A, "OUT", 0)["Transaction Label"].startswith("Starting TX "))
        self.assertEqual(find(rows, D, "OUT", 0)["Transaction Label"], "")
        for txid in (A, B, C, D):
            part = [r for r in rows if r["Transaction Hash"] == txid]
            self.assertEqual([(r["Direction"], r["Number of I/O"]) for r in part],
                             sorted((r["Direction"], r["Number of I/O"]) for r in part))

    def test_duplicate_wrong_direction_wrong_outpoint_and_foreign_transactions_rejected(self):
        for change in ("duplicate", "direction", "outpoint", "amount", "source", "endpoint"):
            graph = copy.deepcopy(self.graph)
            if change == "duplicate": graph["edges"].append(copy.deepcopy(graph["edges"][0]))
            if change == "direction": graph["edges"][0]["source"], graph["edges"][0]["target"] = graph["edges"][0]["target"], graph["edges"][0]["source"]
            if change == "outpoint": graph["edges"][0]["outpoint"] = "wrong"
            if change == "amount":
                node = next(n for n in graph["nodes"] if n["kind"] == "transaction")
                node["details"]["transaction"]["vout"][0]["value"] = 999
            if change == "source": graph["namespace"]["source"] = "other"
            if change == "endpoint":
                edge = next(e for e in graph["edges"] if e["id"] == "out:" + A + ":0")
                other = next(e for e in graph["edges"] if e["id"] == "out:" + B + ":1")
                edge["target"] = other["target"]
            with self.assertRaises(TraceError): transaction_csv_rows(graph, self.state)

    def test_csv_unicode_quotes_multiline_and_whitespace_formula_protection(self):
        address = '  =SUM(1,2),"Synthetic"\nCafé'
        self.state["transactions"][D]["data"]["vout"][0]["pegout"]["scriptpubkey_address"] = address
        graph = build_graph(self.state)
        before = canonical((self.state, graph))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transactions.csv"
            with patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("offline")):
                write_transaction_csv(path, graph, self.state)
            rows = list(csv.DictReader(io.StringIO(path.read_text())))
        row = next(r for r in rows if r["Transaction Hash"] == D and r["Direction"] == "OUT")
        self.assertEqual(row["Address Hash"], "'" + address)
        self.assertEqual(before, canonical((self.state, graph)))

    def test_connection_csv_contains_only_connecting_io_and_empty_result_has_header(self):
        state = graph_state((("a:0", "c"), ("c:0", "b")))
        for hops, count in ((2, 4), (1, 0)):
            graph = connection_graph(state, hops)
            self.assertEqual(len(transaction_csv_rows(graph, state)), count)
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "transactions.csv"
                write_transaction_csv(path, graph, state)
                reader = csv.DictReader(io.StringIO(path.read_text()))
                self.assertEqual(tuple(reader.fieldnames), TRANSACTION_CSV_FIELDS)
                self.assertEqual(len(list(reader)), count)

    def test_connection_preview_and_legacy_snapshot_compatibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = create_investigation(Path(tmp), "CSV connections")
            saved_case(case)
            result = preview_connections(case, max_hops=2)
            directory = Path(result["directory"])
            graph, plan = reviewed_connections(case, result["preview_id"])
            self.assertEqual({p.name for p in directory.glob("*.csv")}, {"transactions.csv"})
            self.assertEqual(len(list(csv.DictReader(io.StringIO((directory/"transactions.csv").read_text())))), 4)
            (directory/"transactions.csv").unlink()
            write_csv(directory/"nodes.csv", node_csv_rows(graph), NODE_CSV_FIELDS)
            write_csv(directory/"edges.csv", graph["edges"], ("id", "source", "target"))
            (directory/"SHA256SUMS").write_text("".join(digest((directory/name).read_bytes()) + "  " + name + "\n"
                for name in sorted(LEGACY_FILES - {"SHA256SUMS"})))
            self.assertEqual(reviewed_connections(case, result["preview_id"]), (graph, plan))


if __name__ == "__main__":
    unittest.main()
