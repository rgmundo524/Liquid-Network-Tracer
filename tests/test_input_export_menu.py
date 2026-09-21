"""Saved input exports stay local and leave active terminal drafts untouched."""

import csv
import importlib.util
import io
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.change_outputs import set_change_output
from liquid_tracer.common import TraceError, read_json
from liquid_tracer.menu import create_app
from liquid_tracer.name_colors import set_name_colors
from liquid_tracer.services import load_services, set_service
from tests import test_change_outputs_menu, test_menu_addresses
from tests.fixtures import A, B


@unittest.skipUnless(importlib.util.find_spec("textual"), "optional terminal UI")
class InputExportMenuTests(unittest.IsolatedAsyncioTestCase):
    click = test_menu_addresses.AddressMenuTests.click
    lookup_process = test_change_outputs_menu.ChangeOutputMenuTests.lookup_process

    async def asyncSetUp(self):
        await test_menu_addresses.AddressMenuTests.asyncSetUp(self)
        set_service(self.case, "SYNTHETIC-saved", name="Saved name", confidence="confirmed",
                    notes='Saved note, with "quotes"\nand a second line', stop_tracing=False,
                    hop_limit=2, source="Investigator record")
        set_name_colors(self.case, [{"name": "Saved name", "color": "#123456"}],
                        expected_revision=load_services(self.case)["revision"])
        set_change_output(self.case, A, 1, notes="Saved change")

    def unchanged_files(self):
        return {str(path.relative_to(self.case)): path.read_bytes()
                for path in self.case.rglob("*") if path.is_file() and "exports" not in path.parts}

    async def open_case(self, app, pilot):
        app.created(self.case)
        await pilot.pause()

    def exported_path(self, screen, prefix):
        from textual.widgets import Static
        message = str(screen.query_one("#" + prefix + "-export-status", Static).render())
        self.assertIn("Only saved values", message)
        path = Path(message.splitlines()[0].removeprefix("Exported saved inputs: "))
        self.assertTrue(path.is_file())
        return path

    def exported_rows(self, path):
        return list(csv.DictReader(io.StringIO(path.read_text())))

    async def test_case_bundle_before_first_run_needs_no_process_and_never_overwrites(self):
        from textual.widgets import Button
        before = self.unchanged_files()
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run") as process:
            async with app.run_test(size=(115, 65)) as pilot:
                await self.open_case(app, pilot)
                self.assertFalse(app.screen.query_one("#input-export", Button).disabled)
                await self.click(app, pilot, "#input-export")
                first = self.exported_path(app.screen, "input")
                with zipfile.ZipFile(first) as archive:
                    self.assertEqual(set(archive.namelist()),
                                     {"attributions.csv", "name-colors.csv", "change-outputs.csv"})
                    attributions = list(csv.DictReader(io.StringIO(archive.read("attributions.csv").decode())))
                    self.assertEqual(attributions[0]["Name"], "Saved name")
                    self.assertEqual(attributions[0]["hop_limit"], "2")
                    changes = list(csv.DictReader(io.StringIO(archive.read("change-outputs.csv").decode())))
                    self.assertEqual(changes[0]["ChangeVout"], "1")
                await self.click(app, pilot, "#input-export")
                second = self.exported_path(app.screen, "input")
                self.assertNotEqual(first, second)
                self.assertEqual(first.read_bytes(), second.read_bytes())
                self.assertEqual(self.unchanged_files(), before)
                self.assertNotIn("latest_run", read_json(self.case / "case.json"))
                process.assert_not_called()

    async def test_import_exports_preserve_paste_review_and_approval_on_success_and_error(self):
        from textual.widgets import Button, Checkbox, Static, TextArea
        scenarios = [
            (("addresses-import", "input-import-advanced-attributions"), "import", "Address,Name\nSYNTHETIC-draft,Draft name\n", "Name", "Saved name"),
            (("name-colors", "name-color-import", "input-import-advanced-name-colors"), "name-color-import", "Name,Color\nSaved name,#abcdef\n", "Color", "#123456"),
            (("change-outputs", "change-output-import", "input-import-advanced-change-outputs"), "change-import", f"Txid,ChangeVout\n{B},0\n", "Txid", A),
        ]
        for navigation, prefix, draft, column, expected in scenarios:
            with self.subTest(prefix=prefix):
                before = self.unchanged_files()
                app = create_app(self.root)
                with patch("liquid_tracer.menu.subprocess.run") as process:
                    async with app.run_test(size=(115, 65)) as pilot:
                        await self.open_case(app, pilot)
                        for action in navigation:
                            await self.click(app, pilot, "#" + action)
                        screen = app.screen
                        screen.query_one("#" + prefix + "-text", TextArea).text = draft
                        await pilot.pause()
                        await self.click(app, pilot, "#" + prefix + "-preview")
                        self.assertTrue(screen.review["valid"])
                        screen.query_one("#" + prefix + "-approved", Checkbox).value = True
                        await pilot.pause()
                        reviewed = screen.review
                        summary = str(screen.query_one("#" + prefix + "-summary", Static).render())
                        await self.click(app, pilot, "#" + prefix + "-export")
                        rows = self.exported_rows(self.exported_path(screen, prefix))
                        self.assertEqual(len(rows), 1)
                        self.assertEqual(rows[0][column], expected)
                        self.assertIs(screen.review, reviewed)
                        self.assertTrue(screen.query_one("#" + prefix + "-approved", Checkbox).value)
                        self.assertFalse(screen.query_one("#" + prefix + "-apply", Button).disabled)
                        with patch("liquid_tracer.input_export.save_input_export", side_effect=TraceError("Disk unavailable")):
                            await self.click(app, pilot, "#" + prefix + "-export")
                        self.assertIn("Disk unavailable", str(screen.query_one("#" + prefix + "-export-status", Static).render()))
                        self.assertIs(screen.review, reviewed)
                        self.assertTrue(screen.query_one("#" + prefix + "-approved", Checkbox).value)
                        self.assertFalse(screen.query_one("#" + prefix + "-apply", Button).disabled)
                        self.assertEqual(screen.query_one("#" + prefix + "-text", TextArea).text, draft)
                        self.assertEqual(str(screen.query_one("#" + prefix + "-summary", Static).render()), summary)
                        self.assertEqual(self.unchanged_files(), before)
                        process.assert_not_called()

    async def test_address_editor_exports_saved_rows_without_saving_its_draft(self):
        from textual.widgets import Input, TextArea
        before = self.unchanged_files()
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run") as process:
            async with app.run_test(size=(115, 65)) as pilot:
                await self.open_case(app, pilot)
                await self.click(app, pilot, "#addresses-review")
                screen = app.screen
                screen.query_one("#address-value", Input).value = "SYNTHETIC-saved"
                await self.click(app, pilot, "#address-select")
                screen.query_one("#service-name", Input).value = "Unsaved name"
                screen.query_one("#service-rationale", TextArea).text = "Unsaved notes"
                await self.click(app, pilot, "#address-export")
                rows = self.exported_rows(self.exported_path(screen, "address"))
                self.assertEqual(rows[0]["Name"], "Saved name")
                self.assertIn("second line", rows[0]["notes"])
                self.assertEqual(screen.selected_address, "SYNTHETIC-saved")
                self.assertEqual(screen.query_one("#service-name", Input).value, "Unsaved name")
                self.assertEqual(screen.query_one("#service-rationale", TextArea).text, "Unsaved notes")
                self.assertEqual(self.unchanged_files(), before)
                process.assert_not_called()

    async def test_name_color_editor_exports_saved_color_and_keeps_selected_draft(self):
        from textual.widgets import Button, DataTable, Input
        before = self.unchanged_files()
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run") as process:
            async with app.run_test(size=(115, 65)) as pilot:
                await self.open_case(app, pilot)
                await self.click(app, pilot, "#name-colors")
                screen = app.screen
                screen.query_one("#name-color-rows", DataTable).focus()
                await pilot.press("enter")
                await pilot.pause()
                selected = screen.selected
                screen.query_one("#name-color-value", Input).value = "#abcdef"
                await self.click(app, pilot, "#name-color-export")
                rows = self.exported_rows(self.exported_path(screen, "name-color"))
                self.assertEqual(rows[0]["Color"], "#123456")
                self.assertIs(screen.selected, selected)
                self.assertEqual(screen.query_one("#name-color-value", Input).value, "#abcdef")
                self.assertFalse(screen.query_one("#name-color-save", Button).disabled)
                self.assertEqual(self.unchanged_files(), before)
                process.assert_not_called()

    async def test_change_editor_export_keeps_lookup_selection_and_notes(self):
        from textual.widgets import Button, Input, Select, TextArea
        before = self.unchanged_files()
        app = create_app(self.root)
        with patch("liquid_tracer.change_outputs.lookup_requires_network", return_value=False), \
                patch("liquid_tracer.menu.subprocess.run", side_effect=self.lookup_process) as process:
            async with app.run_test(size=(115, 65)) as pilot:
                await self.open_case(app, pilot)
                await self.click(app, pilot, "#change-outputs")
                screen = app.screen
                screen.query_one("#change-output-txid", Input).value = A
                await pilot.pause()
                await self.click(app, pilot, "#change-output-lookup")
                lookup = screen.lookup
                screen.query_one("#change-output-vout", Select).value = 0
                screen.query_one("#change-output-notes", TextArea).text = "Unsaved change note"
                process.reset_mock()
                await self.click(app, pilot, "#change-output-export")
                rows = self.exported_rows(self.exported_path(screen, "change-output"))
                self.assertEqual(rows[0]["ChangeVout"], "1")
                self.assertEqual(rows[0]["Notes"], "Saved change")
                self.assertIs(screen.lookup, lookup)
                self.assertEqual(screen.query_one("#change-output-vout", Select).value, 0)
                self.assertEqual(screen.query_one("#change-output-notes", TextArea).text, "Unsaved change note")
                self.assertFalse(screen.query_one("#change-output-save", Button).disabled)
                self.assertEqual(self.unchanged_files(), before)
                process.assert_not_called()
