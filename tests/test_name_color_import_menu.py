"""Terminal imports require review, refresh editors, and reject stale files."""

import importlib.util
import unittest
from unittest.mock import patch

from liquid_tracer.common import read_json
from liquid_tracer.menu import create_app
from liquid_tracer.name_colors import set_name_colors
from liquid_tracer.services import load_services, set_service
from tests import test_menu_addresses


@unittest.skipUnless(importlib.util.find_spec("textual"), "optional terminal UI")
class NameColorImportMenuTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_menu_addresses.AddressMenuTests.asyncSetUp
    click = test_menu_addresses.AddressMenuTests.click

    async def open_import(self, app, pilot):
        app.created(self.case)
        await pilot.pause()
        await self.click(app, pilot, "#name-colors")
        await self.click(app, pilot, "#name-color-import")
        await self.click(app, pilot, "#input-import-advanced-name-colors")
        return app.screen

    async def test_reviewed_paste_is_local_preserves_roles_and_refreshes_parent(self):
        from textual.widgets import Button, Checkbox, DataTable, Input, Static, TextArea
        set_service(self.case, "SYNTHETIC-one", name="BTSE")
        set_service(self.case, "SYNTHETIC-two", name="btse")
        settings = load_services(self.case)
        set_name_colors(self.case, [{"role": "seed", "color": "#123456"}],
                        expected_revision=settings["revision"])
        before = (self.case / "services.json").read_bytes()
        rules = load_services(self.case)["rules"]
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run") as process:
            async with app.run_test(size=(115, 65)) as pilot:
                screen = await self.open_import(app, pilot)
                screen.query_one("#name-color-import-text", TextArea).text = "NAME,COLOR\nbTsE,#93C5FD\n"
                await pilot.pause()
                await self.click(app, pilot, "#name-color-import-preview")
                self.assertEqual(screen.query_one("#name-color-import-rows", DataTable).row_count, 1)
                self.assertEqual(screen.review["changes"][0]["addresses"], 2)
                self.assertTrue(screen.query_one("#name-color-import-apply", Button).disabled)
                self.assertEqual((self.case / "services.json").read_bytes(), before)
                screen.query_one("#name-color-import-approved", Checkbox).value = True
                await pilot.pause()
                await self.click(app, pilot, "#name-color-import-apply")
                self.assertEqual(load_services(self.case)["name_colors"], {"btse": "#93c5fd"})
                self.assertEqual(load_services(self.case)["rules"], rules)
                self.assertEqual(load_services(self.case)["role_colors"], {"seed": "#123456"})
                self.assertNotIn("latest_run", read_json(self.case / "case.json"))
                self.assertIn("Saved 1", str(screen.query_one("#name-color-import-summary", Static).render()))
                await self.click(app, pilot, "#name-color-import-back")
                self.assertEqual(app.screen.report["rows"][0]["color"], "#93c5fd")
                self.assertEqual(app.screen.report["revision"], load_services(self.case)["revision"])
                table = app.screen.query_one("#name-color-rows", DataTable)
                table.focus()
                table.move_cursor(row=0)
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(app.screen.query_one("#name-color-value", Input).value, "#93c5fd")
                app.screen.query_one("#name-color-value", Input).value = "#112233"
                await self.click(app, pilot, "#name-color-save")
                self.assertEqual(load_services(self.case)["name_colors"], {"btse": "#112233"})
                process.assert_not_called()

    async def test_policy_and_input_edits_invalidate_approval_then_clear(self):
        from textual.widgets import Button, Checkbox, DataTable, Input, Select, TextArea
        set_service(self.case, "SYNTHETIC-one", name="BTSE")
        set_name_colors(self.case, [{"name": "BTSE", "color": "#123456"}],
                        expected_revision=load_services(self.case)["revision"])
        app = create_app(self.root)
        async with app.run_test(size=(115, 65)) as pilot:
            screen = await self.open_import(app, pilot)
            screen.query_one("#name-color-import-text", TextArea).text = "Name,Color\nBTSE,\n"
            await pilot.pause()
            await self.click(app, pilot, "#name-color-import-preview")
            self.assertEqual(screen.review["changes"][0]["action"], "keep")
            screen.query_one("#name-color-import-approved", Checkbox).value = True
            await pilot.pause()
            screen.query_one("#name-color-import-replace", Checkbox).value = True
            await pilot.pause()
            self.assertIsNone(screen.review)
            self.assertFalse(screen.query_one("#name-color-import-approved", Checkbox).value)
            self.assertTrue(screen.query_one("#name-color-import-apply", Button).disabled)
            self.assertEqual(screen.query_one("#name-color-import-rows", DataTable).row_count, 0)
            await self.click(app, pilot, "#name-color-import-preview")
            self.assertEqual(screen.review["changes"][0]["action"], "clear")
            for selector, widget, value in (
                ("#name-color-import-format", Select, "csv"),
                ("#name-color-import-text", TextArea, "Name,Color\nBtSe,\n"),
                ("#name-color-import-file", Input, str(self.root / "colors.csv")),
            ):
                screen.query_one("#name-color-import-approved", Checkbox).value = True
                await pilot.pause()
                target = screen.query_one(selector, widget)
                if widget is TextArea:
                    target.text = value
                else:
                    target.value = value
                await pilot.pause()
                self.assertIsNone(screen.review)
                self.assertFalse(screen.query_one("#name-color-import-approved", Checkbox).value)
                self.assertTrue(screen.query_one("#name-color-import-apply", Button).disabled)
                if widget is Input:
                    target.value = ""
                    await pilot.pause()
                await self.click(app, pilot, "#name-color-import-preview")
            screen.query_one("#name-color-import-approved", Checkbox).value = True
            await pilot.pause()
            await self.click(app, pilot, "#name-color-import-apply")
            self.assertEqual(load_services(self.case)["name_colors"], {})

    async def test_file_reloaded_and_changed_file_rejects_stale_approval(self):
        from textual.widgets import Button, Checkbox, Input, Static, TextArea
        set_service(self.case, "SYNTHETIC-one", name="BTSE")
        path = self.root / "colors.csv"
        path.write_text("Name,Color\nBTSE,#123456\n")
        before = (self.case / "services.json").read_bytes()
        app = create_app(self.root)
        async with app.run_test(size=(115, 65)) as pilot:
            screen = await self.open_import(app, pilot)
            screen.query_one("#name-color-import-text", TextArea).text = "invalid ignored pasted content"
            screen.query_one("#name-color-import-file", Input).value = str(path)
            await pilot.pause()
            await self.click(app, pilot, "#name-color-import-preview")
            self.assertTrue(screen.review["valid"])
            screen.query_one("#name-color-import-approved", Checkbox).value = True
            await pilot.pause()
            path.write_text("Name,Color\nBTSE,#654321\n")
            await self.click(app, pilot, "#name-color-import-apply")
            self.assertIn("changed", str(screen.query_one("#name-color-import-error", Static).render()))
            self.assertEqual((self.case / "services.json").read_bytes(), before)
            self.assertIsNone(screen.review)
            self.assertTrue(screen.query_one("#name-color-import-apply", Button).disabled)

    async def test_invalid_rows_prevent_partial_application_and_show_errors(self):
        from textual.widgets import Button, Checkbox, Static, TextArea
        set_service(self.case, "SYNTHETIC-one", name="BTSE")
        before = (self.case / "services.json").read_bytes()
        app = create_app(self.root)
        async with app.run_test(size=(115, 65)) as pilot:
            screen = await self.open_import(app, pilot)
            screen.query_one("#name-color-import-text", TextArea).text = (
                "Name,Color\nBTSE,#123456\nUnknown name,#abcdef\n")
            await pilot.pause()
            await self.click(app, pilot, "#name-color-import-preview")
            self.assertFalse(screen.review["valid"])
            self.assertTrue(screen.review["errors"])
            self.assertIn("Row", str(screen.query_one("#name-color-import-error", Static).render()))
            screen.query_one("#name-color-import-approved", Checkbox).value = True
            await pilot.pause()
            self.assertTrue(screen.query_one("#name-color-import-apply", Button).disabled)
            self.assertEqual((self.case / "services.json").read_bytes(), before)
