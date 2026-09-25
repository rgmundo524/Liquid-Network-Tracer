"""Bulk attribution screen, available before a trace and without credentials."""

import json

from .address_import import TEMPLATE, apply_import, preview_import, read_import
from .common import TraceError


def import_screen(base, button, case):
    from textual.app import ComposeResult
    from textual.containers import Horizontal, VerticalScroll
    from textual.widgets import Checkbox, DataTable, Footer, Header, Input, Label, Select, Static, TextArea

    class ImportScreen(base):
        def __init__(self):
            super().__init__()
            self.review = None

        def compose(self) -> ComposeResult:
            yield Header()
            with VerticalScroll(classes="form-panel"):
                yield Label("Import address attributions", classes="title")
                yield Static("Import before the first run or between runs. No Blockstream calls or Miro changes. "
                             "A plain list uses suspected confidence with tracing stops enabled. "
                             "CSV/JSON can specify names, sources, notes, confidence and independent stop flags. "
                             "CSV columns can be in any order; unrelated columns such as Duplicate count are ignored. "
                             "Supported fields still require valid values, and JSON rejects unsupported fields.", markup=False)
                yield button("Export saved CSV", id="import-export")
                yield Static("", id="import-export-status", markup=False)
                yield Label("CSV / JSON / text file path (optional; takes precedence over pasted text)")
                yield Input(id="import-file")
                yield Label("Or paste addresses / CSV / JSON")
                yield TextArea(id="import-text")
                yield Select([(value.upper(), value) for value in ("auto", "csv", "json", "text")],
                             value="auto", allow_blank=False, id="import-format")
                yield Checkbox("Replace conflicting existing assessments (otherwise keep them)", id="import-replace")
                with Horizontal(classes="buttons"):
                    yield button("Use CSV template", id="import-template")
                    yield button("Preview import", id="import-preview")
                yield Static("Preview before saving. Use public address strings as shown in the trace.", id="import-summary", markup=False)
                yield DataTable(id="import-rows", cursor_type="row")
                yield Static("", id="import-detail", markup=False)
                yield button("Assign name colors", id="import-name-colors")
                yield Static("", id="import-error", markup=False)
            with Horizontal(classes="buttons form-actions"):
                yield button("Back", id="import-back")
                yield button("Apply reviewed import", id="import-apply", variant="primary", disabled=True)
            yield Footer()

        def on_mount(self):
            self.query_one("#import-rows", DataTable).add_columns("Row", "Address", "Action", "Name", "Confidence", "Stop", "hop_limit")
            self.query_one("#import-file", Input).focus()

        def invalidate(self):
            self.review = None
            self.query_one("#import-apply", button).disabled = True
            self.query_one("#import-rows", DataTable).clear()
            self.query_one("#import-detail", Static).update("")
            self.query_one("#import-summary", Static).update("Input changed. Preview again before saving.")

        def on_input_changed(self, event: Input.Changed):
            if event.input.id == "import-file" and self.is_mounted:
                self.invalidate()

        def on_text_area_changed(self, event: TextArea.Changed):
            if self.is_mounted:
                self.invalidate()

        def on_select_changed(self, event: Select.Changed):
            if self.is_mounted:
                self.invalidate()

        def on_checkbox_changed(self, event: Checkbox.Changed):
            if event.checkbox.id == "import-replace" and self.is_mounted:
                self.invalidate()

        def inputs(self):
            path = self.query_one("#import-file", Input).value.strip()
            return (read_import(path) if path else self.query_one("#import-text", TextArea).text,
                    self.query_one("#import-format", Select).value,
                    "replace" if self.query_one("#import-replace", Checkbox).value else "keep")

        def on_data_table_row_selected(self, event: DataTable.RowSelected):
            if self.review and event.data_table.id == "import-rows":
                entry = self.review["changes"][int(event.row_key.value)]
                self.query_one("#import-detail", Static).update(json.dumps(entry, indent=2, ensure_ascii=False))

        def on_button_pressed(self, event):
            event.stop()
            if self.app.busy:
                return
            if event.button.id == "import-export":
                from .input_export_menu import export_saved_inputs
                self.query_one("#import-export-status", Static).update(export_saved_inputs(case, "attributions"))
                return
            error = self.query_one("#import-error", Static)
            try:
                if event.button.id == "import-back":
                    self.action_back()
                elif event.button.id == "import-name-colors":
                    from .name_colors_menu import name_color_screen
                    self.app.push_screen(name_color_screen(base, button, case))
                elif event.button.id == "import-template":
                    self.query_one("#import-file", Input).value = ""
                    self.query_one("#import-text", TextArea).text = TEMPLATE
                    self.query_one("#import-format", Select).value = "csv"
                    error.update("Replace the placeholder addresses with public Liquid addresses before previewing.")
                elif event.button.id == "import-preview":
                    text, format, policy = self.inputs()
                    self.invalidate()
                    self.review = preview_import(case, text, format=format, policy=policy)
                    table = self.query_one("#import-rows", DataTable)
                    from rich.text import Text
                    for index, entry in enumerate(self.review["changes"]):
                        row = entry["rule"]
                        table.add_row(str(entry["row"]), Text(row["address"]), entry["action"], Text(row["name"]),
                                      row["confidence"],
                                      "Yes" if row["enabled"] and row["stop_tracing"] else "No",
                                      str(row["hop_limit"]) if row.get("hop_limit") is not None else "", key=str(index))
                    counts = self.review["counts"]
                    self.query_one("#import-summary", Static).update(
                        f"{self.review['unique_addresses']} unique addresses; {counts['add']} new, "
                        f"{counts['replace']} replacements, {counts['keep']} conflicts kept, "
                        f"{counts['unchanged']} unchanged, {self.review['duplicate_rows']} identical duplicates.\n"
                        + self.review["notice"])
                    error.update("\n".join(f"Row {item['row']}: {item['message']}" for item in self.review["errors"]))
                    self.query_one("#import-apply", button).disabled = not self.review["valid"]
                elif event.button.id == "import-apply":
                    if not self.review or not self.review["valid"]:
                        raise TraceError("Preview the import before saving")
                    text, format, policy = self.inputs()
                    result = apply_import(case, text, approval_sha256=self.review["approval_sha256"], format=format, policy=policy)
                    self.invalidate()
                    self.query_one("#import-summary", Static).update(
                        f"Saved {result['changed']} address assessments. The first/next run will use them. "
                        "Choose Assign name colors to color these names, or Address review to edit entries. No trace was started.")
                    error.update("")
            except (TraceError, OSError, ValueError, TypeError) as exc:
                self.invalidate()
                error.update(str(exc))

    return ImportScreen()
