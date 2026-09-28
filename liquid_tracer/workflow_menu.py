"""Terminal collection, saved-data plotting and investigation board workspace."""

import webbrowser
from pathlib import Path

from .common import TraceError
from .investigations import read_case


GOALS = [("Full investigation", "full"), ("Starter connections", "connections"),
         ("Paths to peg-outs", "pegouts")]
GOAL_NAMES = dict((value, label) for label, value in GOALS)
ERRORS = (TraceError, ValueError, OSError, KeyError, TypeError)


def _endpoint_summary(plot):
    if plot.get("goal") != "pegouts":
        return ""
    query, counts = plot.get("query", {}), plot.get("endpoint_counts", {})
    labels = [f"{counts.get('pegout', plot.get('match_count', 0))} peg-outs"]
    if query.get("include_unspent"):
        labels.append(f"{counts.get('unspent', 0)} unspent UTXOs")
    if query.get("include_unspendable"):
        labels.append(f"{counts.get('unspendable', 0)} unspendable outputs")
    if query.get("include_context"):
        labels.append("context addresses included" +
                      (" (isolated inputs grouped)" if plot.get("layout_settings", {}).get("group_context_inputs") else ""))
    return ", ".join(labels)


def _compatible_plot(plot, board):
    if not board or not plot.get("reviewable") or plot.get("goal") != board.get("goal"):
        return False
    if board.get("status") == "interrupted" or board.get("pending_count"):
        return plot.get("preview_id") == board.get("preview_id")
    mode = plot.get("layout_mode")
    if mode == "update":
        return plot.get("board_record_id") == board.get("id") and plot.get("board_id") == board.get("board_id")
    if mode == "fresh":
        return board.get("status") != "synced" and plot.get("preview_id") == board.get("creation_preview_id")
    return True  # Saved layouts from before board-aware plotting keep their existing workflow.


def plot_arguments(case, goal, run, minimum="0", maximum="10", *, include_unspent=False, include_unspendable=False,
                   include_context=False, layout_mode="fresh", board_record_id=None):
    """Validate terminal fields; plotting always uses an explicit saved run."""
    if goal not in GOAL_NAMES:
        raise TraceError("Choose a plotting goal")
    if type(include_unspent) is not bool or type(include_unspendable) is not bool:
        raise TraceError("Additional endpoint options must be true or false")
    if goal != "pegouts" and (include_unspent or include_unspendable):
        raise TraceError("Additional endpoint options apply only to peg-out paths plots")
    if type(include_context) is not bool:
        raise TraceError("Include context addresses must be true or false")
    if goal != "pegouts" and include_context:
        raise TraceError("Include context addresses applies only to peg-out paths plots")
    if not isinstance(run, str) or not run:
        raise TraceError("Collect transaction data first, then choose a saved run")
    if layout_mode not in ("fresh", "update"):
        raise TraceError("Choose a fresh layout or an update for an existing board")
    if layout_mode == "update" and (not isinstance(board_record_id, str) or not board_record_id):
        raise TraceError("Choose a Miro board for the update layout")
    if layout_mode == "fresh" and board_record_id is not None:
        raise TraceError("A fresh layout does not use an existing board")
    arguments = ["plot", "--case", str(case), "--goal", goal, "--run", run]
    if goal != "full":
        lower, upper = int(minimum) if goal == "pegouts" else 0, int(maximum)
        if not 0 <= lower <= upper <= 2147483647:
            raise TraceError("Use whole-number hops from 0 to 2147483647; minimum cannot exceed maximum")
        arguments += ["--min-hops", str(lower), "--max-hops", str(upper)]
    if include_unspent:
        arguments.append("--include-unspent")
    if include_unspendable:
        arguments.append("--include-unspendable")
    if include_context:
        arguments.append("--include-context")
    if layout_mode == "update":
        arguments += ["--layout-mode", "update", "--board-record-id", board_record_id]
    return arguments + ["--open"], layout_mode == "update"


