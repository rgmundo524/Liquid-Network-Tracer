"""Local terminal color editor shared by import, review, and the case menu."""

from .common import TraceError
from .name_colors import COLOR_PRESETS, NOTICE, name_color_catalog, set_name_colors
from .role_colors import ROLE_LABELS


def name_color_screen(base, button, case):
    from rich.text import Text
    from textual.containers import Horizontal, VerticalScroll
    from textual.widgets import DataTable, Footer, Header, Input, Label, Select, Static

    class NameColorScreen(base):
        def __init__(self):
            super().__init__()
            self.report = None
            self.selected = None
            self.page_offset = 0

        def compose(self):
            yield Header()
            with VerticalScroll(classes="form-panel"):
                yield Label("Assign colors", classes="title")
                yield Static(NOTICE, markup=False)
                yield Input(placeholder="Search attribution names", id="name-color-search")
                with Horizontal(classes="buttons"):
                    yield button("Search / refresh", id="name-color-find")
                    yield button("Previous", id="name-color-prev")
                    yield button("Next", id="name-color-next")
                yield Static("", id="name-color-count", markup=False)
                table = DataTable(id="name-color-rows", cursor_type="row")
                table.styles.height = 10
                yield table
                yield Label("Or choose a graph role (no imported name required)")
                yield Select([("Choose graph role", "")] + [(label, role) for role, label in ROLE_LABELS.items()],
                             value="", allow_blank=False, id="name-color-role")
                yield Static("Select a name with Enter. No colors are assigned automatically.", id="name-color-selected", markup=False)
                yield Label("Palette (fills the color field)")
                yield Select([("Custom / default", "")] + list(COLOR_PRESETS),
                             value="", allow_blank=False, id="name-color-palette")
                yield Label("Color #RRGGBB; leave blank for normal trace colors")
                yield Input(id="name-color-value", max_length=7)
                yield Static("", id="name-color-error", markup=False)
            with Horizontal(classes="buttons form-actions"):
                yield button("Back", id="name-color-back")
                yield button("Clear / reset to default", id="name-color-clear", disabled=True)
                yield button("Save color", id="name-color-save", variant="primary", disabled=True)
            yield Footer()

        def on_mount(self):
            self.query_one("#name-color-rows", DataTable).add_columns("Name", "Addresses", "Active", "Assigned color")
            try:
                self.load()
            except (TraceError, OSError, ValueError, TypeError) as exc:
                self.query_one("#name-color-error", Static).update(str(exc))
            self.query_one("#name-color-search", Input).focus()

        def load(self):
            self.selected = None
            self.query_one("#name-color-role", Select).value = ""
            self.query_one("#name-color-value", Input).value = ""
            self.query_one("#name-color-palette", Select).value = ""
            self.query_one("#name-color-save", button).disabled = True
            self.query_one("#name-color-clear", button).disabled = True
            self.report = name_color_catalog(case, query=self.query_one("#name-color-search", Input).value,
                                            offset=self.page_offset, limit=100)
            table = self.query_one("#name-color-rows", DataTable)
            table.clear()
            for index, row in enumerate(self.report["rows"]):
                swatch = Text(row["color"] or "Default", style=row["color"] or "")
                table.add_row(Text(row["name"]), str(row["addresses"]), str(row["enabled_addresses"]), swatch, key=str(index))
            self.query_one("#name-color-count", Static).update(
                f"{self.report['total']} distinct names. Showing {len(self.report['rows'])} on this page. "
                "Capitalization variants share one color; new addresses using that name inherit it.")
            self.query_one("#name-color-prev", button).disabled = self.page_offset == 0
            self.query_one("#name-color-next", button).disabled = self.page_offset + 100 >= self.report["total"]
            self.query_one("#name-color-selected", Static).update("Select a name with Enter.")

        def on_data_table_row_selected(self, event: DataTable.RowSelected):
            if event.data_table.id != "name-color-rows" or not self.report:
                return
            self.query_one("#name-color-role", Select).value = ""
            self.selected = self.report["rows"][int(event.row_key.value)]
            self.query_one("#name-color-selected", Static).update(
                "Name: " + self.selected["name"] + "\nSpellings: " + ", ".join(self.selected["variants"]))
            self.query_one("#name-color-palette", Select).value = ""
            self.query_one("#name-color-value", Input).value = self.selected["color"] or ""
            self.query_one("#name-color-save", button).disabled = False
            self.query_one("#name-color-clear", button).disabled = False

        def on_select_changed(self, event: Select.Changed):
            if event.select.id == "name-color-role" and event.value and self.report:
                self.selected = next(row for row in self.report["roles"] if row["role"] == event.value)
                self.query_one("#name-color-selected", Static).update(
                    "Role: " + self.selected["name"] + "\nDefault: " + self.selected["default_color"]
                    + ". Seed priority and existing borders are unchanged.")
                self.query_one("#name-color-palette", Select).value = ""
                self.query_one("#name-color-value", Input).value = self.selected["color"] or self.selected["default_color"]
                self.query_one("#name-color-save", button).disabled = False
                self.query_one("#name-color-clear", button).disabled = False
            elif event.select.id == "name-color-palette" and event.value:
                self.query_one("#name-color-value", Input).value = event.value

        def on_input_submitted(self, event: Input.Submitted):
            if event.input.id == "name-color-search" and not self.app.busy:
                self.page_offset = 0
                try:
                    self.load()
                except (TraceError, OSError, ValueError, TypeError) as exc:
                    self.query_one("#name-color-error", Static).update(str(exc))

        def on_button_pressed(self, event):
            event.stop()
            if self.app.busy:
                return
            error = self.query_one("#name-color-error", Static)
            try:
                action = event.button.id
                if action == "name-color-back":
                    self.action_back()
                elif action in ("name-color-find", "name-color-prev", "name-color-next"):
                    self.page_offset = (max(0, self.page_offset - 100) if action == "name-color-prev" else
                                   self.page_offset + 100 if action == "name-color-next" else 0)
                    self.load()
                    error.update("")
                elif action in ("name-color-save", "name-color-clear"):
                    if not self.selected or not self.report:
                        raise TraceError("Select a name or graph role first")
                    color = None if action == "name-color-clear" else self.query_one("#name-color-value", Input).value
                    target = {"role": self.selected["role"]} if "role" in self.selected else {"name": self.selected["name"]}
                    result = set_name_colors(case, [{**target, "color": color}],
                                             expected_revision=self.report["revision"])
                    self.load()
                    error.update(f"Saved {result['changed']} color assignment(s). Regenerate previews or sync Miro; no tracing is needed.")
            except (TraceError, OSError, ValueError, TypeError) as exc:
                error.update(str(exc))

    return NameColorScreen()
