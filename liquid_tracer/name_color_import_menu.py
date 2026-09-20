"""Reviewed bulk name-color import, available without a run or credentials."""

import json

from .common import TraceError
from .name_color_import import NOTICE, TEMPLATE, apply_import, preview_import, read_import


def name_color_import_screen(base, button, case):
    from rich.text import Text
    from textual.containers import Horizontal, VerticalScroll
    from textual.widgets import Checkbox, DataTable, Footer, Header, Input, Label, Select, Static, TextArea

    class NameColorImportScreen(base):
        def __init__(self):
            super().__init__()
            self.review = None

        def compose(self):
            yield Header()
            with VerticalScroll(classes="form-panel"):
                yield Label("Import name colors", classes="title")
                yield Static(NOTICE, markup=False)
                yield Static("CSV columns can be in any order; unrelated columns such as Duplicate count are ignored. "
                             "Supported fields still require valid values, and JSON rejects unsupported fields.", markup=False)
                yield button("Export saved CSV", id="name-color-import-export")
                yield Static("", id="name-color-import-export-status", markup=False)
                yield Label("CSV / JSON file path (optional; takes precedence over pasted text)")
                yield Input(id="name-color-import-file")
                yield Label("Or paste Name,Color CSV / JSON")
                yield TextArea(id="name-color-import-text")
                yield Select([(value.upper(), value) for value in ("auto", "csv", "json")],
                             value="auto", allow_blank=False, id="name-color-import-format")
                yield Checkbox("Replace existing colors; blank colors clear assignments",
                               id="name-color-import-replace")
                with Horizontal(classes="buttons"):
                    yield button("Use CSV template", id="name-color-import-template")
                    yield button("Preview import", id="name-color-import-preview")
                yield Static("Preview before saving. Import attribution names first. Existing colors are kept by default.",
                             id="name-color-import-summary", markup=False)
                table = DataTable(id="name-color-import-rows", cursor_type="row")
                table.styles.height = 10
                yield table
                yield Static("", id="name-color-import-detail", markup=False)
                yield Checkbox("I reviewed these name-color assignments and clear requests",
                               id="name-color-import-approved")
                yield Static("", id="name-color-import-error", markup=False)
            with Horizontal(classes="buttons form-actions"):
                yield button("Back", id="name-color-import-back")
                yield button("Apply reviewed import", id="name-color-import-apply", variant="primary", disabled=True)
            yield Footer()

        def on_mount(self):
            self.query_one("#name-color-import-rows", DataTable).add_columns(
                "Row", "Name", "Addresses", "Action", "Current color", "Requested color")
            self.query_one("#name-color-import-file", Input).focus()

        def invalidate(self):
            self.review = None
            self.query_one("#name-color-import-approved", Checkbox).value = False
            self.query_one("#name-color-import-apply", button).disabled = True
            self.query_one("#name-color-import-rows", DataTable).clear()
            self.query_one("#name-color-import-detail", Static).update("")
            self.query_one("#name-color-import-summary", Static).update("Input changed. Preview again before saving.")
            self.query_one("#name-color-import-error", Static).update("")

        def on_input_changed(self, event: Input.Changed):
            if event.input.id == "name-color-import-file" and self.is_mounted:
                self.invalidate()

        def on_text_area_changed(self, event: TextArea.Changed):
            if event.text_area.id == "name-color-import-text" and self.is_mounted:
                self.invalidate()

        def on_select_changed(self, event: Select.Changed):
            if event.select.id == "name-color-import-format" and self.is_mounted:
                self.invalidate()

        def on_checkbox_changed(self, event: Checkbox.Changed):
            if event.checkbox.id == "name-color-import-replace" and self.is_mounted:
                self.invalidate()
            elif event.checkbox.id == "name-color-import-approved":
                self.query_one("#name-color-import-apply", button).disabled = not (
                    event.value and self.review and self.review["valid"])

        def inputs(self):
            path = self.query_one("#name-color-import-file", Input).value.strip()
            return (read_import(path) if path else self.query_one("#name-color-import-text", TextArea).text,
                    self.query_one("#name-color-import-format", Select).value,
                    "replace" if self.query_one("#name-color-import-replace", Checkbox).value else "keep")

        def on_data_table_row_selected(self, event: DataTable.RowSelected):
            if self.review and event.data_table.id == "name-color-import-rows":
                entry = self.review["changes"][int(event.row_key.value)]
                self.query_one("#name-color-import-detail", Static).update(
                    json.dumps(entry, indent=2, ensure_ascii=False))

        def on_button_pressed(self, event):
            event.stop()
            if self.app.busy:
                return
            if event.button.id == "name-color-import-export":
                from .input_export_menu import export_saved_inputs
                self.query_one("#name-color-import-export-status", Static).update(export_saved_inputs(case, "name-colors"))
                return
            error = self.query_one("#name-color-import-error", Static)
            try:
                action = event.button.id
                if action == "name-color-import-back":
                    self.action_back()
                elif action == "name-color-import-template":
                    self.query_one("#name-color-import-file", Input).value = ""
                    self.query_one("#name-color-import-text", TextArea).text = TEMPLATE
                    self.query_one("#name-color-import-format", Select).value = "csv"
                    self.call_after_refresh(error.update, "Replace the example names with names already in this investigation.")
                elif action == "name-color-import-preview":
                    text, format, policy = self.inputs()
                    self.invalidate()
                    self.review = preview_import(case, text, format=format, policy=policy)
                    table = self.query_one("#name-color-import-rows", DataTable)
                    for index, entry in enumerate(self.review["changes"]):
                        table.add_row(str(entry["row"]), Text(entry["name"]), str(entry["addresses"]),
                                      entry["action"], Text(entry["previous"] or "Default", style=entry["previous"] or ""),
                                      Text(entry["color"] or "Clear / default", style=entry["color"] or ""), key=str(index))
                    counts = self.review["counts"]
                    self.query_one("#name-color-import-summary", Static).update(
                        f"{self.review['unique_names']} unique names; {counts['add']} new, "
                        f"{counts['replace']} replacements, {counts['clear']} clears, "
                        f"{counts['keep']} conflicts kept, {counts['unchanged']} unchanged, "
                        f"{self.review['duplicate_rows']} identical duplicates.\n" + self.review["notice"])
                    error.update("\n".join(f"Row {item['row']}: {item['message']}" for item in self.review["errors"]))
                elif action == "name-color-import-apply":
                    if not self.review or not self.review["valid"] or not self.query_one("#name-color-import-approved", Checkbox).value:
                        raise TraceError("Preview and approve the import before saving")
                    # Re-read local files so edits since preview cannot silently pass approval.
                    text, format, policy = self.inputs()
                    result = apply_import(case, text, approval_sha256=self.review["approval_sha256"], format=format, policy=policy)
                    self.invalidate()
                    self.query_one("#name-color-import-summary", Static).update(
                        f"Saved {result['changed']} name-color assignment(s). Regenerate previews or sync Miro to update the graph.")
                    error.update("")
            except (TraceError, OSError, ValueError, TypeError) as exc:
                self.invalidate()
                error.update(str(exc))

    return NameColorImportScreen()
