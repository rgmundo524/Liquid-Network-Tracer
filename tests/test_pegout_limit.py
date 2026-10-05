"""Exact cumulative peg-out selection and globally scoped CSV provenance."""
from copy import deepcopy
import csv
import json
from pathlib import Path
import tempfile
import random
import unittest
from unittest.mock import patch

from liquid_tracer.common import LBTC, TraceError
from liquid_tracer.pegout_csv import (PEGOUT_LIMIT_FIELDS, PATH_FIELDS, ENDPOINT_FIELDS, ENDPOINT_TABLE_FIELDS,
    endpoint_table_rows, pegout_csv_rows, write_pegout_csvs, write_endpoint_table_csv)
from liquid_tracer.pegout_limit import normalize_pegout_lbtc_limit, validate_pegout_limit_summary
from liquid_tracer.pegout_paths import _paths, pegout_graph, validate_query
from tests.test_attribution_convergence import annotation, graph_state, tx
from tests.test_named_hop_plots import named_state
from tests.test_pegout_csv import set_value, source_paths
from tests.test_pegout_paths import add_pegout, add_unspendable, mark_unspent


def endpoint(state, name, units):
    key = add_pegout(state, tx(name))
    set_value(state, key, units)
    return key


def graph(state, limit="1", **options):
    query = validate_query(seeds=state["seeds"], pegout_lbtc_limit=limit, **options)
    return pegout_graph(state, query)


