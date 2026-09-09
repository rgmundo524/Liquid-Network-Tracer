import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import TraceError, read_json
from liquid_tracer.investigations import (DEFAULTS, create_investigation, list_investigations,
                                         load_settings, read_case)
from liquid_tracer.menu import _command, _seed_values, create_app, run_menu


PROJECT = Path(__file__).resolve().parents[1]
HAS_TEXTUAL = importlib.util.find_spec("textual") is not None


class MenuCommandTests(unittest.TestCase):
    def test_live_command_uses_secretspec_and_preserves_literal_arguments(self):
        arguments = ["miro-sync", "--case", "/cases/Case with spaces", "--board", "BOARD=", "--run", "latest"]
        with patch.dict(os.environ, {"LIQUID_TRACER_ROOT": str(PROJECT),
                        "LIQUID_SECRET_PROVIDER": "protonpass", "LIQUID_SECRET_PROFILE": "development"}, clear=True):
            command = _command(arguments, live=True)
            self.assertEqual(command, ["secretspec", "--file", str(PROJECT / "secretspec.toml"), "run",
                             "--provider", "protonpass", "--profile", "development", "--",
                             sys.executable, "-m", "liquid_tracer", *arguments])
            self.assertEqual(_command(arguments), [sys.executable, "-m", "liquid_tracer", *arguments])

    def test_live_command_uses_pinned_executable_over_host_path(self):
        arguments = ["credentials-check"]
        pinned = "/nix/store/example-secretspec-0.19.1/bin/secretspec"
        with patch.dict(os.environ, {"LIQUID_SECRETSPEC_BIN": pinned, "PATH": "/old/host/bin"}, clear=True):
            self.assertEqual(_command(arguments, live=True)[0], pinned)
            self.assertEqual(_command(arguments)[0], sys.executable)

    def test_seed_entry_accepts_multiline_and_comma_forms(self):
        first, second = "a" * 64 + ":0", "b" * 64 + ":12"
        self.assertEqual(_seed_values(first + ",\n" + second + " " + first.upper()), [first, second])
        for value in ("", "bad", "a" * 64 + ":-1"):
            with self.subTest(value=value), self.assertRaises(TraceError):
                _seed_values(value)

    def test_nonterminal_menu_does_not_load_the_ui_or_credentials(self):
        with patch("sys.stdin.isatty", return_value=False), patch("liquid_tracer.menu.create_app") as make_app, \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(run_menu(), 2)
        make_app.assert_not_called()
        self.assertIn("interactive terminal", error.getvalue())


