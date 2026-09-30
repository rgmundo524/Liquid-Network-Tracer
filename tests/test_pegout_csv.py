"""Path-only transaction lists and one-row-per-terminal-output CSV exports."""
from copy import deepcopy
import csv
import json
from pathlib import Path
import tempfile
import unittest

from liquid_tracer.common import LBTC, TraceError
from liquid_tracer.pegout_csv import ENDPOINT_FIELDS, PATH_FIELDS, pegout_csv_rows, write_pegout_csvs
from liquid_tracer.pegout_paths import pegout_graph, validate_query
from tests.test_attribution_convergence import annotation, graph_state, tx
from tests.test_named_hop_plots import named_state
from tests.test_pegout_paths import add_pegout, add_unspendable, mark_unspent, set_address


def graph(state, minimum=0, maximum=10, **options):
    return pegout_graph(state, validate_query(seeds=state["seeds"], min_hops=minimum,
                                             max_hops=maximum, **options))


def source_paths(row):
    return {entry["seed_outpoint"]: entry["hops"] for entry in json.loads(row["Source Paths"])}


class PegoutCSVTests(unittest.TestCase):
    def test_endpoints_and_paths_exclude_context_and_unrelated_investigation_transactions(self):
        state = graph_state((("a:0", "b"), ("b:0", "c"), ("a:1", "d")),
                            seeds=("a:0",), raw_links=(("e:0", "b"), ("f:0", "b")))
        endpoint = add_pegout(state, tx("c"))
        add_pegout(state, tx("d"))
        original = deepcopy(state)
        ordinary = pegout_csv_rows(graph(state), state)
        context = pegout_graph(state, validate_query(seeds=state["seeds"], include_context=True),
                               group_context_inputs=True)
        grouped = pegout_csv_rows(context, state)
        self.assertEqual(ordinary, grouped)
        path_rows, endpoints = ordinary
        self.assertEqual([row["Transaction Hash"] for row in path_rows], [tx(name) for name in "abc"])
        self.assertEqual([row["Roles"] for row in path_rows], ["Seed", "Intermediate", "Endpoint"])
        self.assertEqual(path_rows[1]["Traced Input Outpoints"], tx("a") + ":0")
        self.assertEqual(path_rows[-1]["Endpoint Outpoints"], endpoint)
        self.assertEqual([row["Outpoint"] for row in endpoints], [endpoint])
        self.assertEqual(state, original)

    def test_multiple_selected_outputs_same_transaction_preserve_exact_source_membership(self):
        state = graph_state((("a:0", "b"), ("a:1", "c"), ("b:0", "d"), ("c:0", "d")),
                            seeds=("a:0", "a:1"))
        endpoint = add_pegout(state, tx("d"))
        rows, endpoints = pegout_csv_rows(graph(state), state)
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(endpoints), 1)
        result = endpoints[0]
        self.assertEqual(result["Outpoint"], endpoint)
        self.assertEqual(result["Source Transactions"], tx("a"))
        self.assertEqual(source_paths(result), {tx("a") + ":0": [2], tx("a") + ":1": [2]})
        by_tx = {row["Transaction Hash"]: row for row in rows}
        self.assertEqual(source_paths(by_tx[tx("b")]), {tx("a") + ":0": [1]})
        self.assertEqual(source_paths(by_tx[tx("c")]), {tx("a") + ":1": [1]})

    def test_source_provenance_respects_minimum_range_after_convergence(self):
        state = graph_state((("a:0", "d"), ("b:0", "c"), ("c:0", "d"), ("d:0", "e")),
                            seeds=("a:0", "b:0"))
        add_pegout(state, tx("e"))
        rows, endpoints = pegout_csv_rows(graph(state, 3, 3), state)
        self.assertEqual({row["Transaction Hash"] for row in rows}, {tx(name) for name in "bcde"})
        self.assertEqual(source_paths(endpoints[0]), {tx("b") + ":0": [3]})
        self.assertTrue(all(row["Source Seed Outpoints"] == tx("b") + ":0" for row in rows))

    def test_reused_address_outputs_remain_distinct_dormant_rows_and_do_not_supply_provenance(self):
        state = graph_state((("a:0", "b"), ("b:0", "c"), ("d:0", "e")), seeds=("a:0", "d:0"))
        for name in "ce":
            key = tx(name) + ":0"
            set_address(state, key, "SYNTHETIC-shared-address")
            mark_unspent(state, key)
        state["labels"] = [annotation(confidence="confirmed", stop=False, name="Perp",
                                      address="SYNTHETIC-shared-address")]
        rows, endpoints = pegout_csv_rows(graph(state, include_unspent=True), state)
        self.assertEqual(len(endpoints), 2)
        by_tx = {row["Transaction Hash"]: row for row in endpoints}
        self.assertEqual(source_paths(by_tx[tx("c")]), {tx("a") + ":0": [2]})
        self.assertEqual(source_paths(by_tx[tx("e")]), {tx("d") + ":0": [1]})
        for row in endpoints:
            self.assertEqual((row["Status"], row["Receiving Entity"]), ("Dormant", "Perp"))
            self.assertEqual((row["Value Base Units"], row["Value LBTC"], row["Asset"]), ("", "", ""))
            self.assertEqual(row["Spend Observation ID"], 7)

    def test_terminal_types_values_assets_and_observation_times(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        dormant = tx("b") + ":0"
        mark_unspent(state, dormant, 7)
        pegout = add_pegout(state, tx("b"))
        unspendable = add_unspendable(state, tx("b"))
        state["transactions"][tx("b")]["observation_id"] = 5
        values = state["transactions"][tx("b")]["data"]["vout"]
        values[1]["value"] = 520_000_001
        # Fee outputs are never terminal matches.
        values.append({"scriptpubkey": "", "scriptpubkey_type": "fee", "value": 3, "asset": LBTC})
        observations = {
            5: {"id": 5, "source": state["source"], "endpoint": "/tx/" + tx("b"),
                "status": 200, "fetched_at": "2026-09-30T12:30:00Z"},
            7: {"id": 7, "source": state["source"], "endpoint": "/tx/" + tx("b") + "/outspends",
                "status": 200, "fetched_at": "2026-09-30T12:31:00Z"},
        }
        _, rows = pegout_csv_rows(graph(state, include_unspent=True, include_unspendable=True),
                                 state, observations=observations)
        self.assertEqual(len(rows), 3)
        by_point = {row["Outpoint"]: row for row in rows}
        self.assertEqual((by_point[pegout]["Status"], by_point[pegout]["Value LBTC"]), ("Peg-out", "5.20000001"))
        self.assertEqual(by_point[pegout]["Address"], "SYNTHETIC-bitcoin-request")
        self.assertEqual((by_point[unspendable]["Status"], by_point[unspendable]["Value Base Units"],
                          by_point[unspendable]["Value LBTC"]), ("OP_RETURN", 0, "0"))
        self.assertEqual(by_point[dormant]["Spend Observed At"], "2026-09-30T12:31:00Z")
        self.assertEqual(by_point[pegout]["Transaction Observed At"], "2026-09-30T12:30:00Z")
        self.assertEqual(by_point[pegout]["Spend Observed At"], "")
        observations[7]["endpoint"] = "/tx/" + tx("c") + "/outspends"
        with self.assertRaisesRegex(TraceError, "observation does not match"):
            pegout_csv_rows(graph(state, include_unspent=True), state, observations=observations)

    def test_stale_or_missing_unspent_evidence_does_not_create_dormant_endpoints(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        mark_unspent(state, tx("a") + ":0")  # Contradicted by a saved spending input.
        state["outputs"][tx("b") + ":0"]["status"] = "unspent_at_observation"
        self.assertEqual(pegout_csv_rows(graph(state, include_unspent=True), state), ([], []))

    def test_named_group_hops_and_attribution_budget_remain_source_specific(self):
        state = named_state((("a:0", "d"), ("b:0", "c"), ("c:0", "d"), ("d:0", "e")),
                            group=("a", "b", "c"), seeds=("a:0", "b:0"), maximum=2)
        endpoint = add_pegout(state, tx("e"))
        state["labels"][0]["hop_limit"] = 1
        state["transactions"][tx("e")]["depth"] = 3
        rows, endpoints = pegout_csv_rows(graph(state, 2, 2), state)
        self.assertEqual([row["Outpoint"] for row in endpoints], [endpoint])
        self.assertEqual(source_paths(endpoints[0]), {tx("b") + ":0": [2]})
        self.assertEqual((endpoints[0]["Hop Reference"], endpoints[0]["Seed Depth"]), ("Perp", 3))
        self.assertEqual({row["Transaction Hash"] for row in rows}, {tx(name) for name in "bcde"})
        self.assertEqual(next(row["Hop Counts"] for row in rows if row["Transaction Hash"] == tx("c")), "0")

    def test_unknown_and_other_assets_are_never_converted_to_lbtc(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        add_pegout(state, tx("b"))
        output = state["transactions"][tx("b")]["data"]["vout"][1]
        output["value"] = 9_007_199_254_740_993
        rows = pegout_csv_rows(graph(state), state)[1]
        self.assertEqual(rows[0]["Value LBTC"], "90071992.54740993")
        output["asset"] = "ab" * 32
        self.assertEqual(pegout_csv_rows(graph(state), state)[1][0]["Value LBTC"], "")
        output.pop("asset")
        self.assertEqual(pegout_csv_rows(graph(state), state)[1][0]["Asset"], "")
        output.pop("value")
        output["valuecommitment"] = "08" + "ab" * 32
        self.assertEqual(pegout_csv_rows(graph(state), state)[1][0]["Value Base Units"], "")

    def test_csv_writes_headers_for_empty_results_and_escapes_formula_labels(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            self.assertEqual(write_pegout_csvs(destination, graph(state), state),
                             {"path_transactions": 0, "trace_endpoints": 0})
            for name, fields in (("path-transactions.csv", PATH_FIELDS), ("trace-endpoints.csv", ENDPOINT_FIELDS)):
                with (destination / name).open(newline="") as stream:
                    self.assertEqual(list(csv.reader(stream)), [list(fields)])
            mark_unspent(state, tx("b") + ":0")
            state["labels"] = [annotation(confidence="confirmed", stop=False, name="=HYPERLINK(1)",
                                          address="SYNTHETIC-b-address")]
            write_pegout_csvs(destination, graph(state, include_unspent=True), state)
            with (destination / "trace-endpoints.csv").open(newline="") as stream:
                row, = list(csv.DictReader(stream))
            self.assertEqual(row["Receiving Entity"], "'=HYPERLINK(1)")

    def test_export_rejects_tampered_membership_and_saved_transaction_facts(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        add_pegout(state, tx("b"))
        original = graph(state)
        changes = (
            lambda value: value["pegouts"]["outpoints"].clear(),
            lambda value: value["pegouts"]["matches"][0]["hops"].append(9),
            lambda value: value["edges"].pop(),
            lambda value: next(node for node in value["nodes"] if node["kind"] == "transaction")["details"].update(transaction={}),
        )
        for change in changes:
            altered = deepcopy(original)
            change(altered)
            with self.assertRaises(TraceError):
                pegout_csv_rows(altered, state)


if __name__ == "__main__":
    unittest.main()