def plot_screen(base, button, case):
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.widgets import Checkbox, Footer, Header, Input, Label, Select, Static
    from .investigation_boards import list_boards

    class PlotScreen(base):
        def compose(self):
            metadata = read_case(case)
            runs = [path.parent.name for path in sorted((Path(case) / "runs").glob("*/trace.json"), reverse=True)]
            selected = metadata.get("latest_run")
            self.boards = [board for board in list_boards(case) if board.get("can_sync")]
            yield Header()
            with VerticalScroll(classes="form-panel"):
                yield Label("2. Plot layouts", classes="title")
                yield Static("Choose a goal using collected transaction data. Fresh layouts run locally. Update layouts read "
                             "the selected Miro board and arrange new objects in an empty area. Plotting never changes Miro.", markup=False)
                yield Label("Saved collection run")
                yield Select([(run, run) for run in runs], value=selected if selected in runs else Select.BLANK,
                             id="plot-run", prompt="Collect transaction data first")
                yield Label("Plotting goal")
                yield Select(GOALS, value="full", allow_blank=False, id="plot-goal")
                yield Label("Layout source")
                yield Select([("Fresh layout for a new board", "fresh"),
                              ("Update an existing Miro board", "update")], value="fresh", allow_blank=False, id="plot-mode")
                with Vertical(id="plot-board-field"):
                    yield Label("Miro board to read")
                    yield Select([], id="plot-board", prompt="Choose a board matching the plotting goal")
                yield Static("Full investigation: all displayed activity. Starter connections: verified paths between "
                             "starting transactions. Paths to peg-outs: verified paths ending in matching requests.", markup=False)
                with Vertical(id="plot-range"):
                    with Vertical(id="plot-minimum"):
                        yield Label("Minimum transaction hops, inclusive")
                        yield Input(value="0", id="plot-min-hops", type="integer")
                    yield Label("Maximum transaction hops, inclusive")
                    yield Input(value="10", id="plot-max-hops", type="integer")
                with Vertical(id="plot-endpoints"):
                    yield Checkbox("Include unspent UTXOs", id="plot-include-unspent")
                    yield Checkbox("Include unspendable outputs", id="plot-include-unspendable")
                    yield Static("Peg-outs are always included. Unspent means recorded as unspent in this saved collection; "
                                 "it is not a live balance check. Fee outputs are excluded.", markup=False)
                    yield Checkbox("Include context addresses", id="plot-include-context")
                    yield Static("Show other input addresses and spendable sibling outputs around the selected path transactions. "
                                 "Context does not extend the trace or add matching endpoints. Grouping follows the saved "
                                 "Group isolated context inputs layout setting.", markup=False)
                yield Static("Hop limits filter the saved data. They do not collect additional transactions. "
                             "A starting transaction is hop 0. Missing matches may reflect incomplete coverage.", markup=False)
                yield Static("", id="workflow-error", markup=False)
            with Horizontal(classes="buttons form-actions"):
                yield button("Back", id="workflow-back")
                yield button("Plot saved data", id="plot-go", variant="primary", disabled=not runs)
            yield Footer()

        def on_mount(self):
            self._range_visibility()

        def _range_visibility(self):
            goal = self.query_one("#plot-goal", Select).value
            self.query_one("#plot-range").display = goal != "full"
            self.query_one("#plot-range").styles.height = "auto"
            self.query_one("#plot-minimum").display = goal == "pegouts"
            self.query_one("#plot-minimum").styles.height = "auto"
            self.query_one("#plot-endpoints").display = goal == "pegouts"
            self.query_one("#plot-endpoints").styles.height = "auto"
            self.query_one("#plot-board-field").display = self.query_one("#plot-mode", Select).value == "update"
            self.query_one("#plot-board-field").styles.height = "auto"
            self.query_one("#plot-board", Select).set_options([
                (board["name"], board["id"]) for board in self.boards if board["goal"] == goal])

        def on_select_changed(self, event):
            if event.select.id in ("plot-goal", "plot-mode"):
                self._range_visibility()

        def on_button_pressed(self, event):
            event.stop()
            if self.app.busy:
                return
            if event.button.id == "workflow-back":
                self.action_back()
            elif event.button.id == "plot-go":
                try:
                    goal = self.query_one("#plot-goal", Select).value
                    mode = self.query_one("#plot-mode", Select).value
                    self.dismiss(plot_arguments(case, goal,
                        self.query_one("#plot-run", Select).value,
                        self.query_one("#plot-min-hops", Input).value,
                        self.query_one("#plot-max-hops", Input).value,
                        include_unspent=goal == "pegouts" and self.query_one("#plot-include-unspent", Checkbox).value,
                        include_unspendable=goal == "pegouts" and self.query_one("#plot-include-unspendable", Checkbox).value,
                        include_context=goal == "pegouts" and self.query_one("#plot-include-context", Checkbox).value,
                        layout_mode=mode, board_record_id=self.query_one("#plot-board", Select).value if mode == "update" else None))
                except ERRORS as error:
                    self.query_one("#workflow-error", Static).update(str(error))

    return PlotScreen()


