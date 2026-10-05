"""Chain selection must isolate collection evidence and reviewed attributions."""
import contextlib
import fcntl
import io
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from liquid_tracer import address_import, shared_attributions as shared
from liquid_tracer.cli import main
from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.investigations import create_investigation, list_investigations, read_case, update_case
from liquid_tracer.services import effective_services, load_services, set_shared_attributions
from liquid_tracer.seed_settings import save_seeds, SeedEditConflict
from liquid_tracer.shared_collection import collect_prepared, dataset_path, load_shared_run, prepare_collection
from liquid_tracer.shared_projection import materialize_shared_source

TX = 'a' * 64
ADDRESS = 'SYNTHETIC-address-on-both-networks'


class BitcoinIsolationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.fixture = self.root / 'fixture.json'
        # Deliberately identical txid, address and payload on two chains. Chain
        # identity cannot be inferred from a fixture digest or an address alone.
        save_json(self.fixture, {
            '/tx/' + TX: {'txid': TX, 'vin': [], 'vout': [
                {'scriptpubkey': '51', 'scriptpubkey_type': 'unknown',
                 'scriptpubkey_address': ADDRESS, 'value': 123456789}],
                'status': {'confirmed': True}},
            '/tx/' + TX + '/outspends': [{'spent': False}],
        })
        self.cases = {chain: create_investigation(self.root, chain, blockchain=chain,
                     seeds=[TX + ':0'], fixture=self.fixture)
                      for chain in ('liquid', 'bitcoin')}

    def test_chain_is_immutable_and_legacy_case_defaults_to_liquid(self):
        case = self.cases['bitcoin']
        with self.assertRaises(TraceError):
            update_case(case, {'blockchain': 'liquid'})
        self.assertEqual(read_case(case)['blockchain'], 'bitcoin')
        old = self.root / 'legacy'; old.mkdir()
        save_json(old / 'case.json', {'schema_version': 1, 'case_id': 'f' * 32})
        before = (old / 'case.json').read_bytes()
        self.assertEqual(read_case(old)['blockchain'], 'liquid')
        self.assertEqual((old / 'case.json').read_bytes(), before)

    def test_shared_libraries_and_approval_tokens_are_isolated(self):
        text = json.dumps([{'address': ADDRESS, 'name': 'Liquid service', 'stop_tracing': True}])
        liquid_plan = shared.preview_import(self.root, text)
        with self.assertRaises(TraceError):
            shared.apply_import(self.root, text, blockchain='bitcoin', approval_sha256=liquid_plan['approval_sha256'])
        shared.apply_import(self.root, text, approval_sha256=liquid_plan['approval_sha256'])
        btc_text = text.replace('Liquid service', 'Bitcoin service')
        plan = shared.preview_import(self.root, btc_text, blockchain='bitcoin')
        shared.apply_import(self.root, btc_text, blockchain='bitcoin', approval_sha256=plan['approval_sha256'])
        self.assertNotEqual(plan['library_id'], liquid_plan['library_id'])
        for chain in self.cases:
            case = self.cases[chain]
            set_shared_attributions(case, True, expected_revision=load_services(case)['revision'])
            rule = effective_services(case)['rules'][ADDRESS]
            self.assertEqual(rule['name'], chain.capitalize() + ' service')
            self.assertIs(rule['stop_tracing'], False)
        self.assertTrue((self.root / '.shared-attributions.json').is_file())
        self.assertTrue((self.root / '.bitcoin' / '.shared-attributions.json').is_file())

    def test_import_network_column_must_match_case(self):
        text = 'Address,Name,Network,StopTracing\n' + ADDRESS + ',Deposit,bitcoin,false\n'
        self.assertTrue(address_import.preview_import(self.cases['bitcoin'], text)['valid'])
        self.assertFalse(address_import.preview_import(self.cases['liquid'], text)['valid'])
        self.assertTrue(shared.preview_import(self.root, text, blockchain='bitcoin')['valid'])
        self.assertFalse(shared.preview_import(self.root, text)['valid'])

    def test_shared_collections_coexist_but_cross_chain_members_are_rejected(self):
        identities = [read_case(case)['case_id'] for case in self.cases.values()]
        with self.assertRaisesRegex(TraceError, 'same blockchain'):
            prepare_collection(self.cases['bitcoin'], identities, hops=1)
        prepared = {}
        for chain, case in self.cases.items():
            prepared[chain] = prepare_collection(case, [read_case(case)['case_id']], hops=1)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(collect_prepared(case, prepared[chain]['request_id']), 0)
            result = json.loads(output.getvalue())
            _, state, _ = load_shared_run(case, result['run_id'])
            self.assertEqual(state['blockchain'], chain)
            _, projected, _ = materialize_shared_source(case, result['run_id'], result['dataset_id'])
            self.assertEqual(projected['blockchain'], chain)
            self.assertEqual(projected['outputs'][TX + ':0']['status'], 'unspent_at_observation')
        self.assertEqual(dataset_path(self.cases['liquid']).name, '.shared-collection')
        self.assertEqual(dataset_path(self.cases['bitcoin']).name, '.shared-collection-bitcoin')
        self.assertEqual({path for path, _ in list_investigations(self.root)}, set(self.cases.values()))
        with self.assertRaises(TraceError):
            load_shared_run(self.cases['bitcoin'], dataset_id=prepared['liquid']['dataset_id'])

    def test_seed_edit_observes_only_its_chain_collector_lock(self):
        for case in self.cases.values():
            dataset_path(case).mkdir()
        with (dataset_path(self.cases['bitcoin']) / 'trace.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaises(SeedEditConflict):
                save_seeds(self.cases['bitcoin'], [TX + ':1'], expected_revision=0)
            self.assertEqual(save_seeds(self.cases['liquid'], [TX + ':1'], expected_revision=0)['revision'], 1)

    def test_cli_refuses_a_chain_mismatch_before_fetching(self):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = main(['trace', '--case', str(self.cases['bitcoin']), '--seed', TX + ':0',
                           '--blockchain', 'liquid', '--fixture', str(self.fixture)])
        self.assertNotEqual(result, 0)
        self.assertIn('blockchain differs', errors.getvalue())
        self.assertFalse((self.cases['bitcoin'] / 'evidence.sqlite').exists())

    def test_legacy_source_cache_keeps_liquid_default_on_second_read(self):
        from liquid_tracer.shared_collection import _source, _SOURCE_HINTS
        from liquid_tracer.cli import run_path
        metadata = read_case(self.cases['liquid'])
        metadata.pop('fixture', None)
        metadata['latest_run'] = 'c' * 16
        archive = run_path(self.cases['liquid'], metadata['latest_run'])
        archive.mkdir(parents=True)
        save_json(archive / 'trace.json', {
            'case_id': metadata['case_id'], 'run_id': metadata['latest_run'],
            'source': 'https://blockstream.info/liquid/api'})
        (archive / 'SHA256SUMS').write_text('synthetic manifest')
        _SOURCE_HINTS.pop(archive / 'trace.json', None)
        first, _ = _source(self.cases['liquid'], metadata, verify=False)
        with patch('liquid_tracer.shared_collection.read_json', side_effect=AssertionError('cache missed')):
            second, parent = _source(self.cases['liquid'], metadata, verify=False)
        self.assertEqual(first, second)
        self.assertEqual(parent['blockchain'], 'liquid')

    def test_public_endpoint_query_keeps_chain_and_rejects_bitcoin_lbtc_budget(self):
        from liquid_tracer.workflow_api import public_plot
        from liquid_tracer.pegout_paths import validate_query
        query = validate_query(seeds=[TX + ':0'], include_unspent=True,
                               include_attributed_stops=True, blockchain='bitcoin')
        report = public_plot({'goal': 'pegouts', 'blockchain': 'bitcoin', 'query': query})
        self.assertEqual(report['query'], query)
        self.assertEqual(report['blockchain'], 'bitcoin')
        report = public_plot({'goal': 'pegouts', 'blockchain': 'bitcoin',
                              'query': {**query, 'pegout_lbtc_limit': '1'}})
        self.assertNotIn('query', report)

    def test_legacy_endpoint_cli_forwards_explicit_endpoint_options(self):
        with patch('liquid_tracer.pegouts.search_pegouts', return_value={}) as search:
            with contextlib.redirect_stdout(io.StringIO()):
                result = main(['pegouts', '--case', str(self.cases['bitcoin']),
                               '--include-unspent', '--include-unspendable', '--include-attributed-stops'])
        self.assertEqual(result, 0)
        for flag in ('include_unspent', 'include_unspendable', 'include_attributed_stops'):
            self.assertIs(search.call_args.kwargs[flag], True)

    def test_name_only_import_does_not_opt_into_a_stop(self):
        for chain, case in self.cases.items():
            for text in ('Address,Name\n' + ADDRESS + ',Service deposit\n',
                         json.dumps([{'address': ADDRESS, 'name': 'Service deposit'}])):
                with self.subTest(chain=chain, text=text):
                    plan = address_import.preview_import(case, text)
                    self.assertTrue(plan['valid'])
                    self.assertEqual(plan['active_stops_to_save'], 0)
