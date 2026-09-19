"""Review complete change-output imports before applying local layout hints."""

import json

from .common import TraceError


def change_output_import_screen(base, button, case):
    from rich.text import Text
    from textual.containers import Horizontal, VerticalScroll
    from textual.widgets import Checkbox, DataTable, Footer, Header, Input, Label, Select, Static, TextArea

    from .change_output_import import NOTICE, TEMPLATE, apply_import, preview_import, read_import

    class ChangeOutputImportScreen(base):
        def __init__(self):
            super().__init__()
            self.review = None

        def compose(self):
            yield Header()
            with VerticalScroll(classes="form-panel"):
                yield Label("Import change outputs", classes="title")
                yield Static(NOTICE, markup=False)
                yield button("Export saved CSV", id="change-import-export")
                yield Static("", id="change-import-export-status", markup=False)
                yield Label("CSV / JSON file path (optional; takes precedence over pasted text)")
                yield Input(id="change-import-file")
                yield Label("Or paste Txid,ChangeVout,Notes CSV / JSON (Notes is optional)")
                yield TextArea(id="change-import-text")
                yield Select([(value.upper(), value) for value in ("auto", "csv", "json")],
                             value="auto", allow_blank=False, id="change-import-format")
                yield Checkbox("Replace existing designations; blank vouts clear them", id="change-import-replace")
                with Horizontal(classes="buttons"):
                    yield button("Use CSV template", id="change-import-template")
                    yield button("Preview import", id="change-import-preview")
                yield Static("Preview before saving. Existing designations are kept by default.",
                             id="change-import-summary", markup=False)
                table = DataTable(id="change-import-rows", cursor_type="row")
                table.styles.height = 10
                yield table
                yield Static("", id="change-import-detail", markup=False)
                yield Checkbox("I reviewed these change-output designations and clear requests", id="change-import-approved")
                yield Static("", id="change-import-error", markup=False)
            with Horizontal(classes="buttons form-actions"):
                yield button("Back", id="change-import-back")
                yield button("Apply reviewed import", id="change-import-apply", variant="primary", disabled=True)
            yield Footer()

        def on_mount(self):
            self.query_one("#change-import-rows", DataTable).add_columns(
                "Row", "Transaction", "Current vout", "Requested vout", "Action", "Notes")
            self.query_one("#change-import-file", Input).focus()

        def invalidate(self):
            self.review = None
            self.query_one("#change-import-approved", Checkbox).value = False
            self.query_one("#change-import-apply", button).disabled = True
            self.query_one("#change-import-rows", DataTable).clear()
            self.query_one("#change-import-detail", Static).update("")
            self.query_one("#change-import-summary", Static).update("Input changed. Preview again before saving.")
            self.query_one("#change-import-error", Static).update("")

        def on_input_changed(self, event: Input.Changed):
            if event.input.id == "change-import-file" and self.is_mounted:
                self.invalidate()

        def on_text_area_changed(self, event: TextArea.Changed):
            if event.text_area.id == "change-import-text" and self.is_mounted:
                self.invalidate()

        def on_select_changed(self, event: Select.Changed):
            if event.select.id == "change-import-format" and self.is_mounted:
                self.invalidate()

        def on_checkbox_changed(self, event: Checkbox.Changed):
            if event.checkbox.id == "change-import-replace" and self.is_mounted:
                self.invalidate()
            elif event.checkbox.id == "change-import-approved":
                self.query_one("#change-import-apply", button).disabled = not (
                    event.value and self.review and self.review["valid"])

        def inputs(self):
            path = self.query_one("#change-import-file", Input).value.strip()
            return (read_import(path) if path else self.query_one("#change-import-text", TextArea).text,
                    self.query_one("#change-import-format", Select).value,
                    "replace" if self.query_one("#change-import-replace", Checkbox).value else "keep")

        def on_data_table_row_selected(self, event: DataTable.RowSelected):
            if self.review and event.data_table.id == "change-import-rows":
                entry = self.review["changes"][int(event.row_key.value)]
                self.query_one("#change-import-detail", Static).update(json.dumps(entry, indent=2, ensure_ascii=False))

        def on_button_pressed(self, event):
            event.stop()
            if self.app.busy:
                return
            if event.button.id == "change-import-export":
                from .input_export_menu import export_saved_inputs
                self.query_one("#change-import-export-status", Static).update(export_saved_inputs(case, "change-outputs"))
                return
            error = self.query_one("#change-import-error", Static)
            try:
                action = event.button.id
                if action == "change-import-back":
                    self.action_back()
                elif action == "change-import-template":
                    self.query_one("#change-import-file", Input).value = ""
                    self.query_one("#change-import-text", TextArea).text = TEMPLATE
                    self.query_one("#change-import-format", Select).value = "csv"
                    self.call_after_refresh(error.update, "Replace the example transaction and vout with your change designation.")
                elif action == "change-import-preview":
                    text, format, policy = self.inputs()
                    self.invalidate()
                    self.review = preview_import(case, text, format=format, policy=policy)
                    table = self.query_one("#change-import-rows", DataTable)
                    for index, entry in enumerate(self.review["changes"]):
                        previous = entry.get("previous")
                        previous_vout = previous.get("vout") if isinstance(previous, dict) else previous
                        table.add_row(str(entry["row"]), Text(entry["txid"]),
                                      "None" if previous_vout is None else str(previous_vout),
                                      "Clear" if entry["vout"] is None else str(entry["vout"]),
                                      entry["action"], Text(entry.get("notes", "")), key=str(index))
                    counts = self.review["counts"]
                    self.query_one("#change-import-summary", Static).update(
                        f"{counts['add']} new, {counts['replace']} replacements, {counts['clear']} clears, "
                        f"{counts['keep']} conflicts kept, {counts['unchanged']} unchanged, "
                        f"{self.review['duplicate_rows']} identical duplicates.\n" + self.review["notice"])
                    error.update("\n".join(f"Row {item['row']}: {item['message']}" for item in self.review["errors"]))
                elif action == "change-import-apply":
                    if not self.review or not self.review["valid"] or not self.query_one("#change-import-approved", Checkbox).value:
                        raise TraceError("Preview and approve the import before saving")
                    text, format, policy = self.inputs()
                    result = apply_import(case, text, approval_sha256=self.review["approval_sha256"], format=format, policy=policy)
                    self.invalidate()
                    self.query_one("#change-import-summary", Static).update(
                        f"Saved {result['changed']} change designation(s). Regenerate a preview, "
                        "or use Sync and reorganize Miro graph to apply the layout.")
                    error.update("")
            except (TraceError, OSError, ValueError, TypeError, KeyError) as exc:
                self.invalidate()
                error.update(str(exc))

    return ChangeOutputImportScreen()
