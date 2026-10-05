"""Pre-layout scope baselines use exact indexed evidence without changing it."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import csv
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from liquid_tracer.common import LBTC, StopRun, TraceError, digest, read_json, save_json
from liquid_tracer.investigations import create_investigation
from liquid_tracer.scope_analysis import (FILES, _analyse_index, analyze_scope, list_analyses,
                                         read_analysis, verified_analysis_file)
from liquid_tracer.services import set_service
from tests.test_attribution_convergence import graph_state, tx
from tests.test_pegout_paths import add_pegout, add_unspendable, mark_unspent
from tests.test_snapshot_query import MemoryIndex, point


class AnalysisIndex(MemoryIndex):
    def __init__(self, state, forbidden=()):
        super().__init__(state, forbidden)
        self.metadata.update(run_id="0123456789abcdef", limits={"max_hops": 15})
        self.manifest_sha256 = "a" * 64

    def observation(self, identity):
        return {"id": identity, "fetched_at": "2026-09-20T01:02:03+00:00"}


class ScopeAnalysisTests(unittest.TestCase):
    def analyze(self, state, maximum=3, labels=None, forbidden=()):
        index = AnalysisIndex(state, forbidden)
        before = deepcopy(index.state)
        result = _analyse_index(index, state["seeds"], state["labels"] if labels is None else labels, maximum)
        self.assertEqual(index.state, before)
        return result, index

    def test_depth_comparison_deduplicates_split_join_and_skips_later_bodies(self):
        state = graph_state((("a:0", "b"), ("a:1", "c"), ("b:0", "d"), ("c:0", "d"),
                             ("d:0", "e"), ("e:0", "f")), seeds=("a:0", "a:1"))
        endpoint = add_pegout(state, tx("d"))
        state["transactions"][tx("d")]["data"]["vout"][-1]["value"] = 123_456_789
        ((rows, frontier), index) = self.analyze(state, 2, forbidden={tx("e"), tx("f")})
        self.assertEqual([row["max_hops"] for row in rows], [0, 1, 2])
        self.assertEqual([row["transaction_count"] for row in rows], [1, 3, 4])
        self.assertEqual(rows[-1]["pegout_count"], 1)
        self.assertEqual(rows[-1]["pegout_lbtc"], "1.23456789")
        self.assertEqual(rows[-1]["pegout_base_units"], "123456789")
        self.assertEqual(frontier, [{"outpoint": point("d"), "txid": tx("d"), "vout": 0, "hop": 2,
            "reason": "analysis_hop_limit", "address": "SYNTHETIC-d-address", "saved_continuation": True,
            "spending_txid": tx("e"), "observation_time": "", "spend_observation_id": "",
            "observation_status": "spent"}])
        self.assertNotIn(endpoint, {row["outpoint"] for row in frontier})
        self.assertEqual(len(index.fetched), len(set(index.fetched)))

    def test_seed_siblings_and_reused_addresses_cannot_manufacture_reachability(self):
        state = graph_state((("a:0", "b"), ("a:1", "c"), ("c:0", "d")), seeds=("a:0",))
        for record in state["transactions"].values():
            for output in record["data"]["vout"]:
                output["scriptpubkey_address"] = "SYNTHETIC-common-address"
            for vin in record["data"]["vin"]:
                vin["prevout"]["scriptpubkey_address"] = "SYNTHETIC-common-address"
        ((rows, _), _) = self.analyze(state, forbidden={tx("c"), tx("d")})
        self.assertEqual(rows[-1]["transaction_count"], 2)
        self.assertEqual(rows[-1]["address_count"], 1)

    def test_global_saved_spender_overrides_old_unspent_at_boundary(self):
        state = graph_state((("a:0", "b"),), raw_links=(("b:0", "c"),), seeds=("a:0",))
        mark_unspent(state, point("b"))
        ((rows, frontier), _) = self.analyze(state, 1, forbidden={tx("c")})
        self.assertEqual(rows[-1]["unspent_count"], 0)
        self.assertEqual(frontier[0]["reason"], "analysis_hop_limit")
        self.assertTrue(frontier[0]["saved_continuation"])
        self.assertEqual(frontier[0]["observation_time"], "2026-09-20T01:02:03+00:00")

    def test_unknown_spend_missing_body_and_true_unspent_are_distinct(self):
        state = graph_state((("a:0", "b"), ("a:1", "c"), ("a:2", "d")),
                            seeds=("a:0", "a:1", "a:2"))
        mark_unspent(state, point("b"))
        state["outputs"][point("c")].update(status="not_observed")
        state["outputs"][point("d")].update(status="spent", observed_spend={"spent": True, "txid": tx("f")},
                                             spend_observation_id=8)
        ((rows, frontier), _) = self.analyze(state, 1)
        self.assertEqual(rows[-1]["unspent_count"], 1)
        self.assertEqual(rows[-1]["data_gap_count"], 2)
        self.assertEqual({row["reason"] for row in frontier}, {"unobserved_outspend", "missing_spending_transaction"})
        self.assertTrue(all(not row["saved_continuation"] for row in frontier))

    def test_stop_rules_and_attribution_budget_end_before_descendant_payload_reads(self):
        state = graph_state((("a:0", "b"), ("b:0", "c"), ("c:0", "d")), seeds=("a:0",))
        for annotation, reason in (({"stop": True}, "explicit_stop"), ({"hop_limit": 0}, "attribution_hop_limit")):
            with self.subTest(annotation=annotation):
                labels = [{"kind": "address", "value": "SYNTHETIC-b-address", **annotation}]
                ((rows, frontier), _) = self.analyze(state, 3, labels=labels, forbidden={tx("c"), tx("d")})
                self.assertEqual(rows[-1]["transaction_count"], 2)
                self.assertEqual(rows[-1]["stopped_count"], 1)
                self.assertEqual(frontier[0]["reason"], reason)
                self.assertTrue(frontier[0]["saved_continuation"])

    def test_independent_longer_unrestricted_path_keeps_its_own_depth_after_join(self):
        state = graph_state((("a:0", "d"), ("b:0", "c"), ("c:0", "d"), ("d:0", "e")),
                            seeds=("a:0", "b:0"))
        labels = [{"kind": "address", "value": "SYNTHETIC-a-address", "hop_limit": 1}]
        ((rows, frontier), _) = self.analyze(state, 3, labels=labels)
        self.assertEqual([row["transaction_count"] for row in rows], [4, 4, 5])
        self.assertEqual(rows[0]["stopped_count"], 1)
        self.assertEqual(rows[1]["frontier_count"], 1)
        self.assertEqual(frontier[0]["outpoint"], point("e"))
        self.assertEqual(frontier[0]["hop"], 3)

    def test_named_groups_never_reset_baseline_depth(self):
        state = graph_state((("a:0", "b"), ("b:0", "c")), seeds=("a:0",))
        state["hop_reference_name"] = "Perp"
        labels = [{"kind": "address", "value": "SYNTHETIC-b-address", "entity": "Perp"}]
        ((rows, frontier), _) = self.analyze(state, 1, labels=labels, forbidden={tx("c")})
        self.assertEqual(rows[-1]["transaction_count"], 2)
        self.assertEqual(frontier[0]["hop"], 1)

    def test_endpoint_totals_exclude_fees_and_unknown_amounts_are_not_zero(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        add_pegout(state, tx("b"))
        add_pegout(state, tx("b"))
        add_pegout(state, tx("b"))
        add_unspendable(state, tx("b"))
        outputs = state["transactions"][tx("b")]["data"]["vout"]
        outputs[1].pop("value")
        outputs[2].pop("asset")
        outputs[3]["asset"] = "f" * 64
        outputs.append({"scriptpubkey": "", "scriptpubkey_type": "fee", "asset": LBTC, "value": 999999})
        ((rows, _), _) = self.analyze(state)
        last = rows[-1]
        self.assertEqual(last["pegout_count"], 3)
        self.assertEqual(last["pegout_lbtc"], "0")
        self.assertEqual(last["unknown_pegout_amount_count"], 1)
        self.assertEqual(last["unknown_pegout_asset_count"], 1)
        self.assertEqual(last["non_lbtc_pegout_count"], 1)
        self.assertEqual(last["unspendable_count"], 1)
        self.assertEqual(last["output_count"], 6)

    def test_zero_and_one_hop_comparisons_are_unique(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        for maximum in (0, 1):
            ((rows, _), _) = self.analyze(state, maximum)
            self.assertEqual([row["max_hops"] for row in rows], list(range(maximum + 1)))


class SavedScopeAnalysisTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.case = create_investigation(Path(temporary.name), "Scope", seeds=[point("a")])
        state = graph_state((("a:0", "b"), ("b:0", "c")), seeds=("a:0",))
        self.index = AnalysisIndex(state)
        self.index.metadata["case_id"] = read_json(self.case / "case.json")["case_id"]
        @contextmanager
        def open_index(*args, **kwargs):
            yield self.index
        self.patch = patch("liquid_tracer.scope_analysis._index", open_index)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def generate(self, maximum=1, **kwargs):
        return analyze_scope(self.case, "0123456789abcdef", max_hops=maximum, **kwargs)

    def test_snapshot_cache_reuses_work_and_exports_are_immutable_and_verified(self):
        before = deepcopy(self.index.state)
        with patch("liquid_tracer.export.build_graph", side_effect=AssertionError("Graph construction is not scope analysis")):
            first = self.generate()
            self.index.fetched.clear()
            second = self.generate()
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["analysis_id"], second["analysis_id"])
        self.assertEqual(first["analysis_number"], 1)
        self.assertEqual(self.index.fetched, [])
        self.assertEqual(self.index.state, before)
        self.assertEqual(first["source"]["collection_max_hops"], 15)
        self.assertEqual(set(p.name for p in (self.case / "analyses" / first["analysis_id"]).iterdir()), FILES)
        for name in FILES:
            self.assertTrue(verified_analysis_file(self.case, first["analysis_id"], name).is_file())
        self.assertEqual(read_analysis(self.case, first["analysis_id"])["frontier_count"], 1)
        self.assertNotIn("frontier", list_analyses(self.case)[0])

    def test_new_scope_controls_and_source_fingerprint_create_distinct_numbered_analyses(self):
        first = self.generate()
        second = self.generate(2)
        set_service(self.case, "SYNTHETIC-b-address", name="Boundary", stop_tracing=True)
        third = self.generate(2)
        self.index.manifest_sha256 = "b" * 64
        fourth = self.generate(2)
        self.assertEqual([row["analysis_number"] for row in (first, second, third, fourth)], [1, 2, 3, 4])
        self.assertEqual(len({row["analysis_id"] for row in (first, second, third, fourth)}), 4)
        self.assertEqual(third["frontier"][0]["reason"], "explicit_stop")
        self.assertEqual([row["analysis_number"] for row in list_analyses(self.case)], [4, 3, 2, 1])

    def test_cancellation_publishes_no_partial_analysis(self):
        with self.assertRaises(StopRun):
            self.generate(cancel=lambda: True)
        self.assertEqual(list_analyses(self.case), [])
        calls = 0
        def cancelled():
            nonlocal calls
            calls += 1
            return calls >= 9
        with self.assertRaises(StopRun):
            self.generate(cancel=cancelled)
        self.assertEqual(list_analyses(self.case), [])
        self.assertFalse(any(p.name.startswith(".analysis-") for p in (self.case / "analyses").iterdir()))

    def test_concurrent_identical_queries_publish_once(self):
        barrier = threading.Barrier(2)
        from liquid_tracer.scope_analysis import _analyse_index as original
        def wait(*args, **kwargs):
            barrier.wait(timeout=10)
            return original(*args, **kwargs)
        with patch("liquid_tracer.scope_analysis._analyse_index", wait):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: self.generate(), range(2)))
        self.assertEqual(len({row["analysis_id"] for row in results}), 1)
        self.assertEqual(sorted(row["cache_hit"] for row in results), [False, True])
        self.assertEqual(len(list_analyses(self.case)), 1)

    def test_bounded_summary_does_not_truncate_csv_and_csv_escapes_formula_text(self):
        state = graph_state(seeds=("a:0",))
        state["transactions"][tx("a")]["data"]["vout"] *= 150
        state["seeds"] = [point("a", i) for i in range(150)]
        for output in state["transactions"][tx("a")]["data"]["vout"]:
            output["scriptpubkey_address"] = "=formula"
        self.index = AnalysisIndex(state)
        self.index.metadata["case_id"] = read_json(self.case / "case.json")["case_id"]
        result = self.generate(0)
        self.assertEqual(result["frontier_count"], 150)
        self.assertEqual(len(result["frontier"]), 100)
        path = verified_analysis_file(self.case, result["analysis_id"], "frontiers.csv")
        with path.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 150)
        self.assertEqual(rows[0]["address"], "'=formula")

    def test_corrupt_exports_and_unsafe_paths_fail_closed(self):
        result = self.generate()
        directory = self.case / "analyses" / result["analysis_id"]
        (directory / "frontiers.csv").write_text("altered", encoding="utf-8")
        with self.assertRaisesRegex(TraceError, "checksum"):
            self.generate()
        with self.assertRaisesRegex(TraceError, "checksum"):
            verified_analysis_file(self.case, result["analysis_id"], "frontiers.csv")
        with self.assertRaises(TraceError):
            verified_analysis_file(self.case, result["analysis_id"], "../case.json")
        with self.assertRaises(TraceError):
            read_analysis(self.case, "../outside")

    def test_invalid_hops_and_source_are_rejected_before_loading(self):
        for maximum in (True, -1, 1.5, "10"):
            with self.subTest(maximum=maximum), self.assertRaises(TraceError):
                self.generate(maximum)
        with self.assertRaises(TraceError):
            self.generate(data_source="remote")
        self.assertEqual(self.index.fetched, [])

    def test_oversized_summary_fails_before_streaming_hash_or_parse(self):
        result = self.generate()
        directory = self.case / "analyses" / result["analysis_id"]
        with (directory / "analysis.json").open("wb") as stream:
            stream.truncate(5 * 1024 * 1024)
        with patch("liquid_tracer.scope_analysis._hash", side_effect=AssertionError("unbounded summary hash")):
            with self.assertRaisesRegex(TraceError, "too large"):
                read_analysis(self.case, result["analysis_id"])
        self.assertEqual(list_analyses(self.case), [])

    def test_real_index_and_legacy_archive_preserve_source_and_do_not_construct_graphs(self):
        from tests.test_connections import saved_case
        self.patch.stop()
        state = graph_state((("a:0", "b"), ("b:0", "c")), seeds=("a:0",))
        _, archive = saved_case(self.case, state)
        with patch("liquid_tracer.export.build_graph", side_effect=AssertionError("graph generation")):
            legacy = self.generate()
        self.assertFalse(legacy["source"]["indexed_source"])
        save_json(archive / "evidence-index.json", [])
        manifest = archive / "SHA256SUMS"
        files = sorted(path for path in archive.iterdir() if path.is_file() and path != manifest)
        manifest.write_text("".join(f"{digest(path.read_bytes())}  {path.name}\n" for path in files))
        before = {path.name: path.read_bytes() for path in archive.iterdir() if path.is_file()}
        with patch("liquid_tracer.export.build_graph", side_effect=AssertionError("graph generation")):
            indexed = self.generate()
            with patch("liquid_tracer.cli.verify_export", side_effect=AssertionError("warm full verification")):
                cached = self.generate()
        self.assertTrue(indexed["source"]["indexed_source"])
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(legacy["comparisons"], indexed["comparisons"])
        self.assertEqual(before, {path.name: path.read_bytes() for path in archive.iterdir() if path.is_file()})
        (archive / "evidence-index.json").unlink()
        with self.assertRaisesRegex(TraceError, "Missing"):
            self.generate()


if __name__ == "__main__":
    unittest.main()
