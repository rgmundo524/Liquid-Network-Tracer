"""Regressions for the caption-side steps reported in the compact T3 preview.

Coordinates and dimensions are retained from the screenshot, with synthetic IDs
and captions. This exercises route geometry only, without ELK or user case I/O.
"""
from copy import deepcopy
import unittest

from liquid_tracer.elk_layout import segment_hits_node
from liquid_tracer.trace_route_cleanup import cleanup_routes, _length, _points

FIXTURE = {'nodes': [{'id': 'n6', 'x': 6869.2, 'y': 14136.0, 'width': 160, 'height': 160, 'kind': 'address'},
           {'id': 'n5', 'x': 6869.2, 'y': 15012.0, 'width': 160, 'height': 160, 'kind': 'address'},
           {'id': 'n7', 'x': 7690.4, 'y': 13698.0, 'width': 160, 'height': 160, 'kind': 'transaction'},
           {'id': 'n4', 'x': 6869.2, 'y': 13698.0, 'width': 160, 'height': 160, 'kind': 'address'},
           {'id': 'n3', 'x': 8485.2, 'y': 14136.0, 'width': 160, 'height': 160, 'kind': 'address'},
           {'id': 'n8', 'x': 7690.4, 'y': 14574.0, 'width': 160, 'height': 160, 'kind': 'transaction'},
           {'id': 'n1', 'x': 8485.2, 'y': 14574.0, 'width': 160, 'height': 160, 'kind': 'event'},
           {'id': 'n2', 'x': 8485.2, 'y': 15012.0, 'width': 160, 'height': 160, 'kind': 'event'},
           {'id': 'n0', 'x': 7149.4, 'y': 14473.0, 'width': 240, 'height': 160, 'kind': 'context_group'}],
 'edges': [{'id': 'e0',
            'sections': [{'startPoint': {'x': 7029.2, 'y': 13778.0},
                          'bendPoints': [{'x': 7450.4, 'y': 13778.0}, {'x': 7450.4, 'y': 13738.0}],
                          'endPoint': {'x': 7690.4, 'y': 13738.0}}],
            'labels': [{'id': 'label:e0',
                        'text': 'Synthetic caption',
                        'x': 7176.6,
                        'y': 13747.0,
                        'width': 126.4,
                        'height': 24}]},
           {'id': 'e1',
            'sections': [{'startPoint': {'x': 7029.2, 'y': 14216.0},
                          'bendPoints': [{'x': 7516.4, 'y': 14216.0},
                                         {'x': 7516.4, 'y': 13963.0},
                                         {'x': 7494.4, 'y': 13963.0},
                                         {'x': 7494.4, 'y': 13818.0}],
                          'endPoint': {'x': 7690.4, 'y': 13818.0}}],
            'labels': [{'id': 'label:e1',
                        'text': 'Synthetic caption',
                        'x': 7442.2,
                        'y': 13932.0,
                        'width': 126.4,
                        'height': 24}]},
           {'id': 'e2',
            'sections': [{'startPoint': {'x': 7850.4, 'y': 13778.0},
                          'bendPoints': [{'x': 8231.6, 'y': 13778.0},
                                         {'x': 8231.6, 'y': 14009.0},
                                         {'x': 8253.6, 'y': 14009.0},
                                         {'x': 8253.6, 'y': 14216.0}],
                          'endPoint': {'x': 8485.2, 'y': 14216.0}}],
            'labels': [{'id': 'label:e2',
                        'text': 'Synthetic caption',
                        'x': 8175.0,
                        'y': 13978.0,
                        'width': 135.2,
                        'height': 24}]},
           {'id': 'e3',
            'sections': [{'startPoint': {'x': 7029.2, 'y': 15092.0},
                          'bendPoints': [{'x': 7516.4, 'y': 15092.0},
                                         {'x': 7516.4, 'y': 14885.0},
                                         {'x': 7494.4, 'y': 14885.0},
                                         {'x': 7494.4, 'y': 14694.0}],
                          'endPoint': {'x': 7690.4, 'y': 14694.0}}],
            'labels': [{'id': 'label:e3',
                        'text': 'Synthetic caption',
                        'x': 7442.2,
                        'y': 14854.0,
                        'width': 126.4,
                        'height': 24}]},
           {'id': 'e4',
            'sections': [{'startPoint': {'x': 7850.4, 'y': 14654.0},
                          'bendPoints': [{'x': 8231.6, 'y': 14654.0}, {'x': 8231.6, 'y': 14614.0}],
                          'endPoint': {'x': 8525.2, 'y': 14614.0}}],
            'labels': [{'id': 'label:e4',
                        'text': 'Synthetic caption',
                        'x': 7925.0,
                        'y': 14623.0,
                        'width': 232.0,
                        'height': 24}]},
           {'id': 'e5',
            'sections': [{'startPoint': {'x': 7850.4, 'y': 14694.0},
                          'bendPoints': [{'x': 8275.6, 'y': 14694.0},
                                         {'x': 8275.6, 'y': 14977.0},
                                         {'x': 8297.6, 'y': 14977.0},
                                         {'x': 8297.6, 'y': 15092.0}],
                          'endPoint': {'x': 8485.2, 'y': 15092.0}}],
            'labels': [{'id': 'label:e5',
                        'text': 'Synthetic caption',
                        'x': 8219.0,
                        'y': 14946.0,
                        'width': 135.2,
                        'height': 24}]},
           {'id': 'e6',
            'sections': [{'startPoint': {'x': 7389.4, 'y': 14553.0},
                          'bendPoints': [{'x': 7539.9, 'y': 14553.0}, {'x': 7539.9, 'y': 14614.0}],
                          'endPoint': {'x': 7690.4, 'y': 14614.0}}],
            'labels': []}],
 'eligible': {'e1': ['n6', 'n7'], 'e2': ['n7', 'n3'], 'e3': ['n5', 'n8'], 'e5': ['n8', 'n2']},
 'width': 9000,
 'height': 15500}


