"""Shortened section routes survive the graph adapter and saved-label contract."""
import copy
import unittest

from liquid_tracer.edge_labels import caption_size, route_signature, validate_label_layout
from liquid_tracer.elk_layout import _apply_candidate, _request_graph
from liquid_tracer.trace_section_geometry import assemble
from tests.test_trace_section_geometry import candidates_for, points


class RouteCleanupIntegrationTests(unittest.TestCase):
    def test_shortened_routes_keep_evidence_attachments_and_caption_signatures(self):
        for kind in ('transaction', 'address', 'event'):
            with self.subTest(kind=kind):
                graph = {'nodes': [
                    {'id': 'source', 'kind': 'transaction', 'column': 0, 'x': 0, 'y': 0,
                     'width': 160, 'height': 160, 'details': {}},
                    {'id': 'target', 'kind': kind, 'column': 1, 'x': 300, 'y': 0,
                     'width': 160, 'height': 160, 'details': {}}],
                    'edges': [{'id': 'output', 'source': 'source', 'target': 'target',
                               'outpoint': 'source:0', 'label': 'vout 0'}],
                    'fee_items': {}, 'graph_options': {'layout_style': 'trace'}}
                before = copy.deepcopy(graph)
                request, port_map, fee_ids = _request_graph(graph)
                request['nodeShapes'] = {node['id']: node['kind'] for node in graph['nodes']}
                groups = [['source'], ['target']]
                candidate = assemble(request, groups, candidates_for(request, groups), [])
                self.assertLessEqual(len(points(candidate['edges'][0])), 4)
                result = _apply_candidate(graph, candidate, port_map, fee_ids, 'elbowed')
                self.assertEqual(graph, before)
                edge = result['edges'][0]
                for key, value in before['edges'][0].items():
                    self.assertEqual(edge[key], value)
                validate_label_layout(edge)
                self.assertEqual(edge['label_layout']['route_signature'],
                                 route_signature([(point['x'], point['y']) for point in edge['route']]))
                size = caption_size(edge)
                self.assertEqual({key: edge['label_layout'][key] for key in size}, size)
                self.assertLessEqual(len(edge['route']), 4)
                for a, b in zip(edge['route'], edge['route'][1:]):
                    self.assertLess(min(abs(a['x']-b['x']), abs(a['y']-b['y'])), .001)


if __name__ == '__main__':
    unittest.main()