@unittest.skipUnless(HAS_TEXTUAL, "Install the optional [tui] extra to run Textual interaction tests")
class TextualWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "investigations with spaces"
        self.environment = patch.dict(os.environ, {"LIQUID_TRACER_ROOT": str(PROJECT),
            "LIQUID_SECRET_PROVIDER": "protonpass", "LIQUID_SECRET_PROFILE": "development",
            "LIQUID_SECRETSPEC_BIN": "/nix/store/test-secretspec/bin/secretspec",
            "LIQUID_CASE_DIR": "/unrelated/environment/case", "LIQUID_MIRO_BOARD": "UNRELATED="})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    async def click(self, app, pilot, selector):
        from textual.widgets import Button
        button = app.screen.query_one(selector, Button)
        button.scroll_visible(immediate=True)
        await pilot.pause()
        # Textual ignores clicks during its timed press effect. Waiting for
        # CPU idle alone does not wait for this timer after a previous click.
        if button.has_class("-active"):
            await pilot.pause(button.active_effect_duration)
            await pilot.pause()
            self.assertFalse(button.has_class("-active"))
        await pilot.click(selector)
        await pilot.pause()

    async def new_demo(self, app, pilot, name="Synthetic case", board="DEMO="):
        from textual.widgets import Input
        await self.click(app, pilot, "#new")
        app.screen.query_one("#case-name", Input).value = name
        app.screen.query_one("#board", Input).value = board
        await self.click(app, pilot, "#submit")
        entries = list_investigations(self.root)
        return next(case for case, metadata in entries if metadata["name"] == name)

    async def finish_action(self, app, pilot):
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        self.assertFalse(app.busy)

    async def test_navigation_and_default_cancel_never_load_credentials(self):
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run", side_effect=AssertionError("Navigation must not run a process")):
            async with app.run_test(size=(110, 55)) as pilot:
                case = await self.new_demo(app, pilot)
                await self.click(app, pilot, "#run")
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsNone(read_case(case).get("latest_run"))
                self.assertFalse((case / "runs").exists())
                await self.click(app, pilot, "#case-settings")
                await pilot.press("escape")
                await pilot.pause()
                await self.click(app, pilot, "#back")
                await self.click(app, pilot, "#continue")
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(app.screen.case, case)

    async def test_fixture_run_restart_continue_and_readonly_preview(self):
        from textual.widgets import Input
        real_run = subprocess.run
        commands = []

        def offline_only(command, **kwargs):
            self.assertEqual(command[0], sys.executable)
            self.assertTrue("--fixture" in command or "--dry-run" in command)
            commands.append(command)
            return real_run(command, **kwargs)

        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run", side_effect=offline_only):
            async with app.run_test(size=(110, 55)) as pilot:
                case = await self.new_demo(app, pilot)
                await self.click(app, pilot, "#run")
                await self.click(app, pilot, "#submit")
                await self.finish_action(app, pilot)
                first = read_case(case)["latest_run"]
                first_path = case / "runs" / first
                original = {p.relative_to(first_path): p.read_bytes() for p in first_path.rglob("*") if p.is_file()}
                self.assertEqual(read_json(first_path / "trace.json")["limits"]["max_hops"], 1)

            restarted = create_app(self.root)
            async with restarted.run_test(size=(110, 55)) as pilot:
                await self.click(restarted, pilot, "#continue")
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(restarted.screen.case, case)
                snapshot = {p.relative_to(case): p.read_bytes() for p in case.rglob("*") if p.is_file()}
                await self.click(restarted, pilot, "#preview")
                self.assertEqual(restarted.screen.query_one("#board", Input).value, "DEMO=")
                await self.click(restarted, pilot, "#submit")
                await self.finish_action(restarted, pilot)
                self.assertEqual(snapshot, {p.relative_to(case): p.read_bytes() for p in case.rglob("*") if p.is_file()})
                await self.click(restarted, pilot, "#run")
                restarted.screen.query_one("#hops", Input).value = "2"
                await self.click(restarted, pilot, "#submit")
                await self.finish_action(restarted, pilot)
                second = read_case(case)["latest_run"]
                self.assertNotEqual(second, first)
                state = read_json(case / "runs" / second / "trace.json")
                self.assertEqual(state["parent_run"], first)
                self.assertEqual(state["limits"]["max_hops"], 3)
                self.assertEqual(read_case(case)["run_defaults"]["hops"], 2)
                self.assertEqual(original, {p.relative_to(first_path): p.read_bytes() for p in first_path.rglob("*") if p.is_file()})
        self.assertEqual(len(commands), 3)
        self.assertTrue(all(command[command.index("--case") + 1] == str(case) for command in commands))
        self.assertIn("DEMO=", commands[1])
        self.assertNotIn("UNRELATED=", commands[1])

    async def test_global_and_case_settings_are_saved_without_remote_actions(self):
        from textual.widgets import Input
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run", side_effect=AssertionError("Settings are local")):
            async with app.run_test(size=(110, 55)) as pilot:
                await self.click(app, pilot, "#settings")
                app.screen.query_one("#hops", Input).value = "3"
                app.screen.query_one("#max_requests", Input).value = "12"
                await self.click(app, pilot, "#submit")
                self.assertEqual(load_settings(self.root)["hops"], 3)
                case = await self.new_demo(app, pilot)
                self.assertEqual(read_case(case)["run_defaults"]["max_requests"], 12)
                await self.click(app, pilot, "#case-settings")
                app.screen.query_one("#case-name", Input).value = "Renamed investigation"
                app.screen.query_one("#board", Input).value = "https://miro.com/app/board/UPDATED%3D/"
                app.screen.query_one("#max_requests", Input).value = "8"
                await self.click(app, pilot, "#submit")
                saved = read_case(case)
                self.assertEqual(saved["name"], "Renamed investigation")
                self.assertEqual(saved["miro_board"], "UPDATED=")
                self.assertEqual(saved["run_defaults"]["max_requests"], 8)
                self.assertEqual(load_settings(self.root)["max_requests"], 12)

    async def test_live_run_uses_secretspec_only_after_explicit_run_selection(self):
        from textual.widgets import Static
        case = create_investigation(self.root, "Live case", seeds=["a" * 64 + ":0"], run_defaults=DEFAULTS)
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run", return_value=subprocess.CompletedProcess([], 1)) as process, \
                patch.object(app, "suspend", return_value=contextlib.nullcontext()):
            async with app.run_test(size=(110, 55)) as pilot:
                await self.click(app, pilot, "#continue")
                await pilot.press("enter")
                await pilot.pause()
                await self.click(app, pilot, "#run")
                process.assert_not_called()
                await pilot.press("enter")
                await pilot.pause()
                process.assert_not_called()
                await self.click(app, pilot, "#run")
                await self.click(app, pilot, "#submit")
                process.assert_called_once()
                command = process.call_args.args[0]
                self.assertEqual(command[0], "/nix/store/test-secretspec/bin/secretspec")
                self.assertEqual(command[command.index("--profile") + 1], "development")
                self.assertEqual(command[command.index("--case") + 1], str(case))
                self.assertNotIn("--miro-board", command)
                self.assertNotIn("capture_output", process.call_args.kwargs)
                self.assertFalse(app.busy)
                self.assertIn("did not complete", str(app.screen.query_one("#action-status", Static).render()))

    async def test_invalid_live_seeds_and_board_are_rejected_without_creating_case(self):
        from textual.widgets import Input, Select, Static, TextArea
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run", side_effect=AssertionError("Invalid form cannot run")):
            async with app.run_test(size=(110, 55)) as pilot:
                await self.click(app, pilot, "#new")
                app.screen.query_one("#case-name", Input).value = "Invalid input"
                app.screen.query_one("#source", Select).value = "live"
                app.screen.query_one("#seeds", TextArea).text = "not an outpoint"
                await self.click(app, pilot, "#submit")
                self.assertIn("Seed", str(app.screen.query_one("#form-error", Static).render()))
                self.assertEqual(list_investigations(self.root), [])
                app.screen.query_one("#seeds", TextArea).text = "a" * 64 + ":0"
                app.screen.query_one("#board", Input).value = "https://example.com/app/board/WRONG/"
                await self.click(app, pilot, "#submit")
                self.assertIn("Miro board", str(app.screen.query_one("#form-error", Static).render()))
                self.assertEqual(list_investigations(self.root), [])


if __name__ == "__main__":
    unittest.main()
