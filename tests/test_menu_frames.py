"""Frame creation is a separate, deliberate live terminal action."""

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
from liquid_tracer.investigations import create_investigation, update_case
from liquid_tracer.menu import create_app


PROJECT = Path(__file__).resolve().parents[1]
HAS_TEXTUAL = importlib.util.find_spec("textual") is not None


@unittest.skipUnless(HAS_TEXTUAL, "Install the optional tui extra")
class MiroFrameMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_framing_requires_explicit_submit_and_uses_only_saved_run_and_item_budget(self):
        from textual.widgets import Button, Static
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cases"
            fixture = PROJECT / "tests/data/synthetic-api.json"
            case = create_investigation(root, "Finished graph", board="SYNTHETIC=", fixture=str(fixture),
                                        run_defaults={"max_new_items": 123})
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["trace", "--case", str(case), "--fixture", str(fixture),
                                       "--seeds-file", str(PROJECT / "tests/data/synthetic-seeds.txt"), "--hops", "0"]), 0)
            before = {str(p.relative_to(case)): p.read_bytes() for p in (case / "runs").rglob("*") if p.is_file()}
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
                    patch.object(app, "suspend", side_effect=contextlib.nullcontext) as suspend:
                async with app.run_test(size=(110, 55)) as pilot:
                    await click(pilot, "#continue")
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertFalse(app.screen.query_one("#frames", Button).disabled)
                    await click(pilot, "#frames")
                    self.assertEqual(app.focused.id, "cancel")
                    self.assertIn("does not trace", str(app.screen.query_one("#frames-notice", Static).render()))
                    self.assertFalse(app.screen.query("#layout_attempts"))
                    await pilot.press("enter")
                    await pilot.pause()
                    process.assert_not_called()
                    suspend.assert_not_called()
                    await click(pilot, "#frames")
                    await click(pilot, "#submit")
                    process.assert_called_once()
                    command = process.call_args.args[0]
                    self.assertEqual(command[0], "/test/secretspec")
                    start = command.index("miro-frames")
                    self.assertEqual(command[start:], ["miro-frames", "--case", str(case), "--run", "latest",
                                                       "--board", "SYNTHETIC=", "--max-new-items", "123"])
                    self.assertIn("Miro export frames updated", str(app.screen.query_one("#action-status", Static).render()))
                    self.assertEqual(before, {str(p.relative_to(case)): p.read_bytes()
                                              for p in (case / "runs").rglob("*") if p.is_file()})
                    update_case(case, {"miro_board": None})
                    app.screen.update_summary()
                    self.assertTrue(app.screen.query_one("#frames", Button).disabled)


if __name__ == "__main__":
    unittest.main()
