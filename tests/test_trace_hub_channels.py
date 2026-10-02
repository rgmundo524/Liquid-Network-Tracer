"""Change-row alignment retains distinct channels around anchored Trace hubs."""
import copy
import unittest

from liquid_tracer.change_layout import _retarget_channel, _routes
from liquid_tracer.elk_layout import attachment_point


def fixture():
    nodes = [dict(id='hub', kind='address', x=100, y=100, width=100, height=100),
             dict(id='first', kind='transaction', x=800, y=1000, width=100, height=100),
             dict(id='second', kind='transaction', x=800, y=1400, width=100, height=100)]
    edges = []
    for key, y, channel in [('first', 1000, 300), ('second', 1400, 450)]:
        for returning in (False, True):
            source, target = (key, 'hub') if returning else ('hub', key)
            coords = ([(850, y), (950 + channel, y), (950 + channel, -channel),
                       (channel, -channel), (channel, 100), (150, 100)] if returning else
                      [(150, 100), (channel, 100), (channel, y), (750, y)])
            edges.append(dict(id=key + ('-return' if returning else '-input'), source=source, target=target,
                              outpoint=key + ':0', role='context_input',
                              attachment={'startItem': {'position': {'x': '100%', 'y': '50%'}},
                                          'endItem': {'position': {'x': '100%' if returning else '0%', 'y': '50%'}}},
                              route=[dict(x=x, y=y) for x, y in coords],
                              connector_shape='elbowed', routing_exception='return' if returning else None))
    return dict(nodes=nodes, edges=edges, fee_items={},
                graph_options={'layout_style': 'trace', 'connector_style': 'elbowed'},
                layout={'branch_organization': {'hub_entries': {'hub': 'entry'}}})


class TraceHubChannelsTests(unittest.TestCase):
    def move_rows(self, graph):
        original = {node['id']: (node['x'], node['y']) for node in graph['nodes']}
        for node, y in zip(graph['nodes'], [500, 1200, 1700]):
            node['y'] = y
        _routes(graph, {node['id']: node for node in graph['nodes']}, original, set())

    def test_moving_hub_and_terminal_rows_does_not_collapse_distinct_native_channels(self):
        graph = fixture()
        original = copy.deepcopy(graph)
        self.move_rows(graph)
        nodes = {node['id']: node for node in graph['nodes']}
        for before, edge in zip(original['edges'], graph['edges']):
            self.assertEqual([p['x'] for p in edge['route']], [p['x'] for p in before['route']])
            self.assertNotEqual(edge['route'], before['route'])
            self.assertEqual(edge['route'][0], attachment_point(nodes[edge['source']], edge['attachment']['startItem']))
            self.assertEqual(edge['route'][-1], attachment_point(nodes[edge['target']], edge['attachment']['endItem']))
            self.assertTrue(all(a['x'] == b['x'] or a['y'] == b['y'] for a, b in zip(edge['route'], edge['route'][1:])))
            for field in ('id', 'source', 'target', 'outpoint', 'role'):
                self.assertEqual(edge[field], before[field])
        inputs = [edge for edge in graph['edges'] if edge['source'] == 'hub']
        self.assertEqual(len({edge['route'][1]['x'] for edge in inputs}), 2)
        returns = [edge for edge in graph['edges'] if edge['target'] == 'hub']
        self.assertEqual(len({min(p['y'] for p in edge['route']) for edge in returns}), 2)

    def test_standard_and_unanchored_trace_keep_existing_rerouting(self):
        standard, unanchored = fixture(), fixture()
        standard['graph_options']['layout_style'] = 'standard'
        unanchored['layout']['branch_organization']['hub_entries'] = {}
        for graph in (standard, unanchored):
            self.move_rows(graph)
        self.assertEqual(standard['edges'], unanchored['edges'])
        inputs = [edge for edge in standard['edges'] if edge['source'] == 'hub']
        self.assertEqual(len({edge['route'][1]['x'] for edge in inputs}), 1)

    def test_fallback_return_preserves_upper_and_lower_vertical_approaches(self):
        for side in ('0%', '100%'):
            with self.subTest(side=side):
                graph = fixture()
                edge = next(edge for edge in graph['edges'] if edge['id'] == 'first-return')
                graph['edges'] = [edge]
                edge['attachment']['endItem']['position'] = {'x': '50%', 'y': side}
                self.move_rows(graph)
                before, end = edge['route'][-2:]
                self.assertEqual(before['x'], end['x'])
                self.assertEqual(before['y'] < end['y'], side == '0%')
                self.assertEqual(edge['routing_exception'], 'return')
                self.assertTrue(all(a['x'] == b['x'] or a['y'] == b['y']
                                    for a, b in zip(edge['route'], edge['route'][1:])))

    def test_reversed_rows_or_changed_attachment_sides_use_fallback(self):
        edge = fixture()['edges'][0]
        self.assertIsNone(_retarget_channel(edge, {'x': 150, 'y': 1000}, {'x': 750, 'y': 100}))
        self.assertIsNone(_retarget_channel(edge, {'x': 50, 'y': 100}, {'x': 750, 'y': 1000}))
        edge['route'] = [edge['route'][0], edge['route'][-1]]
        self.assertIsNone(_retarget_channel(edge, {'x': 150, 'y': 500}, {'x': 750, 'y': 1200}))


if __name__ == '__main__':
    unittest.main()
