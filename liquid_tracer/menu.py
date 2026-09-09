"""Textual investigation interface; secrets are loaded only for live actions."""

import contextlib
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .common import LBTC, TraceError, parse_outpoint, read_json
from .investigations import (create_investigation, default_root, list_investigations,
                             load_settings, read_case, save_settings, update_case, validate_settings)


LIMIT_FIELDS = (
    ("hops", "Initial / additional hops", int, 0),
    ("max_transactions", "Maximum new transactions per run", int, 1),
    ("max_outpoints", "Maximum examined outputs per run", int, 1),
    ("max_requests", "Maximum API attempts per run", int, 1),
    ("max_seconds", "Maximum tracing seconds per run", float, 0),
    ("max_new_items", "Maximum new Miro items per sync", int, 0),
)
ACTION_ERRORS = (TraceError, OSError, ValueError, KeyError, TypeError)


def _project():
    return Path(os.environ.get("LIQUID_TRACER_ROOT") or Path(__file__).resolve().parents[1]).resolve()


def _environment():
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(_project()), environment.get("PYTHONPATH")) if value)
    return environment


def _command(arguments, live=False):
    command = [sys.executable, "-m", "liquid_tracer", *arguments]
    if live:
        executable = os.environ.get("LIQUID_SECRETSPEC_BIN") or "secretspec"
        command = [executable, "--file", str(_project() / "secretspec.toml"), "run",
                   "--provider", os.environ.get("LIQUID_SECRET_PROVIDER") or "protonpass",
                   "--profile", os.environ.get("LIQUID_SECRET_PROFILE") or "development",
                   "--", *command]
    return command


def _seed_values(value):
    values = [part for part in re.split(r"[\s,]+", value.strip()) if part]
    if not values:
        raise TraceError("Provide at least one starting output as HASH:NUMBER, or use Load outputs to choose one.")
    return sorted({f"{txid}:{index}" for txid, index in map(parse_outpoint, values)})


def _lookup_reports(report, txids):
    """Validate the complete lookup before offering any output for selection."""
    invalid = "Output lookup returned an invalid transaction report. No outputs were changed."
    if not isinstance(report, dict):
        raise TraceError(invalid)
    reports = [report] if len(txids) == 1 else report.get("transactions")
    if not isinstance(reports, list) or len(reports) != len(txids):
        raise TraceError(invalid)
    for txid, transaction in zip(txids, reports):
        if (not isinstance(transaction, dict) or transaction.get("txid") != txid
                or not isinstance(transaction.get("outputs"), list)):
            raise TraceError(invalid)
        for index, output in enumerate(transaction["outputs"]):
            if (not isinstance(output, dict) or type(output.get("vout")) is not int
                    or output["vout"] != index or output.get("outpoint") != f"{txid}:{index}"
                    or type(output.get("selectable")) is not bool):
                raise TraceError(invalid)
            if any(output.get(field) is not None and not isinstance(output[field], str)
                   for field in ("address", "asset", "script_type", "reason")):
                raise TraceError(invalid)
            value = output.get("value")
            if value is not None and (type(value) is not int or value < 0):
                raise TraceError(invalid)
    return reports


def _latest(case, metadata, verify=False):
    from .cli import resolve_latest, run_path, verify_export
    selected = resolve_latest(case, "latest")
    path = run_path(case, selected)
    if verify:
        verify_export(path)
    state = read_json(path / "trace.json")
    if state.get("run_id") != selected or state.get("case_id") != metadata["case_id"]:
        raise TraceError("Saved run does not match this investigation.")
    return path, state


def _status(case, metadata):
    if metadata.get("error"):
        return "Unavailable: " + str(metadata["error"])
    if not metadata.get("latest_run"):
        return "Not started"
    try:
        _, state = _latest(case, metadata)
        status = state.get("status", "unknown")
        if state.get("stop_reason"):
            status += " (" + str(state["stop_reason"]) + ")"
        return str(metadata["latest_run"]) + ", " + status
    except ACTION_ERRORS:
        return "Latest run unavailable; review saved evidence"


