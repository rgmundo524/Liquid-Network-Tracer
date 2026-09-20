"""Terminal rebuild confirmation preserves the old board and retry identity."""

import contextlib
import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import main
from liquid_tracer.common import TraceError
from liquid_tracer.investigations import create_investigation, read_case, update_case
from liquid_tracer.menu import create_app

PROJECT = Path(__file__).resolve().parents[1]
HAS_TEXTUAL = importlib.util.find_spec("textual") is not None


@unittest.skipUnless(HAS_TEXTUAL, "Install the optional tui extra")
class BoardRebuildMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_rebuild_form_pins_snapshot_source_and_one_time_budget_then_resumes_same_board(self):
        from textual.widgets import Button, Input, Static
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cases"
            fixture = PROJECT / "tests/data/synthetic-api.json"
            case = create_investigation(root, "Updated graph", board="OLD=", fixture=str(fixture),
                                        run_defaults={"max_new_items": 750})
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["trace", "--case", str(case), "--fixture", str(fixture),
                    "--seeds-file", str(PROJECT / "tests/data/synthetic-seeds.txt"), "--hops", "0"]), 0)
            run = read_case(case)["latest_run"]
            app = create_app(root)
            async def click(pilot, selector):
                button = app.screen.query_one(selector, Button)
                button.scroll_visible(immediate=True)
                button.focus()
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
            with patch.dict(os.environ, {"LIQUID_SECRETSPEC_BIN": "/test/secretspec"}), \
                    patch("liquid_tracer.menu.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as process, \
                    patch.object(app, "suspend", side_effect=contextlib.nullcontext), \
                    patch("liquid_tracer.board_rebuild.rebuild_status", return_value=None) as status:
                async with app.run_test(size=(110, 55)) as pilot:
                    await click(pilot, "#continue")
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertFalse(app.screen.query_one("#rebuild-board", Button).disabled)
                    await click(pilot, "#rebuild-board")
                    self.assertEqual(app.focused.id, "cancel")
                    self.assertIn("manual edits stay there", str(app.screen.query_one("#rebuild-notice", Static).render()))
                    await pilot.press("enter")
                    await pilot.pause()
                    process.assert_not_called()
                    await click(pilot, "#rebuild-board")
                    app.screen.query_one("#board-name", Input).value = "Refreshed graph"
                    app.screen.query_one("#max_new_items", Input).value = "5000"
                    await click(pilot, "#submit")
                    command = process.call_args.args[0]
                    self.assertEqual(command[command.index("miro-rebuild-board"):], ["miro-rebuild-board",
                        "--case", str(case), "--run", run, "--source-board", "OLD=", "--name",
                        "Refreshed graph", "--max-new-items", "5000"])
                    self.assertEqual(read_case(case)["run_defaults"]["max_new_items"], 750)
                    update_case(case, {"miro_board": "NEW="})
                    status.return_value = {"status": "syncing", "previous_board_id": "OLD=", "board_id": "NEW=",
                                           "run_id": run, "name": "Refreshed graph", "notice": "Resume the saved rebuild."}
                    app.screen.update_summary()
                    self.assertEqual(str(app.screen.query_one("#rebuild-board", Button).label), "Resume board rebuild")
                    await click(pilot, "#rebuild-board")
                    self.assertTrue(app.screen.query_one("#board-name", Input).disabled)
                    await click(pilot, "#submit")
                    command = process.call_args.args[0]
                    self.assertEqual(command[command.index("--source-board") + 1], "OLD=")
                    self.assertEqual(command[command.index("--run") + 1], run)
                    status.side_effect = TraceError("private path")
                    app.screen.update_summary()
                    self.assertTrue(app.screen.query_one("#rebuild-board", Button).disabled)
                    self.assertFalse(app.screen.query_one("#run", Button).disabled)
                    self.assertIn("receipt is unavailable", str(app.screen.query_one("#rebuild-status", Static).render()))


if __name__ == "__main__":
    unittest.main()
