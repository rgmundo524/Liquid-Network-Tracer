"""Terminal dialogs for connection-only previews and explicit snapshot publication."""
from pathlib import Path
from .common import TraceError
from .connections import PREVIEW_ID, SCOPE, reviewed_connections
from .investigations import effective_run_settings, read_case


def connection_screen(base, button, case, *, publish=False):
    from textual.containers import Horizontal, VerticalScroll
    from textual.widgets import Checkbox, Footer, Header, Input, Label, Select, Static
    from .cli import board_id, resolve_latest

    class ConnectionScreen(base):
        def compose(self):
            yield Header()
            with VerticalScroll(classes="form-panel"):
                yield Label("Publish starter connections" if publish else "Starter connections", classes="title")
                yield Static(SCOPE, markup=False)
                if publish:
                    settings = effective_run_settings(read_case(case).get("run_defaults", {}))
                    rows = []
                    directory = Path(case) / "previews"
                    for path in sorted(directory.glob("*-connections-*"), reverse=True):
                        if not PREVIEW_ID.fullmatch(path.name): continue
                        try:
                            graph, _ = reviewed_connections(case, path.name)
                            if graph["nodes"]:
                                scope = graph["connections"]
                                label = ("all saved connections" if scope.get("connection_scope") == "all_saved"
                                         else f"{scope['max_hops']} hops")
                                rows.append((path.name + f" ({label})", path.name))
                        except (TraceError, OSError, ValueError, TypeError, KeyError):
                            continue
                    yield Select(rows, id="connection-preview", prompt="Choose a reviewed snapshot")
                    yield Label("Separate Miro board URL or ID")
                    yield Input(id="connection-board")
                    yield Checkbox("I reviewed this snapshot and authorize publication to the board above", id="connection-confirm")
                    yield Static("The full-trace board is protected. One immutable snapshot per board. "
                                 "Repeating the same publication reuses acknowledged items. "
                                 "Older snapshots keep their original stop rules and hop bounds.", markup=False)
                    yield Static(f"New Miro item budget: {settings['max_new_items'] or 'unlimited'}.", markup=False)
                else:
                    yield Static("Uses all verified connections in the latest saved run, with no plotting hop cutoff. "
                                 "Attribution stops and hop limits do not prune this view; labels and recorded confirmation status remain. "
                                 "Each connecting transaction shows all inputs and outputs, including fees and context. "
                                 "Other branch outputs stay visible without following their descendants. Unconnected starters are omitted. "
                                 "Only verified UTXO spends create paths. No blockchain requests or Miro changes are made here.", markup=False)
                yield Static("", id="connection-error", markup=False)
            with Horizontal(classes="buttons form-actions"):
                yield button("Back", id="connection-back")
                yield button("Publish snapshot" if publish else "Plot connections", id="connection-go", variant="primary")
            yield Footer()

        def on_button_pressed(self, event):
            event.stop()
            if self.app.busy: return
            if event.button.id == "connection-back":
                self.action_back(); return
            if event.button.id != "connection-go": return
            try:
                if publish:
                    preview = self.query_one("#connection-preview", Select).value
                    if not isinstance(preview, str): raise TraceError("Choose a reviewed connection snapshot")
                    graph, _ = reviewed_connections(case, preview)
                    if not graph["nodes"]: raise TraceError("This snapshot has no connections to publish")
                    if not self.query_one("#connection-confirm", Checkbox).value:
                        raise TraceError("Review the snapshot and confirm publication first")
                    target = board_id(self.query_one("#connection-board", Input).value)
                    metadata = read_case(case)
                    settings = effective_run_settings(metadata.get("run_defaults", {}))
                    if metadata.get("miro_board") and target == board_id(metadata["miro_board"]):
                        raise TraceError("Choose a separate Miro board; the full trace is protected")
                    arguments = ["connections-publish", "--case", str(case), "--preview", preview,
                                 "--board", target, "--max-items", str(settings["max_new_items"])]
                else:
                    arguments = ["connections", "--case", str(case), "--run", resolve_latest(case, "latest"),
                                 "--open"]
                self.dismiss((arguments, publish))
            except (TraceError, ValueError, OSError, KeyError, TypeError) as exc:
                self.query_one("#connection-error", Static).update(str(exc))

    return ConnectionScreen()
