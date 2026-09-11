import contextlib
import copy
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.investigations import (DEFAULTS, create_investigation, list_investigations,
                                         load_settings, read_case, save_settings, update_case)
from liquid_tracer.menu import _OfflineCalculation, _command, _lookup_reports, _seed_values, create_app, run_menu


PROJECT = Path(__file__).resolve().parents[1]
HAS_TEXTUAL = importlib.util.find_spec("textual") is not None


class MenuCommandTests(unittest.TestCase):
    def test_cancel_before_worker_starts_does_not_launch_a_process(self):
        calculation = _OfflineCalculation()
        calculation.cancel()
        with patch("liquid_tracer.menu.subprocess.Popen") as process:
            result = calculation.run([sys.executable, "-c", "raise AssertionError"], cwd=PROJECT, env=os.environ.copy())
            self.assertEqual(result.returncode, 130)
            calculation.close()
        process.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "Process-group cancellation uses POSIX signals")
    def test_cancel_signals_cli_and_allows_nested_renderer_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            cleaned = Path(directory) / "cleaned"
            command = [sys.executable, "-c", """
import os, signal, subprocess, sys
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
try:
    Path(sys.argv[1]).write_text(str(child.pid))
    signal.pause()
except KeyboardInterrupt:
    pass
finally:
    os.killpg(child.pid, signal.SIGTERM)
    child.wait()
    Path(sys.argv[2]).write_text('cleaned')
""", str(ready), str(cleaned)]
            calculation = _OfflineCalculation()
            results = []
            worker = threading.Thread(target=lambda: results.append(calculation.run(command, cwd=PROJECT, env=os.environ.copy())))
            worker.start()
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "Synthetic renderer did not start")
                calculation.cancel()
                worker.join(5)
                self.assertFalse(worker.is_alive(), "Cancellation failed to release the renderer")
                self.assertTrue(cleaned.is_file(), "The CLI must finish its nested-renderer cleanup")
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(ready.read_text()), 0)
                self.assertEqual(len(results), 1)
            finally:
                calculation.cancel()
                worker.join(5)

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

    def test_output_reports_validate_the_entire_batch_before_selection(self):
        txids = ["a" * 64, "b" * 64]
        report = {"transactions": [{"txid": txid, "outputs": [
            {"outpoint": txid + ":0", "vout": 0, "selectable": True, "value": None, "asset": None}
        ]} for txid in txids]}
        self.assertEqual(_lookup_reports(report, txids), report["transactions"])
        self.assertEqual(_lookup_reports(report["transactions"][0], txids[:1]), report["transactions"][:1])
        invalid_reports = [{"transactions": report["transactions"][:1]},
                           {"transactions": list(reversed(report["transactions"]))}]
        for field, value in (("outpoint", txids[0] + ":0"), ("vout", True),
                             ("selectable", "yes"), ("value", -1), ("address", {"invalid": "address"})):
            invalid = copy.deepcopy(report)
            invalid["transactions"][1]["outputs"][0][field] = value
            invalid_reports.append(invalid)
        for invalid in invalid_reports:
            with self.subTest(report=invalid), self.assertRaisesRegex(TraceError, "No outputs were changed"):
                _lookup_reports(invalid, txids)


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
                for value in ("bad", "a" * 64 + ":vout", "a" * 64 + r"\:0", "a" * 64 + ",bad"):
                    with self.subTest(value=value):
                        app.screen.query_one("#lookup-txid", Input).value = value
                        await self.click(app, pilot, "#lookup")
                        self.assertIn("64",
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

    async def test_ten_transaction_lookup_groups_outputs_and_saves_exact_selections(self):
        from textual.widgets import DataTable, Input, Select, Static, TextArea
        txids = [f"{number:064x}" for number in range(10, 20)]
        report = {"transactions": [{"txid": txid, "outputs": [
            {"outpoint": txid + ":0", "vout": 0, "address": "SYNTHETIC-output-" + str(number),
             "value": None, "asset": None, "script_type": "v0_p2wpkh", "selectable": True},
            {"outpoint": txid + ":1", "vout": 1, "address": None, "value": 100,
             "asset": None, "script_type": "fee", "selectable": False, "reason": "fee"},
        ]} for number, txid in enumerate(txids)]}
        original = "f" * 64 + ":9"
        report_paths = []

        def local_report(command, **kwargs):
            self.assertEqual(command[:9], ["/nix/store/test-secretspec/bin/secretspec", "--file",
                str(PROJECT / "secretspec.toml"), "run", "--provider", "protonpass",
                "--profile", "development", "--"])
            self.assertEqual(command[9:15], [sys.executable, "-m", "liquid_tracer", "inspect-txs",
                                           "--txids", ",".join(txids)])
            self.assertNotIn("capture_output", kwargs)
            self.assertNotIn("stdout", kwargs)
            self.assertNotIn("stderr", kwargs)
            report_path = Path(command[command.index("--output") + 1])
            report_paths.append(report_path)
            report_path.write_text(json.dumps(report))
            return subprocess.CompletedProcess(command, 0)

        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run", side_effect=local_report) as process, \
                patch.object(app, "suspend", side_effect=contextlib.nullcontext) as suspend:
            async with app.run_test(size=(130, 55)) as pilot:
                await self.click(app, pilot, "#new")
                form = app.screen
                form.query_one("#case-name", Input).value = "Ten starting transactions"
                form.query_one("#source", Select).value = "live"
                form.query_one("#seeds", TextArea).text = original
                form.query_one("#lookup-txid", Input).value = ", ".join(txids + [txids[0].upper()])
                await self.click(app, pilot, "#lookup")
                process.assert_called_once()
                suspend.assert_called_once()
                self.assertFalse(self.root.exists())
                self.assertEqual(list(app.screen.outputs), [output["outpoint"]
                    for transaction in report["transactions"] for output in transaction["outputs"]])
                table = app.screen.query_one("#outputs", DataTable)
                self.assertEqual(table.row_count, 20)
                self.assertEqual(app.screen.selected, set())
                self.assertEqual(table.get_row(txids[0] + ":0")[4], "??")
                self.assertEqual(str(table.get_row(txids[0] + ":0")[5]), "??")
                await self.toggle_output(app, pilot, 19)
                self.assertEqual(app.screen.selected, set())
                await self.toggle_output(app, pilot, 18)
                self.assertIn(txids[-1], str(app.screen.query_one("#output-detail", Static).render()))
                await self.click(app, pilot, "#cancel-outputs")
                self.assertIs(app.screen, form)
                self.assertEqual(form.query_one("#seeds", TextArea).text, original)

                await self.click(app, pilot, "#lookup")
                self.assertEqual(app.screen.selected, set())
                for row in range(0, 20, 2):
                    await self.toggle_output(app, pilot, row)
                selected = [txid + ":0" for txid in txids]
                self.assertEqual(app.screen.selected, set(selected))
                await self.click(app, pilot, "#use-outputs")
                self.assertEqual(form.query_one("#seeds", TextArea).text.splitlines(), selected)
                self.assertFalse(self.root.exists())
                await self.click(app, pilot, "#submit")
                case = app.screen.case
                self.assertEqual(read_case(case)["seeds"], selected)
                self.assertIsNone(read_case(case).get("latest_run"))
                self.assertFalse((case / "runs").exists())
                self.assertEqual(process.call_count, 2)
                self.assertEqual(suspend.call_count, 2)
        self.assertTrue(all(not path.exists() for path in report_paths))

    async def test_failed_or_incomplete_batch_lookup_keeps_existing_selection(self):
        from textual.widgets import Input, Select, Static, TextArea
        txids = ["a" * 64, "b" * 64]
        report = {"transactions": [{"txid": txids[0], "outputs": [
            {"outpoint": txids[0] + ":0", "vout": 0, "selectable": True}]}]}
        original = "c" * 64 + ":3"
        app = create_app(self.root)

        def incomplete_report(command, **kwargs):
            Path(command[command.index("--output") + 1]).write_text(json.dumps(report))
            return subprocess.CompletedProcess(command, 0)

        with patch("liquid_tracer.menu.subprocess.run", side_effect=incomplete_report) as process, \
                patch.object(app, "suspend", side_effect=contextlib.nullcontext):
            async with app.run_test(size=(110, 55)) as pilot:
                await self.click(app, pilot, "#new")
                form = app.screen
                form.query_one("#source", Select).value = "live"
                form.query_one("#lookup-txid", Input).value = ", ".join(txids)
                form.query_one("#seeds", TextArea).text = original
                for effect, message in ((incomplete_report, "invalid transaction report"),
                                        (lambda command, **kwargs: subprocess.CompletedProcess(command, 1), "lookup failed"),
                                        (KeyboardInterrupt, "interrupted")):
                    with self.subTest(message=message):
                        process.side_effect = effect
                        await self.click(app, pilot, "#lookup")
                        self.assertIs(app.screen, form)
                        self.assertIn(message, str(form.query_one("#form-error", Static).render()))
                        self.assertEqual(form.query_one("#seeds", TextArea).text, original)
                        self.assertFalse(app.busy)
                        self.assertFalse(self.root.exists())

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

    async def test_arrow_buttons_and_enter_work_without_taking_over_form_controls(self):
        from textual.widgets import Button, Checkbox, Input, Select, TextArea
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run") as process:
            async with app.run_test(size=(80, 24)) as pilot:
                app.query_one("#new", Button).focus()
                await pilot.press("down")
                self.assertEqual(app.focused.id, "continue")
                await pilot.press("right")
                self.assertEqual(app.focused.id, "settings")
                await pilot.press("down")
                await pilot.pause()
                self.assertEqual(app.focused.id, "exit")
                self.assertGreaterEqual(app.focused.region.y, 1)
                self.assertLessEqual(app.focused.region.bottom, 23)
                await pilot.press("up", "up", "left")
                self.assertEqual(app.focused.id, "new")
                await pilot.press("enter")
                await pilot.pause()
                field = app.screen.query_one("#case-name", Input)
                self.assertIs(app.focused, field)
                field.value = "abcd"
                field.cursor_position = 2
                await pilot.press("left")
                self.assertEqual(field.cursor_position, 1)
                await pilot.press("right")
                self.assertEqual(field.cursor_position, 2)
                self.assertIs(app.focused, field)

                seeds = app.screen.query_one("#seeds", TextArea)
                seeds.text = "abc\ndef"
                seeds.focus()
                seeds.move_cursor((0, 1))
                await pilot.press("down", "right")
                self.assertEqual(seeds.cursor_location, (1, 2))
                self.assertIs(app.focused, seeds)

                source = app.screen.query_one("#source", Select)
                source.focus()
                await pilot.press("enter", "down", "enter")
                self.assertEqual(source.value, "live")
                checkbox = app.screen.query_one("#include-fees", Checkbox)
                checkbox.focus()
                await pilot.press("space")
                self.assertTrue(checkbox.value)
                self.assertIs(app.focused, checkbox)

                cancel = app.screen.query_one("#cancel", Button)
                cancel.focus()
                await pilot.press("right")
                self.assertEqual(app.focused.id, "submit")
                await pilot.press("left", "enter")
                await pilot.pause()
                self.assertEqual(len(app.screen_stack), 1)
                process.assert_not_called()

    async def test_arrow_navigation_skips_disabled_hidden_buttons_and_scrolls_small_screen(self):
        from textual.widgets import Button, DataTable
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run") as process:
            async with app.run_test(size=(80, 24)) as pilot:
                case = await self.new_demo(app, pilot, board="")
                for selector in ("#mermaid", "#csv", "#elk-preview", "#layout"):
                    self.assertTrue(app.screen.query_one(selector, Button).disabled)
                run = app.screen.query_one("#run", Button)
                run.focus()
                await pilot.press("right")
                self.assertEqual(app.focused.id, "review")
                await pilot.press("left", "down")
                self.assertEqual(app.focused.id, "preview")
                await pilot.press("down")
                self.assertEqual(app.focused.id, "create-board")
                await pilot.press("down")
                self.assertEqual(app.focused.id, "case-settings")
                await pilot.pause()
                region = app.focused.region
                self.assertGreaterEqual(region.y, 1)
                self.assertLessEqual(region.bottom, 23)
                await pilot.press("right")
                self.assertEqual(app.focused.id, "back")
                hidden = app.screen.query_one("#case-settings", Button)
                hidden.display = False
                await pilot.pause()
                await pilot.press("left")
                self.assertEqual(app.focused.id, "create-board")
                hidden.display = True
                await pilot.pause()
                app.screen.query_one("#back", Button).focus()
                await pilot.press("enter")
                await pilot.pause()

                create_investigation(self.root, "Second synthetic case")
                await self.click(app, pilot, "#continue")
                table = app.screen.query_one("#investigations", DataTable)
                self.assertIs(app.focused, table)
                self.assertEqual(table.cursor_row, 0)
                await pilot.press("down")
                self.assertEqual(table.cursor_row, 1)
                self.assertIs(app.focused, table)
                await pilot.press("tab")
                self.assertEqual(app.focused.id, "back")
                await pilot.press("shift+tab")
                self.assertIs(app.focused, table)
                self.assertIsNone(read_case(case).get("latest_run"))
                process.assert_not_called()

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

    async def test_mermaid_requires_a_saved_run_without_requiring_a_miro_board(self):
        from textual.widgets import Button
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run") as process:
            async with app.run_test(size=(110, 55)) as pilot:
                case = await self.new_demo(app, pilot, board="")
                self.assertFalse(read_case(case).get("miro_board"))
                self.assertTrue(app.screen.query_one("#mermaid", Button).disabled)
                self.assertTrue(app.screen.query_one("#csv", Button).disabled)
                self.assertFalse(app.screen.query_one("#run", Button).disabled)
                self.assertFalse(app.screen.query_one("#create-board", Button).disabled)
                self.assertTrue(app.screen.query_one("#layout", Button).disabled)
                await self.click(app, pilot, "#mermaid")
                await self.click(app, pilot, "#csv")
                await self.click(app, pilot, "#review")
                await self.click(app, pilot, "#back")
                self.assertEqual(app.screen.case, case)
                self.assertTrue(app.screen.query_one("#mermaid", Button).disabled)
                process.assert_not_called()

    async def test_csv_button_uses_offline_worker_and_reports_saved_paths_at_small_size(self):
        from textual.widgets import Button, Static
        from liquid_tracer.cli import main
        fixture = PROJECT / "examples" / "demo-api.json"
        case = create_investigation(self.root, "Synthetic CSV case", fixture=str(fixture))
        with contextlib.redirect_stdout(io.StringIO()):
            status = main(["trace", "--case", str(case), "--fixture", str(fixture),
                           "--seeds-file", str(PROJECT / "examples" / "demo-seeds.txt"), "--hops", "1"])
        self.assertEqual(status, 0)
        self.assertFalse(read_case(case).get("miro_board"))
        snapshot = {p.relative_to(case): p.read_bytes() for p in case.rglob("*") if p.is_file()}
        directory = case / "exports" / "synthetic-csv"
        files = [str(directory / name) for name in ("transactions.csv", "outputs.csv")]
        started, release = threading.Event(), threading.Event()

        def export_locally(command, **kwargs):
            self.assertEqual(command, [sys.executable, "-m", "liquid_tracer", "csv-export",
                                      "--case", str(case), "--run", "latest"])
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["text"])
            started.set()
            if not release.wait(10):
                raise AssertionError("CSV worker was not released")
            return subprocess.CompletedProcess(command, 0, json.dumps({
                "directory": str(directory), "run_id": read_case(case)["latest_run"], "files": files}), "")

        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run", side_effect=export_locally) as process, \
                patch.object(app, "suspend") as suspend:
            async with app.run_test(size=(80, 24)) as pilot:
                await self.click(app, pilot, "#continue")
                await pilot.press("enter")
                await pilot.pause()
                mermaid = app.screen.query_one("#mermaid", Button)
                csv = app.screen.query_one("#csv", Button)
                self.assertFalse(csv.disabled)
                mermaid.focus()
                await pilot.press("right")
                await pilot.pause()
                self.assertIs(app.focused, csv)
                self.assertEqual(mermaid.region.y, csv.region.y)
                self.assertGreaterEqual(csv.region.y, 1)
                self.assertLessEqual(csv.region.right, 80)
                self.assertLessEqual(csv.region.bottom, 23)
                try:
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertTrue(started.is_set())
                    self.assertTrue(app.busy)
                    self.assertTrue(all(button.disabled for button in app.screen.query(Button)))
                    app.screen.perform((["csv-export", "--case", str(case)], False))
                    process.assert_called_once()
                finally:
                    release.set()
                await self.finish_action(app, pilot)
                suspend.assert_not_called()
                self.assertFalse(csv.disabled)
                message = str(app.screen.query_one("#action-status", Static).render())
                self.assertIn("CSV export saved", message)
                self.assertIn(str(directory), message)
                for path in files:
                    self.assertIn(path, message)
                self.assertEqual(snapshot, {p.relative_to(case): p.read_bytes()
                                            for p in case.rglob("*") if p.is_file()})

    async def test_mermaid_button_uses_offline_worker_and_preserves_saved_investigation(self):
        from textual.widgets import Button, Static
        from liquid_tracer.cli import main
        fixture = PROJECT / "examples" / "demo-api.json"
        case = create_investigation(self.root, "Synthetic Mermaid case", fixture=str(fixture))
        with contextlib.redirect_stdout(io.StringIO()):
            status = main(["trace", "--case", str(case), "--fixture", str(fixture),
                           "--seeds-file", str(PROJECT / "examples" / "demo-seeds.txt"), "--hops", "1"])
        self.assertEqual(status, 0)
        self.assertFalse(read_case(case).get("miro_board"))
        settings = dict(read_case(case)["run_defaults"], include_fees=True)
        update_case(case, {"run_defaults": settings})
        snapshot = {p.relative_to(case): p.read_bytes() for p in case.rglob("*") if p.is_file()}
        preview = case / "previews" / "synthetic-mermaid" / "graph.html"
        started, release = threading.Event(), threading.Event()

        def render_locally(command, **kwargs):
            self.assertEqual(command, [sys.executable, "-m", "liquid_tracer", "mermaid",
                                      "--case", str(case), "--run", "latest", "--open"])
            self.assertEqual(kwargs["cwd"], PROJECT)
            started.set()
            if not release.wait(10):
                raise AssertionError("Mermaid worker was not released")
            return subprocess.CompletedProcess(command, 0, json.dumps({
                "html": str(preview), "browser_opened": False}), "")

        app = create_app(self.root)
        with patch("liquid_tracer.menu._OfflineCalculation.run", side_effect=render_locally) as process, \
                patch.object(app, "suspend") as suspend:
            async with app.run_test(size=(110, 55)) as pilot:
                await self.click(app, pilot, "#continue")
                await pilot.press("enter")
                await pilot.pause()
                self.assertFalse(app.screen.query_one("#mermaid", Button).disabled)
                try:
                    await self.click(app, pilot, "#mermaid")
                    self.assertTrue(started.is_set())
                    self.assertTrue(app.busy)
                    self.assertTrue(all(button.disabled for button in app.screen.query(Button)
                                        if button.id != "cancel-calculation"))
                    self.assertFalse(app.screen.query_one("#cancel-calculation", Button).disabled)
                    app.screen.perform((["mermaid", "--case", str(case)], False))
                    process.assert_called_once()
                finally:
                    release.set()
                await self.finish_action(app, pilot)
                suspend.assert_not_called()
                self.assertFalse(app.screen.query_one("#mermaid", Button).disabled)
                message = str(app.screen.query_one("#action-status", Static).render())
                self.assertIn("Mermaid chart saved", message)
                self.assertIn("Open in your browser", message)
                self.assertIn(str(preview), message)
                self.assertFalse(app.screen.query_one("#create-board", Button).disabled)
                app.screen.finished(0, json.dumps({"html": str(preview), "renderer": "direct_svg",
                                                   "fallback_reason": "timeout", "browser_opened": False}))
                message = str(app.screen.query_one("#action-status", Static).render())
                self.assertIn("Direct SVG fallback saved", message)
                self.assertIn("Mermaid reached its time limit", message)
                self.assertIn(str(preview), message)
                self.assertEqual(snapshot, {p.relative_to(case): p.read_bytes()
                                            for p in case.rglob("*") if p.is_file()})

    async def test_elk_preview_uses_offline_worker_without_a_miro_board(self):
        from textual.widgets import Button, Static
        from liquid_tracer.cli import main
        fixture = PROJECT / "examples" / "demo-api.json"
        case = create_investigation(self.root, "Synthetic ELK case", fixture=str(fixture))
        with contextlib.redirect_stdout(io.StringIO()):
            status = main(["trace", "--case", str(case), "--fixture", str(fixture),
                           "--seeds-file", str(PROJECT / "examples" / "demo-seeds.txt"), "--hops", "1"])
        self.assertEqual(status, 0)
        snapshot = {p.relative_to(case): p.read_bytes() for p in case.rglob("*") if p.is_file()}
        preview = case / "previews" / "synthetic-elk" / "graph.html"
        result = subprocess.CompletedProcess([], 0, json.dumps({"html": str(preview), "browser_opened": False}), "")
        app = create_app(self.root)
        with patch("liquid_tracer.menu._OfflineCalculation.run", return_value=result) as process, \
                patch.object(app, "suspend") as suspend:
            async with app.run_test(size=(80, 24)) as pilot:
                await self.click(app, pilot, "#continue")
                await pilot.press("enter")
                await pilot.pause()
                self.assertFalse(app.screen.query_one("#elk-preview", Button).disabled)
                self.assertTrue(app.screen.query_one("#layout", Button).disabled)
                await self.click(app, pilot, "#elk-preview")
                await self.finish_action(app, pilot)
                process.assert_called_once()
                self.assertEqual(process.call_args.args[0], [sys.executable, "-m", "liquid_tracer", "layout-preview",
                                                            "--case", str(case), "--run", "latest", "--open"])
                self.assertEqual(process.call_args.kwargs["cwd"], PROJECT)
                suspend.assert_not_called()
                message = str(app.screen.query_one("#action-status", Static).render())
                self.assertIn("ELK layout preview saved. Miro is unchanged.", message)
                self.assertIn(str(preview), message)
                app.screen.finished(0, json.dumps({"html": str(preview), "layout_algorithm": "dependency_layers_v1",
                                                   "fallback_reason": "size_limit", "browser_opened": False}))
                message = str(app.screen.query_one("#action-status", Static).render())
                self.assertIn("Dependency layout fallback saved", message)
                self.assertIn("ELK size limit", message)
                self.assertIn(str(preview), message)
                self.assertEqual(snapshot, {p.relative_to(case): p.read_bytes()
                                           for p in case.rglob("*") if p.is_file()})

    @unittest.skipUnless(os.name == "posix", "Process-group cancellation uses POSIX signals")
    async def test_calculation_cancel_button_keyboard_and_shutdown_reap_process(self):
        from textual.widgets import Button, Static
        ready = Path(self.temp.name) / "calculation-ready"
        cleaned = Path(self.temp.name) / "calculation-cleaned"
        command = [sys.executable, "-c", """
import os, signal, sys
from pathlib import Path
try:
    Path(sys.argv[1]).write_text(str(os.getpid()))
    signal.pause()
except KeyboardInterrupt:
    pass
finally:
    Path(sys.argv[2]).write_text('cleaned')
""", str(ready), str(cleaned)]
        app = create_app(self.root)
        with patch("liquid_tracer.menu._command", return_value=command):
            async with app.run_test(size=(80, 24)) as pilot:
                case = await self.new_demo(app, pilot)
                snapshot = {p.relative_to(case): p.read_bytes() for p in case.rglob("*") if p.is_file()}
                for action, key in (("layout-preview", "enter"), ("mermaid", "ctrl+x"), ("layout-preview", None)):
                    ready.unlink(missing_ok=True)
                    cleaned.unlink(missing_ok=True)
                    app.screen.perform(([action, "--case", str(case)], False))
                    deadline = time.monotonic() + 5
                    while not ready.exists() and time.monotonic() < deadline:
                        await pilot.pause(0.02)
                    self.assertTrue(ready.is_file())
                    calculation = app.active_calculation
                    pid = int(ready.read_text())
                    self.assertTrue(app.busy)
                    cancel = app.screen.query_one("#cancel-calculation", Button)
                    self.assertIs(app.focused, cancel)
                    self.assertFalse(cancel.disabled)
                    app.screen.update_calculation_status()
                    self.assertIn("elapsed", str(app.screen.query_one("#action-status", Static).render()))
                    if key is None:
                        # Leaving the application must cancel the active child,
                        # even when the user did not press our Cancel button.
                        break
                    await pilot.press(key)
                    await self.finish_action(app, pilot)
                    self.assertTrue(calculation.cancelled)
                    self.assertTrue(cleaned.is_file())
                    self.assertIn("Calculation cancelled", str(app.screen.query_one("#action-status", Static).render()))
                    self.assertIsNone(app.active_calculation)
                    self.assertTrue(cancel.disabled)
                    self.assertEqual(snapshot, {p.relative_to(case): p.read_bytes()
                                               for p in case.rglob("*") if p.is_file()})
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
            self.assertTrue(calculation.cancelled)
            self.assertTrue(cleaned.is_file())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    async def test_cancel_calculation_does_not_cancel_live_actions(self):
        app = create_app(self.root)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.new_demo(app, pilot)
            app.busy = True
            app.screen.current_action = "miro-sync"
            with patch("liquid_tracer.menu.os.killpg") as kill:
                await pilot.press("ctrl+x")
                kill.assert_not_called()
                self.assertTrue(app.busy)
                self.assertIsNone(app.active_calculation)
            app.busy = False

    async def test_global_and_case_settings_are_saved_without_remote_actions(self):
        from textual.widgets import Checkbox, Input, Select, Static
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run", side_effect=AssertionError("Settings are local")):
            async with app.run_test(size=(110, 55)) as pilot:
                await self.click(app, pilot, "#settings")
                app.screen.query_one("#hops", Input).value = "3"
                app.screen.query_one("#max_requests", Input).value = "12"
                self.assertFalse(app.screen.query_one("#include-fees", Checkbox).value)
                app.screen.query_one("#include-fees", Checkbox).value = True
                self.assertEqual(app.screen.query_one("#connector-style", Select).value, "straight")
                app.screen.query_one("#connector-style", Select).value = "curved"
                await self.click(app, pilot, "#submit")
                self.assertEqual(load_settings(self.root)["connector_style"], "curved")
                self.assertEqual(load_settings(self.root)["hops"], 3)
                self.assertIs(load_settings(self.root)["include_fees"], True)
                case = await self.new_demo(app, pilot)
                self.assertEqual(read_case(case)["run_defaults"]["max_requests"], 12)
                self.assertEqual(read_case(case)["run_defaults"]["connector_style"], "curved")
                self.assertIs(read_case(case)["run_defaults"]["include_fees"], True)
                self.assertIn("Transaction fee flows: included", str(app.screen.query_one("#case-summary", Static).render()))
                await self.click(app, pilot, "#case-settings")
                self.assertTrue(app.screen.query_one("#include-fees", Checkbox).value)
                self.assertEqual(app.screen.query_one("#connector-style", Select).value, "curved")
                app.screen.query_one("#connector-style", Select).value = "elbowed"
                app.screen.query_one("#include-fees", Checkbox).value = False
                app.screen.query_one("#case-name", Input).value = "Renamed investigation"
                app.screen.query_one("#board", Input).value = "https://miro.com/app/board/UPDATED%3D/"
                app.screen.query_one("#max_requests", Input).value = "8"
                await self.click(app, pilot, "#submit")
                saved = read_case(case)
                self.assertEqual(saved["name"], "Renamed investigation")
                self.assertEqual(saved["miro_board"], "UPDATED=")
                self.assertEqual(saved["run_defaults"]["max_requests"], 8)
                self.assertEqual(saved["run_defaults"]["connector_style"], "elbowed")
                self.assertEqual(load_settings(self.root)["connector_style"], "curved")
                self.assertIs(saved["run_defaults"]["include_fees"], False)
                self.assertIn("Transaction fee flows: hidden", str(app.screen.query_one("#case-summary", Static).render()))
                self.assertEqual(load_settings(self.root)["max_requests"], 12)
                self.assertIs(load_settings(self.root)["include_fees"], True)

        restarted = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run") as process:
            async with restarted.run_test(size=(110, 55)) as pilot:
                await self.click(restarted, pilot, "#continue")
                await pilot.press("enter")
                await pilot.pause()
                await self.click(restarted, pilot, "#case-settings")
                self.assertFalse(restarted.screen.query_one("#include-fees", Checkbox).value)
                self.assertEqual(restarted.screen.query_one("#connector-style", Select).value, "elbowed")
                process.assert_not_called()

    async def test_new_case_fee_checkbox_and_legacy_settings_ignore_later_global_defaults(self):
        from textual.widgets import Checkbox, Input, Static
        case = create_investigation(self.root, "Legacy defaults")
        metadata = read_case(case)
        metadata["run_defaults"].pop("include_fees")
        save_json(case / "case.json", metadata)
        original = (case / "case.json").read_bytes()
        save_settings(self.root, {"include_fees": True, "hops": 9})
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run") as process:
            async with app.run_test(size=(110, 55)) as pilot:
                await self.click(app, pilot, "#new")
                self.assertTrue(app.screen.query_one("#include-fees", Checkbox).value)
                self.assertEqual(app.screen.query_one("#hops", Input).value, "9")
                app.screen.query_one("#include-fees", Checkbox).value = False
                await self.click(app, pilot, "#cancel")
                await self.click(app, pilot, "#continue")
                await pilot.press("enter")
                await pilot.pause()
                self.assertIn("Transaction fee flows: hidden", str(app.screen.query_one("#case-summary", Static).render()))
                await self.click(app, pilot, "#case-settings")
                self.assertFalse(app.screen.query_one("#include-fees", Checkbox).value)
                self.assertEqual(app.screen.query_one("#hops", Input).value, "1")
                app.screen.query_one("#include-fees", Checkbox).value = True
                await self.click(app, pilot, "#cancel")
                process.assert_not_called()
                self.assertEqual((case / "case.json").read_bytes(), original)

    async def test_organize_requires_a_saved_board_and_completed_run(self):
        from textual.widgets import Button
        case = create_investigation(self.root, "No run yet", board="DEMO=")
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run") as process:
            async with app.run_test(size=(110, 55)) as pilot:
                await self.click(app, pilot, "#continue")
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(app.screen.case, case)
                self.assertTrue(app.screen.query_one("#layout", Button).disabled)
                process.assert_not_called()

    async def test_organize_confirmation_uses_saved_case_and_preserves_trace_evidence(self):
        from textual.widgets import Button, Checkbox, Input, Static
        from liquid_tracer.cli import main
        fixture = PROJECT / "examples" / "demo-api.json"
        case = create_investigation(self.root, "Synthetic layout", board="DEMO=", fixture=str(fixture))
        with contextlib.redirect_stdout(io.StringIO()):
            status = main(["trace", "--case", str(case), "--fixture", str(fixture),
                           "--seeds-file", str(PROJECT / "examples" / "demo-seeds.txt"), "--hops", "1"])
        self.assertEqual(status, 0)
        before = read_case(case)
        run_files = {p.relative_to(case): p.read_bytes() for p in (case / "runs").rglob("*") if p.is_file()}
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as process, \
                patch.object(app, "suspend", side_effect=contextlib.nullcontext) as suspend:
            async with app.run_test(size=(110, 55)) as pilot:
                await self.click(app, pilot, "#continue")
                await pilot.press("enter")
                await pilot.pause()
                self.assertFalse(app.screen.query_one("#layout", Button).disabled)
                await self.click(app, pilot, "#layout")
                self.assertEqual(app.screen.query_one("#board", Input).value, "DEMO=")
                self.assertEqual(app.focused.id, "cancel")
                self.assertIn("replaces their current positions", str(app.screen.query_one("#layout-notice", Static).render()))
                self.assertIn("hidden", str(app.screen.query_one("#fee-status", Static).render()))
                await pilot.press("enter")
                await pilot.pause()
                process.assert_not_called()
                suspend.assert_not_called()

                await self.click(app, pilot, "#case-settings")
                app.screen.query_one("#include-fees", Checkbox).value = True
                await self.click(app, pilot, "#submit")
                await self.click(app, pilot, "#preview")
                self.assertIn("included", str(app.screen.query_one("#fee-status", Static).render()))
                await self.click(app, pilot, "#cancel")
                process.assert_not_called()
                await self.click(app, pilot, "#layout")
                self.assertIn("included", str(app.screen.query_one("#fee-status", Static).render()))
                await self.click(app, pilot, "#submit")
                process.assert_called_once()
                suspend.assert_called_once()
                self.assertEqual(process.call_args.args[0], ["/nix/store/test-secretspec/bin/secretspec", "--file",
                    str(PROJECT / "secretspec.toml"), "run", "--provider", "protonpass", "--profile", "development", "--",
                    sys.executable, "-m", "liquid_tracer", "miro-sync", "--case", str(case), "--run", "latest",
                    "--board", "DEMO=", "--max-new-items", "750", "--reorganize"])
                for field in ("capture_output", "stdout", "stderr"):
                    self.assertNotIn(field, process.call_args.kwargs)
                self.assertIn("Miro graph synced and reorganized", str(app.screen.query_one("#action-status", Static).render()))
                self.assertFalse(app.busy)
                self.assertEqual(read_case(case)["latest_run"], before["latest_run"])
                self.assertEqual(run_files, {p.relative_to(case): p.read_bytes()
                                            for p in (case / "runs").rglob("*") if p.is_file()})

                update_case(case, {"miro_board": None})
                app.screen.update_summary()
                self.assertTrue(app.screen.query_one("#layout", Button).disabled)

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

    async def test_create_board_navigation_cancel_and_invalid_names_never_load_credentials(self):
        from textual.widgets import Button, Input, Select, Static
        case = create_investigation(self.root, "Synthetic board setup", seeds=["a" * 64 + ":0"])
        original = (case / "case.json").read_bytes()
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run") as process:
            async with app.run_test(size=(110, 55)) as pilot:
                await self.click(app, pilot, "#continue")
                await pilot.press("enter")
                await pilot.pause()
                self.assertFalse(app.screen.query_one("#create-board", Button).disabled)
                await self.click(app, pilot, "#create-board")
                self.assertEqual(app.screen.query_one("#board-name", Input).value, "Synthetic board setup")
                self.assertEqual(app.screen.query_one("#board-visibility", Select).value, "private")
                self.assertEqual(app.screen.query_one("#board-team", Input).value, "")
                self.assertEqual(app.focused.id, "cancel")
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(app.screen.case, case)
                self.assertFalse(app.screen.query_one("#create-board", Button).disabled)
                process.assert_not_called()

                await self.click(app, pilot, "#create-board")
                for name in ("", "   ", "x" * 61):
                    with self.subTest(name=name):
                        app.screen.query_one("#board-name", Input).value = name
                        await self.click(app, pilot, "#submit")
                        self.assertIn("1 to 60 characters", str(app.screen.query_one("#form-error", Static).render()))
                        self.assertFalse(app.busy)
                        process.assert_not_called()
                await pilot.press("escape")
                await pilot.pause()
                self.assertEqual(app.screen.case, case)
                self.assertEqual((case / "case.json").read_bytes(), original)
                self.assertFalse((case / "runs").exists())
                self.assertFalse((case / "miro").exists())

    async def test_create_board_after_run_saves_selection_and_preserves_evidence_across_restart(self):
        from textual.widgets import Button, Input, Select, Static
        from liquid_tracer.cli import main
        fixture = PROJECT / "examples" / "demo-api.json"
        case = create_investigation(self.root, "Synthetic board case", fixture=str(fixture))
        with contextlib.redirect_stdout(io.StringIO()):
            status = main(["trace", "--case", str(case), "--fixture", str(fixture),
                           "--seeds-file", str(PROJECT / "examples" / "demo-seeds.txt"), "--hops", "1"])
        self.assertEqual(status, 0)
        before = read_case(case)
        run_files = {p.relative_to(case): p.read_bytes() for p in (case / "runs").rglob("*") if p.is_file()}
        selected_name = "Synthetic graph with spaces"

        def save_created_board(command, **kwargs):
            self.assertEqual(command, ["/nix/store/test-secretspec/bin/secretspec", "--file",
                str(PROJECT / "secretspec.toml"), "run", "--provider", "protonpass",
                "--profile", "development", "--", sys.executable, "-m", "liquid_tracer",
                "miro-create-board", "--case", str(case), "--name", selected_name,
                "--visibility", "team", "--team-id", "SYNTHETIC-TEAM"])
            self.assertNotIn("capture_output", kwargs)
            self.assertNotIn("stdout", kwargs)
            self.assertNotIn("stderr", kwargs)
            self.assertFalse(kwargs["check"])
            update_case(case, {"miro_board": "CREATED="})
            return subprocess.CompletedProcess(command, 0)

        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run", side_effect=save_created_board) as process, \
                patch.object(app, "suspend", side_effect=contextlib.nullcontext) as suspend:
            async with app.run_test(size=(110, 55)) as pilot:
                await self.click(app, pilot, "#continue")
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(app.screen.case, case)
                self.assertFalse(app.screen.query_one("#create-board", Button).disabled)
                await self.click(app, pilot, "#create-board")
                self.assertEqual(app.screen.query_one("#board-name", Input).value,
                                 "SYNTHETIC DEMO · Synthetic board case")
                app.screen.query_one("#board-name", Input).value = selected_name
                app.screen.query_one("#board-visibility", Select).value = "team"
                app.screen.query_one("#board-team", Input).value = "SYNTHETIC-TEAM"
                process.assert_not_called()
                await self.click(app, pilot, "#submit")
                process.assert_called_once()
                suspend.assert_called_once()
                self.assertFalse(app.busy)
                self.assertIn("Miro board saved", str(app.screen.query_one("#action-status", Static).render()))
                self.assertIn("https://miro.com/app/board/CREATED=/",
                              str(app.screen.query_one("#case-summary", Static).render()))
                self.assertTrue(app.screen.query_one("#create-board", Button).disabled)
                self.assertEqual(read_case(case)["miro_board"], "CREATED=")
                self.assertEqual(read_case(case)["latest_run"], before["latest_run"])

        restarted = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run") as process:
            async with restarted.run_test(size=(110, 55)) as pilot:
                await self.click(restarted, pilot, "#continue")
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(restarted.screen.case, case)
                self.assertTrue(restarted.screen.query_one("#create-board", Button).disabled)
                self.assertIn("https://miro.com/app/board/CREATED=/",
                              str(restarted.screen.query_one("#case-summary", Static).render()))
                await self.click(restarted, pilot, "#preview")
                self.assertEqual(restarted.screen.query_one("#board", Input).value, "CREATED=")
                await pilot.press("enter")
                await pilot.pause()
                process.assert_not_called()
        self.assertEqual(run_files, {p.relative_to(case): p.read_bytes()
                                    for p in (case / "runs").rglob("*") if p.is_file()})
        self.assertEqual(read_case(case)["latest_run"], before["latest_run"])

    async def test_failed_board_creation_keeps_private_defaults_and_does_not_retry(self):
        from textual.widgets import Button, Static
        case = create_investigation(self.root, "Synthetic rejected board")
        original = (case / "case.json").read_bytes()
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run", return_value=subprocess.CompletedProcess([], 1)) as process, \
                patch.object(app, "suspend", side_effect=contextlib.nullcontext) as suspend:
            async with app.run_test(size=(110, 55)) as pilot:
                await self.click(app, pilot, "#continue")
                await pilot.press("enter")
                await pilot.pause()
                await self.click(app, pilot, "#create-board")
                process.assert_not_called()
                await self.click(app, pilot, "#submit")
                process.assert_called_once()
                suspend.assert_called_once()
                command = process.call_args.args[0]
                self.assertEqual(command[0], "/nix/store/test-secretspec/bin/secretspec")
                self.assertEqual(command[12:], ["miro-create-board", "--case", str(case), "--name",
                                               "Synthetic rejected board", "--visibility", "private"])
                self.assertNotIn("capture_output", process.call_args.kwargs)
                self.assertFalse(app.busy)
                self.assertFalse(app.screen.query_one("#create-board", Button).disabled)
                self.assertIn("Board creation did not complete",
                              str(app.screen.query_one("#action-status", Static).render()))
                self.assertEqual((case / "case.json").read_bytes(), original)
                await self.click(app, pilot, "#create-board")
                await pilot.press("enter")
                await pilot.pause()
                process.assert_called_once()

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
