import contextlib
import importlib.util
import io
import json
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

    def test_seed_entry_explains_numeric_placeholder_and_literal_backslash(self):
        txid = "a" * 64
        with self.assertRaisesRegex(TraceError, "Replace the word 'vout'"):
            _seed_values(txid + ":vout,")
        with self.assertRaisesRegex(TraceError, "Do not include a backslash"):
            _seed_values(txid + r"\:0,")
        self.assertEqual(_seed_values(txid + ":12,"), [txid + ":12"])

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

    async def toggle_output(self, app, pilot, row):
        from textual.widgets import DataTable
        table = app.screen.query_one("#outputs", DataTable)
        table.move_cursor(row=row)
        table.focus()
        await pilot.press("enter")
        await pilot.pause()

    async def test_malformed_output_lookup_never_starts_a_process_or_creates_case(self):
        from textual.widgets import Input, Select, Static
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run") as process:
            async with app.run_test(size=(110, 55)) as pilot:
                await self.click(app, pilot, "#new")
                app.screen.query_one("#source", Select).value = "live"
                for value in ("bad", "a" * 64 + ":vout", "a" * 64 + r"\:0"):
                    with self.subTest(value=value):
                        app.screen.query_one("#lookup-txid", Input).value = value
                        await self.click(app, pilot, "#lookup")
                        self.assertIn("Enter only the 64-character transaction hash",
                                      str(app.screen.query_one("#form-error", Static).render()))
                        self.assertFalse(app.busy)
                        self.assertFalse(self.root.exists())
                process.assert_not_called()

    async def test_fixture_output_picker_requires_selection_and_preserves_seeds_on_cancel(self):
        from textual.widgets import DataTable, Input, Static, TextArea
        fixture = read_json(PROJECT / "examples" / "demo-api.json")
        transaction = next(value for key, value in fixture.items()
                           if not key.endswith("outspends") and
                           any(output.get("scriptpubkey_type") == "op_return" and not output.get("pegout")
                               for output in value["vout"]))
        txid = transaction["txid"]
        original_seed = "b" * 64 + ":7"
        real_run = subprocess.run
        commands = []

        def offline_inspection(command, **kwargs):
            self.assertEqual(command[:4], [sys.executable, "-m", "liquid_tracer", "inspect-tx"])
            self.assertEqual(command[command.index("--txid") + 1], txid)
            self.assertEqual(command[command.index("--fixture") + 1], str(PROJECT / "examples" / "demo-api.json"))
            commands.append(command)
            return real_run(command, **kwargs)

        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run", side_effect=offline_inspection):
            async with app.run_test(size=(110, 55)) as pilot:
                await self.click(app, pilot, "#new")
                form = app.screen
                form.query_one("#seeds", TextArea).text = original_seed
                form.query_one("#lookup-txid", Input).value = txid.upper()
                await self.click(app, pilot, "#lookup")
                table = app.screen.query_one("#outputs", DataTable)
                choice_column = app.screen.choice_column
                self.assertEqual(table.row_count, len(transaction["vout"]))
                self.assertEqual(app.screen.selected, set())
                self.assertEqual(table.get_cell(txid + ":0", choice_column), "No")
                await self.click(app, pilot, "#use-outputs")
                self.assertIn("Select at least one output", str(app.screen.query_one("#output-error", Static).render()))
                for row in (1, 2):
                    await self.toggle_output(app, pilot, row)
                    self.assertEqual(table.get_cell(f"{txid}:{row}", choice_column), "Unavailable")
                    self.assertEqual(app.screen.selected, set())
                    self.assertIn("cannot start", str(app.screen.query_one("#output-error", Static).render()))
                await self.toggle_output(app, pilot, 0)
                self.assertEqual(table.get_cell(txid + ":0", choice_column), "Yes")
                await self.click(app, pilot, "#cancel-outputs")
                self.assertIs(app.screen, form)
                self.assertEqual(form.query_one("#seeds", TextArea).text, original_seed)
                self.assertFalse(self.root.exists())

                await self.click(app, pilot, "#lookup")
                self.assertEqual(app.screen.selected, set())
                await self.toggle_output(app, pilot, 0)
                await self.click(app, pilot, "#use-outputs")
                self.assertIs(app.screen, form)
                self.assertEqual(form.query_one("#seeds", TextArea).text, txid + ":0")
                self.assertFalse(self.root.exists())
                form.query_one("#case-name", Input).value = "Chosen demo output"
                await self.click(app, pilot, "#submit")
                case = app.screen.case
                self.assertEqual(read_case(case)["seeds"], [txid + ":0"])
                self.assertEqual(read_case(case)["fixture"], str(PROJECT / "examples" / "demo-api.json"))
                self.assertFalse((case / "runs").exists())
        self.assertEqual(len(commands), 2)
        for command in commands:
            self.assertFalse(Path(command[command.index("--output") + 1]).exists())

    async def test_live_output_lookup_uses_pinned_tools_and_saves_only_explicit_selection(self):
        from textual.widgets import DataTable, Input, Select, TextArea
        txid = "c" * 64
        report = {"txid": txid, "outputs": [
            {"outpoint": txid + ":0", "vout": 0, "address": "SYNTHETIC-unselected", "value": None,
             "asset": None, "script_type": "v0_p2wpkh", "selectable": True},
            {"outpoint": txid + ":1", "vout": 1, "address": None, "value": 100,
             "asset": None, "script_type": "fee", "selectable": False, "reason": "fee"},
            {"outpoint": txid + ":2", "vout": 2, "address": "SYNTHETIC-selected", "value": None,
             "asset": None, "script_type": "v0_p2wpkh", "selectable": True},
        ]}

        def local_report(command, **kwargs):
            self.assertEqual(command[:9], ["/nix/store/test-secretspec/bin/secretspec", "--file",
                             str(PROJECT / "secretspec.toml"), "run", "--provider", "protonpass",
                             "--profile", "development", "--"])
            self.assertEqual(command[9:15], [sys.executable, "-m", "liquid_tracer", "inspect-tx", "--txid", txid])
            self.assertNotIn("--fixture", command)
            self.assertNotIn("capture_output", kwargs)
            self.assertNotIn("stdout", kwargs)
            self.assertNotIn("stderr", kwargs)
            Path(command[command.index("--output") + 1]).write_text(json.dumps(report))
            return subprocess.CompletedProcess(command, 0)

        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run", side_effect=local_report) as process, \
                patch.object(app, "suspend", side_effect=contextlib.nullcontext) as suspend:
            async with app.run_test(size=(110, 55)) as pilot:
                await self.click(app, pilot, "#new")
                form = app.screen
                form.query_one("#case-name", Input).value = "Chosen live output"
                form.query_one("#source", Select).value = "live"
                form.query_one("#seeds", TextArea).text = "d" * 64 + ":9"
                form.query_one("#lookup-txid", Input).value = txid
                process.assert_not_called()
                await self.click(app, pilot, "#lookup")
                process.assert_called_once()
                suspend.assert_called_once()
                self.assertFalse(self.root.exists())
                self.assertEqual(app.screen.selected, set())
                await self.toggle_output(app, pilot, 2)
                table = app.screen.query_one("#outputs", DataTable)
                self.assertEqual(table.get_cell(txid + ":0", app.screen.choice_column), "No")
                self.assertEqual(table.get_cell(txid + ":2", app.screen.choice_column), "Yes")
                await self.click(app, pilot, "#use-outputs")
                self.assertIs(app.screen, form)
                self.assertEqual(form.query_one("#seeds", TextArea).text, txid + ":2")
                await self.click(app, pilot, "#submit")
                case = app.screen.case
                metadata = read_case(case)
                self.assertEqual(metadata["seeds"], [txid + ":2"])
                self.assertFalse(metadata.get("fixture"))
                self.assertIsNone(metadata.get("latest_run"))
                self.assertFalse((case / "runs").exists())
                process.assert_called_once()

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
