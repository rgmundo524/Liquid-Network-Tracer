"""Import before the first run using real local HTTP and terminal interfaces."""
import importlib.util
import json
import unittest
from unittest.mock import patch
from liquid_tracer.investigations import read_case
from liquid_tracer.menu import create_app
from liquid_tracer.services import load_services, set_service
from tests import test_web, test_menu_addresses

A, B = 'SYNTHETIC-imported-A', 'SYNTHETIC-imported-B'


class WebImportTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    create = test_web.LocalWebTests.create

    def test_upload_preview_apply_then_list_without_any_run(self):
        _, case = self.create(); path, _ = self.server.case(case['id'])
        route = '/api/cases/' + case['id']
        payload = {'text': 'address,name\n' + A + ',Possible service\n' + B + ',Another\n'}
        with patch.object(self.server, 'start_job') as start:
            review = self.success(route + '/address-import', payload)
            self.assertFalse((path / 'services.json').exists())
            result = self.success(route + '/address-import', {**payload, 'approve_plan': review['approval_sha256']})
            self.assertEqual(result['changed'], 2)
            self.assertEqual(self.success(route + '/addresses', {})['total'], 2)
            self.assertNotIn('latest_run', read_case(path))
            start.assert_not_called()

    def test_rows_errors_safe_and_all_or_none(self):
        _, case = self.create(); route = '/api/cases/' + case['id'] + '/address-import'
        result = self.success(route, {'text': json.dumps([A, {'address': B, 'stop_tracing': 'maybe'}])})
        self.assertFalse(result['valid'])
        self.assertEqual(result['errors'][0]['row'], 2)
        self.assertEqual(self.request(route, {'text': A, 'approve_plan': 'wrong'})[0], 400)
        path, _ = self.server.case(case['id'])
        self.assertFalse((path / 'services.json').exists())

    def test_import_only_body_limit_and_no_remote_path_or_extra_arguments(self):
        _, case = self.create(); route = '/api/cases/' + case['id']
        text = '\n'.join('SYNTHETIC-' + str(i).zfill(30) for i in range(2000))
        self.assertGreater(len(text), 64 * 1024)
        self.assertTrue(self.success(route + '/address-import', {'text': text})['valid'])
        self.assertEqual(self.request(route + '/services', {'text': text})[0], 413)
        for extra in ({'file': '/private/key'}, {'arguments': ['--shell']}, {'format': ['csv']}):
            self.assertEqual(self.request(route + '/address-import', {'text': A, **extra})[0], 400)
        self.assertEqual(self.request(route + '/address-import', {'text': 'x' * (512 * 1024 + 1)})[0], 400)

    def test_security_idle_conflicts_and_stale_approval(self):
        _, case = self.create(); path, _ = self.server.case(case['id'])
        route = '/api/cases/' + case['id'] + '/address-import'
        self.assertEqual(self.request(route, {'text': A}, headers={'X-Liquid-CSRF': 'wrong'})[0], 403)
        self.server.active_job = 'busy'
        try: self.assertEqual(self.request(route, {'text': A})[0], 409)
        finally: self.server.active_job = None
        reviewed = self.success(route, {'text': A}); set_service(path, B)
        self.assertEqual(self.request(route, {'text': A, 'approve_plan': reviewed['approval_sha256']})[0], 400)
        self.assertNotIn(A, load_services(path)['rules'])

    def test_single_edit_preserves_imported_metadata_and_label_only(self):
        _, case = self.create(); route = '/api/cases/' + case['id']
        text = json.dumps([{'address': A, 'name': 'Case alias', 'stop_tracing': False,
                            'source': 'Client records', 'confidence': 'confirmed', 'observed_at': '2023-10-04'}])
        preview = self.success(route + '/address-import', {'text': text})
        self.success(route + '/address-import', {'text': text, 'approve_plan': preview['approval_sha256']})
        selected = self.success(route + '/address', {'address': A})
        self.assertFalse(selected['service']['stop_tracing'])
        self.assertEqual(selected['service']['source'], 'Client records')
        result = self.success(route + '/services', {'address': A, 'enabled': True, 'name': 'Amended'})
        self.assertNotIn('classification', result['service'])
        self.assertEqual(result['service']['confidence'], 'confirmed')
        self.assertFalse(result['service']['stop_tracing'])
        self.assertEqual(self.success(route + '/addresses', {'suspected_only': True})['total'], 0)


