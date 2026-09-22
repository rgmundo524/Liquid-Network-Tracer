"""Terminal workflow for independent bounded peg-out searches and snapshots."""
import re

from .common import TraceError
from .investigations import read_case


def pegout_screen(base, button, case):
    from textual.containers import Horizontal, VerticalScroll
    from textual.widgets import Checkbox, Footer, Header, Input, Label, Select, Static
    from .cli import board_id
    from .pegouts import list_pegout_searches, reviewed_pegouts

    class PegoutScreen(base):
        def compose(self):
            self.searches = {row["search_id"]: row for row in list_pegout_searches(case)}
            rows = [(f"{row['search_id']} · hops {row['min_hops']}–{row['max_hops']} · {row['status']}", identity)
                    for identity, row in self.searches.items()]
            yield Header()
            with VerticalScroll(classes="form-panel"):
                yield Label("Trace to peg-outs", classes="title")
                yield Static("Search forward from one Liquid transaction and plot only paths reaching peg-out requests. "
                             "The origin is hop 0; both hop bounds are included. Uses the investigation's request, "
                             "time, transaction and output budgets. Saved service stop and hop-limit rules apply.", markup=False)
                yield Label("Origin transaction hash")
                yield Input(id="pegout-txid")
                yield Label("Minimum transaction hops")
                yield Input(value="0", id="pegout-min-hops", type="integer")
                yield Label("Maximum transaction hops")
                yield Input(value="10", id="pegout-max-hops", type="integer")
                yield button("Trace and plot peg-outs", id="pegout-search", variant="primary")
                yield Static("Searches save separately from the full investigation. A bounded or interrupted search "
                             "may miss matches; resume it with the same origin and range. Peg-out requests do not "
                             "establish the Bitcoin payout transaction.", markup=False)
                yield Label("Saved peg-out search")
                yield Select(rows, id="pegout-saved", prompt="Choose a saved search")
                with Horizontal(classes="buttons"):
                    yield button("Resume search", id="pegout-resume", disabled=not rows)
                    yield button("Rebuild saved preview", id="pegout-preview", disabled=not rows)
                yield Label("Separate Miro board URL or ID")
                yield Input(id="pegout-board")
                yield Checkbox("I reviewed the selected snapshot and authorize publication to this separate board", id="pegout-confirm")
                yield Static("One immutable snapshot per board. The full-trace board stays unchanged. "
                             "Repeated publication reuses acknowledged items.", markup=False)
                yield button("Publish reviewed snapshot", id="pegout-publish", disabled=not rows)
                yield Static("", id="pegout-error", markup=False)
            with Horizontal(classes="buttons form-actions"):
                yield button("Back", id="pegout-back")
            yield Footer()

        def _clear_approval(self):
            for checkbox in self.query("#pegout-confirm"):
                checkbox.value = False

        def on_select_changed(self, event):
            if event.select.id == "pegout-saved":
                self._clear_approval()

        def on_input_changed(self, event):
            if event.input.id == "pegout-board":
                self._clear_approval()

        def on_button_pressed(self, event):
            event.stop()
            if self.app.busy:
                return
            action = event.button.id
            if action == "pegout-back":
                self.action_back()
                return
            if action not in ("pegout-search", "pegout-resume", "pegout-preview", "pegout-publish"):
                return
            try:
                metadata = read_case(case)
                live = False
                if action == "pegout-search":
                    txid = self.query_one("#pegout-txid", Input).value.strip().lower()
                    if not re.fullmatch(r"[0-9a-f]{64}", txid):
                        raise TraceError("Enter one transaction hash containing 64 hexadecimal characters")
                    lower = int(self.query_one("#pegout-min-hops", Input).value)
                    upper = int(self.query_one("#pegout-max-hops", Input).value)
                    if not 0 <= lower <= upper <= 2147483647:
                        raise TraceError("Use whole-number hops from 0 to 2147483647; minimum cannot exceed maximum")
                    arguments = ["pegouts", "--case", str(case), "--txid", txid,
                                 "--min-hops", str(lower), "--max-hops", str(upper), "--open"]
                    live = not bool(metadata.get("fixture"))
                else:
                    identity = self.query_one("#pegout-saved", Select).value
                    if not isinstance(identity, str) or identity not in self.searches:
                        raise TraceError("Choose a saved peg-out search")
                    if action == "pegout-resume":
                        arguments = ["pegouts", "--case", str(case), "--resume", identity, "--open"]
                        live = not bool(metadata.get("fixture"))
                    elif action == "pegout-preview":
                        arguments = ["pegouts-preview", "--case", str(case), "--search", identity, "--open"]
                    else:
                        preview = self.searches[identity].get("preview_id")
                        if not preview:
                            raise TraceError("Rebuild and review a preview for this saved search first")
                        graph, _ = reviewed_pegouts(case, preview)
                        if not graph.get("nodes"):
                            raise TraceError("This snapshot has no matching paths to publish")
                        if not self.query_one("#pegout-confirm", Checkbox).value:
                            raise TraceError("Review the snapshot and confirm publication first")
                        target = board_id(self.query_one("#pegout-board", Input).value)
                        if metadata.get("miro_board") and target == board_id(metadata["miro_board"]):
                            raise TraceError("Choose a separate Miro board; the full-trace board is protected")
                        arguments = ["pegouts-publish", "--case", str(case), "--preview", preview,
                                     "--board", target, "--max-items", str(metadata.get("run_defaults", {}).get("max_new_items", 750))]
                        live = True
                self.dismiss((arguments, live))
            except (TraceError, ValueError, OSError, KeyError, TypeError) as error:
                self.query_one("#pegout-error", Static).update(str(error))

    return PegoutScreen()
