"""Ending branches share readable columns without changing tracing evidence."""
import copy
import unittest

from liquid_tracer.elk_layout import _request_graph, optimize_graph
from liquid_tracer.trace_layout import trace_structure, trace_order, trace_metrics
from tests.test_elk_layout import HAS_ELK
from tests.test_trace_layout import fixture


def terminal_fixture():
    graph, node, edge = fixture()
    node('hub', 'address', 1, layout_hub=True)
    rows = []
    for index, kind in enumerate(('pegout', 'unspendable', 'unspent')):
        tx, end = f'terminal-tx-{index}', f'terminal-end-{index}'
        rows.append((tx, end))
        node(tx, 'transaction', 2)
        node(end, 'address' if kind == 'unspent' else 'event', 3,
             role='unspent_endpoint' if kind == 'unspent' else kind,
             label=kind, details={'endpoint_kind': kind})
        edge(f'terminal-in-{index}', 'hub', tx, f'external:{index}', role='traced_input')
        edge(f'terminal-out-{index}', tx, end, f'{tx}:0')
    node('fee', 'event', 3)
    edge('fee-output', rows[0][0], 'fee', rows[0][0] + ':fee')
    graph['fee_items'] = {'fee': {'endpoint': 'shapes'}, 'fee-output': {'endpoint': 'connectors'}}
    return graph, rows, node, edge


class TerminalRowTests(unittest.TestCase):
    def test_endpoint_types_share_one_uninterrupted_band(self):
        graph, rows, _, _ = terminal_fixture()
        before = copy.deepcopy(graph)
        structure = trace_structure(graph)
        self.assertEqual(len(structure['terminal_rows']), 1)
        row = structure['terminal_rows'][0]
        self.assertEqual(row['anchors'], ['hub'])
        self.assertEqual(row['transactions'], [tx for tx, _ in rows])
        ordered = trace_order(graph)
        positions = [ordered.index(key) for pair in rows for key in pair]
        band = ordered[min(positions):max(positions) + 1]
        self.assertEqual(set(band), {key for pair in rows for key in pair})
        self.assertNotIn('fee', band)
        graph['nodes'].reverse()
        graph['edges'].reverse()
        self.assertEqual(trace_order(graph), ordered)
        graph['nodes'].reverse()
        graph['edges'].reverse()
        self.assertEqual(graph, before)

    def test_continuing_branch_is_not_treated_as_an_ending_transaction(self):
        graph, rows, node, edge = terminal_fixture()
        node('later', 'transaction', 4)
        edge('later-input', rows[2][1], 'later', rows[2][0] + ':0')
        row = trace_structure(graph)['terminal_rows'][0]
        self.assertEqual(row['transactions'], [tx for tx, _ in rows[:2]])
        self.assertNotIn('later', row['transactions'])

    def test_different_dependencies_keep_their_existing_columns(self):
        graph, rows, _, _ = terminal_fixture()
        next(n for n in graph['nodes'] if n['id'] == rows[2][0])['column'] = 4
        row = trace_structure(graph)['terminal_rows'][0]
        self.assertEqual(row['transactions'], [tx for tx, _ in rows[:2]])
        self.assertEqual(next(n for n in graph['nodes'] if n['id'] == rows[2][0])['column'], 4)
        graph['graph_options']['layout_style'] = 'standard'
        self.assertEqual(trace_structure(graph)['terminal_rows'], [])
        self.assertIsNone(trace_order(graph))

    def test_trace_uses_source_layers_and_measures_column_alignment(self):
        graph, rows, _, _ = terminal_fixture()
        request, _, _ = _request_graph(graph)
        self.assertEqual(request['layoutOptions']['elk.layered.layering.strategy'], 'LONGEST_PATH_SOURCE')
        self.assertEqual(trace_metrics(graph)['terminal_column_drift'], 0)
        next(n for n in graph['nodes'] if n['id'] == rows[1][0])['x'] += 800
        self.assertGreater(trace_metrics(graph)['terminal_column_drift'], 0)

    @unittest.skipUnless(HAS_ELK, 'local ELK package is not installed')
    def test_real_elk_stacks_end_transactions_with_their_endpoints_beside_them(self):
        graph, rows, _, _ = terminal_fixture()
        before = copy.deepcopy(graph)
        result = optimize_graph(graph, 'elbowed', layout_attempts=1)
        nodes = {n['id']: n for n in result['nodes']}
        self.assertEqual(len({nodes[tx]['x'] for tx, _ in rows}), 1)
        self.assertEqual(len({nodes[end]['x'] for _, end in rows}), 1)
        self.assertEqual(len({nodes[tx]['y'] for tx, _ in rows}), 3)
        for tx, end in rows:
            self.assertGreater(nodes[end]['x'], nodes[tx]['x'])
            self.assertLess(abs(nodes[end]['y'] - nodes[tx]['y']), 81)
        self.assertEqual(result['layout']['metrics']['after']['node_overlaps'], 0)
        self.assertEqual(result['layout']['trace_layout']['terminal_column_drift'], 0)
        self.assertEqual(graph, before)
        self.assertEqual([(e['id'], e['source'], e['target'], e['outpoint']) for e in result['edges']],
                         [(e['id'], e['source'], e['target'], e['outpoint']) for e in graph['edges']])
        self.assertEqual({n['id']: n['details'] for n in result['nodes']},
                         {n['id']: n['details'] for n in graph['nodes']})