@unittest.skipUnless(importlib.util.find_spec('textual'), 'optional terminal UI')
class MenuImportTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_menu_addresses.AddressMenuTests.asyncSetUp
    click = test_menu_addresses.AddressMenuTests.click

    async def open_import(self, app, pilot):
        app.created(self.case); await pilot.pause()
        await self.click(app, pilot, '#addresses-import')
        await self.click(app, pilot, '#input-import-advanced-attributions')
        return app.screen

    async def test_paste_preview_requires_approval_then_applies_offline(self):
        from textual.widgets import Button, Checkbox, DataTable, TextArea, Static
        app = create_app(self.root)
        with patch('liquid_tracer.menu.subprocess.run') as process:
            async with app.run_test(size=(115, 60)) as pilot:
                screen = await self.open_import(app, pilot)
                screen.query_one('#import-text', TextArea).text = A + '\n' + B
                await pilot.pause(); await self.click(app, pilot, '#import-preview')
                self.assertEqual(screen.query_one('#import-rows', DataTable).row_count, 2)
                self.assertTrue(screen.query_one('#import-apply', Button).disabled)
                self.assertFalse((self.case / 'services.json').exists())
                screen.query_one('#import-approved', Checkbox).value = True
                await pilot.pause(); await self.click(app, pilot, '#import-apply')
                self.assertEqual(len(load_services(self.case)['rules']), 2)
                self.assertIn('Saved 2', str(screen.query_one('#import-summary', Static).render()))
                process.assert_not_called()

    async def test_file_change_after_preview_cannot_apply_stale_batch(self):
        from textual.widgets import Checkbox, Input, Static
        path = self.root / 'attributions.csv'; path.write_text('address,name\n' + A + ',Original\n')
        app = create_app(self.root)
        async with app.run_test(size=(115, 60)) as pilot:
            screen = await self.open_import(app, pilot)
            screen.query_one('#import-file', Input).value = str(path)
            await pilot.pause(); await self.click(app, pilot, '#import-preview')
            screen.query_one('#import-approved', Checkbox).value = True
            await pilot.pause(); path.write_text('address,name\n' + B + ',Changed\n')
            await self.click(app, pilot, '#import-apply')
            self.assertIn('changed', str(screen.query_one('#import-error', Static).render()))
            self.assertFalse((self.case / 'services.json').exists())

    async def test_imported_metadata_is_editable_in_single_address_review(self):
        from textual.widgets import Input, Checkbox, Select
        set_service(self.case, A, name='Imported known service', classification='service', confidence='confirmed',
                    source='Supplied service records', stop_tracing=False)
        app = create_app(self.root)
        async with app.run_test(size=(115, 65)) as pilot:
            app.created(self.case); await pilot.pause()
            await self.click(app, pilot, '#addresses-review')
            screen = app.screen; screen.query_one('#address-value', Input).value = A
            await self.click(app, pilot, '#address-select')
            self.assertEqual(screen.query_one('#service-confidence', Select).value, 'confirmed')
            self.assertFalse(screen.query_one('#service-stop', Checkbox).value)
            screen.query_one('#service-name', Input).value = 'Revised service'
            await self.click(app, pilot, '#service-save')
            rule = load_services(self.case)['rules'][A]
            self.assertEqual((rule['name'], rule['source'], rule['stop_tracing']),
                             ('Revised service', 'Supplied service records', False))
