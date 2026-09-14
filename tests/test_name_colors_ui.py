"""Real local HTTP and terminal interactions for post-import name colors."""
import importlib.util
import unittest
from unittest.mock import patch

from liquid_tracer.common import read_json
from liquid_tracer.menu import create_app
from liquid_tracer.services import load_services, set_service
from tests import test_web, test_menu_addresses


class WebNameColorTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    create = test_web.LocalWebTests.create

    def imported(self):
        _, case = self.create(); path, _ = self.server.case(case['id'])
        route = '/api/cases/' + case['id']
        payload = {'text': 'Address,Name,confidence,stop_tracing\nSYNTHETIC-one,Perp,Suspected,true\nSYNTHETIC-two,PERP,Confirmed,false\n'}
        preview = self.success(route + '/address-import', payload)
        self.success(route + '/address-import', {**payload, 'approve_plan': preview['approval_sha256']})
        return route, path

    def test_import_list_choose_color_and_clear_without_run(self):
        route, case = self.imported()
        before = load_services(case)['rules']
        with patch.object(self.server, 'start_job') as process:
            catalog = self.success(route + '/name-colors', {})
            self.assertEqual(catalog['total'], 1)
            self.assertEqual(catalog['rows'][0]['addresses'], 2)
            self.success(route + '/name-colors', {'updates': [{'name': 'pErP', 'color': '#F0ABFC'}],
                         'expected_revision': catalog['revision']})
            self.assertEqual(load_services(case)['name_colors'], {'perp': '#f0abfc'})
            self.assertEqual(load_services(case)['rules'], before)
            self.assertNotIn('latest_run', read_json(case / 'case.json'))
            catalog = self.success(route + '/name-colors', {'query': 'PERP'})
            self.success(route + '/name-colors', {'updates': [{'name': 'Perp', 'color': None}],
                         'expected_revision': catalog['revision']})
            self.assertEqual(load_services(case)['name_colors'], {})
            process.assert_not_called()

    def test_rejects_csrf_busy_stale_invalid_and_injected_requests(self):
        route, case = self.imported(); endpoint = route + '/name-colors'
        self.assertEqual(self.request(endpoint, {}, headers={'X-Liquid-CSRF': 'wrong'})[0], 403)
        self.server.active_job = 'busy'
        try: self.assertEqual(self.request(endpoint, {})[0], 409)
        finally: self.server.active_job = None
        catalog = self.success(endpoint, {})
        before = (case / 'services.json').read_bytes()
        for body in ({'file': '/private'}, {'limit': 0}, {'offset': -1}, {'limit': True},
                     {'updates': [{'name': 'Perp', 'color': '#fff"/><script>'}], 'expected_revision': catalog['revision']},
                     {'updates': [{'name': 'Perp', 'color': '#123456'}], 'expected_revision': True},
                     {'updates': [{'name': 'Perp', 'color': '#123456'}]},
                     {'updates': [{'name': 'other', 'color': '#123456'}], 'expected_revision': catalog['revision']}):
            with self.subTest(body=body): self.assertEqual(self.request(endpoint, body)[0], 400)
            self.assertEqual((case / 'services.json').read_bytes(), before)
        set_service(case, 'SYNTHETIC-third', name='Third')
        self.assertEqual(self.request(endpoint, {'updates': [{'name': 'Perp', 'color': '#123456'}],
                         'expected_revision': catalog['revision']})[0], 400)
        self.assertNotIn('name_colors', load_services(case))


@unittest.skipUnless(importlib.util.find_spec('textual'), 'optional terminal UI')
class MenuNameColorTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_menu_addresses.AddressMenuTests.asyncSetUp
    click = test_menu_addresses.AddressMenuTests.click

    async def select_row(self, app, pilot):
        from textual.widgets import DataTable
        table = app.screen.query_one('#name-color-rows', DataTable)
        table.focus(); table.move_cursor(row=0)
        await pilot.press('enter'); await pilot.pause()

    async def test_post_import_button_palette_save_and_clear_offline(self):
        from textual.widgets import Checkbox, DataTable, Input, Select, Static, TextArea
        app = create_app(self.root)
        with patch('liquid_tracer.menu.subprocess.run') as process:
            async with app.run_test(size=(115, 65)) as pilot:
                app.created(self.case); await pilot.pause()
                await self.click(app, pilot, '#addresses-import')
                app.screen.query_one('#import-text', TextArea).text = 'Address,Name\nSYNTHETIC-one,BTSE\nSYNTHETIC-two,btse\n'
                await pilot.pause(); await self.click(app, pilot, '#import-preview')
                app.screen.query_one('#import-approved', Checkbox).value = True
                await pilot.pause(); await self.click(app, pilot, '#import-apply')
                await self.click(app, pilot, '#import-name-colors')
                self.assertEqual(app.screen.query_one('#name-color-rows', DataTable).row_count, 1)
                await self.select_row(app, pilot)
                app.screen.query_one('#name-color-palette', Select).value = '#f0abfc'
                await pilot.pause()
                self.assertEqual(app.screen.query_one('#name-color-value', Input).value, '#f0abfc')
                await self.click(app, pilot, '#name-color-save')
                self.assertEqual(load_services(self.case)['name_colors'], {'btse': '#f0abfc'})
                self.assertIn('Saved 1', str(app.screen.query_one('#name-color-error', Static).render()))
                await self.select_row(app, pilot); await self.click(app, pilot, '#name-color-clear')
                self.assertEqual(load_services(self.case)['name_colors'], {})
                process.assert_not_called()

    async def test_case_and_review_menu_entrypoints_and_invalid_color(self):
        from textual.widgets import Input, Static
        set_service(self.case, 'SYNTHETIC-one', name='Perp')
        app = create_app(self.root)
        async with app.run_test(size=(100, 48)) as pilot:
            app.created(self.case); await pilot.pause()
            await self.click(app, pilot, '#name-colors'); await self.select_row(app, pilot)
            app.screen.query_one('#name-color-value', Input).value = 'invalid'
            await self.click(app, pilot, '#name-color-save')
            self.assertIn('#RRGGBB', str(app.screen.query_one('#name-color-error', Static).render()))
            self.assertNotIn('name_colors', load_services(self.case))
            await self.click(app, pilot, '#name-color-back')
            await self.click(app, pilot, '#addresses-review')
            await self.click(app, pilot, '#review-name-colors')
            self.assertIsNotNone(app.screen.query_one('#name-color-rows'))
