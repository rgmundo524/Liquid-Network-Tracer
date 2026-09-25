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
        labels.append("context addresses included")
    return ", ".join(labels)


def plot_arguments(case, goal, run, minimum="0", maximum="10", *, include_unspent=False, include_unspendable=False,
                   include_context=False):
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
    return arguments + ["--open"], False


def plot_screen(base, button, case):
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.widgets import Checkbox, Footer, Header, Input, Label, Select, Static

    class PlotScreen(base):
        def compose(self):
            metadata = read_case(case)
            runs = [path.parent.name for path in sorted((Path(case) / "runs").glob("*/trace.json"), reverse=True)]
            selected = metadata.get("latest_run")
            yield Header()
            with VerticalScroll(classes="form-panel"):
                yield Label("2. Plot saved data", classes="title")
                yield Static("Choose a goal using the transaction data already collected. Plotting runs locally and makes no "
                             "Blockstream or Miro requests. Collect or continue data first when you need more coverage.", markup=False)
                yield Label("Saved collection run")
                yield Select([(run, run) for run in runs], value=selected if selected in runs else Select.BLANK,
                             id="plot-run", prompt="Collect transaction data first")
                yield Label("Plotting goal")
                yield Select(GOALS, value="full", allow_blank=False, id="plot-goal")
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
                                 "Context does not extend the trace or add matching endpoints.", markup=False)
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

        def on_select_changed(self, event):
            if event.select.id == "plot-goal":
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
                    self.dismiss(plot_arguments(case, goal,
                        self.query_one("#plot-run", Select).value,
                        self.query_one("#plot-min-hops", Input).value,
                        self.query_one("#plot-max-hops", Input).value,
                        include_unspent=goal == "pegouts" and self.query_one("#plot-include-unspent", Checkbox).value,
                        include_unspendable=goal == "pegouts" and self.query_one("#plot-include-unspendable", Checkbox).value,
                        include_context=goal == "pegouts" and self.query_one("#plot-include-context", Checkbox).value))
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
                yield Static("Each board belongs to a plotting goal. Create or link it once, then choose a saved plot "
                             "to sync. Sync keeps existing positions; reorganize applies the reviewed layout.", markup=False)
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
                    yield button("Sync to Miro", id="workflow-sync", disabled=True)
                    yield button("Sync and reorganize", id="workflow-reorganize", disabled=True)
                yield Label("Create or link another board", classes="title")
                yield Select(GOALS, value="full", allow_blank=False, id="workflow-goal")
                yield Label("Board name")
                yield Input(value=read_case(case).get("name", "Investigation")[:60], id="workflow-name")
                yield Static("New boards are private. Creating a board does not publish a plot.", markup=False)
                yield button("Create Miro board", id="workflow-create")
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
            if row.get("goal") != self._board().get("goal") or not row.get("reviewable"):
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
                    if row and plot.get("goal") == row.get("goal") and plot.get("reviewable")
                    and (not resuming or plot["preview_id"] == row.get("preview_id"))]
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
                ready = bool(row and plot and plot.get("reviewable") and row.get("goal") == plot.get("goal"))
                self.query_one("#workflow-review").disabled = not ready
                for selector in ("#workflow-sync", "#workflow-reorganize"):
                    self.query_one(selector).disabled = not (ready and row.get("can_sync") and not plot.get("empty"))
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
                    if plot.get("empty"):
                        raise TraceError("This saved plot has no matching activity to publish")
                    if not row.get("can_sync"):
                        raise TraceError(row.get("notice") or "This board is not available for managed sync")
                    budget = int(self.query_one("#workflow-max-items", Input).value)
                    if budget < 0:
                        raise TraceError("Use a nonnegative whole-number item budget")
                    arguments = ["investigation-board-sync", "--case", str(case), "--record", row["id"],
                                 "--preview", plot["preview_id"], "--max-items", str(budget)]
                    if action == "workflow-reorganize":
                        arguments += ["--reorganize"]
                    self.dismiss((arguments, True))
            except ERRORS as error:
                self.query_one("#workflow-error", Static).update(str(error))

    return BoardsScreen()
