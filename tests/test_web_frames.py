"""Framing is an explicit live action after the saved graph has been synced."""

import unittest
from unittest.mock import patch

from liquid_tracer.investigations import update_case
from tests import test_web


class WebFramesTests(unittest.TestCase):
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
        update_case(path, {"miro_board": "SYNTHETIC-BOARD", "run_defaults": {
            "max_new_items": 19, "layout_attempts": 100, "include_fees": True,
            "group_context_inputs": True, "connector_style": "elbowed"}})
        return route, path, run

    def test_frame_job_uses_saved_board_and_budget_without_layout_or_trace_flags(self):
        route, path, run = self.traced()
        with patch.object(self.server, "start_job", return_value={"id": "synthetic"}) as start:
            self.success(route + "/actions", {"action": "miro-frames", "run_id": run}, 202)
        start.assert_called_once_with(["miro-frames", "--case", str(path), "--run", run,
            "--board", "SYNTHETIC-BOARD", "--max-new-items", "19"],
            action="miro-frames", live=True, case=path)

    def test_browser_cannot_override_paths_board_options_or_graph_settings(self):
        route, _, run = self.traced()
        with patch.object(self.server, "start_job") as start:
            for extra in ({"board": "OTHER"}, {"case": "/private"}, {"settings": {}},
                          {"arguments": ["trace"]}, {"preview_id": "anything"},
                          {"live": False}, {"max_new_items": 1000}, {"confirm_absent": True}):
                with self.subTest(extra=extra):
                    self.assertEqual(self.request(route + "/actions", {
                        "action": "miro-frames", "run_id": run, **extra})[0], 400)
            for invalid in ("../private", "--run", {}, True, None):
                with self.subTest(run=invalid):
                    self.assertEqual(self.request(route + "/actions", {
                        "action": "miro-frames", "run_id": invalid})[0], 400)
            start.assert_not_called()

    def test_missing_run_board_or_pending_recovery_blocks_live_job(self):
        _, case = self.create()
        route = "/api/cases/" + case["id"]
        with patch.object(self.server, "start_job") as start:
            self.assertEqual(self.request(route + "/actions", {"action": "miro-frames"})[0], 400)
            start.assert_not_called()
        route, path, run = self.traced()
        body = {"action": "miro-frames", "run_id": run}
        with patch.object(self.server, "start_job") as start:
            with patch("liquid_tracer.cli.miro_recovery_status", return_value={"pending_count": 1}):
                code, message, _ = self.request(route + "/actions", body)
                self.assertEqual(code, 400)
                self.assertIn("Recover", message["error"])
            update_case(path, {"miro_board": None})
            self.assertEqual(self.request(route + "/actions", body)[0], 400)
            start.assert_not_called()

    def test_frames_retain_csrf_and_active_job_gates(self):
        route, _, run = self.traced()
        body = {"action": "miro-frames", "run_id": run}
        with patch.object(self.server, "start_job") as start:
            self.assertEqual(self.request(route + "/actions", body, headers={"X-Liquid-CSRF": ""})[0], 403)
            self.server.active_job = "synthetic-busy"
            try:
                self.assertEqual(self.request(route + "/actions", body)[0], 409)
            finally:
                self.server.active_job = None
            start.assert_not_called()

    def test_result_exposes_frame_counts_without_saved_plan_or_paths(self):
        route, path, run = self.traced()
        result = {"run_id": run, "created": 3, "created_frames": 3, "updated": 9,
                  "updated_frames": 2, "deleted": 1, "frames_only": True}
        self.assertEqual(self.server.public_result({**result, "plan": {"private": "data"},
            "state_path": "/private/state", "token": "PRIVATE"}, "miro-frames", path, []), result)

    def test_frame_counters_require_nonnegative_safe_integers(self):
        for field in ("created_frames", "updated_frames"):
            for invalid in (True, -1, "2", float("nan"), float("inf"), 2 ** 53):
                with self.subTest(field=field, value=invalid):
                    self.assertNotIn(field, self.server.public_result(
                        {field: invalid}, "miro-frames", None, []))


if __name__ == "__main__":
    unittest.main()
