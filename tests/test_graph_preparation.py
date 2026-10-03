"""Small synthetic preparation tests; no ELK process or investigation data."""

import copy
import os
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from liquid_tracer.common import TraceError, match_labels
from liquid_tracer.connections import connection_graph
from liquid_tracer.elk_layout import _request_graph, optimize_graph
from liquid_tracer.export import build_graph
from liquid_tracer.graph_preparation import LabelLookup, copy_for_geometry
from liquid_tracer.layout_reuse import _fingerprint
from liquid_tracer.pegout_paths import pegout_graph, validate_query
from liquid_tracer.trace_layout import trace_structure
from tests.test_attribution_convergence import annotation, graph_state, tx
from tests.test_elk_layout import synthetic_candidate
from tests.test_pegout_paths import add_pegout
from tests.test_trace_hub_flow import return_graph
from tests.test_trace_layout import fixture
from tests.test_trace_sections import sections


class GraphPreparationTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"XDG_CACHE_HOME": directory,
                                                  "XDG_STATE_HOME": directory,
                                                  "LIQUID_RENDER_HEAP_MB": "512"}))

    def test_label_index_preserves_order_duplicates_and_missing_fields(self):
        labels = [dict(kind="script", value="script"), dict(kind="address", value="A"),
                  dict(kind="outpoint", value="tx:0"), dict(kind="address", value="A"),
                  dict(kind="address", value=None)]
        lookup = LabelLookup(labels)
        for key, output in (("tx:0", {"scriptpubkey_address": "A", "scriptpubkey": "script"}),
                            ("tx:1", {}), ("tx:2", {"scriptpubkey_address": "B"})):
            self.assertEqual(lookup(key, output), match_labels(labels, key, output))
        self.assertIsNot(lookup("tx:0", {}), lookup("tx:0", {}))

    def test_label_index_reads_the_label_collection_once(self):
        class CountedList(list):
            reads = 0
            def __iter__(self):
                self.reads += 1
                return super().__iter__()
        labels = CountedList({"kind": "address", "value": str(i)} for i in range(2000))
        lookup = LabelLookup(labels)
        for i in range(100):
            self.assertEqual(lookup(f"tx:{i}", {"scriptpubkey_address": str(i)}), [labels[i]])
        self.assertEqual(labels.reads, 1)

    def test_indexed_graph_is_identical_to_legacy_label_matching(self):
        labels = [annotation(stop=False), annotation(name="Second", address="SYNTHETIC-b-address", stop=False)]
        state = graph_state((("a:0", "c"), ("c:0", "b")), labels=labels)
        before = copy.deepcopy(state)
        indexed = build_graph(state)
        with patch("liquid_tracer.export.LabelLookup", side_effect=lambda values: lambda key, output: match_labels(values, key, output)):
            reference = build_graph(state)
        self.assertEqual(indexed, reference)
        self.assertEqual(state, before)

    def test_optional_initial_layout_keeps_semantics_and_worker_request(self):
        state = graph_state((("a:0", "c"), ("a:1", "d"), ("c:0", "b"), ("d:0", "b")))
        baseline = build_graph(state, include_fees=True)
        with patch("liquid_tracer.layout._pack", side_effect=AssertionError("unnecessary sweep")):
            prepared = build_graph(state, include_fees=True, initial_layout=False)
        self.assertEqual(_fingerprint(baseline), _fingerprint(prepared))
        self.assertEqual(_request_graph(baseline), _request_graph(prepared))
        self.assertEqual(build_graph(state, initial_layout=True), build_graph(state))
        for node in prepared["nodes"]:
            self.assertGreater(node["width"], 0)
            self.assertGreater(node["height"], 0)
        for value in (None, 0, 1, "false"):
            with self.subTest(value=value), self.assertRaises(TraceError):
                build_graph(state, initial_layout=value)

    def test_connection_and_pegout_builders_skip_both_refinement_passes(self):
        state = graph_state((("a:0", "c"), ("c:0", "b")))
        add_pegout(state, tx("b"))
        query = validate_query(tx("a"))
        for builder in (lambda **options: connection_graph(state, 10, **options),
                        lambda **options: pegout_graph(state, query, **options)):
            ordinary = builder()
            with patch("liquid_tracer.layout._pack", side_effect=AssertionError("unnecessary sweep")):
                prepared = builder(initial_layout=False)
            self.assertEqual(_fingerprint(ordinary), _fingerprint(prepared))
            self.assertEqual(_request_graph(ordinary), _request_graph(prepared))

    def test_grouped_context_retains_historical_member_coordinates(self):
        from tests.test_input_order import input_order_state
        state = input_order_state(4, continuing=(3,))
        ordinary = build_graph(state, group_context_inputs=True)
        prepared = build_graph(state, group_context_inputs=True, initial_layout=False)
        self.assertEqual(prepared, ordinary)
        self.assertTrue(any(node["kind"] == "context_group" for node in prepared["nodes"]))

    def test_coordinate_trial_copies_all_mutable_presentation_state(self):
        graph = {"nodes": [{"id": "a", "x": 1, "details": {"transaction": {"vin": [1]}}}],
                 "edges": [{"id": "e", "details": {"vin": {"vout": 0}},
                            "route": [{"x": 1, "y": 2}], "attachment": {"startItem": {"position": {"x": "50%"}}}}],
                 "layout": {"annotations": {"legend": {"x": 2}}}, "graph_options": {"a": [1]}}
        original = copy.deepcopy(graph)
        trial = copy_for_geometry(graph)
        self.assertEqual(trial, graph)
        self.assertIs(trial["nodes"][0]["details"], graph["nodes"][0]["details"])
        self.assertIs(trial["edges"][0]["details"], graph["edges"][0]["details"])
        trial["nodes"][0]["x"] = 10
        trial["edges"][0]["route"][0]["x"] = 20
        trial["edges"][0]["attachment"]["startItem"]["position"]["x"] = "90%"
        trial["layout"]["annotations"]["legend"]["x"] = 30
        trial["graph_options"]["a"].append(2)
        self.assertEqual(graph, original)

    def optimized(self, graph, *, cached=True):
        import liquid_tracer.elk_layout as elk
        import liquid_tracer.trace_sections as section_module
        with ExitStack() as stack:
            stack.enter_context(patch("liquid_tracer.trace_sections.MIN_SECTION_NODES", 1))
            stack.enter_context(patch("liquid_tracer.trace_sections.iter_sections", side_effect=sections))
            stack.enter_context(patch("liquid_tracer.elk_layout._worker", side_effect=synthetic_candidate))
            if not cached:
                request, metrics, candidates = elk._request_graph, elk.trace_metrics, section_module.iter_candidates
                stack.enter_context(patch("liquid_tracer.elk_layout._request_graph", side_effect=lambda graph, **kw: request(graph)))
                stack.enter_context(patch("liquid_tracer.elk_layout.trace_metrics", side_effect=lambda graph, **kw: metrics(graph)))
                stack.enter_context(patch("liquid_tracer.trace_sections.iter_candidates", side_effect=lambda *args, **kw: candidates(*args)))
                stack.enter_context(patch("liquid_tracer.elk_layout.copy_for_geometry", side_effect=copy.deepcopy))
            return optimize_graph(graph, "elbowed", layout_attempts=2)

    def test_reused_structure_matches_fresh_analysis_and_result_is_detached(self):
        for graph in (fixture()[0], return_graph()):
            with self.subTest(hub=bool(graph["graph_options"].get("hub_addresses"))):
                original = copy.deepcopy(graph)
                reference = self.optimized(graph, cached=False)
                with patch("liquid_tracer.elk_layout.trace_structure", wraps=trace_structure) as top_level, \
                     patch("liquid_tracer.trace_sections.trace_structure", side_effect=AssertionError("repeated planning")), \
                     patch("liquid_tracer.trace_layout.trace_structure", side_effect=AssertionError("repeated metric topology")):
                    result = self.optimized(graph)
                self.assertEqual(top_level.call_count, 1)
                self.assertEqual(result, reference)
                self.assertEqual(graph, original)
                result["nodes"][0]["details"]["changed"] = True
                result["nodes"][0]["x"] = -1000
                result["edges"][0]["details"] = {"changed": True}
                self.assertEqual(graph, original)

    def test_standard_request_accepts_reused_disabled_structure(self):
        graph = fixture()[0]
        graph["graph_options"]["layout_style"] = "standard"
        self.assertEqual(_request_graph(graph), _request_graph(graph, structure=trace_structure(graph)))


if __name__ == "__main__":
    unittest.main()