def _trace_arguments(case, metadata, settings):
    """Validate saved source/evidence before invoking SecretSpec or any remote call."""
    arguments = ["trace", "--case", str(case)]
    fixture = metadata.get("fixture")
    if metadata.get("latest_run"):
        _, parent = _latest(case, metadata, verify=True)
        if parent.get("source", "").startswith("fixture://") and not fixture:
            raise TraceError("This run's fixture path is not configured. Use the trace command with its original --fixture.")
        if not fixture:
            from .api import ENTERPRISE
            if parent.get("source") != ENTERPRISE:
                raise TraceError("Continue with the trace command and the original API source.")
        arguments.extend(["--resume", "latest", "--additional-hops", str(settings["hops"])])
    else:
        seeds = metadata.get("seeds")
        if not seeds:
            raise TraceError("No starting outputs are saved. Start this investigation using the trace command.")
        for seed in seeds:
            arguments.extend(["--seed", seed])
        arguments.extend(["--hops", str(settings["hops"])])
    if fixture:
        path = Path(fixture)
        if not path.is_file():
            raise TraceError("Saved synthetic fixture is unavailable: " + str(path))
        arguments.extend(["--fixture", str(path)])
    for key in ("max_transactions", "max_outpoints", "max_requests", "max_seconds"):
        arguments.extend(["--" + key.replace("_", "-"), str(settings[key])])
    return arguments, not bool(fixture)


