"""Address review stays local until an explicit, bounded refresh is requested."""

import contextlib
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.investigations import create_investigation
from liquid_tracer.menu import _address_activity_text, create_app
from liquid_tracer.services import load_services, set_service

PROJECT = Path(__file__).resolve().parents[1]
HAS_TEXTUAL = importlib.util.find_spec("textual") is not None
ADDRESS = "SYNTHETIC-SERVICE-ADDRESS"
OTHER = "SYNTHETIC-OTHER-ADDRESS"
SUMMARY = {
    "observed_at": "2026-01-10T12:00:00Z",
    "confirmed_tx_count": 12345,
    "mempool_tx_count": 2,
    "confirmed_unspent_output_count": 90,
    "mempool_unspent_output_delta": -1,
    "unspent_output_count": 89,
    "history_pages": 5,
    "history_transactions_seen": 125,
    "history_complete": False,
    "history_stop_reason": "page_limit",
    "first_confirmed_activity": None,
    "oldest_observed_confirmed_activity": {"date_utc": "2025-12-01T00:00:00Z"},
    "latest_confirmed_activity": {"date_utc": "2026-01-10T11:00:00Z"},
    "warnings": [],
}


class AddressActivityTextTests(unittest.TestCase):
    def test_partial_history_does_not_claim_first_use_or_traced_dormancy(self):
        # Even an unexpected first timestamp cannot override incomplete history.
        text = _address_activity_text({**SUMMARY, "first_confirmed_activity": {"date_utc": "2020-01-01T00:00:00Z"}})
        self.assertIn("12,345 confirmed; 2 in mempool", text)
        self.assertIn("General unspent outputs: 89", text)
        self.assertIn("including outputs outside this investigation", text)
        self.assertIn("First use has not been established", text)
        self.assertNotIn("First confirmed activity", text)
        self.assertIn("History: partial", text)
        self.assertIn("2026-01-10T12:00:00Z", text)

    def test_complete_history_has_confirmed_date_without_address_creation_claim(self):
        text = _address_activity_text({**SUMMARY, "history_complete": True,
                                     "first_confirmed_activity": {"date_utc": "2020-01-01T00:00:00Z"}})
        self.assertIn("First confirmed activity (UTC): 2020-01-01T00:00:00Z", text)
        self.assertIn("not when an address was created", text)
        self.assertIn("No saved activity snapshot", _address_activity_text(None))


