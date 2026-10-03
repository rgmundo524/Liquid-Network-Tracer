"""Small, synthetic regression graphs; no case data or production layouts."""
import copy
import os
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.elk_errors import ElkWorkerFailure
from liquid_tracer.elk_layout import _request_graph, _worker, optimize_graph
from liquid_tracer.trace_sections import (MAX_SECTION_NODES, MAX_SECTION_PORTS, SECTION_LAYOUT_VERSION, _validate_local,
                                         enabled, iter_candidates, plan)
from tests.test_elk_layout import HAS_ELK, synthetic_candidate
from tests.test_trace_layout import fixture


def sections(requests, seed, worker, progress, metadata):
    count = max(1, min(3, len(requests)))
    metadata.update(execution="parallel" if count > 1 else "sequential", worker_count=count, memory_retry_count=0)
    for index, request in enumerate(requests, 1):
        yield index, worker(request, [seed])


class TraceSectionsTests(unittest.TestCase):
    def test_only_large_trace_graphs_use_sections(self):
        graph, _, _ = fixture()
        request, _, _ = _request_graph(graph)
        self.assertFalse(enabled(graph, request))
        with patch('liquid_tracer.trace_sections.MIN_SECTION_NODES', 1):
            self.assertTrue(enabled(graph, request))
            graph['graph_options']['layout_style'] = 'standard'
            self.assertFalse(enabled(graph, request))

    def test_partition_preserves_shared_identity_and_bounds_dense_sections(self):
        graph, node, edge = fixture()
        node('shared', 'address', 2)
        for i in range(160):
            node(f'extra{i}', 'transaction', 4)
            edge(f'shared{i}', 'shared', f'extra{i}', f'out:{i}')
        request, _, _ = _request_graph(graph)
        original = copy.deepcopy(request)
        groups, requests, backbone = plan(graph, request)
        keys = [key for group in groups for key in group]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(set(keys), {node['id'] for node in graph['nodes']})
        self.assertEqual(sum('shared' in group for group in groups), 1)
        self.assertIn('root', backbone)
        for item in requests:
            self.assertLessEqual(len(item['children']), MAX_SECTION_NODES)
            if len(item['children']) > 1:
                self.assertLessEqual(sum(len(n['ports']) for n in item['children']), MAX_SECTION_PORTS)
            self.assertTrue(item['sectionLayout'])
        self.assertEqual(request, original)

    def test_long_backbone_is_bounded_and_needs_no_worker(self):
        graph = {"nodes": [], "edges": [], "graph_options": {"layout_style": "trace"}, "fee_items": {}}
        def node(key, kind, column):
            graph["nodes"].append({"id": key, "kind": kind, "column": column,
                                   "x": column * 300, "y": 0, "width": 160, "height": 160,
                                   "details": {}, **({"role": "starting_transaction"} if column == 0 else {})})
        node("t0", "transaction", 0)
        for i in range(350):
            node(f"a{i}", "address", 2*i+1)
            node(f"t{i+1}", "transaction", 2*i+2)
            outpoint = f"t{i}:0"
            graph["edges"].extend([
                {"id": f"o{i}", "source": f"t{i}", "target": f"a{i}", "outpoint": outpoint},
                {"id": f"i{i}", "source": f"a{i}", "target": f"t{i+1}", "outpoint": outpoint,
                 "role": "traced_input"}])
        request, _, _ = _request_graph(graph)
        self.assertTrue(enabled(graph, request))
        groups, requests, backbone = plan(graph, request)
        self.assertGreater(len(groups), 1)
        self.assertEqual(len(backbone), 701)
        self.assertLessEqual(max(map(len, groups)), MAX_SECTION_NODES)
        with patch('liquid_tracer.elk_layout._worker') as worker:
            metadata = {}
            result = list(iter_candidates(graph, request, [1], worker, lambda i,s: lambda e: None, metadata))
        worker.assert_not_called()
        self.assertEqual(metadata['section_worker_count'], 0)
        self.assertEqual(len(result[0][2][0]['nodes']), 701)

    def test_board_alignment_spacers_are_not_published(self):
        from liquid_tracer.output_alignment import update_columns
        graph, _, _ = fixture()
        graph['layout'] = {'output_alignment': update_columns(graph, {n['id'] for n in graph['nodes']})}
        with patch('liquid_tracer.trace_sections.MIN_SECTION_NODES', 1), \
             patch('liquid_tracer.trace_sections.iter_sections', side_effect=sections), \
             patch('liquid_tracer.elk_layout._worker', side_effect=synthetic_candidate):
            result = optimize_graph(graph, 'elbowed', layout_attempts=1)
        self.assertEqual({n['id'] for n in result['nodes']}, {n['id'] for n in graph['nodes']})
        self.assertIn('output_alignment', result['layout'])

    def test_local_validation_rejects_geometry_before_assembly_can_hide_it(self):
        graph, _, _ = fixture()
        request, _, _ = _request_graph(graph)
        good = synthetic_candidate(request, [1])[0]
        _validate_local(request, good)
        for mutate in (lambda c: c['nodes'].pop(), lambda c: c['edges'].pop(),
                       lambda c: c['nodes'].__setitem__(0, None),
                       lambda c: c['edges'].__setitem__(0, None),
                       lambda c: c['nodes'][0].update(ports=None),
                       lambda c: c['nodes'][0].update(x=float('nan')),
                       lambda c: c['nodes'][0]['ports'].pop(),
                       lambda c: c['edges'][0]['sections'][0].update(endPoint={'x': 0, 'y': float('inf')})):
            candidate = copy.deepcopy(good)
            mutate(candidate)
            with self.assertRaises(TraceError):
                _validate_local(request, candidate)

    def test_full_optimization_preserves_evidence_backbone_and_progress(self):
        graph, _, _ = fixture()
        original = copy.deepcopy(graph)
        events = []
        with patch('liquid_tracer.trace_sections.MIN_SECTION_NODES', 1), \
             patch('liquid_tracer.trace_sections.iter_sections', side_effect=sections), \
             patch('liquid_tracer.elk_layout._worker', side_effect=synthetic_candidate) as worker, \
             patch('liquid_tracer.elk_layout.iter_attempts') as monolithic:
            result = optimize_graph(graph, 'elbowed', progress=events.append, layout_attempts=2)
        monolithic.assert_not_called()
        self.assertGreater(worker.call_count, 0)
        self.assertEqual(graph, original)
        self.assertEqual({n['id'] for n in result['nodes']}, {n['id'] for n in graph['nodes']})
        self.assertEqual([(e['id'], e['source'], e['target'], e['outpoint']) for e in result['edges']],
                         [(e['id'], e['source'], e['target'], e['outpoint']) for e in graph['edges']])
        self.assertEqual(result['layout']['search']['section_layout_version'], SECTION_LAYOUT_VERSION)
        self.assertEqual(result['layout']['search']['attempted_count'], 2)
        self.assertEqual(result['layout']['metrics']['after']['node_overlaps'], 0)
        self.assertLess(result['layout']['trace_layout']['spine_alignment'], .01)
        self.assertTrue(any(e.get('stage') == 'section_ready' for e in events))

    def test_one_attempt_can_use_multiple_bounded_section_jobs(self):
        graph, _, _ = fixture()
        request, _, _ = _request_graph(graph)
        calls = []
        def worker(request, seeds):
            calls.append(request)
            return synthetic_candidate(request, seeds)
        metadata = {}
        with patch('liquid_tracer.trace_sections.MAX_SECTION_NODES', 5), \
             patch('liquid_tracer.trace_sections.iter_sections', side_effect=sections):
            result = list(iter_candidates(graph, request, [1], worker, lambda i,s: lambda e: None, metadata))
        self.assertGreater(len(calls), 1)
        self.assertTrue(all(len(call['children']) <= 5 for call in calls))
        self.assertLessEqual(metadata['max_section_nodes'], 5)
        self.assertEqual(metadata['execution'], 'parallel')
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0][2][0]['nodes']), len(graph['nodes']))

    def test_hubs_returns_and_captions_survive_the_complete_pipeline(self):
        from tests.test_trace_hub_flow import mixed_terminal_graph, return_graph
        from liquid_tracer.hub_layout import hub_plan
        for factory in (mixed_terminal_graph, return_graph):
            graph = factory()
            before = copy.deepcopy(graph)
            with patch('liquid_tracer.trace_sections.MIN_SECTION_NODES', 1), \
                 patch('liquid_tracer.trace_sections.iter_sections', side_effect=sections), \
                 patch('liquid_tracer.elk_layout._worker', side_effect=synthetic_candidate):
                result = optimize_graph(graph, 'elbowed', layout_attempts=1)
            self.assertEqual(graph, before)
            nodes = {node['id']: node for node in result['nodes']}
            self.assertEqual(len(nodes), len(graph['nodes']))
            for hub, entry in hub_plan(graph).get('entries', {}).items():
                self.assertGreater(nodes[hub]['x'], nodes[entry]['x'])
            self.assertEqual(result['layout']['metrics']['after']['node_overlaps'], 0)
            self.assertEqual(result['layout']['metrics']['after']['node_intersections'], 0)
            self.assertEqual(result['layout']['metrics']['after']['connector_overlaps'], 0)
            for original, edge in zip(graph['edges'], result['edges']):
                for field in ('id', 'source', 'target', 'outpoint', 'label', 'quantity'):
                    self.assertEqual(original.get(field), edge.get(field))

    def test_failure_never_retries_the_whole_graph(self):
        graph, _, _ = fixture()
        request, _, _ = _request_graph(graph)
        def failed(*args):
            yield 1, ElkWorkerFailure('synthetic failure', failure_code='elk_index_error')
        with patch('liquid_tracer.trace_sections.iter_sections', side_effect=failed):
            result = list(iter_candidates(graph, request, [1], synthetic_candidate, lambda i, s: lambda e: None, {}))
        self.assertIsInstance(result[0][2], ElkWorkerFailure)

    def test_cancellation_closes_scheduler(self):
        graph, _, _ = fixture()
        request, _, _ = _request_graph(graph)
        closed = []
        def scheduled(requests, seed, worker, progress, metadata):
            try:
                yield 1, synthetic_candidate(requests[0], [seed])
            finally:
                closed.append(True)
        def report(event):
            if event['stage'] == 'section_ready':
                raise KeyboardInterrupt()
        with patch('liquid_tracer.trace_sections.iter_sections', side_effect=scheduled):
            with self.assertRaises(KeyboardInterrupt):
                list(iter_candidates(graph, request, [1], synthetic_candidate, lambda i, s: report, {}))
        self.assertEqual(closed, [True])

    @unittest.skipUnless(HAS_ELK, 'local ELK package unavailable')
    def test_real_engine_section_assembly_keeps_the_preferred_backbone(self):
        graph, _, _ = fixture()
        with tempfile.TemporaryDirectory() as cache, \
             patch.dict(os.environ, {'XDG_CACHE_HOME': cache, 'LIQUID_RENDER_HEAP_MB': '512'}), \
             patch('liquid_tracer.trace_sections.MIN_SECTION_NODES', 1):
            result = optimize_graph(graph, 'elbowed', layout_attempts=1)
        self.assertEqual(result['layout']['search']['section_layout_version'], SECTION_LAYOUT_VERSION)
        self.assertEqual(result['layout']['metrics']['after']['node_overlaps'], 0)
        self.assertEqual(result['layout']['metrics']['after']['node_intersections'], 0)
        self.assertLess(result['layout']['trace_layout']['spine_alignment'], .01)

    @unittest.skipUnless(HAS_ELK, 'local ELK package unavailable')
    def test_real_small_section_uses_one_geometry_pass_with_orphan_boundary_ports(self):
        graph, _, _ = fixture()
        request, _, _ = _request_graph(graph)
        _, requests, _ = plan(graph, request)
        section = next(item for item in requests if item['edges'])
        candidates = _worker(section, [1], heap_mb=512)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['inputOrderPolicy'], 'geometry')
        _validate_local(section, candidates[0])


if __name__ == '__main__':
    unittest.main()
