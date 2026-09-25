"""The terminal's primary workflow separates collection, local plots and boards."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import TraceError, save_json
from liquid_tracer.investigations import create_investigation, read_case
from liquid_tracer.menu import create_app
from liquid_tracer.workflow_menu import _endpoint_summary, boards_screen, plot_arguments, plot_screen

HAS_TEXTUAL = importlib.util.find_spec("textual") is not None


class PlotCommandTests(unittest.TestCase):
    def test_every_goal_uses_the_selected_saved_run_without_live_credentials(self):
        for goal in ("full", "connections", "pegouts"):
            arguments, live = plot_arguments(Path("case with spaces"), goal, "saved-run", "2", "10")
            self.assertFalse(live)
            self.assertEqual(arguments[:7], ["plot", "--case", "case with spaces", "--goal", goal, "--run", "saved-run"])
            self.assertNotIn("--seed", arguments)
            self.assertNotIn("--txid", arguments)
            if goal == "full":
                self.assertNotIn("--max-hops", arguments)
            else:
                self.assertEqual(arguments[arguments.index("--min-hops") + 1], "2" if goal == "pegouts" else "0")
                self.assertEqual(arguments[arguments.index("--max-hops") + 1], "10")

    def test_missing_collection_and_invalid_ranges_do_not_start_work(self):
        for goal, run, minimum, maximum in [("full", None, "0", "10"), ("wrong", "run", "0", "10"),
                ("pegouts", "run", "3", "2"), ("connections", "run", "0", "-1")]:
            with self.subTest(goal=goal, run=run, minimum=minimum, maximum=maximum), self.assertRaises(TraceError):
                plot_arguments("case", goal, run, minimum, maximum)

    def test_optional_endpoints_are_pegout_only_and_never_enable_live_collection(self):
        arguments, live = plot_arguments("case", "pegouts", "run", include_unspent=True, include_unspendable=True,
                                        include_context=True)
        self.assertFalse(live)
        self.assertEqual(arguments[-4:], ["--include-unspent", "--include-unspendable", "--include-context", "--open"])
        for key in ("include_unspent", "include_unspendable", "include_context"):
            for value in (None, 1, "true"):
                with self.subTest(option=key, value=value), self.assertRaises(TraceError):
                    plot_arguments("case", "pegouts", "run", **{key: value})
            for goal in ("full", "connections"):
                with self.subTest(option=key, goal=goal), self.assertRaises(TraceError):
                    plot_arguments("case", goal, "run", **{key: True})

    def test_saved_plot_summary_identifies_selected_endpoint_types_even_with_no_matches(self):
        self.assertEqual(_endpoint_summary({"goal": "pegouts", "match_count": 2}), "2 peg-outs")
        self.assertEqual(_endpoint_summary({"goal": "pegouts", "match_count": 0,
            "query": {"include_unspent": True, "include_unspendable": True},
            "endpoint_counts": {"pegout": 0, "unspent": 3, "unspendable": 0}}),
            "0 peg-outs, 3 unspent UTXOs, 0 unspendable outputs")
        self.assertEqual(_endpoint_summary({"goal": "pegouts", "match_count": 1,
            "query": {"include_context": True}}), "1 peg-outs, context addresses included")


@unittest.skipUnless(HAS_TEXTUAL, "Install the optional tui extra")
class WorkflowMenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = create_investigation(self.root, "Shared evidence", seeds=["a" * 64 + ":0"])

    async def click(self, app, pilot, selector):
        from textual.widgets import Button
        widget = app.screen.query_one(selector, Button)
        widget.scroll_visible(immediate=True)
        widget.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    def app_for(self, factory):
        from textual.app import App
        from textual.screen import Screen
        from textual.widgets import Button
        case = self.case

        class Base(Screen):
            def action_back(self):
                self.app.pop_screen()

        class TestApp(App):
            CSS = """
            .form-panel { height: 1fr; }
            .buttons { height: auto; }
            .form-actions { height: auto; }
            .form-panel Label, .form-panel Static { height: auto; }
            """
            busy = False
            result = None

            def on_mount(self):
                self.push_screen(factory(Base, Button, case), self.finished)

            def finished(self, result):
                self.result = result

        return TestApp()

    async def test_new_case_has_visible_collection_plot_and_board_steps(self):
        from textual.widgets import Button
        app = create_app(self.root)
        async with app.run_test(size=(110, 55)) as pilot:
            app.created(self.case)
            await pilot.pause()
            self.assertIn("Collect", str(app.screen.query_one("#run", Button).label))
            self.assertTrue(app.screen.query_one("#workflow-plot", Button).disabled)
            self.assertFalse(app.screen.query_one("#workflow-boards", Button).disabled)
            await self.click(app, pilot, "#workflow-boards")
            self.assertEqual(app.screen.__class__.__name__, "BoardsScreen")
            self.assertFalse(app.screen.query_one("#workflow-create", Button).disabled)

    async def test_pegout_plot_uses_saved_collection_without_search_or_fetch(self):
        from textual.widgets import Input, Select
        run = "saved-run"
        save_json(self.case / "runs" / run / "trace.json", {})
        save_json(self.case / "case.json", {**read_case(self.case), "latest_run": run})
        app = self.app_for(plot_screen)
        async with app.run_test(size=(110, 45)) as pilot:
            app.screen.query_one("#plot-goal", Select).value = "pegouts"
            app.screen.query_one("#plot-min-hops", Input).value = "2"
            app.screen.query_one("#plot-max-hops", Input).value = "10"
            await pilot.pause()
            await self.click(app, pilot, "#plot-go")
            self.assertEqual(app.result, (["plot", "--case", str(self.case), "--goal", "pegouts", "--run", run,
                                          "--min-hops", "2", "--max-hops", "10", "--open"], False))

    async def test_terminal_options_are_visible_only_for_pegouts_and_default_off(self):
        from textual.widgets import Checkbox, Select
        run = "saved-run"
        save_json(self.case / "runs" / run / "trace.json", {})
        save_json(self.case / "case.json", {**read_case(self.case), "latest_run": run})
        app = self.app_for(plot_screen)
        async with app.run_test(size=(110, 55)) as pilot:
            self.assertFalse(app.screen.query_one("#plot-endpoints").display)
            unspent = app.screen.query_one("#plot-include-unspent", Checkbox)
            unspendable = app.screen.query_one("#plot-include-unspendable", Checkbox)
            context = app.screen.query_one("#plot-include-context", Checkbox)
            self.assertFalse(unspent.value)
            self.assertFalse(unspendable.value)
            self.assertFalse(context.value)
            app.screen.query_one("#plot-goal", Select).value = "pegouts"
            await pilot.pause()
            self.assertTrue(app.screen.query_one("#plot-endpoints").display)
            unspent.value = unspendable.value = context.value = True
            await self.click(app, pilot, "#plot-go")
            self.assertEqual(app.result[0][-4:], ["--include-unspent", "--include-unspendable", "--include-context", "--open"])
            self.assertFalse(app.result[1])

    async def test_hidden_terminal_options_do_not_leak_into_other_goals(self):
        from textual.widgets import Checkbox, Select
        run = "saved-run"
        save_json(self.case / "runs" / run / "trace.json", {})
        save_json(self.case / "case.json", {**read_case(self.case), "latest_run": run})
        app = self.app_for(plot_screen)
        async with app.run_test(size=(110, 55)) as pilot:
            app.screen.query_one("#plot-goal", Select).value = "pegouts"
            app.screen.query_one("#plot-include-unspent", Checkbox).value = True
            app.screen.query_one("#plot-include-context", Checkbox).value = True
            app.screen.query_one("#plot-goal", Select).value = "connections"
            await pilot.pause()
            self.assertFalse(app.screen.query_one("#plot-endpoints").display)
            await self.click(app, pilot, "#plot-go")
            self.assertNotIn("--include-unspent", app.result[0])
            self.assertNotIn("--include-context", app.result[0])

    def board_rows(self):
        return [{"id": "board-full", "name": "Full graph", "goal": "full", "board_id": "FULL=",
                 "board_url": "https://miro.com/app/board/FULL=/", "status": "linked", "can_sync": True},
                {"id": "board-pegouts", "name": "Peg-out paths", "goal": "pegouts", "board_id": "PEGOUTS=",
                 "board_url": "https://miro.com/app/board/PEGOUTS=/", "status": "synced", "can_sync": True},
                {"id": "board-legacy", "name": "Old snapshot", "goal": "pegouts", "board_id": "OLD=",
                 "board_url": "https://miro.com/app/board/OLD=/", "status": "archived_snapshot", "can_sync": False,
                 "legacy_snapshot": True, "notice": "Historical fixed snapshot."}]

    def plot_rows(self):
        return [{"preview_id": "full-plot", "goal": "full", "run_id": "run-a", "reviewable": True},
                {"preview_id": "pegout-plot", "goal": "pegouts", "run_id": "run-b", "reviewable": True,
                 "source_run_status": "complete", "source_max_hops": 10},
                {"preview_id": "stale-plot", "goal": "pegouts", "run_id": "run-c", "reviewable": False}]

    async def test_manager_pins_board_and_matching_plot_for_reorganization(self):
        from textual.widgets import Button, Select
        from textual.widgets._select import InvalidSelectValueError
        with patch("liquid_tracer.investigation_boards.list_boards", return_value=self.board_rows()), \
                patch("liquid_tracer.plots.list_plots", return_value=self.plot_rows()):
            app = self.app_for(boards_screen)
            async with app.run_test(size=(110, 55)) as pilot:
                app.screen.query_one("#workflow-board", Select).value = "board-pegouts"
                await pilot.pause()
                preview = app.screen.query_one("#workflow-preview", Select)
                with self.assertRaises(InvalidSelectValueError):
                    preview.value = "full-plot"
                with self.assertRaises(InvalidSelectValueError):
                    preview.value = "stale-plot"
                preview.value = "pegout-plot"
                await pilot.pause()
                self.assertFalse(app.screen.query_one("#workflow-sync", Button).disabled)
                await self.click(app, pilot, "#workflow-reorganize")
                self.assertEqual(app.result, (["investigation-board-sync", "--case", str(self.case), "--record", "board-pegouts",
                                              "--preview", "pegout-plot", "--max-items", "750", "--reorganize"], True))

    async def test_legacy_board_can_open_but_cannot_receive_managed_sync(self):
        from textual.widgets import Button, Select
        with patch("liquid_tracer.investigation_boards.list_boards", return_value=self.board_rows()), \
                patch("liquid_tracer.plots.list_plots", return_value=self.plot_rows()), \
                patch("liquid_tracer.workflow_menu.webbrowser.open") as opened:
            app = self.app_for(boards_screen)
            async with app.run_test(size=(110, 55)) as pilot:
                app.screen.query_one("#workflow-board", Select).value = "board-legacy"
                await pilot.pause()
                app.screen.query_one("#workflow-preview", Select).value = "pegout-plot"
                await pilot.pause()
                self.assertTrue(app.screen.query_one("#workflow-sync", Button).disabled)
                self.assertTrue(app.screen.query_one("#workflow-reorganize", Button).disabled)
                await self.click(app, pilot, "#workflow-open-board")
                opened.assert_called_once_with("https://miro.com/app/board/OLD=/")

    async def test_board_creation_and_linking_work_before_any_plot(self):
        from textual.widgets import Input, Select
        for action, command, live in [("workflow-create", "investigation-board-create", True),
                                      ("workflow-link", "investigation-board-link", False)]:
            with self.subTest(action=action), patch("liquid_tracer.plots.list_plots", return_value=[]):
                app = self.app_for(boards_screen)
                async with app.run_test(size=(110, 55)) as pilot:
                    app.screen.query_one("#workflow-goal", Select).value = "connections"
                    app.screen.query_one("#workflow-name", Input).value = "Connections board"
                    app.screen.query_one("#workflow-link-target", Input).value = "https://miro.com/app/board/LINKED=/"
                    await self.click(app, pilot, "#" + action)
                    expected = [command, "--case", str(self.case), "--goal", "connections", "--name", "Connections board"]
                    if not live:
                        expected += ["--board", "LINKED="]
                    self.assertEqual(app.result, (expected, live))

    async def test_uncertain_board_creation_links_acknowledged_id_to_same_record(self):
        from textual.widgets import Input, Select
        pending = {"id": "pending-board", "name": "Recovered board", "goal": "pegouts", "board_id": None,
                   "status": "pending_creation", "can_sync": False}
        with patch("liquid_tracer.investigation_boards.list_boards", return_value=[pending]), \
                patch("liquid_tracer.plots.list_plots", return_value=[]):
            app = self.app_for(boards_screen)
            async with app.run_test(size=(110, 55)) as pilot:
                app.screen.query_one("#workflow-board", Select).value = "pending-board"
                app.screen.query_one("#workflow-link-target", Input).value = "ACKNOWLEDGED="
                await pilot.pause()
                await self.click(app, pilot, "#workflow-recover")
                self.assertEqual(app.result, (["investigation-board-link", "--case", str(self.case), "--goal", "pegouts",
                                              "--name", "Recovered board", "--board", "ACKNOWLEDGED=",
                                              "--record", "pending-board"], False))

    async def test_interrupted_sync_selects_its_exact_plot_and_keeps_retry_available(self):
        from textual.widgets import Button, Select
        from textual.widgets._select import InvalidSelectValueError
        board = {**self.board_rows()[1], "status": "interrupted", "pending_count": 2, "preview_id": "pegout-plot"}
        plots = self.plot_rows() + [{"preview_id": "newer-plot", "goal": "pegouts", "run_id": "run-d", "reviewable": True}]
        with patch("liquid_tracer.investigation_boards.list_boards", return_value=[board]), \
                patch("liquid_tracer.plots.list_plots", return_value=plots):
            app = self.app_for(boards_screen)
            async with app.run_test(size=(110, 55)) as pilot:
                app.screen.query_one("#workflow-board", Select).value = "board-pegouts"
                await pilot.pause()
                selected = app.screen.query_one("#workflow-preview", Select)
                self.assertEqual(selected.value, "pegout-plot")
                with self.assertRaises(InvalidSelectValueError):
                    selected.value = "newer-plot"
                self.assertFalse(app.screen.query_one("#workflow-sync", Button).disabled)
                await self.click(app, pilot, "#workflow-sync")
                self.assertEqual(app.result[0][app.result[0].index("--preview") + 1], "pegout-plot")


if __name__ == "__main__":
    unittest.main()
