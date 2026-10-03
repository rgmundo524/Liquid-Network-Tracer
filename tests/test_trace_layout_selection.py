"""Trace preferences balance readability only after collision checks."""
import copy
import unittest

from liquid_tracer.elk_layout import _candidate_score, _request_graph, optimize_graph
from tests.test_trace_layout import fixture
from tests.test_elk_layout import HAS_ELK


def score(*, style=True, drift=0, distance=1, interleaving=0, crossings=0, overlap=0,
          intersection=0, coincident=0, truncated=False, connector_overlaps=0, inversions=0):
    geometry = dict(node_overlaps=overlap, node_intersections=intersection, crossings=crossings,
                    connector_overlaps=connector_overlaps, edge_length=1000, truncated=truncated)
    return _candidate_score(geometry, geometry,
        dict(endpoint_order_inversions=inversions, coincident_ports=coincident),
        dict(weighted_vertical_travel=0),
        dict(flow_order_inversions=0, sibling_interleavings=0, transaction_distance=100,
             transaction_center_drift=0),
        dict(interleavings=0, boundary_depth=0, interbranch_travel=0),
        dict(alignment_deviation=drift, center_offset=0), 'traced_first',
        trace=dict(enabled=style, edge_count=20, spine_alignment=drift,
                   terminal_distance=distance, branch_interleaving=interleaving))


class TraceSelectionTests(unittest.TestCase):
    def test_preview_identifies_style_without_mislabeling_preserved_board_geometry(self):
        from liquid_tracer.layout_preview import layout_title, layout_notice
        graph = {"graph_options": {"layout_style": "trace"}, "layout": {}}
        self.assertEqual(layout_title(graph), "Trace layout")
        self.assertIn("separate branches", layout_notice(graph))
        graph["layout"]["algorithm"] = "board_incremental_v1"
        self.assertEqual(layout_title(graph), "Miro board update layout")
        self.assertIn("Existing objects retain", layout_notice(graph))

    def test_small_crossing_tradeoff_can_win_substantially_straighter_trace(self):
        self.assertLess(score(drift=0, crossings=1), score(drift=2, crossings=0))
        self.assertGreater(score(style=False, drift=0, crossings=1),
                           score(style=False, drift=2, crossings=0))

    def test_equal_readability_still_prefers_fewer_crossings(self):
        self.assertLess(score(crossings=0), score(crossings=3))

    def test_collisions_and_ambiguous_ports_cannot_buy_better_alignment(self):
        good = score(drift=10)
        for unsafe in [score(overlap=1), score(intersection=1), score(coincident=1)]:
            self.assertLess(good, unsafe)

    def test_shared_connector_segments_cannot_buy_better_alignment_or_input_order(self):
        self.assertLess(score(drift=10, inversions=3), score(connector_overlaps=1))
        self.assertLess(score(connector_overlaps=1), score(connector_overlaps=3))
        self.assertLess(score(connector_overlaps=10), score(intersection=1))

    def test_complete_measurement_preferred_over_lower_bound(self):
        self.assertLess(score(drift=10), score(truncated=True))

    def test_terminal_proximity_and_branch_order_affect_selection(self):
        self.assertLess(score(distance=1), score(distance=10))
        self.assertLess(score(interleaving=0), score(interleaving=1))

    def test_trace_order_sent_to_worker_excludes_separately_placed_fees(self):
        graph, node, edge = fixture()
        node('fee', 'event', 1)
        edge('fee-out', 'root', 'fee', 'root:fee')
        graph['fee_items'] = {'fee': {'endpoint': 'shapes'}, 'fee-out': {'endpoint': 'connectors'}}
        request, _, fees = _request_graph(graph)
        self.assertEqual(fees, {'fee'})
        self.assertNotIn('fee', request['centerNodeOrder'])
        self.assertEqual(set(request['centerNodeOrder']), {node['id'] for node in request['children']})

    @unittest.skipUnless(HAS_ELK, 'local ELK package is not installed')
    def test_real_engine_keeps_all_evidence_and_separates_the_other_branch(self):
        graph, _, _ = fixture()
        original = copy.deepcopy(graph)
        result = optimize_graph(graph, 'elbowed', layout_attempts=1)
        self.assertEqual(graph, original)
        self.assertEqual({node['id'] for node in result['nodes']}, {node['id'] for node in graph['nodes']})
        self.assertEqual([(e['id'], e['source'], e['target'], e.get('outpoint')) for e in result['edges']],
                         [(e['id'], e['source'], e['target'], e.get('outpoint')) for e in graph['edges']])
        self.assertEqual(result['layout']['metrics']['after']['node_overlaps'], 0)
        self.assertTrue(result['layout']['trace_layout']['enabled'])
        self.assertLess(result['layout']['trace_layout']['spine_alignment'], .3)
        nodes = {node['id']: node for node in result['nodes']}
        self.assertNotEqual(nodes['b-tx-0']['y'], nodes['a-tx-0']['y'])
