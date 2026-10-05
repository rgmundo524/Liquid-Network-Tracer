"""Real HTTP navigation remains bounded, script-free, and separate from review."""
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.investigations import create_investigation, read_case
from liquid_tracer.layout_overview import navigation_files
from liquid_tracer.plots import preview_plot, reviewed_plot
from tests import test_web
from tests.test_attribution_convergence import graph_state
from tests.test_connections import saved_case


class OverviewHTTPTests(unittest.TestCase):
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success

    def setUp(self):
        test_web.LocalWebTests.setUp(self)
        self.enterContext(patch.dict('os.environ', {'XDG_CACHE_HOME': str(self.base / 'cache'),
                                                   'XDG_STATE_HOME': str(self.base / 'state')}))
        self.enterContext(patch('liquid_tracer.elk_layout.optimize_graph', side_effect=lambda graph, **kwargs: graph))
        self.enterContext(patch('liquid_tracer.api.Esplora.get', side_effect=AssertionError('No network collection')))
        self.enterContext(patch('liquid_tracer.layout_overview.MIN_NODES', 1))
        self.enterContext(patch('liquid_tracer.layout_overview.SECTION_NODES', 1))
        self.enterContext(patch('liquid_tracer.layout_overview.SECTIONS_PER_PAGE', 2))
        self.case = create_investigation(self.server.root, 'Synthetic navigation', seeds=['a' * 64 + ':0'])
        saved_case(self.case, graph_state((('a:0', 'b'), ('b:0', 'c')), seeds=('a:0',)))
        self.product = preview_plot(self.case, 'full')
        self.directory = self.case / 'previews' / self.product['preview_id']
        self.case_id = read_case(self.case)['case_id']
        self.base_url = '/files/' + self.case_id + '/previews/' + self.directory.name + '/'
        self.section = sorted(name for name in navigation_files(self.directory) if name.startswith('section-'))[0]

    def assert_navigation_policy(self, response):
        csp = response.getheader('Content-Security-Policy')
        sandbox = csp.split(';')[0].split()
        self.assertIn('sandbox', sandbox)
        self.assertIn('allow-same-origin', sandbox)
        self.assertNotIn('allow-scripts', sandbox)
        self.assertNotIn('allow-forms', sandbox)
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn('script-src', csp)  # Script loads inherit default-src none.
        self.assertEqual(response.getheader('X-Content-Type-Options'), 'nosniff')
        self.assertEqual(response.getheader('Referrer-Policy'), 'no-referrer')

    def test_overview_section_and_next_page_links_have_usable_navigation_policy(self):
        status, body, response = self.request(self.base_url + 'graph.html')
        self.assertEqual(status, 200, body)
        self.assertNotIn(b'<svg', body)
        self.assertNotIn(b'<script', body)
        self.assert_navigation_policy(response)
        links = re.findall(rb'href="((?:section|overview)-[^"]+\.html)"', body)
        self.assertTrue(any(name.startswith(b'section-') for name in links))
        self.assertTrue(any(name.startswith(b'overview-') for name in links))
        for name in set(links):
            with self.subTest(page=name):
                status, body, response = self.request(self.base_url + name.decode(), headers={
                    'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'})
                self.assertEqual(status, 200, body)
                self.assertNotIn(b'<script', body)
                self.assert_navigation_policy(response)
        status, body, response = self.request(self.base_url + 'details.html', headers={'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual(status, 200, body)
        self.assert_navigation_policy(response)

    def test_navigation_relaxes_origin_only_for_navigation_html(self):
        legacy = self.case / 'previews' / ('a' * 16 + '-elk-' + 'b' * 8)
        legacy.mkdir()
        (legacy / 'graph.html').write_text('<!doctype html><title>Historical preview</title>', encoding='utf-8')
        (legacy / 'graph.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding='utf-8')
        base = '/files/' + self.case_id + '/previews/' + legacy.name + '/'
        for name in ('graph.html', 'graph.svg'):
            with self.subTest(name=name):
                status, _, response = self.request(base + name)
                self.assertEqual(status, 200)
                policy = response.getheader('Content-Security-Policy')
                self.assertIn('sandbox', policy)
                self.assertNotIn('allow-same-origin', policy)
                self.assertNotIn('allow-scripts', policy)
                self.assertIn("default-src 'none'", policy)
        status, _, response = self.request(self.base_url + 'graph.svg')
        self.assertEqual(status, 200)
        self.assertNotIn('allow-same-origin', response.getheader('Content-Security-Policy'))

    def test_external_cross_site_and_origin_requests_remain_forbidden(self):
        for name in ('graph.html', self.section, 'details.html'):
            for headers in ({'Sec-Fetch-Site': 'cross-site'}, {'Origin': 'https://attacker.example'}):
                with self.subTest(name=name, headers=headers):
                    status, _, _ = self.request(self.base_url + name, headers=headers)
                    self.assertEqual(status, 403)
        with patch.object(self.server, 'start_job') as start:
            status, _, _ = self.request('/api/cases/' + self.case_id + '/actions', {
                'action': 'board-create-sync', 'preview_id': self.directory.name, 'name': 'Synthetic board'},
                headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(status, 403)
        start.assert_not_called()

    def test_malformed_deep_index_and_report_return_controlled_responses(self):
        malformed = '[' * 100000 + '0' + ']' * 100000
        path = self.directory / 'details.json'
        original = path.read_bytes()
        try:
            path.write_text(malformed)
            status, body, _ = self.request(self.base_url + self.section)
            self.assertEqual(status, 400, body)
        finally:
            path.write_bytes(original)
        (self.directory / 'plot.json').write_text(malformed)
        endpoint = '/api/cases/' + self.case_id + '/plots'
        status, body, _ = self.request(endpoint)
        self.assertEqual(status, 200, body)
        self.assertEqual(body['plots'], [])
        status, _, _ = self.request(endpoint + '/' + self.directory.name + '/summary')
        self.assertEqual(status, 404)

    def test_section_http_reads_only_navigation_files_and_small_metadata(self):
        opened, original = [], Path.open
        forbidden = {'graph.json', 'miro-plan.json', 'inputs.json', 'trace.json', 'transactions.csv'}
        def bounded(path, *args, **kwargs):
            opened.append(path.name)
            self.assertNotIn(path.name, forbidden)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', bounded), \
             patch('liquid_tracer.plots.reviewed_plot', side_effect=AssertionError('Publication review is separate')):
            for name in ('graph.html', self.section, 'details.html'):
                status, body, _ = self.request(self.base_url + name, headers={'Sec-Fetch-Site': 'same-origin'})
                self.assertEqual(status, 200, body)
        self.assertIn(self.section, opened)
        self.assertIn('details.json', opened)
        self.assertIn('SHA256SUMS', opened)

    def test_tampered_missing_or_unlisted_navigation_is_not_served(self):
        for name in ('graph.html', self.section, 'details.html', 'details.json'):
            path = self.directory / name
            original = path.read_bytes()
            try:
                with self.subTest(name=name, change='modified'):
                    path.write_bytes(original + b'\n ')
                    status, _, _ = self.request(self.base_url + self.section if name == 'details.json' else self.base_url + name)
                    self.assertEqual(status, 400)
                path.unlink()
                with self.subTest(name=name, change='missing'):
                    status, _, _ = self.request(self.base_url + self.section if name == 'details.json' else self.base_url + name)
                    self.assertIn(status, (400, 404))
            finally:
                path.write_bytes(original)
        status, _, _ = self.request(self.base_url + 'section-999999-000001.html')
        self.assertEqual(status, 404)
        (self.directory / 'SHA256SUMS').unlink()
        status, _, _ = self.request(self.base_url + self.section)
        self.assertEqual(status, 400)

    def test_publication_still_reviews_complete_graph_after_successful_navigation(self):
        action = {'action': 'board-create-sync', 'preview_id': self.directory.name, 'name': 'Synthetic board'}
        endpoint = '/api/cases/' + self.case_id + '/actions'
        with patch('liquid_tracer.plots.reviewed_plot', wraps=reviewed_plot) as review, \
             patch.object(self.server, 'start_job', return_value={'id': 'synthetic'}) as start:
            self.assertEqual(self.request(self.base_url + self.section)[0], 200)
            review.assert_not_called()
            status, body, _ = self.request(endpoint, action)
            self.assertEqual(status, 202, body)
            review.assert_called_once_with(self.case, self.directory.name)
            self.assertEqual(start.call_args.args[0][0], 'investigation-board-create-sync')
            review.reset_mock(); start.reset_mock()
            (self.directory / 'graph.json').write_bytes(b'tampered canonical graph')
            self.assertEqual(self.request(self.base_url + self.section)[0], 200)
            review.assert_not_called()
            status, _, _ = self.request(endpoint, action)
            self.assertEqual(status, 400)
            review.assert_called_once_with(self.case, self.directory.name)
            start.assert_not_called()

    def test_publication_plan_download_is_not_a_navigation_fast_path(self):
        with patch('liquid_tracer.plots.reviewed_plot', side_effect=TraceError('Full review required')) as review:
            self.assertEqual(self.request(self.base_url + self.section)[0], 200)
            review.assert_not_called()
            status, body, _ = self.request(self.base_url + 'miro-plan.json')
            self.assertEqual(status, 400, body)
            review.assert_called_once_with(self.case, self.directory.name)


if __name__ == '__main__':
    unittest.main()
