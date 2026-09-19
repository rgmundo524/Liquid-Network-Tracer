"""Current input exports preserve import semantics and never touch saved evidence."""

import copy
import csv
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from liquid_tracer import address_import, change_output_import, name_color_import
from liquid_tracer.cli import main
from liquid_tracer.common import TraceError, save_json
from liquid_tracer.input_export import HEADERS, KINDS, build_input_export, save_input_export
from liquid_tracer.investigations import create_investigation
from liquid_tracer.services import load_services
from tests.fixtures import A, B


class InputExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = create_investigation(self.root, "Input export", seeds=[A + ":0"])

    def settings(self, *, rules=None, colors=None, changes=None):
        settings = load_services(self.case)
        settings.update({"revision": 17, "rules": rules or {}, "name_colors": colors or {},
                         "change_outputs": changes or {}})
        save_json(self.case / "services.json", settings)
        return settings

    def rule(self, address, **fields):
        return {"address": address, "name": "Example", "notes": "", "enabled": True,
                "confidence": "confirmed", "source": "Case research", "observed_at": "2026-09-19",
                "stop_tracing": False, "hop_limit": None, "created_at": "2026-09-19T00:00:00Z",
                "updated_at": "2026-09-19T01:00:00Z", **fields}

    def files(self, result):
        if result["content_type"] == "application/zip":
            with zipfile.ZipFile(io.BytesIO(result["data"])) as archive:
                return {name: archive.read(name) for name in archive.namelist()}
        return {result["filename"]: result["data"]}

    def snapshot(self):
        return {str(path.relative_to(self.case)): path.read_bytes()
                for path in self.case.rglob("*") if path.is_file()}

    def test_attributions_roundtrip_disabled_zero_limit_and_multiline_unicode(self):
        first, second = "SYNTHETIC-Z", "SYNTHETIC-A"
        self.settings(rules={
            first: self.rule(first, name='Café, "Wallet"', notes='Line 1, "quoted"\nLine 2\t漢字',
                             enabled=False, hop_limit=0, stop_tracing=True),
            second: self.rule(second, name="", notes="Retain case-sensitive address"),
        })
        exported = build_input_export(self.case, "attributions")
        self.assertEqual(exported["filename"], "attributions.csv")
        self.assertEqual(exported["rows"], {"attributions": 2})
        self.assertEqual(exported["revision"], 17)
        text = exported["data"].decode("utf-8")
        parsed = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual([row["Address"] for row in parsed], [second, first])
        self.assertEqual(parsed[1]["enabled"], "false")
        self.assertEqual(parsed[1]["hop_limit"], "0")
        self.assertEqual(parsed[1]["notes"], 'Line 1, "quoted"\nLine 2\t漢字')
        self.assertEqual(parsed[0]["hop_limit"], "")
        preview = address_import.preview_import(self.case, text, format="csv", policy="replace")
        self.assertTrue(preview["valid"], preview["errors"])
        self.assertEqual(preview["counts"]["unchanged"], 2)
        fresh = create_investigation(self.root, "Fresh input", seeds=[A + ":0"])
        preview = address_import.preview_import(fresh, text, format="csv")
        self.assertEqual(preview["counts"]["add"], 2)
        restored = {item["rule"]["address"]: item["rule"] for item in preview["changes"]}
        self.assertFalse(restored[first]["enabled"])
        self.assertEqual(restored[first]["hop_limit"], 0)

    def test_legacy_fields_export_the_same_semantics_without_migrating_settings(self):
        address = "SYNTHETIC-legacy"
        rule = self.rule(address)
        for field in ("notes", "source", "observed_at", "stop_tracing", "hop_limit"):
            rule.pop(field)
        rule.update({"rationale": "Legacy evidence\nRetained", "classification": "label", "confidence": "candidate"})
        self.settings(rules={address: rule})
        before = self.snapshot()
        exported = build_input_export(self.case, "attributions")
        preview = address_import.preview_import(self.case, exported["data"].decode(), format="csv")
        self.assertTrue(preview["valid"], preview["errors"])
        self.assertEqual(preview["counts"]["unchanged"], 1)
        restored = preview["changes"][0]["rule"]
        self.assertEqual((restored["confidence"], restored["source"], restored["stop_tracing"]),
                         ("suspected", "Investigator designation", False))
        self.assertEqual(restored["notes"], rule["rationale"])
        self.assertEqual(before, self.snapshot())

    def test_name_colors_include_unused_assignments_and_choose_deterministic_spelling(self):
        rules = {"SYNTHETIC-1": self.rule("SYNTHETIC-1", name="éXCHANGE"),
                 "SYNTHETIC-2": self.rule("SYNTHETIC-2", name="Éxchange", enabled=False),
                 "SYNTHETIC-3": self.rule("SYNTHETIC-3", name="No assignment")}
        self.settings(rules=rules, colors={"éxchange": "#abcdef", "unused name": "#000000"})
        exported = build_input_export(self.case, "name-colors")
        text = exported["data"].decode()
        self.assertEqual(list(csv.reader(io.StringIO(text))),
                         [["Name", "Color"], ["unused name", "#000000"], ["Éxchange", "#abcdef"]])
        preview = name_color_import.preview_import(self.case, text, format="csv")
        self.assertTrue(preview["valid"], preview["errors"])
        self.assertEqual(preview["counts"]["unchanged"], 2)
        # Fresh cases must first contain matching attribution names. Unused assignments
        # stay in the export so the investigator can restore those names too.
        fresh = create_investigation(self.root, "No names", seeds=[A + ":0"])
        preview = name_color_import.preview_import(fresh, text, format="csv")
        self.assertFalse(preview["valid"])
        self.assertTrue(all("Unknown attribution name" in error["message"] for error in preview["errors"]))

    def test_change_outputs_roundtrip_zero_vout_and_notes_without_lookups(self):
        self.settings(changes={B: {"vout": 4294967295, "notes": "Future transaction", "updated_at": "2026-09-19"},
                               A: {"vout": 0, "notes": '説明, "reviewed"\nAnother line', "updated_at": "2026-09-19"}})
        with patch("liquid_tracer.api.http", side_effect=AssertionError("No network")):
            exported = build_input_export(self.case, "change-outputs")
            preview = change_output_import.preview_import(self.case, exported["data"].decode(), format="csv")
        self.assertTrue(preview["valid"], preview["errors"])
        self.assertEqual(preview["counts"]["unchanged"], 2)
        self.assertEqual([row["txid"] for row in preview["changes"]], sorted([A, B]))
        self.assertEqual(next(row["vout"] for row in preview["changes"] if row["txid"] == A), 0)

    def test_all_uses_one_snapshot_is_deterministic_and_writes_nothing(self):
        settings = self.settings(rules={"SYNTHETIC-1": self.rule("SYNTHETIC-1")}, colors={"example": "#123456"})
        original = copy.deepcopy(settings)
        before = self.snapshot()
        with patch("liquid_tracer.input_export.load_services", return_value=settings) as load, \
                patch("liquid_tracer.api.http", side_effect=AssertionError("No network")):
            exported = build_input_export(self.case)
        load.assert_called_once_with(self.case)
        self.assertEqual(settings, original)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(exported["filename"], "input-csvs.zip")
        self.assertEqual(exported["rows"], {"attributions": 1, "name-colors": 1, "change-outputs": 0})
        self.assertEqual(exported["parts"], 3)
        self.assertEqual(list(self.files(exported)), [kind + ".csv" for kind in KINDS])
        self.assertEqual(build_input_export(self.case)["data"], exported["data"])

    def test_empty_case_exports_header_only_csvs_without_creating_settings(self):
        before = self.snapshot()
        for kind in KINDS:
            with self.subTest(kind=kind):
                exported = build_input_export(self.case, kind)
                self.assertEqual(exported["rows"], {kind: 0})
                self.assertEqual(list(csv.reader(io.StringIO(exported["data"].decode()))), [list(HEADERS[kind])])
        self.assertEqual(before, self.snapshot())

    def test_row_count_splits_all_records_into_reimportable_parts(self):
        colors = {f"name {index:05d}": "#123456" for index in range(5001)}
        self.settings(colors=colors)
        exported = build_input_export(self.case, "name-colors")
        self.assertEqual(exported["filename"], "name-colors.zip")
        files = self.files(exported)
        self.assertEqual(list(files), ["name-colors-part-001.csv", "name-colors-part-002.csv"])
        seen = []
        for content in files.values():
            preview = name_color_import.preview_import(self.case, content.decode(), format="csv")
            self.assertTrue(preview["valid"], preview["errors"])
            self.assertLessEqual(preview["input_rows"], name_color_import.MAX_ROWS)
            seen.extend(row["key"] for row in preview["changes"])
        self.assertEqual(seen, sorted(colors))
        self.assertEqual(exported["rows"], {"name-colors": 5001})

    def test_utf8_byte_limit_splits_complete_multiline_records_without_loss(self):
        rules = {f"SYNTHETIC-{index:03d}": self.rule(f"SYNTHETIC-{index:03d}", notes="漢" * 3900 + '\n,"x"')
                 for index in range(100)}
        self.settings(rules=rules)
        exported = build_input_export(self.case, "attributions")
        self.assertEqual(exported["filename"], "attributions.zip")
        self.assertEqual(exported["parts"], 3)
        seen = []
        for content in self.files(exported).values():
            self.assertLessEqual(len(content), address_import.MAX_BYTES)
            preview = address_import.preview_import(self.case, content.decode(), format="csv")
            self.assertTrue(preview["valid"], preview["errors"])
            self.assertEqual(preview["counts"]["unchanged"], preview["input_rows"])
            seen.extend(row["rule"]["address"] for row in preview["changes"])
        self.assertEqual(seen, sorted(rules))

    def test_exact_byte_limit_includes_header_and_does_not_split_early(self):
        self.settings(colors={"aa": "#123456", "bb": "#abcdef"})
        unsplit = build_input_export(self.case, "name-colors")
        with patch.object(name_color_import, "MAX_BYTES", len(unsplit["data"])):
            self.assertEqual(build_input_export(self.case, "name-colors")["parts"], 1)
        with patch.object(name_color_import, "MAX_BYTES", len(unsplit["data"]) - 1):
            self.assertEqual(build_input_export(self.case, "name-colors")["parts"], 2)

    def test_save_creates_unique_outputs_and_refuses_existing_or_archived_paths(self):
        self.settings(colors={"example": "#123456"})
        before = self.snapshot()
        first = save_input_export(self.case, "name-colors")
        second = save_input_export(self.case, "name-colors")
        self.assertNotEqual(first["directory"], second["directory"])
        self.assertEqual(Path(first["path"]).read_bytes(), build_input_export(self.case, "name-colors")["data"])
        self.assertEqual(Path(first["directory"]).parent, self.case / "exports")
        for path, content in before.items():
            self.assertEqual((self.case / path).read_bytes(), content)
        with self.assertRaisesRegex(TraceError, "already exists"):
            save_input_export(self.case, out=first["directory"])
        archive = self.case / "runs" / "saved"
        archive.mkdir(parents=True)
        with self.assertRaisesRegex(TraceError, "outside runs"):
            save_input_export(self.case, out=archive / "new")
        link = self.root / "archive-alias"
        link.symlink_to(archive, target_is_directory=True)
        with self.assertRaisesRegex(TraceError, "outside runs"):
            save_input_export(self.case, out=link / "new")
        dangling = self.root / "dangling"
        dangling.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(TraceError, "already exists"):
            save_input_export(self.case, out=dangling)
        self.assertEqual(list(archive.iterdir()), [])

    def test_cli_saves_current_inputs_without_a_run_and_returns_json(self):
        target = self.root / "download"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["input-export", "--case", str(self.case), "--kind", "all", "--out", str(target)])
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["directory"], str(target))
        self.assertEqual(report["rows"], {kind: 0 for kind in KINDS})
        self.assertTrue(Path(report["path"]).is_file())
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["input-export", "--case", str(self.case), "--out", str(target)]), 1)

    def test_invalid_kind_never_creates_output_or_loads_settings(self):
        for kind in ("trace", "../secret", None, []):
            with self.subTest(kind=kind), patch("liquid_tracer.input_export.load_services") as load:
                with self.assertRaises(TraceError):
                    build_input_export(self.case, kind)
                load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
