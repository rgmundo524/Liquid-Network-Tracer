"""Case-scoped Bitcoin lookups and optional terminal output selection."""
import contextlib
import copy
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer import change_outputs, menu
from liquid_tracer.boards import default_board_name
from liquid_tracer.common import TraceError, digest, read_json, save_json
from liquid_tracer.inspection import transaction_outputs
from liquid_tracer.investigations import DEFAULTS, create_investigation, read_case
from liquid_tracer.networks import default_api
from tests.fixtures import A, B, X
from tests.test_bitcoin_core import bitcoin_fixture
from tests.test_change_outputs import save_archive


class BitcoinLookupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = create_investigation(self.root, "Bitcoin change", blockchain="bitcoin", seeds=[A + ":0"])

    def saved(self, chain="bitcoin"):
        transactions = {key[4:]: value for key, value in bitcoin_fixture().items() if key.count("/") == 2}
        archive = save_archive(self.case, transactions, source=default_api("bitcoin"))
        save_json(archive / "trace.json", {**read_json(archive / "trace.json"), "blockchain": chain,
                                           "seeds": read_case(self.case)["seeds"]})
        (archive / "SHA256SUMS").write_text("".join(
            digest((archive / name).read_bytes()) + "  " + name + "\n"
            for name in ("trace.json", "graph.json", "miro-plan.json")))
        return archive

    def test_new_bitcoin_lookup_uses_bitcoin_api_and_explicit_chain(self):
        expected = {"fixture": None, "base_url": default_api("bitcoin"), "auth": "blockstream", "blockchain": "bitcoin"}
        self.assertEqual(change_outputs._lookup_options(self.case, None), expected)
        with patch("liquid_tracer.inspection.inspect_transaction", return_value={"txid": X, "outputs": [], "blockchain": "bitcoin"}) as inspect:
            self.assertEqual(change_outputs.transaction_lookup(self.case, X)["blockchain"], "bitcoin")
        inspect.assert_called_once_with(X, **expected, max_requests=5, max_seconds=30)

    def test_saved_bitcoin_lookup_and_change_selection_do_not_use_liquid_fee_rules(self):
        archive = self.saved()
        before = (archive / "trace.json").read_bytes()
        with patch("liquid_tracer.inspection.inspect_transaction", side_effect=AssertionError("offline only")):
            report = change_outputs.transaction_lookup(self.case, B)
            self.assertEqual(report["blockchain"], "bitcoin")
            self.assertTrue(report["outputs"][3]["selectable"])  # Bitcoin empty script is not a Liquid fee.
            self.assertFalse(report["outputs"][2]["selectable"])
            self.assertFalse(change_outputs.lookup_requires_network(self.case, B))
            change_outputs.set_change_output(self.case, B, 3)
        self.assertEqual((archive / "trace.json").read_bytes(), before)

    def test_wrong_chain_saved_snapshot_is_rejected_before_lookup_or_edit(self):
        self.saved("liquid")
        with patch("liquid_tracer.inspection.inspect_transaction", side_effect=AssertionError("No network")):
            for action in (lambda: change_outputs.transaction_lookup(self.case, B),
                           lambda: change_outputs.lookup_requires_network(self.case, B),
                           lambda: change_outputs.set_change_output(self.case, B, 0),
                           lambda: menu._trace_arguments(self.case, read_case(self.case), DEFAULTS)):
                with self.subTest(action=action), self.assertRaisesRegex(TraceError, "match"):
                    action()
        self.assertFalse((self.case / "services.json").exists())
        with self.assertRaisesRegex(TraceError, "blockchain"):
            change_outputs._lookup_options(self.case, {"blockchain": "liquid", "source": default_api("liquid")})

    def test_bitcoin_menu_resume_preserves_source_and_chain(self):
        self.saved()
        arguments, live = menu._trace_arguments(self.case, read_case(self.case), DEFAULTS)
        self.assertTrue(live)
        self.assertEqual(arguments[arguments.index("--blockchain") + 1], "bitcoin")
        self.assertEqual(arguments[arguments.index("--resume") + 1], "latest")

    def test_lookup_reports_reject_mixed_or_wrong_chain_but_accept_legacy_liquid(self):
        report = transaction_outputs(B, bitcoin_fixture()["/tx/" + B], blockchain="bitcoin")
        self.assertEqual(menu._lookup_reports(report, [B], expected_blockchain="bitcoin"), [report])
        with self.assertRaisesRegex(TraceError, "different blockchain"):
            menu._lookup_reports(report, [B], expected_blockchain="liquid")
        legacy = {k: v for k, v in report.items() if k != "blockchain"}
        self.assertEqual(menu._lookup_reports(legacy, [B], expected_blockchain="liquid"), [legacy])
        another = {**copy.deepcopy(report), "txid": A, "outputs": []}
        with self.assertRaisesRegex(TraceError, "invalid transaction"):
            menu._lookup_reports({"blockchain": "bitcoin", "transactions": [report, {**another, "blockchain": "liquid"}]}, [B, A])

    def test_menu_amounts_are_exact_and_board_name_matches_chain(self):
        self.assertEqual(menu._output_cells({"value": 1}, "bitcoin"), ("0.00000001 BTC", "BTC"))
        self.assertEqual(menu._output_cells({"value": 2 ** 53 + 1}, "bitcoin"), ("90071992.54740993 BTC", "BTC"))
        self.assertEqual(menu._output_cells({"value": None}, "bitcoin"), ("??", "BTC"))
        self.assertEqual(menu._output_cells({"value": 1}), ("1 base units", "??"))
        self.assertEqual(default_board_name({"blockchain": "bitcoin"}), "Bitcoin investigation")
        self.assertEqual(default_board_name({}), "Liquid investigation")