class PegoutLimitTests(unittest.TestCase):
    def test_decimal_normalization_is_exact_and_omitted_by_default(self):
        self.assertIsNone(normalize_pegout_lbtc_limit(None))
        for raw, expected in ((" 001.20000000 ", "1.2"), ("0.00000001", "0.00000001"),
                              ("9007199254740993.00000001", "9007199254740993.00000001")):
            self.assertEqual(normalize_pegout_lbtc_limit(raw), expected)
        for invalid in (True, False, 1, 1.2, "", "0", "0.00000000", "-1", "+1", "1e2", ".1", "1.", "0.000000001"):
            with self.subTest(value=invalid), self.assertRaises(TraceError):
                normalize_pegout_lbtc_limit(invalid)
        self.assertNotIn("pegout_lbtc_limit", validate_query(tx("a")))
        self.assertEqual(validate_query(tx("a"), pegout_lbtc_limit="01.00000000")["pegout_lbtc_limit"], "1")

    def test_threshold_crossing_output_is_whole_and_later_ties_are_excluded(self):
        state = graph_state((("a:0", "b"), ("a:1", "c"), ("a:2", "d")), seeds=("a:0", "a:1", "a:2"))
        first, crossing, later = [endpoint(state, name, units) for name, units in (("b", 70_000_000), ("c", 60_000_000), ("d", 90_000_000))]
        result = graph(state)
        self.assertEqual([item["outpoint"] for item in result["pegouts"]["matches"]], [first, crossing])
        summary = result["pegouts"]["pegout_limit_summary"]
        self.assertEqual((summary["total_lbtc"], summary["excess_lbtc"], summary["stopping_outpoint"]), ("1.3", "0.3", crossing))
        self.assertEqual(summary["cutoff_seed_hops"], 1)
        self.assertEqual(summary["counted_pegout_count"], 2)
        self.assertTrue(summary["limit_reached"])
        self.assertEqual(validate_pegout_limit_summary(summary, result["pegouts"]["query"]), summary)
        _, rows = pegout_csv_rows(result, state)
        self.assertEqual({row["Outpoint"] for row in rows}, {first, crossing})
        self.assertNotIn(later, {row["Outpoint"] for row in rows})

    def test_exact_total_and_numeric_output_order(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        keys = [endpoint(state, "b", 10_000_000) for _ in range(11)]
        result = graph(state, "0.2")
        self.assertEqual([item["outpoint"] for item in result["pegouts"]["matches"]], keys[:2])
        self.assertEqual(result["pegouts"]["pegout_limit_summary"]["excess_base_units"], "0")
        self.assertEqual(result["pegouts"]["pegout_limit_summary"]["total_base_units"], "20000000")

    def test_seed_endpoint_stops_at_zero_without_admitting_unselected_siblings(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        start = endpoint(state, "a", 100_000_000)
        endpoint(state, "b", 200_000_000)
        result = graph(state)
        self.assertNotEqual(result["pegouts"]["matches"][0]["outpoint"], start)
        state["seeds"].append(start)
        result = graph(state)
        self.assertEqual(result["pegouts"]["matches"][0]["outpoint"], start)
        self.assertEqual(result["pegouts"]["outpoints"], [])
        self.assertEqual(result["pegouts"]["pegout_limit_summary"]["cutoff_seed_hops"], 0)

    def test_same_depth_join_retains_every_seed_without_double_counting(self):
        state = graph_state((("a:0", "b"), ("a:1", "c"), ("b:0", "d"), ("c:0", "d")), seeds=("a:0", "a:1"))
        chosen = endpoint(state, "d", 120_000_000)
        result = graph(state, transaction_io="complete")
        self.assertEqual(result["pegouts"]["outpoints"], sorted(state["links"]))
        self.assertEqual(result["pegouts"]["pegout_limit_summary"]["counted_pegout_count"], 1)
        self.assertEqual(result["pegouts"]["pegout_limit_summary"]["total_lbtc"], "1.2")
        _, rows = pegout_csv_rows(result, state)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Outpoint"], chosen)
        self.assertEqual(source_paths(rows[0]), {seed: [2] for seed in state["seeds"]})
        self.assertEqual(json.loads(rows[0]["Source Seed Hops"]), {seed: 2 for seed in state["seeds"]})

    def test_global_budget_does_not_restart_per_seed_or_include_later_route(self):
        state = graph_state((("a:0", "c"), ("b:0", "d"), ("d:0", "e"), ("e:0", "f"),
                             ("d:1", "c")), seeds=("a:0", "b:0"))
        first, second, last = endpoint(state, "c", 60_000_000), endpoint(state, "e", 50_000_000), endpoint(state, "f", 80_000_000)
        result = graph(state)
        self.assertEqual({row["outpoint"] for row in result["pegouts"]["matches"]}, {first, second})
        _, rows = pegout_csv_rows(result, state)
        by_key = {row["Outpoint"]: row for row in rows}
        self.assertEqual(set(by_key), {first, second})
        self.assertEqual(source_paths(by_key[first]), {tx("a") + ":0": [1], tx("b") + ":0": [2]})
        self.assertEqual(source_paths(by_key[second]), {tx("b") + ":0": [2]})
        self.assertNotIn(last, by_key)

    def test_cutoff_rejects_later_path_to_already_counted_endpoint(self):
        state = graph_state((("a:0", "d"), ("b:0", "c"), ("c:0", "d")), seeds=("a:0", "b:0"))
        chosen = endpoint(state, "d", 100_000_000)
        result = graph(state)
        self.assertEqual(result["pegouts"]["matches"][0]["hops"], [1])
        _, rows = pegout_csv_rows(result, state)
        self.assertEqual(rows[0]["Outpoint"], chosen)
        self.assertEqual(source_paths(rows[0]), {tx("a") + ":0": [1]})

    def test_named_hops_do_not_reorder_ordinary_seed_distances(self):
        state = named_state((("a:0", "b"), ("b:0", "c"), ("a:1", "d")), group=("a", "b"), seeds=("a:0", "a:1"))
        farther, nearer = endpoint(state, "c", 200_000_000), endpoint(state, "d", 100_000_000)
        result = graph(state, min_hops=1, max_hops=1)
        self.assertEqual([row["outpoint"] for row in result["pegouts"]["matches"]], [nearer])
        self.assertNotEqual(result["pegouts"]["pegout_limit_summary"]["stopping_outpoint"], farther)
        _, rows = pegout_csv_rows(result, state)
        self.assertEqual(rows[0]["Hops from Seed"], 1)
        self.assertEqual(rows[0]["Hop Counts"], "1")

    def test_unreached_limit_preserves_named_resets_all_paths_and_bounds(self):
        state = named_state((("a:0", "b"), ("b:0", "d"), ("b:1", "c"), ("c:0", "d"), ("d:0", "e")), group=("a", "b", "d"))
        endpoint(state, "e", 50_000_000)
        regular = graph(state, None, min_hops=1, max_hops=1)
        limited = graph(state, "100", min_hops=1, max_hops=1)
        for field in ("matches", "outpoints", "transaction_count"):
            self.assertEqual(limited["pegouts"][field], regular["pegouts"][field])
        def without_limit(rows):
            return [[{key: value for key, value in row.items() if key not in PEGOUT_LIMIT_FIELDS}
                     for row in table] for table in rows]
        self.assertEqual(without_limit(pegout_csv_rows(limited, state)),
                         without_limit(pegout_csv_rows(regular, state)))
        summary = limited["pegouts"]["pegout_limit_summary"]
        self.assertEqual(summary["stop_reason"], "paths_exhausted")
        self.assertIsNone(summary["cutoff_seed_hops"])

    def test_unknown_and_non_lbtc_are_retained_but_not_added_and_fees_never_count(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        unknown_amount, unknown_asset, other, counted = [endpoint(state, "b", units) for units in (None, 500_000_000, 500_000_000, 100_000_000)]
        set_value(state, unknown_asset, 500_000_000, None)
        set_value(state, other, 500_000_000, "f" * 64)
        state["transactions"][tx("b")]["data"]["vout"].append({"scriptpubkey": "", "scriptpubkey_type": "fee", "asset": LBTC, "value": 900_000_000})
        result = graph(state, transaction_io="complete")
        summary = result["pegouts"]["pegout_limit_summary"]
        self.assertEqual(summary["total_lbtc"], "1")
        for field in ("counted_pegout_count", "unknown_amount_count", "unknown_asset_count", "non_lbtc_count"):
            self.assertEqual(summary[field], 1)
        self.assertEqual({row["outpoint"] for row in result["pegouts"]["matches"]}, {unknown_amount, unknown_asset, other, counted})
        self.assertEqual(len(pegout_csv_rows(result, state)[1]), 4)

    def test_minimum_hops_stops_and_full_evidence_validation_remain(self):
        state = graph_state((("a:0", "b"), ("b:0", "c")), seeds=("a:0",))
        first, second = endpoint(state, "b", 100_000_000), endpoint(state, "c", 100_000_000)
        self.assertEqual([item["outpoint"] for item in graph(state, min_hops=2)["pegouts"]["matches"]], [second])
        state["labels"] = [annotation(stop=True, address="SYNTHETIC-b-address")]
        result = graph(state, min_hops=2)
        self.assertEqual(result["pegouts"]["matches"], [])
        self.assertFalse(result["pegouts"]["pegout_limit_summary"]["limit_reached"])
        state["links"][tx("b") + ":0"]["vin"] = 999
        with self.assertRaises(TraceError):
            graph(state)

    def test_does_not_expand_frontier_beyond_threshold(self):
        state = graph_state((("a:0", "b"), ("b:0", "c"), ("c:0", "d")), seeds=("a:0",))
        endpoint(state, "b", 100_000_000)
        endpoint(state, "d", 200_000_000)
        from liquid_tracer.pegout_limit import output_budget
        inspected = []
        def record(labels, key, output, **kwargs):
            inspected.append(key)
            return output_budget(labels, key, output, **kwargs)
        with patch("liquid_tracer.pegout_limit.output_budget", side_effect=record):
            graph(state)
        self.assertFalse(any(key.startswith(tx("c") + ":") or key.startswith(tx("d") + ":") for key in inspected))

    def test_complete_context_may_show_excluded_pegout_without_counting_it(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        chosen, excluded = endpoint(state, "b", 100_000_000), endpoint(state, "b", 200_000_000)
        result = graph(state, transaction_io="complete")
        self.assertIn("out:" + excluded, {edge["id"] for edge in result["edges"]})
        self.assertEqual([item["outpoint"] for item in result["pegouts"]["matches"]], [chosen])
        self.assertEqual([item["Outpoint"] for item in pegout_csv_rows(result, state)[1]], [chosen])
        self.assertFalse(result["pegouts"]["pegout_limit_summary"]["context_outputs_counted"])

    def test_optional_endpoints_follow_the_same_global_cutoff_without_adding_value(self):
        state = graph_state((("a:0", "b"), ("a:1", "c")), seeds=("a:0", "a:1"))
        mark_unspent(state, tx("b") + ":0")
        terminal = add_unspendable(state, tx("b"))
        chosen = endpoint(state, "b", 100_000_000)
        later = endpoint(state, "c", 200_000_000)
        result = graph(state, include_unspent=True, include_unspendable=True, transaction_io="complete")
        self.assertEqual(result["pegouts"]["endpoint_counts"], {"pegout": 1, "unspent": 1, "unspendable": 1})
        self.assertEqual(result["pegouts"]["pegout_limit_summary"]["total_lbtc"], "1")
        _, rows = pegout_csv_rows(result, state)
        self.assertEqual({row["Outpoint"] for row in rows}, {tx("b") + ":0", terminal, chosen})
        self.assertNotIn(later, {row["Outpoint"] for row in rows})

    def test_summary_tampering_and_removed_limit_are_rejected_by_csv(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        endpoint(state, "b", 120_000_000)
        original = graph(state)
        for field, value in (("total_base_units", "120000001"), ("ordering", "random"), ("cutoff_seed_hops", True),
                             ("stopping_outpoint", tx("c") + ":1"), ("unknown_asset_count", 1)):
            changed = deepcopy(original)
            changed["pegouts"]["pegout_limit_summary"][field] = value
            with self.subTest(field=field), self.assertRaises(TraceError):
                pegout_csv_rows(changed, state)
        changed = deepcopy(original)
        del changed["pegouts"]["query"]["pegout_lbtc_limit"]
        with self.assertRaises(TraceError):
            pegout_csv_rows(changed, state)

    def test_csv_exports_append_exact_cutoff_provenance_for_every_selected_row(self):
        state = graph_state((("a:0", "b"), ("b:0", "c")), seeds=("a:0",))
        endpoint(state, "b", 60_000_001)
        endpoint(state, "c", 60_000_001)
        result = graph(state)
        expected = dict(zip(PEGOUT_LIMIT_FIELDS, ("1", "1.20000002", "0.20000002", "limit_reached")))
        paths, endpoints = pegout_csv_rows(result, state)
        tables = endpoint_table_rows(result, state)
        for row in paths + endpoints + tables:
            self.assertEqual({key: row[key] for key in PEGOUT_LIMIT_FIELDS}, expected)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_pegout_csvs(directory, result, state)
            write_endpoint_table_csv(directory / "endpoints.csv", result, state)
            for filename, fields in (("path-transactions.csv", PATH_FIELDS), ("trace-endpoints.csv", ENDPOINT_FIELDS),
                                     ("endpoints.csv", ENDPOINT_TABLE_FIELDS)):
                with (directory / filename).open(newline="") as stream:
                    reader = csv.DictReader(stream)
                    self.assertEqual(reader.fieldnames, list(fields))
                    self.assertEqual(reader.fieldnames[-4:], list(PEGOUT_LIMIT_FIELDS))
                    rows = list(reader)
                self.assertTrue(rows)
                for row in rows:
                    self.assertEqual({key: row[key] for key in PEGOUT_LIMIT_FIELDS}, expected)

    def test_csv_provenance_is_blank_without_limit_and_explicit_when_unreached(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        endpoint(state, "b", 50_000_000)
        for limit, expected in ((None, ("", "", "", "")), ("1", ("1", "0.5", "0", "paths_exhausted"))):
            result = graph(state, limit)
            paths, endpoints = pegout_csv_rows(result, state)
            for row in paths + endpoints + endpoint_table_rows(result, state):
                self.assertEqual(tuple(row[key] for key in PEGOUT_LIMIT_FIELDS), expected)

    def test_large_unreached_limit_matches_existing_paths_on_random_dags(self):
        rng = random.Random(103)
        for iteration in range(35):
            names = list("abcdef")
            links, counts = [], {name: 0 for name in names}
            for left, name in enumerate(names):
                for child in names[left + 1:]:
                    if rng.random() < .27:
                        links.append((f"{name}:{counts[name]}", child))
                        counts[name] += 1
            seeds = ("a:0", "b:0")
            state = graph_state(links, seeds=seeds)
            for name in names:
                if tx(name) in state["transactions"]:
                    endpoint(state, name, 1)
            lower, upper = rng.randrange(3), rng.randrange(3, 6)
            query = validate_query(seeds=state["seeds"], min_hops=lower, max_hops=upper)
            expected = _paths(state, query)
            actual = _paths(state, {**query, "pegout_lbtc_limit": "100"})
            self.assertEqual(actual, expected, iteration)
            state["hop_reference_name"] = "Perp"
            state["labels"] = [annotation(stop=False, name="Perp", address="SYNTHETIC-" + name + "-address")
                               for name in names if rng.random() < .5]
            expected = _paths(state, query)
            actual = _paths(state, {**query, "pegout_lbtc_limit": "100"})
            self.assertEqual(actual, expected, (iteration, "named"))


if __name__ == "__main__":
    unittest.main()
