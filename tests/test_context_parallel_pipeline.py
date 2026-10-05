"""Repeated address inputs use fewer display ports, keeping complete evidence."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import LBTC, canonical
from liquid_tracer.context_connectors import display_graph, prepare, summaries
from liquid_tracer.context_groups import group_context_inputs
from liquid_tracer.elk_layout import _request_graph, optimize_graph
from liquid_tracer.export import build_graph, svg_graph
from liquid_tracer.layout_preview import export_layout, render_svg
from liquid_tracer.layout_reuse import reusable_elk_preview
from liquid_tracer.mermaid import mermaid_source
from liquid_tracer.miro import make_plan, validate_plan
from liquid_tracer.pegout_csv import pegout_csv_rows
from liquid_tracer.pegout_paths import pegout_graph, validate_query
from liquid_tracer.transaction_csv import transaction_csv_rows
from tests.test_attribution_convergence import graph_state, tx
from tests.test_context_connector_integration import evidence, svg_edge_ids
from tests.test_elk_layout import synthetic_candidate
from tests.test_input_order import child_input, input_order_state
from tests.test_layout import txid
from tests.test_pegout_paths import add_pegout, add_unspendable, mark_unspent, set_address


def shared_inputs(count=201, traced=1):
    continuing = tuple(range(count - traced, count))
    state = input_order_state(count, continuing=continuing)
    state.update(run_id="1234567890abcdef", ancestor_runs=[])
    child = state["transactions"][txid("input-order-child")]["data"]
    for index, vin in enumerate(child["vin"]):
        vin["prevout"]["scriptpubkey_address"] = "SYNTHETIC-shared-address"
        if index in continuing:
            state["transactions"][vin["txid"]]["data"]["vout"][0]["scriptpubkey_address"] = "SYNTHETIC-shared-address"
    child["vin"][0]["prevout"].update(value=123456789, asset=LBTC)
    plain = build_graph(state)
    for edge in plain["edges"]:
        if edge["id"] in {child_input(index) for index in continuing}:
            edge["role"] = "traced_input"
    return state, plain, group_context_inputs(plain, enabled=True)


class ParallelContextPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict("os.environ", {"XDG_CACHE_HOME": temporary, "XDG_STATE_HOME": temporary}))

    def layout(self, graph):
        requests = []
        def worker(request, seeds, **kwargs):
            requests.append(deepcopy(request))
            ordered = deepcopy(request)
            ordered["children"].sort(key=lambda node: (int(node["layoutOptions"]["elk.partitioning.partition"]), node["id"]))
            return synthetic_candidate(ordered, seeds, **kwargs)
        with patch("liquid_tracer.elk_layout._worker", side_effect=worker), \
             patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("No data collection")):
            result = optimize_graph(graph, "straight", layout_attempts=1)
        return result, requests

    def test_201_parallel_inputs_become_bundle_and_individual_traced_inputs(self):
        for traced in (1, 2):
            with self.subTest(traced=traced):
                state, plain, grouped = shared_inputs(traced=traced)
                before = canonical((state, plain, grouped))
                bundle, = summaries(grouped)
                result, requests = self.layout(grouped)
                displayed = display_graph(result)
                target_edges = [edge for edge in displayed["edges"] if edge["target"] == bundle["target"]]
                self.assertEqual(len(target_edges), traced + 1)
                self.assertEqual(len(bundle["details"]["context_summary"]["member_edge_ids"]), 201 - traced)
                self.assertEqual(len(requests), 1)
                self.assertEqual(len(requests[0]["edges"]), len(displayed["edges"]))
                self.assertEqual({node["id"] for node in result["nodes"]}, {node["id"] for node in plain["nodes"]})
                self.assertEqual(evidence(result), evidence(plain))
                self.assertEqual(transaction_csv_rows(result, state), transaction_csv_rows(plain, state))
                self.assertEqual(canonical((state, plain, grouped)), before)

    def test_visible_links_match_svg_mermaid_and_miro_without_losing_address(self):
        _, plain, grouped = shared_inputs(count=6, traced=2)
        result, _ = self.layout(grouped)
        visible = display_graph(result)
        keys = {edge["id"] for edge in visible["edges"]}
        self.assertEqual(svg_edge_ids(render_svg(result)), keys)
        self.assertEqual(svg_edge_ids(svg_graph(result)), keys)
        source = mermaid_source(result)
        bundle, = summaries(result)
        self.assertIn(bundle["label"], source)
        self.assertIn("vin 4", source)
        self.assertIn("vin 5", source)
        plan = make_plan(result)
        validate_plan(plan)
        self.assertEqual(set(plan["context_parallel_items"]), {bundle["id"]})
        self.assertEqual(set(plan["context_parallel_items"][bundle["id"]]["inputs"]),
                         set(bundle["details"]["context_summary"]["member_edge_ids"]))
        self.assertEqual({item["key"] for item in plan["connectors"]}, keys)
        self.assertEqual({item["key"] for item in plan["shapes"] if item["key"] != "legend"}, {node["id"] for node in plain["nodes"]})

    def test_local_details_list_each_bundled_input_with_escaped_label_and_exact_value(self):
        state, plain, _ = shared_inputs(count=6)
        edge = next(edge for edge in plain["edges"] if edge["id"] == child_input(0))
        edge["label"] = "vin 0 <script>unsafe</script>"
        graph = group_context_inputs(plain, enabled=True)
        result, _ = self.layout(graph)
        with tempfile.TemporaryDirectory() as temporary:
            paths = export_layout(result, Path(temporary) / "preview")
            text = Path(paths["details"]).read_text()
            self.assertIn("Only context inputs between these two objects are bundled", text)
            self.assertIn("&lt;script&gt;unsafe&lt;/script&gt;", text)
            self.assertNotIn("<script>", text)
            bundle, = summaries(result)
            by_id = {edge["id"]: edge for edge in plain["edges"]}
            for key in bundle["details"]["context_summary"]["member_edge_ids"]:
                self.assertIn(key, text)
                self.assertIn(by_id[key]["outpoint"], text)
            self.assertEqual(evidence(json.loads(Path(paths["graph"]).read_text())), evidence(plain))
            self.assertEqual(transaction_csv_rows(result, state), transaction_csv_rows(plain, state))

    def test_reuse_keeps_bundles_and_legacy_snapshot_does_not_satisfy_new_request(self):
        _, _, graph = shared_inputs(count=6)
        result, _ = self.layout(graph)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / (graph["run_id"] + "-elk-12345678")
            export_layout(result, directory)
            self.assertEqual(reusable_elk_preview(graph, Path(temporary), "straight", layout_attempts=1), result)
            legacy = deepcopy(graph)
            legacy["context_connectors"] = {"version": 1, "summaries": []}
            self.assertIsNone(reusable_elk_preview(legacy, Path(temporary), "straight", layout_attempts=1))
            self.assertIs(display_graph(legacy), legacy)
            self.assertEqual(prepare(legacy), legacy)

    def test_pegout_paths_and_endpoint_csvs_survive_parallel_context_projection(self):
        state = graph_state((("a:0", "b"), ("b:0", "c")),
                            raw_links=(("d:0", "b"), ("e:0", "b"),
                                       ("f:0", "b")), seeds=("a:0",))
        for key in ("a:0", "d:0", "e:0", "f:0"):
            set_address(state, tx(key.split(":")[0]) + ":0", "SYNTHETIC-shared-address")
        add_pegout(state, tx("c"))
        add_unspendable(state, tx("c"))
        mark_unspent(state, tx("c") + ":0")
        query = validate_query(seeds=[tx("a") + ":0"], max_hops=10,
                               include_unspent=True, include_unspendable=True, transaction_io="complete")
        plain = pegout_graph(state, query)
        graph = group_context_inputs(plain, enabled=True)
        self.assertTrue(any(s["details"]["context_summary"].get("kind") == "parallel" for s in summaries(graph)))
        result, _ = self.layout(graph)
        self.assertEqual(result["pegouts"], plain["pegouts"])
        self.assertEqual(result["branch_structure"], plain["branch_structure"])
        self.assertEqual(pegout_csv_rows(result, state), pegout_csv_rows(plain, state))
        self.assertEqual(transaction_csv_rows(result, state), transaction_csv_rows(plain, state))
        self.assertEqual(evidence(result), evidence(plain))


if __name__ == "__main__":
    unittest.main()
