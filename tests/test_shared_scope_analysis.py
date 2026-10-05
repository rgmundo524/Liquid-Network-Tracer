"""Shared baselines retain each investigation's seeds and current boundaries."""
import unittest
from unittest.mock import patch

from liquid_tracer.scope_analysis import analyze_scope
from liquid_tracer.services import set_service
from tests import test_shared_snapshot_plots
from tests.test_attribution_convergence import tx


class SharedScopeAnalysisTests(unittest.TestCase):
    setUp = test_shared_snapshot_plots.SharedSnapshotPlotTests.setUp
    collect = test_shared_snapshot_plots.SharedSnapshotPlotTests.collect
    files = staticmethod(test_shared_snapshot_plots.SharedSnapshotPlotTests.files)

    def test_shared_baseline_keeps_seed_distances_and_never_materializes_a_graph(self):
        original = self.files(self.source_archive)
        with patch('liquid_tracer.export.build_graph', side_effect=AssertionError('No graph')):
            result = analyze_scope(self.case, self.shared_run, data_source='shared',
                                   dataset_id=self.shared_id, max_hops=2)
            warm = analyze_scope(self.case, self.shared_run, data_source='shared',
                                 dataset_id=self.shared_id, max_hops=2)
        self.assertEqual([row['transaction_count'] for row in result['comparisons']], [1, 2, 3])
        self.assertEqual(result['comparisons'][-1]['pegout_count'], 1)
        self.assertEqual(result['source']['dataset_id'], self.shared_id)
        self.assertEqual(result['source']['seed_basis'], 'investigation_settings')
        self.assertTrue(warm['cache_hit'])
        boundary = next(row for row in result['frontier'] if row['outpoint'] == tx('b') + ':0')
        self.assertEqual(boundary['hop'], 2)
        self.assertTrue(boundary['saved_continuation'])
        self.assertEqual(boundary['reason'], 'analysis_hop_limit')
        self.assertEqual(original, self.files(self.source_archive))
        self.assertFalse((self.case / 'runs').exists())

    def test_current_investigation_stop_applies_without_inheriting_other_seed_paths(self):
        set_service(self.case, 'SYNTHETIC-c-address', name='Stop here', stop_tracing=True)
        result = analyze_scope(self.case, self.shared_run, data_source='shared',
                               dataset_id=self.shared_id, max_hops=10)
        self.assertEqual(result['comparisons'][-1]['transaction_count'], 2)
        self.assertEqual(result['comparisons'][-1]['pegout_count'], 0)
        self.assertEqual(result['frontier'][0]['outpoint'], tx('c') + ':0')
        self.assertEqual(result['frontier'][0]['reason'], 'explicit_stop')


if __name__ == '__main__':
    unittest.main()
