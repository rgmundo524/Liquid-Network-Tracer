"""Projected context geometry survives reuse, fallback and incremental updates."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.board_layout import prepare_graph
from liquid_tracer.common import read_json, save_json
from liquid_tracer.compaction import compact_graph
from liquid_tracer.context_connectors import display_graph
from liquid_tracer.elk_layout import fallback_graph, optimize_graph
from liquid_tracer.export import build_graph
from liquid_tracer.layout_preview import export_layout, render_svg
from liquid_tracer.layout_reuse import _fingerprint, reusable_elk_preview
from liquid_tracer.miro import make_plan, validate_plan
from tests.test_context_connector_integration import evidence
from tests.test_elk_layout import synthetic_candidate
from tests.test_input_order import input_order_state


class ContextConnectorPipelineTests(unittest.TestCase):
    def graph(self):
        state = input_order_state(5, continuing=(4,))
        state.update(run_id='1234567890abcdef', ancestor_runs=[])
        return build_graph(state, group_context_inputs=True)

    def layout(self, graph, style='straight'):
        with patch('liquid_tracer.elk_layout._worker', side_effect=synthetic_candidate):
            return optimize_graph(graph, connector_style=style, layout_attempts=1)

    def test_completed_summary_geometry_reuses_and_corruption_invalidates(self):
        for style in ('straight', 'curved'):
            with self.subTest(style=style), tempfile.TemporaryDirectory() as temporary:
                graph = self.graph()
                result = self.layout(graph, style)
                self.assertEqual(_fingerprint(graph), _fingerprint(result))
                directory = Path(temporary) / (graph['run_id'] + '-elk-12345678')
                export_layout(result, directory)
                self.assertEqual(reusable_elk_preview(graph, Path(temporary), style, layout_attempts=1), result)
                modified = deepcopy(result)
                modified['context_connectors']['summaries'][0]['route'][0]['x'] = float('nan')
                save_json(directory / 'graph.json', modified)
                self.assertIsNone(reusable_elk_preview(graph, Path(temporary), style, layout_attempts=1))
                save_json(directory / 'graph.json', result)
                report = read_json(directory / 'layout-report.json')
                report['display_edge_count'] += 1
                save_json(directory / 'layout-report.json', report)
                self.assertIsNone(reusable_elk_preview(graph, Path(temporary), style, layout_attempts=1))

    def test_fallback_and_compaction_preserve_all_input_evidence(self):
        graph = self.graph()
        expected = evidence(graph)
        fallback = fallback_graph(graph)
        compact = compact_graph(self.layout(graph))
        for result in (fallback, compact):
            self.assertEqual(evidence(result), expected)
            self.assertEqual(len(display_graph(result)['edges']), len(graph['edges']) - 3)
            self.assertTrue(render_svg(result).startswith(b'<svg'))
            validate_plan(make_plan(result))

    def test_existing_board_geometry_and_partial_group_addition(self):
        graph = self.graph()
        mapped, geometry = {}, {}
        for index, node in enumerate(graph['nodes']):
            if node['kind'] == 'context_group':
                continue
            remote = f'remote-{index}'
            mapped[node['id']] = {'id': remote, 'endpoint': 'shapes'}
            geometry[remote] = [5000 + index * 200, 100, node['width'], node['height']]
        snapshot = {'version': 1, 'mode': 'update', 'board_id': 'synthetic',
                    'namespace': graph['namespace'], 'mapped': mapped, 'geometry': geometry,
                    'bounds': [0, 0, 10000, 1000], 'items': {remote: {} for remote in geometry}}
        requests = []
        def worker(request, seeds, **kwargs):
            requests.append(request)
            return synthetic_candidate(request, seeds, **kwargs)
        with patch('liquid_tracer.elk_layout._worker', side_effect=worker):
            result = prepare_graph(graph, snapshot, layout_attempts=1)
        self.assertEqual(evidence(result), evidence(graph))
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(requests[0]['children']), 1)
        self.assertEqual(requests[0]['edges'], [])
        for node in result['nodes']:
            if node['id'] in mapped:
                self.assertEqual([node[key] for key in ('x', 'y', 'width', 'height')], geometry[mapped[node['id']]['id']])
        self.assertEqual(result['board_layout']['counts']['new_connectors'], len(display_graph(result)['edges']))
        self.assertTrue(render_svg(result))
        validate_plan(make_plan(result))
