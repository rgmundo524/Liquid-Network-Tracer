"""The browser can review and resolve one frame without supplying journal data."""

import copy
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.investigations import create_investigation, read_case, update_case
from liquid_tracer.web import LocalServer, public_frame_recovery


REVIEW_ID = "a" * 64
RUN_ID = "b" * 16
FRAME = {"title": "Synthetic activity", "x": -120.5, "y": 60, "width": 900, "height": 600}


def review_report():
    return {"schema_version": 1, "recovery": "pending_frame_review", "review_id": REVIEW_ID,
            "run_id": RUN_ID, "pending_frame": dict(FRAME),
            "candidates": [{"id": "frame_123-456", **FRAME}],
            "potential_match_count": 0, "can_confirm_absent": False}


class WebFrameRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        assets = base / "assets"
        assets.mkdir()
        self.server = LocalServer(base / "cases", assets, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.case = create_investigation(self.server.root, "Synthetic frame recovery",
            seeds=["a" * 64 + ":0"], board="SYNTHETIC-FRAME-BOARD")
        self.route = "/api/cases/" + read_case(self.case)["case_id"]
        helper = patch("liquid_tracer.cli.miro_recovery_status", return_value={
            "pending_count": 1, "can_confirm_empty": False, "can_recover_frame": True})
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
        connection.request("GET" if body is None else "POST",
            self.route + ("/actions" if body is not None else ""),
            None if body is None else json.dumps(body), headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def test_review_uses_saved_board_and_live_fixed_command(self):
        with patch.object(self.server, "start_job", return_value={"id": "synthetic-job"}) as start:
            code, result = self.request({"action": "miro-frame-review"})
        self.assertEqual(code, 202, result)
        start.assert_called_once_with(["miro-frame-review", "--case", str(self.case)],
                                     action="miro-frame-review", live=True, case=self.case)
        self.status.assert_called_once_with(self.case)
        code, detail = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(detail["miro_recovery"], self.status.return_value)

    def test_recovery_accepts_exactly_one_explicit_resolution(self):
        for selection, suffix in (({"item_id": "frame_123-456"}, ["--item-id", "frame_123-456"]),
                                  ({"confirm_absent": True}, ["--confirm-absent"])):
            with self.subTest(selection=selection), patch.object(
                    self.server, "start_job", return_value={"id": "synthetic-job"}) as start:
                code, result = self.request({"action": "miro-frame-recover", "review_id": REVIEW_ID, **selection})
                self.assertEqual(code, 202, result)
                start.assert_called_once_with(["miro-frame-recover", "--case", str(self.case),
                    "--review-id", REVIEW_ID, *suffix], action="miro-frame-recover", live=True, case=self.case)

    def test_browser_cannot_supply_paths_state_settings_or_run(self):
        for extra in ({"case": "/private/case"}, {"board": "OTHER-BOARD"},
                      {"arguments": ["trace"]}, {"pending": {}}, {"run_id": RUN_ID},
                      {"settings": {}}, {"live": False}, {"output": "/private/output"}):
            for body in ({"action": "miro-frame-review"},
                         {"action": "miro-frame-recover", "review_id": REVIEW_ID, "confirm_absent": True}):
                with self.subTest(extra=extra, action=body["action"]), patch.object(self.server, "start_job") as start:
                    code, message = self.request({**body, **extra})
                    self.assertEqual(code, 400, message)
                    start.assert_not_called()
        self.status.assert_not_called()

    def test_recovery_rejects_invalid_ids_ambiguous_selection_and_nonboolean_confirmation(self):
        bodies = [
            {"review_id": value, "item_id": "frame-1"}
            for value in (None, "A" * 64, "a" * 63, "../private", 12)
        ] + [
            {"review_id": REVIEW_ID, "item_id": value}
            for value in (None, "", "--help", "../../private", "id with space", "x" * 201, 123)
        ] + [
            {"review_id": REVIEW_ID, "confirm_absent": value}
            for value in (None, False, "true", 1, [], {})
        ] + [{"review_id": REVIEW_ID},
             {"review_id": REVIEW_ID, "item_id": "frame-1", "confirm_absent": True}]
        with patch.object(self.server, "start_job") as start:
            for body in bodies:
                with self.subTest(body=body):
                    code, message = self.request({"action": "miro-frame-recover", **body})
                    self.assertEqual(code, 400, message)
            start.assert_not_called()
        self.status.assert_not_called()

    def test_both_actions_require_local_eligibility_and_saved_board(self):
        bodies = [{"action": "miro-frame-review"},
                  {"action": "miro-frame-recover", "review_id": REVIEW_ID, "confirm_absent": True}]
        with patch.object(self.server, "start_job") as start:
            self.status.return_value["can_recover_frame"] = False
            for body in bodies:
                self.assertEqual(self.request(body)[0], 400)
            self.status.return_value["can_recover_frame"] = True
            update_case(self.case, {"miro_board": None})
            self.status.reset_mock()
            for body in bodies:
                self.assertEqual(self.request(body)[0], 400)
            self.status.assert_not_called()
            start.assert_not_called()

    def test_recovery_retains_csrf_and_active_job_gates(self):
        with patch.object(self.server, "start_job") as start:
            for body in ({"action": "miro-frame-review"},
                         {"action": "miro-frame-recover", "review_id": REVIEW_ID, "item_id": "frame-1"}):
                self.assertEqual(self.request(body, csrf=False)[0], 403)
                self.server.active_job = "synthetic-busy"
                try:
                    self.assertEqual(self.request(body)[0], 409)
                finally:
                    self.server.active_job = None
            start.assert_not_called()
        self.status.assert_not_called()

    def test_result_whitelist_hides_journal_fields_and_rebuilds_board_url(self):
        for action, expected in (("miro-frame-review", review_report()),
                                ("miro-frame-recover", {"recovery": "adopted_frame", "run_id": RUN_ID,
                                                       "resolved_count": 1, "remaining_pending": 0})):
            report = copy.deepcopy(expected)
            report.update(state_path="/private/state.json", token="SECRET", key="private-logical-key",
                          body={"private": True}, board_url="https://example.com/private")
            if action == "miro-frame-review":
                report["pending_frame"]["body"] = {"private": True}
                report["candidates"][0]["createdBy"] = {"id": "private-user"}
            with self.subTest(action=action):
                value = self.server.public_result(report, action, self.case, None)
                self.assertEqual(value, {**expected, "board_url": "https://miro.com/app/board/SYNTHETIC-FRAME-BOARD/"})


class PublicFrameRecoveryTests(unittest.TestCase):
    def test_invalid_review_metadata_cannot_become_an_actionable_review(self):
        invalid = [
            ("schema_version", True), ("schema_version", 2), ("recovery", "private-key"),
            ("run_id", "/private/run"), ("review_id", "x" * 64), ("candidates", {}),
            ("potential_match_count", -1), ("potential_match_count", True),
            ("potential_match_count", 2 ** 53), ("can_confirm_absent", 1),
            ("can_confirm_absent", True),
        ]
        for key, value in invalid:
            with self.subTest(key=key, value=value), self.assertRaises(TraceError):
                public_frame_recovery({**review_report(), key: value}, review=True)
        report = review_report()
        report["candidates"].append(dict(report["candidates"][0]))
        with self.assertRaises(TraceError):
            public_frame_recovery(report, review=True)

    def test_only_finite_geometry_positive_dimensions_and_valid_candidate_ids_are_public(self):
        invalid = [("title", None), ("title", ""), ("title", "x" * 6001), ("x", True),
                   ("x", float("inf")), ("y", float("nan")), ("x", 10 ** 1000),
                   ("width", 0), ("height", -1), ("height", "600")]
        for destination in ("pending_frame", "candidate"):
            for key, value in invalid:
                report = review_report()
                target = report["pending_frame"] if destination == "pending_frame" else report["candidates"][0]
                target[key] = value
                with self.subTest(destination=destination, key=key), self.assertRaises(TraceError):
                    public_frame_recovery(report, review=True)
        for identity in (None, "", "../../private", "bad id", 123, "x" * 201):
            report = review_report()
            report["candidates"][0]["id"] = identity
            with self.subTest(identity=identity), self.assertRaises(TraceError):
                public_frame_recovery(report, review=True)

    def test_potential_matches_prevent_absence_confirmation_even_without_exact_candidates(self):
        report = {**review_report(), "candidates": [], "potential_match_count": 1}
        self.assertEqual(public_frame_recovery(report, review=True), report)
        with self.assertRaises(TraceError):
            public_frame_recovery({**report, "can_confirm_absent": True}, review=True)
        empty = {**report, "potential_match_count": 0, "can_confirm_absent": True}
        self.assertEqual(public_frame_recovery(empty, review=True), empty)

    def test_resolution_result_requires_exact_success_counters(self):
        report = {"recovery": "confirmed_absent_frame", "run_id": RUN_ID,
                  "resolved_count": 1, "remaining_pending": 0}
        self.assertEqual(public_frame_recovery(report, review=False), report)
        for key, value in (("recovery", "anything"), ("run_id", "bad"),
                           ("resolved_count", True), ("resolved_count", 2),
                           ("remaining_pending", False), ("remaining_pending", 1)):
            with self.subTest(key=key, value=value), self.assertRaises(TraceError):
                public_frame_recovery({**report, key: value}, review=False)


if __name__ == "__main__":
    unittest.main()