@unittest.skipUnless(HAS_TEXTUAL, "Install the optional [tui] extra for Textual interaction tests")
class AddressMenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "cases"
        self.case = create_investigation(self.root, "Address review fixture", seeds=["a" * 64 + ":0"])
        self.environment = patch.dict(os.environ, {"LIQUID_TRACER_ROOT": str(PROJECT),
            "LIQUID_SECRET_PROVIDER": "protonpass", "LIQUID_SECRET_PROFILE": "development",
            "LIQUID_SECRETSPEC_BIN": "/nix/store/test-secretspec/bin/secretspec"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def report(self, case, **kwargs):
        rules = load_services(case)["rules"]
        rows = [{"address": ADDRESS, "run_output_count": 4, "service": rules.get(ADDRESS), "activity": SUMMARY}]
        if kwargs.get("query") and kwargs["query"] not in ADDRESS:
            rows = []
        if kwargs.get("suspected_only"):
            rows = [row for row in rows if (row["service"] or {}).get("enabled")]
        return {"run_id": "synthetic-run", "rows": rows, "total": len(rows), "offset": 0, "limit": 25}

    async def click(self, app, pilot, selector):
        from textual.widgets import Button
        button = app.screen.query_one(selector, Button)
        button.scroll_visible(immediate=True)
        await pilot.pause()
        if button.has_class("-active"):
            await pilot.pause(button.active_effect_duration)
        await pilot.click(selector)
        await pilot.pause()

    async def open_review(self, app, pilot):
        app.created(self.case)
        await pilot.pause()
        await self.click(app, pilot, "#addresses-review")
        return app.screen

    async def select_row(self, screen, pilot):
        from textual.widgets import DataTable
        screen.query_one("#addresses", DataTable).focus()
        await pilot.press("enter")
        await pilot.pause()

    async def test_open_select_search_and_cached_review_make_no_requests(self):
        from textual.widgets import DataTable, Input, Static
        app = create_app(self.root)
        with patch("liquid_tracer.address_review.list_addresses", side_effect=self.report), \
                patch("liquid_tracer.address_review.saved_activity", return_value=SUMMARY), \
                patch("liquid_tracer.menu.subprocess.run") as process:
            async with app.run_test(size=(115, 55)) as pilot:
                screen = await self.open_review(app, pilot)
                self.assertEqual(screen.query_one("#addresses", DataTable).row_count, 1)
                await self.select_row(screen, pilot)
                self.assertEqual(screen.query_one("#address-value", Input).value, ADDRESS)
                self.assertIn("History: partial", str(screen.query_one("#address-activity", Static).render()))
                screen.query_one("#address-search", Input).value = "absent"
                await self.click(app, pilot, "#address-find")
                self.assertEqual(screen.query_one("#addresses", DataTable).row_count, 0)
                screen.query_one("#address-value", Input).value = OTHER
                await self.click(app, pilot, "#address-select")
                self.assertEqual(screen.selected_address, OTHER)
        process.assert_not_called()

    async def test_service_rule_survives_reopen_and_can_be_disabled(self):
        from textual.widgets import Checkbox, Input, Static, TextArea
        app = create_app(self.root)
        with patch("liquid_tracer.address_review.list_addresses", side_effect=self.report), \
                patch("liquid_tracer.address_review.saved_activity", return_value=SUMMARY), \
                patch("liquid_tracer.menu.subprocess.run") as process:
            async with app.run_test(size=(115, 55)) as pilot:
                screen = await self.open_review(app, pilot)
                await self.select_row(screen, pilot)
                screen.query_one("#service-enabled", Checkbox).value = True
                screen.query_one("#service-name", Input).value = "Suspected exchange"
                screen.query_one("#service-rationale", TextArea).text = "Repeated deposit consolidation.\nInvestigator assessment."
                await self.click(app, pilot, "#service-save")
                self.assertTrue(load_services(self.case)["rules"][ADDRESS]["enabled"])
                self.assertIn("next run", str(screen.query_one("#address-error", Static).render()))
                await self.click(app, pilot, "#address-back")
                await self.click(app, pilot, "#addresses-review")
                screen = app.screen
                await self.select_row(screen, pilot)
                self.assertTrue(screen.query_one("#service-enabled", Checkbox).value)
                self.assertEqual(screen.query_one("#service-name", Input).value, "Suspected exchange")
                self.assertEqual(screen.query_one("#service-rationale", TextArea).text,
                                 "Repeated deposit consolidation.\nInvestigator assessment.")
                screen.query_one("#service-enabled", Checkbox).value = False
                await self.click(app, pilot, "#service-save")
                stored = load_services(self.case)
                self.assertFalse(stored["rules"][ADDRESS]["enabled"])
                self.assertEqual(stored["revision"], 2)
                self.assertIn("Repeated deposit consolidation", stored["rules"][ADDRESS]["rationale"])
        process.assert_not_called()

    async def test_live_refresh_is_bounded_and_uses_provider_terminal(self):
        from textual.widgets import Input, Static
        app = create_app(self.root)
        suspend_calls = []

        @contextlib.contextmanager
        def suspend():
            suspend_calls.append("terminal")
            yield

        with patch("liquid_tracer.address_review.list_addresses", side_effect=self.report), \
                patch("liquid_tracer.address_review.saved_activity", return_value=SUMMARY), \
                patch.object(app, "suspend", side_effect=suspend), \
                patch("liquid_tracer.menu.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as process:
            async with app.run_test(size=(115, 55)) as pilot:
                screen = await self.open_review(app, pilot)
                await self.select_row(screen, pilot)
                screen.query_one("#address-pages", Input).value = "0"
                await self.click(app, pilot, "#address-refresh")
                process.assert_not_called()
                self.assertIn("at least 1", str(screen.query_one("#address-error", Static).render()))
                screen.query_one("#address-pages", Input).value = "5"
                await self.click(app, pilot, "#address-refresh")
                self.assertFalse(app.busy)
                self.assertIn("snapshot saved", str(screen.query_one("#address-error", Static).render()))
        command = process.call_args.args[0]
        self.assertEqual(command[0], "/nix/store/test-secretspec/bin/secretspec")
        self.assertEqual(command[command.index("--provider") + 1], "protonpass")
        self.assertEqual(command[command.index("--profile") + 1], "development")
        self.assertEqual(command[command.index("--address") + 1], ADDRESS)
        for flag, expected in (("--case", str(self.case)), ("--max-pages", "5"),
                               ("--max-requests", "10"), ("--max-seconds", "60")):
            self.assertEqual(command[command.index(flag) + 1], expected)
        self.assertEqual(suspend_calls, ["terminal"])
        self.assertNotIn("capture_output", process.call_args.kwargs)
        self.assertEqual(load_services(self.case)["rules"], {})

    async def test_changed_address_cannot_inherit_previous_rule_accidentally(self):
        from textual.widgets import Input, Static
        set_service(self.case, ADDRESS, name="Selected service", enabled=True)
        app = create_app(self.root)
        with patch("liquid_tracer.address_review.list_addresses", side_effect=self.report), \
                patch("liquid_tracer.address_review.saved_activity", return_value=SUMMARY), \
                patch("liquid_tracer.menu.subprocess.run") as process:
            async with app.run_test(size=(115, 55)) as pilot:
                screen = await self.open_review(app, pilot)
                await self.select_row(screen, pilot)
                screen.query_one("#address-value", Input).value = OTHER
                await self.click(app, pilot, "#service-save")
                self.assertNotIn(OTHER, load_services(self.case)["rules"])
                self.assertIn("changed address", str(screen.query_one("#address-error", Static).render()))
                await self.click(app, pilot, "#address-refresh")
        process.assert_not_called()

    async def test_pre_run_review_lists_saved_rules_and_refuses_changes_during_trace(self):
        import fcntl
        from textual.widgets import Checkbox, DataTable, Static
        set_service(self.case, ADDRESS, enabled=True)
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run") as process:
            async with app.run_test(size=(115, 55)) as pilot:
                screen = await self.open_review(app, pilot)
                self.assertEqual(screen.query_one("#addresses", DataTable).row_count, 1)
                await self.select_row(screen, pilot)
                self.assertIn("No saved activity snapshot", str(screen.query_one("#address-activity", Static).render()))
                screen.query_one("#service-enabled", Checkbox).value = False
                with (self.case / "trace.lock").open("a") as active_trace:
                    fcntl.flock(active_trace, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    await self.click(app, pilot, "#service-save")
                    self.assertIn("trace is running", str(screen.query_one("#address-error", Static).render()))
                    self.assertEqual(load_services(self.case)["revision"], 1)
                await self.click(app, pilot, "#service-save")
                self.assertFalse(load_services(self.case)["rules"][ADDRESS]["enabled"])
        process.assert_not_called()
