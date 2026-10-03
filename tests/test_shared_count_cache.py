"""Later shared count checkpoints hydrate plots without refetching or resealing."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from liquid_tracer.address_count_cache import CountCacheJournal
from liquid_tracer.address_counts import addresses, apply_saved_counts
from liquid_tracer.common import canonical, digest, read_json, save_json
from liquid_tracer.investigations import create_investigation, read_case
from liquid_tracer.plots import preview_plot, reviewed_plot
from liquid_tracer.shared_projection import materialize_shared_run
from tests import test_shared_projection as fixtures
from tests.test_attribution_convergence import tx


class SharedCountCacheTests(unittest.TestCase):
    def setUp(self):
        # Reuse the sealed a->c->b and separate a->d/f->e evidence scenario.
        # Import its module, rather than its TestCase, to avoid loading that
        # independent suite again through unittest's module discovery.
        self.scenario = fixtures.SharedProjectionTests()
        self.scenario.setUp()
        self.addCleanup(self.scenario.doCleanups)
        self.scenario.collect()
        self.case = self.scenario.case
        self.dataset = self.scenario.dataset
        self.source = self.scenario.source
        self.shared_id = self.scenario.shared_id
        self.shared_run = self.scenario.shared_run
        self.source_archive = self.scenario.source_archive
        self.source_before = self.scenario.files(self.source_archive)
        self.assertEqual(self.source.get('address_tx_counts', {}), {})
        network = patch('liquid_tracer.api.Esplora', side_effect=AssertionError('Shared plotting must stay offline'))
        network.start()
        self.addCleanup(network.stop)

    def record(self, address, count, *, day=2, source=None):
        return {'address': address, 'source': source or self.source['source'],
                'observed_at': f'2026-10-{day:02d}T00:00:00Z',
                'confirmed_tx_count': count, 'mempool_tx_count': 2,
                'observation_ids': [1000 + count]}

    def write_counts(self, case, identity, records, *, source=None):
        journal = CountCacheJournal(case, identity, source or self.source['source'])
        try:
            journal.write(records)
        finally:
            journal.close()

    def complete_shared_counts(self):
        records = {address: self.record(address, 100 + index)
                   for index, address in enumerate(addresses(self.source))}
        self.write_counts(self.dataset, self.shared_id, records)
        return records

    def plot(self, case=None):
        return preview_plot(case or self.case, 'full', self.shared_run,
                            data_source='shared', dataset_id=self.shared_id)

    def assert_plot_counts(self, case, plotted, expected):
        inputs = read_json(Path(plotted['directory']) / 'inputs.json')
        self.assertEqual(inputs['address_tx_counts'], expected)
        graph, _ = reviewed_plot(case, plotted['preview_id'])
        displayed = [node for node in graph['nodes']
                     if node['kind'] == 'address' and node['details'].get('address') in expected]
        self.assertTrue(displayed)
        for node in displayed:
            record = expected[node['details']['address']]
            self.assertEqual(node['tx_count'], record['confirmed_tx_count'] + record['mempool_tx_count'])

    def test_new_shared_plot_uses_later_counts_only_for_projected_addresses(self):
        records = self.complete_shared_counts()
        # A completed standalone count command publishes its compatibility
        # JSON, then clears the incremental journal used while collecting.
        data = {'schema_version': 1, 'case_id': self.shared_id,
                'source': self.source['source'], 'counts': records}
        data['sha256'] = digest(canonical(data))
        save_json(self.dataset / 'address-counts.json', data)
        journal = CountCacheJournal(self.dataset, self.shared_id, self.source['source'])
        try:
            journal.compacted()
            self.assertEqual(journal.connection.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 0)
        finally:
            journal.close()
        plotted = self.plot()
        state = read_json(self.case / 'runs' / plotted['run_id'] / 'trace.json')
        selected = set(addresses(state))
        self.assertLess(selected, set(records))
        self.assert_plot_counts(self.case, plotted, {address: records[address] for address in selected})
        self.assertEqual(self.scenario.files(self.source_archive), self.source_before)
        self.assertFalse((self.case / 'evidence.sqlite').exists())
        self.assertFalse((self.case / 'address-counts.json').exists())

    def test_existing_projection_and_saved_plot_stay_immutable_as_new_plot_gets_counts(self):
        previous = self.plot()
        projection = self.case / 'runs' / previous['run_id']
        projection_before = self.scenario.files(projection)
        preview_before = self.scenario.files(Path(previous['directory']))
        records = self.complete_shared_counts()
        updated = self.plot()
        self.assertEqual(updated['run_id'], previous['run_id'])
        self.assertNotEqual(updated['preview_id'], previous['preview_id'])
        state = read_json(projection / 'trace.json')
        self.assert_plot_counts(self.case, updated, {address: records[address] for address in addresses(state)})
        self.assertEqual(self.scenario.files(projection), projection_before)
        self.assertEqual(self.scenario.files(Path(previous['directory'])), preview_before)
        reviewed_plot(self.case, previous['preview_id'])
        self.assertEqual(self.scenario.files(self.source_archive), self.source_before)

    def test_new_investigation_can_reuse_covered_descendant_seed_and_shared_counts(self):
        records = self.complete_shared_counts()
        case = create_investigation(self.scenario.root, 'Created after collection',
                                    fixture=self.scenario.fixture, seeds=[tx('c') + ':0'])
        plotted = self.plot(case)
        state = read_json(case / 'runs' / plotted['run_id'] / 'trace.json')
        self.assertEqual(set(state['transactions']), {tx('c'), tx('b')})
        self.assertEqual(state['seeds'], [tx('c') + ':0'])
        self.assert_plot_counts(case, plotted, {address: records[address] for address in addresses(state)})
        self.assertNotIn('latest_run', read_case(case))
        self.assertFalse((case / 'evidence.sqlite').exists())

    def test_replaced_shared_identity_or_source_does_not_import_foreign_counts(self):
        identity = materialize_shared_run(self.case, self.shared_run, self.shared_id)
        state = read_json(self.case / 'runs' / identity / 'trace.json')
        self.complete_shared_counts()
        metadata = read_case(self.dataset)
        for change in ({'case_id': 'f' * 32}, {'source': 'https://example.invalid/liquid/api'}):
            with self.subTest(change=change):
                save_json(self.dataset / 'case.json', {**metadata, **change})
                try:
                    self.assertEqual(apply_saved_counts(self.case, deepcopy(state)), {})
                finally:
                    save_json(self.dataset / 'case.json', metadata)

    def test_other_source_cache_rows_are_ignored_and_newer_recipient_observations_win(self):
        identity = materialize_shared_run(self.case, self.shared_run, self.shared_id)
        state = read_json(self.case / 'runs' / identity / 'trace.json')
        address = addresses(state)[0]
        other_source = 'https://example.invalid/liquid/api'
        foreign = self.record(address, 999, source=other_source)
        self.write_counts(self.dataset, self.shared_id, {address: foreign}, source=other_source)
        self.assertEqual(apply_saved_counts(self.case, deepcopy(state)), {})
        shared = self.record(address, 100)
        self.write_counts(self.dataset, self.shared_id, {address: shared})
        for day in (1, 2, 3):
            with self.subTest(day=day):
                local = self.record(address, 7, day=day)
                self.write_counts(self.case, read_case(self.case)['case_id'], {address: local})
                self.assertEqual(apply_saved_counts(self.case, deepcopy(state)),
                                 {address: shared if day == 1 else local})


if __name__ == '__main__':
    unittest.main()
