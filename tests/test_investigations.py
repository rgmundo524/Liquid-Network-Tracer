import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.cli import main, verify_export
from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.investigations import (
    DEFAULTS, create_investigation, list_investigations, load_settings,
    read_case, save_settings, update_case, validate_settings,
)


class InvestigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "cases"
        self.project = Path(__file__).resolve().parents[1]

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

    def test_invalid_fee_booleans_and_boolean_limits_do_not_write(self):
        case = create_investigation(self.root, "Strict settings")
        save_settings(self.root, {})
        original_case = (case / "case.json").read_bytes()
        original_global = (self.root / "settings.json").read_bytes()
        for values in ({"include_fees": 0}, {"include_fees": 1}, {"include_fees": "false"},
                       {"include_fees": None}, {"include_fees": []}, {"hops": True},
                       {"max_seconds": False}):
            with self.subTest(values=values):
                with self.assertRaises(TraceError):
                    save_settings(self.root, values)
                with self.assertRaises(TraceError):
                    update_case(case, {"run_defaults": values})
        self.assertEqual((case / "case.json").read_bytes(), original_case)
        self.assertEqual((self.root / "settings.json").read_bytes(), original_global)

    def test_run_snapshots_preserve_board_history_when_case_settings_change(self):
        case = create_investigation(self.root, "Original name", board="FIRST=")
        base = ["trace", "--case", str(case), "--fixture", str(self.project / "examples/demo-api.json")]
        with contextlib.redirect_stdout(io.StringIO()):
            status = main(base + ["--seeds-file", str(self.project / "examples/demo-seeds.txt"), "--hops", "1"])
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
