"""Peg-out search commands, local HTTP boundary, artifacts and terminal flow."""
import contextlib
import io
import json
import subprocess
import unittest
from unittest.mock import patch

from liquid_tracer.cli import main
from liquid_tracer.common import read_json
from liquid_tracer.investigations import read_case, update_case
from liquid_tracer.menu import create_app
from liquid_tracer.pegouts import search_pegouts
from tests import test_web, test_menu_addresses
from tests.test_elk_layout import HAS_ELK

ORIGIN = "ec0b7d94da1c9692a875dfcee91fb869ea7cb5c79ffde619810b98c6098363b9"


class PegoutWebTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    create = test_web.LocalWebTests.create
    wait = test_web.LocalWebTests.wait

    def setup_case(self):
        _, summary = self.create()
        case, _ = self.server.case(summary["id"])
        update_case(case, {"run_defaults": {"max_transactions": 20, "max_outpoints": 100,
                                          "max_requests": 100, "max_seconds": 30, "layout_attempts": 1}})
        return case, "/api/cases/" + summary["id"]

    @unittest.skipUnless(HAS_ELK, "Install the pinned local ELK engine")
    def test_live_search_workflow_uses_fixture_without_changing_full_investigation(self):
        case, route = self.setup_case()
        original = (case / "case.json").read_bytes()
        job = self.success(route + "/actions", {"action": "pegouts", "txid": ORIGIN.upper(),
            "min_hops": 1, "max_hops": 3}, status=202)
        self.assertFalse(job["live"])
        self.assertTrue(job["cancellable"])
        result = self.wait(job)
        self.assertEqual(result["match_count"], 1)
        self.assertEqual(result["txid"], ORIGIN)
        self.assertEqual((result["min_hops"], result["max_hops"]), (1, 3))
        self.assertNotIn("directory", result)
        self.assertNotIn("run_id", result)
        artifact = result["artifact"]
        self.assertEqual(artifact["preview_id"][:16], result["search_id"])
        self.assertIn(b"peg-out", self.success(artifact["preview_url"]))
        report_link = next(row["url"] for row in artifact["downloads"] if row["name"] == "pegouts.json")
        self.assertEqual(self.success(report_link)["match_count"], 1)
        detail = self.success(route)
        self.assertIsNone(detail["latest_run"])
        self.assertEqual(detail["runs"], [])
        self.assertEqual(detail["artifacts"], {})
        self.assertEqual(detail["pegout_searches"][0]["artifact"], artifact)
        self.assertEqual((case / "case.json").read_bytes(), original)
        preview = self.success(route + "/actions", {"action": "pegouts-preview", "search_id": result["search_id"]}, status=202)
        refreshed = self.wait(preview)
        self.assertFalse(preview["live"])
        self.assertEqual(refreshed["search_id"], result["search_id"])
        self.assertEqual(refreshed["match_count"], 1)
        self.assertNotEqual(refreshed["artifact"]["preview_id"], artifact["preview_id"])

    def test_invalid_inputs_cannot_start_worker_or_change_defaults(self):
        case, route = self.setup_case()
        original = (case / "case.json").read_bytes()
        valid = {"action": "pegouts", "txid": ORIGIN, "min_hops": 0, "max_hops": 3}
        invalid = [{**valid, field: value} for field, value in (
            ("txid", "../trace.json"), ("txid", ORIGIN + ":0"), ("min_hops", True),
            ("min_hops", -1), ("max_hops", "3"), ("max_hops", 1.5), ("max_hops", 2147483648))]
        invalid += [{**valid, "min_hops": 4}, {**valid, "fixture": "/tmp/data.json"},
                    {**valid, "settings": {"max_requests": 99999}},
                    {"action": "pegouts", "resume": "../runs/latest"},
                    {"action": "pegouts", "resume": "a" * 16, "txid": ORIGIN},
                    {"action": "pegouts-preview", "search_id": "a" * 16},
                    {"action": "miro-pegouts", "confirm_pegouts": False}]
        with patch.object(self.server, "start_job") as worker:
            for body in invalid:
                with self.subTest(body=body):
                    self.assertEqual(self.request(route + "/actions", body)[0], 400)
            self.assertEqual(self.request(route + "/actions", valid, headers={"X-Liquid-CSRF": "wrong"})[0], 403)
            worker.assert_not_called()
        self.assertEqual((case / "case.json").read_bytes(), original)

    def test_live_source_uses_credentials_and_fixed_arguments(self):
        case, route = self.setup_case()
        metadata = read_case(case)
        metadata["fixture"] = None
        with patch.object(self.server, "start_job", return_value={"id": "fake"}) as worker:
            self.server.action(case, metadata, {"action": "pegouts", "txid": ORIGIN, "min_hops": 2, "max_hops": 4})
            self.assertEqual(worker.call_args.args[0], ["pegouts", "--case", str(case),
                             "--txid", ORIGIN, "--min-hops", "2", "--max-hops", "4"])
            self.assertTrue(worker.call_args.kwargs["live"])

    def test_cli_trace_errors_signal_failure_but_paused_searches_remain_successful(self):
        case, _ = self.setup_case()
        for status, expected in (("error", 1), ("paused", 0)):
            output = io.StringIO()
            report = {"status": status, "search_id": "a" * 16, "match_count": 0}
            with self.subTest(status=status), contextlib.redirect_stdout(output), \
                    patch("liquid_tracer.pegouts.search_pegouts", return_value=report):
                self.assertEqual(main(["pegouts", "--case", str(case), "--txid", ORIGIN]), expected)
            self.assertEqual(json.loads(output.getvalue()), report)

    def test_empty_cli_search_reopens_and_can_resume_without_full_run(self):
        case, route = self.setup_case()
        original = (case / "case.json").read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["pegouts", "--case", str(case), "--txid", ORIGIN,
                                   "--min-hops", "0", "--max-hops", "0"]), 0)
        first = json.loads(output.getvalue())
        self.assertEqual(first["match_count"], 0)
        summary = self.success(route)["pegout_searches"][0]
        self.assertEqual(summary["search_id"], first["search_id"])
        graph_link = next(row["url"] for row in summary["artifact"]["downloads"] if row["name"] == "graph.json")
        self.assertEqual(self.success(graph_link)["nodes"], [])
        self.assertFalse((case / "runs").exists() and any((case / "runs").iterdir()))
        with patch.object(self.server, "start_job", return_value={"id": "fake"}) as worker:
            self.success(route + "/actions", {"action": "pegouts", "resume": first["search_id"]}, status=202)
            self.assertEqual(worker.call_args.args[0], ["pegouts", "--case", str(case), "--resume", first["search_id"]])
            self.assertFalse(worker.call_args.kwargs["live"])
        self.assertEqual((case / "case.json").read_bytes(), original)

    def test_saved_search_survives_preview_failure_and_artifact_tampering_is_rejected(self):
        case, route = self.setup_case()
        with patch("liquid_tracer.pegouts.preview_pegouts", side_effect=RuntimeError("renderer failed")):
            with self.assertRaisesRegex(RuntimeError, "renderer failed"):
                search_pegouts(case, ORIGIN, 0, 0)
        detail = self.success(route)
        summary = detail["pegout_searches"][0]
        self.assertTrue(summary["resumable"])
        self.assertNotIn("artifact", summary)
        job = self.success(route + "/actions", {"action": "pegouts-preview", "search_id": summary["id"]}, status=202)
        result = self.wait(job)
        artifact = result["artifact"]
        path = case / "previews" / artifact["preview_id"] / "graph.html"
        path.write_text("changed")
        self.assertEqual(self.request(artifact["preview_url"])[0], 400)
        self.assertNotIn("artifact", self.success(route)["pegout_searches"][0])

    @unittest.skipUnless(HAS_ELK, "Install the pinned local ELK engine")
    def test_publish_requires_review_and_separate_board(self):
        case, route = self.setup_case()
        result = search_pegouts(case, ORIGIN, 1, 3)
        update_case(case, {"miro_board": "full-board"})
        body = {"action": "miro-pegouts", "preview_id": result["preview_id"],
                "board": "separate-board", "confirm_pegouts": True}
        with patch.object(self.server, "start_job", return_value={"id": "fake"}) as worker:
            for changes in ({"confirm_pegouts": False}, {"board": "full-board"}, {"arguments": ["--force"]}):
                self.assertEqual(self.request(route + "/actions", {**body, **changes})[0], 400)
            worker.assert_not_called()
            self.success(route + "/actions", body, status=202)
            self.assertEqual(worker.call_args.args[0][:3], ["pegouts-publish", "--case", str(case)])
            self.assertIn(result["preview_id"], worker.call_args.args[0])
            self.assertTrue(worker.call_args.kwargs["live"])


