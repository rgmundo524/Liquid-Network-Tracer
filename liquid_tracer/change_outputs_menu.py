"""Optional, investigator-selected change outputs for graph layout."""

import contextlib
import tempfile
from pathlib import Path

from .common import LBTC, TraceError, read_json


def change_output_screen(base, button, case):
    from rich.text import Text
    from textual.containers import Horizontal, VerticalScroll
    from textual.widgets import DataTable, Footer, Header, Input, Label, Select, Static, TextArea

    from .change_outputs import NOTICE, catalog, lookup_requires_network, set_change_output
    from .inspection import parse_transaction_hashes
    from . import menu

    class ChangeOutputScreen(base):
        def __init__(self):
            super().__init__()
            self.report = None
            self.lookup = None
            self.lookup_input = None
            self.page_offset = 0

        def compose(self):
            yield Header()
            with VerticalScroll(classes="form-panel"):
                yield Label("Change outputs", classes="title")
                yield Static(NOTICE, markup=False)
                yield button("Import change outputs", id="change-output-import")
                yield button("Export saved CSV", id="change-output-export")
                yield Static("", id="change-output-export-status", markup=False)
                yield Input(placeholder="Search saved transaction IDs", id="change-output-search")
                with Horizontal(classes="buttons"):
                    yield button("Search / refresh", id="change-output-find")
                    yield button("Previous", id="change-output-prev")
                    yield button("Next", id="change-output-next")
                yield Static("", id="change-output-count", markup=False)
                saved = DataTable(id="change-output-saved", cursor_type="row")
                saved.styles.height = 7
                yield saved
                yield Label("Transaction hash (without :vout)")
                yield Input(id="change-output-txid", max_length=64)
                yield button("Look up transaction outputs", id="change-output-lookup")
                yield Static("Lookup uses saved transaction data first. Missing data uses a bounded explorer request.", markup=False)
                outputs = DataTable(id="change-output-rows", cursor_type="row")
                outputs.styles.height = 9
                yield outputs
                yield Label("Change output: choose one spendable vout, or no designation")
                yield Select([("No change designation (original ELK rules)", "")], value="", allow_blank=False,
                             id="change-output-vout", disabled=True)
                yield Label("Notes (optional)")
                yield TextArea(id="change-output-notes", disabled=True)
                yield Static("", id="change-output-error", markup=False)
            with Horizontal(classes="buttons form-actions"):
                yield button("Back", id="change-output-back")
                yield button("Clear designation", id="change-output-clear", disabled=True)
                yield button("Save change output", id="change-output-save", variant="primary", disabled=True)
            yield Footer()

        def on_mount(self):
            self.query_one("#change-output-saved", DataTable).add_columns("Transaction", "Change vout", "Notes")
            self.query_one("#change-output-rows", DataTable).add_columns("Vout", "Address", "Value", "Asset", "Use")
            self.refresh_catalog()
            self.query_one("#change-output-txid", Input).focus()

        def invalidate_lookup(self):
            self.lookup = None
            self.lookup_input = None
            self.query_one("#change-output-rows", DataTable).clear()
            selector = self.query_one("#change-output-vout", Select)
            selector.set_options([("No change designation (original ELK rules)", "")])
            selector.value = ""
            selector.disabled = True
            notes = self.query_one("#change-output-notes", TextArea)
            notes.text = ""
            notes.disabled = True
            self.query_one("#change-output-save", button).disabled = True
            self.query_one("#change-output-clear", button).disabled = True

        def refresh_catalog(self):
            try:
                self.report = catalog(case, query=self.query_one("#change-output-search", Input).value,
                                      offset=self.page_offset, limit=100)
                table = self.query_one("#change-output-saved", DataTable)
                table.clear()
                for index, row in enumerate(self.report["rows"]):
                    table.add_row(Text(row["txid"]), str(row["vout"]), Text(row.get("notes", "")), key=str(index))
                self.query_one("#change-output-count", Static).update(
                    f"{self.report['total']} saved change designation(s). Showing {len(self.report['rows'])}. "
                    "Select a saved transaction with Enter to look up its outputs.")
                self.query_one("#change-output-prev", button).disabled = self.page_offset == 0
                self.query_one("#change-output-next", button).disabled = self.page_offset + 100 >= self.report["total"]
            except menu.ACTION_ERRORS as exc:
                self.query_one("#change-output-error", Static).update(str(exc))

        def on_screen_resume(self):
            if self.report is not None:
                self.invalidate_lookup()
                self.refresh_catalog()

        def on_input_changed(self, event: Input.Changed):
            if event.input.id == "change-output-txid" and self.is_mounted:
                # Input.Changed may arrive after a synchronous lookup. Compare
                # the current field, so a queued event cannot clear its result.
                if self.lookup is not None and event.input.value != self.lookup_input:
                    self.invalidate_lookup()

        def on_input_submitted(self, event: Input.Submitted):
            if self.app.busy:
                return
            if event.input.id == "change-output-txid":
                self.lookup_outputs()
            elif event.input.id == "change-output-search":
                self.page_offset = 0
                self.refresh_catalog()

        def on_data_table_row_selected(self, event: DataTable.RowSelected):
            if self.app.busy:
                return
            if event.data_table.id == "change-output-saved" and self.report:
                row = self.report["rows"][int(event.row_key.value)]
                self.query_one("#change-output-txid", Input).value = row["txid"]
                self.lookup_outputs()
            elif event.data_table.id == "change-output-rows" and self.lookup:
                output = self.lookup["outputs"][int(event.row_key.value)]
                if output["selectable"]:
                    self.query_one("#change-output-vout", Select).value = output["vout"]
                    self.query_one("#change-output-error", Static).update("")
                else:
                    self.query_one("#change-output-error", Static).update(
                        "Only spendable outputs can be designated as change. This output is "
                        + str(output.get("reason") or "not spendable") + ".")

        def lookup_outputs(self):
            error = self.query_one("#change-output-error", Static)
            self.invalidate_lookup()
            error.update("")
            try:
                lookup_input = self.query_one("#change-output-txid", Input).value
                txids = parse_transaction_hashes(lookup_input)
                if len(txids) != 1:
                    raise TraceError("Look up one transaction at a time")
                txid = txids[0]
                live = lookup_requires_network(case, txid)
                self.app.busy = True
                with tempfile.TemporaryDirectory(prefix="liquid-change-lookup-") as directory:
                    report_path = Path(directory) / "outputs.json"
                    arguments = ["change-output-lookup", "--case", str(case), "--txid", txid,
                                 "--output", str(report_path)]
                    with self.app.suspend() if live else contextlib.nullcontext():
                        options = {} if live else {"capture_output": True, "text": True}
                        result = menu.subprocess.run(menu._command(arguments, live=live), cwd=menu._project(),
                                                     env=menu._environment(), check=False, **options)
                    if result.returncode:
                        raise TraceError("Transaction lookup failed. Check the terminal for credential or API errors and retry. "
                                         "The saved change designation is unchanged." if live else
                                         "Transaction lookup failed. The saved change designation is unchanged.")
                    report = read_json(report_path)
                    menu._lookup_reports(report, [txid])
                    if (type(report.get("revision")) is not int or not isinstance(report.get("current_notes"), str)
                            or report.get("current_vout") is not None and
                            (type(report["current_vout"]) is not int or report["current_vout"] < 0)):
                        raise TraceError("Transaction lookup returned invalid change metadata. Look up the transaction again.")
                self.lookup = report
                self.lookup_input = lookup_input
                table = self.query_one("#change-output-rows", DataTable)
                options = [("No change designation (original ELK rules)", "")]
                for index, output in enumerate(report["outputs"]):
                    asset = output.get("asset")
                    table.add_row(str(output["vout"]), Text(output.get("address") or "No address"),
                                  str(output["value"]) + " base units" if output.get("value") is not None else "??",
                                  Text("L-BTC" if asset == LBTC else asset or "??"),
                                  "Spendable" if output["selectable"] else Text(output.get("reason") or "Not spendable"),
                                  key=str(index))
                    if output["selectable"]:
                        options.append((f"Vout {output['vout']} · {output.get('address') or 'No address'}", output["vout"]))
                selector = self.query_one("#change-output-vout", Select)
                selector.set_options(options)
                current = report["current_vout"]
                selector.value = current if current in [value for _, value in options] else ""
                selector.disabled = False
                notes = self.query_one("#change-output-notes", TextArea)
                notes.text = report["current_notes"]
                notes.disabled = False
                self.query_one("#change-output-save", button).disabled = False
                self.query_one("#change-output-clear", button).disabled = current is None
                error.update("The saved designation is not a spendable output in this transaction. "
                             "Choose a replacement or clear it." if current is not None and selector.value == "" else
                             "Select the change output, then save. This does not infer ownership or change tracing.")
            except FileNotFoundError:
                error.update("SecretSpec or lookup output is unavailable. Reopen the project's devenv shell and retry.")
            except KeyboardInterrupt:
                error.update("Transaction lookup interrupted. The saved change designation is unchanged.")
            except menu.ACTION_ERRORS as exc:
                error.update(str(exc))
            finally:
                self.app.busy = False

        def on_button_pressed(self, event):
            event.stop()
            if self.app.busy:
                return
            if event.button.id == "change-output-export":
                from .input_export_menu import export_saved_inputs
                self.query_one("#change-output-export-status", Static).update(export_saved_inputs(case, "change-outputs"))
                return
            error = self.query_one("#change-output-error", Static)
            try:
                action = event.button.id
                if action == "change-output-back":
                    self.action_back()
                elif action == "change-output-import":
                    from .change_output_import_menu import change_output_import_screen
                    self.app.push_screen(change_output_import_screen(base, button, case))
                elif action in ("change-output-find", "change-output-prev", "change-output-next"):
                    self.page_offset = (max(0, self.page_offset - 100) if action == "change-output-prev" else
                                        self.page_offset + 100 if action == "change-output-next" else 0)
                    self.refresh_catalog()
                elif action == "change-output-lookup":
                    self.lookup_outputs()
                elif action in ("change-output-save", "change-output-clear"):
                    if not self.lookup or self.query_one("#change-output-txid", Input).value != self.lookup_input:
                        self.invalidate_lookup()
                        raise TraceError("Look up a transaction before saving")
                    selected = self.query_one("#change-output-vout", Select).value
                    vout = None if action == "change-output-clear" or selected == "" else selected
                    if vout is not None and not any(output["vout"] == vout and output["selectable"]
                                                   for output in self.lookup["outputs"]):
                        raise TraceError("Choose one spendable output or clear the designation")
                    result = set_change_output(case, self.lookup["txid"], vout,
                                               notes=self.query_one("#change-output-notes", TextArea).text,
                                               expected_revision=self.lookup["revision"])
                    self.invalidate_lookup()
                    self.refresh_catalog()
                    error.update(f"Saved {result['changed']} change designation(s). "
                                 "Regenerate a preview, or use Sync and reorganize Miro graph to apply the layout.")
            except menu.ACTION_ERRORS as exc:
                error.update(str(exc))

    return ChangeOutputScreen()
