"""Admission uses operation identities, with conservative legacy compatibility."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import save_json
from liquid_tracer.job_resources import conflicts, job_resources


class JobResourceTests(unittest.TestCase):
    def test_argument_values_cannot_become_scope_flags(self):
        for name in ("--board", "--record", "--preview", "--run", "--layout-mode"):
            resource = job_resources(["plot-sync", "--name", name, "--max-items", "750"], "plot-sync")
            self.assertEqual(resource, {"resource_kind": "board", "resource_key": "new-board"})

    def test_old_previews_remain_case_exclusive_before_credential_access(self):
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory)
            preview = "a" * 16 + "-plots-" + "b" * 8
            report = case / "previews" / preview / "plot.json"
            report.parent.mkdir(parents=True)
            for version in (None, True, 1):
                save_json(report, {} if version is None else {"input_snapshot_version": version})
                with patch("liquid_tracer.investigation_boards.list_boards", return_value=[]):
                    for action in ("board-sync", "board-create-sync"):
                        result = job_resources([action, "--preview", preview], action, case)
                        self.assertEqual(result["resource_kind"], "board" if type(version) is int else "exclusive")
                        self.assertEqual(result["source_run_id"], "a" * 16)

    def test_recovered_creation_uses_its_existing_board_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory)
            preview = "a" * 16 + "-plots-" + "b" * 8
            report = case / "previews" / preview / "plot.json"
            report.parent.mkdir(parents=True)
            save_json(report, {"input_snapshot_version": 1})
            record = {"id": "record", "creation_preview_id": preview, "board_id": "EXISTING="}
            with patch("liquid_tracer.investigation_boards.list_boards", return_value=[record]):
                resource = job_resources(["create", "--preview", preview], "board-create-sync", case)
            self.assertEqual(resource["resource_key"], "EXISTING=")
            self.assertTrue(conflicts({**resource, "case_id": "a"}, {**resource, "case_id": "b"}))
            self.assertFalse(conflicts({"case_id": "a", "resource_kind": "board", "resource_key": "new-board"},
                                       {"case_id": "b", "resource_kind": "board", "resource_key": "new-board"}))


if __name__ == "__main__":
    unittest.main()
