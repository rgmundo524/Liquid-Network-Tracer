"""Path-only transaction lists and one-row-per-terminal-output CSV exports."""
from copy import deepcopy
import csv
import json
from pathlib import Path
import tempfile
import unittest

from liquid_tracer.common import LBTC, TraceError
from liquid_tracer.pegout_csv import (
    ENDPOINT_FIELDS, ENDPOINT_TABLE_FIELDS, PATH_FIELDS, endpoint_table_rows, pegout_csv_rows,
    write_endpoint_table_csv, write_pegout_csvs,
)
from liquid_tracer.pegout_paths import pegout_graph, validate_query
from tests.test_attribution_convergence import annotation, graph_state, tx
from tests.test_named_hop_plots import named_state
from tests.test_pegout_paths import add_pegout, add_unspendable, mark_unspent, set_address


def graph(state, minimum=0, maximum=10, **options):
    return pegout_graph(state, validate_query(seeds=state["seeds"], min_hops=minimum,
                                             max_hops=maximum, **options))


def source_paths(row):
    return {entry["seed_outpoint"]: entry["hops"] for entry in json.loads(row["Source Paths"])}


def set_value(state, key, value, asset=LBTC):
    txid, index = key.rsplit(":", 1)
    output = state["transactions"][txid]["data"]["vout"][int(index)]
    output.pop("valuecommitment", None)
    output.pop("assetcommitment", None)
    output.update(value=value, asset=asset)
    for record in state["transactions"].values():
        for vin in record["data"]["vin"]:
            if vin.get("txid") == txid and vin.get("vout") == int(index):
                vin["prevout"] = deepcopy(output)


