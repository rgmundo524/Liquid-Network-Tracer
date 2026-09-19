"""Terminal change selection and import use explicit review and no live boards."""

import contextlib
import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.change_outputs import set_change_output
from liquid_tracer.common import read_json, save_json
from liquid_tracer.inspection import transaction_outputs
from liquid_tracer.menu import create_app
from liquid_tracer.services import load_services, set_service
from tests import test_menu_addresses
from tests.fixtures import A, B, fixture


@unittest.skipUnless(importlib.util.find_spec("textual"), "optional terminal UI")
class ChangeOutputMenuTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_menu_addresses.AddressMenuTests.asyncSetUp
    click = test_menu_addresses.AddressMenuTests.click

    async def open_editor(self, app, pilot):
        app.created(self.case)
        await pilot.pause()
        await self.click(app, pilot, "#change-outputs")
        return app.screen

    async def open_import(self, app, pilot):
        await self.open_editor(app, pilot)
        await self.click(app, pilot, "#change-output-import")
        return app.screen

    def lookup_process(self, command, **kwargs):
        txid = command[command.index("--txid") + 1]
        report_path = Path(command[command.index("--output") + 1])
        settings = load_services(self.case)
        current = settings.get("change_outputs", {}).get(txid, {})
        save_json(report_path, {**transaction_outputs(txid, fixture()["/tx/" + txid]),
                               "current_vout": current.get("vout"), "current_notes": current.get("notes", ""),
                               "revision": settings["revision"], "notice": "Synthetic fixture lookup"})
        return subprocess.CompletedProcess(command, 0, "", "")

    async def lookup(self, app, pilot, txid=A):
        from textual.widgets import Input
        app.screen.query_one("#change-output-txid", Input).value = txid
        await pilot.pause()
        await self.click(app, pilot, "#change-output-lookup")

    async def test_select_one_spendable_change_then_clear_without_tracing(self):
        from textual.widgets import Button, DataTable, Input, Select, Static, TextArea
        set_service(self.case, "SYNTHETIC-address", name="Evidence unchanged")
        rules = load_services(self.case)["rules"]
        app = create_app(self.root)
        with patch("liquid_tracer.change_outputs.lookup_requires_network", return_value=False), \
                patch("liquid_tracer.menu.subprocess.run", side_effect=self.lookup_process) as process:
            async with app.run_test(size=(115, 65)) as pilot:
                screen = await self.open_editor(app, pilot)
                process.assert_not_called()
                self.assertTrue(screen.query_one("#change-output-save", Button).disabled)
                await self.lookup(app, pilot, A.upper())
                self.assertEqual(screen.lookup["txid"], A)
                self.assertEqual(screen.query_one("#change-output-rows", DataTable).row_count, 3)
                self.assertEqual(screen.query_one("#change-output-vout", Select).value, "")
                table = screen.query_one("#change-output-rows", DataTable)
                table.focus()
                table.move_cursor(row=2)
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(screen.query_one("#change-output-vout", Select).value, "")
                self.assertIn("Only spendable", str(screen.query_one("#change-output-error", Static).render()))
                table.move_cursor(row=1)
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(screen.query_one("#change-output-vout", Select).value, 1)
                screen.query_one("#change-output-notes", TextArea).text = "Investigator designation"
                await self.click(app, pilot, "#change-output-save")
                self.assertEqual(load_services(self.case)["change_outputs"][A]["vout"], 1)
                self.assertEqual(load_services(self.case)["change_outputs"][A]["notes"], "Investigator designation")
                self.assertEqual(load_services(self.case)["rules"], rules)
                self.assertNotIn("latest_run", read_json(self.case / "case.json"))
                self.assertIn("Sync and reorganize", str(screen.query_one("#change-output-error", Static).render()))
                saved = screen.query_one("#change-output-saved", DataTable)
                saved.focus()
                saved.move_cursor(row=0)
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(screen.query_one("#change-output-vout", Select).value, 1)
                screen.query_one("#change-output-txid", Input).value = B
                await pilot.pause()
                self.assertIsNone(screen.lookup)
                self.assertTrue(screen.query_one("#change-output-save", Button).disabled)
                await self.lookup(app, pilot)
                await self.click(app, pilot, "#change-output-clear")
                self.assertEqual(load_services(self.case)["change_outputs"], {})
                self.assertNotIn("secretspec", process.call_args.args[0][0])

    async def test_live_lookup_suspends_for_credentials_and_stale_save_is_rejected(self):
        from textual.widgets import Select, Static
        app = create_app(self.root)
        with patch("liquid_tracer.change_outputs.lookup_requires_network", return_value=True), \
                patch("liquid_tracer.menu.subprocess.run", side_effect=self.lookup_process) as process, \
                patch.object(app, "suspend", side_effect=contextlib.nullcontext) as suspend:
            async with app.run_test(size=(115, 65)) as pilot:
                screen = await self.open_editor(app, pilot)
                await self.lookup(app, pilot)
                suspend.assert_called_once()
                self.assertEqual(process.call_args.args[0][0], "/nix/store/test-secretspec/bin/secretspec")
                self.assertNotIn("capture_output", process.call_args.kwargs)
                self.assertEqual(process.call_args.args[0][process.call_args.args[0].index("--profile") + 1], "development")
                screen.query_one("#change-output-vout", Select).value = 0
                set_change_output(self.case, B, 1)
                await self.click(app, pilot, "#change-output-save")
                self.assertIn("changed", str(screen.query_one("#change-output-error", Static).render()))
                self.assertNotIn(A, load_services(self.case)["change_outputs"])

    async def test_reviewed_paste_refreshes_parent_and_input_changes_invalidate(self):
        from textual.widgets import Button, Checkbox, DataTable, Select, TextArea
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run") as process:
            async with app.run_test(size=(115, 65)) as pilot:
                screen = await self.open_import(app, pilot)
                screen.query_one("#change-import-text", TextArea).text = f"Txid,ChangeVout,Notes\n{A},0,Change assessment\n"
                await pilot.pause()
                await self.click(app, pilot, "#change-import-preview")
                self.assertTrue(screen.review["valid"])
                self.assertTrue(screen.query_one("#change-import-apply", Button).disabled)
                self.assertEqual(screen.query_one("#change-import-rows", DataTable).row_count, 1)
                screen.query_one("#change-import-approved", Checkbox).value = True
                await pilot.pause()
                screen.query_one("#change-import-format", Select).value = "csv"
                await pilot.pause()
                self.assertIsNone(screen.review)
                self.assertFalse(screen.query_one("#change-import-approved", Checkbox).value)
                self.assertTrue(screen.query_one("#change-import-apply", Button).disabled)
                await self.click(app, pilot, "#change-import-preview")
                screen.query_one("#change-import-approved", Checkbox).value = True
                await pilot.pause()
                await self.click(app, pilot, "#change-import-apply")
                self.assertEqual(load_services(self.case)["change_outputs"][A]["vout"], 0)
                await self.click(app, pilot, "#change-import-back")
                self.assertEqual(app.screen.report["revision"], load_services(self.case)["revision"])
                self.assertEqual(app.screen.report["rows"][0]["txid"], A)
                self.assertEqual(app.screen.report["rows"][0]["vout"], 0)
                process.assert_not_called()

    async def test_import_file_is_reread_and_clear_requires_replace(self):
        from textual.widgets import Button, Checkbox, Input, Static
        set_change_output(self.case, A, 1)
        path = self.root / "changes.csv"
        path.write_text(f"Txid,ChangeVout\n{A},\n")
        before = (self.case / "services.json").read_bytes()
        app = create_app(self.root)
        async with app.run_test(size=(115, 65)) as pilot:
            screen = await self.open_import(app, pilot)
            screen.query_one("#change-import-file", Input).value = str(path)
            await pilot.pause()
            await self.click(app, pilot, "#change-import-preview")
            self.assertEqual(screen.review["changes"][0]["action"], "keep")
            screen.query_one("#change-import-approved", Checkbox).value = True
            await pilot.pause()
            screen.query_one("#change-import-replace", Checkbox).value = True
            await pilot.pause()
            self.assertIsNone(screen.review)
            self.assertTrue(screen.query_one("#change-import-apply", Button).disabled)
            await self.click(app, pilot, "#change-import-preview")
            self.assertEqual(screen.review["changes"][0]["action"], "clear")
            screen.query_one("#change-import-approved", Checkbox).value = True
            await pilot.pause()
            path.write_text(f"Txid,ChangeVout\n{A},0\n")
            await self.click(app, pilot, "#change-import-apply")
            self.assertIn("changed", str(screen.query_one("#change-import-error", Static).render()))
            self.assertEqual((self.case / "services.json").read_bytes(), before)
            self.assertIsNone(screen.review)
            path.write_text(f"Txid,ChangeVout\n{A},\n")
            await self.click(app, pilot, "#change-import-preview")
            screen.query_one("#change-import-approved", Checkbox).value = True
            await pilot.pause()
            await self.click(app, pilot, "#change-import-apply")
            self.assertEqual(load_services(self.case)["change_outputs"], {})

    async def test_invalid_import_prevents_partial_save(self):
        from textual.widgets import Button, Checkbox, Static, TextArea
        app = create_app(self.root)
        async with app.run_test(size=(115, 65)) as pilot:
            screen = await self.open_import(app, pilot)
            screen.query_one("#change-import-text", TextArea).text = f"Txid,ChangeVout\n{A},0\nnot-a-hash,1\n"
            await pilot.pause()
            await self.click(app, pilot, "#change-import-preview")
            self.assertFalse(screen.review["valid"])
            self.assertIn("Row", str(screen.query_one("#change-import-error", Static).render()))
            screen.query_one("#change-import-approved", Checkbox).value = True
            await pilot.pause()
            self.assertTrue(screen.query_one("#change-import-apply", Button).disabled)
            self.assertEqual(load_services(self.case).get("change_outputs", {}), {})