@unittest.skipUnless(test_menu_addresses.HAS_TEXTUAL, "Install the optional Textual UI")
class PegoutMenuTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_menu_addresses.AddressMenuTests.asyncSetUp
    click = test_menu_addresses.AddressMenuTests.click

    async def test_terminal_origin_and_range_work_before_first_full_run(self):
        from textual.widgets import Input
        app = create_app(self.root)
        with patch.object(app, "suspend", side_effect=contextlib.nullcontext), \
                patch("liquid_tracer.menu.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as worker:
            async with app.run_test(size=(120, 70)) as pilot:
                app.created(self.case)
                await pilot.pause()
                await self.click(app, pilot, "#pegouts")
                app.screen.query_one("#pegout-txid", Input).value = ORIGIN
                app.screen.query_one("#pegout-min-hops", Input).value = "2"
                app.screen.query_one("#pegout-max-hops", Input).value = "4"
                await self.click(app, pilot, "#pegout-search")
                await pilot.pause()
                args = worker.call_args.args[0]
                self.assertIn("secretspec", args[0])
                self.assertIn("pegouts", args)
                self.assertIn(ORIGIN, args)
                self.assertEqual(args[args.index("--min-hops") + 1], "2")
                self.assertEqual(args[args.index("--max-hops") + 1], "4")

    async def test_terminal_rejects_reversed_range_before_start(self):
        from textual.widgets import Input, Static
        app = create_app(self.root)
        with patch("liquid_tracer.menu.subprocess.run") as worker:
            async with app.run_test(size=(120, 70)) as pilot:
                app.created(self.case)
                await pilot.pause()
                await self.click(app, pilot, "#pegouts")
                app.screen.query_one("#pegout-txid", Input).value = ORIGIN
                app.screen.query_one("#pegout-min-hops", Input).value = "4"
                app.screen.query_one("#pegout-max-hops", Input).value = "2"
                await self.click(app, pilot, "#pegout-search")
                worker.assert_not_called()
                self.assertIn("minimum cannot exceed", str(app.screen.query_one("#pegout-error", Static).render()))

    async def test_changing_reviewed_search_or_destination_clears_publication_approval(self):
        from textual.widgets import Checkbox, Input, Select
        records = [{"search_id": identity, "min_hops": 0, "max_hops": 3, "status": "bounded_complete"}
                   for identity in ("a" * 16, "b" * 16)]
        app = create_app(self.root)
        with patch("liquid_tracer.pegouts.list_pegout_searches", return_value=records):
            async with app.run_test(size=(120, 70)) as pilot:
                app.created(self.case)
                await pilot.pause()
                await self.click(app, pilot, "#pegouts")
                choice = app.screen.query_one("#pegout-saved", Select)
                approved = app.screen.query_one("#pegout-confirm", Checkbox)
                choice.value = records[0]["search_id"]
                await pilot.pause()
                approved.value = True
                choice.value = records[1]["search_id"]
                await pilot.pause()
                self.assertFalse(approved.value)
                approved.value = True
                app.screen.query_one("#pegout-board", Input).value = "another-board"
                await pilot.pause()
                self.assertFalse(approved.value)

    async def test_terminal_preview_uses_saved_search_without_live_credentials(self):
        from textual.widgets import Select
        identity = "a" * 16
        record = {"search_id": identity, "min_hops": 2, "max_hops": 4, "status": "paused"}
        app = create_app(self.root)
        report = json.dumps({"search_id": identity, "match_count": 1, "status": "paused"})
        with patch("liquid_tracer.pegouts.list_pegout_searches", return_value=[record]), \
                patch("liquid_tracer.menu._OfflineCalculation.run", return_value=subprocess.CompletedProcess([], 0, report, "")) as worker:
            async with app.run_test(size=(120, 70)) as pilot:
                app.created(self.case)
                await pilot.pause()
                await self.click(app, pilot, "#pegouts")
                app.screen.query_one("#pegout-saved", Select).value = identity
                await self.click(app, pilot, "#pegout-preview")
                args = worker.call_args.args[0]
                self.assertIn("pegouts-preview", args)
                self.assertEqual(args[args.index("--search") + 1], identity)
                self.assertNotIn("secretspec", " ".join(args))


if __name__ == "__main__":
    unittest.main()
