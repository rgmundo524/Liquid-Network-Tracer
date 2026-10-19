"""Admission uses operation identities, with conservative legacy compatibility."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import save_json
from liquid_tracer.job_resources import PARALLEL_ACTIONS, conflicts, job_resources


class JobResourceTests(unittest.TestCase):
    def test_all_pegin_matching_modes_reserve_the_same_workspace_index(self):
        workspace = "/synthetic/cases"
        resources = []
        with patch("liquid_tracer.common.read_json", side_effect=AssertionError("No admission file reads")), \
                patch("liquid_tracer.investigations.read_case", side_effect=AssertionError("No case reads")):
            for source in (["--txid", "a" * 64, "--vouts", "0,2"],
                           ["--txids", "a" * 64 + "," + "b" * 64],
                           ["--from-liquid", "c" * 64]):
                resources.append(job_resources(["pegin-match", *source, "--investigations-dir", workspace],
                                               "pegin-match"))
        self.assertTrue(all(resource == resources[0] for resource in resources))
        for left in resources:
            for right in resources:
                self.assertTrue(conflicts(left, right))
        other = job_resources(["pegin-match", "--investigations-dir", "/synthetic/other"], "pegin-match")
        self.assertFalse(conflicts(resources[0], other))

    def test_matching_value_options_cannot_masquerade_as_scope_flags(self):
        expected = job_resources(["pegin-match", "--investigations-dir", "/synthetic/cases"], "pegin-match")
        for flag in ("--txids", "--from-liquid", "--vouts", "--resume-report"):
            with self.subTest(flag=flag):
                actual = job_resources(["pegin-match", flag, "--investigations-dir",
                                        "--investigations-dir", "/synthetic/cases"], "pegin-match")
                self.assertEqual(actual, expected)

    def test_pegin_output_admission_needs_no_file_or_board_reads(self):
        with patch("liquid_tracer.common.read_json", side_effect=AssertionError("No admission file reads")), \
                patch("liquid_tracer.investigations.read_case", side_effect=AssertionError("No case required")), \
                patch("liquid_tracer.investigation_boards.list_boards", side_effect=AssertionError("No board required")):
            resource = job_resources(["inspect-txs", "--txids", "a" * 64, "--blockchain", "bitcoin",
                                      "--auth", "none", "--base-url", "https://blockstream.info/api"],
                                     "pegin-outputs")
        self.assertIn("pegin-outputs", PARALLEL_ACTIONS)
        self.assertEqual(resource, {"resource_kind": "pegin_lookup", "resource_key": "bitcoin-outputs"})

    def test_pegin_outputs_conflict_only_with_another_output_lookup(self):
        output = {**job_resources(["inspect-txs", "--txids", "a" * 64], "pegin-outputs"), "case_id": None}
        duplicate = {**job_resources(["inspect-txs", "--txids", "b" * 64], "pegin-outputs"), "case_id": None}
        self.assertTrue(conflicts(output, duplicate))
        self.assertTrue(conflicts(duplicate, output))
        for action, arguments, case in (
                ("pegin-match", ["pegin-match", "--investigations-dir", "/synthetic/cases"], None),
                ("plot", ["plot", "--case", "/synthetic/cases/example"], Path("/synthetic/cases/example")),
                ("shared-trace", ["shared-trace", "--case", "/synthetic/cases/example"], Path("/synthetic/cases/example")),
                ("lookup", ["inspect-txs", "--txids", "c" * 64], None)):
            with self.subTest(action=action):
                other = {**job_resources(arguments, action, case), "case_id": "case" if case else None}
                self.assertFalse(conflicts(output, other))
                self.assertFalse(conflicts(other, output))

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





class BridgeJobResourceTests(unittest.TestCase):
    def test_session_storage_and_network_do_not_change_shared_workspace_lock(self):
        legacy = job_resources(['pegin-match','--investigations-dir','/synthetic/workspace'],'pegin-match')
        for bridge in ('pegin','pegout'):
            for network in ('bitcoin','liquid'):
                args=['bridge-match','--bridge',bridge,'--network',network,'--report-root','--case',
                      '--report-id','--run','--mode','batch','--investigations-dir','/synthetic/workspace']
                current=job_resources(args,'pegin-match')
                self.assertEqual(current,legacy)
                self.assertTrue(conflicts(current,legacy))
        self.assertFalse(conflicts(legacy,{'case_id':'other','resource_kind':'plot'}))

    def test_output_lookup_remains_separate_from_matching_but_excludes_other_lookups(self):
        output=job_resources(['bridge-outputs','--network','liquid','--bridge','pegout'],'pegin-outputs')
        match=job_resources(['bridge-match','--investigations-dir','/synthetic/workspace'],'pegin-match')
        self.assertFalse(conflicts(output,match))
        self.assertTrue(conflicts(output,job_resources(['inspect-txs'],'pegin-outputs')))


if __name__ == "__main__":
    unittest.main()
