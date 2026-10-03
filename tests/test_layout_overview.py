"""Large preview navigation stays bounded and preserves all evidence."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError, canonical, save_json
from liquid_tracer.layout_preview import export_layout
from liquid_tracer.layout_overview import navigation_files, verified_navigation_file, _groups
from liquid_tracer.plots import plot_files, preview_plot, reviewed_plot
from liquid_tracer.web import LocalServer, RequestError
from liquid_tracer.investigations import create_investigation
from tests.test_layout_preview import graph_fixture
from tests.test_context_parallel_pipeline import shared_inputs
from tests.test_attribution_convergence import graph_state
from tests.test_connections import saved_case


class LayoutOverviewTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict('os.environ', {'XDG_CACHE_HOME': str(self.root / 'cache'),
                                                   'XDG_STATE_HOME': str(self.root / 'state')}))

    def export(self, graph):
        directory = self.root / 'preview'
        with patch('liquid_tracer.layout_overview.MIN_NODES', 1):
            result = export_layout(graph, directory)
        return directory, result

    def test_overview_has_no_full_drawing_and_every_object_has_one_section(self):
        graph = graph_fixture()
        graph['layout']['section_geometry'] = {'sections': [
            {'node_ids': [node['id'] for node in graph['nodes'][:2]]},
            {'node_ids': [graph['nodes'][2]['id']]}]}
        before = canonical(graph)
        directory, result = self.export(graph)
        html = Path(result['html']).read_text()
        self.assertIn('Trace section overview', html)
        self.assertNotIn('<svg', html)
        self.assertIn('Open complete SVG', html)
        self.assertNotIn('<script', html)
        self.assertEqual(canonical(graph), before)
        self.assertEqual(json.loads(Path(result['graph']).read_text()), graph)
        pages = [path.read_text() for path in directory.glob('section-*.html')]
        for node in graph['nodes']:
            self.assertEqual(sum(page.count('data-node-id="' + node['id'] + '"') for page in pages), 1)
        self.assertIn('data-edge-id="out:synthetic:0"', ''.join(pages))
        self.assertEqual(sum('out:synthetic:1</code>' in page for page in pages), 2)
        self.assertIn('Section 2</a>', ''.join(pages))
        self.assertEqual(navigation_files(directory), {path.name for path in directory.glob('section-*.html')})

    def test_high_degree_records_are_paginated_and_complete(self):
        graph = graph_fixture()
        graph['edges'] = [{**deepcopy(graph['edges'][0]), 'id': f'in:child:{index}'} for index in range(451)]
        directory, _ = self.export(graph)
        pages = sorted(directory.glob('section-*.html'))
        self.assertEqual(len(pages), 3)
        text = ''.join(path.read_text() for path in pages)
        for edge in graph['edges']:
            self.assertEqual(text.count(edge['id'] + '</code>'), 1)
        self.assertLessEqual(pages[0].read_text().count('data-edge-id='), 200)
        self.assertNotIn('<svg', pages[1].read_text())
        self.assertIn('Page 3 of 3', pages[2].read_text())
        self.assertIn('section-000001-000003.html', pages[1].read_text())

    def test_parallel_bundle_details_preserve_original_inputs_and_escaped_labels(self):
        _, _, graph = shared_inputs(count=6, traced=1)
        graph['nodes'][0]['label'] = '<script>untrusted</script>'
        directory, _ = self.export(graph)
        text = ''.join(path.read_text() for path in directory.glob('section-*.html'))
        self.assertNotIn('<script>', text)
        self.assertIn('&lt;script&gt;', text)
        for edge in graph['edges']:
            self.assertIn(edge['id'] + '</code>', text)
            if edge.get('outpoint'):
                self.assertIn(edge['outpoint'], text)

    def test_section_keeps_attribution_evidence_and_interpretation_visible(self):
        graph = graph_fixture()
        address = next(node for node in graph['nodes'] if node['kind'] == 'address')
        address['details'] = {'address': 'SYNTHETIC-labelled', 'network': 'liquid',
            'address_attributions': [{'entity': 'Example <exchange>', 'confidence': 'confirmed',
                'stop': True, 'source': 'Synthetic evidence source', 'notes': 'Unique review note',
                'observed_at': '2026-10-01T12:00:00Z'}]}
        graph['layout']['section_geometry'] = {'sections': [
            {'node_ids': [node['id']]} for node in graph['nodes']]}
        before = canonical(graph)
        directory, _ = self.export(graph)
        pages = [path.read_text() for path in directory.glob('section-*.html')]
        evidence_pages = [page for page in pages if 'Synthetic evidence source' in page]
        self.assertEqual(len(evidence_pages), 1)
        page = evidence_pages[0]
        for text in ('Unique review note', 'Confidence: confirmed', 'Stop tracing: Yes',
                     'Example &lt;exchange&gt;', '2026-10-01T12:00:00Z'):
            self.assertIn(text, page)
        self.assertIn('Address attribution register', page)
        self.assertIn('Graph legend', (directory / 'graph.html').read_text())
        for name in ('graph.html', 'details.html'):
            self.assertIn(graph['notice'], (directory / name).read_text())
            self.assertIn('do not prove ownership', (directory / name).read_text())
        self.assertEqual(canonical(graph), before)

    def test_deeply_nested_navigation_index_reports_damaged_artifact(self):
        directory, _ = self.export(graph_fixture())
        (directory / 'details.json').write_text('[' * 100000 + '0' + ']' * 100000)
        with self.assertRaisesRegex(TraceError, 'Invalid preview navigation index'):
            navigation_files(directory)

    def test_index_pages_are_bounded_even_with_many_singleton_sections(self):
        graph = graph_fixture(); template = graph['nodes'][0]
        graph['nodes'] = [{**template, 'id': f'tx:{index:04}', 'x': index * 250} for index in range(125)]
        graph['edges'] = []
        graph['layout']['section_geometry'] = {'sections': [{'node_ids': [node['id']]} for node in graph['nodes']]}
        directory, _ = self.export(graph)
        for name, count in [('graph.html', 60), ('overview-000002.html', 60), ('overview-000003.html', 5)]:
            text = (directory / name).read_text()
            self.assertEqual(text.count('<article class="card">'), count)
            self.assertNotIn('<svg', text)
        self.assertEqual(len(navigation_files(directory)), 127)

    def test_stale_or_duplicate_section_hints_cannot_hide_or_duplicate_nodes(self):
        graph = graph_fixture()
        graph['layout']['section_geometry'] = {'sections': [{'node_ids': ['missing', graph['nodes'][0]['id']]},
                                                            {'node_ids': [graph['nodes'][0]['id']]}]}
        groups = _groups(graph, graph['nodes'])
        self.assertEqual(sorted(key for group in groups for key in group), sorted(node['id'] for node in graph['nodes']))
        graph['nodes'].reverse()
        self.assertEqual(groups, _groups(graph, graph['nodes']))

    def test_atomic_completion_marker_is_last_after_navigation_files(self):
        original, observed = Path.replace, []
        def replace(path, target):
            if Path(target).name == 'graph.html':
                directory = Path(target).parent
                observed.append(all((directory / name).is_file() for name in navigation_files(directory)))
            return original(path, target)
        with patch.object(Path, 'replace', replace):
            self.export(graph_fixture())
        self.assertEqual(observed, [True])

    def test_failed_section_export_never_publishes_graph_html(self):
        original = Path.write_text
        def write(path, *args, **kwargs):
            if path.name.startswith('section-'):
                raise OSError('disk full')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'write_text', write), self.assertRaises(TraceError):
            self.export(graph_fixture())
        self.assertFalse((self.root / 'preview' / 'graph.html').exists())

    def test_small_layout_keeps_existing_full_preview(self):
        result = export_layout(graph_fixture(), self.root / 'small')
        self.assertIn('<svg', Path(result['html']).read_text())
        self.assertFalse(navigation_files(self.root / 'small'))

    def test_export_rejects_page_and_index_limits_before_completion(self):
        for field, limit in [('MAX_PAGES', 1), ('INDEX_BYTES', 10)]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                graph = graph_fixture()
                graph['layout']['section_geometry'] = {'sections': [{'node_ids': [node['id']]} for node in graph['nodes']]}
                directory = Path(temp) / 'preview'
                with patch('liquid_tracer.layout_overview.' + field, limit), \
                     patch('liquid_tracer.layout_overview.SECTIONS_PER_PAGE', 1), \
                     patch('liquid_tracer.layout_overview.MIN_NODES', 1), self.assertRaises(TraceError):
                    export_layout(graph, directory)
                self.assertFalse((directory / 'graph.html').exists())

    def test_invalid_counts_and_symlinks_are_rejected(self):
        directory, _ = self.export(graph_fixture())
        original = json.loads((directory / 'details.json').read_text())
        for value in [True, 0, -1, 200001, '1']:
            invalid = deepcopy(original)
            invalid['navigation']['sections'][0]['pages'] = value
            save_json(directory / 'details.json', invalid)
            with self.subTest(value=value), self.assertRaises(TraceError):
                navigation_files(directory)
        (directory / 'details.json').unlink()
        (directory / 'details.json').symlink_to(self.root / 'outside')
        with self.assertRaises(TraceError):
            navigation_files(directory)

    def saved_plot(self):
        case = create_investigation(self.root, 'Overview', seeds=['a' * 64 + ':0'])
        state = graph_state((('a:0', 'b'),), seeds=('a:0',))
        saved_case(case, state)
        with patch('liquid_tracer.elk_layout.optimize_graph', side_effect=lambda graph, **kwargs: graph), \
             patch('liquid_tracer.layout_overview.MIN_NODES', 1):
            product = preview_plot(case, 'full')
        return case, case / 'previews' / product['preview_id'], product

    def test_manifest_and_full_publication_review_include_navigation(self):
        case, directory, product = self.saved_plot()
        names = navigation_files(directory)
        self.assertTrue(names)
        self.assertTrue(names <= plot_files(directory))
        graph, plan = reviewed_plot(case, directory.name)
        self.assertEqual(graph['run_id'], plan['run_id'])
        page = directory / sorted(names)[0]
        page.write_text(page.read_text() + 'tampered')
        with self.assertRaises(TraceError):
            reviewed_plot(case, directory.name)
        with self.assertRaisesRegex(TraceError, 'checksum'):
            verified_navigation_file(directory, page.name)

    def test_navigation_http_resolution_hashes_only_viewed_html_and_index(self):
        case, directory, _ = self.saved_plot()
        name = sorted(navigation_files(directory))[0]
        opened, original = [], Path.open
        def bounded(path, *args, **kwargs):
            opened.append(path.name)
            self.assertNotIn(path.name, {'graph.json', 'miro-plan.json', 'inputs.json', 'trace.json'})
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', bounded), \
             patch('liquid_tracer.plots.reviewed_plot', side_effect=AssertionError('Publication-only verification')):
            self.assertEqual(LocalServer.artifact(case, ['previews', directory.name, name]), directory / name)
            self.assertEqual(LocalServer.artifact(case, ['previews', directory.name, 'graph.html']), directory / 'graph.html')
        self.assertIn(name, opened)
        with patch('liquid_tracer.plots.reviewed_plot', side_effect=AssertionError('still required')):
            with self.assertRaisesRegex(AssertionError, 'still required'):
                LocalServer.artifact(case, ['previews', directory.name, 'miro-plan.json'])
        with self.assertRaises(RequestError):
            LocalServer.artifact(case, ['previews', directory.name, 'section-999999-000001.html'])
        (directory / 'SHA256SUMS').unlink()
        with self.assertRaises(TraceError):
            LocalServer.artifact(case, ['previews', directory.name, name])

    def test_navigation_rejects_fifo_before_opening_it(self):
        case, directory, _ = self.saved_plot()
        name = sorted(navigation_files(directory))[0]
        page = directory / name
        page.unlink(); os.mkfifo(page)
        with self.assertRaisesRegex(TraceError, 'ordinary files'):
            verified_navigation_file(directory, name)

    def test_connections_navigation_entry_checks_its_checksum(self):
        _, directory, _ = self.saved_plot()
        case = directory.parent.parent
        target = directory.with_name(directory.name.replace('-plots-', '-connections-'))
        directory.rename(target)
        (target / 'graph.html').write_text('tampered')
        with self.assertRaisesRegex(TraceError, 'checksum'):
            LocalServer.artifact(case, ['previews', target.name, 'graph.html'])


if __name__ == '__main__':
    unittest.main()
