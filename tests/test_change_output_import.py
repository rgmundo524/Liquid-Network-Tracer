"""Bulk change assignments are previewed and atomically applied without API calls."""

import fcntl
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from liquid_tracer import change_output_import as imports
from liquid_tracer.change_outputs import set_change_output
from liquid_tracer.cli import main
from liquid_tracer.common import TraceError, save_json
from liquid_tracer.investigations import create_investigation
from liquid_tracer.services import load_services, set_service
from tests.fixtures import A, B, C, D, X
from tests.test_change_outputs import save_archive


class ChangeOutputImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = create_investigation(self.root, "Imported change", seeds=[A + ":0"])

    def apply(self, source, **options):
        preview = imports.preview_import(self.case, source, **options)
        self.assertTrue(preview["valid"], preview["errors"])
        return imports.apply_import(self.case, source, approval_sha256=preview["approval_sha256"], **options)

    def test_csv_normalization_atomic_revision_and_preservation(self):
        set_service(self.case, "SYNTHETIC-exchange", name="Exchange", notes="Retain assessment")
        before = load_services(self.case)
        source = f'TXID,ChangeVout,Notes\n{A.upper()},0,"Reviewed, retain this note"\n{B},1,\n'
        with patch("liquid_tracer.inspection.inspect_transaction", side_effect=AssertionError("No lookup")), \
                patch("liquid_tracer.api.http", side_effect=AssertionError("No API")), \
                patch("liquid_tracer.change_output_import.save_json", wraps=save_json) as save:
            preview = imports.preview_import(self.case, source)
            self.assertEqual(preview["unique_transactions"], 2)
            self.assertEqual(preview["counts"]["add"], 2)
            self.assertEqual(preview["changes"][0]["previous"], None)
            result = self.apply(source)
            self.assertEqual(save.call_count, 1)
        after = load_services(self.case)
        self.assertEqual(result["changed"], 2)
        self.assertEqual(after["revision"], before["revision"] + 1)
        self.assertEqual(after["rules"], before["rules"])
        self.assertEqual(after["history"][:-1], before["history"])
        self.assertEqual(after["history"][-1]["import_id"], result["import_id"])
        self.assertEqual(after["history"][-1]["type"], "change_outputs")
        self.assertEqual(after["change_outputs"][A]["notes"], "Reviewed, retain this note")

    def test_keep_replace_clear_notes_change_and_noop(self):
        self.apply(f"Txid,ChangeVout,Notes\n{A},0,Before\n{B},1,\n")
        source = f"Txid,ChangeVout,Notes\n{A},0,After\n{B},,Clear\n"
        before = (self.case / "services.json").read_bytes()
        result = self.apply(source)
        self.assertEqual((result["changed"], result["counts"]["keep"]), (0, 2))
        self.assertIsNone(result["import_id"])
        self.assertEqual((self.case / "services.json").read_bytes(), before)
        result = self.apply(source, policy="replace")
        self.assertEqual((result["counts"]["replace"], result["counts"]["clear"]), (1, 1))
        self.assertEqual(load_services(self.case)["change_outputs"][A]["notes"], "After")
        self.assertNotIn(B, load_services(self.case)["change_outputs"])
        before = (self.case / "services.json").read_bytes()
        self.assertEqual(self.apply(source, policy="replace")["changed"], 0)
        self.assertEqual((self.case / "services.json").read_bytes(), before)

    def test_json_exact_indexes_identical_duplicates_and_conflicting_duplicates(self):
        source = json.dumps([{"Txid": A.upper(), "ChangeVout": 0}, {"txid": A, "changevout": 0}])
        preview = imports.preview_import(self.case, source)
        self.assertEqual((preview["input_rows"], preview["unique_transactions"], preview["duplicate_rows"]), (2, 1, 1))
        self.assertEqual(self.apply(source)["changed"], 1)
        for value in (True, False, -1, 2**32, "0", 0.0):
            source = json.dumps([{"Txid": B, "ChangeVout": value}])
            with self.subTest(value=value):
                self.assertFalse(imports.preview_import(self.case, source)["valid"])
        source = f"Txid,ChangeVout\n{A},0\n{A.upper()},1\n"
        self.assertFalse(imports.preview_import(self.case, source)["valid"])
        source = f"Txid,ChangeVout,Notes\n{A},0,First\n{A},0,Second\n"
        self.assertFalse(imports.preview_import(self.case, source)["valid"])

    def test_bad_row_or_known_unselectable_output_rejects_whole_batch(self):
        archive = save_archive(self.case)
        evidence = (archive / "trace.json").read_bytes()
        for row in (f"{A},2", f"{C},1", f"{D},0", f"{A},99", "invalid,0", f"{B},-1"):
            source = f"Txid,ChangeVout\n{X},0\n{row}\n"
            with self.subTest(row=row):
                report = imports.preview_import(self.case, source)
                self.assertFalse(report["valid"])
                self.assertIsNone(report["approval_sha256"])
                with self.assertRaises(TraceError):
                    imports.apply_import(self.case, source, approval_sha256="0" * 64)
                self.assertFalse((self.case / "services.json").exists())
        self.assertEqual((archive / "trace.json").read_bytes(), evidence)

    def test_stale_hash_binds_exact_bytes_options_settings_case_and_evidence(self):
        source = f"Txid,ChangeVout\n{A},0\n"
        original = imports.preview_import(self.case, source)["approval_sha256"]
        for text, options in ((source + "\n", {}), ("\ufeff" + source, {}),
                              (source, {"format": "csv"}), (source, {"policy": "replace"})):
            with self.assertRaises(TraceError):
                imports.apply_import(self.case, text, approval_sha256=original, **options)
        other = create_investigation(self.root, "Other case")
        with self.assertRaises(TraceError):
            imports.apply_import(other, source, approval_sha256=original)
        save_archive(self.case)
        with self.assertRaises(TraceError):
            imports.apply_import(self.case, source, approval_sha256=original)
        original = imports.preview_import(self.case, source)["approval_sha256"]
        set_change_output(self.case, B, 0)
        with self.assertRaises(TraceError):
            imports.apply_import(self.case, source, approval_sha256=original)
        original = imports.preview_import(self.case, source)["approval_sha256"]
        settings = load_services(self.case)
        settings["history"].append({"out_of_band": True})
        save_json(self.case / "services.json", settings)
        with self.assertRaises(TraceError):
            imports.apply_import(self.case, source, approval_sha256=original)

    def test_read_only_preview_and_lock_protected_single_save(self):
        source = f"Txid,ChangeVout\n{A},0\n"
        before = sorted(p.name for p in self.case.iterdir())
        preview = imports.preview_import(self.case, source)
        self.assertEqual(sorted(p.name for p in self.case.iterdir()), before)
        with (self.case / "trace.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            with self.assertRaisesRegex(TraceError, "active"):
                imports.apply_import(self.case, source, approval_sha256=preview["approval_sha256"])

        def guarded_save(path, data):
            for filename in ("trace.lock", "case.lock"):
                with (self.case / filename).open("a") as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            save_json(path, data)
        with patch("liquid_tracer.change_output_import.save_json", side_effect=guarded_save) as save:
            self.apply(source)
            self.assertEqual(save.call_count, 1)

    def test_bounded_formats_and_file_input(self):
        source = f"\ufeffTxid,ChangeVout\n{A},0\n"
        path = self.root / "change.csv"
        path.write_bytes(source.encode())
        self.assertEqual(imports.read_import(path), source)
        self.assertTrue(imports.preview_import(self.case, source)["valid"])
        for text in ("", "Txid,ChangeVout\n", "Txid,ChangeVout,Extra\n", "Txid,txID,ChangeVout\n", "{}", "[",
                     '[{"Txid":"' + A + '","ChangeVout":0,"ChangeVout":1}]',
                     "Txid,ChangeVout\n" + (A + ",0\n") * 5001, "x" * (imports.MAX_BYTES + 1)):
            with self.subTest(text=text[:80]), self.assertRaises(TraceError):
                imports.parse_import(text)
        self.assertEqual(imports.parse_import("Txid,ChangeVout\n" + (A + ",0\n") * 5000)["duplicates"], 4999)
        path.write_bytes(b"\xff")
        with self.assertRaisesRegex(TraceError, "UTF-8"):
            imports.read_import(path)
        with self.assertRaises(TraceError):
            imports.preview_import(self.case, source, policy="invalid")
        with self.assertRaises(TraceError):
            imports.preview_import(self.case, source, format="text")

    def test_cli_preview_apply_then_reject_changed_file(self):
        path = self.root / "change.csv"
        path.write_text(f"Txid,ChangeVout\n{A},0\n")
        arguments = ["change-output-import", "--case", str(self.case), "--file", str(path)]
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main(arguments), 0)
        approval = json.loads(stdout.getvalue())["approval_sha256"]
        self.assertFalse((self.case / "services.json").exists())
        path.write_text(f"Txid,ChangeVout\n{A},1\n")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(arguments + ["--approve-plan", approval]), 1)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main(arguments), 0)
        approval = json.loads(stdout.getvalue())["approval_sha256"]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(arguments + ["--approve-plan", approval]), 0)
        self.assertEqual(load_services(self.case)["change_outputs"][A]["vout"], 1)
        path.write_text("Txid,ChangeVout\ninvalid,0\n")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(arguments), 1)


if __name__ == "__main__":
    unittest.main()