def create_app(root=None):
    """Construct the optional TUI lazily so ordinary CLI commands stay dependency-free."""
    from textual import work
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import Screen
    from textual.widgets import Button, Checkbox, DataTable, Footer, Header, Input, Label, RichLog, Select, Static, TextArea
    from rich.text import Text

    investigation_root = (Path(root) if root is not None else default_root()).expanduser().resolve()

    class BaseScreen(Screen):
        BINDINGS = [("escape", "back", "Back")]

        def action_back(self):
            if not self.app.busy:
                self.app.pop_screen()

        def show_error(self, error):
            self.app.notify(str(error), severity="error", timeout=8)

    class FormScreen(BaseScreen):
        def __init__(self, mode, case=None):
            super().__init__()
            self.mode, self.case = mode, case
            self.metadata = read_case(case) if case else {}
            # Global defaults apply when a case is created. Missing settings on an
            # older case use built-in defaults, never later global preferences.
            self.settings = (validate_settings(self.metadata.get("run_defaults", {}))
                             if case else load_settings(investigation_root))

        def compose(self) -> ComposeResult:
            titles = {"new": "New investigation", "global": "Settings", "case": "Investigation settings",
                      "run": "Continue latest run" if self.metadata.get("latest_run") else "Start first run",
                      "preview": "Preview Miro changes", "sync": "Sync latest to Miro",
                      "layout": "Organize Miro graph"}
            yield Header()
            with VerticalScroll(classes="form-panel"):
                yield Label(titles[self.mode], classes="title")
                if self.mode in ("new", "case"):
                    yield Label("Investigation name")
                    yield Input(str(self.metadata.get("name", "")), id="case-name")
                if self.mode == "new":
                    yield Label("Data source")
                    yield Select([("Synthetic demo (offline)", "demo"), ("Live Liquid", "live")],
                                 value="demo", allow_blank=False, id="source")
                    yield Label("Transaction hashes separated by commas")
                    yield Input(placeholder="64-character hash, another hash, ...", id="lookup-txid")
                    yield Button("Load outputs", id="lookup")
                    yield Static("Load one or more transactions, then choose their starting outputs together. "
                                 "Live lookup uses SecretSpec and Blockstream. "
                                 "It may use API credits. It does not start a trace.", markup=False)
                    yield Label("Starting outputs: HASH:NUMBER (for example, :0 means output 0)")
                    yield Static("Replace NUMBER with an actual output number, not the word 'vout'. "
                                 "Separate multiple outputs with spaces, commas or new lines.", markup=False)
                    yield TextArea(id="seeds")
                if self.mode in ("new", "case", "preview", "sync", "layout"):
                    yield Label("Miro board URL or ID" + (" (optional)" if self.mode in ("new", "case") else ""))
                    yield Input(self.metadata.get("miro_board") or "", id="board")
                if self.mode == "global":
                    yield Static("Defaults apply to new investigations. Existing investigations retain their saved settings.", markup=False)
                    yield Static("Credential provider: " + (os.environ.get("LIQUID_SECRET_PROVIDER") or "protonpass")
                                 + " / profile: " + (os.environ.get("LIQUID_SECRET_PROFILE") or "development"), markup=False)
                if self.mode == "run":
                    source = "Synthetic demo: no Blockstream requests." if self.metadata.get("fixture") else "Live Liquid: running this trace may consume Blockstream credits."
                    yield Static(source, markup=False)
                    if self.metadata.get("latest_run"):
                        yield Static("Adds hops to the saved run's existing ceiling. Use 0 to retry the current frontier.", markup=False)
                    yield Static("Tracing saves a new run. Miro is updated separately.", markup=False)
                if self.mode in ("new", "global", "case"):
                    yield Checkbox("Include transaction fee flows", value=self.settings["include_fees"], id="include-fees")
                    yield Static("Graph display only. Included fees appear in a chronological row above the graph. "
                                 "Trace evidence always retains fee outputs.", markup=False)
                if self.mode in ("preview", "sync", "layout"):
                    text = ("Offline preview. This does not change case settings, saved runs or the Miro board."
                            if self.mode == "preview" else "Updates the existing board and saves its ID with this investigation.")
                    yield Static(text, markup=False)
                    yield Static("Transaction fee flows: " + ("included" if self.settings["include_fees"] else "hidden")
                                 + ". Change this in Investigation settings.", id="fee-status", markup=False)
                    if not self.settings["include_fees"]:
                        yield Static("Sync checks previously generated fee items for manual edits before removing them. "
                                     "Saved trace evidence is unchanged.", markup=False)
                    if self.mode == "layout":
                        yield Static("Arrange the graph's managed items from left to right, keeping transaction inputs "
                                     "and outputs nearby. This replaces their current positions. "
                                     "Annotations, item content and dimensions are retained. "
                                     "You can still drag items in Miro afterward.", id="layout-notice", markup=False)
                    elif self.mode == "sync":
                        yield Static("Existing item positions are retained. Choose Organize Miro graph to rearrange them.", markup=False)
                for key, label, converter, _ in LIMIT_FIELDS:
                    if self.mode in ("preview", "sync", "layout") and key != "max_new_items":
                        continue
                    yield Label(label)
                    yield Input(str(self.settings[key]), id=key,
                                type="number" if converter is float else "integer")
                yield Static("", id="form-error", markup=False)
            with Horizontal(classes="buttons form-actions"):
                yield Button("Cancel", id="cancel")
                labels = {"new": "Create investigation", "global": "Save defaults", "case": "Save settings",
                          "run": "Run trace", "preview": "Preview (offline)", "sync": "Sync to Miro",
                          "layout": "Organize graph"}
                yield Button(labels[self.mode], id="submit", variant="primary")
            yield Footer()

        def on_mount(self):
            # Run and publication require a deliberate selection; Enter initially cancels.
            if self.mode in ("run", "sync", "layout"):
                self.query_one("#cancel", Button).focus()
            elif self.mode in ("new", "case"):
                self.query_one("#case-name", Input).focus()
            elif self.mode == "preview":
                self.query_one("#board", Input).focus()
            else:
                self.query_one("#hops", Input).focus()

        def read_limits(self):
            settings = dict(self.settings)
            for key, label, converter, minimum in LIMIT_FIELDS:
                if self.mode in ("preview", "sync", "layout") and key != "max_new_items":
                    continue
                try:
                    value = converter(self.query_one("#" + key, Input).value)
                    if not math.isfinite(value) or value < minimum or (converter is float and value <= 0):
                        raise ValueError
                except ValueError:
                    qualifier = "positive number" if converter is float else f"whole number of at least {minimum}"
                    raise TraceError(label + ": enter a " + qualifier) from None
                settings[key] = value
            if self.mode in ("new", "global", "case"):
                settings["include_fees"] = self.query_one("#include-fees", Checkbox).value
            return settings

        def on_button_pressed(self, event: Button.Pressed):
            if self.app.busy:
                return
            if event.button.id == "lookup" and self.mode == "new":
                self.lookup_outputs()
                return
            if event.button.id == "cancel":
                self.dismiss(None)
                return
            if event.button.id != "submit":
                return
            try:
                from .cli import board_id
                settings = self.read_limits()
                board = None
                if self.mode in ("new", "case", "preview", "sync", "layout"):
                    value = self.query_one("#board", Input).value.strip()
                    board = board_id(value) if value else None
                if self.mode in ("new", "case"):
                    name = self.query_one("#case-name", Input).value.strip()
                    if not name:
                        raise TraceError("Enter an investigation name.")
                if self.mode == "new":
                    fixture = None
                    if self.query_one("#source", Select).value == "demo":
                        fixture = _project() / "examples" / "demo-api.json"
                        lines = (_project() / "examples" / "demo-seeds.txt").read_text().splitlines()
                        entered = self.query_one("#seeds", TextArea).text.strip()
                        seeds = _seed_values(entered or " ".join(line.split("#", 1)[0] for line in lines))
                        if not fixture.is_file():
                            raise TraceError("The synthetic demo fixture is unavailable.")
                    else:
                        seeds = _seed_values(self.query_one("#seeds", TextArea).text)
                    case = create_investigation(investigation_root, name, board=board, fixture=fixture,
                                                seeds=seeds, run_defaults=settings)
                    self.dismiss(case)
                elif self.mode == "global":
                    save_settings(investigation_root, settings)
                    self.dismiss(None)
                elif self.mode == "case":
                    update_case(self.case, {"name": name, "miro_board": board, "run_defaults": settings})
                    self.dismiss(None)
                elif self.mode == "run":
                    # Re-read at submission rather than using a stale form snapshot.
                    metadata = read_case(self.case)
                    arguments, live = _trace_arguments(self.case, metadata, settings)
                    update_case(self.case, {"run_defaults": settings})
                    self.dismiss((arguments, live))
                else:
                    if not board:
                        raise TraceError("Enter the existing Miro board URL or ID.")
                    _latest(self.case, read_case(self.case), verify=True)
                    arguments = ["miro-sync", "--case", str(self.case), "--run", "latest", "--board", board,
                                 "--max-new-items", str(settings["max_new_items"])]
                    if self.mode == "preview":
                        arguments.append("--dry-run")
                    elif self.mode == "layout":
                        arguments.append("--reorganize")
                    # The CLI saves a live board selection only after its local preflight.
                    self.dismiss((arguments, self.mode in ("sync", "layout")))
            except ACTION_ERRORS as error:
                self.query_one("#form-error", Static).update(str(error))

        def lookup_outputs(self):
            error_field = self.query_one("#form-error", Static)
            error_field.update("")
            try:
                from .inspection import parse_transaction_hashes
                txids = parse_transaction_hashes(self.query_one("#lookup-txid", Input).value)
            except ACTION_ERRORS as error:
                error_field.update(str(error))
                return
            live = self.query_one("#source", Select).value == "live"
            try:
                # Keep provider prompts on the real terminal. Only the transaction
                # report passes through this temporary file, never credentials.
                self.app.busy = True
                with tempfile.TemporaryDirectory(prefix="liquid-output-lookup-") as directory:
                    report_path = Path(directory) / "outputs.json"
                    arguments = (["inspect-tx", "--txid", txids[0]] if len(txids) == 1
                                 else ["inspect-txs", "--txids", ",".join(txids)])
                    arguments.extend(["--output", str(report_path)])
                    if not live:
                        arguments.extend(["--fixture", str(_project() / "examples" / "demo-api.json")])
                    with self.app.suspend() if live else contextlib.nullcontext():
                        options = {} if live else {"capture_output": True, "text": True}
                        result = subprocess.run(_command(arguments, live=live), cwd=_project(),
                                                env=_environment(), check=False, **options)
                    if result.returncode:
                        raise TraceError("Output lookup failed. Check the terminal for credential or API errors, then retry."
                                         if live else "Transaction not available in the synthetic demo, or lookup failed.")
                    reports = _lookup_reports(read_json(report_path), txids)
                self.app.push_screen(OutputScreen(reports), self.use_outputs)
            except KeyboardInterrupt:
                error_field.update("Output lookup interrupted. No investigation was created.")
            except ACTION_ERRORS as error:
                error_field.update(str(error))
            finally:
                self.app.busy = False

        def use_outputs(self, selection):
            if selection is not None:
                # The picker replaces the field deliberately; it must not merge
                # an earlier mistaken entry or silently include other outputs.
                self.query_one("#seeds", TextArea).text = "\n".join(selection)
                self.query_one("#form-error", Static).update("")

    class OutputScreen(BaseScreen):
        def __init__(self, reports):
            super().__init__()
            self.reports = reports
            self.selected = set()
            self.outputs = {output["outpoint"]: output for report in reports for output in report["outputs"]}
            self.transactions = {output["outpoint"]: (number, report["txid"])
                                 for number, report in enumerate(reports, start=1) for output in report["outputs"]}

        def compose(self) -> ComposeResult:
            yield Header()
            with Vertical(classes="panel"):
                yield Label("Choose starting outputs", classes="title")
                yield Static(f"{len(self.reports)} transaction(s). Rows are grouped in the order entered.", markup=False)
                yield Static("Enter toggles the highlighted output. Nothing is selected initially. "
                             "Using a selection replaces the starting-output field.", markup=False)
                yield DataTable(id="outputs", cursor_type="row")
                yield Static("", id="output-detail", markup=False)
                yield Static("", id="output-error", markup=False)
                with Horizontal(classes="buttons"):
                    yield Button("Cancel", id="cancel-outputs")
                    yield Button("Use selected outputs", id="use-outputs", variant="primary")
            yield Footer()

        def on_mount(self):
            table = self.query_one("#outputs", DataTable)
            self.choice_column = table.add_columns("Selected", "Transaction", "Output", "Address", "Amount", "Asset", "Type")[0]
            for outpoint, output in self.outputs.items():
                asset = output.get("asset")
                number, txid = self.transactions[outpoint]
                table.add_row("No" if output["selectable"] else "Unavailable", f"{number}: {txid[:8]}…{txid[-6:]}",
                              str(output["vout"]),
                              Text(output.get("address") or "No address"),
                              str(output["value"]) + " base units" if output.get("value") is not None else "??",
                              Text("L-BTC" if asset == LBTC else asset or "??"),
                              Text(output.get("reason") or output.get("script_type") or "??"), key=outpoint)
            table.focus()

        def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted):
            key = event.row_key.value
            if key in self.outputs:
                number, txid = self.transactions[key]
                self.query_one("#output-detail", Static).update(
                    f"Transaction {number}: {txid}\nOutput {self.outputs[key]['vout']}")

        def on_data_table_row_selected(self, event: DataTable.RowSelected):
            key = event.row_key.value
            error_field = self.query_one("#output-error", Static)
            if not self.outputs[key]["selectable"]:
                error_field.update("This is a fee, peg-out, or unspendable output; it cannot start a forward Liquid trace.")
                return
            error_field.update("")
            if key in self.selected:
                self.selected.remove(key)
            else:
                self.selected.add(key)
            self.query_one("#outputs", DataTable).update_cell(key, self.choice_column, "Yes" if key in self.selected else "No")

        def on_button_pressed(self, event: Button.Pressed):
            event.stop()
            if event.button.id == "cancel-outputs":
                self.dismiss(None)
            elif event.button.id == "use-outputs":
                if not self.selected:
                    self.query_one("#output-error", Static).update("Select at least one output, or cancel.")
                    return
                self.dismiss(sorted(self.selected))

    class CreateBoardScreen(BaseScreen):
        def __init__(self, case):
            super().__init__()
            self.case = case
            self.metadata = read_case(case)

        def compose(self) -> ComposeResult:
            from .boards import default_board_name
            yield Header()
            with VerticalScroll(classes="form-panel"):
                yield Label("Create Miro board", classes="title")
                yield Label("Board name (1 to 60 characters)")
                yield Input(default_board_name(self.metadata), id="board-name")
                yield Label("Visibility")
                yield Select([("Private", "private"), ("Team members can edit", "team")],
                             value="private", allow_blank=False, id="board-visibility")
                yield Label("Miro team ID (optional)")
                yield Input(placeholder="Leave blank unless selecting a specific team", id="board-team")
                yield Static("Creates an empty board and saves it with this investigation. "
                             "Choose Sync to Miro afterward to add the traced graph.", markup=False)
                yield Static("Uses your Miro access token through SecretSpec. "
                             "Available visibility settings depend on your Miro plan and team permissions.", markup=False)
                yield Static("", id="form-error", markup=False)
            with Horizontal(classes="buttons form-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Create Miro board", id="submit", variant="primary")
            yield Footer()

        def on_mount(self):
            self.query_one("#cancel", Button).focus()

        def on_button_pressed(self, event: Button.Pressed):
            event.stop()
            if event.button.id == "cancel":
                self.dismiss(None)
            elif event.button.id == "submit":
                try:
                    from .boards import board_options
                    if read_case(self.case).get("miro_board"):
                        raise TraceError("A Miro board is already saved. Use Preview Miro or Sync to Miro.")
                    name = self.query_one("#board-name", Input).value.strip()
                    team = self.query_one("#board-team", Input).value.strip() or None
                    visibility = self.query_one("#board-visibility", Select).value
                    board_options(name, team, visibility)
                    arguments = ["miro-create-board", "--case", str(self.case), "--name", name,
                                 "--visibility", visibility]
                    if team:
                        arguments.extend(["--team-id", team])
                    self.dismiss((arguments, True))
                except ACTION_ERRORS as error:
                    self.query_one("#form-error", Static).update(str(error))

    class CaseScreen(BaseScreen):
        def __init__(self, case):
            super().__init__()
            self.case = case

        def compose(self) -> ComposeResult:
            yield Header()
            with VerticalScroll(classes="panel"):
                yield Static("", id="case-summary", markup=False)
                with Horizontal(classes="buttons"):
                    yield Button("Run / continue", id="run", variant="primary")
                    yield Button("Review saved runs", id="review")
                with Horizontal(classes="buttons"):
                    yield Button("Preview Miro", id="preview")
                    yield Button("Sync to Miro", id="sync")
                with Horizontal(classes="buttons"):
                    yield Button("Create Miro board", id="create-board")
                    yield Button("Organize Miro graph", id="layout")
                with Horizontal(classes="buttons"):
                    yield Button("Investigation settings", id="case-settings")
                    yield Button("Back", id="back")
                yield Static("Ready", id="action-status", markup=False)
                yield RichLog(id="action-log", wrap=True, markup=False, highlight=False)
            yield Footer()

        def update_summary(self):
            try:
                metadata = read_case(self.case)
                source = "Synthetic demo" if metadata.get("fixture") else "Live Liquid"
                board = metadata.get("miro_board")
                settings = validate_settings(metadata.get("run_defaults", {}))
                self.query_one("#case-summary", Static).update(
                    f"{metadata.get('name') or self.case.name}\n{source}\n"
                    f"Latest run: {_status(self.case, metadata)}\n"
                    f"Miro board: {'https://miro.com/app/board/' + board + '/' if board else 'not set'}\n"
                    f"Transaction fee flows: {'included' if settings['include_fees'] else 'hidden'}\nDirectory: {self.case}")
                self.query_one("#run", Button).label = "Continue latest run" if metadata.get("latest_run") else "Start first run"
                self.query_one("#create-board", Button).disabled = self.app.busy or bool(board)
                self.query_one("#layout", Button).disabled = self.app.busy or not (board and metadata.get("latest_run"))
            except ACTION_ERRORS as error:
                self.show_error(error)

        def on_mount(self):
            self.update_summary()

        def on_screen_resume(self):
            self.update_summary()

        def on_button_pressed(self, event: Button.Pressed):
            if self.app.busy:
                return
            try:
                action = event.button.id
                if action == "back":
                    self.action_back()
                elif action == "case-settings":
                    self.app.push_screen(FormScreen("case", self.case))
                elif action == "create-board":
                    self.app.push_screen(CreateBoardScreen(self.case), self.perform)
                elif action in ("run", "preview", "sync", "layout"):
                    self.app.push_screen(FormScreen(action, self.case), self.perform)
                elif action == "review":
                    self.app.push_screen(ReviewScreen(self.case))
            except ACTION_ERRORS as error:
                self.show_error(error)

        def set_busy(self, busy):
            self.app.busy = busy
            for button in self.query(Button):
                button.disabled = busy
            self.query_one("#action-status", Static).update("Running..." if busy else "Ready")

        def perform(self, selection):
            if not selection or self.app.busy:
                return
            arguments, live = selection
            self.current_action = arguments[0]
            self.reorganizing = "--reorganize" in arguments
            self.set_busy(True)
            if not live:
                self.offline_action(arguments)
                return
            try:
                # Give SecretSpec and Proton Pass the real terminal for unlock/login prompts.
                with self.app.suspend():
                    result = subprocess.run(_command(arguments, live=True), cwd=_project(),
                                            env=_environment(), check=False)
                self.finished(result.returncode, "Live action returned exit code " + str(result.returncode))
            except FileNotFoundError:
                self.finished(1, "SecretSpec is unavailable. Reopen the project's devenv shell and try again.")
            except KeyboardInterrupt:
                self.finished(130, "Live action interrupted. Any saved evidence remains available.")
            except OSError as error:
                self.finished(1, "Could not start the live action: " + str(error))

        @work(thread=True)
        def offline_action(self, arguments):
            try:
                result = subprocess.run(_command(arguments), cwd=_project(), env=_environment(),
                                        check=False, capture_output=True, text=True)
                status, output = result.returncode, result.stdout + result.stderr
            except OSError as error:
                status, output = 1, "Could not start the offline action: " + str(error)
            self.app.call_from_thread(self.finished, status, output)

        def finished(self, status, output):
            self.set_busy(False)
            self.query_one("#action-log", RichLog).write(output)
            if getattr(self, "current_action", None) == "miro-create-board":
                message = ("Miro board saved. Choose Preview Miro, then Sync to Miro to add the traced graph."
                           if status == 0 else "Board creation did not complete. Check the terminal result before retrying.")
            elif getattr(self, "reorganizing", False):
                message = ("Miro graph organized. You can adjust item positions directly in Miro."
                           if status == 0 else "Graph organization did not complete. Check the terminal result before retrying.")
            else:
                message = ("Completed. Saved evidence is available under Review saved runs." if status == 0
                           else "Action did not complete successfully. Saved evidence remains available; no automatic retry.")
            self.query_one("#action-status", Static).update(message)
            self.update_summary()

    class SelectScreen(BaseScreen):
        def compose(self) -> ComposeResult:
            yield Header()
            with Vertical(classes="panel"):
                yield Label("Continue an investigation", classes="title")
                yield DataTable(id="investigations", cursor_type="row")
                yield Button("Back", id="back")
            yield Footer()

        def on_mount(self):
            table = self.query_one(DataTable)
            table.add_columns("Investigation", "Latest run", "Directory")
            self.entries = {}
            for case, metadata in list_investigations(investigation_root):
                key = str(case)
                self.entries[key] = metadata
                table.add_row(Text(str(metadata.get("name") or case.name)), Text(_status(case, metadata)), Text(str(case)), key=key)
            if not self.entries:
                self.app.notify("No investigations saved here yet. Choose New investigation to begin.")
            table.focus()

        def on_data_table_row_selected(self, event: DataTable.RowSelected):
            key = event.row_key.value
            metadata = self.entries[key]
            if metadata.get("error"):
                self.show_error(metadata["error"])
                return
            self.app.push_screen(CaseScreen(Path(key)))

        def on_button_pressed(self, event: Button.Pressed):
            if event.button.id == "back":
                self.action_back()

    class ReviewScreen(BaseScreen):
        def __init__(self, case):
            super().__init__()
            self.case = case

        def compose(self) -> ComposeResult:
            yield Header()
            with Vertical(classes="panel"):
                yield Label("Saved runs", classes="title")
                yield RichLog(id="runs", wrap=True, markup=False, highlight=False)
                yield Button("Back", id="back")
            yield Footer()

        def on_mount(self):
            log = self.query_one("#runs", RichLog)
            paths = sorted((self.case / "runs").glob("*/trace.json"))
            if not paths:
                log.write("No saved runs yet.")
            for path in paths:
                try:
                    state = read_json(path)
                    log.write(f"{path.parent.name}: {state.get('status', 'unknown')}\n"
                              f"Started: {state.get('started_at', 'unknown')}\n"
                              f"Parent: {state.get('parent_run') or 'none'}\n"
                              + "Limits: " + ", ".join(f"{key}={value}" for key, value in state.get("limits", {}).items())
                              + "\nFiles: " + str(path.parent) + "\n")
                    if (path.parent / "investigation.json").is_file():
                        selection = read_json(path.parent / "investigation.json")
                        log.write("Board selected when tracing: " + (selection.get("miro_board") or "not set"))
                    log.write("Miro sync reports: " + str(self.case / "miro" / "reports") + "\n")
                except ACTION_ERRORS:
                    log.write("Unreadable saved run: " + str(path.parent))

        def on_button_pressed(self, event: Button.Pressed):
            if event.button.id == "back":
                self.action_back()

    class InvestigationApp(App):
        TITLE = "Liquid Network Tracer"
        SUB_TITLE = "Saved investigations"
        BINDINGS = [("ctrl+q", "quit", "Quit")]
        CSS = """
        Screen { background: $background; }
        .panel, .form-panel { padding: 1 2; width: 100%; height: 1fr; }
        #home { width: 64; max-width: 100%; height: auto; margin: 2 2; padding: 1 2; border: round $primary; }
        #home Button { width: 100%; margin-top: 1; }
        .title { text-style: bold; color: $accent; margin-bottom: 1; }
        .buttons { height: auto; margin-top: 1; }
        .buttons Button { width: 1fr; margin-right: 1; }
        .form-actions { padding: 0 2; margin-bottom: 1; }
        .form-panel Label { margin-top: 1; height: auto; }
        .form-panel Static { height: auto; margin-top: 1; }
        TextArea { height: 6; }
        #form-error { color: $error; }
        #case-summary { height: auto; margin-bottom: 1; }
        #action-status { height: auto; margin: 1 0; }
        #action-log { height: 1fr; min-height: 10; border: round $panel; }
        DataTable, #runs { height: 1fr; }
        """

        def __init__(self):
            super().__init__()
            self.busy = False
            self.investigation_root = investigation_root

        def compose(self) -> ComposeResult:
            yield Header()
            with Vertical(id="home"):
                yield Label("Investigations", classes="title")
                yield Static("Start a bounded trace or continue saved work.", markup=False)
                yield Button("New investigation", id="new", variant="primary")
                yield Button("Continue an investigation", id="continue")
                yield Button("Settings", id="settings")
                yield Button("Exit", id="exit")
            yield Footer()

        def on_button_pressed(self, event: Button.Pressed):
            if self.screen is not self.screen_stack[0]:
                return
            try:
                if event.button.id == "new":
                    self.push_screen(FormScreen("new"), self.created)
                elif event.button.id == "continue":
                    self.push_screen(SelectScreen())
                elif event.button.id == "settings":
                    self.push_screen(FormScreen("global"))
                elif event.button.id == "exit":
                    self.action_quit()
            except ACTION_ERRORS as error:
                self.notify(str(error), severity="error", timeout=8)

        def created(self, case):
            if case:
                self.push_screen(CaseScreen(case))

        def action_quit(self):
            if self.busy:
                self.notify("A bounded action is running. Wait for its result before exiting.")
            else:
                self.exit(0)

    return InvestigationApp()


def run_menu(root=None):
    if not sys.stdin.isatty():
        print("The investigation interface requires an interactive terminal. Use explicit subcommands for scripts.", file=sys.stderr)
        return 2
    try:
        app = create_app(root)
    except ModuleNotFoundError as error:
        if error.name and (error.name == "textual" or error.name.startswith("textual.")):
            print("The TUI requires Textual. Enter devenv shell, or install this package with its [tui] extra.", file=sys.stderr)
            return 2
        raise
    try:
        return app.run() or 0
    except (EOFError, KeyboardInterrupt):
        return 0
