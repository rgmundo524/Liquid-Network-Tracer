"""Saved caps are opt-in across the HTTP, prepared-job, and publication boundaries."""

import unittest
from unittest.mock import patch

from liquid_tracer.common import read_json, save_json
from liquid_tracer.investigations import read_case
from liquid_tracer.shared_collection import collect_prepared
from tests import test_web


class UnlimitedWorkflowTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    wait = test_web.LocalWebTests.wait
    create = test_web.LocalWebTests.create

    caps = {"max_transactions": 123, "max_outpoints": 456, "max_requests": 789,
            "max_seconds": 321, "max_new_items": 987}

    def setup_case(self):
        _, info = self.create()
        case, _ = self.server.case(info["id"])
        return case, "/api/cases/" + info["id"] + "/actions"

    def settings(self, case, enabled):
        # A missing field represents an existing investigation from before the
        # opt-in setting. Its finite numbers must survive opening and launching.
        metadata = read_json(case / "case.json")
        metadata["run_defaults"].update(self.caps)
        metadata["run_defaults"].pop("budget_limits_enabled", None)
        if enabled is not None:
            metadata["run_defaults"]["budget_limits_enabled"] = enabled
        save_json(case / "case.json", metadata)
        return (case / "case.json").read_bytes()

    def assert_caps(self, arguments, enabled, keys=None):
        for key in keys or ("max_transactions", "max_outpoints", "max_requests", "max_seconds"):
            flag = "--" + key.replace("_", "-")
            self.assertEqual(float(arguments[arguments.index(flag) + 1]), self.caps[key] if enabled else 0)

    def test_private_fresh_and_continue_ignore_legacy_caps_until_enabled(self):
        case, route = self.setup_case()
        for continuation in (False, True):
            for enabled in (None, False, True):
                with self.subTest(continuation=continuation, enabled=enabled):
                    before = self.settings(case, enabled)
                    with patch.object(self.server, "start_job", return_value={"id": "captured"}) as start:
                        self.success(route, {"action": "trace", "hops": 2}, 202)
                    args = start.call_args.args[0]
                    self.assert_caps(args, enabled)
                    self.assertEqual("--resume" in args, continuation)
                    self.assertEqual(args[args.index("--additional-hops" if continuation else "--hops") + 1], "2")
                    self.assertEqual((case / "case.json").read_bytes(), before)
            if not continuation:
                self.settings(case, None)
                result = self.wait(self.success(route, {"action": "trace", "hops": 1}, 202))
                state = read_json(case / "runs" / result["run_id"] / "trace.json")
                self.assertTrue(all(state["limits"][key] == 0 for key in self.caps if key != "max_new_items"))

    def test_shared_fresh_and_continue_pin_effective_caps_at_launch(self):
        case, route = self.setup_case()
        identity = read_case(case)["case_id"]
        self.settings(case, None)
        result = self.wait(self.success(route, {"action": "shared-trace", "mode": "collect",
            "case_ids": [identity], "hops": 1}, 202))
        for continuation in (False, True):
            for enabled in (None, False, True):
                with self.subTest(continuation=continuation, enabled=enabled):
                    before = self.settings(case, enabled)
                    body = {"action": "shared-trace", "mode": "continue" if continuation else "collect", "hops": 2}
                    body.update({"run_id": result["run_id"]} if continuation else {"case_ids": [identity]})
                    with patch.object(self.server, "start_job", return_value={"id": "captured"}) as start:
                        self.success(route, body, 202)
                    args = start.call_args.args[0]
                    request_id = args[args.index("--request") + 1]
                    saved = read_json(self.server.root / ".shared-collection" / "requests" / (request_id + ".json"))
                    self.assertEqual(saved["resume"], result["run_id"] if continuation else None)
                    self.assertEqual(saved["settings"]["hops"], 3 if continuation else 2)
                    for key, finite in self.caps.items():
                        self.assertEqual(saved["settings"][key], finite if enabled else 0)
                    self.assertEqual((case / "case.json").read_bytes(), before)
                    # Later preference edits cannot change a prepared job.
                    self.settings(case, not enabled)
                    with patch("liquid_tracer.cli.run_trace", return_value={}) as run:
                        collect_prepared(case, request_id)
                    parsed = run.call_args.args[0]
                    for key in self.caps.keys() - {"max_new_items"}:
                        self.assertEqual(getattr(parsed, key), self.caps[key] if enabled else 0)
        self.assertIsNone(read_case(case).get("latest_run"))

    def test_count_lookup_and_managed_miro_routes_use_effective_budgets(self):
        case, route = self.setup_case()
        self.settings(case, None)
        result = self.wait(self.success(route, {"action": "trace", "hops": 1}, 202))
        preview = result["run_id"] + "-plots-12345678"
        record = {"id": "a" * 32, "board_id": "SYNTHETIC=", "goal": "full",
                  "can_sync": True, "creation_preview_id": preview}
        graph = {"nodes": [{"id": "synthetic"}], "plot": {"goal": "full", "layout_mode": "fresh"}}
        actions = [
            {"action": "plot-sync", "goal": "full", "run_id": "latest", "min_hops": 0,
             "max_hops": 2, "name": "Fresh"},
            {"action": "plot-sync", "goal": "full", "run_id": "latest", "min_hops": 0,
             "max_hops": 2, "layout_mode": "update", "board_record_id": record["id"]},
            {"action": "board-create-sync", "name": "Saved", "preview_id": preview},
            {"action": "board-sync", "record_id": record["id"], "preview_id": preview, "reorganize": False},
        ]
        for enabled in (None, False, True):
            with self.subTest(enabled=enabled):
                before = self.settings(case, enabled)
                with patch.object(self.server, "start_job", return_value={"id": "captured"}) as start, \
                        patch("liquid_tracer.investigation_boards.list_boards", return_value=[record]), \
                        patch("liquid_tracer.plots.reviewed_plot", return_value=(graph, None)):
                    self.success(route, {"action": "address-counts", "run_id": "latest"}, 202)
                    self.assert_caps(start.call_args.args[0], enabled, ("max_requests", "max_seconds"))
                    for body in actions:
                        with self.subTest(action=body):
                            self.success(route, body, 202)
                            args = start.call_args.args[0]
                            self.assertEqual(int(args[args.index("--max-items") + 1]),
                                             self.caps["max_new_items"] if enabled else 0)
                self.assertEqual((case / "case.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
