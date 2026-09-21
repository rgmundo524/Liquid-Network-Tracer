"""One reviewed terminal workflow for the investigation's CSV input files."""

import json
from pathlib import Path

from .common import TraceError


KINDS = [("Detect from CSV headers", "auto"), ("Address attributions", "attributions"),
         ("Name colors", "name-colors"), ("Change outputs", "change-outputs")]
KIND_LABELS = dict((kind, label) for label, kind in KINDS)


def _counts_text(counts):
    return ", ".join(f"{counts.get(key, 0)} {label}" for key, label in (
        ("add", "new"), ("replace", "replacements"), ("clear", "clears"),
        ("keep", "conflicts kept"), ("unchanged", "unchanged")))


def _row_text(kind, entry):
    if kind == "attributions":
        rule = entry["rule"]
        stop = "yes" if rule["enabled"] and rule["stop_tracing"] else "no"
        limit = rule.get("hop_limit")
        return rule["address"], (f"{rule['name']} | {rule['confidence']} | stop: {stop} | "
                                 f"hop limit: {'none' if limit is None else limit}")
    if kind == "name-colors":
        return entry["name"], entry.get("color") or "Clear color"
    return entry["txid"], "Clear designation" if entry["vout"] is None else f"Vout {entry['vout']}"


def input_import_screen(base, button, case):
    from rich.text import Text
    from textual.containers import Horizontal, VerticalScroll
    from textual.widgets import Checkbox, Collapsible, DataTable, Footer, Header, Label, Select, Static, TextArea

    from .input_import import apply_import, preview_import, read_import

    class InputImportScreen(base):
        def __init__(self):
            super().__init__()
            self.review = None
            self.rows = []

        def compose(self):
            yield Header()
            with VerticalScroll(classes="form-panel"):
                yield Label("Import CSV files", classes="title")
                yield Static("Add address attributions, name colors and change outputs together. "
                             "Choose up to three files, one of each type, in any order. "
                             "CSV headers identify each file. New attribution names are available to the colors file "
                             "in the same import. All files are reviewed and saved together.", markup=False)
                yield Label("CSV file paths, one per line")
                paths = TextArea(id="input-import-paths")
                paths.styles.height = 5
                yield paths
                with Collapsible(title="CSV type overrides (optional)", collapsed=True, id="input-import-types"):
                    for index in range(3):
                        yield Label(f"File {index + 1}")
                        yield Select(KINDS, value="auto", allow_blank=False, id=f"input-import-kind-{index}")
                yield Checkbox("Replace conflicting saved entries in all selected files", id="input-import-replace")
                yield Static("Existing entries are kept by default. Select Replace when updating saved attribution "
                             "details or hop limits. With Replace selected, blank colors or change vouts clear saved "
                             "assignments. Rows missing from the files are retained.", markup=False)
                with Horizontal(classes="buttons"):
                    yield button("Preview files", id="input-import-preview", variant="primary")
                    yield button("Export saved CSVs", id="input-import-export")
                yield Static("", id="input-import-export-status", markup=False)
                yield Static("Preview each file, then approve the complete import.", id="input-import-summary", markup=False)
                files = DataTable(id="input-import-files", cursor_type="row")
                files.styles.height = 6
                yield files
                rows = DataTable(id="input-import-rows", cursor_type="row")
                rows.styles.height = 10
                yield rows
                yield Static("Select a file or row with Enter to inspect its details and previous values.", markup=False)
                yield Static("", id="input-import-detail", markup=False)
                yield Checkbox("I reviewed all files, tracing settings and clear requests", id="input-import-approved")
                yield Static("", id="input-import-error", markup=False)
                yield Label("Advanced: paste text or JSON using a single-type importer")
                with Horizontal(classes="buttons"):
                    yield button("Address attributions", id="input-import-advanced-attributions")
                    yield button("Name colors", id="input-import-advanced-name-colors")
                    yield button("Change outputs", id="input-import-advanced-change-outputs")
            with Horizontal(classes="buttons form-actions"):
                yield button("Back", id="input-import-back")
                yield button("Apply reviewed files", id="input-import-apply", variant="primary", disabled=True)
            yield Footer()

        def on_mount(self):
            self.query_one("#input-import-files", DataTable).add_columns("File", "Type", "Policy", "Review")
            self.query_one("#input-import-rows", DataTable).add_columns("File", "Row", "Entry", "Action", "Requested value")
            self.query_one("#input-import-paths", TextArea).focus()

        def invalidate(self):
            self.review = None
            self.rows = []
            self.query_one("#input-import-approved", Checkbox).value = False
            self.query_one("#input-import-apply", button).disabled = True
            self.query_one("#input-import-files", DataTable).clear()
            self.query_one("#input-import-rows", DataTable).clear()
            self.query_one("#input-import-detail", Static).update("")
            self.query_one("#input-import-summary", Static).update("Input changed. Preview all files again before saving.")
            self.query_one("#input-import-error", Static).update("")

        def on_text_area_changed(self, event: TextArea.Changed):
            if event.text_area.id == "input-import-paths" and self.is_mounted:
                self.invalidate()

        def on_select_changed(self, event: Select.Changed):
            if (event.select.id or "").startswith("input-import-kind-") and self.is_mounted:
                self.invalidate()

        def on_checkbox_changed(self, event: Checkbox.Changed):
            if event.checkbox.id == "input-import-replace" and self.is_mounted:
                self.invalidate()
            elif event.checkbox.id == "input-import-approved":
                self.query_one("#input-import-apply", button).disabled = not (
                    event.value and self.review and self.review["valid"])

        def inputs(self):
            paths = [line.strip() for line in self.query_one("#input-import-paths", TextArea).text.splitlines()
                     if line.strip()]
            if not 1 <= len(paths) <= 3:
                raise TraceError("Choose one to three CSV files, with one file path per line")
            policy = "replace" if self.query_one("#input-import-replace", Checkbox).value else "keep"
            files = []
            for index, value in enumerate(paths):
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                path = Path(value).expanduser()
                files.append({"name": path.name, "text": read_import(path), "policy": policy,
                              "kind": self.query_one(f"#input-import-kind-{index}", Select).value})
            return files

        def show_review(self):
            table = self.query_one("#input-import-files", DataTable)
            rows = self.query_one("#input-import-rows", DataTable)
            for index, file in enumerate(self.review["files"]):
                table.add_row(Text(file["name"]), KIND_LABELS.get(file["kind"], file["kind"]), file["policy"],
                              _counts_text(file.get("counts", {})), key=str(index))
                for entry in file.get("changes", []):
                    target, requested = _row_text(file["kind"], entry)
                    rows.add_row(Text(file["name"]), str(entry["row"]), Text(target), entry["action"],
                                 Text(requested), key=str(len(self.rows)))
                    self.rows.append({"file": file["name"], "kind": file["kind"], **entry})
            self.query_one("#input-import-summary", Static).update(
                f"{len(self.review['files'])} file(s): {_counts_text(self.review['counts'])}.\n" + self.review["notice"])
            messages = []
            for error in self.review["errors"]:
                location = str(error.get("file", "Import"))
                if error.get("row") is not None:
                    location += f", row {error['row']}"
                messages.append(f"{location}: {error['message']}")
            self.query_one("#input-import-error", Static).update("\n".join(messages))

        def on_data_table_row_selected(self, event: DataTable.RowSelected):
            if self.review and event.data_table.id == "input-import-files":
                file = self.review["files"][int(event.row_key.value)]
                self.query_one("#input-import-detail", Static).update(
                    file["name"] + "\n" + _counts_text(file["counts"]) + "\n" + file.get("notice", ""))
            elif self.review and event.data_table.id == "input-import-rows":
                entry = self.rows[int(event.row_key.value)]
                self.query_one("#input-import-detail", Static).update(json.dumps(entry, indent=2, ensure_ascii=False))

        def on_button_pressed(self, event):
            event.stop()
            if self.app.busy:
                return
            error = self.query_one("#input-import-error", Static)
            try:
                action = event.button.id
                if action == "input-import-back":
                    self.action_back()
                elif action == "input-import-export":
                    from .input_export_menu import export_saved_inputs
                    self.query_one("#input-import-export-status", Static).update(export_saved_inputs(case, "all"))
                elif action == "input-import-advanced-attributions":
                    from .address_import_menu import import_screen
                    self.app.switch_screen(import_screen(base, button, case))
                elif action == "input-import-advanced-name-colors":
                    from .name_color_import_menu import name_color_import_screen
                    self.app.switch_screen(name_color_import_screen(base, button, case))
                elif action == "input-import-advanced-change-outputs":
                    from .change_output_import_menu import change_output_import_screen
                    self.app.switch_screen(change_output_import_screen(base, button, case))
                elif action == "input-import-preview":
                    files = self.inputs()
                    self.invalidate()
                    self.review = preview_import(case, files)
                    self.show_review()
                elif action == "input-import-apply":
                    if not self.review or not self.review["valid"] or not self.query_one("#input-import-approved", Checkbox).value:
                        raise TraceError("Preview and approve all files before saving")
                    result = apply_import(case, self.inputs(), approval_sha256=self.review["approval_sha256"])
                    self.invalidate()
                    self.query_one("#input-import-summary", Static).update(
                        f"Saved {result['changed']} change(s) from {len(result['files'])} file(s). "
                        "Attributions apply to the next run. Regenerate previews or sync Miro for new colors and layout hints. "
                        "No trace was started.")
            except (TraceError, OSError, ValueError, TypeError, KeyError) as exc:
                self.invalidate()
                error.update(str(exc))

    return InputImportScreen()