def boards_screen(base, button, case):
    from textual.containers import Horizontal, VerticalScroll
    from textual.widgets import Footer, Header, Input, Label, Select, Static
    from .cli import board_id
    from .investigation_boards import list_boards
    from .plots import list_plots, reviewed_plot

    class BoardsScreen(base):
        def compose(self):
            self.boards = {row["id"]: row for row in list_boards(case)}
            self.plots = {row["preview_id"]: row for row in list_plots(case)}
            self.settings = read_case(case).get("run_defaults", {})
            yield Header()
            with VerticalScroll(classes="form-panel"):
                yield Label("3. Miro boards", classes="title")
                yield Static("Create and sync a fresh plot to a new board. For an existing board, generate an update layout "
                             "that reads its arrangement, then sync the additions and removals. Existing positions stay in place.", markup=False)
                yield Label("Investigation boards")
                yield Select([(f"{row['name']} | {GOAL_NAMES.get(row['goal'], row['goal'])} | {row['status']}", identity)
                              for identity, row in self.boards.items()], id="workflow-board", prompt="Choose a board")
                yield Static("Choose a board to view its status and compatible plots." if self.boards else
                             "No boards yet. Create or link a board below, before or after plotting.",
                             id="workflow-board-status", markup=False)
                yield button("Open board in Miro", id="workflow-open-board", disabled=True)
                yield Label("Saved plot for the selected board")
                yield Select([], id="workflow-preview", prompt="Choose a board first")
                yield Static("", id="workflow-preview-status", markup=False)
                yield button("Review saved plot", id="workflow-review", disabled=True)
                yield Label("Maximum new Miro items for this sync")
                yield Input(value=str(self.settings.get("max_new_items", 750)), id="workflow-max-items", type="integer")
                with Horizontal(classes="buttons"):
                    yield button("Update board", id="workflow-sync", disabled=True)
                    yield button("Sync and reorganize", id="workflow-reorganize", disabled=True)
                yield Label("Create or link another board", classes="title")
                yield Select(GOALS, value="full", allow_blank=False, id="workflow-goal")
                yield Label("Board name")
                yield Input(value=read_case(case).get("name", "Investigation")[:60], id="workflow-name")
                yield Label("Fresh layout for a new board")
                yield Select([(f"{GOAL_NAMES.get(plot['goal'], plot['goal'])} | {plot['preview_id']}", plot["preview_id"])
                              for plot in self.plots.values() if plot.get("reviewable") and not plot.get("empty")
                              and plot.get("layout_mode", "fresh") == "fresh"],
                             id="workflow-create-preview", prompt="Choose a fresh saved layout")
                yield button("Create board and sync", id="workflow-create-sync")
                yield Static("New boards are private. The selected fresh layout determines the board's plotting goal.", markup=False)
                yield button("Create empty board (legacy)", id="workflow-create")
                yield Label("Existing Miro board URL or ID")
                yield Input(id="workflow-link-target")
                yield button("Link existing board", id="workflow-link")
                yield button("Link acknowledged board for selected creation", id="workflow-recover", disabled=True)
                yield Static("Linking saves the destination locally. Legacy immutable snapshots remain available to open; "
                             "create a managed board to update their goal with later saved plots.", markup=False)
                yield Static("", id="workflow-error", markup=False)
            with Horizontal(classes="buttons form-actions"):
                yield button("Back", id="workflow-back")
            yield Footer()

        def _board(self):
            selected = self.query_one("#workflow-board", Select).value
            if selected not in self.boards:
                raise TraceError("Choose an investigation board")
            return self.boards[selected]

        def _plot(self):
            selected = self.query_one("#workflow-preview", Select).value
            if selected not in self.plots:
                raise TraceError("Choose a saved plot for this board")
            row = self.plots[selected]
            if not _compatible_plot(row, self._board()):
                raise TraceError("Generate a current plot for this board's goal before syncing")
            board = self._board()
            if (board.get("status") == "interrupted" or board.get("pending_count")) and selected != board.get("preview_id"):
                raise TraceError("Resume the exact saved plot for this board's interrupted sync")
            return row

        def on_select_changed(self, event):
            if event.select.id == "workflow-board":
                selected = self.query_one("#workflow-board", Select).value
                row = self.boards.get(selected)
                resuming = bool(row and (row.get("status") == "interrupted" or row.get("pending_count")))
                compatible = [plot for plot in self.plots.values()
                    if _compatible_plot(plot, row)]
                self.query_one("#workflow-preview", Select).set_options([
                    (f"{plot['run_id']} | {plot['preview_id']}" +
                     (" | " + _endpoint_summary(plot) if plot.get("goal") == "pegouts" else ""),
                     plot["preview_id"]) for plot in compatible])
                if resuming and compatible:
                    self.query_one("#workflow-preview", Select).value = compatible[0]["preview_id"]
                self.query_one("#workflow-board-status", Static).update(
                    (f"{row['name']}: {row['status']}\n{row.get('board_url') or 'Board creation not acknowledged'}\n"
                     f"{row.get('notice') or ''}") if row else "Choose an investigation board.")
                self.query_one("#workflow-open-board").disabled = not (row and row.get("board_id"))
                self.query_one("#workflow-recover").disabled = not (row and
                    row.get("status") in ("pending_creation", "creation_rejected"))
            if event.select.id in ("workflow-board", "workflow-preview"):
                row = self.boards.get(self.query_one("#workflow-board", Select).value)
                plot = self.plots.get(self.query_one("#workflow-preview", Select).value)
                ready = bool(plot and _compatible_plot(plot, row))
                self.query_one("#workflow-review").disabled = not ready
                for selector in ("#workflow-sync", "#workflow-reorganize"):
                    self.query_one(selector).disabled = not (ready and row.get("can_sync")
                        and (not plot.get("empty") or plot.get("layout_mode") == "update")
                        and (selector != "#workflow-reorganize" or "layout_mode" not in plot))
                self.query_one("#workflow-preview-status", Static).update(
                    f"Saved run: {plot['run_id']}\nCollection status: {plot.get('source_run_status', 'unknown')}; "
                    f"hop ceiling: {plot.get('source_max_hops', 'unknown')}."
                    + ("\nEndpoints: " + _endpoint_summary(plot) if plot.get("goal") == "pegouts" else "")
                    + (" No matching activity to publish." if plot.get("empty") else "") if ready else
                    "Choose a compatible saved plot. If none are listed, return to Plot saved data.")

        def on_button_pressed(self, event):
            event.stop()
            if self.app.busy:
                return
            action = event.button.id
            if action == "workflow-back":
                self.action_back()
                return
            try:
                if action == "workflow-open-board":
                    target = board_id(self._board().get("board_id") or "")
                    webbrowser.open("https://miro.com/app/board/" + target + "/")
                    return
                if action == "workflow-review":
                    preview = self._plot()["preview_id"]
                    reviewed_plot(case, preview)
                    webbrowser.open((Path(case) / "previews" / preview / "graph.html").resolve().as_uri())
                    return
                if action == "workflow-recover":
                    row = self._board()
                    if row.get("status") not in ("pending_creation", "creation_rejected"):
                        raise TraceError("Choose a board with an unfinished creation record")
                    target = board_id(self.query_one("#workflow-link-target", Input).value)
                    self.dismiss((["investigation-board-link", "--case", str(case), "--goal", row["goal"],
                                  "--name", row["name"], "--board", target, "--record", row["id"]], False))
                elif action == "workflow-create-sync":
                    preview = self.query_one("#workflow-create-preview", Select).value
                    plot = self.plots.get(preview)
                    if not plot or not plot.get("reviewable") or plot.get("empty") or plot.get("layout_mode", "fresh") != "fresh":
                        raise TraceError("Choose a fresh saved layout for the new board")
                    name = self.query_one("#workflow-name", Input).value.strip()
                    if not name:
                        raise TraceError("Provide a board name")
                    budget = int(self.query_one("#workflow-max-items", Input).value)
                    if budget < 0:
                        raise TraceError("Use a nonnegative whole-number item budget")
                    self.dismiss((["investigation-board-create-sync", "--case", str(case), "--preview", preview,
                                   "--name", name, "--max-items", str(budget)], True))
                elif action in ("workflow-create", "workflow-link"):
                    goal = self.query_one("#workflow-goal", Select).value
                    name = self.query_one("#workflow-name", Input).value.strip()
                    if goal not in GOAL_NAMES or not name:
                        raise TraceError("Choose a goal and provide a board name")
                    command = "investigation-board-create" if action == "workflow-create" else "investigation-board-link"
                    arguments = [command, "--case", str(case), "--goal", goal, "--name", name]
                    if action == "workflow-link":
                        arguments += ["--board", board_id(self.query_one("#workflow-link-target", Input).value)]
                    self.dismiss((arguments, action == "workflow-create"))
                elif action in ("workflow-sync", "workflow-reorganize"):
                    row, plot = self._board(), self._plot()
                    if plot.get("empty") and plot.get("layout_mode") != "update":
                        raise TraceError("This saved plot has no matching activity to publish")
                    if not row.get("can_sync"):
                        raise TraceError(row.get("notice") or "This board is not available for managed sync")
                    budget = int(self.query_one("#workflow-max-items", Input).value)
                    if budget < 0:
                        raise TraceError("Use a nonnegative whole-number item budget")
                    arguments = ["investigation-board-sync", "--case", str(case), "--record", row["id"],
                                 "--preview", plot["preview_id"], "--max-items", str(budget)]
                    if action == "workflow-reorganize":
                        if plot.get("layout_mode") == "update":
                            raise TraceError("An update layout preserves the investigator's existing arrangement")
                        arguments += ["--reorganize"]
                    self.dismiss((arguments, True))
            except ERRORS as error:
                self.query_one("#workflow-error", Static).update(str(error))

    return BoardsScreen()
