"""Admission uses operation identities, with conservative legacy compatibility."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import save_json
from liquid_tracer.job_resources import PARALLEL_ACTIONS, conflicts, job_resources


class JobResourceTests(unittest.TestCase):
    def test_argument_values_cannot_become_scope_flags(self):
        for name in ("--board", "--record", "--preview", "--run", "--layout-mode"):
            resource = job_resources(["plot-sync", "--name", name, "--max-items", "750"], "plot-sync")
            self.assertEqual(resource, {"resource_kind": "board", "resource_key": "new-board"})

    def test_fresh_graph_reservation_does_not_inspect_board_mappings(self):
        with patch("liquid_tracer.investigation_boards.list_boards",
                   side_effect=AssertionError("Mapping validation belongs in the worker")):
            resource = job_resources(["plot-sync", "--case", "/synthetic/case", "--run", "a" * 16,
                                      "--name", "New graph"], "plot-sync", Path("/synthetic/case"))
        self.assertEqual(resource, {"resource_kind": "board", "resource_key": "new-board",
                                    "source_run_id": "a" * 16})

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

    def test_delete_admission_uses_only_the_pinned_board_id(self):
        with patch("liquid_tracer.investigation_boards.list_boards",
                   side_effect=AssertionError("Do not inspect mappings for deletion admission")), \
                patch("liquid_tracer.investigations.read_case",
                      side_effect=AssertionError("The target was already pinned")), \
                patch("liquid_tracer.common.read_json",
                      side_effect=AssertionError("Do not read graph or evidence files")):
            resource = job_resources(["investigation-board-delete", "--case", "/synthetic/case",
                                      "--record", "record", "--board", "TARGET=", "--confirm-delete"],
                                     "board-delete", Path("/synthetic/case"))
        self.assertIn("board-delete", PARALLEL_ACTIONS)
        self.assertEqual(resource, {"resource_kind": "board_delete", "resource_key": "TARGET="})

    def test_delete_conflicts_with_every_same_case_resource_in_both_directions(self):
        deletion = {"case_id": "a", "resource_kind": "board_delete", "resource_key": "TARGET="}
        for kind in ("exclusive", "collection", "shared_collection", "plot", "board", "board_delete"):
            with self.subTest(kind=kind):
                active = {"case_id": "a", "resource_kind": kind, "resource_key": "UNRELATED="}
                self.assertTrue(conflicts(deletion, active))
                self.assertTrue(conflicts(active, deletion))

    def test_delete_conflicts_only_with_matching_remote_board_across_cases(self):
        deletion = {"case_id": "a", "resource_kind": "board_delete", "resource_key": "TARGET="}
        for kind in ("board", "board_delete", "exclusive"):
            for target in ("TARGET=", "UNRELATED=", "new-board", None, ""):
                with self.subTest(kind=kind, target=target):
                    active = {"case_id": "b", "resource_kind": kind, "resource_key": target}
                    self.assertEqual(conflicts(deletion, active), target == "TARGET=")
                    self.assertEqual(conflicts(active, deletion), target == "TARGET=")
        for kind in ("collection", "shared_collection", "plot"):
            active = {"case_id": "b", "resource_kind": kind, "resource_key": "TARGET="}
            self.assertFalse(conflicts(deletion, active))
            self.assertFalse(conflicts(active, deletion))

    def test_unknown_or_new_board_reservations_do_not_match_remote_deletion(self):
        for target in (None, "", "new-board"):
            with self.subTest(target=target):
                deletion = {"case_id": "a", "resource_kind": "board_delete", "resource_key": target}
                active = {"case_id": "b", "resource_kind": "board", "resource_key": target}
                self.assertFalse(conflicts(deletion, active))
                self.assertFalse(conflicts(active, deletion))

    def test_legacy_explicit_targets_need_no_mapping_or_metadata_reads(self):
        with patch("liquid_tracer.investigation_boards.list_boards",
                   side_effect=AssertionError("No legacy mapping scan")), \
                patch("liquid_tracer.investigations.read_case",
                      side_effect=AssertionError("The explicit target takes precedence")):
            for flag in ("--board", "--board-id", "--source-board"):
                with self.subTest(flag=flag):
                    resource = job_resources(["miro-sync", flag, "https://miro.com/app/board/TARGET%3D/"],
                                             "miro-sync", "/synthetic/case")
                    self.assertEqual(resource, {"resource_kind": "exclusive", "resource_key": "TARGET="})

    def test_legacy_linked_targets_use_only_case_metadata(self):
        for action in ("miro-sync", "miro-organize", "miro-compact", "miro-frames", "miro-frame-review",
                       "miro-frame-recover", "miro-recover", "miro-preview", "miro-rebuild", "address-merge"):
            with self.subTest(action=action), \
                    patch("liquid_tracer.investigations.read_case", return_value={"miro_board": "TARGET="}) as read, \
                    patch("liquid_tracer.investigation_boards.list_boards",
                          side_effect=AssertionError("No legacy mapping scan")):
                resource = job_resources([action, "--case", "/synthetic/case"], action, "/synthetic/case")
                self.assertEqual(resource, {"resource_kind": "exclusive", "resource_key": "TARGET="})
                read.assert_called_once_with(Path("/synthetic/case"))
                self.assertTrue(conflicts({**resource, "case_id": "b"},
                                          {"case_id": "a", "resource_kind": "board_delete", "resource_key": "TARGET="}))

    def test_legacy_without_linked_target_and_unrelated_actions_stay_exclusive(self):
        with patch("liquid_tracer.investigations.read_case", return_value={}):
            self.assertEqual(job_resources(["miro-create-board"], "miro-create", "/synthetic/case"),
                             {"resource_kind": "exclusive"})
        with patch("liquid_tracer.investigations.read_case",
                   side_effect=AssertionError("Local actions do not need a Miro target")):
            self.assertEqual(job_resources(["csv-export"], "csv", "/synthetic/case"),
                             {"resource_kind": "exclusive"})

    def test_legacy_value_arguments_cannot_masquerade_as_board_flags(self):
        for flag in ("--name", "--visibility", "--approve-plan", "--review-id", "--item-id", "--compact-preview"):
            with self.subTest(flag=flag), \
                    patch("liquid_tracer.investigations.read_case", return_value={"miro_board": "TARGET="}):
                resource = job_resources(["miro-sync", flag, "--board", "--run", "a" * 16],
                                         "miro-sync", "/synthetic/case")
                self.assertEqual(resource, {"resource_kind": "exclusive", "resource_key": "TARGET=",
                                            "source_run_id": "a" * 16})

    def test_old_preview_keeps_case_exclusivity_and_remote_board_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory)
            preview = "a" * 16 + "-plots-" + "b" * 8
            report = case / "previews" / preview / "plot.json"
            report.parent.mkdir(parents=True)
            save_json(report, {})
            record = {"id": "record", "creation_preview_id": preview, "board_id": "TARGET="}
            with patch("liquid_tracer.investigation_boards.list_boards", return_value=[record]):
                for action in ("board-sync", "board-create-sync"):
                    with self.subTest(action=action):
                        resource = job_resources([action, "--record", "record", "--preview", preview], action, case)
                        self.assertEqual(resource, {"resource_kind": "exclusive", "resource_key": "TARGET=",
                                                    "source_run_id": "a" * 16})
                        self.assertTrue(conflicts({**resource, "case_id": "b"},
                                                  {"case_id": "a", "resource_kind": "board_delete", "resource_key": "TARGET="}))


if __name__ == "__main__":
    unittest.main()