class PegoutCSVTests(unittest.TestCase):
    def test_endpoint_table_uses_sample_columns_and_selected_source_value(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        set_value(state, tx("a") + ":0", 500_000_001)
        pegout = add_pegout(state, tx("b"))
        set_value(state, pegout, 125_000_000)
        row, = endpoint_table_rows(graph(state), state)
        self.assertEqual(ENDPOINT_TABLE_FIELDS[:7], (
            "Source", "Source Value", "Deposit/Peg-out Tx", "Address/Peg-out Address",
            "Receiving Entity", "Status", "Pegout LBTC",
        ))
        self.assertEqual((row["Source"], row["Source Value"]), (tx("a"), "5.00000001 L-BTC"))
        self.assertEqual((row["Deposit/Peg-out Tx"], row["Outpoint"]), (tx("b"), pegout))
        self.assertEqual((row["Status"], row["Pegout LBTC"]), ("Pegout", "1.25"))
        self.assertEqual(row["Source Seed Outpoints"], tx("a") + ":0")
        self.assertEqual(row["Hops from Seed"], 1)
        self.assertEqual(json.loads(row["Source Seed Hops"]), {tx("a") + ":0": 1})

    def test_seed_endpoint_is_zero_hops(self):
        for kind in ("pegout", "unspent", "unspendable"):
            with self.subTest(kind=kind):
                state = graph_state(seeds=("a:0",))
                key = (add_pegout(state, tx("a")) if kind == "pegout" else
                       add_unspendable(state, tx("a")) if kind == "unspendable" else tx("a") + ":0")
                if kind == "unspent":
                    mark_unspent(state, key)
                state["seeds"] = [key]
                row, = endpoint_table_rows(graph(state, include_unspent=True, include_unspendable=True), state)
                self.assertEqual(row["Hops from Seed"], 0)
                self.assertEqual(json.loads(row["Source Seed Hops"]), {key: 0})

    def test_named_hop_reset_keeps_actual_seed_distances_for_each_source(self):
        state = named_state((("a:0", "d"), ("b:0", "c"), ("c:0", "d"), ("d:0", "e")),
                            group=("a", "b", "c", "d"), seeds=("a:0", "b:0"))
        add_pegout(state, tx("e"))
        state["transactions"][tx("e")]["depth"] = 99  # Cached archive depths are not this query's distances.
        row, = endpoint_table_rows(graph(state, 1, 1), state)
        self.assertEqual((row["Hop Counts"], row["Hop Reference"]), ("1", "Perp"))
        self.assertEqual(row["Hops from Seed"], 2)
        self.assertEqual(json.loads(row["Source Seed Hops"]), {tx("a") + ":0": 2, tx("b") + ":0": 3})

    def test_converging_named_paths_use_the_shortest_qualifying_seed_distance(self):
        state = named_state((("a:0", "b"), ("b:0", "d"), ("b:1", "c"), ("c:0", "d"), ("d:0", "e")),
                            group=("a", "b", "c", "d"))
        add_pegout(state, tx("e"))
        row, = endpoint_table_rows(graph(state, 1, 1), state)
        self.assertEqual(row["Hop Counts"], "1")
        self.assertEqual(row["Hops from Seed"], 3)
        self.assertEqual(json.loads(row["Source Seed Hops"]), {tx("a") + ":0": 3})

    def test_seed_distance_excludes_a_shorter_route_outside_the_named_hop_range(self):
        state = named_state((("a:0", "b"), ("b:0", "d"), ("b:1", "c"),
                             ("c:0", "e"), ("e:0", "d"), ("d:0", "f")), group=("a", "b"))
        add_pegout(state, tx("f"))
        row, = endpoint_table_rows(graph(state, 4, 4), state)
        self.assertEqual(row["Hop Counts"], "4")
        self.assertEqual(row["Hops from Seed"], 5)
        self.assertEqual(json.loads(row["Source Seed Hops"]), {tx("a") + ":0": 5})

    def test_endpoint_table_multiple_sources_preserves_each_value_without_aggregation(self):
        state = graph_state((("a:0", "b"), ("a:1", "b"), ("c:0", "b")),
                            seeds=("a:0", "a:1", "c:0"))
        set_value(state, tx("a") + ":0", 100_000_000)
        set_value(state, tx("a") + ":1", 250_000_000)
        add_pegout(state, tx("b"))
        row, = endpoint_table_rows(graph(state), state)
        self.assertEqual(row["Source"], tx("a") + "; " + tx("c"))
        self.assertEqual(json.loads(row["Source Value"]), {
            tx("a") + ":0": "1 L-BTC", tx("a") + ":1": "2.5 L-BTC", tx("c") + ":0": "",
        })
        self.assertEqual(len(source_paths(row)), 3)

    def test_endpoint_table_confidential_source_unknowns_and_non_lbtc_assets(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        add_pegout(state, tx("b"))
        row, = endpoint_table_rows(graph(state), state)
        self.assertEqual(row["Source Value"], "")
        set_value(state, tx("a") + ":0", 1234, asset="bc" * 32)
        row, = endpoint_table_rows(graph(state), state)
        self.assertEqual(row["Source Value"], "1234 base units " + "bc" * 32)
        set_value(state, tx("a") + ":0", 1234, asset=None)
        row, = endpoint_table_rows(graph(state), state)
        self.assertEqual(row["Source Value"], "")

    def test_endpoint_table_status_and_pegout_amount_are_endpoint_specific(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        mark_unspent(state, tx("b") + ":0")
        set_value(state, tx("b") + ":0", 20_000_000)
        add_unspendable(state, tx("b"))
        add_pegout(state, tx("b"))
        rows = endpoint_table_rows(graph(state, include_unspent=True, include_unspendable=True), state)
        self.assertEqual({row["Status"] for row in rows}, {"Dormant", "OP_Return", "Pegout"})
        for row in rows:
            self.assertEqual(row["Pegout LBTC"], row["Value LBTC"] if row["Status"] == "Pegout" else "")
            self.assertEqual(row["Hops from Seed"], 1)
        self.assertEqual(next(row["Value LBTC"] for row in rows if row["Status"] == "Dormant"), "0.2")
        self.assertFalse(any(row["Status"] == "Deposit" for row in rows))

    def test_endpoint_table_headers_formula_safety_and_historical_snapshot_controls(self):
        from liquid_tracer.services import apply_service_labels
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        address = "SYNTHETIC-b-address"
        controls = {"rules": {address: {
            "enabled": True, "name": "=Saved label", "confidence": "confirmed",
            "stop_tracing": False, "updated_at": "2026-09-30T12:00:00Z",
        }}}
        state["service_controls"] = deepcopy(controls)
        state["labels"] = apply_service_labels([], controls)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "endpoints.csv"
            self.assertEqual(write_endpoint_table_csv(path, graph(state), state), 0)
            with path.open(newline="") as stream:
                self.assertEqual(list(csv.reader(stream)), [list(ENDPOINT_TABLE_FIELDS)])
            mark_unspent(state, tx("b") + ":0")
            saved = graph(state, include_unspent=True)
            changed = deepcopy(controls)
            changed["rules"][address]["name"] = "Current label"
            state["labels"] = apply_service_labels(state["labels"], changed)
            restored = deepcopy(state)
            restored["labels"] = apply_service_labels(restored["labels"], saved["service_controls"])
            self.assertEqual(write_endpoint_table_csv(path, saved, restored), 1)
            with path.open(newline="") as stream:
                row, = list(csv.DictReader(stream))
            self.assertEqual(row["Receiving Entity"], "'=Saved label")

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

    def test_complete_transaction_io_keeps_endpoint_exports_and_amounts_path_scoped(self):
        from liquid_tracer.pegout_csv import pegout_lbtc_summary
        from liquid_tracer.transaction_csv import transaction_csv_rows

        state = graph_state((("a:0", "b"), ("b:0", "c"), ("a:1", "d")), seeds=("a:0",),
                            raw_links=(("e:0", "b"), ("f:0", "b")))
        # These peg-outs are displayed siblings, but neither is in the chosen
        # hop range. The unrelated d transaction must remain absent altogether.
        for name, value in (("a", 700_000_000), ("b", 500_000_000),
                            ("c", 200_000_000), ("d", 900_000_000)):
            set_value(state, add_pegout(state, tx(name)), value)
        mark_unspent(state, tx("c") + ":0")
        add_unspendable(state, tx("c"))
        state["transactions"][tx("c")]["data"]["vout"].append(
            {"scriptpubkey": "", "scriptpubkey_type": "fee", "asset": LBTC, "value": 100})
        original = deepcopy(state)
        expected = pegout_csv_rows(graph(state, minimum=2, maximum=2), state)
        for grouped in (False, True):
            with self.subTest(grouped=grouped):
                complete = pegout_graph(state, validate_query(seeds=state["seeds"], min_hops=2,
                    max_hops=2, transaction_io="complete"), group_context_inputs=grouped)
                self.assertEqual(pegout_csv_rows(complete, state), expected)
                self.assertEqual(pegout_lbtc_summary(complete, state)["lbtc"], "2")
                self.assertEqual([row["Status"] for row in endpoint_table_rows(complete, state)], ["Pegout"])
                rows = transaction_csv_rows(complete, state)
                expected_ios = {(tx(name), direction, index) for name in "abc"
                                for direction, field in (("IN", "vin"), ("OUT", "vout"))
                                for index in range(len(state["transactions"][tx(name)]["data"][field]))}
                self.assertEqual({(row["Transaction Hash"], row["Direction"], row["Number of I/O"])
                                  for row in rows}, expected_ios)
                self.assertTrue(any("FEE" in row["Address Flags"] for row in rows))
                self.assertTrue(any("CONTEXT" in row["Address Flags"] and "PEG-OUT REQUEST" in row["Address Flags"]
                                    for row in rows))
                self.assertFalse(any("UNSPENT AT OBSERVATION" in row["Address Flags"] for row in rows))
                missing = deepcopy(complete)
                missing["edges"] = [edge for edge in missing["edges"]
                                    if edge["id"] != "out:" + tx("a") + ":1"]
                with self.assertRaisesRegex(TraceError, "complete transaction"):
                    pegout_csv_rows(missing, state)
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
        self.assertEqual(endpoints[0]["Hops from Seed"], 3)
        self.assertEqual(json.loads(endpoints[0]["Source Seed Hops"]), {tx("b") + ":0": 3})
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
        self.assertEqual(endpoints[0]["Hops from Seed"], 3)
        self.assertEqual(json.loads(endpoints[0]["Source Seed Hops"]), {tx("b") + ":0": 3})
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