@unittest.skipUnless(importlib.util.find_spec("textual"), "optional terminal UI")
class BitcoinMenuTests(unittest.IsolatedAsyncioTestCase):
    from tests.test_menu import TextualWorkflowTests as _Common
    asyncSetUp = _Common.asyncSetUp
    click = _Common.click
    toggle_output = _Common.toggle_output

    async def test_bitcoin_change_picker_displays_btc_and_rejects_wrong_chain_report(self):
        from textual.widgets import Button, DataTable, Input, Select, Static
        case = create_investigation(self.root, "Bitcoin change picker", blockchain="bitcoin", seeds=[A + ":0"])
        app = menu.create_app(self.root)
        report = {**transaction_outputs(B, bitcoin_fixture()["/tx/" + B], blockchain="bitcoin"),
                  "revision": 0, "current_vout": None, "current_notes": ""}

        def lookup(command, **kwargs):
            save_json(Path(command[command.index("--output") + 1]), report)
            return subprocess.CompletedProcess(command, 0)

        with patch("liquid_tracer.menu.subprocess.run", side_effect=lookup), \
                patch("liquid_tracer.change_outputs.lookup_requires_network", return_value=False):
            async with app.run_test(size=(110, 65)) as pilot:
                app.created(case)
                await pilot.pause()
                await self.click(app, pilot, "#change-outputs")
                screen = app.screen
                screen.query_one("#change-output-txid", Input).value = B
                await self.click(app, pilot, "#change-output-lookup")
                cells = screen.query_one("#change-output-rows", DataTable).get_row("0")
                self.assertEqual(cells[2], "0.5 BTC")
                self.assertEqual(str(cells[3]), "BTC")
                screen.query_one("#change-output-vout", Select).value = 3
                self.assertFalse(screen.query_one("#change-output-save", Button).disabled)
                report["blockchain"] = "liquid"
                await self.click(app, pilot, "#change-output-lookup")
                self.assertIsNone(screen.lookup)
                self.assertTrue(screen.query_one("#change-output-save", Button).disabled)
                self.assertIn("different blockchain", str(screen.query_one("#change-output-error", Static).render()))

    async def test_select_bitcoin_lookup_exact_values_create_case_and_clear_on_switch(self):
        from textual.widgets import DataTable, Input, Select, Static, TextArea
        app = menu.create_app(self.root)
        report = transaction_outputs(B, bitcoin_fixture()["/tx/" + B], blockchain="bitcoin")

        def lookup(command, **kwargs):
            self.assertEqual(command[command.index("--blockchain") + 1], "bitcoin")
            save_json(Path(command[command.index("--output") + 1]), report)
            return subprocess.CompletedProcess(command, 0)

        with patch("liquid_tracer.menu.subprocess.run", side_effect=lookup) as process, \
                patch.object(app, "suspend", side_effect=contextlib.nullcontext):
            async with app.run_test(size=(110, 65)) as pilot:
                await self.click(app, pilot, "#new")
                form = app.screen
                form.query_one("#case-name", Input).value = "Bitcoin menu case"
                form.query_one("#seeds", TextArea).text = A + ":0"
                form.query_one("#blockchain", Select).value = "bitcoin"
                await pilot.pause()
                self.assertEqual(form.query_one("#seeds", TextArea).text, "")
                self.assertIn("Live Bitcoin", str(form.query_one("#lookup-network", Static).render()))
                form.query_one("#lookup-txid", Input).value = B
                await self.click(app, pilot, "#lookup")
                cells = app.screen.query_one("#outputs", DataTable).get_row(B + ":0")
                self.assertEqual(cells[4], "0.5 BTC")
                self.assertEqual(str(cells[5]), "BTC")
                await self.toggle_output(app, pilot, 0)
                await self.click(app, pilot, "#use-outputs")
                self.assertEqual(form.query_one("#seeds", TextArea).text, B + ":0")
                await self.click(app, pilot, "#submit")
                metadata = read_case(app.screen.case)
                self.assertEqual(metadata["blockchain"], "bitcoin")
                self.assertEqual(metadata["seeds"], [B + ":0"])
                self.assertNotIn("latest_run", metadata)
                process.assert_called_once()
