"""The UI reviews a fixed conversion before invoking the existing live CLI."""
import contextlib
import importlib.util
import subprocess
import unittest
from unittest.mock import patch

from liquid_tracer.menu import create_app
from tests import test_web
from tests import test_menu_compaction


def report():
    return {"approval_sha256": "a" * 64, "board_id": "SYNTHETIC-BOARD=", "run_id": "b" * 16,
            "address_objects_before": 10, "address_objects_after": 7, "duplicates_to_remove": 3,
            "connectors_to_redirect": 6, "resume": False, "notice": "Preserve duplicate comments first.",
            "remote_preflight_required": True, "plan": {"private": "/private/mapping"}}


class WebAddressMigrationTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    create = test_web.LocalWebTests.create

    def test_preview_is_local_and_does_not_expose_the_mapping(self):
        _, case = self.create()
        route = "/api/cases/" + case["id"]
        with patch("liquid_tracer.address_migration.preview_merge", return_value=report()), \
                patch.object(self.server, "start_job") as start:
            result = self.success(route + "/address-merge-preview", {})
            self.assertEqual(result["approval_sha256"], "a" * 64)
            self.assertNotIn("plan", result)
            self.assertNotIn("/private", str(result))
            start.assert_not_called()
        self.assertEqual(self.request(route + "/address-merge-preview", {},
                                     headers={"X-Liquid-CSRF": "wrong"})[0], 403)

    def test_apply_requires_exact_approval_and_uses_only_server_identifiers(self):
        _, case = self.create()
        route = "/api/cases/" + case["id"] + "/actions"
        path, _ = self.server.case(case["id"])
        with patch("liquid_tracer.address_migration.preview_merge", return_value=report()), \
                patch.object(self.server, "start_job", return_value={"id": "test"}) as start:
            for body in ({"action": "address-merge"},
                         {"action": "address-merge", "confirm_merge": True, "approval_sha256": "b" * 64},
                         {"action": "address-merge", "confirm_merge": True, "approval_sha256": "../../"}):
                self.assertEqual(self.request(route, body)[0], 400)
                start.assert_not_called()
            self.success(route, {"action": "address-merge", "confirm_merge": True,
                "approval_sha256": "a" * 64, "board": "OTHER", "run_id": "../../", "args": ["--delete-all"]}, 202)
            self.assertEqual(start.call_args.args[0], ["miro-merge-addresses", "--case", str(path),
                "--board", "SYNTHETIC-BOARD=", "--approve-plan", "a" * 64])
            self.assertTrue(start.call_args.kwargs["live"])
        safe = self.server.public_result({**report(), "converted": True, "sync_required": True,
                                         "backup": "/private/path"}, "address-merge", path, [])
        self.assertTrue(safe["converted"])
        self.assertNotIn("backup", safe)
        self.assertNotIn("plan", safe)


@unittest.skipUnless(importlib.util.find_spec("textual"), "optional Textual UI")
class MenuAddressMigrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_menu_compaction.CompactionMenuTests.asyncSetUp
    run_files = test_menu_compaction.CompactionMenuTests.run_files
    click = test_menu_compaction.CompactionMenuTests.click
    open_case = test_menu_compaction.CompactionMenuTests.open_case

    async def test_review_cancel_confirm_and_live_command(self):
        from textual.widgets import Checkbox, Static
        app = create_app(self.root)
        with patch("liquid_tracer.address_migration.preview_merge", return_value=report()), \
                patch("liquid_tracer.menu.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run, \
                patch.object(app, "suspend", side_effect=contextlib.nullcontext):
            async with app.run_test(size=(115, 60)) as pilot:
                await self.open_case(app, pilot)
                await self.click(app, pilot, "#address-merge")
                self.assertEqual(app.focused.id, "cancel")
                run.assert_not_called()
                await self.click(app, pilot, "#submit")
                self.assertIn("confirmation", str(app.screen.query_one("#form-error", Static).render()))
                run.assert_not_called()
                await self.click(app, pilot, "#cancel")
                await self.click(app, pilot, "#address-merge")
                app.screen.query_one("#address-merge-approved", Checkbox).value = True
                await self.click(app, pilot, "#submit")
                command = run.call_args.args[0]
                self.assertIn("secretspec", command[0])
                self.assertIn("miro-merge-addresses", command)
                self.assertEqual(command[-2:], ["--approve-plan", "a" * 64])
        self.assertEqual(self.run_files(), self.before)

    async def test_changed_approval_stops_before_live_action(self):
        from textual.widgets import Checkbox, Static
        changed = {**report(), "approval_sha256": "c" * 64}
        app = create_app(self.root)
        with patch("liquid_tracer.address_migration.preview_merge", side_effect=[report(), changed]), \
                patch("liquid_tracer.menu.subprocess.run") as run:
            async with app.run_test(size=(115, 60)) as pilot:
                await self.open_case(app, pilot)
                await self.click(app, pilot, "#address-merge")
                app.screen.query_one("#address-merge-approved", Checkbox).value = True
                await self.click(app, pilot, "#submit")
                self.assertIn("changed", str(app.screen.query_one("#form-error", Static).render()))
                run.assert_not_called()
