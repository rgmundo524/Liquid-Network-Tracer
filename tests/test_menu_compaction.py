"""Compaction is reviewed locally before the exact saved layout is sent to Miro."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.investigations import create_investigation, read_case, update_case
from liquid_tracer.menu import create_app


PROJECT = Path(__file__).resolve().parents[1]
HAS_TEXTUAL = importlib.util.find_spec("textual") is not None


@unittest.skipUnless(HAS_TEXTUAL, "Install the optional [tui] extra for Textual interaction tests")
class CompactionMenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from liquid_tracer.cli import main
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "cases"
        fixture = PROJECT / "tests" / "data" / "synthetic-api.json"
        self.case = create_investigation(self.root, "Compaction fixture", board="SYNTHETIC-BOARD=",
                                         fixture=str(fixture), run_defaults={"max_new_items": 321})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["trace", "--case", str(self.case), "--fixture", str(fixture),
                                   "--seeds-file", str(PROJECT / "tests" / "data" / "synthetic-seeds.txt"),
                                   "--hops", "1"]), 0)
        self.run_id = read_case(self.case)["latest_run"]
        self.preview_id = self.run_id + "-compact-abcd1234"
        self.before = self.run_files()
        self.saved_preview = None
        self.recovery = {"pending_count": 0, "can_confirm_empty": False}
        self.environment = patch.dict(os.environ, {"LIQUID_TRACER_ROOT": str(PROJECT),
            "LIQUID_SECRET_PROVIDER": "protonpass", "LIQUID_SECRET_PROFILE": "development",
            "LIQUID_SECRETSPEC_BIN": "/nix/store/test-secretspec/bin/secretspec"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.preview_lookup = patch("liquid_tracer.cli.latest_compaction_preview",
                                    side_effect=lambda *a, **k: self.saved_preview, create=True)
        self.preview_lookup.start()
        self.addCleanup(self.preview_lookup.stop)
        self.preview_verify = patch("liquid_tracer.cli.verified_compaction_preview",
                                    return_value=({}, {}), create=True)
        self.verify = self.preview_verify.start()
        self.addCleanup(self.preview_verify.stop)
        self.recovery_lookup = patch("liquid_tracer.cli.miro_recovery_status",
                                     side_effect=lambda *a: self.recovery)
        self.recovery_lookup.start()
        self.addCleanup(self.recovery_lookup.stop)

    def run_files(self):
        return {p.relative_to(self.case): p.read_bytes()
                for p in (self.case / "runs").rglob("*") if p.is_file()}

    async def click(self, app, pilot, selector):
        from textual.widgets import Button
        button = app.screen.query_one(selector, Button)
        button.scroll_visible(immediate=True)
        await pilot.pause()
        if button.has_class("-active"):
            await pilot.pause(button.active_effect_duration)
        await pilot.click(selector)
        await pilot.pause()

    async def open_case(self, app, pilot, case=None):
        app.created(case or self.case)
        await pilot.pause()
        return app.screen

    async def test_preview_is_offline_cancellable_and_preserves_saved_run(self):
        from textual.widgets import Button, Static
        commands = []

        def local_preview(calculation, command, **kwargs):
            commands.append(command)
            self.saved_preview = self.preview_id
            return subprocess.CompletedProcess(command, 0, json.dumps({
                "directory": str(self.case / "previews" / self.preview_id),
                "preview_id": self.preview_id, "run_id": self.run_id,
            }), "")

        app = create_app(self.root)
        with patch("liquid_tracer.menu._OfflineCalculation.run", new=local_preview), \
                patch("liquid_tracer.menu.subprocess.run") as process:
            async with app.run_test(size=(115, 60)) as pilot:
                screen = await self.open_case(app, pilot)
                self.assertFalse(screen.query_one("#compact-preview", Button).disabled)
                self.assertTrue(screen.query_one("#compact-apply", Button).disabled)
                await self.click(app, pilot, "#compact-preview")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertFalse(app.busy)
                self.assertIn("Miro is unchanged", str(screen.query_one("#action-status", Static).render()))
                self.assertFalse(screen.query_one("#compact-apply", Button).disabled)
        process.assert_not_called()
        self.assertEqual(commands, [[sys.executable, "-m", "liquid_tracer", "compact-preview",
                                    "--case", str(self.case), "--run", "latest", "--open"]])
        self.assertEqual(self.run_files(), self.before)

    async def test_reopen_review_cancel_and_apply_pin_the_saved_preview(self):
        from textual.widgets import Button, Checkbox, Static
        self.saved_preview = self.preview_id
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as process, \
                patch.object(app, "suspend", side_effect=contextlib.nullcontext) as suspend, \
                patch("webbrowser.open", return_value=True) as browser:
            async with app.run_test(size=(115, 60)) as pilot:
                screen = await self.open_case(app, pilot)
                self.assertFalse(screen.query_one("#compact-apply", Button).disabled)
                await self.click(app, pilot, "#compact-apply")
                self.assertEqual(app.focused.id, "cancel")
                self.assertIn(self.preview_id, str(app.screen.query_one("#compact-selection", Static).render()))
                notice = str(app.screen.query_one("#compact-notice", Static).render())
                self.assertIn("not the current Miro board", notice)
                self.assertIn("including your manual moves", notice)
                await pilot.press("enter")
                await pilot.pause()
                self.assertIs(app.screen, screen)
                process.assert_not_called()
                suspend.assert_not_called()
                await self.click(app, pilot, "#compact-apply")
                await self.click(app, pilot, "#compact-open")
                browser.assert_called_once_with((self.case / "previews" / self.preview_id / "graph.html").as_uri())
                await self.click(app, pilot, "#submit")
                process.assert_not_called()
                self.assertIn("confirmation checkbox", str(app.screen.query_one("#form-error", Static).render()))
                app.screen.query_one("#compact-reviewed", Checkbox).value = True
                await self.click(app, pilot, "#submit")
                self.assertIs(app.screen, screen)
                self.assertIn("Reviewed compact layout applied", str(screen.query_one("#action-status", Static).render()))
        suspend.assert_called_once()
        process.assert_called_once()
        command = process.call_args.args[0]
        self.assertEqual(command[:9], ["/nix/store/test-secretspec/bin/secretspec", "--file",
                                      str(PROJECT / "secretspec.toml"), "run", "--provider", "protonpass",
                                      "--profile", "development", "--"])
        self.assertEqual(command[9:], [sys.executable, "-m", "liquid_tracer", "miro-sync", "--case", str(self.case),
                                     "--run", self.run_id, "--compact-preview", self.preview_id,
                                     "--reorganize", "--max-new-items", "321"])
        self.assertNotIn("capture_output", process.call_args.kwargs)
        self.assertEqual(self.run_files(), self.before)
        self.assertEqual(read_case(self.case)["latest_run"], self.run_id)

    async def test_no_run_or_no_board_and_pending_recovery_disable_apply(self):
        from textual.widgets import Button, Static
        no_run = create_investigation(self.root, "No trace", board="SYNTHETIC-BOARD=")
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run") as process:
            async with app.run_test(size=(115, 60)) as pilot:
                screen = await self.open_case(app, pilot, no_run)
                self.assertTrue(screen.query_one("#compact-preview", Button).disabled)
                self.assertTrue(screen.query_one("#compact-apply", Button).disabled)
                await self.click(app, pilot, "#back")
                self.saved_preview = self.preview_id
                screen = await self.open_case(app, pilot)
                update_case(self.case, {"miro_board": None})
                screen.update_summary()
                self.assertFalse(screen.query_one("#compact-preview", Button).disabled)
                self.assertTrue(screen.query_one("#compact-apply", Button).disabled)
                update_case(self.case, {"miro_board": "SYNTHETIC-BOARD="})
                self.recovery["pending_count"] = 20
                screen.update_summary()
                self.assertTrue(screen.query_one("#compact-apply", Button).disabled)
                self.assertIn("needs recovery", str(screen.query_one("#compact-status", Static).render()))
                self.recovery = {"pending_count": 0, "unavailable": True}
                screen.update_summary()
                self.assertTrue(screen.query_one("#compact-apply", Button).disabled)
        process.assert_not_called()

    async def test_board_change_or_new_pending_creation_rejects_apply_without_credentials(self):
        from textual.widgets import Checkbox, Static
        self.saved_preview = self.preview_id
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run") as process, \
                patch.object(app, "suspend", side_effect=contextlib.nullcontext) as suspend:
            async with app.run_test(size=(115, 60)) as pilot:
                await self.open_case(app, pilot)
                await self.click(app, pilot, "#compact-apply")
                app.screen.query_one("#compact-reviewed", Checkbox).value = True
                update_case(self.case, {"miro_board": "OTHER-SYNTHETIC="})
                await self.click(app, pilot, "#submit")
                self.assertIn("linked Miro board changed", str(app.screen.query_one("#form-error", Static).render()))
                update_case(self.case, {"miro_board": "SYNTHETIC-BOARD="})
                self.recovery["pending_count"] = 1
                await self.click(app, pilot, "#submit")
                self.assertIn("needs recovery", str(app.screen.query_one("#form-error", Static).render()))
        process.assert_not_called()
        suspend.assert_not_called()
        self.assertEqual(self.run_files(), self.before)

    async def test_invalidated_preview_cannot_open_or_apply(self):
        from textual.widgets import Checkbox, Static
        self.saved_preview = self.preview_id
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run") as process, patch("webbrowser.open") as browser:
            async with app.run_test(size=(115, 60)) as pilot:
                await self.open_case(app, pilot)
                await self.click(app, pilot, "#compact-apply")
                self.verify.side_effect = TraceError("The service assessment changed; create a new comparison.")
                await self.click(app, pilot, "#compact-open")
                app.screen.query_one("#compact-reviewed", Checkbox).value = True
                await self.click(app, pilot, "#submit")
                self.assertIn("service assessment changed", str(app.screen.query_one("#form-error", Static).render()))
        process.assert_not_called()
        browser.assert_not_called()

    async def test_ctrl_x_cancels_compaction_worker_without_running_miro(self):
        from textual.widgets import Static

        def wait_for_cancel(calculation, command, **kwargs):
            deadline = time.monotonic() + 5
            while not calculation.cancelled and time.monotonic() < deadline:
                time.sleep(0.01)
            return subprocess.CompletedProcess(command, 130 if calculation.cancelled else 1, "", "")

        app = create_app(self.root)
        with patch("liquid_tracer.menu._OfflineCalculation.run", new=wait_for_cancel), \
                patch("liquid_tracer.menu.subprocess.run") as process:
            async with app.run_test(size=(115, 60)) as pilot:
                screen = await self.open_case(app, pilot)
                await self.click(app, pilot, "#compact-preview")
                self.assertTrue(app.busy)
                self.assertIsNotNone(app.active_calculation)
                await pilot.press("ctrl+x")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertFalse(app.busy)
                self.assertIn("Calculation cancelled", str(screen.query_one("#action-status", Static).render()))
        process.assert_not_called()
        self.assertEqual(self.run_files(), self.before)
