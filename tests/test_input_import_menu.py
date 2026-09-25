"""The shared terminal CSV workflow reviews and saves all files together."""

import importlib.util
import unittest
from unittest.mock import patch

from liquid_tracer.menu import create_app
from liquid_tracer.services import load_services, set_service
from tests import test_menu_addresses


ADDRESS = "SYNTHETIC-batch-address"
TXID = "a" * 64


@unittest.skipUnless(importlib.util.find_spec("textual"), "optional terminal UI")
class InputImportMenuTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_menu_addresses.AddressMenuTests.asyncSetUp
    click = test_menu_addresses.AddressMenuTests.click

    def write_csv(self, name, text):
        path = self.root / name
        path.write_text(text)
        return path

    def files(self):
        # Deliberately put colors first to verify names from the same batch work.
        return [self.write_csv("colors.csv", "Name,Color\nNew service,#123456\n"),
                self.write_csv("addresses.csv", f"Address,Name,stop_tracing,hop_limit\n{ADDRESS},New service,false,4\n"),
                self.write_csv("changes.csv", f"Txid,ChangeVout,Notes\n{TXID},1,Investigator designation\n")]

    async def open_import(self, app, pilot, *, parent=None):
        app.created(self.case)
        await pilot.pause()
        if parent:
            await self.click(app, pilot, "#" + parent[0])
        await self.click(app, pilot, "#" + (parent[1] if parent else "addresses-import"))
        return app.screen

    async def set_paths(self, screen, pilot, paths):
        from textual.widgets import TextArea
        screen.query_one("#input-import-paths", TextArea).text = "\n".join(str(path) for path in paths)
        await pilot.pause()

    async def test_one_review_imports_three_types_in_any_order_without_starting_run(self):
        from textual.widgets import Button, DataTable, Static
        paths = self.files()
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run") as process:
            async with app.run_test(size=(120, 65)) as pilot:
                screen = await self.open_import(app, pilot)
                await self.set_paths(screen, pilot, paths)
                await self.click(app, pilot, "#input-import-preview")
                self.assertTrue(screen.review["valid"])
                self.assertEqual(screen.query_one("#input-import-files", DataTable).row_count, 3)
                self.assertEqual(screen.query_one("#input-import-rows", DataTable).row_count, 3)
                self.assertFalse(screen.query_one("#input-import-apply", Button).disabled)
                self.assertFalse((self.case / "services.json").exists())
                file_table = screen.query_one("#input-import-files", DataTable)
                file_table.focus()
                file_table.move_cursor(row=0)
                await pilot.press("enter")
                await pilot.pause()
                self.assertIn("colors.csv", str(screen.query_one("#input-import-detail", Static).render()))
                row_table = screen.query_one("#input-import-rows", DataTable)
                row_table.focus()
                row_table.move_cursor(row=1)
                await pilot.press("enter")
                await pilot.pause()
                self.assertIn('"hop_limit": 4', str(screen.query_one("#input-import-detail", Static).render()))
                await self.click(app, pilot, "#input-import-apply")
                settings = load_services(self.case)
                self.assertEqual(settings["rules"][ADDRESS]["hop_limit"], 4)
                self.assertFalse(settings["rules"][ADDRESS]["stop_tracing"])
                self.assertEqual(settings["name_colors"], {"new service": "#123456"})
                self.assertEqual(settings["change_outputs"][TXID]["vout"], 1)
                self.assertIn("Saved 3", str(screen.query_one("#input-import-summary", Static).render()))
                self.assertIsNone(screen.review)
                self.assertTrue(screen.query_one("#input-import-apply", Button).disabled)
                process.assert_not_called()

    async def test_policy_and_type_changes_invalidate_and_replace_updates_existing_hop_limit(self):
        from textual.widgets import Button, Checkbox, Select
        set_service(self.case, ADDRESS, name="New service", stop_tracing=False, hop_limit=1)
        path = self.files()[1]
        app = create_app(self.root)
        async with app.run_test(size=(120, 65)) as pilot:
            screen = await self.open_import(app, pilot)
            await self.set_paths(screen, pilot, [path])
            await self.click(app, pilot, "#input-import-preview")
            self.assertEqual(screen.review["counts"]["keep"], 1)
            screen.query_one("#input-import-replace", Checkbox).value = True
            await pilot.pause()
            self.assertIsNone(screen.review)
            self.assertTrue(screen.query_one("#input-import-apply", Button).disabled)
            await self.click(app, pilot, "#input-import-preview")
            self.assertEqual(screen.review["counts"]["replace"], 1)
            screen.query_one("#input-import-kind-0", Select).value = "attributions"
            await pilot.pause()
            self.assertIsNone(screen.review)
            self.assertTrue(screen.query_one("#input-import-apply", Button).disabled)
            await self.click(app, pilot, "#input-import-preview")
            await self.click(app, pilot, "#input-import-apply")
            self.assertEqual(load_services(self.case)["rules"][ADDRESS]["hop_limit"], 4)

    async def test_changed_file_rejects_whole_batch_and_paths_invalidate_review(self):
        from textual.widgets import Button, Checkbox, Static
        paths = self.files()
        app = create_app(self.root)
        async with app.run_test(size=(120, 65)) as pilot:
            screen = await self.open_import(app, pilot)
            await self.set_paths(screen, pilot, paths)
            await self.click(app, pilot, "#input-import-preview")
            paths[0].write_text("Name,Color\nNew service,#abcdef\n")
            await self.click(app, pilot, "#input-import-apply")
            self.assertIn("changed", str(screen.query_one("#input-import-error", Static).render()))
            self.assertFalse((self.case / "services.json").exists())
            await self.click(app, pilot, "#input-import-preview")
            await self.set_paths(screen, pilot, paths[:2])
            self.assertIsNone(screen.review)
            self.assertTrue(screen.query_one("#input-import-apply", Button).disabled)

    async def test_invalid_second_file_prevents_apply_and_identifies_source(self):
        from textual.widgets import Button, Static
        paths = self.files()
        paths[0].write_text("Name,Color\nUnknown service,#123456\n")
        app = create_app(self.root)
        async with app.run_test(size=(120, 65)) as pilot:
            screen = await self.open_import(app, pilot)
            await self.set_paths(screen, pilot, paths)
            await self.click(app, pilot, "#input-import-preview")
            self.assertFalse(screen.review["valid"])
            self.assertIn("colors.csv", str(screen.query_one("#input-import-error", Static).render()))
            self.assertTrue(screen.query_one("#input-import-apply", Button).disabled)
            self.assertFalse((self.case / "services.json").exists())

    async def test_editor_shortcuts_open_shared_import_and_refresh_on_return(self):
        for parent in (("name-colors", "name-color-import"), ("change-outputs", "change-output-import")):
            with self.subTest(parent=parent[0]):
                app = create_app(self.root)
                async with app.run_test(size=(120, 65)) as pilot:
                    screen = await self.open_import(app, pilot, parent=parent)
                    await self.set_paths(screen, pilot, self.files())
                    await self.click(app, pilot, "#input-import-preview")
                    await self.click(app, pilot, "#input-import-apply")
                    await self.click(app, pilot, "#input-import-back")
                    self.assertEqual(app.screen.report["revision"], load_services(self.case)["revision"])
                    self.assertEqual(len(app.screen.report["rows"]), 1)
