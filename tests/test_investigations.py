import contextlib
import io
import fcntl
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.cli import main, verify_export
from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.investigations import (
    DEFAULTS, create_investigation, list_investigations, load_settings,
    read_case, save_plot_settings, save_settings, update_case, validate_settings,
)


class InvestigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "cases"
        self.project = Path(__file__).resolve().parents[1]

    def test_plot_settings_merge_preserves_latest_budgets_metadata_and_other_investigations(self):
        save_settings(self.root, {"hops": 8, "connector_style": "curved"})
        case = create_investigation(self.root, "Plot settings", board="ORIGINAL=",
            seeds=["a" * 64 + ":2"], run_defaults={"hops": 4, "max_requests": 456, "max_transactions": 87})
        other = create_investigation(self.root, "Separate case")
        metadata = read_case(case)
        metadata.update(latest_run="a" * 16, analyst_note="Preserve this note")
        save_json(case / "case.json", metadata)
        evidence = case / "runs" / metadata["latest_run"]
        evidence.mkdir(parents=True)
        save_json(evidence / "trace.json", {"saved_evidence": True})
        protected = {path: path.read_bytes() for path in
                     (self.root / "settings.json", other / "case.json", evidence / "trace.json")}
        settings = {"layout_attempts": 31, "connector_style": "elbowed", "include_fees": True,
                    "color_attribution_arrows": True, "group_context_inputs": True,
                    "center_name": " Treasury ", "hub_addresses": ["H" * 34, "G" * 34, "H" * 34]}
        saved = save_plot_settings(case, settings)
        expected = {**metadata, "run_defaults": {**metadata["run_defaults"], **settings,
                    "center_name": "Treasury", "hub_addresses": ["G" * 34, "H" * 34]}}
        self.assertEqual(saved, expected)
        self.assertEqual(read_case(Path(str(case))), expected)
        # Another session changes unrelated settings before a later partial save.
        latest_defaults = {**saved["run_defaults"], "hops": 10, "max_requests": 987}
        update_case(case, {"name": "Latest name", "miro_board": "LATEST=", "run_defaults": latest_defaults})
        partial = save_plot_settings(case, {"include_fees": False})
        self.assertEqual(partial["run_defaults"], {**latest_defaults, "include_fees": False})
        self.assertEqual((partial["name"], partial["miro_board"], partial["blockchain"]),
                         ("Latest name", "LATEST=", "liquid"))
        self.assertEqual((partial["seeds"], partial["latest_run"], partial["analyst_note"]),
                         (metadata["seeds"], metadata["latest_run"], metadata["analyst_note"]))
        self.assertEqual(protected, {path: path.read_bytes() for path in protected})

    def test_plot_settings_reject_nonlayout_fields_and_invalid_values_without_write(self):
        case = create_investigation(self.root, "Strict plot settings")
        before = (case / "case.json").read_bytes()
        invalid = [None, [], True, {"hops": 10}, {"max_new_items": 200}, {"name": "Changed"},
                   {"blockchain": "liquid"}, {"miro_board": "OTHER="}, {"layout_attempts": None},
                   {"layout_attempts": 0}, {"include_fees": "false"}, {"color_attribution_arrows": 1},
                   {"group_context_inputs": None}, {"center_name": "bad\nname"},
                   {"hub_addresses": ["short"]}, {"connector_style": "unknown"}]
        for settings in invalid:
            with self.subTest(settings=settings), self.assertRaises(TraceError):
                save_plot_settings(case, settings)
            self.assertEqual((case / "case.json").read_bytes(), before)
        self.assertEqual(save_plot_settings(case, {}), read_case(case))
        self.assertEqual((case / "case.json").read_bytes(), before)

    def test_plot_settings_do_not_wait_on_active_collection_or_shared_preview_locks(self):
        case = create_investigation(self.root, "Busy plot settings")
        before = (case / "case.json").read_bytes()
        for filename, mode in (("trace.lock", fcntl.LOCK_EX), ("case.lock", fcntl.LOCK_SH),
                               ("case.lock", fcntl.LOCK_EX)):
            with self.subTest(lock=filename, mode=mode), (case / filename).open("a") as lock:
                fcntl.flock(lock, mode | fcntl.LOCK_NB)
                with self.assertRaisesRegex(TraceError, "operation is active"):
                    save_plot_settings(case, {"include_fees": True})
            self.assertEqual((case / "case.json").read_bytes(), before)
        self.assertTrue(save_plot_settings(case, {"include_fees": True})["run_defaults"]["include_fees"])

    def test_blockchain_is_persisted_for_new_cases_and_survives_updates(self):
        for options in ({}, {"blockchain": "liquid"}):
            with self.subTest(options=options):
                case = create_investigation(self.root, "Liquid case", **options)
                self.assertEqual(read_json(case / "case.json")["blockchain"], "liquid")
                self.assertEqual(read_case(Path(str(case)))["blockchain"], "liquid")
                update_case(case, {"name": "Renamed Liquid case"})
                self.assertEqual(read_case(case)["blockchain"], "liquid")
                before = (case / "case.json").read_bytes()
                with self.assertRaises(TraceError):
                    update_case(case, {"blockchain": "bitcoin"})
                self.assertEqual((case / "case.json").read_bytes(), before)

    def test_legacy_blockchain_defaults_on_read_without_rewriting_case_or_evidence(self):
        case = create_investigation(self.root, "Legacy Liquid", fixture=self.project / "tests/data/synthetic-api.json")
        metadata = read_json(case / "case.json")
        metadata.pop("blockchain")
        save_json(case / "case.json", metadata)
        evidence = case / "runs" / ("a" * 16)
        evidence.mkdir(parents=True)
        save_json(evidence / "trace.json", {"legacy_evidence": True})
        before = {str(path.relative_to(case)): path.read_bytes() for path in case.rglob("*") if path.is_file()}
        readback = read_case(case)
        self.assertEqual(readback["blockchain"], "liquid")
        self.assertEqual(readback["fixture"], metadata["fixture"])
        self.assertEqual(dict(list_investigations(self.root))[case]["blockchain"], "liquid")
        self.assertEqual(before, {str(path.relative_to(case)): path.read_bytes() for path in case.rglob("*") if path.is_file()})

    def test_unsupported_or_malformed_blockchains_fail_before_creating_case(self):
        for blockchain in ("bitcoin", "ethereum", "fixture", "live", "Liquid", " liquid ", "", None, True, [], {}):
            with self.subTest(blockchain=blockchain), self.assertRaisesRegex(TraceError, "Only Liquid"):
                create_investigation(self.root, "Unsupported chain", blockchain=blockchain)
            self.assertFalse(self.root.exists())

    def test_invalid_stored_blockchain_is_rejected_without_rewriting_metadata(self):
        case = create_investigation(self.root, "Invalid chain")
        for blockchain in ("bitcoin", None, [], {}):
            with self.subTest(blockchain=blockchain):
                metadata = read_json(case / "case.json")
                metadata["blockchain"] = blockchain
                save_json(case / "case.json", metadata)
                before = (case / "case.json").read_bytes()
                with self.assertRaisesRegex(TraceError, "Only Liquid"):
                    read_case(case)
                self.assertIn("Only Liquid", list_investigations(self.root)[0][1]["error"])
                self.assertEqual((case / "case.json").read_bytes(), before)

    def test_distinct_investigations_survive_restart_and_preserve_metadata(self):
        first = create_investigation(self.root, "The same case", board="https://miro.com/app/board/FIRST=/")
        second = create_investigation(self.root, "The same case", board="SECOND=")
        self.assertNotEqual(first, second)
        metadata = read_case(first)
        save_json(first / "case.json", {**metadata, "latest_run": "a" * 16, "analyst_note": "Keep me"})
        update_case(first, {"name": "Renamed investigation", "miro_board": "UPDATED="})
        cases = dict(list_investigations(Path(str(self.root))))
        self.assertEqual(cases[first]["case_id"], metadata["case_id"])
        self.assertEqual(cases[first]["latest_run"], "a" * 16)
        self.assertEqual(cases[first]["analyst_note"], "Keep me")
        self.assertEqual(cases[second]["miro_board"], "SECOND=")

    def test_discovery_is_read_only_and_corrupt_cases_are_visible(self):
        self.assertEqual(list_investigations(self.root), [])
        self.assertEqual(load_settings(self.root), DEFAULTS)
        self.assertFalse(self.root.exists())
        case = create_investigation(self.root, "Damaged case")
        (case / "case.json").write_text("invalid json")
        self.assertIn("error", list_investigations(self.root)[0][1])

    def test_settings_persist_and_reject_invalid_values_without_writes(self):
        expected = {**DEFAULTS, "max_requests": 7}
        save_settings(self.root, expected)
        self.assertEqual(load_settings(Path(str(self.root))), expected)
        original = (self.root / "settings.json").read_bytes()
        for values in ({"max_seconds": float("nan")}, {"hops": 1.5}, {"max_requests": 0}, {"MIRO_ACCESS_TOKEN": "never-store"}):
            with self.subTest(values=values), self.assertRaises(TraceError):
                save_settings(self.root, values)
        self.assertEqual((self.root / "settings.json").read_bytes(), original)

    def test_invalid_creation_does_not_create_case_and_name_cannot_escape_root(self):
        for values in ({"name": ""}, {"name": "case", "board": "https://other.example/"},
                       {"name": "case", "seeds": ["invalid"]}):
            with self.subTest(values=values), self.assertRaises(TraceError):
                create_investigation(self.root, **values)
        self.assertFalse(self.root.exists())
        case = create_investigation(self.root, "../../escape")
        self.assertEqual(case.parent, self.root)
        with self.assertRaises(TraceError):
            update_case(case, {"case_id": "f" * 32})

    def test_fee_setting_is_boolean_and_persists_without_changing_existing_cases(self):
        case = create_investigation(self.root, "Original defaults")
        self.assertIs(read_case(case)["run_defaults"]["include_fees"], False)
        original = (case / "case.json").read_bytes()
        save_settings(self.root, {"include_fees": True})
        self.assertIs(load_settings(self.root)["include_fees"], True)
        self.assertEqual((case / "case.json").read_bytes(), original)
        new_case = create_investigation(self.root, "New defaults", run_defaults=load_settings(self.root))
        self.assertIs(read_case(new_case)["run_defaults"]["include_fees"], True)
        update_case(case, {"run_defaults": {"include_fees": True}})
        self.assertIs(read_case(Path(str(case)))["run_defaults"]["include_fees"], True)

    def test_legacy_fee_setting_uses_false_without_writing_migration(self):
        case = create_investigation(self.root, "Legacy case")
        metadata = read_case(case)
        metadata["run_defaults"].pop("include_fees")
        save_json(case / "case.json", metadata)
        original = (case / "case.json").read_bytes()
        save_settings(self.root, {"include_fees": True})
        self.assertIs(validate_settings(read_case(case)["run_defaults"])["include_fees"], False)
        self.assertEqual((case / "case.json").read_bytes(), original)

    def test_context_grouping_is_opt_in_and_case_defaults_survive_global_changes(self):
        case = create_investigation(self.root, "Existing case")
        metadata = read_case(case)
        self.assertIs(metadata["run_defaults"].pop("group_context_inputs"), False)
        save_json(case / "case.json", metadata)
        original = (case / "case.json").read_bytes()
        save_settings(self.root, {"group_context_inputs": True})
        self.assertIs(validate_settings(read_case(case)["run_defaults"])["group_context_inputs"], False)
        self.assertEqual((case / "case.json").read_bytes(), original)
        new_case = create_investigation(self.root, "New case", run_defaults=load_settings(self.root))
        self.assertIs(read_case(new_case)["run_defaults"]["group_context_inputs"], True)
        update_case(case, {"run_defaults": {"group_context_inputs": True}})
        self.assertIs(read_case(case)["run_defaults"]["group_context_inputs"], True)

    def test_attribution_arrows_are_opt_in_and_persist_per_investigation(self):
        case = create_investigation(self.root, "Existing case")
        metadata = read_case(case)
        self.assertIs(metadata["run_defaults"].pop("color_attribution_arrows"), False)
        save_json(case / "case.json", metadata)
        original = (case / "case.json").read_bytes()
        save_settings(self.root, {"color_attribution_arrows": True})
        self.assertIs(validate_settings(read_case(case)["run_defaults"])["color_attribution_arrows"], False)
        self.assertEqual((case / "case.json").read_bytes(), original)
        new_case = create_investigation(self.root, "Named arrows", run_defaults=load_settings(self.root))
        self.assertIs(read_case(new_case)["run_defaults"]["color_attribution_arrows"], True)
        update_case(case, {"run_defaults": {"color_attribution_arrows": True}})
        self.assertIs(read_case(case)["run_defaults"]["color_attribution_arrows"], True)
        update_case(case, {"run_defaults": {"color_attribution_arrows": False}})
        self.assertIs(read_case(case)["run_defaults"]["color_attribution_arrows"], False)
        self.assertIs(load_settings(self.root)["color_attribution_arrows"], True)

    def test_layout_attempts_default_persist_and_legacy_cases_keep_builtin_default(self):
        self.assertEqual(DEFAULTS["layout_attempts"], 25)
        self.assertEqual(validate_settings({})["layout_attempts"], 25)
        case = create_investigation(self.root, "Legacy layout")
        metadata = read_case(case)
        metadata["run_defaults"].pop("layout_attempts")
        save_json(case / "case.json", metadata)
        original = (case / "case.json").read_bytes()
        save_settings(self.root, {"layout_attempts": 75})
        self.assertEqual(load_settings(self.root)["layout_attempts"], 75)
        self.assertEqual(validate_settings(read_case(case)["run_defaults"])["layout_attempts"], 25)
        self.assertEqual((case / "case.json").read_bytes(), original)
        created = create_investigation(self.root, "Expanded search", run_defaults=load_settings(self.root))
        self.assertEqual(read_case(created)["run_defaults"]["layout_attempts"], 75)
        for attempts in (1, 1000):
            update_case(case, {"run_defaults": {"layout_attempts": attempts}})
            self.assertEqual(read_case(case)["run_defaults"]["layout_attempts"], attempts)

    def test_center_name_is_opt_in_and_preserves_display_spelling_per_case(self):
        case = create_investigation(self.root, "Existing case")
        metadata = read_case(case)
        self.assertEqual(metadata["run_defaults"].pop("center_name"), "")
        save_json(case / "case.json", metadata)
        original = (case / "case.json").read_bytes()
        save_settings(self.root, {"center_name": "  Treasury Group  "})
        self.assertEqual(load_settings(self.root)["center_name"], "Treasury Group")
        self.assertEqual(validate_settings(read_case(case)["run_defaults"])["center_name"], "")
        self.assertEqual((case / "case.json").read_bytes(), original)
        new_case = create_investigation(self.root, "Centered case", run_defaults=load_settings(self.root))
        self.assertEqual(read_case(new_case)["run_defaults"]["center_name"], "Treasury Group")
        update_case(case, {"run_defaults": {"center_name": "  Other Group "}})
        self.assertEqual(read_case(case)["run_defaults"]["center_name"], "Other Group")
        update_case(case, {"run_defaults": {"center_name": "  "}})
        self.assertEqual(read_case(case)["run_defaults"]["center_name"], "")
        self.assertEqual(load_settings(self.root)["center_name"], "Treasury Group")

    def test_invalid_center_names_do_not_change_settings_or_case(self):
        case = create_investigation(self.root, "Case")
        save_settings(self.root, {"center_name": "Treasury"})
        settings_before = (self.root / "settings.json").read_bytes()
        case_before = (case / "case.json").read_bytes()
        for value in (None, False, 5, [], {}, "a" * 121, "Treasury\nGroup", "Group\x00", "a\x7fb"):
            with self.subTest(value=value):
                with self.assertRaises(TraceError):
                    save_settings(self.root, {"center_name": value})
                with self.assertRaises(TraceError):
                    update_case(case, {"run_defaults": {"center_name": value}})
        self.assertEqual((self.root / "settings.json").read_bytes(), settings_before)
        self.assertEqual((case / "case.json").read_bytes(), case_before)

    def test_invalid_layout_attempts_do_not_change_saved_settings(self):
        case = create_investigation(self.root, "Strict layout")
        save_settings(self.root, {})
        original_case = (case / "case.json").read_bytes()
        original_global = (self.root / "settings.json").read_bytes()
        for value in (None, True, False, 0, -1, 1001, 25.0, 1.5, "25", [], {}, float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(TraceError):
                    save_settings(self.root, {"layout_attempts": value})
                with self.assertRaises(TraceError):
                    update_case(case, {"run_defaults": {"layout_attempts": value}})
        self.assertEqual((case / "case.json").read_bytes(), original_case)
        self.assertEqual((self.root / "settings.json").read_bytes(), original_global)

    def test_invalid_fee_booleans_and_boolean_limits_do_not_write(self):
        case = create_investigation(self.root, "Strict settings")
        save_settings(self.root, {})
        original_case = (case / "case.json").read_bytes()
        original_global = (self.root / "settings.json").read_bytes()
        for values in ({"include_fees": 0}, {"include_fees": 1}, {"include_fees": "false"},
                       {"include_fees": None}, {"include_fees": []}, {"hops": True},
                       {"group_context_inputs": 0}, {"group_context_inputs": 1},
                       {"group_context_inputs": "false"}, {"group_context_inputs": None},
                       {"color_attribution_arrows": 0}, {"color_attribution_arrows": 1},
                       {"color_attribution_arrows": "false"}, {"color_attribution_arrows": None},
                       {"max_seconds": False}):
            with self.subTest(values=values):
                with self.assertRaises(TraceError):
                    save_settings(self.root, values)
                with self.assertRaises(TraceError):
                    update_case(case, {"run_defaults": values})
        self.assertEqual((case / "case.json").read_bytes(), original_case)
        self.assertEqual((self.root / "settings.json").read_bytes(), original_global)

    def test_hub_addresses_are_normalized_without_changing_case_or_evidence(self):
        first, second = "G" + "a" * 33, "H" + "b" * 33
        supplied = [" " + second + " ", first, second]
        case = create_investigation(self.root, "Hubs", run_defaults={"hub_addresses": supplied})
        self.assertEqual(read_case(case)["run_defaults"]["hub_addresses"], [first, second])
        self.assertEqual(supplied, [" " + second + " ", first, second])
        self.assertEqual(validate_settings({"hub_addresses": [first, first.lower()]})["hub_addresses"],
                         [first, first.lower()])
        save_settings(self.root, {"hub_addresses": [second]})
        self.assertEqual(read_case(case)["run_defaults"]["hub_addresses"], [first, second])
        legacy = validate_settings({})
        legacy["hub_addresses"].append(first)
        self.assertEqual(DEFAULTS["hub_addresses"], [])
        self.assertEqual(validate_settings({})["hub_addresses"], [])

    def test_invalid_hub_settings_do_not_write(self):
        case = create_investigation(self.root, "Strict hubs")
        save_settings(self.root, {})
        original_case = (case / "case.json").read_bytes()
        original_global = (self.root / "settings.json").read_bytes()
        for value in (None, True, "Ga" * 17, {}, [1], [None], [""], ["short"],
                      ["x" * 201], ["https://example.test/address"], ["G" * 34 + ":0"], ["G" * 20 + "…"]):
            with self.subTest(value=value), self.assertRaises(TraceError):
                save_settings(self.root, {"hub_addresses": value})
            with self.subTest(value=value), self.assertRaises(TraceError):
                update_case(case, {"run_defaults": {"hub_addresses": value}})
        self.assertEqual((case / "case.json").read_bytes(), original_case)
        self.assertEqual((self.root / "settings.json").read_bytes(), original_global)

    def test_run_snapshots_preserve_board_history_when_case_settings_change(self):
        case = create_investigation(self.root, "Original name", board="FIRST=")
        base = ["trace", "--case", str(case), "--fixture", str(self.project / "tests/data/synthetic-api.json")]
        with contextlib.redirect_stdout(io.StringIO()):
            status = main(base + ["--seeds-file", str(self.project / "tests/data/synthetic-seeds.txt"), "--hops", "1"])
        self.assertEqual(status, 0)
        first_dir = case / "runs" / read_case(case)["latest_run"]
        original = (first_dir / "investigation.json").read_bytes()
        self.assertEqual(read_json(first_dir / "investigation.json")["miro_board"], "FIRST=")
        original_run = {path.relative_to(first_dir): path.read_bytes() for path in first_dir.rglob("*") if path.is_file()}
        latest = read_case(case)["latest_run"]
        update_case(case, {"name": "Updated name", "miro_board": "SECOND=", "run_defaults": {"include_fees": True}})
        self.assertEqual(read_case(case)["latest_run"], latest)
        self.assertEqual(original_run, {path.relative_to(first_dir): path.read_bytes()
                                       for path in first_dir.rglob("*") if path.is_file()})
        with contextlib.redirect_stdout(io.StringIO()):
            status = main(base + ["--resume", "latest", "--additional-hops", "1"])
        self.assertEqual(status, 0)
        second_dir = case / "runs" / read_case(case)["latest_run"]
        self.assertEqual((first_dir / "investigation.json").read_bytes(), original)
        snapshot = read_json(second_dir / "investigation.json")
        self.assertEqual((snapshot["name"], snapshot["miro_board"]), ("Updated name", "SECOND="))
        self.assertEqual(snapshot["parent_run"], first_dir.name)
        verify_export(first_dir)
        verify_export(second_dir)


if __name__ == "__main__":
    unittest.main()
