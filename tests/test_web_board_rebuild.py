"""A fresh-board publication uses a pinned snapshot and bounded browser inputs."""

import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.investigations import read_case, update_case
from tests import test_web


class WebBoardRebuildTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    wait = test_web.LocalWebTests.wait
    create = test_web.LocalWebTests.create

    def traced(self):
        _, case = self.create()
        route = "/api/cases/" + case["id"]
        run = self.wait(self.success(route + "/actions", {"action": "trace"}, 202))["run_id"]
        path, _ = self.server.case(case["id"])
        update_case(path, {"miro_board": "OLD=", "run_defaults": {"max_new_items": 750}})
        return route, path, run

    def body(self, run):
        return {"action": "miro-rebuild", "run_id": run, "source_board": "OLD=",
                "name": "Refreshed graph", "max_new_items": 5000}

    def test_rebuild_uses_explicit_snapshot_source_and_one_time_budget_despite_old_recovery(self):
        route, path, run = self.traced()
        before = read_case(path)
        with patch.object(self.server, "start_job", return_value={"id": "fresh"}) as start, \
                patch("liquid_tracer.cli.miro_recovery_status", return_value={"pending_count": 3}):
            self.success(route + "/actions", self.body(run), 202)
        start.assert_called_once_with(["miro-rebuild-board", "--case", str(path), "--run", run,
            "--source-board", "OLD=", "--name", "Refreshed graph", "--max-new-items", "5000"],
            action="miro-rebuild", live=True, case=path)
        self.assertEqual(read_case(path), before)

    def test_browser_cannot_supply_paths_visibility_or_settings_and_budget_is_validated(self):
        route, _, run = self.traced()
        with patch.object(self.server, "start_job") as start:
            for extra in ({"board": "OTHER"}, {"settings": {}}, {"case": "/private"},
                          {"arguments": []}, {"visibility": "team"}, {"team_id": "123"}):
                self.assertEqual(self.request(route + "/actions", {**self.body(run), **extra})[0], 400)
            for budget in (True, -1, 1.5, "3000", None, 2 ** 53):
                self.assertEqual(self.request(route + "/actions", {**self.body(run), "max_new_items": budget})[0], 400)
            for invalid in ("latest", "../private", {}, None):
                self.assertEqual(self.request(route + "/actions", {**self.body(run), "run_id": invalid})[0], 400)
            start.assert_not_called()

    def test_rebuild_retains_csrf_busy_and_saved_board_gates(self):
        route, path, run = self.traced()
        with patch.object(self.server, "start_job") as start:
            self.assertEqual(self.request(route + "/actions", self.body(run), headers={"X-Liquid-CSRF": ""})[0], 403)
            self.server.active_job = "busy"
            self.assertEqual(self.request(route + "/actions", self.body(run))[0], 409)
            self.server.active_job = None
            update_case(path, {"miro_board": None})
            self.assertEqual(self.request(route + "/actions", self.body(run))[0], 400)
            start.assert_not_called()

    def test_resume_forwards_original_source_even_when_new_board_is_linked(self):
        route, path, run = self.traced()
        update_case(path, {"miro_board": "NEW="})
        with patch.object(self.server, "start_job", return_value={"id": "resume"}) as start:
            self.success(route + "/actions", self.body(run), 202)
        args = start.call_args.args[0]
        self.assertEqual(args[args.index("--source-board") + 1], "OLD=")

    def test_summary_exposes_resume_fields_and_isolates_an_unreadable_receipt(self):
        route, path, run = self.traced()
        status = {"status": "syncing", "previous_board_id": "OLD=", "board_id": "NEW=",
                  "run_id": run, "name": "Saved name", "notice": "Resume publication."}
        with patch("liquid_tracer.board_rebuild.rebuild_status", return_value={**status,
                "receipt_file": "/private/receipt", "resume_command": "private path", "token": "secret"}):
            self.assertEqual(self.success(route)["miro_rebuild"], status)
        with patch("liquid_tracer.board_rebuild.rebuild_status", side_effect=TraceError("private path")):
            detail = self.success(route)
            self.assertEqual(detail["miro_rebuild"]["status"], "unavailable")
            self.assertEqual(detail["miro_board"], "OLD=")
            self.assertEqual(detail["latest_run"], run)
            self.assertNotIn("private path", str(detail))

    def test_result_constructs_board_links_and_excludes_private_artifacts(self):
        _, path, run = self.traced()
        result = self.server.public_result({"run_id": run, "board_id": "NEW=", "previous_board_id": "OLD=",
            "board_url": "https://attacker.invalid", "previous_board_url": "javascript:alert(1)",
            "rebuild_status": "complete", "receipt_file": "/private/file", "state_file": "/private/state",
            "token": "secret", "created": True, "reused": False}, "miro-rebuild", path, [])
        self.assertEqual(result["board_url"], "https://miro.com/app/board/NEW%3D/")
        self.assertEqual(result["previous_board_url"], "https://miro.com/app/board/OLD%3D/")
        self.assertEqual(result["rebuild_status"], "complete")
        self.assertNotIn("receipt_file", result)
        self.assertNotIn("state_file", result)
        self.assertNotIn("secret", str(result))


if __name__ == "__main__":
    unittest.main()