class ReportedRouteStepTests(unittest.TestCase):
    def test_reported_input_and_output_steps_simplify_with_surrounding_objects(self):
        before = deepcopy(FIXTURE)
        nodes, edges = FIXTURE['nodes'], FIXTURE['edges']
        result, stats = cleanup_routes(nodes, edges, FIXTURE['eligible'],
            node_shapes={node['id']:node['kind'] for node in nodes},
            width=FIXTURE['width'], height=FIXTURE['height'])
        self.assertEqual(FIXTURE, before)
        self.assertEqual(stats['applied'], 4)
        self.assertEqual(stats['steps_applied'], 4)
        self.assertEqual(stats['bends_removed'], 8)
        self.assertEqual(stats['length_removed'], 88)
        for original, edge in zip(edges, result):
            if edge['id'] not in FIXTURE['eligible']:
                self.assertEqual(edge, original)
                continue
            route, old = _points(edge), _points(original)
            self.assertEqual(len(route), 4)
            self.assertEqual((route[0], route[-1]), (old[0], old[-1]))
            self.assertLessEqual(_length(route), _length(old) + 1e-7)
            self.assertEqual(route[1]['x'], route[2]['x'])
            self.assertEqual(route[0]['y'], route[1]['y'])
            self.assertEqual(route[2]['y'], route[3]['y'])
            self.assertNotEqual(edge['labels'], original['labels'])
            for new_label, old_label in zip(edge['labels'], original['labels']):
                for field in ('id', 'text', 'width', 'height'):
                    self.assertEqual(new_label[field], old_label[field])
            for first, last in zip(route, route[1:]):
                for node in nodes:
                    centered = {**node, 'x':node['x'] + node['width']/2,
                                'y':node['y'] + node['height']/2}
                    self.assertFalse(segment_hits_node(first, last, centered))

    def test_budget_exhaustion_preserves_reported_geometry_and_captions(self):
        result, stats = cleanup_routes(FIXTURE['nodes'], FIXTURE['edges'], FIXTURE['eligible'],
            node_shapes={node['id']:node['kind'] for node in FIXTURE['nodes']},
            width=FIXTURE['width'], height=FIXTURE['height'], max_checks=0)
        self.assertEqual(result, FIXTURE['edges'])
        self.assertEqual(stats['applied'], 0)
        self.assertEqual(stats['checks'], 0)
        self.assertEqual(stats['budget_exhausted'], 4)
