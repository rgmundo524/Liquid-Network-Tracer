"""Shared-library APIs retain browser security and nonblocking task polling."""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from liquid_tracer.services import load_services
from tests import test_web


class SharedAttributionWebTests(unittest.TestCase):
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    create = test_web.LocalWebTests.create

    def setUp(self):
        test_web.LocalWebTests.setUp(self)
        _, self.metadata = self.create()
        self.case, _ = self.server.case(self.metadata['id'])
        self.route = '/api/cases/' + self.metadata['id'] + '/shared-attributions'
        self.text = 'Address,Name,stop_tracing,hop_limit\nSYNTHETIC-shared-address,Exchange,true,0\n'

    def save_library(self):
        body = {'text': self.text, 'format': 'csv'}
        preview = self.success('/api/shared-attributions/import', body)
        self.assertTrue(preview['valid'])
        self.assertEqual(preview['active_stops_to_save'], 0)
        return self.success('/api/shared-attributions/import', {**body, 'approve_plan': preview['approval_sha256']})

    def test_review_import_opt_in_and_export_without_credentials_or_blockchain(self):
        with patch('liquid_tracer.web.worker_command', side_effect=AssertionError('No worker')), \
                patch('liquid_tracer.api.Esplora.get', side_effect=AssertionError('No fetch')):
            self.save_library()
            catalog = self.success('/api/shared-attributions', {'query': 'exchange'})
            self.assertEqual(catalog['total'], 1)
            self.assertFalse(catalog['rows'][0]['stop_tracing'])
            self.assertIsNone(catalog['rows'][0]['hop_limit'])
            state = self.success(self.route)
            self.assertFalse(state['enabled'])
            state = self.success(self.route, {'enabled': True, 'expected_revision': state['revision']})
            self.assertTrue(state['enabled'])
            self.assertEqual(state['shared_count'], 1)
            self.assertEqual(load_services(self.case)['rules'], {})
            detail = self.success('/api/cases/' + self.metadata['id'] + '/address',
                {'address': 'SYNTHETIC-shared-address'})
            self.assertEqual(detail['service']['attribution_origin'], 'shared')
            code, data, response = self.request('/api/shared-attributions/export')
            self.assertEqual(code, 200)
            self.assertIn(b'SYNTHETIC-shared-address', data)
            self.assertIn('attachment', response.getheader('Content-Disposition'))
        self.assertEqual(self.success('/api/jobs')['jobs'], [])

    def test_shared_library_routes_keep_bitcoin_separate_from_liquid(self):
        self.save_library()
        text = "Address,Name,stop_tracing\nbc1qexampleaddress,Bitcoin Exchange,true\n"
        body = {"text": text, "format": "csv", "blockchain": "bitcoin"}
        reviewed = self.success("/api/shared-attributions/import", body)
        self.assertTrue(reviewed["valid"])
        self.success("/api/shared-attributions/import", {**body, "approve_plan": reviewed["approval_sha256"]})
        bitcoin = self.success("/api/shared-attributions", {"blockchain": "bitcoin"})
        liquid = self.success("/api/shared-attributions", {})
        self.assertEqual([row["address"] for row in bitcoin["rows"]], ["bc1qexampleaddress"])
        self.assertEqual([row["address"] for row in liquid["rows"]], ["SYNTHETIC-shared-address"])
        self.assertFalse(bitcoin["rows"][0]["stop_tracing"])
        code, data, _ = self.request("/api/shared-attributions/export?blockchain=bitcoin")
        self.assertEqual(code, 200)
        self.assertIn(b"bc1qexampleaddress", data)
        self.assertNotIn(b"SYNTHETIC-shared-address", data)
        for suffix in ("blockchain=ethereum", "blockchain=bitcoin&blockchain=liquid", "path=/tmp/x"):
            self.assertEqual(self.request("/api/shared-attributions/export?" + suffix)[0], 400)

    def test_binding_rejects_stale_revision_invalid_fields_and_active_case(self):
        self.save_library()
        for body in ({'enabled': 'true', 'expected_revision': 0}, {'enabled': True},
                     {'enabled': True, 'expected_revision': False},
                     {'enabled': True, 'expected_revision': 0, 'path': '/tmp/injected'}):
            self.assertEqual(self.request(self.route, body)[0], 400)
        self.success(self.route, {'enabled': True, 'expected_revision': 0})
        self.assertEqual(self.request(self.route, {'enabled': False, 'expected_revision': 0})[0], 400)
        job = test_web.synthetic_running_job(self.metadata['id'])
        self.server.jobs[job['id']] = job
        self.assertEqual(self.request(self.route, {'enabled': False, 'expected_revision': 1})[0], 409)
        self.assertTrue(self.success(self.route)['enabled'])

    def test_global_import_enforces_csrf_origin_and_rejects_paths(self):
        for headers in ({'X-Liquid-CSRF': 'wrong'}, {'Origin': 'https://attacker.invalid'}):
            self.assertEqual(self.request('/api/shared-attributions/import', {'text': self.text}, headers=headers)[0], 403)
        self.assertEqual(self.request('/api/shared-attributions/import', {'path': '/tmp/a.csv'})[0], 400)
        self.assertEqual(self.request('/api/shared-attributions', {'case_id': self.metadata['id']})[0], 400)
        self.assertEqual(self.request('/api/shared-attributions/unknown', {})[0], 404)

    def test_slow_import_validation_does_not_block_task_polling(self):
        entered, release = threading.Event(), threading.Event()
        from liquid_tracer.shared_attributions import preview_import
        def slow(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Task polling blocked behind import')
            return preview_import(*args, **kwargs)
        with patch('liquid_tracer.shared_attributions.preview_import', side_effect=slow), \
                ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(self.request, '/api/shared-attributions/import', {'text': self.text})
            try:
                self.assertTrue(entered.wait(2))
                listing = pool.submit(self.request, '/api/jobs')
                self.assertEqual(listing.result(timeout=2)[0], 200)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=2)[0], 200)

    def test_slow_inherited_address_read_does_not_block_task_polling(self):
        self.save_library()
        self.success(self.route, {'enabled': True, 'expected_revision': 0})
        entered, release = threading.Event(), threading.Event()
        from liquid_tracer.shared_attributions import load_library
        def slow(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Task polling blocked behind attribution read')
            return load_library(*args, **kwargs)
        with patch('liquid_tracer.shared_attributions.load_library', side_effect=slow), \
                ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(self.request, '/api/cases/' + self.metadata['id'] + '/address',
                {'address': 'SYNTHETIC-shared-address'})
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(pool.submit(self.request, '/api/jobs').result(timeout=2)[0], 200)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=2)[0], 200)


if __name__ == '__main__':
    unittest.main()
