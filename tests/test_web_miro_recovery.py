"""The local UI may request only a confirmed recovery of its saved Miro board."""

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.investigations import create_investigation, read_case, update_case
from liquid_tracer.web import LocalServer


class WebMiroRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        assets = self.base / "assets"
        assets.mkdir()
        self.server = LocalServer(self.base / "cases", assets, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.case = create_investigation(self.server.root, "Synthetic recovery", seeds=["a" * 64 + ":0"],
                                         board="SYNTHETIC-RECOVERY-BOARD")
        self.case_id = read_case(self.case)["case_id"]
        self.route = "/api/cases/" + self.case_id
        helper = patch("liquid_tracer.cli.miro_recovery_status", create=True,
                       return_value={"pending_count": 20, "can_confirm_empty": True})
        self.status = helper.start()
        self.addCleanup(helper.stop)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, body=None, *, csrf=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        self.addCleanup(connection.close)
        headers = {"Origin": self.server.origin, "Content-Type": "application/json"}
        if csrf:
            headers["X-Liquid-CSRF"] = self.server.csrf
        connection.request("GET" if body is None else "POST", self.route + ("/actions" if body is not None else ""),
                           None if body is None else json.dumps(body), headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def test_detail_exposes_counts_without_live_action_or_logical_keys(self):
        with patch.object(self.server, "start_job") as start:
            code, detail = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(detail["miro_recovery"], {"pending_count": 20, "can_confirm_empty": True})
        self.status.assert_called_once_with(self.case)
        start.assert_not_called()
        self.assertNotIn("miro_recovery", self.server.case_summary(self.case, read_case(self.case)))

    def test_recovery_requires_explicit_boolean_confirmation_and_saved_board(self):
        with patch.object(self.server, "start_job") as start:
            for value in (None, False, "true", "on", 1, [], {}):
                with self.subTest(value=value):
                    code, message = self.request({"action": "miro-recover", "confirm_empty": value})
                    self.assertEqual(code, 400, message)
            self.assertEqual(self.request({"action": "miro-recover"})[0], 400)
            update_case(self.case, {"miro_board": None})
            code, message = self.request({"action": "miro-recover", "confirm_empty": True,
                                          "board": "BROWSER-SUPPLIED-BOARD"})
            self.assertEqual(code, 400, message)
            start.assert_not_called()
        self.status.assert_not_called()

    def test_recovery_rechecks_local_eligibility_before_starting_worker(self):
        self.status.return_value = {"pending_count": 20, "can_confirm_empty": False}
        with patch.object(self.server, "start_job") as start:
            code, message = self.request({"action": "miro-recover", "confirm_empty": True,
                                          "can_confirm_empty": True})
            self.assertEqual(code, 400, message)
            start.assert_not_called()
        self.status.assert_called_once_with(self.case)

    def test_recovery_uses_fixed_live_command_and_ignores_browser_paths_or_state(self):
        with patch.object(self.server, "start_job", return_value={"id": "synthetic-job"}) as start:
            code, result = self.request({"action": "miro-recover", "confirm_empty": True,
                "case": "/browser/path", "board": "BROWSER-BOARD", "state": {"pending": []},
                "arguments": ["trace"], "run_id": "browser-run", "live": False})
        self.assertEqual(code, 202, result)
        self.assertEqual(result, {"id": "synthetic-job"})
        start.assert_called_once_with(["miro-recover", "--case", str(self.case), "--confirm-empty"],
                                      action="miro-recover", live=True, case=self.case)

    def test_recovery_retains_csrf_and_active_job_gates(self):
        body = {"action": "miro-recover", "confirm_empty": True}
        with patch.object(self.server, "start_job") as start:
            self.assertEqual(self.request(body, csrf=False)[0], 403)
            self.server.active_job = "synthetic-busy"
            try:
                self.assertEqual(self.request(body)[0], 409)
            finally:
                self.server.active_job = None
            start.assert_not_called()
        self.status.assert_not_called()

    def test_recovery_result_only_exposes_validated_success_metadata(self):
        expected = {"recovery": "confirmed_empty_board", "recovered_items": 20, "shape_batch_size": 1,
                    "run_id": "a" * 16}
        report = {**expected, "state_path": "/private/state.json", "pending": {"private-key": {}},
                  "message": "SYNTHETIC-PRIVATE-SENTINEL", "token": "secret"}
        self.assertEqual(self.server.public_result(report, "miro-recover", self.case, []), expected)
        self.assertEqual(self.server.public_result(report, "miro-sync", self.case, []), {"run_id": "a" * 16})
        invalid_run = self.server.public_result({**expected, "run_id": "/private/run"}, "miro-recover", self.case, [])
        self.assertNotIn("run_id", invalid_run)
        for field, value in (("recovery", "private-key"), ("recovered_items", True),
                             ("recovered_items", 21), ("recovered_items", 0),
                             ("shape_batch_size", True), ("shape_batch_size", 20)):
            with self.subTest(field=field, value=value):
                self.assertEqual(self.server.public_result({**expected, field: value}, "miro-recover", self.case, []), {})


if __name__ == "__main__":
    unittest.main()
